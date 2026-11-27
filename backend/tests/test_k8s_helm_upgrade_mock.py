#!/usr/bin/env python3
"""Helm 升级集群身份隔离测试：一次性 PostgreSQL + mock SSH，零真实集群写入。

运行：cd /opt/qianshu/backend && python3 tests/test_k8s_helm_upgrade_mock.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
from contextlib import contextmanager
from types import SimpleNamespace

# 必须先于 app.* import，绝不触碰开发库和真实 MASTER_KEY。
_TMPDIR = tempfile.TemporaryDirectory(prefix="qianshu-k8s-helm-upgrade-mock-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("helm-upgrade")
os.environ["MASTER_KEY"] = "ab" * 32
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.api.v1 import k8mgmt as KM  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.k8s import K8sCluster  # noqa: E402

Base.metadata.create_all(engine)
USER = SimpleNamespace(id=1001, username="mock-admin", role="admin", department_id=None)
PASS, FAIL = [], []


class MonkeyPatch:
    def __init__(self):
        self.original = []

    def setattr(self, obj, name, value):
        self.original.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self.original):
            setattr(obj, name, value)


@contextmanager
def expect_http(match):
    try:
        yield
    except HTTPException as e:
        assert e.status_code == 400, f"预期 400，实际 {e.status_code}"
        assert re.search(match, str(e.detail)), f"错误文案不匹配: {e.detail}"
    else:
        raise AssertionError(f"预期 HTTPException/{match}，实际却放行")


def seed_clusters():
    with SessionLocal() as db:
        db.query(K8sCluster).delete()
        db.add_all([
            K8sCluster(id=1, name="cluster-a", api_url="https://a.mock.invalid",
                       ssh_host="a.mock.invalid", ssh_user="root", read_only=False),
            K8sCluster(id=2, name="cluster-b", api_url="https://b.mock.invalid",
                       ssh_host="b.mock.invalid", ssh_user="root", read_only=False),
        ])
        db.commit()


def body(cluster_id=0, cluster_name=""):
    return KM.HelmOpIn(confirm_name="demo-release", chart_ref="repo/demo",
                       cluster_id=cluster_id, cluster_name=cluster_name)


def test_upgrade_targets_selected_cluster_b(monkeypatch):
    import app.collector.ssh as ssh

    calls = []

    def fake_ssh(host, user, command, **kwargs):
        calls.append({"host": host, "user": user, "command": command})
        return {"ok": True, "lines": ["release upgraded"]}

    monkeypatch.setattr(ssh, "run_ssh", fake_ssh)
    with SessionLocal() as db:
        out = KM.helm_upgrade(name="demo-release", body=body(cluster_id=2),
                              namespace="default", db=db, user=USER)
    assert out["ok"] is True
    assert len(calls) == 1, calls
    assert calls[0]["host"] == "b.mock.invalid", calls
    assert calls[0]["user"] == "root", calls
    assert "helm upgrade demo-release repo/demo -n default" in calls[0]["command"]


def test_upgrade_without_cluster_identity_fails_before_guards_and_ssh(monkeypatch):
    import app.collector.ssh as ssh

    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("缺少集群身份时不得进入权限解析、SSH 解析或执行")

    monkeypatch.setattr(KM, "_ns_guard", forbidden)
    monkeypatch.setattr(KM, "_admin_ssh", forbidden)
    monkeypatch.setattr(ssh, "run_ssh", forbidden)
    with SessionLocal() as db:
        with expect_http("目标集群"):
            KM.helm_upgrade(name="demo-release", body=body(),
                            namespace="default", db=db, user=USER)
    assert calls == [], calls


def run(name, fn):
    monkeypatch = MonkeyPatch()
    try:
        seed_clusters()
        fn(monkeypatch)
    except Exception:
        FAIL.append(name)
        print(f"[FAIL] {name}")
        traceback.print_exc()
    else:
        PASS.append(name)
        print(f"[ok] {name}")
    finally:
        monkeypatch.undo()


def main():
    run("selected cluster B is the only SSH target", test_upgrade_targets_selected_cluster_b)
    run("missing cluster identity fails closed", test_upgrade_without_cluster_identity_fails_before_guards_and_ssh)
    print(f"\nK8s Helm upgrade mock: {len(PASS)} ok / {len(FAIL)} fail")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
