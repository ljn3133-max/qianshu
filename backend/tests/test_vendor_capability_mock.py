#!/usr/bin/env python3
"""厂商能力位口径收口 mock 测试（0.3.1）。

背景：能力判断涉及 clouds.py、data_advisor.py、bootstrap.py、onboard.py、multicloud.py
五处消费面；若分别硬编码厂商清单，新增厂商或能力变化就会出现
"账号下拉可建、状态说登记与测试、点测试抛 NotImplementedError"的断层。
本测试锁定唯一事实源 provider.py::VendorCapability 及全部消费出口。

验证项：
  1) 11 家厂商能力矩阵 / 档位推导（含未知厂商最保守）；
  2) `_account_out` 带 capability，前端无需第二份清单；
  3) `/clouds/vendors` 路由存在且 payload 如实（stub probe/sync=False）；
  4) 监测分母排除仅登记/仅探活厂商，保留 Agent（onprem）；
  5) sync_accounts 对不采集厂商返回 skipped 而非 error，且不推进 last_sync_at；
  6) 账单分母只认 billing 厂商（vCenter/占位厂商不适用）。

用法：cd /opt/qianshu/backend && python3 tests/test_vendor_capability_mock.py（退出码 0=全过）
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-vcap-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("vcap")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = ""
os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = ""
os.environ["TENCENTCLOUD_SECRET_ID"] = ""
os.environ["TENCENTCLOUD_SECRET_KEY"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _p in (pathlib.Path(__file__).resolve().parent.parent / "app" / "models").glob("*.py"):
    if _p.stem != "__init__":
        importlib.import_module(f"app.models.{_p.stem}")

from app.api.v1 import clouds as clouds_api  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.services.bootstrap import _billable_accounts_present  # noqa: E402
from app.services.cloud.provider import (  # noqa: E402
    PROVIDERS, REGISTER_ONLY, capabilities_of, get_provider, vendors_payload, vendors_with,
)
from app.services.cloud.sync_job import sync_accounts  # noqa: E402
from app.services.data_advisor import _monitored_account_ids  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  <-- {detail}"))


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # ---- 1) 能力矩阵与档位推导 ----
        expected = {
            "aliyun": ("asset_billing", dict(probe=True, sync=True, billing=True, power=True, metrics=True, needs_ak_sk=True)),
            "tencent": ("asset_billing", dict(probe=True, sync=True, billing=True, power=False, metrics=False, needs_ak_sk=True)),
            "esxi": ("asset_sync", dict(probe=True, sync=True, billing=False, needs_ak_sk=False)),
            "vcenter": ("asset_sync", dict(probe=True, sync=True, billing=False, needs_ak_sk=False)),
            "kvm": ("probe_only", dict(probe=True, sync=False, billing=False, needs_ak_sk=False)),
            "onprem": ("agent", dict(probe=True, sync=False, agent=True, needs_ak_sk=False)),
            "volc": ("register_only", dict(probe=False, sync=False, billing=False)),
            "baidu": ("register_only", dict(probe=False, sync=False)),
            "huawei": ("register_only", dict(probe=False, sync=False)),
            "aws": ("register_only", dict(probe=False, sync=False)),
            "azure": ("register_only", dict(probe=False, sync=False)),
        }
        check("注册表 11 家厂商", len(PROVIDERS) == 11, sorted(PROVIDERS))
        for vendor, (tier, flags) in expected.items():
            cap = capabilities_of(vendor)
            ok = cap.tier == tier and all(getattr(cap, k) is v for k, v in flags.items())
            check(f"能力矩阵 {vendor} -> {tier}", ok, f"tier={cap.tier} flags={cap}")
        unknown = capabilities_of("not-a-vendor")
        check("未知厂商最保守（不可登记）", unknown.register is False and unknown.tier == "register_only")
        check("基类默认=REGISTER_ONLY", get_provider("volc").capability == REGISTER_ONLY)

        # ---- 2) 消费出口：vendors_with / payload / 路由 ----
        check("billing 厂商 = aliyun,tencent", set(vendors_with("billing")) == {"aliyun", "tencent"})
        check("sync 厂商 = 4 家", set(vendors_with("sync")) == {"aliyun", "tencent", "esxi", "vcenter"})
        check("agent 厂商 = onprem", vendors_with("agent") == ("onprem",))
        payload = {x["vendor"]: x for x in vendors_payload()}
        # 裸机/IDC 不是云账号，onprem 不出现在云账号厂商清单
        check("payload 10 项且排除 onprem", len(payload) == 10 and "onprem" not in payload,
              sorted(payload))
        check("payload label_key", payload["aliyun"]["label_key"] == "clouds.vendor.aliyun")
        check("payload stub probe/sync=False",
              payload["volc"]["capabilities"]["probe"] is False
              and payload["volc"]["capabilities"]["sync"] is False)
        check("payload KVM probe_only",
              payload["kvm"]["tier"] == "probe_only"
              and payload["kvm"]["capabilities"]["probe"] is True
              and payload["kvm"]["capabilities"]["sync"] is False)
        check("GET /clouds/vendors 已注册",
              any(getattr(r, "path", "") == "/clouds/vendors" for r in clouds_api.router.routes))

        # ---- 3) 账号输出带能力位 ----
        tencent = CloudAccount(name="tc-test", vendor="tencent", enabled=False)
        vcenter = CloudAccount(name="vc-test", vendor="vcenter", enabled=True)
        stub = CloudAccount(name="volc-test", vendor="volc", enabled=True)
        kvm = CloudAccount(name="kvm-test", vendor="kvm", enabled=True)
        onprem = CloudAccount(name="idc-test", vendor="onprem", enabled=True)
        aliyun = CloudAccount(name="ali-test", vendor="aliyun", enabled=False)
        weird = CloudAccount(name="weird-test", vendor="nope", enabled=True)
        db.add_all([tencent, vcenter, stub, kvm, onprem, aliyun, weird])
        db.commit()
        out = clouds_api._account_out(tencent)
        check("_account_out.capability.tier", out.get("capability", {}).get("tier") == "asset_billing")
        check("_account_out.capability.metrics=False（腾讯走 Prometheus）",
              out["capability"].get("metrics") is False)

        # ---- 4) 监测分母：排除 stub/KVM，保留 onprem ----
        scope = _monitored_account_ids(db)
        check("监测分母排除仅登记/仅探活",
              stub.id not in scope and kvm.id not in scope and weird.id not in scope,
              sorted(scope))
        check("监测分母保留 Agent 自建", onprem.id in scope)
        check("监测分母排除停用账号", aliyun.id not in scope)
        check("监测分母含 0（未归属）", 0 in scope)
        check("监测分母保留可采集云账号", vcenter.id in scope)

        # ---- 5) 同步：不采集厂商 skipped 而非 error ----
        before = stub.last_sync_at
        out_sync = sync_accounts(db, account_ids=[stub.id], force=True)
        row = out_sync["results"][0]
        db.refresh(stub)
        check("stub 同步 skipped", row.get("skipped") is True
              and row.get("skipped_reason") == "vendor_not_supported")
        check("stub 同步不计 error", not out_sync["summary"]["errors"], out_sync["summary"])
        check("stub 同步不推进 last_sync_at", stub.last_sync_at == before)
        check("stub 同步 note 如实", "仅支持登记" in (row.get("note") or ""), row.get("note"))

        # ---- 6) 账单分母只认 billing 厂商 ----
        check("仅占位厂商时无计费账号", _billable_accounts_present(db) is False)
        tencent.enabled = True
        db.commit()
        check("腾讯账号登场后计费面适用", _billable_accounts_present(db) is True)

    finally:
        db.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
