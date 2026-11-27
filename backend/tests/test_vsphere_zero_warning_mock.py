#!/usr/bin/env python3
"""VMware 零样本警告是平台按采集结果自动生成的（回归测试）。

警告必须由采集结果自动生成，不能依赖人工写入。
本测试不连 vCenter、不动真实账号，直接驱动平台的采集状态生成函数，验证：

- 本轮 0 点 + 三档探测全空 -> 自动产生 warnings，account_details 带探测结果；
- 本轮 0 点 + 账号异常     -> 自动产生 errors + warnings；
- 本轮有数据               -> warnings 自动为空（前端警告随之消失）。

运行：cd /opt/qianshu/backend && python3 tests/test_vsphere_zero_warning_mock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collector import metrics_job  # noqa: E402
from app.core import security  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


class Acc:
    id = 1
    name = "fake-vcenter"
    ak_cipher = "x"
    secret_cipher = "y"


class FakeQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return [Acc()]


class FakeDB:
    dirty = False

    def query(self, model):
        return FakeQuery()


def run_case(lines, diag, write_ok=True, raise_exc=None):
    """用平台真实的 _run_vsphere_locked 跑一轮，返回状态。"""
    orig = {k: getattr(metrics_job, k, None) for k in ("write_prometheus",)}
    saved_status = dict(metrics_job._vsphere_status)
    saved_secret = security.decrypt_secret
    try:
        security.decrypt_secret = lambda v: "fake"
        # 本用例只验证零样本告警归因，不验证分级调度；绕过 collect_due 的账号表查询。
        import app.services.monitor_policy as mp
        saved_policy = {k: getattr(mp, k) for k in ("collect_due", "mark_collected")}
        mp.collect_due = lambda *a, **k: True
        mp.mark_collected = lambda *a, **k: None

        def fake_collect(*a, **k):
            if raise_exc:
                raise raise_exc
            return lines

        import app.services.vsphere as vs
        saved = {k: getattr(vs, k) for k in
                 ("collect_metrics_lines", "get_last_perf_diag", "reset_perf_diag")}
        vs.collect_metrics_lines = fake_collect
        vs.get_last_perf_diag = lambda: diag
        vs.reset_perf_diag = lambda: None
        metrics_job.write_prometheus = lambda ls: write_ok
        try:
            return metrics_job._run_vsphere_locked(FakeDB(), "vmware_vm")
        finally:
            for k, v in saved.items():
                setattr(vs, k, v)
    finally:
        for k, v in saved_policy.items():
            setattr(mp, k, v)
        metrics_job.write_prometheus = orig["write_prometheus"]
        security.decrypt_secret = saved_secret
        metrics_job._vsphere_status.update(saved_status)


def main():
    dead = {"stats_level": 1, "interval_enabled": True,
            "probes": {"20": 0, "300": 0, "1800": 0}}

    st = run_case([], dead)
    check("Z1 零样本自动产生警告", bool(st.get("warnings")), str(st))
    check("Z2 账号明细记录 0 点与三档探测", (st.get("account_details") or [{}])[0].get("points") == 0
          and (st.get("account_details") or [{}])[0].get("perf_probes") == dead["probes"],
          str(st.get("account_details")))
    check("Z3 警告含 vSphere Client 对照指引",
          any("vSphere Client" in w for w in st.get("warnings", [])), str(st.get("warnings")))

    st = run_case([], {}, raise_exc=RuntimeError("cannot connect"))
    check("Z4 账号异常自动产生 errors", bool(st.get("errors")), str(st))
    check("Z5 账号异常同时产生警告", bool(st.get("warnings")), str(st))

    ok_lines = ['vmware_vm_cpu_usage_percent{instance_id="vm-1"} 1.5']
    st = run_case(ok_lines, dead)
    check("Z6 有数据时警告自动为空", not st.get("warnings"), str(st))
    check("Z7 有数据时点数正确", st.get("points_written") == 1, str(st))

    st = run_case(ok_lines, dead, write_ok=False)
    check("Z8 写库失败按账号异常处理", bool(st.get("errors")) and bool(st.get("warnings")), str(st))
    check("Z9 写库失败不计入已写入点数", st.get("points_written") == 0, str(st))

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\nmock 测试: {passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
