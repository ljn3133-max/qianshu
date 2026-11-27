"""K8s 管理平台 API：任务中心/节点/备份/升级/清空/资源操作/统计/事件。

安全门（高危操作）：升级/清空/驱逐/删除必须 confirm_name 与集群名/资源名一致，否则 400 拒绝。
写操作仅限 read_only=0 的登记测试集群（生产集群默认只读，随集群登记配置）。
目标机一律来自集群登记（ssh_host/ssh_user），不硬编码任何环境事实。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.k8s import K8sCluster, K8sFailure, K8sPodLog, K8sResource
from app.models.k8s_task import K8sTask, K8sTaskLog
from app.models.user import User

logger = logging.getLogger("qianshu.k8s_mgmt_api")
_EVENTS_CACHE: dict = {}   # 事件查询 TTL 缓存（key→(ts, items)）
_SLOW_CACHE: dict = {}     # SSH 型慢接口缓存（charts/images/helm/secrets，5min TTL——504 根因治理）
_SLOW_REFRESHING: set = set()   # SWR 后台刷新去重（同 key 只允许一个刷新线程）


def _cached(key: str, ttl: int, fn):
    """SSH 型慢接口缓存，stale-while-revalidate：
    - 新鲜：直接返回；
    - 过期但有旧值：立即返回旧值 + 后台线程刷新（用户永不等 SSH，网关不再被拖穿）；
    - 完全无缓存（首次）：同步计算（多集群已并行，秒级）。
    过期时不能同步重算：多集群串行 SSH 可能超过网关 30s 超时并周期性触发 504。"""
    import time as _t

    hit = _SLOW_CACHE.get(key)
    now = _t.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    if hit:   # stale：先回旧值，后台刷新
        if key not in _SLOW_REFRESHING:
            _SLOW_REFRESHING.add(key)

            def _refresh():
                try:
                    _SLOW_CACHE[key] = (_t.time(), fn())
                except Exception as e:   # 刷新失败保留旧值，下次再试
                    logger.warning("SWR 后台刷新失败 %s: %s", key, e)
                finally:
                    _SLOW_REFRESHING.discard(key)

            threading.Thread(target=_refresh, daemon=True, name=f"swr-{key}").start()
        return hit[1]
    val = fn()
    _SLOW_CACHE[key] = (_t.time(), val)
    return val
router = APIRouter(prefix="/k8mgmt", tags=["k8mgmt"])

# shell 参数白名单（kubectl 参数直拼命令——kind/name/ns 一律过正则，防注入）
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}$")
_KINDS = {"Node", "Pod", "Service", "Ingress", "Deployment", "Namespace",
          "ConfigMap", "Secret", "StatefulSet", "DaemonSet", "CronJob", "Job",
          "PersistentVolume", "PersistentVolumeClaim", "StorageClass"}


def _safe(kind: str, name: str, namespace: str = "") -> None:
    if kind not in _KINDS:
        raise HTTPException(400, f"资源类型不支持：{kind}")
    if not _NAME_RE.match(name or ""):
        raise HTTPException(400, "资源名非法")
    if namespace and not _NAME_RE.match(namespace):
        raise HTTPException(400, "命名空间非法")


def _admin_ssh(db: Session, cluster_name: str, cluster_id: int = 0) -> tuple:
    """管理面 SSH（集群登记解析）——写操作专用通道。
    严格模式：无显式 ssh_host 的集群直接拒绝，绝不拿 api_url 猜 SSH，
    避免误连生产控制面 IP 后 Permission denied。"""
    from app.services.k8s_ops import resolve_master_ssh

    host, user, c = resolve_master_ssh(db, cluster_name, cluster_id, strict=True)
    if not host:
        raise HTTPException(400, "该集群未登记管理面 SSH（集群管理里补 ssh_host）——此操作不可用；只读功能不受影响")
    return host, user, c


def _cluster_api(db: Session, cluster_name: str, cluster_id: int = 0) -> tuple:
    """只读通道：集群登记 → API 凭证（token），与 SSH 无关。"""
    from app.services.k8s_monitor import cluster_creds

    q = db.query(K8sCluster)
    c = (q.filter(K8sCluster.id == cluster_id).first() if cluster_id
         else (q.filter(K8sCluster.name == cluster_name).first() if cluster_name
               else q.order_by(K8sCluster.id).first()))
    if c is None:
        raise HTTPException(400, "未接入集群（K8s 管理-集群管理）")
    api_url, token, verify = cluster_creds(db, c)
    if not token:
        raise HTTPException(400, f"集群「{c.name}」未配置 SA Token——接入时填写只读 token")
    return api_url, token, verify, c


def _assert_writable(c) -> None:
    """read_only 集群（默认）禁止一切变更/高危操作。"""
    if c is not None and c.read_only:
        raise HTTPException(403, f"集群「{c.name}」为只读登记——变更/高危操作需在集群管理里切换为可管理")


def _ns_guard(db: Session, user: User, cluster_id: int, cluster_name: str,
              namespace: str, need: str, for_op: bool = True) -> None:
    """Namespace 授权门：查询/操作按授权过滤（need=view/use/admin）。

    for_op=True（默认）=操作面：非 admin 必须持授权（授权只增不减）；
    for_op=False=查询面：无授权行的集群不过滤（存量零变化）。
    只读集群不受授权影响（read_only 硬门在 _assert_writable，保持原报错口径）；
    语义全表见 services/authz.py 模块注释。放在 _admin_ssh 之前——无授权不碰任何通道。
    """
    from app.services.authz import require_ns, resolve_cluster

    c = resolve_cluster(db, cluster_id, cluster_name)
    require_ns(db, user, c, namespace or "", need, for_op=for_op)


def _machine_creds(db: Session, machine_id: int) -> dict:
    """机器库 → SSH 凭证（管理任务从库选机，不现填密码）。"""
    from app.core.security import decrypt_secret
    from app.models.machine import K8sMachine

    m = db.get(K8sMachine, machine_id)
    if m is None:
        raise HTTPException(404, "机器不存在（机器管理-机器列表）")
    pwd = ""
    if m.ssh_password_cipher:
        try:
            pwd = decrypt_secret(m.ssh_password_cipher)
        except Exception:
            raise HTTPException(400, f"机器「{m.ip}」凭证解密失败（MASTER_KEY 变更？）")
    return {"ip": m.ip, "ssh_user": m.ssh_user, "ssh_password": pwd}


# ================= 任务中心 =================

@router.get("/tasks")
def list_tasks(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin", "operator", "viewer"))):
    from app.services.task_engine import task_out

    rows = db.query(K8sTask).order_by(K8sTask.id.desc()).limit(limit).all()
    return {"items": [task_out(t) for t in rows], "total": len(rows)}


@router.get("/tasks/{tid}")
def get_task(tid: int, db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin", "operator", "viewer"))):
    from app.services.task_engine import task_out

    t = db.get(K8sTask, tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return task_out(t)


@router.get("/tasks/{tid}/logs")
def task_logs(tid: int, after_id: int = Query(0), limit: int = Query(200, ge=1, le=1000),
              db: Session = Depends(get_db),
              user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """任务日志（增量拉取，前端 2 秒轮询）。"""
    q = db.query(K8sTaskLog).filter(K8sTaskLog.task_id == tid)
    if after_id:
        q = q.filter(K8sTaskLog.id > after_id)
    rows = q.order_by(K8sTaskLog.id).limit(limit).all()
    return {"items": [{"id": r.id, "ts": r.ts.isoformat(timespec="seconds"), "line": r.line}
                      for r in rows]}


@router.post("/tasks/{tid}/abort")
def abort_task(tid: int, db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin", "operator"))):
    """终止任务（running/pending → aborted）。"""
    t = db.get(K8sTask, tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if t.status not in ("running", "pending"):
        raise HTTPException(400, f"任务状态 {t.status} 不可终止")
    t.status = "aborted"
    t.finished_at = datetime.now()
    db.add(K8sTaskLog(task_id=tid, line="【操作】用户请求终止任务"))
    db.commit()
    return {"ok": True}


# ================= 添加 Node =================

class AddNodeIn(BaseModel):
    cluster_id: int = 0
    machine_id: int = 0   # 从机器库选机（凭证托管）
    ip: str = Field(max_length=64)
    ssh_user: str = Field(default="root", max_length=64)
    ssh_password: str = Field(default="", max_length=128)
    master_ssh: str = Field(default="", max_length=64)
    master_user: str = Field(default="root", max_length=64)
    k8s_version: str = Field(default="", max_length=32)
    cluster_name: str = Field(default="", max_length=64)


@router.post("/nodes/add")
def add_node(body: AddNodeIn, request: Request, db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin"))):
    """提交添加工作节点任务（每次一台，过程进任务中心）。"""
    from app.services.k8s_ops import add_node_steps, run_add_node
    from app.services.task_engine import submit

    meta = body.model_dump()
    if body.machine_id:
        mc = _machine_creds(db, body.machine_id)
        meta.update(ip=mc["ip"], ssh_user=mc["ssh_user"], ssh_password=mc["ssh_password"])
    if not _NAME_RE.match(meta.get("ip", "")):
        raise HTTPException(400, "目标机 IP/主机名非法")
    if not meta.get("master_ssh"):
        host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
        _assert_writable(c)
        meta["master_ssh"], meta["master_user"] = host, suser
    task = submit(
        db, "add_node", f"添加节点 {body.ip}", meta,
        steps=add_node_steps(),
        runner=lambda tid, log, set_steps: run_add_node(tid, log, set_steps, meta),
        submitted_by=user.username, submitted_by_id=user.id)
    return {"task_id": task.id, "status": task.status}


# ================= 集群备份 =================

class BackupIn(BaseModel):
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)
    master_ssh: str = Field(default="", max_length=64)
    master_user: str = Field(default="root", max_length=64)


@router.get("/backups")
def list_backups(db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator", "viewer"))):
    rows = db.query(K8sTask).filter(K8sTask.task_type == "backup").order_by(K8sTask.id.desc()).limit(20).all()
    from app.services.task_engine import task_out

    return {"items": [task_out(t) for t in rows]}


@router.post("/backups")
def create_backup(body: BackupIn, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator"))):
    """发起集群备份任务（配置+yaml，可恢复）。"""
    from app.services.k8s_ops import run_backup
    from app.services.task_engine import submit

    meta = body.model_dump()
    if not meta.get("master_ssh"):
        meta["master_ssh"], meta["master_user"], _c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    task = submit(db, "backup", f"集群备份 {body.cluster_name or 'default'}", meta,
                  steps=[], runner=lambda tid, log, set_steps: run_backup(tid, log, set_steps, meta),
                  submitted_by=user.username, submitted_by_id=user.id)
    return {"task_id": task.id}


@router.get("/backups/sets")
def backup_sets(cluster_name: str = Query(""), db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """备份集管理（时间/大小/内容清单）——清单来自控制面真实备份目录。
    60s SWR 缓存避免 tar 清点阻塞请求；备份/恢复任务本身 >60s，
    60s TTL 不影响"备份完再开对话框看新集"的时效。"""
    return _cached(f"bsets:{cluster_name}", 60,
                   lambda: _backup_sets_impl(cluster_name, db))


def _backup_sets_impl(cluster_name: str, db: Session):
    from app.collector.ssh import run_ssh

    host, suser, _c = _admin_ssh(db, cluster_name)
    r = run_ssh(host, suser,
                "cd /data/k8s-backup 2>/dev/null && for f in k8s-backup-*.tar.gz; do "
                "[ -f \"$f\" ] || continue; "
                "echo \"$f|$(du -h \"$f\" | cut -f1)|$(stat -c %y \"$f\" | cut -d. -f1)|$(tar tzf \"$f\" | wc -l)\"; done",
                timeout=30)
    items = []
    for ln in (r.get("lines") or []):
        parts = ln.split("|")
        if len(parts) == 4 and parts[0].startswith("k8s-backup-"):
            items.append({"name": parts[0][len("k8s-backup-"):-len(".tar.gz")],
                          "file": parts[0], "size": parts[1], "created_at": parts[2],
                          "entries": int(parts[3]) if parts[3].strip().isdigit() else 0})
    items.sort(key=lambda x: x["name"], reverse=True)
    return {"items": items, "total": len(items), "host": host,
            **({} if r["ok"] else {"note": "备份目录不可读（集群未备份过？）"})}


class RestoreIn(BaseModel):
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)
    backup_set: str = Field(max_length=80)
    confirm_name: str = Field(max_length=64)
    master_ssh: str = Field(default="", max_length=64)
    master_user: str = Field(default="root", max_length=64)


@router.post("/backups/restore")
def restore_backup(body: RestoreIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    """集群恢复（高危）：名称对不上拒绝；恢复前留 .pre-restore 现场。"""
    if body.confirm_name != body.cluster_name:
        raise HTTPException(400, "集群名称不匹配——恢复被拒绝（高危操作需输入集群名称确认）")
    from app.services.k8s_ops import run_restore
    from app.services.task_engine import submit

    meta = body.model_dump()
    if not meta.get("master_ssh"):
        meta["master_ssh"], meta["master_user"], c = _admin_ssh(db, body.cluster_name, body.cluster_id)
        _assert_writable(c)
    task = submit(db, "restore", f"集群恢复 ← {body.backup_set}", meta,
                  steps=[], runner=lambda tid, log, set_steps: run_restore(tid, log, set_steps, meta),
                  submitted_by=user.username, submitted_by_id=user.id)
    return {"task_id": task.id}


# ================= 图形化编辑（表单改字段，服务端改 yaml 后 apply） =================

class FormEditIn(BaseModel):
    cluster_id: int = 0
    confirm_name: str = Field(max_length=255)
    cluster_name: str = Field(default="", max_length=64)
    replicas: Optional[int] = None
    image: str = Field(default="", max_length=512)
    labels: dict = Field(default_factory=dict)
    limits_cpu: str = Field(default="", max_length=32)
    limits_mem: str = Field(default="", max_length=32)
    requests_cpu: str = Field(default="", max_length=32)
    requests_mem: str = Field(default="", max_length=32)


@router.put("/resources/{kind}/{name}/form")
def resource_form_edit(kind: str, name: str, body: FormEditIn,
                       namespace: str = Query(""), db: Session = Depends(get_db),
                       user: User = Depends(require_roles("admin", "operator"))):
    """图形化编辑：白名单字段（副本数/镜像/标签/资源限额）服务端改 yaml 后 apply。
    表单值全部类型化（int/正则串），不进 shell 拼接——注入面为零。
    非 admin 需该命名空间 use 级授权（Namespace 授权委托）。"""
    import re as _re

    import yaml as _yaml

    from app.collector.ssh import run_ssh

    _safe(kind, name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name,
              "" if kind in ("Node", "Namespace") else namespace, "use")
    if body.confirm_name != name:
        raise HTTPException(400, "资源名不匹配——变更被拒绝")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    ns_arg = f"-n {namespace}" if namespace and kind not in ("Node", "Namespace") else ""
    r = run_ssh(host, suser, f"kubectl get {kind} {name} {ns_arg} -o yaml", timeout=60)
    if not r["ok"]:
        raise HTTPException(400, "读取资源现状失败（见 detail）")
    try:
        doc = _yaml.safe_load("\n".join(r.get("lines") or []))
    except Exception:
        raise HTTPException(400, "yaml 解析失败")
    if not isinstance(doc, dict):
        raise HTTPException(400, "资源内容异常")

    changed = []
    WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet")
    if body.replicas is not None:
        if kind not in ("Deployment", "StatefulSet"):
            raise HTTPException(400, f"{kind} 不支持改副本数")
        if not (0 <= body.replicas <= 1000):
            raise HTTPException(400, "副本数范围 0~1000")
        doc.setdefault("spec", {})["replicas"] = int(body.replicas)
        changed.append(f"副本数={body.replicas}")
    if body.image:
        if kind not in WORKLOADS:
            raise HTTPException(400, f"{kind} 不支持改镜像（工作负载才有 Pod 模板）")
        if not _re.fullmatch(r"[a-zA-Z0-9._/:@-]{1,300}", body.image):
            raise HTTPException(400, "镜像地址非法")
        ctrs = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
        if not ctrs:
            raise HTTPException(400, "无容器定义")
        ctrs[0]["image"] = body.image
        changed.append(f"镜像={body.image}")
    if body.labels:
        meta = doc.setdefault("metadata", {})
        labs = meta.setdefault("labels", {})
        for k, v in body.labels.items():
            if not _re.fullmatch(r"[a-zA-Z0-9._/-]{1,63}", str(k)) or not _re.fullmatch(r"[a-zA-Z0-9._/-]{0,63}", str(v)):
                raise HTTPException(400, f"标签非法: {k}")
            labs[str(k)] = str(v)
        changed.append(f"标签×{len(body.labels)}")
    res_keys = [("limits", "cpu", body.limits_cpu), ("limits", "memory", body.limits_mem),
                ("requests", "cpu", body.requests_cpu), ("requests", "memory", body.requests_mem)]
    if any(v for _, _, v in res_keys):
        if kind not in WORKLOADS:
            raise HTTPException(400, f"{kind} 不支持改资源限额")
        ctrs = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
        if not ctrs:
            raise HTTPException(400, "无容器定义")
        res = ctrs[0].setdefault("resources", {})
        for grp, key, val in res_keys:
            if not val:
                continue
            if not _re.fullmatch(r"[0-9.]+(m|Mi|Gi|G|k|M|Ki|Ti)?", val):
                raise HTTPException(400, f"{grp}.{key} 值非法: {val}")
            res.setdefault(grp, {})[key] = val
        changed.append("资源限额已更新")

    if not changed:
        raise HTTPException(400, "没有可应用的变更")
    yml = _yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    script = "kubectl apply -f - <<'QSYAML'\n" + yml + "\nQSYAML"
    r2 = run_ssh(host, suser, script, timeout=120)
    return {"ok": r2["ok"], "changed": changed,
            "detail": "\n".join(r2.get("lines") or [])[:500]}


# ================= 集群升级 =================

class UpgradeIn(BaseModel):
    cluster_id: int = 0
    target_version: str = Field(max_length=32)
    confirm_name: str = Field(max_length=64)
    cluster_name: str = Field(default="", max_length=64)
    master_ssh: str = Field(default="", max_length=64)
    master_user: str = Field(default="root", max_length=64)


@router.get("/upgrade/versions")
def upgrade_versions(cluster_name: str = Query(""), cluster_id: int = Query(0),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """版本监控：当前版本 + 可升级路径（kubeadm 逐 minor 升级规则）——token 通道。"""
    from app.collector.k8s import ping

    current = ""
    try:
        api_url, token, verify, _c = _cluster_api(db, cluster_name, cluster_id)
        r = ping(api_url, token, verify)
        m = re.search(r"v?(\d+\.\d+\.\d+)", r.get("detail", ""))
        if m:
            current = "v" + m.group(1)
    except HTTPException:
        pass
    cur_minor = None
    try:
        cur_minor = int(current.lstrip("v").split(".")[1])
    except (IndexError, TypeError, ValueError):
        pass
    path = ([] if cur_minor is None else
            [f"1.{m}.x" for m in range(cur_minor + 1, min(cur_minor + 4, 33))])
    return {"current": current, "upgrade_path": path,
            "rule": "跨多个次版本需逐级升级（不允许跳 minor）",
            **({} if cur_minor is not None else {"note": "当前版本未知，无法生成升级路径"})}


def _k8s_json(api_url: str, token: str, path: str, verify_tls: bool = True,
              params: Optional[dict] = None) -> dict:
    """预检专用严格只读 GET：非 200/非对象响应均是失败，不能解释为空资源。"""
    from app.collector.k8s import _client

    with _client(api_url, token, verify_tls) as c:
        r = c.get(path, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"{path} HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"{path} 响应不是合法 JSON") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} 响应结构异常")
    return data


@router.get("/upgrade/precheck")
def upgrade_precheck(target: str = Query(...), cluster_name: str = Query(""),
                     cluster_id: int = Query(0),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_roles("admin", "operator"))):
    """升级预检查：证据不完整一律不通过，并留下供启动升级核验的记录。"""
    if not re.fullmatch(r"\d+\.\d+", target or ""):
        raise HTTPException(400, "目标版本格式非法（须 X.Y）")

    # 废弃 API 对照表（升级到 target 版本前的已知废弃）——(apiVersion, kind, REST 路径)
    DEPRECATED = {
        "1.29": [("policy/v1beta1", "PodSecurityPolicy", "/apis/policy/v1beta1/podsecuritypolicies"),
                 ("batch/v1beta1", "CronJob", "/apis/batch/v1beta1/cronjobs")],
        "1.30": [("flowcontrol.apiserver.k8s.io/v1beta2", "FlowSchema",
                  "/apis/flowcontrol.apiserver.k8s.io/v1beta2/flowschemas"),
                 ("flowcontrol.apiserver.k8s.io/v1beta2", "PriorityLevelConfiguration",
                  "/apis/flowcontrol.apiserver.k8s.io/v1beta2/prioritylevelconfigurations")],
        "1.31": [("flowcontrol.apiserver.k8s.io/v1beta3", "PriorityLevelConfiguration",
                  "/apis/flowcontrol.apiserver.k8s.io/v1beta3/prioritylevelconfigurations")],
    }
    findings = []
    incomplete = []
    current = ""
    kube_proxy = {"mode": "", "compatible": None, "note": "kube-proxy 模式尚未探测"}
    c = None
    try:
        api_url, token, verify, c = _cluster_api(db, cluster_name, cluster_id)
    except HTTPException as e:
        incomplete.append(f"集群凭证/接入不可用：{e.detail}")
        findings.append({"note": f"扫描未完成: {e.detail}"})
    except Exception as e:
        incomplete.append(f"集群凭证/接入不可用：{str(e)[:80]}")
        findings.append({"note": f"扫描未完成: {str(e)[:80]}"})

    if c is not None:
        try:
            version_doc = _k8s_json(api_url, token, "/version", verify)
            m = re.search(r"v?(\d+\.\d+\.\d+)", str(version_doc.get("gitVersion") or ""))
            if not m:
                raise RuntimeError("/version 未返回 gitVersion")
            current = m.group(1)
        except Exception as e:
            incomplete.append(f"当前版本未知：{str(e)[:80]}")

        if target not in DEPRECATED:
            incomplete.append(f"目标 {target} 缺少废弃 API 兼容性规则，扫描未完成")
        else:
            try:
                discovery = _k8s_json(api_url, token, "/apis", verify)
                groups = discovery.get("groups")
                if not isinstance(groups, list):
                    raise RuntimeError("/apis 响应缺少 groups 列表")
                versions = set()
                for group in groups:
                    if not isinstance(group, dict) or not isinstance(group.get("versions"), list):
                        raise RuntimeError("/apis groups/versions 响应结构异常")
                    for version in group["versions"]:
                        if not isinstance(version, dict) or not version.get("groupVersion"):
                            raise RuntimeError("/apis groupVersion 响应结构异常")
                        versions.add(version["groupVersion"])
                for api, kind, path in DEPRECATED[target]:
                    if api not in versions:
                        continue
                    doc = _k8s_json(api_url, token, path, verify, params={"limit": 100})
                    items = doc.get("items")
                    if not isinstance(items, list):
                        raise RuntimeError(f"{path} 响应缺少 items 列表")
                    instances = [
                        f"{(it.get('metadata') or {}).get('namespace', '')}/"
                        f"{(it.get('metadata') or {}).get('name', '')}"
                        for it in items[:100]
                    ]
                    findings.append({
                        "apiVersion": api, "kind": kind, "instances": instances[:5],
                        "note": f"集群中存在 {kind}（{api}）——升级前需迁移"
                                + (f"，涉及 {len(instances)} 个资源" if instances else "（无实例）")})
            except Exception as e:
                incomplete.append(f"废弃 API 扫描失败：{str(e)[:80]}")
                findings.append({"note": f"废弃 API 扫描未完成: {str(e)[:80]}"})

        try:
            import yaml

            cm = _k8s_json(api_url, token,
                           "/api/v1/namespaces/kube-system/configmaps/kube-proxy", verify)
            raw = (cm.get("data") or {}).get("config.conf") or ""
            conf = yaml.safe_load(raw) or {}
            mode = str(conf.get("mode") or "").strip()
            if not mode:
                raise RuntimeError("config.conf 未声明 mode")
            kube_proxy["mode"] = mode
            if target in DEPRECATED and mode in ("iptables", "ipvs"):
                kube_proxy.update(
                    compatible=True,
                    note=f"从 kube-proxy ConfigMap 实际读取 mode={mode}，目标 {target} 支持该模式")
            else:
                if target not in DEPRECATED:
                    incomplete.append(f"目标 {target} 缺少 kube-proxy 兼容性规则")
                else:
                    incomplete.append(f"kube-proxy mode={mode} 对目标 {target} 的兼容性未确认")
                kube_proxy["note"] = f"实际读取 mode={mode}，但未确认其与目标 {target} 兼容"
        except Exception as e:
            incomplete.append(f"kube-proxy 模式/兼容性探测失败：{str(e)[:80]}")
            kube_proxy["note"] = f"kube-proxy 模式/兼容性未探测完成：{str(e)[:80]}"

    blocking = [f for f in findings if f.get("instances")]
    result = {
        "target": target, "current_version": current,
        "completed": not incomplete, "incomplete_reasons": incomplete,
        "passed": not incomplete and not blocking and kube_proxy["compatible"] is True,
        "findings": findings, "kube_proxy": kube_proxy,
        "valid_for_minutes": 30,
    }
    if c is not None:
        # 复用任务模型的 JSON 元数据留存预检证据；不新增表、不启动后台任务。
        now = datetime.now()
        db.add(K8sTask(
            task_type="upgrade_precheck", title=f"升级预检 → {target}",
            cluster_id=c.id, cluster_name=c.name,
            status="succeeded" if result["passed"] else "failed",
            meta_json=json.dumps(result, ensure_ascii=False), steps_json="[]",
            error="；".join(incomplete)[:2000], created_at=now, finished_at=now,
            submitted_by=user.username, submitted_by_id=user.id))
        db.commit()
    return result


# ================= 深度预检（部署向导第 4 步，逐项结论） =================

# 预检命令模板（代码生成，一次跑完逐项输出 KEY|PASS|detail——参考 k8smanager 14 项思路）
DEEP_PRECHECK_CMD = r"""
echo "SSH|PASS|连通正常 $(hostname)"
. /etc/os-release 2>/dev/null && echo "OS|PASS|${NAME:-unknown} ${VERSION_ID:-}" || echo "OS|WARN|无法识别系统版本"
echo "ARCH|PASS|$(uname -m)"
MEM=$(free -m | awk '/Mem:/{print $2}'); [ "$MEM" -ge 1900 ] && echo "MEM|PASS|${MEM}MB" || echo "MEM|WARN|仅 ${MEM}MB（建议 ≥2G）"
DISK=$(df -P /var 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}'); DISK=${DISK:-0}
[ "$DISK" -le 85 ] && echo "DISK|PASS|已用 ${DISK}%" || echo "DISK|FAIL|/var 已用 ${DISK}%（≥85%）"
CPU=$(nproc); [ "$CPU" -ge 2 ] && echo "CPU|PASS|${CPU} 核" || echo "CPU|WARN|仅 ${CPU} 核（建议 ≥2）"
SWAP=$(swapon --show --noheadings | wc -l); [ "$SWAP" -eq 0 ] && echo "SWAP|PASS|已关闭" || echo "SWAP|WARN|启用中（kubeadm 建议关闭）"
NTP=$(timedatectl show -p NTPSynchronized --value 2>/dev/null); [ "$NTP" = "yes" ] && echo "CLOCK|PASS|已同步" || echo "CLOCK|WARN|时间未同步/无法确认（升级与证书依赖时钟）"
for pt in 6443 2379 10250; do ss -lnt 2>/dev/null | grep -q ":$pt " && echo "PORT|WARN|端口 $pt 已被占用" ; done
[ -d /etc/kubernetes ] && echo "STALE|WARN|存在旧集群残留（/etc/kubernetes，将被清理）" || echo "STALE|PASS|无旧集群残留"
getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ] && echo "SELINUX|WARN|SELinux Enforcing（建议 Permissive）" || echo "SELINUX|PASS|SELinux 非 Enforcing"
systemctl is-active firewalld >/dev/null 2>&1 && echo "FIREWALL|WARN|firewalld 运行中（放行 6443/10250 或关闭）" || echo "FIREWALL|PASS|firewalld 未运行"
lsmod | grep -q br_netfilter && echo "KERNEL|PASS|br_netfilter 已加载" || echo "KERNEL|WARN|br_netfilter 未加载（init 后自动加载）"
command -v containerd >/dev/null && echo "RUNTIME|PASS|containerd $(containerd --version 2>/dev/null | grep -oP '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" || echo "RUNTIME|WARN|无 containerd（任务内自动安装）"
curl -sk --max-time 6 https://mirrors.aliyun.com >/dev/null 2>&1 && echo "NET|PASS|外网镜像源可达" || echo "NET|WARN|外网源不可达（安装可能失败）"
"""


class DeepPrecheckIn(BaseModel):
    machine_id: int = 0
    ip: str = Field(default="", max_length=64)
    ssh_user: str = Field(default="root", max_length=64)
    ssh_password: str = Field(default="", max_length=128)


@router.post("/precheck/deep")
def deep_precheck(body: DeepPrecheckIn, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator"))):
    """深度预检（部署前信任闸门）：14 项逐项结论，全过才建议提交。"""
    from app.collector.ssh import run_ssh

    ip, ssh_user, pwd = body.ip, body.ssh_user, body.ssh_password
    if body.machine_id:
        mc = _machine_creds(db, body.machine_id)
        ip, ssh_user, pwd = mc["ip"], mc["ssh_user"], mc["ssh_password"]
    if not ip:
        raise HTTPException(400, "目标机 IP 或机器库 machine_id 二选一")
    r = run_ssh(ip, ssh_user, DEEP_PRECHECK_CMD, timeout=90, password=pwd)
    items = []
    for ln in (r.get("lines") or []):
        parts = ln.split("|", 2)
        if len(parts) == 3 and parts[1] in ("PASS", "WARN", "FAIL"):
            items.append({"key": parts[0], "result": parts[1], "detail": parts[2]})
    failed = [i for i in items if i["result"] == "FAIL"]
    warned = [i for i in items if i["result"] == "WARN"]
    return {"ok": r["ok"] and not failed, "items": items,
            "summary": {"pass": len(items) - len(failed) - len(warned),
                        "warn": len(warned), "fail": len(failed)},
            "note": "FAIL 项须处理后重检；WARN 项可带风险继续（任务内自动适配项除外）"}


class WipeIn(BaseModel):
    cluster_id: int = 0
    confirm_name: str = Field(max_length=64)
    cluster_name: str = Field(default="", max_length=64)
    master_ssh: str = Field(default="", max_length=64)
    master_user: str = Field(default="root", max_length=64)


def _require_upgrade_precheck(db: Session, c: K8sCluster, target_version: str,
                              current_version: str) -> K8sTask:
    """只接受本集群、本目标 minor、当前版本一致且 30 分钟内通过的最近预检。"""
    target = ".".join(target_version.split(".")[:2])
    rows = (db.query(K8sTask)
            .filter(K8sTask.task_type == "upgrade_precheck", K8sTask.cluster_id == c.id)
            .order_by(K8sTask.id.desc()).limit(50).all())
    for row in rows:
        try:
            evidence = json.loads(row.meta_json or "{}")
        except (TypeError, ValueError):
            continue
        if evidence.get("target") != target:
            continue
        if row.created_at is None or datetime.now() - row.created_at > timedelta(minutes=30):
            raise HTTPException(400, "最近一次同目标预检已超过 30 分钟——请重新预检")
        if (row.status != "succeeded" or evidence.get("passed") is not True
                or evidence.get("completed") is not True):
            raise HTTPException(400, "最近一次同目标预检未完整通过——请处理后重新预检")
        if evidence.get("current_version") != current_version:
            raise HTTPException(
                400, f"集群当前版本 v{current_version} 与预检版本 "
                     f"v{evidence.get('current_version') or '未知'} 不一致——请重新预检")
        return row
    raise HTTPException(
        400, f"缺少集群「{c.name}」升级到 {target}.x 的有效通过预检——请先执行升级预检")


@router.post("/upgrade")
def start_upgrade(body: UpgradeIn, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin"))):
    """启动升级（同集群/目标/当前版本的有效预检通过 + 名称确认才放行）。"""
    if body.confirm_name != body.cluster_name:
        raise HTTPException(400, "集群名称不匹配——升级被拒绝（请手动输入集群名称确认）")
    if not re.fullmatch(r"\d+\.\d+\.\d+", body.target_version or ""):
        raise HTTPException(400, "目标版本格式非法（须 X.Y.Z）")
    from app.services.k8s_ops import run_upgrade
    from app.services.task_engine import submit

    meta = body.model_dump()
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    # 预检与执行必须绑定同一登记目标；请求体不得把执行切到另一台 SSH 机器。
    meta["master_ssh"], meta["master_user"] = host, suser
    meta["cluster_name"], meta["cluster_id"] = c.name, c.id

    # 当前版本必须真实取得；探测失败不再跳过安全门。
    from app.collector.ssh import run_ssh

    try:
        r = run_ssh(host, suser, "kubeadm version -o short 2>/dev/null", timeout=20)
    except Exception as e:
        raise HTTPException(400, f"当前版本探测失败，升级被拒绝：{str(e)[:80]}") from e
    cur = ""
    for ln in (r.get("lines") or []):
        m = re.search(r"v?(\d+\.\d+\.\d+)", ln)
        if m:
            cur = m.group(1)
            break
    if not r.get("ok") or not cur:
        raise HTTPException(400, "当前版本未知或 kubeadm 探测失败——升级被拒绝")
    _require_upgrade_precheck(db, c, body.target_version, cur)

    # kubeadm 真实语义：补丁级自由、跨 minor 只许 +1、不许跳级。
    cm, tm = int(cur.split(".")[1]), int(body.target_version.split(".")[1])
    if tm not in (cm, cm + 1):
        raise HTTPException(400,
                            f"逐级升级规则：当前 v{cur} 只能升到 1.{cm}.x（补丁）或 1.{cm + 1}.x（目标 {body.target_version} 跳级被拒绝）")

    task = submit(db, "upgrade", f"集群升级 → {body.target_version}",
                  meta, steps=[],
                  runner=lambda tid, log, set_steps: run_upgrade(tid, log, set_steps, meta),
                  submitted_by=user.username, submitted_by_id=user.id)
    return {"task_id": task.id}


@router.post("/cluster/wipe")
def wipe_cluster(body: WipeIn, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin"))):
    """清空集群（高危）：名称对不上拒绝；流程七步全记录。"""
    if body.confirm_name != body.cluster_name:
        raise HTTPException(400, "集群名称不匹配——清空被拒绝（高危操作需输入集群名称确认）")
    from app.services.k8s_ops import run_wipe
    from app.services.task_engine import submit

    meta = body.model_dump()
    if not meta.get("master_ssh"):
        meta["master_ssh"], meta["master_user"], c = _admin_ssh(db, body.cluster_name, body.cluster_id)
        _assert_writable(c)
    task = submit(db, "wipe", f"清空集群 {body.cluster_name}", meta,
                  steps=[], runner=lambda tid, log, set_steps: run_wipe(tid, log, set_steps, meta),
                  submitted_by=user.username, submitted_by_id=user.id)
    return {"task_id": task.id}


# ================= 资源管理 =================

class ResourceOpIn(BaseModel):
    cluster_id: int = 0
    confirm_name: str = Field(default="", max_length=255)
    yaml: str = Field(default="", max_length=200000)
    action: str = Field(default="", max_length=32)     # cordon/uncordon（cordon 接口用）
    cluster_name: str = Field(default="", max_length=64)


@router.get("/resources")
def list_resources(kind: str = Query("Node"), namespace: str = "",
                   status: str = Query("", description="按状态筛：如 Pending/Running/NotReady"),
                   cluster_id: int = 0, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin", "operator", "viewer"))):
    q = db.query(K8sResource).filter(K8sResource.kind == kind)
    if cluster_id:
        q = q.filter(K8sResource.cluster_id == cluster_id)
    if namespace:
        q = q.filter(K8sResource.namespace == namespace)
    if status:
        q = q.filter(K8sResource.status_json.like(f'%"status": "{status}"%'))
    rows = q.order_by(K8sResource.namespace, K8sResource.name).limit(500).all()
    from app.services.authz import ns_row_ok, ns_scope

    _scopes = {c.id: ns_scope(db, user, c) for c in db.query(K8sCluster).all()}
    rows = [r for r in rows if ns_row_ok(_scopes.get(r.cluster_id), r.namespace)]
    out = []
    for r in rows:
        st = json.loads(r.status_json or "{}")
        out.append({"id": r.id, "kind": r.kind, "namespace": r.namespace, "name": r.name,
                    "cluster_id": r.cluster_id,
                    "status": st.get("status", ""), "ready": st.get("ready"),
                    "restarts": st.get("restarts"), "node": st.get("node"),
                    "ip": st.get("pod_ip") or st.get("cluster_ip"),
                    "created": st.get("created", ""),
                    "is_active": r.is_active,
                    "observed_at": r.observed_at.isoformat(timespec="seconds")})
    return {"items": out, "total": len(out)}


@router.get("/resources/{kind}/{name}/yaml")
def resource_yaml(kind: str, name: str, namespace: str = Query(""),
                  cluster_id: int = Query(0), cluster_name: str = Query(""),
                  db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """yaml 查看（只读）——token 通道（K8s API），不依赖管理面 SSH。"""
    from app.collector.k8s import get_resource_yaml

    _safe(kind, name, namespace)
    _ns_guard(db, user, cluster_id, cluster_name, namespace, "view", for_op=False)
    api_url, token, verify, _c = _cluster_api(db, cluster_name, cluster_id)
    r = get_resource_yaml(api_url, token, kind, name, namespace, verify)
    if not r["ok"]:
        return {"ok": False, "yaml": "", "error": r.get("error", "读取失败")}
    return {"yaml": r["yaml"], "ok": True}


class YamlApplyIn(BaseModel):
    cluster_id: int = 0
    yaml: str = Field(max_length=200000)
    confirm_name: str = Field(default="", max_length=255)
    cluster_name: str = Field(default="", max_length=64)


@router.put("/resources/{kind}/{name}/yaml")
def resource_yaml_apply(kind: str, name: str, body: YamlApplyIn,
                        namespace: str = Query(""), db: Session = Depends(get_db),
                        user: User = Depends(require_roles("admin", "operator"))):
    """yaml 编辑保存（apply）：高危变更——confirm_name 须等于资源名。
    非 admin 需该命名空间 use 级授权（Namespace 授权委托）。"""
    from app.collector.ssh import run_ssh

    _safe(kind, name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name,
              "" if kind in ("Node", "Namespace") else namespace, "use")
    if body.confirm_name != name:
        raise HTTPException(400, "资源名不匹配——变更被拒绝")
    if not (body.yaml or "").strip():
        raise HTTPException(400, "yaml 不能为空")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    # yaml 经 heredoc 喂给 kubectl apply -f -（内容不进 shell 拼接面）
    script = "kubectl apply -f - <<'QSYAML'\n" + body.yaml + "\nQSYAML"
    r = run_ssh(host, suser, script, timeout=120)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:800]}


@router.post("/resources/{kind}/{name}/cordon")
def cordon_node(kind: str, name: str, body: ResourceOpIn,
                db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin"))):
    """禁止调度/恢复调度（node 专属；confirm_name 须等于节点名）。"""
    from app.collector.ssh import run_ssh

    _safe("Node", name)
    if body.confirm_name != name:
        raise HTTPException(400, "节点名不匹配——操作被拒绝")
    action = body.action or body.yaml or "cordon"   # 兼容旧字段
    if action not in ("cordon", "uncordon"):
        raise HTTPException(400, "动作仅支持 cordon/uncordon")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    r = run_ssh(host, suser, f"kubectl {action} {name}", timeout=60)   # cordon 参数就是节点名
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:500]}


@router.post("/resources/{kind}/{name}/evict")
def evict_pod(kind: str, name: str, body: ResourceOpIn,
              namespace: str = Query("default"),
              db: Session = Depends(get_db),
              user: User = Depends(require_roles("admin", "operator"))):
    """驱逐/删除（confirm_name 须等于资源名）。
    非 admin 需该命名空间 use 级授权（Namespace 授权委托）。"""
    from app.collector.ssh import run_ssh

    _safe(kind, name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name,
              "" if kind in ("Node", "Namespace") else namespace, "use")
    if body.confirm_name != name:
        raise HTTPException(400, "资源名不匹配——高危操作被拒绝")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    ns_arg = f"-n {namespace}" if kind not in ("Node", "Namespace") else ""
    r = run_ssh(host, suser,
                f"kubectl delete {kind} {name} {ns_arg} --grace-period=30", timeout=120)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:500]}


# ================= 统计 / 事件 / 总览 =================

@router.get("/stats")
def stats(cluster_id: int = 0, db: Session = Depends(get_db),
          user: User = Depends(require_roles("admin", "operator", "viewer"))):
    from sqlalchemy import func

    from app.services.authz import ns_row_ok, ns_scope

    _scopes = {c.id: ns_scope(db, user, c) for c in db.query(K8sCluster).all()}
    restricted = any(sc is not None for sc in _scopes.values())
    q = db.query(K8sResource.kind, func.count(K8sResource.id))
    if cluster_id:
        q = q.filter(K8sResource.cluster_id == cluster_id)
    if restricted:
        # 按 (cluster, ns, kind) 计数，再按 Namespace 授权口径汇总
        kinds = {}
        rows = (q.with_entities(K8sResource.cluster_id, K8sResource.namespace,
                                K8sResource.kind, func.count(K8sResource.id))
                .filter(K8sResource.is_active == True)  # noqa: E712
                .group_by(K8sResource.cluster_id, K8sResource.namespace, K8sResource.kind).all())
        for cid, ns, kind, n in rows:
            if ns_row_ok(_scopes.get(cid), ns):
                kinds[kind] = kinds.get(kind, 0) + n
    else:
        kinds = dict(q.filter(K8sResource.is_active == True).group_by(K8sResource.kind).all())  # noqa: E712
    wq = db.query(func.count(K8sFailure.id)).filter(K8sFailure.status == "pending")
    if cluster_id:
        # 待处理故障统计必须跟随集群筛选，与 Pod/Node 等统计保持同口径。
        wq = wq.filter(K8sFailure.cluster_id == cluster_id)
    if restricted:
        warnings = 0
        fq = (db.query(K8sFailure.cluster_id, K8sFailure.namespace,
                       func.count(K8sFailure.id))
              .filter(K8sFailure.status == "pending"))
        if cluster_id:
            fq = fq.filter(K8sFailure.cluster_id == cluster_id)
        for cid, ns, n in fq.group_by(K8sFailure.cluster_id, K8sFailure.namespace).all():
            if ns_row_ok(_scopes.get(cid), ns):
                warnings += n
    else:
        warnings = wq.scalar() or 0
    return {"ns": kinds.get("Namespace", 0), "node": kinds.get("Node", 0),
            "pod": kinds.get("Pod", 0), "workload": kinds.get("Deployment", 0),
            "service": kinds.get("Service", 0), "ingress": kinds.get("Ingress", 0),
            "storage": kinds.get("PersistentVolumeClaim", 0), "warning": warnings}


@router.get("/events")
def events(cluster_id: int = 0, namespace: str = "", cluster_name: str = "",
           limit: int = Query(100, ge=1, le=500),
           db: Session = Depends(get_db),
           user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """事件中心（分集群/类型/原因/ns/对象/消息/次数/最后时间）——token 通道。"""
    import time as _t

    from app.collector.k8s import list_events

    from app.services.authz import ns_row_ok, ns_scope, resolve_cluster

    # Namespace 授权过滤：缓存存全量，按用户口径出
    _ns_cluster = resolve_cluster(db, cluster_id, cluster_name)

    def _filter(items):
        sc = ns_scope(db, user, _ns_cluster) if _ns_cluster is not None else None
        return [it for it in items if ns_row_ok(sc, it.get("namespace", ""))]

    # 进程内 TTL 缓存 60s（事件实时查询是 SSH/API 往返；刷新档位 10s+ 缓存=友好折中）
    cache_key = f"{cluster_id}|{cluster_name}|{namespace}|{limit}"
    if namespace:
        _ns_guard(db, user, cluster_id, cluster_name, namespace, "view", for_op=False)
    cached = _EVENTS_CACHE.get(cache_key)
    if cached and _t.time() - cached[0] < 60:
        items = _filter(cached[1])
        return {"items": items, "total": len(items), "cached": True}
    api_url, token, verify, _c = _cluster_api(db, cluster_name, cluster_id)
    try:
        items = list_events(api_url, token, namespace if _NAME_RE.match(namespace) else "",
                            verify, limit=limit)
        _EVENTS_CACHE[cache_key] = (_t.time(), items)
        items = _filter(items)
        return {"items": items, "total": len(items)}
    except Exception as e:
        return {"items": [], "total": 0, "note": f"事件获取失败: {str(e)[:80]}"}


# ================= 资源操作扩展（drain/滚动重启/CronJob/批量） =================

class NodeDrainIn(BaseModel):
    confirm_name: str = Field(max_length=255)
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)


@router.post("/resources/Node/{name}/drain")
def drain_node(name: str, body: NodeDrainIn, db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin"))):
    """驱逐节点（drain，高危）：排空存量 Pod 后标记不可调度——confirm_name 须等于节点名。"""
    from app.collector.ssh import run_ssh

    _safe("Node", name)
    if body.confirm_name != name:
        raise HTTPException(400, "节点名不匹配——驱逐被拒绝")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    r = run_ssh(host, suser,
                f"kubectl drain {name} --ignore-daemonsets --delete-emptydir-data --grace-period=60",
                timeout=600)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:600]}


class RestartIn(BaseModel):
    confirm_name: str = Field(default="", max_length=255)
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)


@router.post("/resources/{kind}/{name}/restart")
def restart_workload(kind: str, name: str, body: RestartIn,
                     namespace: str = Query("default"),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_roles("admin", "operator"))):
    """滚动重启（工作负载）：kubectl rollout restart——confirm_name 须等于资源名。
    非 admin 需该命名空间 use 级授权（Namespace 授权委托）。"""
    from app.collector.ssh import run_ssh

    if kind not in ("Deployment", "StatefulSet", "DaemonSet"):
        raise HTTPException(400, f"{kind} 不支持滚动重启")
    _safe(kind, name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name, namespace, "use")
    if body.confirm_name != name:
        raise HTTPException(400, "资源名不匹配——重启被拒绝")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    r = run_ssh(host, suser,
                f"kubectl rollout restart {kind.lower()}/{name} -n {namespace}", timeout=120)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:400]}


class CronJobOpIn(BaseModel):
    confirm_name: str = Field(max_length=255)
    action: str = Field(default="trigger", max_length=16)   # trigger / suspend / resume
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)


@router.post("/resources/CronJob/{name}/cronop")
def cronjob_op(name: str, body: CronJobOpIn, namespace: str = Query("default"),
               db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin", "operator"))):
    """CronJob 手动触发 / 暂停 / 恢复——confirm_name 须等于资源名。
    非 admin 需该命名空间 use 级授权（Namespace 授权委托）。"""
    from app.collector.ssh import run_ssh

    _safe("CronJob", name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name, namespace, "use")
    if body.confirm_name != name:
        raise HTTPException(400, "资源名不匹配——操作被拒绝")
    if body.action not in ("trigger", "suspend", "resume"):
        raise HTTPException(400, "动作仅支持 trigger/suspend/resume")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    if body.action == "trigger":
        r = run_ssh(host, suser,
                    f"kubectl create job --from=cronjob/{name} {name}-manual-$(date +%H%M%S) -n {namespace}",
                    timeout=120)
    else:
        flag = "true" if body.action == "suspend" else "false"
        r = run_ssh(host, suser,
                    f"kubectl patch cronjob {name} -n {namespace} -p '{{\"spec\":{{\"suspend\":{flag}}}}}'",
                    timeout=60)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:400]}


class BatchOpIn(BaseModel):
    items: list = Field(default_factory=list)   # [{kind, name, namespace, cluster_id}]
    action: str = Field(max_length=16)          # restart / delete
    confirm_count: int = 0                      # 必须等于 items 数（防误选误删）
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)


# 批量删除沿用单行删除菜单的禁删清单：
# Node/Namespace 一删即伤筋动骨，单行"更多"菜单已隐藏删除项，批量同样禁止。
BATCH_DELETE_FORBIDDEN = {"Node", "Namespace"}


@router.post("/resources/batch")
def batch_op(body: BatchOpIn, db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin", "operator"))):
    """批量重启/删除：confirm_count 须等于条目数，逐条执行结果汇总。
    items 带 cluster_id 逐条解析目标集群，避免整批共用一个 SSH 或空参回落第一个集群
    造成跨集群误操作；批量删除沿用单行禁删清单。"""
    from app.collector.ssh import run_ssh

    if body.action not in ("restart", "delete"):
        raise HTTPException(400, "批量仅支持 restart/delete")
    if not body.items or len(body.items) > 50:
        raise HTTPException(400, "批量条目 1~50 条")
    if body.confirm_count != len(body.items):
        raise HTTPException(400, f"确认数量不符（需填 {len(body.items)}）——批量操作被拒绝")
    if body.action == "delete":
        banned = [str(it.get("name", "")) for it in body.items
                  if str(it.get("kind", "")) in BATCH_DELETE_FORBIDDEN]
        if banned:
            raise HTTPException(400,
                                f"Node/Namespace 不可批量删除（与单行删除同一禁删清单）：{'、'.join(banned[:5])}"
                                "——请去掉后重试")
    # 每条目标独立解析集群（item.cluster_id 优先，缺省回落请求级集群参数）；
    # 同集群只解析一次 SSH
    ssh_cache: dict = {}
    results = []
    for it in body.items:
        kind = str(it.get("kind", ""))
        name = str(it.get("name", ""))
        ns = str(it.get("namespace", "") or "default")
        it_cid = 0
        try:
            it_cid = int(it.get("cluster_id") or 0)
        except (TypeError, ValueError):
            it_cid = 0
        try:
            _safe(kind, name, ns)
            # Namespace 授权：逐条按目标 ns 查 use 级授权；Node/Namespace 视为集群级
            _ns_guard(db, user, it_cid or body.cluster_id, body.cluster_name,
                      "" if kind in ("Node", "Namespace") else ns, "use")
        except HTTPException as e:
            results.append({"name": name, "ok": False, "detail": e.detail})
            continue
        if body.action == "restart":
            if kind not in ("Deployment", "StatefulSet", "DaemonSet"):
                results.append({"name": name, "ok": False, "detail": f"{kind} 不支持重启"})
                continue
            cmd = f"kubectl rollout restart {kind.lower()}/{name} -n {ns}"
        else:
            ns_arg = f"-n {ns}" if kind not in ("Node", "Namespace") else ""
            cmd = f"kubectl delete {kind} {name} {ns_arg} --grace-period=30"
        cache_key = (it_cid, body.cluster_name, body.cluster_id)
        try:
            if cache_key not in ssh_cache:
                host, suser, c = _admin_ssh(db, body.cluster_name if not it_cid else "",
                                            it_cid or body.cluster_id)
                _assert_writable(c)
                ssh_cache[cache_key] = (host, suser)
            host, suser = ssh_cache[cache_key]
        except HTTPException as e:
            results.append({"name": name, "ok": False, "detail": e.detail})
            continue
        r = run_ssh(host, suser, cmd, timeout=120)
        results.append({"name": name, "ok": r["ok"],
                        "detail": "\n".join(r.get("lines") or [])[:150]})
    ok_n = sum(1 for x in results if x["ok"])
    return {"ok": ok_n == len(results), "done": ok_n, "failed": len(results) - ok_n,
            "results": results}


# ================= 审计 / 镜像 / Helm / Secret 揭示 =================

@router.get("/audits")
def k8s_audits(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """K8s/机器 操作审计（终端会话/资源变更/批量结果——复用平台审计流）。
    同套 humanize 中文摘要（action_label/summary），与系统设置-审计日志同体验；
    全量审计（含非 K8s 动作）在 GET /audit/logs。"""
    from app.models.audit import AuditLog
    from app.services.audit import humanize

    rows = (db.query(AuditLog)
            .filter(AuditLog.action.like("k8s.%") | AuditLog.action.like("machine.%"))
            .order_by(AuditLog.id.desc()).limit(limit).all())
    return {"items": [{
        "id": r.id, "action": r.action, "username": r.username,
        "resource_type": r.resource_type, "resource_id": r.resource_id,
        "detail": json.loads(r.detail) if isinstance(r.detail, str) and r.detail.startswith("{") else (r.detail or ""),
        "client_ip": r.client_ip,
        "ts": (r.created_at.isoformat(timespec="seconds") if r.created_at else ""),
        **humanize(r.action, r.resource_type, r.resource_id, r.detail or "", r.result or "ok"),
    } for r in rows], "total": len(rows)}


@router.get("/images")
def list_images_cached(cluster_name: str = Query(""), cluster_id: int = Query(0),
                db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """镜像清单（5min 缓存）。"""
    return _cached(f"images:{cluster_id}:{cluster_name}", 300,
                   lambda: _list_images_impl(cluster_name, cluster_id, db))


def _list_images_impl(cluster_name: str, cluster_id: int, db: Session):
    """镜像管理（节点镜像缓存清单）——控制面采集各节点 status.images。
    不带集群=聚合所有可管理集群，不能只取第一个集群（它可能是生产只读集群而无管理面数据）。"""
    from app.collector.ssh import run_ssh

    from app.services.k8s_ops import resolve_master_ssh

    if cluster_id or cluster_name:
        targets = [(host, suser, c) for host, suser, c in
                   [resolve_master_ssh(db, cluster_name, cluster_id, strict=True)] if host]
    else:
        targets = []
        for c in db.query(K8sCluster).all():
            h, u, cc = resolve_master_ssh(db, "", c.id, strict=True)
            if h:
                targets.append((h, u, cc))
    items = []
    skipped = []
    # 多集群并行采集：总耗时取决于最慢节点；单台 SSH 超时限制为 20s，
    # 避免坏集群拖住全部结果或拖穿网关超时。
    from concurrent.futures import ThreadPoolExecutor

    def _pull(t):
        host, suser, c = t
        return (c, run_ssh(host, suser,
                           "kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}"
                           "{range .status.images[*]}{.names[0]}{\" \"}{.sizeBytes}{\"\\n\"}{end}{end}'",
                           timeout=20))

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        pulled = list(pool.map(_pull, targets))
    for c, r in pulled:
        node = ""
        for ln in (r.get("lines") or []):
            ln = ln.strip()
            if not ln:
                continue
            if " " not in ln and "/" not in ln:
                node = ln
                continue
            parts = ln.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isdigit():
                items.append({"node": node, "image": parts[0],
                              "size_mb": round(int(parts[1]) / 1048576, 1),
                              "cluster": c.name if c else ""})
    if not targets:
        return {"items": [], "total": 0,
                "note": "无可用管理面 SSH 的集群（只读集群看不到镜像缓存——集群管理里登记 ssh_host）"}
    return {"items": items[:200], "total": len(items)}


@router.get("/helm")
def list_helm_cached(cluster_name: str = Query(""), cluster_id: int = Query(0),
              db: Session = Depends(get_db),
              user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """Helm 清单（5min 缓存）。"""
    from app.services.authz import ns_row_ok, ns_scope, resolve_cluster

    out = _cached(f"helm:{cluster_id}:{cluster_name}", 300,
                  lambda: _list_helm_impl(cluster_name, cluster_id, db))
    # Namespace 授权过滤：缓存存全量，按用户口径出
    c = resolve_cluster(db, cluster_id, cluster_name)
    sc = ns_scope(db, user, c) if c is not None else None
    items = [it for it in (out.get("items") or []) if ns_row_ok(sc, it.get("namespace", ""))]
    return {**out, "items": items, "total": len(items)}


def _list_helm_impl(cluster_name: str, cluster_id: int, db: Session):
    """Helm 管理（release 清单）——控制面 helm list；未装 helm 优雅降级提示。"""
    from app.collector.ssh import run_ssh

    from app.services.k8s_ops import resolve_master_ssh

    if cluster_id or cluster_name:
        targets = [(h, u) for h, u, _ in
                   [resolve_master_ssh(db, cluster_name, cluster_id, strict=True)] if h]
    else:
        targets = [(h, u) for c in db.query(K8sCluster).all()
                   for h, u, _ in [resolve_master_ssh(db, "", c.id, strict=True)] if h]
    if not targets:
        return {"items": [], "total": 0,
                "note": "无可用管理面 SSH 的集群（集群管理里登记 ssh_host 后可见）"}
    host, suser = targets[0]
    r = run_ssh(host, suser,
                "command -v helm >/dev/null && helm list -A -o json 2>/dev/null || echo NO-HELM",
                timeout=20)
    lines = r.get("lines") or []
    if any("NO-HELM" in ln for ln in lines):
        return {"items": [], "total": 0, "note": "控制面未安装 helm（装后自动出清单）"}
    try:
        data = json.loads("\n".join(lines))
        items = [{"name": x.get("name", ""), "namespace": x.get("namespace", ""),
                  "chart": x.get("chart", ""), "app_version": x.get("app_version", ""),
                  "status": x.get("status", ""), "updated": (x.get("updated") or "")[:19]}
                 for x in data]
        return {"items": items, "total": len(items)}
    except Exception:
        return {"items": [], "total": 0, "note": "helm 输出解析失败"}


@router.get("/secrets")
def list_secrets(cluster_name: str = Query(""), cluster_id: int = Query(0),
                 namespace: str = Query(""), db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """Secret 清单（管理面 SSH 通道）——SA view 角色不含 secrets（K8s 设计），
    清单与揭示统一走管理通道，可用性一致。5min SWR 缓存避免 SSH 慢查询阻塞请求。"""
    from app.services.authz import ns_row_ok, ns_scope, resolve_cluster

    out = _cached(f"secrets:{cluster_id}:{cluster_name}:{namespace}", 300,
                  lambda: _list_secrets_impl(cluster_name, cluster_id, namespace, db))
    # Namespace 授权过滤：缓存存全量，按用户口径出
    c = resolve_cluster(db, cluster_id, cluster_name)
    sc = ns_scope(db, user, c) if c is not None else None
    items = [it for it in (out.get("items") or []) if ns_row_ok(sc, it.get("namespace", ""))]
    return {**out, "items": items, "total": len(items)}


def _list_secrets_impl(cluster_name: str, cluster_id: int, namespace: str, db: Session):
    from app.collector.ssh import run_ssh

    try:
        host, suser, _c = _admin_ssh(db, cluster_name, cluster_id)
    except HTTPException as e:
        return {"items": [], "total": 0, "note": e.detail}
    ns_arg = f"-n {namespace}" if namespace and _NAME_RE.match(namespace) else "-A"
    r = run_ssh(host, suser,
                f"kubectl get secrets {ns_arg} --no-headers -o custom-columns="
                "NS:.metadata.namespace,NAME:.metadata.name,TYPE:.type,AGE:.metadata.creationTimestamp",
                timeout=20)
    items = []
    for ln in (r.get("lines") or []):
        parts = ln.split(None, 3)
        if len(parts) >= 3:
            items.append({"kind": "Secret", "namespace": parts[0], "name": parts[1],
                          "status": parts[2], "is_active": True, "cluster_id": cluster_id})
    return {"items": items, "total": len(items)}


class HelmOpIn(BaseModel):
    confirm_name: str = Field(max_length=128)
    chart_ref: str = Field(default="", max_length=300)   # 升级用：repo/chart 或本地 chart 路径
    cluster_name: str = Field(default="", max_length=64)
    cluster_id: int = 0


@router.post("/helm/{name}/upgrade")
def helm_upgrade(name: str, body: HelmOpIn, namespace: str = Query("default"),
                 db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator"))):
    """Helm 升级（confirm_name 须等于 release 名；chart_ref 过白名单）。
    非 admin 需该命名空间 use 级授权。"""
    from app.collector.ssh import run_ssh

    if not body.cluster_id and not body.cluster_name:
        raise HTTPException(400, "Helm 升级必须指定目标集群——请先选择具体集群")
    _safe("Deployment", name, namespace)   # release 名同资源名白名单
    _ns_guard(db, user, body.cluster_id, body.cluster_name, namespace, "use")
    if body.confirm_name != name:
        raise HTTPException(400, "Release 名不匹配——升级被拒绝")
    if not body.chart_ref or not re.fullmatch(r"[a-zA-Z0-9._/@-]{1,280}", body.chart_ref):
        raise HTTPException(400, "chart 引用非法")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    r = run_ssh(host, suser,
                f"helm upgrade {name} {body.chart_ref} -n {namespace} 2>&1 | tail -8", timeout=300)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:600]}


@router.post("/helm/{name}/uninstall")
def helm_uninstall(name: str, body: HelmOpIn, namespace: str = Query("default"),
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin", "operator"))):
    """Helm 卸载（高危，confirm_name 须等于 release 名）。
    非 admin 需该命名空间 admin 级授权。"""
    from app.collector.ssh import run_ssh

    _safe("Deployment", name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name, namespace, "admin")
    if body.confirm_name != name:
        raise HTTPException(400, "Release 名不匹配——卸载被拒绝")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    _assert_writable(c)
    r = run_ssh(host, suser,
                f"helm uninstall {name} -n {namespace} 2>&1 | tail -5", timeout=300)
    return {"ok": r["ok"], "detail": "\n".join(r.get("lines") or [])[:400]}


class SecretRevealIn(BaseModel):
    confirm_name: str = Field(max_length=255)
    cluster_id: int = 0
    cluster_name: str = Field(default="", max_length=64)


@router.post("/resources/Secret/{name}/reveal")
def reveal_secret(name: str, body: SecretRevealIn, namespace: str = Query("default"),
                  request: Request = None, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator"))):
    """Secret 明文揭示（高危）：confirm_name 须等于 Secret 名；揭示即审计。
    非 admin 需该命名空间 admin 级授权。"""
    from app.collector.ssh import run_ssh
    from app.services import audit as audit_svc

    _safe("Secret", name, namespace)
    _ns_guard(db, user, body.cluster_id, body.cluster_name, namespace, "admin")
    if body.confirm_name != name:
        raise HTTPException(400, "Secret 名不匹配——揭示被拒绝")
    host, suser, c = _admin_ssh(db, body.cluster_name, body.cluster_id)
    r = run_ssh(host, suser,
                f"kubectl get secret {name} -n {namespace} -o jsonpath='{{.data}}'", timeout=60)
    audit_svc.record(
        db, action="k8s.secret.reveal", user_id=user.id, username=user.username,
        resource_type="secret", resource_id=f"{namespace}/{name}",
        detail={"ok": r["ok"]},
        client_ip=get_client_ip(request))
    db.commit()
    return {"ok": r["ok"], "data_raw": "\n".join(r.get("lines") or [])[:4000],
            "note": "值为 base64 编码；本次揭示已写审计"}


class EventDiagIn(BaseModel):
    message: str = Field(default="", max_length=500)
    namespace: str = Field(default="", max_length=128)
    object_name: str = Field(default="", max_length=255)


@router.post("/events/diagnose")
def event_diagnose(body: EventDiagIn, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin", "operator"))):
    """事件接入 AI 故障诊断（跳诊断页预填问题）。"""
    q = f"分析 K8s 事件：{body.namespace}/{body.object_name}——{body.message[:200]}。结合集群状态给根因与处置"
    return {"question": q, "goto": "/diagnose"}


@router.get("/overview")
def overview(cluster_id: int = Query(0), db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """集群总览（随所选集群过滤：规模/任务/计数）。"""
    from sqlalchemy import func

    cq = db.query(K8sCluster)
    if cluster_id:
        cq = cq.filter(K8sCluster.id == cluster_id)
    clusters = cq.all()
    from app.services.task_engine import task_out

    tqs = db.query(K8sTask)
    rq = db.query(K8sResource)
    if cluster_id:
        tqs = tqs.filter(K8sTask.cluster_name == (clusters[0].name if clusters else ""))
        rq = rq.filter(K8sResource.cluster_id == cluster_id)
    recent = tqs.order_by(K8sTask.id.desc()).limit(10).all()
    from app.services.authz import ns_row_ok, ns_scope

    _scopes = {c.id: ns_scope(db, user, c) for c in db.query(K8sCluster).all()}
    if any(sc is not None for sc in _scopes.values()):
        # 按 (cluster, ns, kind) 计数，再按 Namespace 授权口径汇总
        kinds = {}
        for cid, ns, kind, n in (rq.with_entities(K8sResource.cluster_id, K8sResource.namespace,
                                                  K8sResource.kind, func.count(K8sResource.id))
                                 .filter(K8sResource.is_active == True)  # noqa: E712
                                 .group_by(K8sResource.cluster_id, K8sResource.namespace,
                                           K8sResource.kind).all()):
            if ns_row_ok(_scopes.get(cid), ns):
                kinds[kind] = kinds.get(kind, 0) + n
    else:
        kinds = dict(rq.with_entities(K8sResource.kind, func.count(K8sResource.id))
                     .filter(K8sResource.is_active == True).group_by(K8sResource.kind).all())  # noqa: E712
    return {
        "cluster_total": len(clusters),
        "cluster_ok": sum(1 for c in clusters if c.status == "ok"),
        "nodes": kinds.get("Node", 0), "namespaces": kinds.get("Namespace", 0),
        "pods": kinds.get("Pod", 0), "workloads": kinds.get("Deployment", 0),
        "recent_tasks": [task_out(t) for t in recent],
        "cert_monitor": {"note": "证书到期监控接现有到期总账（ssl_cert）"},
    }


@router.get("/charts")
def charts(cluster_id: int = Query(0), cluster_name: str = Query(""),
           db: Session = Depends(get_db),
           user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """总览图表（随所选集群过滤）：趋势/规模/状态分布/负载类型/K8s 证书到期。
    证书只统计 K8s 集群内部证书（kubeadm pki 组件证书），不掺资产总账的 SSL/域名。
    5min SWR 缓存避免 SSH 证书采集超过网关超时并触发 504。"""
    return _cached(f"charts:{cluster_id}:{cluster_name}", 300,
                   lambda: _charts_impl(cluster_id, cluster_name, db))


def _charts_impl(cluster_id: int, cluster_name: str, db: Session):
    from datetime import timedelta

    from sqlalchemy import func

    from app.collector.ssh import run_ssh

    q_res = db.query(K8sResource)
    q_task = db.query(K8sTask)
    cname_filter = ""
    if cluster_id:
        q_res = q_res.filter(K8sResource.cluster_id == cluster_id)
        c = db.get(K8sCluster, cluster_id)
        cname_filter = c.name if c else ""
    if cname_filter:
        q_task = q_task.filter(K8sTask.cluster_name == cname_filter)

    # 1) 运维操作趋势（近 7 天）
    today = datetime.now().date()
    trend = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        rows = (q_task.with_entities(K8sTask.status, func.count(K8sTask.id))
                .filter(func.date(K8sTask.created_at) == d).group_by(K8sTask.status).all())
        total = sum(n for _, n in rows)
        failed = sum(n for st, n in rows if st == "failed")
        trend.append({"date": d.strftime("%m-%d"), "total": total, "failed": failed})

    # 2) 资源规模
    scale = [{"kind": k, "count": n} for k, n in
             q_res.with_entities(K8sResource.kind, func.count(K8sResource.id))
             .filter(K8sResource.is_active == True).group_by(K8sResource.kind).all()]  # noqa: E712

    # 3) 集群运行状态（单集群时只显示该集群）
    cq = db.query(K8sCluster)
    if cluster_id:
        cq = cq.filter(K8sCluster.id == cluster_id)
    cluster_status = [{"status": (st or "unknown"), "count": n} for st, n in
                      cq.with_entities(K8sCluster.status, func.count(K8sCluster.id))
                      .group_by(K8sCluster.status).all()]

    # 4) Pod 状态分布
    pod_rows = q_res.with_entities(K8sResource.status_json).filter(
        K8sResource.kind == "Pod", K8sResource.is_active == True).all()  # noqa: E712
    dist = {}
    for (sj,) in pod_rows:
        try:
            st = (json.loads(sj or "{}") or {}).get("status") or "Unknown"
        except Exception:
            st = "Unknown"
        dist[st] = dist.get(st, 0) + 1
    pod_status = [{"status": k, "count": v} for k, v in sorted(dist.items(), key=lambda x: -x[1])]

    # 5) 工作负载类型分布
    workload_types = [{"kind": k, "count": n} for k, n in
                      q_res.with_entities(K8sResource.kind, func.count(K8sResource.id))
                      .filter(K8sResource.kind.in_(("Deployment", "StatefulSet", "DaemonSet",
                                                    "CronJob", "Job", "ReplicaSet")),
                              K8sResource.is_active == True)  # noqa: E712
                      .group_by(K8sResource.kind).all()]

    # 6) K8s 集群证书到期（kubeadm pki 组件证书；管理面 SSH 采集，只统计 K8s 内部证书）
    from app.services.k8s_ops import resolve_master_ssh

    cert_soon = []
    cl_q = db.query(K8sCluster)
    if cluster_id:
        cl_q = cl_q.filter(K8sCluster.id == cluster_id)
    cert_targets = []
    for c in cl_q.all():
        host, suser, _ = resolve_master_ssh(db, "", c.id, strict=True)
        if host:
            cert_targets.append((host, suser, c))
    # 证书采集多集群并行（同 images：串行冷路径≈30s 同样会拖穿网关）
    from concurrent.futures import ThreadPoolExecutor

    def _pull_cert(t):
        host, suser, c = t
        return (c, run_ssh(host, suser,
                           "for f in /etc/kubernetes/pki/*.crt /etc/kubernetes/pki/etcd/*.crt; do "
                           "[ -f \"$f\" ] || continue; "
                           "echo \"$f|$(openssl x509 -enddate -noout -in \"$f\" 2>/dev/null | cut -d= -f2)\"; done",
                           timeout=20))

    with ThreadPoolExecutor(max_workers=max(1, len(cert_targets))) as pool:
        cert_pulled = list(pool.map(_pull_cert, cert_targets))
    for c, r in cert_pulled:
        for ln in (r.get("lines") or []):
            if "|" not in ln:
                continue
            fp, end = ln.strip().split("|", 1)
            try:
                from datetime import timezone
                exp = datetime.strptime(end.strip(), "%b %d %H:%M:%S %Y %Z")
                days = (exp - datetime.now()).days
            except Exception:
                continue
            cert_soon.append({
                "name": fp.replace("/etc/kubernetes/pki/", ""),
                "product": f"集群 {c.name}", "days_left": days,
                "expire_time": exp.strftime("%Y-%m-%d"),
            })
    cert_soon.sort(key=lambda x: x["days_left"])
    cert_soon = cert_soon[:10]

    scope = "跨集群"
    if cluster_id and cname_filter:
        scope = f"集群 {cname_filter}"
    return {"trend": trend, "scale": scale, "cluster_status": cluster_status,
            "pod_status": pod_status, "workload_types": workload_types, "cert_soon": cert_soon,
            "cert_note": "K8s 集群内部证书（kubeadm pki）", "scope": scope}


class CreateClusterIn(BaseModel):
    machine_id: int = 0   # 从机器库选机（凭证托管）
    ip: str = Field(max_length=64)
    ssh_user: str = Field(default="root", max_length=64)
    ssh_password: str = Field(default="", max_length=128)
    k8s_version: str = Field(default="1.29.4", max_length=32)
    pod_cidr: str = Field(default="10.244.0.0/16", max_length=64)
    svc_cidr: str = Field(default="10.96.0.0/12", max_length=64)
    cluster_name: str = Field(default="test-cluster", max_length=64)


@router.post("/clusters/create")
def create_cluster(body: CreateClusterIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    """创建新集群（kubeadm init）——任务中心任务类型之一；成功后自动登记集群。"""
    from app.services.k8s_ops import run_create_cluster
    from app.services.task_engine import submit

    meta = body.model_dump()
    if body.machine_id:
        mc = _machine_creds(db, body.machine_id)
        meta.update(ip=mc["ip"], ssh_user=mc["ssh_user"], ssh_password=mc["ssh_password"])
    if not _NAME_RE.match(meta.get("ip", "")):
        raise HTTPException(400, "目标机 IP/主机名非法")
    if not re.fullmatch(r"\d+\.\d+\.\d+", meta.get("k8s_version", "") or ""):
        raise HTTPException(400, "k8s 版本格式非法（须 X.Y.Z）")

    def runner(tid, log, set_steps):
        ok, err = run_create_cluster(tid, log, set_steps, meta)
        if ok:
            # 成功即登记（管理面 SSH=新集群控制面；新集群由本流程创建→可管理）
            try:
                from app.core.db import SessionLocal

                sdb = SessionLocal()
                c = sdb.query(K8sCluster).filter(K8sCluster.name == body.cluster_name).first()
                if c is None:
                    c = K8sCluster(name=body.cluster_name, api_url=f"https://{body.ip}:6443",
                                   ssh_host=body.ip, ssh_user=body.ssh_user or "root",
                                   read_only=False, status="unknown",
                                   status_note="由创建集群任务自动登记（token 待补）")
                    sdb.add(c)
                else:
                    c.ssh_host = body.ip
                    c.read_only = False
                sdb.commit()
                sdb.close()
                log(f"集群「{body.cluster_name}」已登记（管理面 {body.ip}）")
            except Exception as e:
                log(f"集群登记跳过: {str(e)[:80]}")
        return ok, err

    task = submit(db, "create_cluster", f"创建集群 {body.cluster_name}（{body.ip}）", meta,
                  steps=[], runner=runner, submitted_by=user.username, submitted_by_id=user.id)
    return {"task_id": task.id, "status": task.status}
