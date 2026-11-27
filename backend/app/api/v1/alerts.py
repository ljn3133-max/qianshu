"""告警渠道/规则/记录 API：渠道管理、规则评估与记录查询。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.collector.cms import ALERT_METRIC_CONFIG
from app.core.db import get_db
from app.core.deps import require_roles
from app.core.security import encrypt_secret
from app.models.alert_channel import AlertChannel
from app.models.alert_record import AlertRecord
from app.models.alert_rule import AlertRule
from app.models.user import User
from app.schemas.alerts import (
    ChannelCreate, ChannelUpdate, RuleAutoCreateIn, RuleCreate, RuleDryRunIn, RuleUpdate,
)
from app.services import alert_ack, audit
from app.services.alert_eval import get_status as eval_status
from app.services.alert_eval import run_evaluation
from app.services.biz_map import resolve_biz
from app.services.notify import NotifyError, send_to_channel

logger = logging.getLogger("qianshu.alerts")
router = APIRouter(prefix="/alerts", tags=["alerts"])


def _mask_webhook(cipher_row) -> str:
    """webhook 脱敏：只展示 scheme://host/path 与 query 尾 4 位。"""
    from app.core.security import decrypt_secret

    try:
        url = decrypt_secret(cipher_row.webhook_url_cipher)
    except Exception:
        return "(解密失败)"
    parts = urlsplit(url)
    if parts.query:
        tail = parts.query[-4:]
        return f"{parts.scheme}://{parts.netloc}{parts.path}?****{tail}"
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _channel_out(row: AlertChannel) -> dict:
    try:
        mobiles = json.loads(row.at_mobiles or "[]")
    except ValueError:
        mobiles = []
    return {
        "id": row.id, "name": row.name, "type": row.type,
        "webhook_masked": _mask_webhook(row),
        "at_mobiles": mobiles, "enabled": row.enabled,
        # 自签证书 verify 开关默认校验；旧行无列时按 True
        "verify_ssl": bool(getattr(row, "verify_ssl", True)),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _validate_webhook_url(url: str) -> None:
    """入口校验：http(s) 且 host 非空（端口非法等交给 httpx 的错误兜底）。"""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "webhook 地址必须以 http(s):// 开头")
    parts = urlsplit(url)
    if not parts.hostname:
        raise HTTPException(400, "webhook 地址缺少主机名")


def _validate_channel_config(kind: str, *, webhook_url: str = "") -> None:
    """按渠道类型校验（创建/更新共用，避免两套口径）。邮件渠道已下线。"""
    _validate_webhook_url(webhook_url)


@router.get("/channels")
def list_channels(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator")),
):
    # 邮件渠道已下线：历史 email 行不展示；保留删除入口供清理（不静默删数据）
    rows = (db.query(AlertChannel).filter(AlertChannel.type != "email")
            .order_by(AlertChannel.id).all())
    return [_channel_out(r) for r in rows]


