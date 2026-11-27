"""K8s 容器监控 API：接入/资源/日志/失败分析/保留配置。

POST /k8s/clusters            接入集群（API 地址+Token，token 加密落库）
POST /k8s/clusters/test       连通测试（不保存）
GET  /k8s/clusters            集群列表
GET  /k8s/overview            资源总览（各 kind 计数+失败统计）
POST /k8s/scan                手动扫描（资源同步+失败检测）
GET  /k8s/resources           资源清单（kind/namespace 筛选）
GET  /k8s/logs                容器日志（ns/pod/关键字，含已销毁 pod 的历史）
GET/PUT /k8s/log-retention    日志保留天数（修改即清存量）
GET  /k8s/failures            失败列表（状态筛选）
POST /k8s/failures/batch      批量忽略/确认
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.k8s import K8sCluster, K8sFailure, K8sPodLog, K8sResource
from app.models.user import User

logger = logging.getLogger("qianshu.k8s_api")
router = APIRouter(prefix="/k8s", tags=["k8s"])


class ClusterIn(BaseModel):
    name: str = Field(max_length=64)
    api_url: str = Field(default="", max_length=255)
    token: str = Field(default="", max_length=4096)
    kubeconfig: str = Field(default="", max_length=65536)   # 粘贴 kubeconfig 自动提取 server/token
    verify_tls: bool = True
    ns_scope: str = Field(default="", max_length=512)
    # 管理面（备份/升级/清空/加节点走控制面 SSH，随集群登记——换环境改配置不改代码）
    ssh_host: str = Field(default="", max_length=64)
    ssh_user: str = Field(default="root", max_length=64)
    read_only: bool = True   # 安全默认只读；登记测试集群显式传 False 开管理操作


class RetentionIn(BaseModel):
    days: int = Field(ge=0, le=3650)   # 0=永久保留，与系统参数口径一致


class BatchIn(BaseModel):
    ids: list[int]
    action: str = Field(pattern="^(ignore|confirm)$")
    note: str = Field(default="", max_length=500)
    confirm_count: int = 0   # 数量核对门（批量模式下须等于 ids 数）


def _cluster_out(c: K8sCluster) -> dict:
    return {"id": c.id, "name": c.name, "api_url": c.api_url,
            "verify_tls": c.verify_tls, "ns_scope": c.ns_scope,
            "ssh_host": c.ssh_host, "ssh_user": c.ssh_user, "read_only": c.read_only,
            "status": c.status, "status_note": c.status_note,
            "synced_at": c.synced_at.isoformat(timespec="seconds") if c.synced_at else None}


def _ns_scopes(db: Session, user: User) -> dict:
    """cluster_id → 可见命名空间口径（None=不过滤；空集=已收口但无授权=全不可见）。

    查询按 Namespace 授权过滤，只读集群/无授权行的集群不过滤，保持存量可见范围。
    """
    from app.services.authz import ns_scope

    return {c.id: ns_scope(db, user, c) for c in db.query(K8sCluster).all()}


def _rows_visible(rows, scopes: dict) -> list:
    from app.services.authz import ns_row_ok

    return [r for r in rows if ns_row_ok(scopes.get(r.cluster_id), r.namespace)]


@router.post("/clusters")
def create_cluster(body: ClusterIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    from app.core.security import encrypt_secret

    api_url, token = body.api_url, body.token
    if body.kubeconfig.strip() and (not api_url or not token):
        try:
            import base64 as _b64

            import yaml as _y

            kc = _y.safe_load(body.kubeconfig)
            cluster0 = (kc.get("clusters") or [{}])[0].get("cluster") or {}
            user0 = (kc.get("users") or [{}])[0].get("user") or {}
            api_url = api_url or cluster0.get("server", "")
            token = token or user0.get("token", "")
            if not token and user0.get("client-key-data"):
                token = "KUBECONFIG-CERT"   # 证书型凭据标记（后续扩展）
        except Exception:
            raise HTTPException(400, "kubeconfig 解析失败（请检查内容或改填 API 地址+Token）")
    if not api_url or not token:
        raise HTTPException(400, "API 地址与 Token 必填（或粘贴 kubeconfig 自动提取）")
    body = body.model_copy(update={"api_url": api_url, "token": token})
    if db.query(K8sCluster).filter(K8sCluster.name == body.name).first():
        raise HTTPException(400, "集群名已存在——多集群请用不同名称（集群管理里可查看已接入集群）")
    if db.query(K8sCluster).filter(K8sCluster.api_url == body.api_url.rstrip("/")).first():
        raise HTTPException(400, "该集群地址已接入——重复接入请到「K8s 管理-集群管理」查看，勿重复添加")
    c = K8sCluster(name=body.name, api_url=body.api_url.rstrip("/"),
                   token_cipher=encrypt_secret(body.token),
                   verify_tls=body.verify_tls, ns_scope=body.ns_scope,
                   ssh_host=body.ssh_host, ssh_user=body.ssh_user or "root",
                   read_only=body.read_only)
    db.add(c)
    db.commit()
    return _cluster_out(c)


class ClusterPatchIn(BaseModel):
    # 一律"传了才改"（None=保持原值）——防止部分更新误清空已有配置
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    read_only: Optional[bool] = None
    token: str = Field(default="", max_length=4096)
    ns_scope: Optional[str] = None
    verify_tls: Optional[bool] = None   # kubeadm 自签集群需跳过校验


@router.patch("/clusters/{cid}")
def patch_cluster(cid: int, body: ClusterPatchIn, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin"))):
    """改集群管理面配置（SSH/只读开关/换 token）——开写动作留审计。"""
    from app.services import audit as audit_svc

    c = db.get(K8sCluster, cid)
    if c is None:
        raise HTTPException(404, "集群不存在")
    was_ro = c.read_only
    if body.ssh_host is not None:
        c.ssh_host = body.ssh_host
    if body.ssh_user is not None:
        c.ssh_user = body.ssh_user or "root"
    if body.read_only is not None:
        c.read_only = body.read_only
    if body.verify_tls is not None:
        c.verify_tls = body.verify_tls
    if body.ns_scope is not None:
        c.ns_scope = body.ns_scope   # 可清空（空=全部命名空间）
    if body.token:
        from app.core.security import encrypt_secret

        c.token_cipher = encrypt_secret(body.token)
        c.rotated_at = __import__("datetime").datetime.now()   # 凭据轮换时间留痕
    audit_svc.record(
        db, action="k8s.cluster_update", user_id=user.id, username=user.username,
        resource_type="k8s_cluster", resource_id=str(cid),
        detail={"read_only": f"{was_ro}→{c.read_only}", "ssh_host": c.ssh_host},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _cluster_out(c)


@router.post("/clusters/test")
def test_cluster(body: ClusterIn, user: User = Depends(require_roles("admin", "operator"))):
    """连通测试（不保存）。"""
    from app.collector.k8s import ping

    return ping(body.api_url, body.token, body.verify_tls)


@router.get("/clusters")
def list_clusters(db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator", "viewer"))):
    scopes = _ns_scopes(db, user)
    out = []
    for c in db.query(K8sCluster).order_by(K8sCluster.id).all():
        sc = scopes.get(c.id)
        if sc is not None and not sc:
            continue   # 已收口集群且无任何授权 → 对该用户不可见
        out.append(_cluster_out(c))
    return out


@router.get("/overview")
def overview(cluster_id: int = Query(0, description="0=全部集群；>0 时总数按该集群口径统计"),
             db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin", "operator", "viewer"))):
    from sqlalchemy import func

    scopes = _ns_scopes(db, user)
    restricted = any(sc is not None for sc in scopes.values())

    def _ns_cond(model, sc):
        """授权口径下追加的命名空间条件（None=不加；'*' 通配；空集=无可看）。"""
        if sc is None or "*" in sc:
            return None
        names = [n for n in sc if n]
        return model.namespace.in_(names) if names else model.id < 0

    if not restricted:
        kq = (db.query(K8sResource.kind, func.count(K8sResource.id))
              .filter(K8sResource.is_active == True))  # noqa: E712
        if cluster_id:
            kq = kq.filter(K8sResource.cluster_id == cluster_id)
        kinds = dict(kq.group_by(K8sResource.kind).all())
    else:
        kinds = {}
        for c in db.query(K8sCluster).order_by(K8sCluster.id).all():
            if cluster_id and c.id != cluster_id:
                continue
            sc = scopes.get(c.id)
            if sc is not None and not sc:
                continue
            q = (db.query(K8sResource.kind, func.count(K8sResource.id))
                 .filter(K8sResource.cluster_id == c.id, K8sResource.is_active == True))  # noqa: E712
            cond = _ns_cond(K8sResource, sc)
            if cond is not None:
                q = q.filter(cond)
            for k, n in q.group_by(K8sResource.kind).all():
                kinds[k] = kinds.get(k, 0) + n
    clusters_view = []
    failed_sum = 0
    logs_sum = 0
    for c in db.query(K8sCluster).order_by(K8sCluster.id).all():
        sc = scopes.get(c.id)
        if sc is not None and not sc:
            continue
        nq = (db.query(func.count(K8sResource.id))
              .filter(K8sResource.cluster_id == c.id, K8sResource.is_active == True))  # noqa: E712
        fq1 = db.query(func.count(K8sFailure.id)).filter(
            K8sFailure.cluster_id == c.id, K8sFailure.status == "pending")
        lq1 = db.query(func.count(K8sPodLog.id)).filter(K8sPodLog.cluster_id == c.id)
        ckq = (db.query(K8sResource.kind, func.count(K8sResource.id))
               .filter(K8sResource.cluster_id == c.id, K8sResource.is_active == True))  # noqa: E712
        cond = _ns_cond(K8sResource, sc)
        if cond is not None:
            nq = nq.filter(cond)
            ckq = ckq.filter(cond)
        fcond = _ns_cond(K8sFailure, sc)
        if fcond is not None:
            fq1 = fq1.filter(fcond)
        lcond = _ns_cond(K8sPodLog, sc)
        if lcond is not None:
            lq1 = lq1.filter(lcond)
        n_res = nq.scalar() or 0
        n_fail = fq1.scalar() or 0
        failed_sum += n_fail
        logs_sum += lq1.scalar() or 0
        # 每集群按 kind 计数，供卡片数字跳转对应资源清单
        ck = dict(ckq.group_by(K8sResource.kind).all())
        clusters_view.append({**_cluster_out(c), "resources": n_res,
                              "pending_failures": n_fail, "kinds": ck})
    fq = db.query(func.count(K8sFailure.id)).filter(K8sFailure.status == "pending")
    if cluster_id:
        fq = fq.filter(K8sFailure.cluster_id == cluster_id)
    failed = fq.scalar() or 0
    lq = db.query(func.count(K8sPodLog.id))
    if cluster_id:
        lq = lq.filter(K8sPodLog.cluster_id == cluster_id)
    log_rows = lq.scalar() or 0
    if restricted:
        # 授权口径下全局计数=各可见集群（按 ns 授权过滤）之和，与卡片口径一致
        failed = failed_sum
        log_rows = logs_sum
    return {"resources": kinds, "pending_failures": failed, "log_rows": log_rows,
            "clusters": clusters_view,
            "retention_days": __import__("app.services.k8s_monitor", fromlist=["log_retention_days"]).log_retention_days()}


@router.post("/scan")
def scan(cluster_id: int = Query(0, description="0=全部集群"),
         db: Session = Depends(get_db),
         user: User = Depends(require_roles("admin", "operator"))):
    """手动扫描：资源同步 + 失败检测。cluster_id 指定时只同步该集群
    （集群详情里点「同步」不应把其他集群一起串行扫一遍）。"""
    from app.services.k8s_monitor import scan_failures, sync_cluster

    q = db.query(K8sCluster)
    if cluster_id:
        q = q.filter(K8sCluster.id == cluster_id)
    clusters = q.all()
    if not clusters:
        raise HTTPException(400, "未接入集群（集成中心-数据源-Kubernetes）")
    out = []
    for c in clusters:
        try:
            r = sync_cluster(db, c)
            f = scan_failures(db, c)
            out.append({**r, "new_failures": len(f)})
        except Exception as e:
            db.rollback()
            out.append({"cluster": c.name, "error": str(e)[:120], "new_failures": 0})
    return {"ok": True, "clusters": out}


@router.get("/resources")
def resources(kind: str = Query("Pod"), namespace: str = "",
              keyword: str = "", include_destroyed: int = 0, status: str = "",
              cluster_id: int = Query(0, description="0=全部集群"),
              db: Session = Depends(get_db),
              user: User = Depends(require_roles("admin", "operator", "viewer"))):
    import json as _json

    q = db.query(K8sResource).filter(K8sResource.kind == kind)
    if cluster_id:
        q = q.filter(K8sResource.cluster_id == cluster_id)
    if namespace:
        q = q.filter(K8sResource.namespace == namespace)
    if not include_destroyed:
        q = q.filter(K8sResource.is_active == True)  # noqa: E712
    if status:
        q = q.filter(K8sResource.status_json.like(f'%"status": "{status}"%'))
    rows = q.order_by(K8sResource.namespace, K8sResource.name).limit(500).all()
    rows = _rows_visible(rows, _ns_scopes(db, user))   # Namespace 授权过滤
    # cluster_id → 集群名，供 Node 等资源行显示所属集群
    cluster_names = dict(db.query(K8sCluster.id, K8sCluster.name).all())
    out = []
    for r in rows:
        if keyword and keyword.lower() not in r.name.lower() and keyword.lower() not in r.namespace.lower():
            continue
        try:
            st = _json.loads(r.status_json or "{}")
        except Exception:
            st = {}
        out.append({"id": r.id, "kind": r.kind, "namespace": r.namespace, "name": r.name,
                    "cluster_id": r.cluster_id,
                    "cluster": cluster_names.get(r.cluster_id, ""),
                    "is_active": r.is_active, "observed_at": r.observed_at.isoformat(timespec="seconds"),
                    "status": st.get("status", ""), "ready": st.get("ready"),
                    "restarts": st.get("restarts"), "node": st.get("node"),
                    # Node 行的 IP = 节点 InternalIP（pod_ip/cluster_ip 对 Node 均为空）
                    "ip": st.get("pod_ip") or st.get("cluster_ip") or st.get("internal_ip") or "",
                    "kubelet": st.get("kubelet", ""),
                    "internal_ip": st.get("internal_ip", ""),
                    "created": st.get("created", ""),
                    "hosts": st.get("hosts"), "cluster_ip": st.get("cluster_ip"),
                    "labels": _json.loads(r.labels_json or "{}"),
                    "matched_pods": st.get("matched_pods") or [],
                    "chain": st.get("chain") or [],
                    "containers": st.get("containers") or []})

    # 节点水位按 InternalIP/主机名匹配机器管理登记，
    # 补 CPU/内存/磁盘水位；未登记= machine: null（前端显示 —+「去机器管理登记」提示）
    if kind == "Node" and out:
        from app.models.machine import K8sMachine

        machines = db.query(K8sMachine).all()
        by_ip = {m.ip: m for m in machines if m.ip}
        by_host = {(m.host_name or "").lower(): m for m in machines if m.host_name}
        for item in out:
            m = (by_ip.get(item.get("internal_ip") or "")
                 or by_ip.get(item.get("name") or "")
                 or by_host.get((item.get("name") or "").lower()))
            item["machine"] = None if m is None else {
                "id": m.id, "host_name": m.host_name, "ip": m.ip,
                "cpu_pct": m.cpu_pct, "mem_pct": m.mem_pct, "disk_pct": m.disk_pct,
            }
    return {"items": out, "total": len(out)}


@router.get("/logs")
def logs(namespace: str = Query(""), pod: str = "", keyword: str = "",
         include_destroyed: int = 1, limit: int = Query(100, ge=1, le=500),
         cluster_id: int = Query(0),
         db: Session = Depends(get_db),
         user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """容器日志查询（默认含已销毁 pod 的历史——日志在系统内留存）。

    按 K8s 日志保留天数夹时间窗，设置修改后读路径立即生效；
    物理清理由保存即清/每日清理执行，保证读写路径口径一致。"""
    from datetime import datetime, timedelta

    from app.services.k8s_monitor import log_retention_days

    q = db.query(K8sPodLog)
    days = log_retention_days()
    if days > 0:
        q = q.filter(K8sPodLog.ts >= datetime.now() - timedelta(days=days))
    if cluster_id:
        q = q.filter(K8sPodLog.cluster_id == cluster_id)
    if namespace:
        q = q.filter(K8sPodLog.namespace == namespace)
    if pod:
        q = q.filter(K8sPodLog.pod == pod)
    if keyword:
        q = q.filter(K8sPodLog.line.ilike(f"%{keyword}%"))
    rows = q.order_by(K8sPodLog.ts.desc()).limit(limit).all()
    rows = _rows_visible(rows, _ns_scopes(db, user))   # Namespace 授权过滤
    return {"items": [{
        "ts": r.ts.isoformat(timespec="seconds"), "namespace": r.namespace,
        "pod": r.pod, "container": r.container, "stream": r.stream, "line": r.line,
    } for r in rows], "total": len(rows)}


@router.get("/log-retention")
def get_retention(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    from app.services.k8s_monitor import log_retention_days

    return {"days": log_retention_days()}


@router.put("/log-retention")
def put_retention(body: RetentionIn, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin"))):
    """修改保留天数：立即按新规则清存量。

    与 PUT /settings/retention 的 k8s_days 是**同一个配置**：同一 env 键
    K8S_LOG_RETENTION_DAYS、同一 env_store 写口、同一清存入口 cleanup_logs。
    本端点保留为 K8s 页快捷入口，保存语义与系统参数路径一致，并写审计留痕。"""
    from app.core.config import settings as st
    from app.services import audit as audit_svc
    from app.services.k8s_monitor import cleanup_logs

    st.K8S_LOG_RETENTION_DAYS = body.days
    # 写 .env 持久化（统一 env_store 原子写）
    from app.services.env_store import set_keys
    set_keys({"K8S_LOG_RETENTION_DAYS": body.days})
    removed = cleanup_logs(db)   # 立即清存量
    # 审计与 系统设置-系统参数 保存同口径（action/resource 一致，detail 标注入口）
    audit_svc.record(
        db, action="settings.update", user_id=user.id, username=user.username,
        resource_type="settings", resource_id="retention",
        detail={"scope": "retention", "values": {"k8s_days": body.days},
                "via": "k8s.log-retention"},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "days": body.days, "cleaned_rows": removed}


@router.get("/failures")
def failures(status: str = Query(""), limit: int = Query(100, ge=1, le=500),
             cluster_id: int = Query(0),
             db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin", "operator", "viewer"))):
    q = db.query(K8sFailure)
    if cluster_id:
        q = q.filter(K8sFailure.cluster_id == cluster_id)
    if status:
        q = q.filter(K8sFailure.status == status)
    rows = q.order_by(K8sFailure.created_at.desc()).limit(limit).all()
    rows = _rows_visible(rows, _ns_scopes(db, user))   # Namespace 授权过滤
    return {"items": [{
        "id": r.id, "namespace": r.namespace, "pod": r.pod, "container": r.container,
        "failure_type": r.failure_type, "diagnosis": r.diagnosis, "status": r.status,
        "note": r.note, "confirmed_by": r.confirmed_by,
        "created_at": r.created_at.isoformat(timespec="seconds"),
    } for r in rows], "total": len(rows)}


@router.post("/failures/batch")
def failures_batch(body: BatchIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin", "operator"))):
    """批量忽略/确认（确认带操作人留痕）。"""
    from app.services import audit as audit_svc

    if not body.ids:
        raise HTTPException(400, "未选择条目")
    if body.confirm_count and body.confirm_count != len(body.ids):
        raise HTTPException(400, f"确认数量不符（需填 {len(body.ids)}）——批量操作被拒绝")
    rows = db.query(K8sFailure).filter(K8sFailure.id.in_(body.ids)).all()
    for r in rows:
        r.status = "ignored" if body.action == "ignore" else "confirmed"
        r.note = body.note[:500]
        r.confirmed_by = user.username if body.action == "confirm" else r.confirmed_by
        # 确认时双写 confirmed_by=用户名、confirmed_by_id=users.id
        r.confirmed_by_id = user.id if body.action == "confirm" else r.confirmed_by_id
        r.updated_at = __import__("datetime").datetime.now()
    audit_svc.record(
        db, action=f"k8s.failures_{body.action}", user_id=user.id, username=user.username,
        resource_type="k8s_failure", resource_id=",".join(map(str, body.ids[:20])),
        detail={"count": len(rows), "note": body.note[:100]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "updated": len(rows)}


@router.get("/namespaces")
def namespaces(db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """命名空间列表（从已同步资源提取——前端下拉选择，不手输）。"""
    from sqlalchemy import distinct

    from app.services.authz import ns_row_ok

    rows = db.query(distinct(K8sResource.namespace)).filter(
        K8sResource.namespace != "").order_by(K8sResource.namespace).all()
    scopes = _ns_scopes(db, user)
    if any(sc is not None for sc in scopes.values()):
        allowed = set()
        for sc in scopes.values():
            if sc is None or "*" in sc:
                allowed.add("*")
            else:
                allowed |= sc
        return [r[0] for r in rows if ns_row_ok(allowed, r[0])]
    return [r[0] for r in rows]


@router.get("/pods")
def pods(namespace: str = Query(""),
         include_destroyed: int = 0,
         db: Session = Depends(get_db),
         user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """Pod 名列表（ns 联动下拉）。"""
    q = db.query(K8sResource).filter(K8sResource.kind == "Pod")
    if namespace:
        q = q.filter(K8sResource.namespace == namespace)
    if not include_destroyed:
        q = q.filter(K8sResource.is_active == True)  # noqa: E712
    rows = q.order_by(K8sResource.name).limit(300).all()
    rows = _rows_visible(rows, _ns_scopes(db, user))   # Namespace 授权过滤
    return [r.name for r in rows]


@router.delete("/clusters/{cid}")
def delete_cluster(cid: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    """删除集群（连带其资源/日志/失败记录）。"""
    from app.services import audit as audit_svc

    c = db.get(K8sCluster, cid)
    if c is None:
        raise HTTPException(404, "集群不存在")
    if c.read_only:
        raise HTTPException(403, f"集群「{c.name}」为只读登记（生产集群）——禁止删除登记；如确需删除先在「配置」里切换为可管理")
    db.query(K8sResource).filter(K8sResource.cluster_id == cid).delete()
    db.query(K8sPodLog).filter(K8sPodLog.cluster_id == cid).delete()
    db.query(K8sFailure).filter(K8sFailure.cluster_id == cid).delete()
    name = c.name
    db.delete(c)
    audit_svc.record(
        db, action="k8s.cluster_delete", user_id=user.id, username=user.username,
        resource_type="k8s_cluster", resource_id=str(cid), detail={"name": name},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "deleted": name}


@router.post("/clusters/{cid}/test")
def test_saved_cluster(cid: int, db: Session = Depends(get_db),
                       user: User = Depends(require_roles("admin", "operator"))):
    """已接入集群连通测试。"""
    from app.collector.k8s import ping
    from app.services.k8s_monitor import cluster_creds

    c = db.get(K8sCluster, cid)
    if c is None:
        raise HTTPException(404, "集群不存在")
    api_url, token, verify = cluster_creds(db, c)
    r = ping(api_url, token, verify)
    c.status = "ok" if r["ok"] else "error"
    c.status_note = r["detail"][:255]
    db.commit()
    return r
