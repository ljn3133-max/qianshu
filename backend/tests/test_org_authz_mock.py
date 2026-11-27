#!/usr/bin/env python3
"""部门与命名空间授权隔离 mock 测试：部门树 + 部门权限叠加 + K8s Namespace 授权。

运行：cd /opt/qianshu/backend && python3 tests/test_org_authz_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu.db、不碰 admin 账号（全部一次性自建用户）、零网络（ops 授权断言停在
"无授权 403 / 通过授权后落 400 未登记 SSH"，绝不真正执行任何变更）。

验证项：
  A 部门树 CRUD：建/改名/移动（子树 path 重写）/同级排序/删叶/删有子拒/删部门用户脱钩/防环
  B 权限叠加矩阵：角色默认 / 部门 allow 继承 / 部门 deny / 链上 deny 压 allow /
    用户 allow 压部门 deny / 用户 deny 压一切（用户 deny>用户 allow>部门 deny>部门 allow>角色默认）
  C Namespace 授权过滤：查询按授权过滤（无授权行集群=存量全可见）；'*' 通配含集群级
    Node 行；只读集群不受授权影响；admin 直通；操作面 use/admin 分级（403 拦 vs 放行）
  D 用户级叠加：无部门用户策略行为与旧版逐位一致（向后兼容零变化）
  E 迁移幂等：旧形 users 表补 department_id，连跑两遍不报错不重复
  F 旧签名向后兼容：effective_codes 两参调用结果与升级前公式一致
"""
import os
import sys
import tempfile
from datetime import datetime

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-org-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("org")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import run_migrations  # noqa: E402
from app.core.permissions import ROLE_DEFAULTS, effective_codes  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.k8s import K8sCluster, K8sFailure, K8sPodLog, K8sResource  # noqa: E402
from app.models.org import Department, DeptPermission, NamespaceGrant  # noqa: E402
from app.models.permission import PermissionPolicy  # noqa: E402
from app.models.user import User  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


Base.metadata.create_all(engine)
db = SessionLocal()
for uname, role in (("admin1", "admin"), ("op1", "operator"), ("op2", "operator"),
                    ("view1", "viewer")):
    db.add(User(username=uname, password_hash=hash_password("Test-Only-Pw1"),
                role=role, is_active=True, must_change_password=False))
db.commit()


