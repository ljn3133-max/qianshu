"""机器库 API：目标机资产台账——部署/加节点/备份等管理任务从库选机。

安全：SSH 密码 AES 加密托管（不落明文）；连通/水位采集为只读命令；
管理任务引用机器库凭证，表单不再现填密码。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.machine import K8sMachine
from app.models.user import User

logger = logging.getLogger("qianshu.machines_api")
router = APIRouter(prefix="/machines", tags=["machines"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
_ROLES = ("未分配", "master", "etcd", "node", "loadbalancer")

# 水位采集（只读；vmstat 1 2 取第二行 cpu idle，free/df 取用量）
USAGE_CMD = ("vmstat 1 2 2>/dev/null | tail -1 | awk '{print int(100-$15)}'; "
             "free | awk '/Mem:/{print int($3*100/$2)}'; "
             "df / | awk 'NR==2{gsub(/%/,\"\",$5); print $5}'")


def _machine_out(m: K8sMachine, linkage: dict | None = None) -> dict:
    return {
        "id": m.id, "host_name": m.host_name, "ip": m.ip, "ssh_port": m.ssh_port,
        "ssh_user": m.ssh_user, "has_password": bool(m.ssh_password_cipher),
        "role": m.role, "cluster_id": m.cluster_id, "cluster_name": m.cluster_name,
        "status": m.status, "status_note": m.status_note,
        "cpu_pct": m.cpu_pct, "mem_pct": m.mem_pct, "disk_pct": m.disk_pct,
        # 机器与资产/链路按归一 IP 精确关联，不猜测归属
        "assets": (linkage or {}).get("assets", []),
        "asset_count": (linkage or {}).get("asset_count", 0),
        "link_count": (linkage or {}).get("link_count", 0),
        "last_seen": m.last_seen.isoformat(timespec="seconds") if m.last_seen else None,
        "created_at": m.created_at.isoformat(timespec="seconds"),
    }


def _linkage_one(db, m: K8sMachine) -> dict:
    """单机互通（create/update/test 行回包用；列表走批量 ip_linkage_map）。"""
    from app.services.ipnorm import ip_linkage

    return ip_linkage(db, m.ip)


class MachineIn(BaseModel):
    host_name: str = Field(default="", max_length=64)
    ip: str = Field(max_length=64)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", max_length=64)
    ssh_password: str = Field(default="", max_length=128)   # 留空=不改/密钥免密
    role: str = Field(default="未分配", max_length=32)

class MachinePatchIn(BaseModel):
    host_name: str = Field(default="", max_length=64)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", max_length=64)
    ssh_password: str = Field(default="", max_length=128)   # 留空=不改
    role: str = Field(default="未分配", max_length=32)
    cluster_name: Optional[str] = Field(default=None, max_length=64)   # 不传=不改
    cluster_id: Optional[int] = None   # 集群 id 优先，名字用于回落与快照


def _validate(body) -> None:
    if not _NAME_RE.match(body.ip):
        raise HTTPException(400, "机器 IP/主机名非法")
    if body.role not in _ROLES:
        raise HTTPException(400, f"角色仅支持: {'/'.join(_ROLES)}")


@router.get("")
def list_machines(keyword: str = "", db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator", "viewer"))):
    q = db.query(K8sMachine).order_by(K8sMachine.id)
    rows = q.all()
    # 批量扫描资产/链路，在行上显示关联资产与链路边数
    from app.services.ipnorm import ip_linkage_map, norm_ip

    linkage = ip_linkage_map(db, [m.ip for m in rows])
    items = []
    for m in rows:
        if keyword and keyword.lower() not in (m.ip + m.host_name + m.ssh_user).lower():
            continue
        items.append(_machine_out(m, linkage.get(norm_ip(m.ip))))
    total = db.query(func.count(K8sMachine.id)).scalar() or 0
    ok_n = db.query(func.count(K8sMachine.id)).filter(K8sMachine.status == "ok").scalar() or 0
    err_n = db.query(func.count(K8sMachine.id)).filter(K8sMachine.status == "error").scalar() or 0
    return {"items": items, "total": total,
            "summary": {"total": total, "ok": ok_n, "error": err_n,
                        "assigned": sum(1 for m in rows if m.cluster_id)}}


@router.post("")
def create_machine(body: MachineIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    from app.core.security import encrypt_secret
    from app.services import audit as audit_svc

    _validate(body)
    if db.query(K8sMachine).filter(K8sMachine.ip == body.ip).first():
        raise HTTPException(400, "该机器已登记")
    m = K8sMachine(
        host_name=body.host_name or body.ip, ip=body.ip, ssh_port=body.ssh_port,
        ssh_user=body.ssh_user,
        ssh_password_cipher=encrypt_secret(body.ssh_password) if body.ssh_password else "",
        role=body.role)
    db.add(m)
    audit_svc.record(db, action="machine.create", user_id=user.id, username=user.username,
                     resource_type="machine", resource_id=body.ip,
                     detail={"role": body.role},
                     client_ip=get_client_ip(request))
    db.commit()
    return _machine_out(m, _linkage_one(db, m))


@router.put("/{mid}")
def update_machine(mid: int, body: MachinePatchIn, request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    from app.core.security import encrypt_secret

    m = db.get(K8sMachine, mid)
    if m is None:
        raise HTTPException(404, "机器不存在")
    if body.role not in _ROLES:
        raise HTTPException(400, f"角色仅支持: {'/'.join(_ROLES)}")
    m.host_name = body.host_name or m.host_name
    m.ssh_port = body.ssh_port
    m.ssh_user = body.ssh_user or m.ssh_user
    m.role = body.role
    if body.ssh_password:
        m.ssh_password_cipher = encrypt_secret(body.ssh_password)
    if body.cluster_name is not None or body.cluster_id is not None:
        # 集群引用按 id 优先解析并双写 (cluster_id, cluster_name)，
        # 保证 assigned 统计和删除守卫使用一致归属；未传引用字段时不修改。
        from app.services.refs import resolve_cluster_ref

        cid, cname = resolve_cluster_ref(db, body.cluster_id or 0, body.cluster_name or "")
        m.cluster_id = cid
        m.cluster_name = cname or (body.cluster_name or "")
    m.updated_at = datetime.now()
    db.commit()
    return _machine_out(m, _linkage_one(db, m))


@router.delete("/{mid}")
def delete_machine(mid: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    from app.services import audit as audit_svc

    m = db.get(K8sMachine, mid)
    if m is None:
        raise HTTPException(404, "机器不存在")
    if m.cluster_id:
        raise HTTPException(400, f"机器已分配给集群「{m.cluster_name}」——先在集群里移除/清空")
    db.delete(m)
    audit_svc.record(db, action="machine.delete", user_id=user.id, username=user.username,
                     resource_type="machine", resource_id=m.ip,
                     detail={}, client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True}


def _probe(ip: str, user: str, password: str, port: int = 22) -> dict:
    from app.collector.ssh import run_ssh

    return run_ssh(ip, user, "echo ok && hostname && uptime", timeout=15, password=password)


@router.post("/test")
def test_unsaved(body: MachineIn, user: User = Depends(require_roles("admin", "operator"))):
    """连通测试（不保存）。"""
    _validate(body)
    r = _probe(body.ip, body.ssh_user, body.ssh_password, body.ssh_port)
    return {"ok": r["ok"], "detail": ("可达：" + (r.get("lines") or [""])[-1][:80]) if r["ok"]
            else "SSH 不可达（检查 IP/端口/凭证）"}


@router.post("/{mid}/test")
def test_machine(mid: int, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator"))):
    """连通检测 + 水位采集（合一）。"""
    from app.core.security import decrypt_secret
    from app.collector.ssh import run_ssh

    m = db.get(K8sMachine, mid)
    if m is None:
        raise HTTPException(404, "机器不存在")
    pwd = ""
    if m.ssh_password_cipher:
        try:
            pwd = decrypt_secret(m.ssh_password_cipher)
        except Exception:
            m.status = "error"
            m.status_note = "凭证解密失败（MASTER_KEY 变更？）"
            db.commit()
            return {"ok": False, "detail": m.status_note}
    r = _probe(m.ip, m.ssh_user, pwd, m.ssh_port)
    m.status = "ok" if r["ok"] else "error"
    m.status_note = "" if r["ok"] else "SSH 不可达"
    m.last_seen = datetime.now() if r["ok"] else m.last_seen
    usage = {}
    if r["ok"]:
        u = run_ssh(m.ip, m.ssh_user, USAGE_CMD, timeout=25, password=pwd)
        vals = [x.strip() for x in (u.get("lines") or []) if x.strip().isdigit()]
        if len(vals) >= 3:
            m.cpu_pct, m.mem_pct, m.disk_pct = float(vals[-3]), float(vals[-2]), float(vals[-1])
            usage = {"cpu_pct": m.cpu_pct, "mem_pct": m.mem_pct, "disk_pct": m.disk_pct}
    m.updated_at = datetime.now()
    db.commit()
    return {"ok": r["ok"], "detail": "可达" if r["ok"] else "SSH 不可达（检查 IP/端口/凭证）",
            "usage": usage, "machine": _machine_out(m, _linkage_one(db, m))}
