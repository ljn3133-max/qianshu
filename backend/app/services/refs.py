"""引用统一：user/cluster 引用规范与解析。

规范（audit_logs 的 (user_id, username) 模式推广为全平台模板）：
  - user 引用 = **双字段**：`*_by_id` int 连 users.id（改名/删号不断链、可 join），
    `*_by` / `username` 字符串为展示快照（历史页面零改动）。写入处双写，读取两列都给。
  - cluster 引用 = **双字段**：`cluster_id` int 连 k8s_clusters.id（0=未关联），
    `cluster_name` 展示快照。id 优先解析、名字只作快照/回落。

历史列名注记（不改列防回滚风险，只改口径）：
  diagnosis_sessions / diagnosis_cases / saved_queries 的 `created_by` 是 int——
  语义即 users.id（历史命名），读取按 id 用；不与字符串 *_by 混淆。
"""
from __future__ import annotations


def user_pair(user) -> tuple:
    """User 对象/duck 类型 → (user_id, username)；无 id/名字时给 0/""（不猜）。"""
    uid = getattr(user, "id", 0) or 0
    name = getattr(user, "username", "") or ""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        uid = 0
    return uid, str(name)


def user_fields(user, by_field: str = "created_by") -> dict:
    """写入双字段模板：{created_by: username, created_by_id: user_id}。

    by_field 给列名前缀（ack_by/confirmed_by/submitted_by/created_by）。
    """
    uid, name = user_pair(user)
    return {by_field: name, f"{by_field}_id": uid}


def resolve_cluster_ref(db, cluster_id: int = 0, cluster_name: str = "") -> tuple:
    """(cluster_id, cluster_name) 解析：id 优先，名字回落；返回 (id, name 快照)。

    都对不上 → (0, 传入名字)（快照保留，不造引用）。
    """
    from app.models.k8s import K8sCluster

    cid = int(cluster_id or 0)
    name = (cluster_name or "").strip()
    if cid:
        row = db.get(K8sCluster, cid)
        return (cid, row.name if row is not None else name)
    if name:
        row = db.query(K8sCluster).filter(K8sCluster.name == name).first()
        if row is not None:
            return row.id, row.name
    return 0, name
