"""组织架构管理 API：部门树 + 部门权限 + Namespace 授权。

GET    /org/departments                       部门平表（前端建树，含用户数）
POST   /org/departments                       新建部门
PUT    /org/departments/{id}                  改名 / 移动（parent_id 传了才改，防环）
DELETE /org/departments/{id}                  删除（有子部门拒；用户脱钩；连带策略/授权清理）
POST   /org/departments/reorder               同级拖动排序
GET    /org/departments/{id}/permissions      部门权限（本级 + 链上继承）
PUT    /org/departments/{id}/permissions      设置部门权限策略（allow/deny/default）
PUT    /org/users/{uid}/department            用户分配/移出部门
GET    /org/namespace-grants                  Namespace 授权列表
POST   /org/namespace-grants                  新增/更新授权（同主体+集群+ns 覆盖）
DELETE /org/namespace-grants/{id}             删除授权

全部仅 admin（授权收口的配置面）。每个变更落审计。
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.k8s import K8sCluster
from app.models.org import Department, DeptPermission, NamespaceGrant
from app.models.user import User
from app.services import audit
from app.services.authz import NS_ROLE_LEVELS, dept_chain_ids

router = APIRouter(prefix="/org", tags=["org"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}$")


def _dept_out(d: Department, user_count: int = 0) -> dict:
    return {"id": d.id, "name": d.name, "parent_id": d.parent_id or 0,
            "path": d.path, "sort_order": d.sort_order, "user_count": user_count}


def _get_dept(db: Session, dept_id: int) -> Department:
    d = db.get(Department, dept_id)
    if d is None:
        raise HTTPException(404, "部门不存在")
    return d


def _set_path(db: Session, d: Department, parent_path: str) -> None:
    """本部门 path=父 path + 自身 id + '/'（根='/<id>/'）；子树随之重写（移动/新建后调用）。

    必须先按旧 path 取子孙（排除自身）再改：自改路径后重查会把自身当子孙二次拼接，
    生成 '/1/2/3/2/3/' 之类的病态路径。
    """
    old = d.path or ""
    new = f"{parent_path or '/'}{d.id}/"
    subs = []
    if old and old != new:
        subs = (db.query(Department)
                .filter(Department.path.like(f"{old}%"), Department.id != d.id).all())
    d.path = new
    for sub in subs:
        sub.path = new + sub.path[len(old):]


# ---------------- 部门树 ----------------

class DeptIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    parent_id: int = 0   # 0=顶级


class DeptPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    parent_id: Optional[int] = None   # 传了才改；不允许挂到自己的子孙下


@router.get("/departments")
def list_departments(db: Session = Depends(get_db),
                     user: User = Depends(require_roles("admin"))):
    counts = dict(db.query(User.department_id, func.count(User.id))
                  .filter(User.department_id.isnot(None)).group_by(User.department_id).all())
    rows = db.query(Department).order_by(Department.path, Department.sort_order).all()
    return [_dept_out(d, counts.get(d.id, 0)) for d in rows]


@router.post("/departments")
def create_department(body: DeptIn, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_roles("admin"))):
    parent_path = ""
    if body.parent_id:
        parent = _get_dept(db, body.parent_id)
        parent_path = parent.path
    nxt = ((db.query(func.max(Department.sort_order))
            .filter(Department.parent_id == (body.parent_id or 0)).scalar() or 0) + 1)
    d = Department(name=body.name.strip(), parent_id=body.parent_id or 0, sort_order=nxt)
    db.add(d)
    db.flush()   # 取 id 再拼物化路径
    _set_path(db, d, parent_path)
    audit.record(db, action="department.create", user_id=user.id, username=user.username,
                 resource_type="department", resource_id=str(d.id),
                 detail={"name": d.name, "parent_id": d.parent_id},
                 client_ip=get_client_ip(request))
    db.commit()
    return _dept_out(d)


@router.put("/departments/{dept_id}")
def update_department(dept_id: int, body: DeptPatchIn, request: Request,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_roles("admin"))):
    d = _get_dept(db, dept_id)
    moved = False
    if body.name is not None and body.name.strip():
        d.name = body.name.strip()
    if body.parent_id is not None and (body.parent_id or 0) != (d.parent_id or 0):
        parent_path = ""
        if body.parent_id:
            parent = _get_dept(db, body.parent_id)
            # 防环：不能挂到自己的子孙下（物化路径前缀判定）
            if parent.path.startswith(d.path or f"/{d.id}/"):
                raise HTTPException(400, "不能把部门移动到它自己的下级部门之下")
            parent_path = parent.path
        d.parent_id = body.parent_id or 0
        _set_path(db, d, parent_path)
        moved = True
    audit.record(db, action="department.update", user_id=user.id, username=user.username,
                 resource_type="department", resource_id=str(dept_id),
                 detail={"name": d.name, "moved": moved, "parent_id": d.parent_id},
                 client_ip=get_client_ip(request))
    db.commit()
    return _dept_out(d)


class ReorderIn(BaseModel):
    parent_id: int = 0
    ids: list = Field(default_factory=list)   # 同级部门 id（按新顺序）


@router.post("/departments/reorder")
def reorder_departments(body: ReorderIn, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_roles("admin"))):
    """同级拖动排序：ids=该父级下全部子部门的新顺序。"""
    if not all(isinstance(x, int) for x in body.ids):
        raise HTTPException(400, "排序列表格式非法")
    children = db.query(Department).filter(
        Department.parent_id == (body.parent_id or 0)).all()
    cur = {c.id for c in children}
    if set(body.ids) != cur or len(body.ids) != len(cur):
        raise HTTPException(400, "排序列表与现有子部门不一致（请刷新后重试）")
    for i, did in enumerate(body.ids):
        db.get(Department, did).sort_order = i + 1
    audit.record(db, action="department.reorder", user_id=user.id, username=user.username,
                 resource_type="department", resource_id=str(body.parent_id or 0),
                 detail={"count": len(body.ids)}, client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True}


@router.delete("/departments/{dept_id}")
def delete_department(dept_id: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_roles("admin"))):
    d = _get_dept(db, dept_id)
    if db.query(Department).filter(Department.parent_id == d.id).count():
        raise HTTPException(400, "该部门下还有子部门——请先移动/删除子部门")
    n_users = db.query(User).filter(User.department_id == d.id).count()
    db.query(User).filter(User.department_id == d.id).update({User.department_id: None})
    db.query(DeptPermission).filter(DeptPermission.department_id == d.id).delete()
    db.query(NamespaceGrant).filter(NamespaceGrant.subject_type == "dept",
                                    NamespaceGrant.subject_id == d.id).delete()
    name = d.name
    db.delete(d)
    audit.record(db, action="department.delete", user_id=user.id, username=user.username,
                 resource_type="department", resource_id=str(dept_id),
                 detail={"name": name, "detached_users": n_users},
                 client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True, "detached_users": n_users}


# ---------------- 部门权限策略 ----------------

class DeptPermIn(BaseModel):
    code: str = Field(max_length=64)
    effect: str = Field(max_length=8)   # allow / deny / default(删除覆盖)


@router.get("/departments/{dept_id}/permissions")
def department_permissions(dept_id: int, db: Session = Depends(get_db),
                           user: User = Depends(require_roles("admin"))):
    """本级覆盖 + 链上继承（根→本级；子部门继承父级，deny 优先见 effective_codes）。"""
    from app.core.permissions import PERMISSION_CODES

    d = _get_dept(db, dept_id)
    chain = dept_chain_ids(d)
    own = {p.code: p.effect for p in
           db.query(DeptPermission).filter(DeptPermission.department_id == dept_id).all()}
    inherited = []
    for cid in chain:
        if cid == dept_id:
            continue
        parent = db.get(Department, cid)
        for p in db.query(DeptPermission).filter(DeptPermission.department_id == cid).all():
            inherited.append({"code": p.code, "effect": p.effect,
                              "department_id": cid,
                              "department_name": parent.name if parent else ""})
    return {"catalog": PERMISSION_CODES, "own": own, "inherited": inherited,
            "chain": [{"id": cid,
                       "name": (db.get(Department, cid).name if db.get(Department, cid) else "")}
                      for cid in chain]}


@router.put("/departments/{dept_id}/permissions")
def set_department_permission(dept_id: int, body: DeptPermIn, request: Request,
                              db: Session = Depends(get_db),
                              user: User = Depends(require_roles("admin"))):
    from app.core.permissions import PERMISSION_CODES

    _get_dept(db, dept_id)
    if body.code not in PERMISSION_CODES:
        raise HTTPException(400, "未知权限码")
    if body.effect not in ("allow", "deny", "default"):
        raise HTTPException(400, "effect 仅支持 allow/deny/default")
    q = db.query(DeptPermission).filter(DeptPermission.department_id == dept_id,
                                        DeptPermission.code == body.code)
    q.delete()
    if body.effect != "default":
        db.add(DeptPermission(department_id=dept_id, code=body.code, effect=body.effect))
    audit.record(db, action="dept_permission.set", user_id=user.id, username=user.username,
                 resource_type="department", resource_id=str(dept_id),
                 detail={"code": body.code, "effect": body.effect},
                 client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True}


# ---------------- 用户分配部门 ----------------

class UserDeptIn(BaseModel):
    department_id: Optional[int] = None   # None=移出部门


@router.put("/users/{uid}/department")
def set_user_department(uid: int, body: UserDeptIn, request: Request,
                        db: Session = Depends(get_db),
                        user: User = Depends(require_roles("admin"))):
    row = db.get(User, uid)
    if row is None:
        raise HTTPException(404, "用户不存在")
    if body.department_id:
        _get_dept(db, body.department_id)
    row.department_id = body.department_id or None
    audit.record(db, action="user.department.set", user_id=user.id, username=user.username,
                 resource_type="user", resource_id=str(uid),
                 detail={"department_id": row.department_id},
                 client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True, "department_id": row.department_id}


# ---------------- Namespace 授权 ----------------

class GrantIn(BaseModel):
    subject_type: str = Field(pattern="^(user|dept)$")
    subject_id: int
    cluster_id: int
    namespace: str = Field(default="", max_length=128)   # '*'=全部；''=集群级（仅 '*' 可覆盖）
    role: str = Field(default="view", max_length=8)      # view / use / admin


def _grant_out(g: NamespaceGrant, db: Session) -> dict:
    subject_name = ""
    if g.subject_type == "user":
        u = db.get(User, g.subject_id)
        subject_name = u.username if u else ""
    else:
        d = db.get(Department, g.subject_id)
        subject_name = d.name if d else ""
    c = db.get(K8sCluster, g.cluster_id)
    return {"id": g.id, "subject_type": g.subject_type, "subject_id": g.subject_id,
            "subject_name": subject_name, "cluster_id": g.cluster_id,
            "cluster_name": c.name if c else "", "namespace": g.namespace,
            "role": g.role,
            "created_at": g.created_at.isoformat(timespec="seconds") if g.created_at else ""}


@router.get("/namespace-grants")
def list_grants(cluster_id: int = Query(0), subject_type: str = Query(""),
                subject_id: int = Query(0),
                db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin"))):
    q = db.query(NamespaceGrant)
    if cluster_id:
        q = q.filter(NamespaceGrant.cluster_id == cluster_id)
    if subject_type:
        q = q.filter(NamespaceGrant.subject_type == subject_type)
    if subject_id:
        q = q.filter(NamespaceGrant.subject_id == subject_id)
    rows = q.order_by(NamespaceGrant.cluster_id, NamespaceGrant.subject_type,
                      NamespaceGrant.subject_id, NamespaceGrant.namespace).all()
    return [_grant_out(g, db) for g in rows]


@router.post("/namespace-grants")
def upsert_grant(body: GrantIn, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin"))):
    ns = (body.namespace or "").strip()
    if ns != "*" and ns and not _NAME_RE.match(ns):
        raise HTTPException(400, "命名空间非法（或用 * 表示全部）")
    if body.role not in NS_ROLE_LEVELS:
        raise HTTPException(400, "role 仅支持 view/use/admin")
    if db.get(K8sCluster, body.cluster_id) is None:
        raise HTTPException(400, "集群不存在")
    if body.subject_type == "user":
        if db.get(User, body.subject_id) is None:
            raise HTTPException(400, "用户不存在")
    else:
        _get_dept(db, body.subject_id)
    # 同主体+集群+ns 唯一：覆盖式更新
    q = db.query(NamespaceGrant).filter(
        NamespaceGrant.subject_type == body.subject_type,
        NamespaceGrant.subject_id == body.subject_id,
        NamespaceGrant.cluster_id == body.cluster_id,
        NamespaceGrant.namespace == ns)
    q.delete()
    g = NamespaceGrant(subject_type=body.subject_type, subject_id=body.subject_id,
                       cluster_id=body.cluster_id, namespace=ns, role=body.role)
    db.add(g)
    db.flush()
    audit.record(db, action="namespace_grant.set", user_id=user.id, username=user.username,
                 resource_type="namespace_grant", resource_id=str(g.id),
                 detail={"subject": f"{body.subject_type}:{body.subject_id}",
                         "cluster_id": body.cluster_id, "namespace": ns or "(集群级)",
                         "role": body.role},
                 client_ip=get_client_ip(request))
    db.commit()
    return _grant_out(g, db)


@router.delete("/namespace-grants/{grant_id}")
def delete_grant(grant_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin"))):
    g = db.get(NamespaceGrant, grant_id)
    if g is None:
        raise HTTPException(404, "授权不存在")
    detail = {"subject": f"{g.subject_type}:{g.subject_id}", "cluster_id": g.cluster_id,
              "namespace": g.namespace or "(集群级)", "role": g.role}
    db.delete(g)
    audit.record(db, action="namespace_grant.delete", user_id=user.id, username=user.username,
                 resource_type="namespace_grant", resource_id=str(grant_id),
                 detail=detail, client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True}
