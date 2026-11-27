from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.i18n_msg import lang_of, t
from app.core.security import create_access_token, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, UserInfo
from pydantic import BaseModel, Field
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------- 登录安全矩阵：SVG 验证码 + TOTP 二步 ----------------
_CAPTCHA: dict = {}   # id -> (code, expire_ts)

# ---------------- 滑块拼图验证码 ----------------
# 融合口径：与 SVG 验证码共用 CAPTCHA_ENABLED 单开关、登录单校验点（二选一，均一次性），
# 不另起开关体系。拼图纯前端生成（seed 派生缺口位），后端只管一次性票据：
# challenge 验证即焚 + ticket 登录即焚 + 过期拒 + 错位拒——防脚本爆破登录接口。
_SLIDER: dict = {}          # challenge_id -> (expected_x, expire_ts)
_SLIDER_TICKETS: dict = {}  # ticket -> expire_ts
SLIDER_W, SLIDER_H, SLIDER_PIECE = 320, 160, 48
SLIDER_MIN_X, SLIDER_TOLERANCE = 30, 6
# 缺口位派生（前后端同一公式，见 frontend/src/components/SlideCaptcha.vue）：
# expected_x = SLIDER_MIN_X + seed % (SLIDER_W - SLIDER_PIECE - 2 * SLIDER_MIN_X)
SLIDER_RANGE = SLIDER_W - SLIDER_PIECE - 2 * SLIDER_MIN_X


def _totp(secret_b32: str, at: float = 0) -> str:
    """TOTP（RFC 6238，6 位/30s）——零依赖实现。"""
    import base64
    import hmac
    import struct
    import time

    key = base64.b32decode(secret_b32)
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


@router.get("/captcha", summary="获取图形验证码")
def get_captcha():
    """SVG 图形验证码（CAPTCHA_ENABLED=true 时登录必填；关闭时返回 enabled=false）。"""
    import random
    import time as _t

    from app.core.config import settings

    if not getattr(settings, "CAPTCHA_ENABLED", False):
        return {"enabled": False}
    code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=4))
    cid = f"c{_t.time():.3f}{random.randint(100, 999)}"
    _CAPTCHA[cid] = (code, _t.time() + 300)
    # 清过期
    for k in [k for k, v in _CAPTCHA.items() if v[1] < _t.time()]:
        _CAPTCHA.pop(k, None)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">'
           f'<rect width="120" height="40" fill="#eef2f7"/>'
           f'<text x="60" y="28" text-anchor="middle" font-size="24" font-family="monospace" '
           f'fill="#3b82f6" transform="rotate(-4 60 20)">{code}</text></svg>')
    return {"enabled": True, "id": cid, "svg": svg}


@router.get("/slider/challenge", summary="获取滑块验证挑战")
def slider_challenge():
    """滑块拼图挑战（CAPTCHA_ENABLED=true 时登录用；关闭时 enabled=false）。

    拼图本体纯前端按 seed 生成，服务端只存缺口位用于对齐校验；
    challenge 一次性（验证即焚），5 分钟过期。
    """
    import random
    import secrets
    import time as _t

    from app.core.config import settings

    if not getattr(settings, "CAPTCHA_ENABLED", False):
        return {"enabled": False}
    seed = random.randint(10 ** 6, 10 ** 9)
    expected_x = SLIDER_MIN_X + seed % SLIDER_RANGE
    cid = "s" + secrets.token_urlsafe(16)
    _SLIDER[cid] = (expected_x, _t.time() + 300)
    for k in [k for k, v in _SLIDER.items() if v[1] < _t.time()]:
        _SLIDER.pop(k, None)
    return {
        "enabled": True, "challenge_id": cid, "seed": seed,
        "width": SLIDER_W, "height": SLIDER_H, "piece": SLIDER_PIECE,
        "min_x": SLIDER_MIN_X, "range": SLIDER_RANGE, "tolerance": SLIDER_TOLERANCE,
    }


class SliderVerifyIn(BaseModel):
    challenge_id: str
    offset: int = Field(description="拼图块最终 X 偏移（前端滑动松手位置）")


