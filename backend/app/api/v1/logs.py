"""日志查询 API：SLS/Loki/CLS/ES 多源统一查询 + 常用查询保存。

多源统一：查询走「已配置日志源」（services/log_query <- services/datasource 的
LOG_SOURCES 清单 + SLS 隐式源），GET /logs/sources 回可用源（名称/类型/状态），
查询带 source 参数（缺省=第一个可用源）；无任何日志源回真实空态引导（不报错）。
SLS 现有查询行为向后兼容：旧调用（带 project/logstore、不带 source/target）仍走
原 SLS 路径与原响应口径（默认源语义）；/logs/projects、/logs/logstores 原样保留
（Alerts.vue 告警规则配置共用）。
K8s 容器日志不在此（k8s_pod_logs 库表，入口「K8s 管理-容器日志」）——分工不抢。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.collector.cls import CLSQueryError
from app.collector.credential import CredentialError, resolve_sls_credential
from app.collector.es_query import ESQueryError
from app.collector.loki import LokiQueryError
from app.collector.sls import SLSQueryError, list_logstores, list_projects, query_logs
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.saved_query import SavedQuery
from app.models.user import User
from app.schemas.logs import SavedQueryCreate
from app.services import audit, log_query

logger = logging.getLogger("qianshu.logs")
router = APIRouter(prefix="/logs", tags=["logs"])

# 未配置阿里云凭证属于产品常态：列表类回 200 空态和说明，查询类回 503 并给接入指引
_NO_CREDS_NOTE = ("未配置阿里云凭证：请到「云账号管理」添加只读账号，"
                  "或配置环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET")

# 时间档位 -> 秒
RANGE_SECONDS = {
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "12h": 43200,
    "24h": 86400,
    "3d": 259200,
    "7d": 604800,
}


@router.get("/sources")
def log_sources_list(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """可用日志源（名称/类型/状态）。无可查询源时 empty=true + guide=集成中心引导。"""
    return log_query.available_sources(db)


@router.get("/targets")
def log_targets(
    source: str = Query(""),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """源的目标清单：SLS=projects / CLS=日志主题 / ES=索引 / Loki=无目标。"""
    return log_query.targets(db, source)


@router.get("/projects")
def sls_projects(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    try:
        ak, sk, _src = resolve_sls_credential(db)
    except CredentialError:
        return {"projects": [], "note": _NO_CREDS_NOTE, "no_credential": True}
    try:
        return {"projects": list_projects(ak, sk)}
    except Exception as e:
        logger.exception("SLS project 列表失败")
        raise HTTPException(502, f"SLS 查询失败: {str(e)[:200]}")


@router.get("/logstores")
def sls_logstores(
    project: str = Query(min_length=1),
    source: str = Query(""),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    endpoint = ""
    if source:
        try:
            src = log_query.resolve_source(db, name=source)
        except log_query.LogSourceError as e:
            raise HTTPException(e.status, str(e))
        if src["type"] != "sls":
            raise HTTPException(400, f"日志源「{source}」不是 SLS")
        endpoint = src.get("url") or ""
    try:
        ak, sk, _src = resolve_sls_credential(db)
    except CredentialError:
        return {"project": project, "logstores": [], "note": _NO_CREDS_NOTE, "no_credential": True}
    try:
        return {"project": project, "logstores": list_logstores(ak, sk, project, endpoint=endpoint)}
    except Exception as e:
        logger.exception("SLS logstore 列表失败")
        raise HTTPException(502, f"SLS 查询失败: {str(e)[:200]}")


def _search_response(result: dict, source_name: str, typ: str,
                     project: str, logstore: str, target: str,
                     query: str, range_key: str, page: int, page_size: int) -> dict:
    """统一响应：原 SLS 响应键全保留，多源扩展键为 source/type/target/paged。"""
    return {
        "source": source_name,
        "type": typ,
        "project": project or "",
        "logstore": logstore or "",
        "target": target or "",
        "query": query,
        "range": range_key,
        "page": page,
        "page_size": page_size,
        "is_sql": result["is_sql"],
        # sls/es 有 offset 翻页；cls/loki 单页（has_more 仅提示还有）
        "paged": bool((typ in ("sls", "es")) and not result["is_sql"]),
        "count": result["count"],
        "has_more": result["has_more"],
        "logs": result["logs"],
    }


@router.get("/search")
def sls_search(
    project: Optional[str] = Query(None),
    logstore: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    target: Optional[str] = Query(None),
    query: str = Query(""),
    range: str = Query("1h"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),  # SLS 检索 line 上限 100
    reverse: bool = Query(True),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """日志检索（多源统一）。query 含 `|` 在 SLS/CLS 走 SQL 分析；支持 7d 内档位。

    source 缺省=第一个可用源；旧调用（带 project/logstore、不带 source/target）
    走 SLS 原路径——SLS 现有查询行为向后兼容（默认源语义）。
    """
    seconds = RANGE_SECONDS.get(range)
    if seconds is None:
        raise HTTPException(400, f"不支持的时间档位: {range}（可选: {list(RANGE_SECONDS)}）")
    end = int(time.time())
    start = end - seconds

    # ---- 兼容路径：SLS 现有查询行为向后兼容（默认源语义）----
    if source is None and target is None and project and logstore:
        try:
            ak, sk, _src = resolve_sls_credential(db)
        except CredentialError:
            raise HTTPException(503, _NO_CREDS_NOTE)
        try:
            result = query_logs(
                ak, sk, project, logstore, query,
                start=start, end=end,
                limit=page_size, offset=(page - 1) * page_size, reverse=reverse,
            )
        except SLSQueryError as e:
            # 常见的是查询语法/参数问题，返回 400 让前端直接显示 SLS 报错
            raise HTTPException(400, str(e))
        except Exception as e:
            logger.exception("SLS 检索失败")
            raise HTTPException(502, f"SLS 检索失败: {str(e)[:200]}")
        return _search_response(result, "", "sls", project, logstore, "",
                                query, range, page, page_size)

    # ---- 统一路径：按 source 分发（缺省=第一个可用源）----
    try:
        src = log_query.resolve_source(db, name=source or "",
                                       project=project or "", logstore=logstore or "")
        result = log_query.query(
            db, src, query, start=start, end=end,
            limit=page_size, offset=(page - 1) * page_size,
            project=project or "", logstore=logstore or "",
            target=target or "", reverse=reverse,
        )
    except log_query.LogSourceError as e:
        raise HTTPException(e.status, str(e))
    except (SLSQueryError, CLSQueryError, LokiQueryError, ESQueryError) as e:
        # 查询语法/参数问题，400 让前端直接显示后端报错
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("日志检索失败（source=%s）", source)
        raise HTTPException(502, f"日志检索失败: {str(e)[:200]}")
    return _search_response(result, src["name"], src["type"],
                            project or "", logstore or "", target or "",
                            query, range, page, page_size)


# ---------------- 常用查询（团队共享，支持 source/target 多源） ----------------


@router.get("/saved")
def list_saved_queries(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    rows = db.query(SavedQuery).order_by(SavedQuery.id.desc()).all()
    return [
        {
            "id": r.id, "name": r.name,
            "source": r.source, "target": r.target,
            "project": r.project, "logstore": r.logstore,
            "query": r.query, "time_range": r.time_range, "created_by": r.created_by,
        }
        for r in rows
    ]


@router.post("/saved")
def create_saved_query(
    body: SavedQueryCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    if db.query(SavedQuery).filter(SavedQuery.name == body.name).first():
        raise HTTPException(400, "查询名称已存在")
    if body.time_range not in RANGE_SECONDS:
        raise HTTPException(400, f"不支持的时间档位: {body.time_range}")
    source = (body.source or "").strip()
    target = (body.target or "").strip()
    project = (body.project or "").strip()
    logstore = (body.logstore or "").strip()
    # 目标校验按源类型（source 空=旧行语义 SLS，需 project/logstore）
    if source:
        entry = next((e for e in log_query.source_entries(db)
                      if e["name"] == source), None)
        if entry is None:
            raise HTTPException(400, f"日志源不存在: {source}")
        if entry["type"] == "sls" and not (project and logstore):
            raise HTTPException(400, "SLS 常用查询需要 project 与 logstore")
        if entry["type"] in ("cls", "es") and not target:
            raise HTTPException(400, "CLS/ES 常用查询需要 target（主题/索引）")
    elif not (project and logstore):
        raise HTTPException(400, "缺查询目标（SLS 需 project/logstore；CLS/ES 需 target）")
    row = SavedQuery(
        name=body.name, source=source, target=target,
        project=project, logstore=logstore,
        query=body.query, time_range=body.time_range, created_by=user.id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db, action="logs.saved_query.create", user_id=user.id, username=user.username,
        resource_type="saved_query", resource_id=row.id,
        detail={"name": body.name, "source": source, "target": target,
                "project": project, "logstore": logstore},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {
        "id": row.id, "name": row.name,
        "source": row.source, "target": row.target,
        "project": row.project, "logstore": row.logstore,
        "query": row.query, "time_range": row.time_range, "created_by": row.created_by,
    }


@router.delete("/saved/{query_id}")
def delete_saved_query(
    query_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    row = db.get(SavedQuery, query_id)
    if row is None:
        raise HTTPException(404, "常用查询不存在")
    name = row.name
    db.delete(row)
    audit.record(
        db, action="logs.saved_query.delete", user_id=user.id, username=user.username,
        resource_type="saved_query", resource_id=query_id, detail={"name": name},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}
