from __future__ import annotations

from typing import Optional
"""认证与 RBAC 依赖。"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import API_TOKEN_PREFIX, decode_access_token, hash_api_token
from app.models.user import User

bearer = HTTPBearer(auto_error=False)


def _extract_token(request: Optional[Request], cred: Optional[HTTPAuthorizationCredentials]) -> str:
    """token 提取：Authorization 头优先；GET/HEAD 兼容 query 参数（access_token/token）。

    query token 用于「API 清单页 GET URL 点开即看」的浏览器直开通道：
    新窗口导航无法携带请求头，GET/HEAD URL 附 ?access_token=… 即可打开；
    写操作不接受 query token（POST/PUT/DELETE 必须走 Bearer 头）。
    request 为空（历史手工调用点直接传 cred）时只认头。
    """
    if cred is not None and cred.credentials:
        return cred.credentials
    if request is not None and request.method in ("GET", "HEAD"):
        return (request.query_params.get("access_token")
                or request.query_params.get("token") or "")
    return ""


def _api_token_user(db: Session, token: str, touch: bool = True) -> Optional[User]:
    """个人 API Token（qs_ 前缀）鉴权：哈希匹配 + 启用 + 未过期。

    权限=所属用户角色（返回 User，走既有 require_roles/角色判断）；
    无效/吊销/过期/用户失效一律 401。touch=True 时刷新 last_used_at（>60s 才写，防写放大）。
    """
    from datetime import datetime

    from app.models.user_api_token import UserApiToken

    row = db.query(UserApiToken).filter(
        UserApiToken.token_hash == hash_api_token(token)).first()
    now = datetime.now()
    if row is None or not row.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Token 无效或已吊销")
    if row.expires_at is not None and row.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Token 已过期")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    if touch and (row.last_used_at is None or (now - row.last_used_at).total_seconds() > 60):
        row.last_used_at = now
        db.commit()
    return user


def get_current_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
    request: Request = None,   # FastAPI 按类型注入；手工调用 (cred, db) 兼容（缺省 None）
) -> User:
    # request 由 FastAPI 注入，用于读取 query token；保持 (cred, db) 位置签名，
    # 兼容 mcp_server / synth / bootstrap 等手工调用点。
    token = _extract_token(request, cred)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    # Bearer 双轨：qs_ 前缀走个人 API Token，其余按 JWT 解析。
    if token.startswith(API_TOKEN_PREFIX):
        return _api_token_user(db, token)
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    return user


def require_roles(*roles: str):
    """require_roles("admin") / require_roles("admin", "operator")"""

    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            # 不要直接把 tuple repr 弹给用户（「需要角色: ('admin',)」）
            raise HTTPException(status.HTTP_403_FORBIDDEN, "需要角色: " + "/".join(roles))
        return user

    return dep


# ---------------- 强制改密门（默认密码必须换掉才能用系统） ----------------

def require_password_changed(
    request: Request,
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
):
    """must_change_password 用户只放行登录/查身份/改密/登出/向导状态，其余 403。

    无 token 或 token 无效时放行——由各路由自己的鉴权依赖报 401，不重复拦截。
    个人 API Token（qs_ 前缀）同样受本门约束，防止借令牌绕过强制改密。
    """
    token = _extract_token(request, cred)
    if not token:
        return
    user = None
    if token.startswith(API_TOKEN_PREFIX):
        try:
            user = _api_token_user(db, token, touch=False)
        except HTTPException:
            return   # 无效令牌放行，由路由自己的鉴权依赖报 401
    else:
        try:
            payload = decode_access_token(token)
            user_id = int(payload["sub"])
        except Exception:
            return
        user = db.get(User, user_id)
    if user is None or not user.must_change_password:
        return
    path = request.url.path
    allowed_suffix = ("/auth/login", "/auth/me", "/auth/password",
                      "/auth/logout", "/bootstrap/status")
    if any(path.endswith(sfx) for sfx in allowed_suffix):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={"code": "NEED_CHANGE_PASSWORD", "message": "首次登录请先修改初始密码"},
    )