@router.post("/slider/verify", summary="校验滑块并签发登录票据")
def slider_verify(body: SliderVerifyIn, request: Request):
    """校验滑块对齐并签发一次性登录票据（5 分钟过期，登录时即焚）。"""
    import secrets
    import time as _t

    from app.core.config import settings

    if not getattr(settings, "CAPTCHA_ENABLED", False):
        return {"enabled": False, "ok": True, "ticket": ""}
    ent = _SLIDER.pop(body.challenge_id, None)   # 一次性：无论成败即焚，错了换新挑战
    if not ent or ent[1] < _t.time():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("auth.slider_expired", lang_of(request)))
    if abs(int(body.offset) - ent[0]) > SLIDER_TOLERANCE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("auth.slider_misaligned", lang_of(request)))
    ticket = "st" + secrets.token_urlsafe(16)
    _SLIDER_TICKETS[ticket] = _t.time() + 300
    for k in [k for k, v in _SLIDER_TICKETS.items() if v < _t.time()]:
        _SLIDER_TICKETS.pop(k, None)
    return {"enabled": True, "ok": True, "ticket": ticket}


@router.post("/login", response_model=LoginResponse, summary="账号密码登录")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """返回 12 小时 JWT 和当前用户信息。启用验证码时须携带有效验证码或滑块票据；
    OTP_ENABLED 为真且用户启用 TOTP 时还须提交 otp_code（默认关闭，不做二步校验）。
    外部自动化建议使用可吊销的 API Token，不要在脚本中保存登录密码。"""
    import time as _t

    from app.core.config import settings

    ip = get_client_ip(request)
    # 人机验证（启用时）：滑块票据 与 图形验证码 二选一（同开关同校验点，均一次性）。
    # 失败/缺票沿用原报错与审计口径，失败次数锁定等既有登录行为不动。
    if getattr(settings, "CAPTCHA_ENABLED", False):
        ok_verify = False
        if body.slider_ticket:
            ent = _SLIDER_TICKETS.pop(body.slider_ticket, None)
            ok_verify = bool(ent and ent >= _t.time())
        if not ok_verify and body.captcha_id:
            ent = _CAPTCHA.pop(body.captcha_id, None)
            ok_verify = bool(ent and ent[0] == (body.captcha_code or "").strip().upper()
                             and ent[1] >= _t.time())
        if not ok_verify:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, t("auth.captcha_bad", lang_of(request)))
    user = db.query(User).filter(User.username == body.username).first()
    if user is None:
        audit.record(
            db, action="login", username=body.username, resource_type="user",
            detail={"ok": False, "reason": "no_user"}, result="failed", client_ip=ip,
        )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.user_not_found", lang_of(request)))
    # TOTP 二步（容错前后 1 个时间窗）：默认关闭，仅 OTP_ENABLED 且用户已启用时校验。
    if getattr(settings, "OTP_ENABLED", False) and user.otp_enabled and verify_password(body.password, user.password_hash):
        ok_otp = body.otp_code and any(
            _totp(user.otp_secret, _t.time() + off) == body.otp_code.strip() for off in (-30, 0, 30))
        if not ok_otp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.otp_bad", lang_of(request)))
    if not verify_password(body.password, user.password_hash):
        audit.record(
            db, action="login", username=body.username, resource_type="user",
            detail={"ok": False, "reason": "bad_password"}, result="failed", client_ip=ip,
        )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.bad_password", lang_of(request)))
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("auth.disabled", lang_of(request)))

    user.last_login_at = datetime.now()
    audit.record(
        db, action="login", user_id=user.id, username=user.username,
        resource_type="user", resource_id=user.id, client_ip=ip,
    )
    db.commit()
    token = create_access_token(user.id, user.role)
    return LoginResponse(token=token, user=_user_info(user, db))


