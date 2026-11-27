#!/usr/bin/env python3
"""K8s 升级预检 fail-closed 隔离测试：一次性 PostgreSQL + 全 mock，零集群/SSH/kubeadm。

运行：cd /opt/qianshu/backend && python3 tests/test_k8s_upgrade_precheck_mock.py
仅使用标准库测试壳，不依赖 pytest，不导入 app.main、不启动服务。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

# 必须先于 app.* import，绝不触碰开发库和真实 MASTER_KEY。
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-k8s-precheck-mock-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("precheck")
os.environ["MASTER_KEY"] = "ab" * 32
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from app.api.v1 import k8mgmt as KM  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.k8s_task import K8sTask  # noqa: E402

Base.metadata.create_all(engine)
CLUSTER = SimpleNamespace(id=7, name="mock-cluster", read_only=False)
USER = SimpleNamespace(id=1001, username="mock-operator")
KUBE_PROXY_CM = "/api/v1/namespaces/kube-system/configmaps/kube-proxy"
PASS, FAIL = [], []


class MonkeyPatch:
    """最小 monkeypatch：每个用例结束恢复，防止 mock 泄漏。"""

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
        assert re.search(match, str(e.detail)), f"错误文案不匹配: {e.detail}"
    else:
        raise AssertionError(f"预期 HTTPException/{match}，实际却放行")


def _responses(**overrides):
    base = {
        "/version": {"gitVersion": "v1.29.4"},
        "/apis": {"groups": []},
        KUBE_PROXY_CM: {"data": {"config.conf": "mode: iptables\n"}},
    }
    base.update(overrides)
    return base


def _install_k8s_json(monkeypatch, responses):
    def fake(api_url, token, path, verify_tls=True, params=None):
        value = responses.get(path, RuntimeError(f"mock unreachable: {path}"))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(KM, "_k8s_json", fake)


def _precheck(db, monkeypatch, responses=None, target="1.29"):
    _install_k8s_json(monkeypatch, _responses() if responses is None else responses)
    return KM.upgrade_precheck(target=target, cluster_id=CLUSTER.id, db=db, user=USER)


def _body(target="1.29.5"):
    return KM.UpgradeIn(cluster_id=CLUSTER.id, cluster_name=CLUSTER.name,
                        target_version=target, confirm_name=CLUSTER.name)


def _seed_record(db, **changes):
    evidence = {"target": "1.29", "current_version": "1.29.4",
                "completed": True, "passed": True,
                "kube_proxy": {"mode": "iptables", "compatible": True}}
    evidence.update(changes.pop("evidence", {}))
    values = dict(task_type="upgrade_precheck", title="mock precheck",
                  cluster_id=CLUSTER.id, cluster_name=CLUSTER.name,
                  status="succeeded", meta_json=json.dumps(evidence), steps_json="[]",
                  created_at=datetime.now(), finished_at=datetime.now(),
                  submitted_by=USER.username, submitted_by_id=USER.id)
    values.update(changes)
    row = K8sTask(**values)
    db.add(row)
    db.commit()
    return row


def _isolate(monkeypatch):
    with SessionLocal() as db:
        db.query(K8sTask).delete()
        db.commit()
    monkeypatch.setattr(KM, "_cluster_api",
                        lambda *a, **k: ("https://mock.invalid", "mock-token", True, CLUSTER))
    monkeypatch.setattr(KM, "_admin_ssh", lambda *a, **k: ("mock.invalid", "root", CLUSTER))

    import app.collector.ssh as ssh
    import app.services.task_engine as task_engine

    def no_ssh(*a, **k):
        raise AssertionError("测试守卫：不允许意外执行 SSH")

    def no_submit(*a, **k):
        raise AssertionError("测试守卫：不允许意外提交后台任务")

    monkeypatch.setattr(ssh, "run_ssh", no_ssh)
    monkeypatch.setattr(task_engine, "submit", no_submit)


def test_unknown_current_version_has_no_upgrade_path(monkeypatch):
    import app.collector.k8s as collector

    monkeypatch.setattr(collector, "ping", lambda *a: {"ok": False, "detail": "mock unreachable"})
    with SessionLocal() as db:
        out = KM.upgrade_versions(cluster_id=CLUSTER.id, db=db, user=USER)
    assert out["current"] == ""
    assert out["upgrade_path"] == []
    assert "无法生成" in out["note"]


def test_known_current_version_still_has_upgrade_path(monkeypatch):
    import app.collector.k8s as collector

    monkeypatch.setattr(collector, "ping", lambda *a: {"ok": True, "detail": "K8s v1.28.7"})
    with SessionLocal() as db:
        out = KM.upgrade_versions(cluster_id=CLUSTER.id, db=db, user=USER)
    assert out["current"] == "v1.28.7"
    assert out["upgrade_path"] == ["1.29.x", "1.30.x", "1.31.x"]


def test_unreachable_cluster_is_incomplete_not_passed(monkeypatch):
    _install_k8s_json(monkeypatch, {})
    with SessionLocal() as db:
        out = KM.upgrade_precheck(target="1.29", cluster_id=CLUSTER.id, db=db, user=USER)
        row = db.query(K8sTask).one()
    assert out["passed"] is False
    assert out["completed"] is False
    assert out["current_version"] == ""
    assert out["kube_proxy"]["mode"] == ""
    assert out["kube_proxy"]["compatible"] is None
    assert any("当前版本未知" in x for x in out["incomplete_reasons"])
    assert any("废弃 API 扫描失败" in x for x in out["incomplete_reasons"])
    assert row.status == "failed"


def test_unknown_git_version_is_incomplete_not_passed(monkeypatch):
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, _responses(**{"/version": {"gitVersion": ""}}))
    assert out["passed"] is False
    assert out["completed"] is False
    assert any("当前版本未知" in x for x in out["incomplete_reasons"])


def test_discovery_failure_is_not_empty_scan(monkeypatch):
    responses = _responses(**{"/apis": RuntimeError("HTTP 503")})
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, responses)
    assert out["passed"] is False
    assert out["completed"] is False
    assert any("废弃 API 扫描失败" in x and "HTTP 503" in x
               for x in out["incomplete_reasons"])


def test_resource_scan_failure_is_not_zero_instances(monkeypatch):
    responses = _responses(**{
        "/apis": {"groups": [{"versions": [{"groupVersion": "policy/v1beta1"}]}]},
        "/apis/policy/v1beta1/podsecuritypolicies": RuntimeError("HTTP 403"),
    })
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, responses)
    assert out["passed"] is False
    assert out["completed"] is False
    assert any("HTTP 403" in x for x in out["incomplete_reasons"])


def test_malformed_resource_list_is_not_empty_scan(monkeypatch):
    responses = _responses(**{
        "/apis": {"groups": [{"versions": [{"groupVersion": "policy/v1beta1"}]}]},
        "/apis/policy/v1beta1/podsecuritypolicies": {},
    })
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, responses)
    assert out["passed"] is False
    assert out["completed"] is False
    assert any("缺少 items 列表" in x for x in out["incomplete_reasons"])


def test_target_without_compatibility_rules_cannot_pass(monkeypatch):
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, target="1.32")
    assert out["passed"] is False
    assert out["completed"] is False
    assert out["kube_proxy"]["compatible"] is None
    assert any("缺少废弃 API 兼容性规则" in x for x in out["incomplete_reasons"])
    assert any("缺少 kube-proxy 兼容性规则" in x for x in out["incomplete_reasons"])


def test_unprobed_kube_proxy_compatibility_blocks_pass(monkeypatch):
    responses = _responses(**{KUBE_PROXY_CM: {"data": {"config.conf": "mode: ''\n"}}})
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, responses)
    assert out["passed"] is False
    assert out["completed"] is False
    assert out["kube_proxy"]["mode"] == ""
    assert out["kube_proxy"]["compatible"] is None
    assert any("kube-proxy" in x for x in out["incomplete_reasons"])


def test_complete_evidence_passes_and_persists_record(monkeypatch):
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch)
        row = db.query(K8sTask).one()
        evidence = json.loads(row.meta_json)
    assert out["passed"] is True
    assert out["completed"] is True
    assert out["incomplete_reasons"] == []
    assert out["current_version"] == "1.29.4"
    assert out["kube_proxy"]["mode"] == "iptables"
    assert out["kube_proxy"]["compatible"] is True
    assert row.cluster_id == CLUSTER.id
    assert row.status == "succeeded"
    assert evidence["current_version"] == "1.29.4"


def test_real_deprecated_instances_block_completed_precheck(monkeypatch):
    responses = _responses(**{
        "/apis": {"groups": [{"versions": [{"groupVersion": "policy/v1beta1"}]}]},
        "/apis/policy/v1beta1/podsecuritypolicies": {
            "items": [{"metadata": {"namespace": "", "name": "legacy-psp"}}]},
    })
    with SessionLocal() as db:
        out = _precheck(db, monkeypatch, responses)
    assert out["completed"] is True
    assert out["passed"] is False
    assert out["findings"][0]["instances"] == ["/legacy-psp"]


def test_start_rejects_missing_precheck_before_submit(monkeypatch):
    import app.collector.ssh as ssh

    monkeypatch.setattr(ssh, "run_ssh", lambda *a, **k: {"ok": True, "lines": ["v1.29.4"]})
    with SessionLocal() as db, expect_http("有效通过预检"):
        KM.start_upgrade(body=_body(), request=None, db=db, user=USER)


def test_start_accepts_matching_fresh_passed_record(monkeypatch):
    import app.collector.ssh as ssh
    import app.services.task_engine as task_engine

    monkeypatch.setattr(ssh, "run_ssh", lambda *a, **k: {"ok": True, "lines": ["v1.29.4"]})
    monkeypatch.setattr(task_engine, "submit",
                        lambda *a, **k: SimpleNamespace(id=321, status="pending"))
    with SessionLocal() as db:
        assert _precheck(db, monkeypatch)["passed"] is True
        out = KM.start_upgrade(body=_body(), request=None, db=db, user=USER)
    assert out == {"task_id": 321}


def test_start_binds_execution_to_registered_ssh(monkeypatch):
    import app.collector.ssh as ssh
    import app.services.task_engine as task_engine

    ssh_targets = []
    submitted = {}

    def fake_ssh(host, *args, **kwargs):
        ssh_targets.append(host)
        return {"ok": True, "lines": ["v1.29.4"]}

    def fake_submit(*args, **kwargs):
        submitted["meta"] = args[3]
        return SimpleNamespace(id=322, status="pending")

    monkeypatch.setattr(ssh, "run_ssh", fake_ssh)
    monkeypatch.setattr(task_engine, "submit", fake_submit)
    body = _body()
    body.master_ssh = "other.invalid"
    body.master_user = "other"
    with SessionLocal() as db:
        _seed_record(db)
        out = KM.start_upgrade(body=body, request=None, db=db, user=USER)
    assert out == {"task_id": 322}
    assert ssh_targets == ["mock.invalid"]
    assert submitted["meta"]["master_ssh"] == "mock.invalid"
    assert submitted["meta"]["master_user"] == "root"
    assert submitted["meta"]["cluster_name"] == CLUSTER.name
    assert submitted["meta"]["cluster_id"] == CLUSTER.id


def test_precheck_record_binding_and_validity(monkeypatch, changes, match):
    with SessionLocal() as db:
        _seed_record(db, **changes)
        with expect_http(match):
            KM._require_upgrade_precheck(db, CLUSTER, "1.29.5", "1.29.4")


def test_start_rejects_unknown_kubeadm_version(monkeypatch):
    import app.collector.ssh as ssh

    monkeypatch.setattr(ssh, "run_ssh", lambda *a, **k: {"ok": False, "lines": []})
    with SessionLocal() as db:
        _seed_record(db)
        with expect_http("当前版本未知"):
            KM.start_upgrade(body=_body(), request=None, db=db, user=USER)


def run(name, fn, *args):
    monkeypatch = MonkeyPatch()
    try:
        _isolate(monkeypatch)
        fn(monkeypatch, *args)
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
    run("unknown current -> empty upgrade path", test_unknown_current_version_has_no_upgrade_path)
    run("known current -> real upgrade path", test_known_current_version_still_has_upgrade_path)
    run("unreachable -> incomplete/failed", test_unreachable_cluster_is_incomplete_not_passed)
    run("unknown gitVersion -> incomplete/failed", test_unknown_git_version_is_incomplete_not_passed)
    run("discovery failure -> not empty scan", test_discovery_failure_is_not_empty_scan)
    run("resource scan failure -> not zero instances", test_resource_scan_failure_is_not_zero_instances)
    run("malformed resource list -> not empty scan", test_malformed_resource_list_is_not_empty_scan)
    run("target without rules -> failed", test_target_without_compatibility_rules_cannot_pass)
    run("kube-proxy unprobed -> failed", test_unprobed_kube_proxy_compatibility_blocks_pass)
    run("complete evidence -> passed + persisted", test_complete_evidence_passes_and_persists_record)
    run("deprecated instances -> blocked", test_real_deprecated_instances_block_completed_precheck)
    run("start without precheck -> rejected", test_start_rejects_missing_precheck_before_submit)
    run("matching fresh precheck -> start accepted (mock submit)",
        test_start_accepts_matching_fresh_passed_record)
    run("execution bound to registered SSH target", test_start_binds_execution_to_registered_ssh)
    validity_cases = [
        ("different cluster", {"cluster_id": 999}, "有效通过预检"),
        ("different target", {"evidence": {"target": "1.30"}}, "有效通过预检"),
        ("current version changed", {"evidence": {"current_version": "1.29.3"}}, "当前版本.*不一致"),
        ("expired precheck", {"created_at": datetime.now() - timedelta(minutes=31)}, "超过 30 分钟"),
        ("failed precheck", {"status": "failed", "evidence": {"passed": False}}, "未完整通过"),
        ("incomplete precheck", {"evidence": {"completed": False}}, "未完整通过"),
    ]
    for name, changes, match in validity_cases:
        run(name, test_precheck_record_binding_and_validity, changes, match)
    run("unknown kubeadm current -> start rejected", test_start_rejects_unknown_kubeadm_version)
    print(f"\nK8s upgrade precheck mock: {len(PASS)} ok / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
