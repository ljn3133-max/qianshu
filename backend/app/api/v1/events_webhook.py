"""事件源 Webhook：GitLab / Gitea / GitHub / Jenkins → change_events。

变更时间线同时覆盖云操作（ActionTrail）与代码/CI 变更，排障时可联合查看发版和云操作。

安全：shared token 校验（EVENT_WEBHOOK_TOKEN）；未配置=403 拒收（不静默丢）。
幂等：event_id 用源事件 ID，重复推送不重复入库。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.change_event import ChangeEvent

logger = logging.getLogger("qianshu.events_webhook")
router = APIRouter(prefix="/events/webhook", tags=["events"])


def _check_token(request: Request, body: bytes) -> None:
    if not settings.EVENT_WEBHOOK_TOKEN:
        raise HTTPException(403, "webhook 未启用（配置 EVENT_WEBHOOK_TOKEN 后生效）")
    candidates = [
        request.headers.get("x-webhook-token", ""),
        request.headers.get("x-gitlab-token", ""),
        request.headers.get("x-gitea-token", ""),
        request.headers.get("x-github-token", ""),
        request.headers.get("x-hub-signature-256", "").removeprefix("sha256="),
    ]
    token = settings.EVENT_WEBHOOK_TOKEN
    ok = any(c and (c == token or _hmac_ok(c, body, token)) for c in candidates)
    if not ok:
        raise HTTPException(403, "webhook token 校验失败")


def _hmac_ok(sig: str, body: bytes, token: str) -> bool:
    """标准验签：GitHub X-Hub-Signature-256 = HMAC-SHA256(secret, body) 十六进制。

    必须使用标准 HMAC 算法，不能用自创摘要替代，否则真实 GitHub 请求验签失败。
    """
    import hmac
    if not sig:
        return False
    expect = hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expect)


def _to_event(event_id: str, event_time: datetime, service: str, event_name: str,
              source: str, user: str, resource: str, summary: str, raw: dict) -> ChangeEvent:
    return ChangeEvent(
        account_id=1,   # CI 事件属平台默认账号（webhook 无凭证上下文）
        event_id=event_id, source_type=service, external_id=event_id,
        event_time=event_time, service=service,
        event_name=event_name, event_source=source, user=user,
        resource=resource[:255], summary=summary[:2000],
        raw=json.dumps(raw, ensure_ascii=False)[:8000],
    )


def _save(db: Session, events: list) -> int:
    """幂等入库：event_id 已存在跳过。"""
    n = 0
    for ev in events:
        if db.query(ChangeEvent).filter(ChangeEvent.event_id == ev.event_id).first():
            continue
        db.add(ev)
        n += 1
    db.commit()
    return n


def _parse_github(headers: dict, payload: dict) -> list:
    ev_type = headers.get("x-github-event", "")
    repo = (payload.get("repository") or {}).get("full_name", "?")
    sender = (payload.get("sender") or {}).get("login", "?")
    now = datetime.now()
    out = []
    if ev_type == "push":
        ref = payload.get("ref", "")
        commits = payload.get("commits") or []
        msgs = [c.get("message", "").splitlines()[0] for c in commits[:3] if c.get("message")]
        eid = f"github:push:{payload.get('after') or payload.get('head_commit', {}).get('id', '')}"
        out.append(_to_event(eid, now, "github", "Push", f"github.com/{repo}", sender,
                             f"{repo} {ref}", f"推送 {len(commits)} 个提交到 {ref}：{'；'.join(msgs)}", payload))
    elif ev_type in ("pull_request", "merge_request"):
        pr = payload.get("pull_request") or {}
        action = payload.get("action", "")
        eid = f"github:pr:{pr.get('number')}:{action}"
        out.append(_to_event(eid, now, "github", f"PullRequest {action}", f"github.com/{repo}", sender,
                             f"{repo}#{pr.get('number')}", f"PR {action}：{pr.get('title', '')}", payload))
    elif ev_type in ("workflow_job", "workflow_run"):
        run = payload.get("workflow_run") or payload.get("workflow_job") or {}
        eid = f"github:ci:{run.get('id')}:{run.get('status') or run.get('conclusion')}"
        out.append(_to_event(eid, now, "github", "CI", f"github.com/{repo}", sender,
                             run.get("name", "workflow"),
                             f"CI {run.get('name', '')} {run.get('status') or run.get('conclusion', '')}", payload))
    elif ev_type == "release":
        rel = payload.get("release") or {}
        eid = f"github:release:{rel.get('id')}"
        out.append(_to_event(eid, now, "github", "Release", f"github.com/{repo}", sender,
                             rel.get("tag_name", ""), f"发布 {rel.get('tag_name', '')}", payload))
    return out


def _ci_source(payload: dict) -> str:
    """GitLab / Gitea 真实 source 判定。

    GitLab payload 恒带 object_kind，Gitea 不带（与 integrations._event_evidence
    的拆分口径同源），入库记真实来源，不能统一硬编码为 'gitlab'。
    """
    return "gitlab" if "object_kind" in (payload or {}) else "gitea"


def _parse_gitlab(headers: dict, payload: dict) -> list:
    """GitLab / Gitea 同族事件解析（两家共用本函数）。

    service/event_source/eid 前缀全部按 _ci_source 记真实 source（gitlab / gitea）；
    历史数据不回洗，新数据按真实协议归因。
    事件匹配兼容两家协议头：X-Gitlab-Event（Push Hook / Merge Request Hook /
    Pipeline Hook）与 X-Gitea-Event（push / pull_request，小写）；Gitea PR 走
    pull_request 对象（无 object_attributes）归一。
    """
    ev_type = (headers.get("x-gitlab-event", "") or headers.get("x-gitea-event", "")).lower()
    kind = payload.get("object_kind", "")
    src = _ci_source(payload)
    project = (payload.get("project") or {}).get("name") or (payload.get("repository") or {}).get("name", "?")
    user = (payload.get("user_name") or (payload.get("user") or {}).get("login")
            or (payload.get("sender") or {}).get("login") or "?")
    now = datetime.now()
    out = []
    if kind == "push" or "push" in ev_type:
        ref = payload.get("ref", "")
        commits = payload.get("commits") or []
        msgs = [c.get("message", "").splitlines()[0] for c in commits[:3] if c.get("message")]
        eid = f"{src}:push:{payload.get('checkout_sha') or payload.get('after', '')}"
        out.append(_to_event(eid, now, src, "Push", f"{src}/{project}", user,
                             f"{project} {ref}", f"推送 {len(commits)} 个提交到 {ref}：{'；'.join(msgs)}", payload))
    elif kind in ("merge_request", "pull_request") or "merge request" in ev_type or "pull_request" in ev_type:
        attrs = payload.get("object_attributes") or {}
        if not attrs and isinstance(payload.get("pull_request"), dict):
            # Gitea/GitHub 形态：{action, number, pull_request:{number,title,state}}（无 object_attributes）
            pr = payload["pull_request"]
            attrs = {"iid": pr.get("number"), "title": pr.get("title", ""),
                     "action": payload.get("action", ""), "state": pr.get("state", "")}
        eid = f"{src}:mr:{attrs.get('iid')}:{attrs.get('action') or attrs.get('state')}"
        out.append(_to_event(eid, now, src, f"MergeRequest {attrs.get('action') or attrs.get('state', '')}",
                             f"{src}/{project}", user, f"{project}!{attrs.get('iid')}",
                             f"MR {attrs.get('action') or attrs.get('state', '')}：{attrs.get('title', '')}", payload))
    elif kind == "pipeline" or "pipeline" in ev_type:
        attrs = payload.get("object_attributes") or {}
        eid = f"{src}:pipeline:{attrs.get('id')}:{attrs.get('status')}"
        out.append(_to_event(eid, now, src, "Pipeline", f"{src}/{project}", user,
                             f"{project} #{attrs.get('id')}", f"流水线 {attrs.get('status', '')}", payload))
    return out


def _parse_apm_alert(source: str, payload: dict) -> list:
    """SkyWalking/Zipkin 告警 webhook（只做告警/事件集成，不做全量 trace 存储——产品边界）。"""
    now = datetime.now()
    name = (payload.get("name") or payload.get("ruleName")
            or payload.get("alertName") or "APM 告警")
    level = payload.get("level") or payload.get("severity") or "warning"
    msg = payload.get("message") or payload.get("desc") or json.dumps(payload, ensure_ascii=False)[:200]
    eid = f"{source}:{hashlib.md5(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]}"
    ev = _to_event(eid, now, source, f"Alert {level}", source,
                   payload.get("receiver") or source, name, f"{name}：{msg}"[:500], payload)
    return [ev]


def _parse_jenkins(payload: dict) -> list:
    name = payload.get("name", "?")
    build = payload.get("build") or {}
    phase = build.get("phase") or build.get("status", "")
    scm = build.get("scm") or {}
    now = datetime.now()
    eid = f"jenkins:{name}:{build.get('number')}:{phase}"
    summary = f"构建 {name} #{build.get('number')} {phase}"
    if scm.get("commit"):
        summary += f"（提交 {scm['commit'][:8]}）"
    return [_to_event(eid, now, "jenkins", "Build", "jenkins", build.get("startedBy", "jenkins"),
                      f"{name} #{build.get('number')}", summary, payload)]


@router.post("/{source}")
async def webhook_ingest(source: str, request: Request):
    """接收 CI/代码平台事件。source: gitlab | gitea | github | jenkins。"""
    body = await request.body()
    _check_token(request, body)
    try:
        payload = json.loads(body or b"{}")
    except Exception:
        raise HTTPException(400, "payload 不是合法 JSON")

    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        if source == "github":
            events = _parse_github(headers, payload)
        elif source in ("gitlab", "gitea"):
            events = _parse_gitlab(headers, payload)
        elif source == "jenkins":
            events = _parse_jenkins(payload)
        elif source in ("skywalking", "zipkin"):
            events = _parse_apm_alert(source, payload)
        else:
            raise HTTPException(400, f"未知事件源: {source}（支持 gitlab/gitea/github/jenkins）")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("webhook 解析失败 %s", source)
        raise HTTPException(400, f"解析失败: {str(e)[:100]}")

    if not events:
        return {"ok": True, "ingested": 0, "note": "事件类型未映射（不影响，属正常忽略）"}

    db = SessionLocal()
    try:
        n = _save(db, events)
    finally:
        db.close()
    logger.info("webhook 事件入库 %s: %d/%d", source, n, len(events))
    return {"ok": True, "ingested": n, "received": len(events)}
