"""组织与授权求值：部门权限树 + K8s Namespace 授权。

两条正交的授权轴：

一、权限码轴（部门策略 × 用户策略 × 角色默认）——求值在 core/permissions.py
   effective_codes(role, user_policies, dept_policies)，叠加优先级（高→低）：
       用户 deny > 用户 allow > 部门 deny > 部门 allow > 角色默认
   部门维度沿部门链（物化路径）继承：链上任意 deny 压过任意 allow（deny 优先）；
   本模块负责把"用户 → 部门链 → 策略行"收集出来交给 effective_codes。

二、Namespace 授权轴（dept/user × cluster × namespace × role[view/use/admin]）——
   K8s 资源查询/操作按授权过滤，规则：
   - admin 用户直通（平台管理员豁免）；
   - 只读集群不受授权影响（查询维持现状，变更本就有 read_only 硬门拦）；
   - 查询：集群**无任何授权行**=未启用授权过滤（存量行为零变化）；
     有授权行=非 admin 仅可见被授权命名空间（部门链+用户直接授权取最大等级，
     namespace='*' 通配全部；Node 等集群级资源（namespace=''）只认 '*' 授权）；
   - 操作：授权只增不减——非 admin 必须持有 ≥need 的授权（不设"无授权直通"），
     namespace='view' 只看不能改；'use'=变更（apply/重启/驱逐/批量/helm 升级）；
     'admin'=高危（Secret 揭示 / helm 卸载）。平台角色下限不变：viewer 永不操作。
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.k8s import K8sCluster
from app.models.org import DeptPermission, Department, NamespaceGrant
from app.models.permission import PermissionPolicy
from app.models.user import User

NS_ROLE_LEVELS = {"view": 1, "use": 2, "admin": 3}


# ---------------- 部门链与部门权限 ----------------

def dept_chain_ids(dept: Optional[Department]) -> list:
    """部门链 id（根→本部门）。path='/1/3/7/' → [1, 3, 7]；坏路径回退单点。"""
    if dept is None:
        return []
    try:
        ids = [int(x) for x in (dept.path or "").strip("/").split("/") if x]
    except ValueError:
        ids = []
    return ids or ([dept.id] if dept.id else [])


def dept_chain_policies(db: Session, user: User) -> list:
    """用户部门链上全部策略行 [(code, effect)]（链上继承，交给 effective_codes 叠加）。"""
    if not user.department_id:
        return []
    dept = db.get(Department, user.department_id)
    ids = dept_chain_ids(dept)
    if not ids:
        return []
    rows = db.query(DeptPermission).filter(DeptPermission.department_id.in_(ids)).all()
    return [(p.code, p.effect) for p in rows]


def user_policies(db: Session, user: User) -> list:
    """用户级策略行 [(code, effect)]（与历史 permission_policies 口径一致）。"""
    return [(p.code, p.effect) for p in
            db.query(PermissionPolicy).filter(PermissionPolicy.user_id == user.id).all()]


# ---------------- Namespace 授权 ----------------

def resolve_cluster(db: Session, cluster_id: int = 0, cluster_name: str = "") -> Optional[K8sCluster]:
    q = db.query(K8sCluster)
    if cluster_id:
        return q.filter(K8sCluster.id == cluster_id).first()
    if cluster_name:
        return q.filter(K8sCluster.name == cluster_name).first()
    return q.order_by(K8sCluster.id).first()


def _grant_level(db: Session, user: User, cluster_id: int, namespace: str) -> int:
    """授权行求值：用户直接授权 ∪ 部门链授权，按 (cluster, ns|'*') 取最大等级。

    namespace=''（集群级资源，如 Node）只匹配 '*' 授权。
    """
    if not cluster_id:
        return 0
    sids_user = [user.id]
    sids_dept = []
    if user.department_id:
        dept = db.get(Department, user.department_id)
        sids_dept = dept_chain_ids(dept)
    rows = []
    if sids_user:
        rows += db.query(NamespaceGrant).filter(
            NamespaceGrant.subject_type == "user",
            NamespaceGrant.subject_id.in_(sids_user),
            NamespaceGrant.cluster_id == cluster_id).all()
    if sids_dept:
        rows += db.query(NamespaceGrant).filter(
            NamespaceGrant.subject_type == "dept",
            NamespaceGrant.subject_id.in_(sids_dept),
            NamespaceGrant.cluster_id == cluster_id).all()
    level = 0
    for g in rows:
        if g.namespace == "*" or (namespace and g.namespace == namespace):
            level = max(level, NS_ROLE_LEVELS.get(g.role, 0))
    return level


def cluster_has_grants(db: Session, cluster_id: int) -> bool:
    return (db.query(NamespaceGrant.id)
            .filter(NamespaceGrant.cluster_id == cluster_id).first() is not None)


def ns_view_level(db: Session, user: User, cluster: Optional[K8sCluster], namespace: str) -> int:
    """查询面等级（0..3）：admin / 只读集群 / 集群无授权行 = 3（不过滤，存量零变化）。"""
    if cluster is None or user.role == "admin":
        return 3
    if cluster.read_only:
        return 3   # 只读集群不受授权影响（任务口径）
    if not cluster_has_grants(db, cluster.id):
        return 3   # 未启用收口
    return _grant_level(db, user, cluster.id, namespace)


def ns_op_level(db: Session, user: User, cluster: Optional[K8sCluster], namespace: str) -> int:
    """操作面等级（0..3）：授权只增不减——非 admin 无授权=0（不设"无授权直通"）。

    admin / 只读集群返回 3：前者直通，后者变更由 read_only 硬门拦（保持原报错口径）。
    """
    if cluster is None or user.role == "admin":
        return 3
    if cluster.read_only:
        return 3
    return _grant_level(db, user, cluster.id, namespace)


def ns_scope(db: Session, user: User, cluster: K8sCluster) -> Optional[set]:
    """查询面行过滤口径：None=不过滤；否则=可见命名空间集合（含 '*' 通配）。"""
    if user.role == "admin" or cluster.read_only:
        return None
    if not cluster_has_grants(db, cluster.id):
        return None
    sids_user = [user.id]
    sids_dept = []
    if user.department_id:
        dept = db.get(Department, user.department_id)
        sids_dept = dept_chain_ids(dept)
    rows = []
    if sids_user:
        rows += db.query(NamespaceGrant).filter(
            NamespaceGrant.subject_type == "user",
            NamespaceGrant.subject_id.in_(sids_user),
            NamespaceGrant.cluster_id == cluster.id).all()
    if sids_dept:
        rows += db.query(NamespaceGrant).filter(
            NamespaceGrant.subject_type == "dept",
            NamespaceGrant.subject_id.in_(sids_dept),
            NamespaceGrant.cluster_id == cluster.id).all()
    return {g.namespace for g in rows if NS_ROLE_LEVELS.get(g.role, 0) >= 1}


def ns_row_ok(scope: Optional[set], namespace: str) -> bool:
    """行可见性：scope=None 全见；'*' 通配全见（含集群级 namespace=''）；
    否则须为已授权命名空间（集群级资源 namespace='' 只认 '*'）。"""
    if scope is None or "*" in scope:
        return True
    return bool(namespace) and namespace in scope


def require_ns(db: Session, user: User, cluster: Optional[K8sCluster], namespace: str,
               need: str, for_op: bool = True) -> None:
    """操作/查询门：等级不足 403（报错说明缺什么授权）。"""
    need_level = NS_ROLE_LEVELS.get(need, 1)
    level = ns_op_level(db, user, cluster, namespace) if for_op else \
        ns_view_level(db, user, cluster, namespace)
    if level < need_level:
        raise HTTPException(
            403,
            f"无权{'操作' if for_op else '查看'}命名空间「{namespace or '（集群级）'}」——"
            f"需要 {need} 级授权（当前 {'无授权' if not level else '授权不足'}）；"
            "请管理员在「组织架构-Namespace 授权」为你的部门/账号授权")