def _req(method: str, url: str, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


client = type("C", (), {
    "get": staticmethod(lambda url, **kw: _req("GET", url, **kw)),
    "post": staticmethod(lambda url, **kw: _req("POST", url, **kw)),
    "put": staticmethod(lambda url, **kw: _req("PUT", url, **kw)),
    "delete": staticmethod(lambda url, **kw: _req("DELETE", url, **kw)),
})()


def login_jwt(username: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": "Test-Only-Pw1"})
    assert r.status_code == 200, f"login {username} 失败: {r.status_code} {r.text[:200]}"
    return r.json()["token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


jwt_admin = login_jwt("admin1")
jwt_op1 = login_jwt("op1")
jwt_op2 = login_jwt("op2")
jwt_view = login_jwt("view1")
H_ADMIN, H_OP1, H_OP2, H_VIEW = (bearer(jwt_admin), bearer(jwt_op1),
                                 bearer(jwt_op2), bearer(jwt_view))


# ================= A 部门树 CRUD =================
print("A 部门树 CRUD")
r = client.post("/api/org/departments", headers=H_ADMIN, json={"name": "总部", "parent_id": 0})
d_root = r.json()
check("建顶级部门", r.status_code == 200 and d_root.get("path") == f"/{d_root.get('id')}/",
      str(d_root)[:200])
r = client.post("/api/org/departments", headers=H_ADMIN,
                json={"name": "研发", "parent_id": d_root["id"]})
d_dev = r.json()
r = client.post("/api/org/departments", headers=H_ADMIN,
                json={"name": "平台组", "parent_id": d_dev["id"]})
d_plat = r.json()
check("建子部门 path 物化", d_dev.get("path") == f"/{d_root['id']}/{d_dev['id']}/"
      and d_plat.get("path") == f"/{d_root['id']}/{d_dev['id']}/{d_plat['id']}/",
      f"{d_dev.get('path')} {d_plat.get('path')}")

r = client.put(f"/api/org/departments/{d_dev['id']}", headers=H_ADMIN, json={"name": "研发部"})
check("改名", r.status_code == 200 and r.json()["name"] == "研发部", r.text[:150])

# 移动：平台组 挂到 顶级 → 子树 path 重写
r = client.put(f"/api/org/departments/{d_plat['id']}", headers=H_ADMIN,
               json={"parent_id": 0})
check("移动子树 path 重写", r.status_code == 200
      and r.json()["path"] == f"/{d_plat['id']}/", r.text[:150])
r = client.put(f"/api/org/departments/{d_plat['id']}", headers=H_ADMIN,
               json={"parent_id": d_dev["id"]})
check("移回（path 还原）", r.json()["path"] == f"/{d_root['id']}/{d_dev['id']}/{d_plat['id']}/")

# 防环：研发部 挂到 平台组 下 → 拒
r = client.put(f"/api/org/departments/{d_dev['id']}", headers=H_ADMIN,
               json={"parent_id": d_plat["id"]})
check("防环：不能挂到自己子孙下", r.status_code == 400, r.text[:150])

# 同级排序
r = client.post("/api/org/departments", headers=H_ADMIN,
                json={"name": "测试组", "parent_id": d_dev["id"]})
d_qa = r.json()
r = client.post("/api/org/departments/reorder", headers=H_ADMIN,
                json={"parent_id": d_dev["id"], "ids": [d_qa["id"], d_plat["id"]]})
ok1 = r.status_code == 200
r = client.get("/api/org/departments", headers=H_ADMIN)
rows = {x["id"]: x for x in r.json()}
check("同级拖动排序", ok1 and rows[d_qa["id"]]["sort_order"] < rows[d_plat["id"]]["sort_order"],
      str(rows.get(d_qa["id"], {}))[:150])

# 非 admin 无权管理
r = client.get("/api/org/departments", headers=H_OP1)
check("组织管理仅 admin", r.status_code == 403, r.text[:100])

# 删有子部门拒；删叶成功；删除部门用户脱钩+策略/授权连带清理
r = client.delete(f"/api/org/departments/{d_dev['id']}", headers=H_ADMIN)
check("有子部门拒删", r.status_code == 400, r.text[:150])
uid_op2 = db.query(User).filter(User.username == "op2").first().id
r = client.put(f"/api/org/users/{uid_op2}/department", headers=H_ADMIN,
               json={"department_id": d_qa["id"]})
ok_assign = r.status_code == 200
client.put(f"/api/org/departments/{d_qa['id']}/permissions", headers=H_ADMIN,
           json={"code": "k8s.write", "effect": "deny"})
# 给 d_qa 挂一条 ns 授权（验证连带清理）
clu0 = K8sCluster(name="clu-del", api_url="https://127.0.0.1:6443", read_only=False)
db.add(clu0)
db.commit()
client.post("/api/org/namespace-grants", headers=H_ADMIN,
            json={"subject_type": "dept", "subject_id": d_qa["id"],
                  "cluster_id": clu0.id, "namespace": "ns-a", "role": "view"})
r = client.delete(f"/api/org/departments/{d_qa['id']}", headers=H_ADMIN)
row = db.get(User, uid_op2)
check("删叶部门+用户脱钩+连带清理",
      ok_assign and r.status_code == 200 and row.department_id is None
      and db.query(DeptPermission).filter(DeptPermission.department_id == d_qa["id"]).count() == 0
      and db.query(NamespaceGrant).filter(NamespaceGrant.subject_id == d_qa["id"]).count() == 0,
      r.text[:200])
check("移出部门", client.put(f"/api/org/users/{uid_op2}/department", headers=H_ADMIN,
                          json={"department_id": None}).json().get("department_id") is None)


# ================= B 权限叠加矩阵 =================
print("B 权限叠加矩阵（用户 deny > 用户 allow > 部门 deny > 部门 allow > 角色默认）")


def eff_of(uid: int) -> list:
    r = client.get(f"/api/auth/permissions/{uid}", headers=H_ADMIN)
    return r.json().get("effective", [])


uid_op1 = db.query(User).filter(User.username == "op1").first().id
op_default = set(ROLE_DEFAULTS["operator"])

# op1 挂到 平台组（链：总部→研发部→平台组）
client.put(f"/api/org/users/{uid_op1}/department", headers=H_ADMIN,
           json={"department_id": d_plat["id"]})

# 1) 角色默认（无任何策略）
got = set(eff_of(uid_op1))
check("B1 角色默认不变", got == op_default, f"{sorted(got)} != {sorted(op_default)}")

# 2) 部门 allow 沿链继承（挂在总部 → 平台组用户生效）
client.put(f"/api/org/departments/{d_root['id']}/permissions", headers=H_ADMIN,
           json={"code": "agent.change", "effect": "allow"})
got = set(eff_of(uid_op1))
check("B2 部门 allow 继承", "agent.change" in got and got >= op_default, str(sorted(got)))

# 3) 部门 deny 压角色默认（挂在研发部 → 平台组用户生效）
client.put(f"/api/org/departments/{d_dev['id']}/permissions", headers=H_ADMIN,
           json={"code": "machines.ssh", "effect": "deny"})
got = set(eff_of(uid_op1))
check("B3 部门 deny 压角色默认", "machines.ssh" not in got, str(sorted(got)))

# 4) 链上 deny 压 allow（总部 allow k8s.dangerous / 平台组 deny k8s.dangerous）
client.put(f"/api/org/departments/{d_root['id']}/permissions", headers=H_ADMIN,
           json={"code": "k8s.dangerous", "effect": "allow"})
client.put(f"/api/org/departments/{d_plat['id']}/permissions", headers=H_ADMIN,
           json={"code": "k8s.dangerous", "effect": "deny"})
got = set(eff_of(uid_op1))
check("B4 链上 deny 压 allow（就近 deny 生效）", "k8s.dangerous" not in got, str(sorted(got)))
# 反向：总部 deny / 子部门 allow → 仍拒绝（deny 优先）
client.put(f"/api/org/departments/{d_root['id']}/permissions", headers=H_ADMIN,
           json={"code": "k8s.dangerous", "effect": "deny"})
client.put(f"/api/org/departments/{d_plat['id']}/permissions", headers=H_ADMIN,
           json={"code": "k8s.dangerous", "effect": "allow"})
got = set(eff_of(uid_op1))
check("B5 反向：祖先 deny 压后代 allow", "k8s.dangerous" not in got, str(sorted(got)))

# 6) 用户 allow 压部门 deny（machines.ssh 被研发部 deny，用户 allow 翻回）
client.post("/api/auth/permissions/manage", headers=H_ADMIN,
            json={"user_id": uid_op1, "code": "machines.ssh", "effect": "allow"})
got = set(eff_of(uid_op1))
check("B6 用户 allow 压部门 deny", "machines.ssh" in got, str(sorted(got)))

# 7) 用户 deny 压一切（角色默认 k8s.write + 部门 allow agent.change 全压掉）
client.post("/api/auth/permissions/manage", headers=H_ADMIN,
            json={"user_id": uid_op1, "code": "k8s.write", "effect": "deny"})
client.post("/api/auth/permissions/manage", headers=H_ADMIN,
            json={"user_id": uid_op1, "code": "agent.change", "effect": "deny"})
got = set(eff_of(uid_op1))
check("B7 用户 deny 压一切", "k8s.write" not in got and "agent.change" not in got,
      str(sorted(got)))

# 8) /auth/me 与 /auth/permissions 口径一致（生效面同源）
r = client.get("/api/auth/me", headers=H_OP1)
check("B8 /auth/me 同口径", set(r.json()["permission_codes"]) == got,
      f"{r.json()['permission_codes']} vs {sorted(got)}")

# 清理 B 组策略（后续 C 组不受影响）
db.query(PermissionPolicy).filter(PermissionPolicy.user_id == uid_op1).delete()
db.query(DeptPermission).delete()
db.commit()


# ================= C Namespace 授权过滤 =================
print("C K8s Namespace 授权过滤")
clu_rw = K8sCluster(name="clu-rw", api_url="https://10.0.0.1:6443", read_only=False,
                    ssh_host="")
clu_ro = K8sCluster(name="clu-ro", api_url="https://10.0.0.2:6443", read_only=True)
db.add_all([clu_rw, clu_ro])
db.commit()
rows_seed = [
    (clu_rw.id, "Pod", "ns-a", "p-a1"), (clu_rw.id, "Pod", "ns-b", "p-b1"),
    (clu_rw.id, "Node", "", "node1"),
    (clu_ro.id, "Pod", "ns-a", "p-a2"), (clu_ro.id, "Pod", "ns-b", "p-b2"),
]
for cid, kind, ns, name in rows_seed:
    db.add(K8sResource(cluster_id=cid, kind=kind, namespace=ns, name=name,
                       status_json='{"status": "Running"}'))
db.add(K8sPodLog(cluster_id=clu_rw.id, namespace="ns-a", pod="p-a1", ts=datetime.now(),
                 line="log-a"))
db.add(K8sPodLog(cluster_id=clu_rw.id, namespace="ns-b", pod="p-b1", ts=datetime.now(),
                 line="log-b"))
db.add(K8sFailure(cluster_id=clu_rw.id, namespace="ns-a", pod="p-a1", failure_type="Pending"))
db.add(K8sFailure(cluster_id=clu_rw.id, namespace="ns-b", pod="p-b1", failure_type="Pending"))
db.commit()


def res_names(headers, kind, cluster_id=0):
    r = client.get("/api/k8s/resources", headers=headers,
                   params={"kind": kind, "cluster_id": cluster_id, "include_destroyed": 1})
    return sorted(x["name"] for x in r.json().get("items", []))


# C0 存量零变化：无任何授权行 → 全可见
check("C0 无授权行=存量全可见",
      res_names(H_OP1, "Pod") == ["p-a1", "p-a2", "p-b1", "p-b2"]
      and res_names(H_OP1, "Node") == ["node1"], str(res_names(H_OP1, "Pod")))

# C1 部门 view 授权（op1 所属部门）→ 只见 clu_rw/ns-a；只读集群 clu_ro 不受影响
client.put(f"/api/org/users/{uid_op1}/department", headers=H_ADMIN,
           json={"department_id": d_plat["id"]})
r = client.post("/api/org/namespace-grants", headers=H_ADMIN,
                json={"subject_type": "dept", "subject_id": d_plat["id"],
                      "cluster_id": clu_rw.id, "namespace": "ns-a", "role": "view"})
grant_view_id = r.json().get("id")
got_pods = res_names(H_OP1, "Pod")
check("C1 部门授权过滤+只读集群不受影响",
      got_pods == ["p-a1", "p-a2", "p-b2"], str(got_pods))
check("C1b 集群级 Node 行只认 '*' 授权", res_names(H_OP1, "Node") == [],
      str(res_names(H_OP1, "Node")))

# C2 '*' 通配 → Node 行可见 + clu_rw 全 ns
r = client.post("/api/org/namespace-grants", headers=H_ADMIN,
                json={"subject_type": "user", "subject_id": uid_op1,
                      "cluster_id": clu_rw.id, "namespace": "*", "role": "view"})
grant_star_id = r.json().get("id")
check("C2 '*' 通配（含集群级）",
      res_names(H_OP1, "Pod") == ["p-a1", "p-a2", "p-b1", "p-b2"]
      and res_names(H_OP1, "Node") == ["node1"], str(res_names(H_OP1, "Pod")))

# C3 日志/失败行同口径（收窄到 ns-a：去掉 '*' 行）；
# namespaces 下拉=跨集群并集（clu-ro 不受授权影响仍带出 ns-b）
client.delete(f"/api/org/namespace-grants/{grant_star_id}", headers=H_ADMIN)
r = client.get("/api/k8s/logs", headers=H_OP1, params={"limit": 50})
logs = sorted(x["line"] for x in r.json().get("items", []))
r = client.get("/api/k8s/failures", headers=H_OP1, params={"limit": 50})
fails = sorted(x["namespace"] for x in r.json().get("items", []))
r = client.get("/api/k8s/namespaces", headers=H_OP1)
nss = r.json()
check("C3 日志/失败/namespaces 同口径", logs == ["log-a"] and fails == ["ns-a"]
      and nss == ["ns-a", "ns-b"], f"{logs} {fails} {nss}")

# C4 无授权用户看已收口集群=不可见；只读集群可见；admin 直通
r = client.get("/api/k8s/clusters", headers=H_VIEW)
vis_names = sorted(c["name"] for c in r.json())
check("C4 已收口集群对无授权用户隐藏", vis_names == ["clu-del", "clu-ro"], str(vis_names))
check("C4b admin 直通",
      res_names(H_ADMIN, "Pod") == ["p-a1", "p-a2", "p-b1", "p-b2"], str(res_names(H_ADMIN, "Pod")))
check("C4c viewer 无授权只见只读集群数据",
      res_names(H_VIEW, "Pod") == ["p-a2", "p-b2"], str(res_names(H_VIEW, "Pod")))

# C5 操作面：use 授权放行（落到"未登记 SSH"=过门），无授权 403，view-only 403
client.post("/api/org/namespace-grants", headers=H_ADMIN,   # view → use（覆盖式）
            json={"subject_type": "dept", "subject_id": d_plat["id"],
                  "cluster_id": clu_rw.id, "namespace": "ns-a", "role": "use"})
r = client.post(f"/api/k8mgmt/resources/Pod/p-a1/evict", headers=H_OP1,
                params={"namespace": "ns-a"},
                json={"confirm_name": "p-a1", "cluster_id": clu_rw.id})
check("C5 use 授权放行（停在 SSH 400，未执行任何变更）",
      r.status_code == 400 and "无权" not in r.text, f"{r.status_code} {r.text[:150]}")
r = client.post(f"/api/k8mgmt/resources/Pod/p-b1/evict", headers=H_OP1,
                params={"namespace": "ns-b"},
                json={"confirm_name": "p-b1", "cluster_id": clu_rw.id})
check("C5b 无授权命名空间操作 403", r.status_code == 403 and "无权" in r.text,
      f"{r.status_code} {r.text[:150]}")
r = client.post(f"/api/k8mgmt/resources/Pod/p-a1/evict", headers=H_VIEW,
                params={"namespace": "ns-a"},
                json={"confirm_name": "p-a1", "cluster_id": clu_rw.id})
check("C5c viewer 永不操作", r.status_code == 403, f"{r.status_code} {r.text[:100]}")

# C6 高危分级：Secret 揭示需 admin 授权（use 不够）
r = client.post(f"/api/k8mgmt/resources/Secret/s1/reveal", headers=H_OP1,
                params={"namespace": "ns-a"},
                json={"confirm_name": "s1", "cluster_id": clu_rw.id})
check("C6 use 不够揭示 Secret", r.status_code == 403 and "无权" in r.text,
      f"{r.status_code} {r.text[:150]}")
client.post("/api/org/namespace-grants", headers=H_ADMIN,
            json={"subject_type": "user", "subject_id": uid_op1,
                  "cluster_id": clu_rw.id, "namespace": "ns-a", "role": "admin"})
r = client.post(f"/api/k8mgmt/resources/Secret/s1/reveal", headers=H_OP1,
                params={"namespace": "ns-a"},
                json={"confirm_name": "s1", "cluster_id": clu_rw.id})
check("C6b admin 授权过门（停在 SSH 400）",
      r.status_code == 400 and "无权" not in r.text, f"{r.status_code} {r.text[:150]}")

# C7 只读集群不受授权影响：clu_ro 无授权也过 ns 门（变更仍由只读硬门/SSH 拦）
r = client.post(f"/api/k8mgmt/resources/Pod/p-a2/evict", headers=H_OP2,
                params={"namespace": "ns-a"},
                json={"confirm_name": "p-a2", "cluster_id": clu_ro.id})
check("C7 只读集群不受授权影响（非授权 403）",
      r.status_code != 403 or "无权" not in r.text, f"{r.status_code} {r.text[:150]}")

# C8 查询面：已收口集群对无授权用户过滤；只读集群不过滤
r = client.get("/api/k8mgmt/resources", headers=H_OP2,
               params={"kind": "Pod", "cluster_id": clu_rw.id})
n_rw = len(r.json().get("items", []))
r = client.get("/api/k8mgmt/resources", headers=H_OP2,
               params={"kind": "Pod", "cluster_id": clu_ro.id})
n_ro = sorted(x["name"] for x in r.json().get("items", []))
check("C8 已收口集群过滤/只读集群不过滤",
      n_rw == 0 and n_ro == ["p-a2", "p-b2"], f"{n_rw} {n_ro}")

# C9 聚合面同口径（overview/stats 按授权过滤；op1 可见 p-a1 + clu_ro 两行 = 3 Pod）
r = client.get("/api/k8s/overview", headers=H_OP1)
ov = r.json()
r = client.get("/api/k8mgmt/stats", headers=H_OP1)
st = r.json()
r = client.get("/api/k8mgmt/overview", headers=H_OP1)
mv = r.json()
check("C9 聚合面按授权过滤",
      r.status_code == 200 and ov.get("resources", {}).get("Pod") == 3
      and st.get("pod") == 3 and st.get("node") == 0 and mv.get("pods") == 3,
      f"{ov.get('resources')} {st} {mv.get('pods')}")
r = client.get("/api/k8s/overview", headers=H_ADMIN)
check("C9b admin 聚合不过滤", r.json().get("resources", {}).get("Pod") == 4,
      str(r.json().get("resources")))


# ================= D 用户级叠加/向后兼容 =================
print("D 向后兼容（旧策略零变化）")
uid_view = db.query(User).filter(User.username == "view1").first().id
client.post("/api/auth/permissions/manage", headers=H_ADMIN,
            json={"user_id": uid_view, "code": "k8s.write", "effect": "allow"})
client.post("/api/auth/permissions/manage", headers=H_ADMIN,
            json={"user_id": uid_view, "code": "agent.query", "effect": "deny"})
got = set(eff_of(uid_view))
old_formula = (set(ROLE_DEFAULTS["viewer"]) | {"k8s.write"}) - {"agent.query"}
check("D1 无部门用户=旧公式逐位一致", got == old_formula and got == {"k8s.write"},
      f"{sorted(got)} vs {sorted(old_formula)}")

# F 旧签名（两参）与历史公式一致
check("F1 旧签名向后兼容",
      effective_codes("operator", [("k8s.write", "deny")])
      == sorted((set(ROLE_DEFAULTS["operator"]) - {"k8s.write"}) & {
          "k8s.write", "k8s.dangerous", "machines.ssh", "agent.query", "agent.change"})
      and effective_codes("admin", []) == sorted(ROLE_DEFAULTS["admin"]),
      str(effective_codes("operator", [("k8s.write", "deny")])))
check("F2 部门维度优先级公式",
      effective_codes("operator", [("machines.ssh", "allow"), ("k8s.write", "deny")],
                      [("k8s.write", "allow"), ("k8s.dangerous", "allow"),
                       ("machines.ssh", "deny"), ("agent.query", "deny")])
      == ["k8s.dangerous", "machines.ssh"],
      str(effective_codes("operator",
                          [("machines.ssh", "allow"), ("k8s.write", "deny")],
                          [("k8s.write", "allow"), ("k8s.dangerous", "allow"),
                           ("machines.ssh", "deny"), ("agent.query", "deny")])))


# ================= E 迁移幂等 =================
print("E 迁移幂等（旧形 users 表补列）")
from sqlalchemy import create_engine, text  # noqa: E402

mig_dir = tempfile.mkdtemp(prefix="qianshu-mig-")
mig_engine = create_engine(test_database_url("orgmig"))
with mig_engine.begin() as conn:
    conn.execute(text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64),"
        " password_hash VARCHAR(128), display_name VARCHAR(64), role VARCHAR(16),"
        " is_active BOOLEAN, must_change_password BOOLEAN, otp_secret VARCHAR(64),"
        " otp_enabled BOOLEAN, last_login_at TIMESTAMP)"))
    conn.execute(text(
        "INSERT INTO users (id, username, password_hash, display_name, role, is_active,"
        " must_change_password, otp_secret, otp_enabled)"
        " VALUES (1, 'legacy_user', 'x', '', 'admin', true, false, '', false)"))
Base.metadata.create_all(bind=mig_engine)   # 新表照建，旧 users 不动
run_migrations(mig_engine)
cols1 = [c["name"] for c in __import__("sqlalchemy").inspect(mig_engine).get_columns("users")]
with mig_engine.begin() as conn:
    n_rows = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
check("E1 补列成功+存量行保留", "department_id" in cols1 and n_rows == 1, str(cols1))
run_migrations(mig_engine)   # 第二遍
run_migrations(mig_engine)   # 第三遍
cols2 = [c["name"] for c in __import__("sqlalchemy").inspect(mig_engine).get_columns("users")]
check("E2 幂等（重跑不报错不重复）", cols2 == cols1, f"{cols1} vs {cols2}")

print()
print(f"org_authz mock: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    for f in FAIL:
        print(f"  ✗ {f}")
    sys.exit(1)