@router.get("/recovery-hint", summary="获取本机密码找回提示")
def recovery_hint(request: Request, username: str = "", db: Session = Depends(get_db)):
    """登录找回兜底提示：两个条件同时满足才下发提示文本——
    1) 该账号 15 分钟内登录失败 ≥3 次（按审计记录计，跨 worker 一致）；
    2) 请求来自本机/私网（is_local_client，解析后真实客户端是 loopback/RFC1918/ULA）。
    外部（公网）来源恒返回空串——不向攻击者暴露服务器找回命令；命令文本只在本响应下发，
    不进前端静态资源。"""
    from datetime import timedelta

    from app.core.client_ip import is_local_client
    from app.models.audit import AuditLog

    if not username or not is_local_client(request):
        return {"hint": ""}
    cutoff = datetime.now() - timedelta(minutes=15)
    n = (db.query(AuditLog)
         .filter(AuditLog.action == "login", AuditLog.result == "failed",
                 AuditLog.username == username, AuditLog.created_at >= cutoff)
         .count())
    if n < 3:
        return {"hint": ""}
    return {"hint": t("auth.recovery_hint", lang_of(request), user=username)}


def _user_info(user: User, db: Session) -> UserInfo:
    from app.core.permissions import effective_codes
    from app.models.permission import PermissionPolicy
    from app.services.authz import dept_chain_policies

    policies = [(p.code, p.effect) for p in
                db.query(PermissionPolicy).filter(PermissionPolicy.user_id == user.id).all()]
    info = UserInfo.model_validate(user)
    # 权限优先级：用户 deny > 用户 allow > 部门 deny > 部门 allow > 角色默认
    info.permission_codes = effective_codes(user.role, policies, dept_chain_policies(db, user))
    return info


@router.get("/me", response_model=UserInfo, summary="查询当前登录身份")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """验证 Bearer JWT 或 API Token，返回所属用户、角色和生效权限码；不返回令牌或密码。"""
    return _user_info(user, db)


