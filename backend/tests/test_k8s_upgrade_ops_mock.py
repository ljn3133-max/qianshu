#!/usr/bin/env python3
"""K8s 受限升级执行体隔离测试：全 mock，零真实集群/SSH/kubeadm/数据库事实。

运行：cd /opt/qianshu/backend && python3 tests/test_k8s_upgrade_ops_mock.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from unittest.mock import patch

# 仅防止导入 app.core.db 时落到开发库；测试不会执行真实 SQL。
_TMPDIR = tempfile.TemporaryDirectory(prefix="qianshu-k8s-upgrade-ops-mock-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("upgrade")
os.environ["MASTER_KEY"] = "ab" * 32
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import k8s_ops as OPS  # noqa: E402

INVENTORY_CMD = "kubectl get nodes --no-headers"
SINGLE_CP = ["lab-cp   Ready   control-plane   10d   v1.29.4"]
PASS, FAIL = [], []


class FakeSession:
    """登记阶段空查询替身；不连接数据库、不写入任何记录。"""

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return None

    def commit(self):
        raise AssertionError("空查询不应提交登记变更")


def exercise(nodes=None, node_ok=True, fail_on=""):
    commands, backup_calls, step_snapshots = [], [], []

    def fake_ssh(host, user, command, **kwargs):
        assert host == "mock.invalid" and user == "root"
        commands.append(command)
        if command == INVENTORY_CMD:
            return {"ok": node_ok, "lines": list(SINGLE_CP if nodes is None else nodes)}
        if fail_on and fail_on in command:
            return {"ok": False, "lines": ["mock failure"]}
        return {"ok": True, "lines": ["mock ok"]}

    def fake_backup(task_id, log, set_steps, meta):
        backup_calls.append(task_id)
        return True, ""

    def set_steps(steps):
        step_snapshots.append([dict(step) for step in steps])

    with patch.object(OPS, "run_ssh", fake_ssh), \
            patch.object(OPS, "run_backup", fake_backup), \
            patch("app.core.db.SessionLocal", return_value=FakeSession()):
        ok, error = OPS.run_upgrade(
            1, lambda line: None, set_steps,
            {"master_ssh": "mock.invalid", "master_user": "root",
             "target_version": "1.29.5", "cluster_name": "mock-lab"},
        )
    return ok, error, commands, backup_calls, step_snapshots


def assert_no_apply(commands):
    assert not any("kubeadm upgrade apply" in command for command in commands), commands


def test_multi_control_plane_and_worker_rejected_before_writes():
    cases = {
        "multi-control-plane": [
            "cp-1   Ready   control-plane   10d   v1.29.4",
            "cp-2   Ready   control-plane   10d   v1.29.4",
        ],
        "control-plane-plus-worker": [
            "cp-1   Ready   control-plane   10d   v1.29.4",
            "worker-1   Ready   <none>   10d   v1.29.4",
        ],
    }
    for name, nodes in cases.items():
        ok, error, commands, backups, _ = exercise(nodes=nodes)
        assert ok is False, name
        assert "当前仅支持单控制面独立实验节点，生产/多节点未开放" in error
        assert commands == [INVENTORY_CMD], (name, commands)
        assert backups == []
        assert_no_apply(commands)


def test_inventory_failure_and_malformed_inventory_fail_closed():
    for nodes, node_ok in ((SINGLE_CP, False), (["unparseable"], True), ([], True)):
        ok, error, commands, backups, _ = exercise(nodes=nodes, node_ok=node_ok)
        assert ok is False
        assert "已终止" in error
        assert commands == [INVENTORY_CMD]
        assert backups == []
        assert_no_apply(commands)


def test_environment_failure_stops_before_apply():
    ok, error, commands, backups, snapshots = exercise(
        fail_on="systemctl is-active kubelet")
    assert ok is False and "环境检查失败" in error
    assert backups == []
    assert not any("kubeadm config images pull" in command for command in commands)
    assert_no_apply(commands)
    last = {step["key"]: step["status"] for step in snapshots[-1]}
    assert last["env"] == "failed" and last["upgrade"] == "pending"


def test_image_pull_failure_stops_before_apply():
    ok, error, commands, backups, snapshots = exercise(
        fail_on="kubeadm config images pull")
    assert ok is False and "镜像预拉失败" in error
    assert backups == [1]
    assert_no_apply(commands)
    assert not any("systemctl restart kubelet" in command for command in commands)
    last = {step["key"]: step["status"] for step in snapshots[-1]}
    assert last["registry"] == "failed" and last["upgrade"] == "pending"


def test_single_control_plane_enters_mock_upgrade():
    ok, error, commands, backups, snapshots = exercise()
    assert ok is True, error
    assert commands[0] == INVENTORY_CMD
    assert any("kubeadm upgrade apply -y v1.29.5" in command for command in commands)
    assert any("kubeadm config images pull" in command for command in commands)
    assert backups == [1]
    last = {step["key"]: step["status"] for step in snapshots[-1]}
    assert last["nodecheck"] == "succeeded" and last["register"] == "succeeded"


def run(name, fn):
    try:
        fn()
    except Exception:
        FAIL.append(name)
        print(f"[FAIL] {name}")
        traceback.print_exc()
    else:
        PASS.append(name)
        print(f"[ok] {name}")


def main():
    run("multi control plane / worker -> read-only rejection",
        test_multi_control_plane_and_worker_rejected_before_writes)
    run("inventory failure / malformed / empty -> fail closed",
        test_inventory_failure_and_malformed_inventory_fail_closed)
    run("environment failure -> no apply", test_environment_failure_stops_before_apply)
    run("image pull failure -> no apply", test_image_pull_failure_stops_before_apply)
    run("single control plane -> full mock path", test_single_control_plane_enters_mock_upgrade)
    print(f"\nK8s upgrade ops mock: {len(PASS)} ok / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