@router.post("/channels")
def create_channel(
    body: ChannelCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    if db.query(AlertChannel).filter(AlertChannel.name == body.name).first():
        raise HTTPException(400, "渠道名称已存在")
    _validate_channel_config(body.type, webhook_url=body.webhook_url)
    row = AlertChannel(
        name=body.name,
        type=body.type,
        webhook_url_cipher=(encrypt_secret(body.webhook_url) if body.webhook_url else ""),
        at_mobiles=json.dumps([m.strip() for m in body.at_mobiles if m.strip()]),
        enabled=body.enabled,
        verify_ssl=bool(getattr(body, "verify_ssl", True)),   # 自签证书校验开关
    )
    db.add(row)
    db.flush()
    audit.record(
        db, action="alerts.channel.create", user_id=user.id, username=user.username,
        resource_type="alert_channel", resource_id=row.id,
        detail={"name": body.name, "type": body.type,
                "endpoint": _mask_webhook(row)},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _channel_out(row)


@router.put("/channels/{channel_id}")
def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.get(AlertChannel, channel_id)
    if row is None or row.type == "email":
        raise HTTPException(404, "渠道不存在")
    if body.name and body.name != row.name:
        if db.query(AlertChannel).filter(AlertChannel.name == body.name).first():
            raise HTTPException(400, "渠道名称已存在")
        row.name = body.name
    kind = body.type or row.type
    # 合并后的最终配置做条件校验（更新可能只传部分字段）
    webhook_url = body.webhook_url or ""
    if not webhook_url:
        try:
            webhook_url = decrypt_secret(row.webhook_url_cipher) if row.webhook_url_cipher else ""
        except Exception:
            webhook_url = ""
    # 只改名称/启停/证书校验时不重新校验通道参数（避免 MASTER_KEY 变更等历史坏行
    # 连"停用"都做不了）；一旦触碰配置字段或切换类型则整份合并配置必须过校验。
    touches_config = any([body.type, body.webhook_url])
    if touches_config:
        _validate_channel_config(kind, webhook_url=webhook_url)
    if body.type:
        row.type = body.type
    if body.webhook_url:
        row.webhook_url_cipher = encrypt_secret(body.webhook_url)
    if body.at_mobiles is not None:
        row.at_mobiles = json.dumps([m.strip() for m in body.at_mobiles if m.strip()])
    if body.enabled is not None:
        row.enabled = body.enabled
    if getattr(body, "verify_ssl", None) is not None:   # 自签证书校验开关
        row.verify_ssl = body.verify_ssl
    audit.record(
        db, action="alerts.channel.update", user_id=user.id, username=user.username,
        resource_type="alert_channel", resource_id=channel_id,
        detail={
            "fields": [k for k, v in body.model_dump().items() if v is not None],
            "endpoint": _mask_webhook(row),
        },
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _channel_out(row)


@router.delete("/channels/{channel_id}")
def delete_channel(
    channel_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.get(AlertChannel, channel_id)
    if row is None:
        raise HTTPException(404, "渠道不存在")
    name = row.name
    db.delete(row)
    audit.record(
        db, action="alerts.channel.delete", user_id=user.id, username=user.username,
        resource_type="alert_channel", resource_id=channel_id, detail={"name": name},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}


@router.post("/channels/{channel_id}/test")
def test_channel(
    channel_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """向该渠道发一条测试消息（用户在页面上主动点击触发）。"""
    row = db.get(AlertChannel, channel_id)
    if row is None or row.type == "email":
        raise HTTPException(404, "渠道不存在")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"这是 AIOps 平台的渠道连通性测试。\n\n"
        f"- 渠道：{row.name}（{row.type}）\n"
        f"- 触发人：{user.username}\n"
        f"- 时间：{now}\n\n"
        f"收到本消息说明该渠道配置正确。"
    )
    try:
        send_to_channel(row, "AIOps 测试消息", content)
    except NotifyError as e:
        logger.warning("渠道测试失败 %s: %s", row.name, e)
        audit.record(
            db, action="alerts.channel.test", user_id=user.id, username=user.username,
            resource_type="alert_channel", resource_id=channel_id,
            detail={"name": row.name, "result": "failed", "error": str(e)[:200]},
            client_ip=get_client_ip(request),
        )
        db.commit()
        return {"ok": False, "message": f"发送失败: {e}"}
    audit.record(
        db, action="alerts.channel.test", user_id=user.id, username=user.username,
        resource_type="alert_channel", resource_id=channel_id,
        detail={"name": row.name, "result": "ok"},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "message": "测试消息已发送，请在群里确认收到"}


# ---------------- 告警规则 ----------------


_SILENCE_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")


def _validate_silences(items: list) -> None:
    """静默时段格式校验：HH:MM-HH:MM。非法条目会被评估器静默跳过，必须在入口拦下。"""
    for s in items:
        if not _SILENCE_RE.match(s):
            raise HTTPException(400, f"静默时段格式不正确: {s}（示例 23:00-07:00，支持跨零点，多个用逗号分隔）")
        for part in s.split("-", 1):
            h, m = (int(x) for x in part.split(":"))
            if h > 23 or m > 59:
                raise HTTPException(400, f"静默时段不合法: {s}")


def _validate_rule(source: str, product=None, metric=None, project=None, logstore=None, query_expr=None):
    if source == "metric":
        if not product or not metric:
            raise HTTPException(400, "指标规则必须指定 product 和 metric")
        if product not in ALERT_METRIC_CONFIG:
            raise HTTPException(400, f"不支持的产品: {product}（可选: {list(ALERT_METRIC_CONFIG)}）")
        names = [m for m, _label, _unit in ALERT_METRIC_CONFIG[product]]
        if metric not in names:
            raise HTTPException(400, f"产品 {product} 不支持的指标: {metric}（可选: {names}）")
    else:
        if not project or not logstore:
            raise HTTPException(400, "SLS 规则必须指定 project 和 logstore")


def _rule_out(row: AlertRule, acc_map: dict | None = None) -> dict:
    try:
        channel_ids = json.loads(row.channel_ids or "[]")
    except ValueError:
        channel_ids = []
    try:
        silences = json.loads(row.silences or "[]")
    except ValueError:
        silences = []
    from app.services.alert_eval import _in_silence

    acc_id = getattr(row, "account_id", 0) or 0
    acc = (acc_map or {}).get(acc_id) or {}
    return {
        "id": row.id, "name": row.name, "source": row.source, "level": row.level,
        "product": row.product, "metric": row.metric,
        "project": row.project, "logstore": row.logstore, "query_expr": row.query_expr,
        "comparator": row.comparator, "threshold": row.threshold, "duration": row.duration,
        "channel_ids": channel_ids, "silences": silences,
        "in_silence": bool(_in_silence(row.silences)),
        # 账号维度：0=全部账号；>0=绑定账号（带展示名）
        "account_id": acc_id,
        "account_name": acc.get("name", ""),
        # 规则来源：manual=人工；auto=接入账号后自动播种（同权可编辑）
        "origin": getattr(row, "origin", "manual") or "manual",
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/rules")
def list_rules(
    account_id: int = 0,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """规则列表：account_id=0 全部，>0 只看绑定该账号的规则。"""
    from app.services.tenant import account_index

    q = db.query(AlertRule)
    if account_id:
        q = q.filter(AlertRule.account_id == account_id)
    rows = q.order_by(AlertRule.id.desc()).all()
    acc_map = account_index(db)
    return [_rule_out(r, acc_map) for r in rows]


@router.post("/rules")
def create_rule(
    body: RuleCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    if db.query(AlertRule).filter(AlertRule.name == body.name).first():
        raise HTTPException(400, "规则名称已存在")
    _validate_rule(body.source, body.product, body.metric, body.project, body.logstore, body.query_expr)
    _validate_silences(body.silences)
    row = AlertRule(
        name=body.name, source=body.source, level=body.level,
        product=body.product, metric=body.metric,
        project=body.project, logstore=body.logstore, query_expr=body.query_expr,
        comparator=body.comparator, threshold=body.threshold, duration=body.duration,
        channel_ids=json.dumps(body.channel_ids),
        silences=json.dumps([s.strip() for s in body.silences if s.strip()]),
        account_id=body.account_id or 0,
        enabled=body.enabled,
    )
    db.add(row)
    db.flush()
    audit.record(
        db, action="alerts.rule.create", user_id=user.id, username=user.username,
        resource_type="alert_rule", resource_id=row.id,
        detail={"name": body.name, "source": body.source, "level": body.level,
                "account_id": body.account_id or 0},
        client_ip=get_client_ip(request),
    )
    db.commit()
    from app.services.tenant import account_index

    return _rule_out(row, account_index(db))


@router.get("/rules/auto-analyze")
def auto_analyze_rules(
    account_id: int = Query(0, ge=0),
    days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """规则自动分析（只读）：盘点规则覆盖缺口，按真实历史数据给建议阈值。

    这是"平台自己分析"而非开发者按账号手工写规则：输入是实际资产清单 + 规则表 +
    VictoriaMetrics 历史样本，输出是缺口候选、建议阈值、依据和风险提示。
    不写库、不发通知；用户在规则页勾选后才调 /rules/auto-create 落库。
    """
    from app.services.rule_advisor import analyze

    return analyze(db, account_id=account_id, days=days)


@router.post("/rules/auto-create")
def auto_create_rules(
    body: RuleAutoCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """创建用户在自动分析结果中勾选的规则（服务端重新分析后落库，不信任前端阈值）。

    新规则 origin=auto、channel_ids=[]（只站内记录，不外发）；创建后与人工规则同权可改删。
    """
    from app.services.rule_advisor import create_selected

    result = create_selected(db, body.keys, user.username)
    audit.record(
        db, action="alerts.rule.auto_create", user_id=user.id, username=user.username,
        resource_type="alert_rule", resource_id="auto",
        detail={"created": [c["name"] for c in result["created"]],
                "skipped": len(result["skipped"])},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.post("/rules/试算")
def dry_run_rule(
    body: RuleDryRunIn,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator")),
):
    """规则试算：保存前用近 N 天历史数据回放该规则草稿——

    返回 {would_fire: [{ts, value, threshold, target, ...}], count, samples_summary}，
    帮用户调阈值不盲拍。判据与线上评估同源（复用 alert_eval 公共判据，见 alert_sim）。
    只读历史数据零写库：不建告警记录、不发通知、不记审计。
    """
    d = body.rule_draft
    _validate_rule(d.source, d.product, d.metric, d.project, d.logstore, d.query_expr)
    _validate_silences([s.strip() for s in d.silences if s.strip()])
    from app.services.alert_sim import simulate
    try:
        return simulate(db, d.model_dump(), days=body.days)
    except Exception as e:
        logger.warning("规则试算失败: %s", str(e)[:200])
        raise HTTPException(502, f"试算失败: {str(e)[:150]}")


@router.put("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    body: RuleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    row = db.get(AlertRule, rule_id)
    if row is None:
        raise HTTPException(404, "规则不存在")
    product = body.product if body.product is not None else row.product
    metric = body.metric if body.metric is not None else row.metric
    project = body.project if body.project is not None else row.project
    logstore = body.logstore if body.logstore is not None else row.logstore
    _validate_rule(row.source, product, metric, project, logstore)
    if body.silences is not None:
        _validate_silences([s.strip() for s in body.silences if s.strip()])
    if body.name and body.name != row.name:
        if db.query(AlertRule).filter(AlertRule.name == body.name).first():
            raise HTTPException(400, "规则名称已存在")
        row.name = body.name
    if body.level is not None:
        row.level = body.level
    if row.source == "metric":
        row.product, row.metric = product, metric
    else:
        row.project, row.logstore, row.query_expr = project, logstore, (
            body.query_expr if body.query_expr is not None else row.query_expr
        )
    if body.comparator is not None:
        row.comparator = body.comparator
    if body.threshold is not None:
        row.threshold = body.threshold
    if body.duration is not None:
        row.duration = body.duration
    if body.channel_ids is not None:
        row.channel_ids = json.dumps(body.channel_ids)
    if body.silences is not None:
        row.silences = json.dumps([s.strip() for s in body.silences if s.strip()])
    if body.account_id is not None:
        row.account_id = body.account_id or 0   # 账号绑定；0 表示全部账号
    if body.enabled is not None:
        row.enabled = body.enabled
    audit.record(
        db, action="alerts.rule.update", user_id=user.id, username=user.username,
        resource_type="alert_rule", resource_id=rule_id,
        detail={"fields": [k for k, v in body.model_dump().items() if v is not None]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    from app.services.tenant import account_index

    return _rule_out(row, account_index(db))


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    row = db.get(AlertRule, rule_id)
    if row is None:
        raise HTTPException(404, "规则不存在")
    name = row.name
    # 历史记录保留（rule_name 已快照），仅删规则
    db.delete(row)
    audit.record(
        db, action="alerts.rule.delete", user_id=user.id, username=user.username,
        resource_type="alert_rule", resource_id=rule_id, detail={"name": name},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}


# ---------------- 告警记录与评估 ----------------


class BaselineIn(BaseModel):
    rule_id: int
    target: str


@router.post("/accept-baseline")
def accept_baseline(
    body: BaselineIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """把某机器某指标"设为常态"。

    取该指标近 7 天实测水位，触发线上调到 水位×1.05——之后只报显著偏离
    （破新线=真异常；跌破水位 60%=该忙反闲）。与行为画像 accept 同哲学。
    """
    import time as _time
    from app.collector.vm import query_range as vm_query_range
    from app.collector.vm import metric_name as vm_metric_name
    from app.models.alert_baseline import AlertTargetBaseline

    rule = db.query(AlertRule).get(body.rule_id)
    if rule is None or rule.source != "metric":
        raise HTTPException(404, "规则不存在或非指标规则")
    name = vm_metric_name(rule.product, rule.metric)
    end_s = int(_time.time())
    try:
        series = vm_query_range(f'{name}{{instance_id="{body.target}"}}', end_s - 7 * 86400, end_s, 300)
        points = [(int(float(ts)), float(v)) for s in series for ts, v in s.get("values", [])]
    except Exception as e:
        raise HTTPException(502, f"水位测量失败: {str(e)[:150]}")
    if not points:
        raise HTTPException(400, "近 7 天无该指标数据，无法测量水位")
    # 水位 = 近 7 天「每日峰值」的中位数——"经常 95%"指它每天都能到的高度
    # （点级均值/P90 都会被低谷稀释：实测该机 95% 冲高只占少数采样点，均值才 35%）
    from collections import defaultdict as _dd
    daily_peak: dict = {}
    for ts, v in points:
        day = datetime.fromtimestamp(ts).date()
        daily_peak[day] = max(daily_peak.get(day, v), v)
    peaks = sorted(daily_peak.values())
    # 取每日峰值的 P90（"经常 95%"=常态能冲到的高度）；峰值天波动大时中位数
    # 会把正常冲高误判为异常（实测 P50=88.6 会让 95% 继续报）
    baseline = peaks[int(len(peaks) * 0.9)] if len(peaks) >= 5 else max(peaks)
    # 触发线 = max(原阈值, 水位×1.05)，百分比型封顶 100：
    # - 低位机器（master 水位 19.6%）绝不下调触发线——设 20.5 反而更吵
    # - 贴满机器（core 水位 95.9%×1.05>100）封顶 100——"打满持续"才是它的异常信号
    override = max(rule.threshold, baseline * 1.05)
    if rule.threshold <= 100:
        override = min(override, 100.0)
    override = round(override, 2)
    row = (db.query(AlertTargetBaseline)
           .filter(AlertTargetBaseline.rule_id == rule.id, AlertTargetBaseline.target == body.target)
           .first())
    if row is None:
        row = AlertTargetBaseline(rule_id=rule.id, target=body.target)
        db.add(row)
    row.rule_name = rule.name
    row.target_name = body.target
    row.baseline_value = round(baseline, 3)
    row.threshold_override = override
    row.confirmed_by = user.username
    row.confirmed_by_id = user.id   # confirmed_by_id 引用 users.id
    row.confirmed_at = datetime.now()
    # 已在 firing 的记录：下轮评估会因新线不再越限自动恢复；这里直接收口更利落
    (db.query(AlertRecord)
     .filter(AlertRecord.rule_id == rule.id, AlertRecord.target == body.target,
             AlertRecord.status == "firing")
     .update({AlertRecord.status: "recovered", AlertRecord.recovered_at: datetime.now(),
              AlertRecord.content: AlertRecord.content + " | [已设为常态：该机器水位 "
                      + f"{baseline:.1f}，新触发线 {override}" + "]"},
             synchronize_session=False))
    audit.record(
        db, action="alert.baseline_accept", user_id=user.id, username=user.username,
        resource_type="alert_rule", resource_id=f"{rule.id}:{body.target}",
        detail={"baseline": round(baseline, 2), "threshold_override": override},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"rule": rule.name, "target": body.target, "baseline": round(baseline, 2),
            "threshold_override": override, "note": "已记录为常态（水位=近7天每日峰值 P90），之后只报显著偏离（含骤降）"}


def _own_account_name(db, acc_map, r) -> str:
    """account_name 空缺时过归属解析层补。

    告警是历史记录：已有采集账号名不覆盖，只补空缺（"account_name 空则过解析层"）；
    无登记返回 ''（保持不显示，不造数据）。
    """
    from app.services.ownership import account_name_for

    return account_name_for(db, acc_map, target=r.target or "", target_name=r.target_name or "")


def _query_records(db, *, account_id: int = 0, status: str = "", rule_id: int = 0,
                   keyword: str = "", source: str = ""):
    """告警记录查询构造器——列表与导出共用（禁止第二套筛选口径）。"""
    q = db.query(AlertRecord)
    if account_id:
        q = q.filter(AlertRecord.account_id == account_id)   # 多账号过滤
    if status:
        q = q.filter(AlertRecord.status == status)
    if rule_id:
        q = q.filter(AlertRecord.rule_id == rule_id)
    if source:
        q = q.filter(AlertRecord.source == source)
    if keyword:   # 深链 ?keyword= 过滤（标题/目标/规则名模糊匹配）
        from sqlalchemy import or_

        like = f"%{keyword}%"
        q = q.filter(
            or_(
                AlertRecord.title.ilike(like),
                AlertRecord.target.ilike(like),
                AlertRecord.target_name.ilike(like),
                AlertRecord.rule_name.ilike(like),
            )
        )
    return q


@router.get("/records")
def list_records(
    account_id: int = 0,
    status: str = Query(""),
    rule_id: int = Query(0),
    keyword: str = Query(""),
    source: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    q = _query_records(db, account_id=account_id, status=status, rule_id=rule_id,
                       keyword=keyword, source=source)
    total = q.count()
    rows = (
        q.order_by(AlertRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    # 归属标识：account_id -> 账号名（云账号优先/数据分组兜底，无则空）
    from app.services.tenant import account_index
    from app.models.oncall import AlertAck

    acc_map = account_index(db)
    # 记录返回 claimed，供前端隐藏「认领」入口并回显状态，
    # 避免重复认领被展示为首次成功。
    alert_ids = [r.id for r in rows]
    claimed_ids = {
        aid for (aid,) in db.query(AlertAck.alert_id)
        .filter(AlertAck.alert_id.in_(alert_ids)).all()
    } if alert_ids else set()
    # 告警 → 指标页深链：记录表本身没有 product，从规则带出；
    # 内置/合成告警 rule_id=0 无产品，前端据此不显示「查看指标」
    rule_products = {
        rid: prod for rid, prod in db.query(AlertRule.id, AlertRule.product)
        .filter(AlertRule.product.isnot(None), AlertRule.product != "").all()
    }
    return {
        "total": total,
        "items": [
            {
                "id": r.id, "rule_id": r.rule_id, "rule_name": r.rule_name,
                "source": r.source, "target": r.target, "target_name": r.target_name,
                "product": rule_products.get(r.rule_id) or "",
                "level": r.level, "title": r.title, "content": r.content,
                "status": r.status, "breach_value": r.breach_value,
                "fired_at": r.fired_at.isoformat() if r.fired_at else None,
                "last_eval_at": r.last_eval_at.isoformat() if r.last_eval_at else None,
                "recovered_at": r.recovered_at.isoformat() if r.recovered_at else None,
                "notified_channels": json.loads(r.notified_channels or "[]"),
                "silenced": r.silenced,
                "module": r.module or "alert",
                "ack_status": r.ack_status or "none",
                "ack_label": alert_ack.ACK_LABELS.get(r.ack_status or "none", ""),
                "ack_note": r.ack_note or "",
                "ack_by": r.ack_by or "",
                "ack_at": r.ack_at.isoformat() if r.ack_at else "",
                "next_notify_at": r.next_notify_at.isoformat() if r.next_notify_at else "",
                "notify_count": r.notify_count or 0,
                "last_notified_at": (r.last_notified_at.isoformat()
                                     if r.last_notified_at else ""),
                "account_id": r.account_id,
                "claimed": r.id in claimed_ids,
                "account_name": (acc_map.get(r.account_id) or {}).get("name", "") or _own_account_name(db, acc_map, r),
                "biz": resolve_biz(db, r.target, r.target_name, f"{r.title} {(r.content or '')[:200]}"),
            }
            for r in rows
        ],
    }


@router.get("/records/export")
def export_records(
    request: Request,
    account_id: int = 0,
    status: str = Query(""),
    rule_id: int = Query(0),
    keyword: str = Query(""),
    source: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """告警记录 CSV（与列表同筛选口径；≤2 万行，真实总数见 X-Total-Rows）。"""
    from app.core.csvx import MAX_EXPORT_ROWS, csv_response

    q = _query_records(db, account_id=account_id, status=status, rule_id=rule_id,
                       keyword=keyword, source=source)
    total = q.count()
    rows = q.order_by(AlertRecord.id.desc()).limit(MAX_EXPORT_ROWS).all()
    status_labels = {"firing": "触发中", "recovered": "已恢复"}
    # 来源必须覆盖记录表全部写入方：alert_eval(metric/sls)、host_watch(builtin)、
    # synth(拨测)；k8s/external 为 0.3.2 预留，避免新来源导出回退成裸代码。
    source_labels = {
        "metric": "指标阈值", "sls": "SLS 错误", "builtin": "主机看护",
        "synth": "拨测", "k8s": "K8s 故障", "external": "外部告警",
    }
    items = [
        {
            "id": r.id, "level": r.level, "title": r.title, "target": r.target,
            "target_name": r.target_name, "rule_name": r.rule_name,
            "module": r.module or "alert", "source": r.source,
            "source_label": source_labels.get(r.source, r.source),
            "status_label": status_labels.get(r.status, r.status),
            "ack_label": alert_ack.ACK_LABELS.get(r.ack_status or "none", ""),
            "fired_at": r.fired_at.isoformat(sep=" ", timespec="seconds") if r.fired_at else "",
            "last_eval_at": (r.last_eval_at.isoformat(sep=" ", timespec="seconds")
                             if r.last_eval_at else ""),
            "recovered_at": (r.recovered_at.isoformat(sep=" ", timespec="seconds")
                             if r.recovered_at else ""),
        }
        for r in rows
    ]
    audit.record(
        db, action="alerts.records.export", user_id=user.id, username=user.username,
        resource_type="alert_record",
        detail={"account_id": account_id, "status": status, "rule_id": rule_id,
                "keyword": keyword, "source": source,
                "rows": len(items), "total": total},
        client_ip=get_client_ip(request),
    )
    db.commit()
    headers = [
        ("id", "ID"), ("level", "级别"), ("title", "标题"), ("target", "目标"),
        ("target_name", "目标名称"), ("rule_name", "规则"), ("module", "模块"),
        ("source_label", "来源"), ("status_label", "状态"), ("ack_label", "确认状态"),
        ("fired_at", "首次触发"), ("last_eval_at", "最近评估"),
        ("recovered_at", "恢复时间"),
    ]
    return csv_response(
        headers, items,
        filename="告警记录.csv", ascii_filename="alert-records.csv",
        total_rows=total,
    )


@router.get("/eval/status")
def get_eval_status(
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    return eval_status()


@router.post("/records/{record_id}/resolve")
def resolve_record(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动标记已恢复。

    人工处置完的告警应能主动收口，不用干等指标跌进恢复线。
    """
    rec = db.get(AlertRecord, record_id)
    if rec is None:
        raise HTTPException(404, "告警记录不存在")
    if rec.status != "firing":
        raise HTTPException(400, "该记录不是触发中状态")
    rec.status = "recovered"
    rec.recovered_at = datetime.now()
    rec.content = (rec.content or "") + " | [人工标记已恢复]"
    audit.record(
        db, action="alert.record_resolve", user_id=user.id, username=user.username,
        resource_type="alert_record", resource_id=str(record_id),
        detail={"title": rec.title}, client_ip=get_client_ip(request),
    )
    db.commit()
    return {"id": record_id, "status": "recovered"}


class RecordAckIn(BaseModel):
    action: str = "acknowledge"      # acknowledge | ignore
    note: str = ""


@router.post("/records/{record_id}/ack")
def ack_record(
    record_id: int,
    body: RecordAckIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """确认/忽略一条 firing 告警。

    确认与忽略都表示「我处理过了」，区别只在语义标签；到期重申按数据源优先级
    （medium 7 天 / low 30 天 / high 不重申 / 账号 repeat_days 可覆盖）。
    值班认领是另一回事（POST /oncall/ack/{id}），不混用。
    """
    rec = db.get(AlertRecord, record_id)
    if rec is None:
        raise HTTPException(404, "告警记录不存在")
    if rec.status != "firing":
        raise HTTPException(400, "该记录不是触发中状态，无需确认")
    if body.action not in alert_ack.ACTIONS:
        raise HTTPException(400, f"不支持的操作: {body.action}")
    payload = alert_ack.apply_ack(db, rec, body.action, body.note, user)
    audit.record(
        db, action="alert.record.ack", user_id=user.id, username=user.username,
        resource_type="alert_record", resource_id=str(record_id),
        detail={"action": body.action, "title": rec.title,
                "next_notify_at": payload["next_notify_at"]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return payload


@router.delete("/records/{record_id}/ack")
def unack_record(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """取消确认/忽略，恢复该告警的正常提醒。"""
    rec = db.get(AlertRecord, record_id)
    if rec is None:
        raise HTTPException(404, "告警记录不存在")
    payload = alert_ack.clear_ack(rec)
    audit.record(
        db, action="alert.record.unack", user_id=user.id, username=user.username,
        resource_type="alert_record", resource_id=str(record_id),
        detail={"title": rec.title}, client_ip=get_client_ip(request),
    )
    db.commit()
    return payload


@router.post("/eval/run")
def trigger_evaluation(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动触发一轮告警评估（与后台循环互斥）。"""
    try:
        result = run_evaluation(db, force=True)
    except Exception as e:
        logger.exception("手动告警评估失败")
        raise HTTPException(502, f"评估失败: {str(e)[:200]}")
    audit.record(
        db, action="alerts.eval.run", user_id=user.id, username=user.username,
        resource_type="alert_rule", resource_id="eval",
        detail={"fired": result.get("fired", 0), "recovered": result.get("recovered", 0)},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result