@router.post("/logout", summary="退出登录并记录审计")
def logout(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """记录退出审计。JWT 无服务端会话，客户端仍须删除本地令牌；API Token 需在令牌管理中吊销。"""
    # JWT 无服务端状态，登出由前端丢弃 token；此处仅记审计
    audit.record(
        db, action="logout", user_id=user.id, username=user.username,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=64, description="新密码至少 8 位")


@router.post("/password", summary="修改当前用户密码")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改本人密码（首次登录强制走这里；成功后解除强制改密）。"""
    from app.core.security import hash_password, verify_password

    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("auth.pwd_old_wrong", lang_of(request)))
    if body.new_password == body.old_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("auth.pwd_same", lang_of(request)))
    from app.core.security import check_password_strength
    weak = check_password_strength(body.new_password, lang_of(request))
    if weak:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, weak)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    audit.record(
        db, action="auth.change_password", user_id=user.id, username=user.username,
        resource_type="user", resource_id=user.id,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "message": t("auth.pwd_changed", lang_of(request))}



class OtpConfirmIn(BaseModel):
    code: str = Field(max_length=8)


def _otp_available() -> None:
    """OTP 能力默认关闭：实现与数据字段保留，关闭时不暴露接口（404）。"""
    from app.core.config import settings
    if not getattr(settings, "OTP_ENABLED", False):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OTP 未启用")


@router.post("/otp/setup", dependencies=[Depends(_otp_available)], summary="初始化 TOTP 二步验证")
def otp_setup(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """生成 TOTP 密钥（未激活；confirm 后生效）。返回 otpauth:// 供验证器 App 扫码。"""
    import base64
    import os

    secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    user.otp_secret = secret
    user.otp_enabled = False
    db.commit()
    uri = (f"otpauth://totp/千枢:{user.username}?secret={secret}"
           f"&issuer=QianShu&algorithm=SHA1&digits=6&period=30")
    return {"secret": secret, "otpauth_uri": uri, "note": t("auth.otp_setup_note", lang_of(request))}


@router.post("/otp/confirm", dependencies=[Depends(_otp_available)], summary="确认并启用 TOTP")
def otp_confirm(body: OtpConfirmIn, request: Request, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """校验当前验证器的 6 位动态码，校验通过后启用 TOTP 二步验证。"""
    import time as _t

    if not user.otp_secret:
        raise HTTPException(400, t("auth.otp_setup_first", lang_of(request)))
    ok = any(_totp(user.otp_secret, _t.time() + off) == body.code.strip() for off in (-30, 0, 30))
    if not ok:
        raise HTTPException(400, t("auth.otp_code_bad", lang_of(request)))
    user.otp_enabled = True
    db.commit()
    audit.record(db, action="otp.enable", user_id=user.id, username=user.username,
                 resource_type="user", resource_id=str(user.id), detail={},
                 client_ip="")
    db.commit()
    return {"ok": True, "otp_enabled": True}


@router.post("/otp/disable", dependencies=[Depends(_otp_available)], summary="验证并停用 TOTP")
def otp_disable(body: OtpConfirmIn, request: Request, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """校验当前验证器的 6 位动态码，校验通过后停用 TOTP 并清除密钥。"""
    import time as _t

    if not user.otp_enabled:
        return {"ok": True}
    ok = any(_totp(user.otp_secret, _t.time() + off) == body.code.strip() for off in (-30, 0, 30))
    if not ok:
        raise HTTPException(400, t("auth.otp_disable_need_code", lang_of(request)))
    user.otp_enabled = False
    user.otp_secret = ""
    db.commit()
    return {"ok": True, "otp_enabled": False}


class PermPolicyIn(BaseModel):
    user_id: int
    code: str = Field(max_length=64)
    effect: str = Field(max_length=8)   # allow / deny / default(删除覆盖)


@router.get("/permissions", summary="查询当前用户权限")
def my_permissions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """返回权限码目录、本人权限覆盖及叠加部门策略后的生效权限集。
    当前生产 Web 前端未直接调用（页面从 /auth/me 读取生效权限），保留供权限联调。"""
    from app.core.permissions import PERMISSION_CODES, effective_codes
    from app.models.permission import PermissionPolicy
    from app.services.authz import dept_chain_policies

    policies = [{"code": p.code, "effect": p.effect} for p in
                db.query(PermissionPolicy).filter(PermissionPolicy.user_id == user.id).all()]
    return {"catalog": PERMISSION_CODES, "policies": policies,
            "effective": effective_codes(user.role, [(p["code"], p["effect"]) for p in policies],
                                         dept_chain_policies(db, user)),
            "role": user.role}


@router.post("/permissions/manage", summary="设置用户权限覆盖（管理员）")
def manage_permissions(body: PermPolicyIn, request: Request,
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """管理员设置单个用户的权限覆盖：allow 授予、deny 拒绝、default 删除覆盖；
    拒绝优先于允许。这是权限管理写操作，当前生产 Web 前端未直接调用，仅用于权限联调。"""
    from app.core.permissions import PERMISSION_CODES
    from app.models.permission import PermissionPolicy

    if user.role != "admin":
        raise HTTPException(403, "仅管理员可配置权限策略")
    if body.code not in PERMISSION_CODES:
        raise HTTPException(400, "未知权限码")
    if body.effect not in ("allow", "deny", "default"):
        raise HTTPException(400, "effect 仅支持 allow/deny/default")
    q = db.query(PermissionPolicy).filter(PermissionPolicy.user_id == body.user_id,
                                          PermissionPolicy.code == body.code)
    q.delete()
    if body.effect != "default":
        db.add(PermissionPolicy(user_id=body.user_id, code=body.code, effect=body.effect))
    audit.record(db, action="permission.set", user_id=user.id, username=user.username,
                 resource_type="user", resource_id=str(body.user_id),
                 detail={"code": body.code, "effect": body.effect},
                 client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True}


@router.get("/permissions/{uid}", summary="查询指定用户权限（管理员）")
def user_permissions(uid: int, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """管理员按用户 ID 查询权限码目录、权限覆盖及叠加部门策略后的生效权限集。
    当前生产 Web 前端未直接调用，仅用于权限联调。"""
    from app.core.permissions import PERMISSION_CODES, effective_codes
    from app.models.permission import PermissionPolicy
    from app.services.authz import dept_chain_policies

    if user.role != "admin":
        raise HTTPException(403, "仅管理员可查看他人权限策略")
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    policies = [{"code": p.code, "effect": p.effect} for p in
                db.query(PermissionPolicy).filter(PermissionPolicy.user_id == uid).all()]
    return {"catalog": PERMISSION_CODES, "policies": policies,
            "effective": effective_codes(target.role,
                                         [(p["code"], p["effect"]) for p in policies],
                                         dept_chain_policies(db, target)),
            "role": target.role}
