#!/usr/bin/env python3
"""vSphere QueryPerf 结果兼容性回归测试。

背景：pyvmomi 8.0（vSphere 8 WSDL）把 PerfEntityMetric 的序列字段从
series 改名为 value；旧代码只读 series，导致连接正常、vCenter 有数据时
仍采集到 0 个点。本测试用假对象覆盖两种字段名、-1 无数据哨兵、以及
"磁盘只在 5 分钟档"的双档合并逻辑，不连真实 vCenter。

运行：cd /opt/qianshu/backend && python3 tests/test_vsphere_perf_compat_mock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.vsphere import (fetch_perf, _latest_valid,  # noqa: E402
                                  _probe_intervals, _result_series)
from pyVmomi import vim  # noqa: E402

RESULTS = []

# 测试不连 vCenter：用普通对象替换 pyVmomi 的强类型 QuerySpec/MetricId，
# 否则 entity 必须是真实 ManagedObject。
class _PlainData:
    def __init__(self, **kw):
        self.__dict__.update(kw)


vim.PerformanceManager.QuerySpec = _PlainData
vim.PerformanceManager.MetricId = _PlainData


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Series:
    def __init__(self, counter_id, values, instance=""):
        self.id = Obj(counterId=counter_id, instance=instance)
        self.value = values


class LegacyResult:
    """旧 pyvmomi：序列在 series。"""
    def __init__(self, mo_id, series):
        self.entity = Obj(_moId=mo_id)
        self.series = series
        self.sampleInfo = [Obj(timestamp=None, interval=300)] * 3


class NewResult:
    """pyvmomi 8：序列在 value（实测生产环境形态）。"""
    def __init__(self, mo_id, series):
        self.entity = Obj(_moId=mo_id)
        self.value = series
        self.sampleInfo = [Obj(timestamp=None, interval=300)] * 3


class FakePM:
    def __init__(self):
        self.perfCounter = [
            Obj(key=2, groupInfo=Obj(key="cpu"), nameInfo=Obj(key="usage"),
                rollupType="average", unitInfo=Obj(key="percent")),
            Obj(key=24, groupInfo=Obj(key="mem"), nameInfo=Obj(key="usage"),
                rollupType="average", unitInfo=Obj(key="percent")),
            Obj(key=266, groupInfo=Obj(key="disk"), nameInfo=Obj(key="used"),
                rollupType="latest", unitInfo=Obj(key="kiloBytes")),
        ]
        self.historicalInterval = []

    def QueryPerf(self, querySpec):
        spec = querySpec[0]
        mo_id = spec.entity._moId
        counter_ids = {m.counterId for m in (spec.metricId or [])}
        if spec.intervalId == 20:
            # 实时档：cpu 有效，mem 为 -1（无数据）
            return [NewResult(mo_id, [
                Series(2, [1500, 1700]),
                Series(24, [-1]),
            ])]
        if spec.intervalId == 300 and 266 in counter_ids:
            # 历史档：磁盘按实例多序列；-1 必须忽略
            return [LegacyResult(mo_id, [
                Series(266, [1000], instance="DISKFILE"),
                Series(266, [500], instance="OTHERFILE"),
                Series(266, [-1], instance="SWAPFILE"),
            ])]
        return []


class FakeContent:
    def __init__(self):
        self.perfManager = FakePM()


class FakeContentWith(FakeContent):
    def __init__(self, pm):
        self.perfManager = pm


def main():
    # 字段兼容：新旧两种结果都能取到序列
    old = LegacyResult("vm-1", [Series(2, [100])])
    new = NewResult("vm-1", [Series(2, [100])])
    check("R1 旧字段 series 可读", len(_result_series(old)) == 1)
    check("R2 pyvmomi8 字段 value 可读", len(_result_series(new)) == 1)

    # -1 哨兵：向前找最后一个有效值
    check("R3 -1 无数据返回 None", _latest_valid(Series(2, [-1])) is None)
    check("R4 末尾 -1 时回退到有效值", _latest_valid(Series(2, [1200, -1])) == 1200.0)
    check("R5 空序列返回 None", _latest_valid(Series(2, [])) is None)

    # 双档合并：cpu/mem 来自 20 秒档，磁盘来自 300 秒档
    vm = Obj(_moId="vm-1")
    out = fetch_perf(FakeContent(), [("vm-1", vm)])
    rec = out.get("vm-1") or {}
    check("F1 实时档 cpu 百分比正确", rec.get("cpu_usage_pct") == 17.0, str(rec))
    check("F2 -1 内存不写成 0/-0.01", rec.get("mem_usage_pct") is None, str(rec))
    check("F3 磁盘无聚合值时按分项求和并忽略 -1", rec.get("disk_used_kb") == 1500.0, str(rec))

    # 有 instance="" 聚合值时直接用聚合值，不能再加各文件类型分项（否则翻倍）
    class AggPM(FakePM):
        def QueryPerf(self, querySpec):
            spec = querySpec[0]
            if spec.intervalId == 300:
                return [LegacyResult("vm-1", [
                    Series(266, [2000], instance=""),
                    Series(266, [1500], instance="DISKFILE"),
                    Series(266, [500], instance="OTHERFILE"),
                ])]
            return super().QueryPerf(querySpec)

    rec2 = (fetch_perf(FakeContentWith(AggPM()), [("vm-1", vm)]) or {}).get("vm-1") or {}
    check("F4 磁盘聚合值不重复累加分项", rec2.get("disk_used_kb") == 2000.0, str(rec2))

    # 探测计数：有有效值的序列才计数
    class ProbePM(FakePM):
        def QueryPerf(self, querySpec):
            spec = querySpec[0]
            if spec.intervalId == 20:
                return [NewResult("vm-1", [Series(2, [1700]), Series(24, [-1])])]
            return [LegacyResult("vm-1", [Series(2, [-1])])]

    probes = _probe_intervals(ProbePM(), [("vm-1", vm)],
                              {"cpu_usage_pct": 2, "mem_usage_pct": 24, "disk_used_kb": 266})
    check("P1 20 秒档计有值序列", probes.get("20") == 1, str(probes))
    check("P2 历史档全 -1 计 0", probes.get("300") == 0 and probes.get("1800") == 0, str(probes))

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\nmock 测试: {passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
