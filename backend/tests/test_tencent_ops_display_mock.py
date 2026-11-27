#!/usr/bin/env python3
"""腾讯产品 Ops 展示契约回归。

一次性 PostgreSQL + 纯静态前端契约，不访问腾讯云、VM 或本机业务库。
锁定四类产品在 RESOURCE_TYPES、cloud-overview、Ops 页签/深链白名单、
状态筛选、专用详情及中英文案之间不再漂移。
"""
from __future__ import annotations

import importlib
import os
import pathlib
import re
import sys
import tempfile

TMPDIR = tempfile.mkdtemp(prefix="qianshu-tencent-ops-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

BACKEND = pathlib.Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
for path in (BACKEND / "app" / "models").glob("*.py"):
    if path.stem != "__init__":
        importlib.import_module(f"app.models.{path.stem}")

from app.api.v1.assets import cloud_overview  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.services.cloud.provider import RESOURCE_TYPES  # noqa: E402
from app.services.cloud.sync_job import _PRODUCT_LABELS as SYNC_LABELS  # noqa: E402
from app.services.copilot.workflows.onboard import _PRODUCT_LABELS as ONBOARD_LABELS  # noqa: E402
from app.services.expiry import _PRODUCT_LABELS as EXPIRY_LABELS  # noqa: E402
from app.services.rule_advisor import PRODUCT_LABELS as ADVISOR_LABELS  # noqa: E402

PASS, FAIL = [], []
PRODUCTS = ("tc_cdn", "tc_mongodb", "tc_es", "tc_postgres")
EXPECTED_LABELS = {
    "tc_cdn": "腾讯 CDN",
    "tc_mongodb": "腾讯 MongoDB",
    "tc_es": "腾讯 Elasticsearch",
    "tc_postgres": "腾讯 PostgreSQL",
}
EXPECTED_DETAIL_FIELDS = {
    "tc_cdn": {"cname", "service_type", "area", "origin_type", "disable",
               "read_only", "create_time", "update_time"},
    "tc_mongodb": {"engine", "engine_version", "cluster_type", "memory_mb", "storage_mb",
                   "cpu", "machine_type", "secondary_num", "replication_set_num",
                   "connection_string", "port", "net_type", "vpc_id", "subnet_id", "create_time"},
    "tc_es": {"engine", "engine_version", "node_num", "cpu", "memory_gb", "disk_gb",
              "disk_type", "es_domain", "es_vip", "es_port", "license_type", "create_time"},
    "tc_postgres": {"engine", "engine_version", "memory_gb", "storage_gb", "cpu",
                    "connection_string", "port", "vpc_id", "subnet_id",
                    "master_instance_id", "create_time"},
}
EXPECTED_STATUSES = {
    "tc_cdn": {"online", "offline", "processing", "closing", "rejected", "Deleted"},
    "tc_mongodb": {"initializing", "processing", "running", "isolated", "Deleted"},
    "tc_es": {"processing", "running", "stopped", "destroying", "destroyed",
              "isolated", "initializing", "Deleted"},
    # 与已安装 tencentcloud SDK DBInstance.DBInstanceStatus 文档枚举一致。
    "tc_postgres": {
        "applying", "init", "initing", "running", "limited run", "isolating", "isolated",
        "disisolating", "recycling", "recycled", "job running", "offline", "migrating",
        "expanding", "waitSwitch", "switching", "readonly", "restarting", "network changing",
        "upgrading", "audit-switching", "primary-switching", "offlining", "deployment changing",
        "cloning", "parameter modifying", "log-switching", "restoring", "Deleted",
    },
}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  <-- {detail}"))


def object_block(source, name):
    match = re.search(rf"const {name} = \{{(.*?)\n\}}", source, re.S)
    if not match:
        raise AssertionError(f"找不到前端配置 {name}")
    return match.group(1)


def product_array(block, product):
    match = re.search(rf"^  {product}: \[(.*?)\n  \],", block, re.S | re.M)
    if not match:
        raise AssertionError(f"找不到 {product} 配置")
    return match.group(1)


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        account = CloudAccount(name="mock-tencent", vendor="tencent", enabled=True, status="ok")
        db.add(account)
        db.flush()
        for product in PRODUCTS:
            db.add(Asset(account_id=account.id, product=product, instance_id=product + "-1",
                         name=product + "-demo", status="running", extra="{}"))
        db.commit()
        overview = cloud_overview(db)
        actual = {p["product"]: p["label"]
                  for a in overview["accounts"] for p in a["products"]}
        check("cloud-overview 四类资产全部返回", set(actual) == set(PRODUCTS), str(actual))
        for product, label in EXPECTED_LABELS.items():
            check(f"RESOURCE_TYPES/API 名称 {product}", RESOURCE_TYPES[product] == label
                  and actual.get(product) == label, actual.get(product))
        for source_name, labels in (("同步播种", SYNC_LABELS), ("到期总账", EXPIRY_LABELS),
                                    ("规则分析", ADVISOR_LABELS), ("安装向导", ONBOARD_LABELS)):
            check(f"{source_name}复用统一产品名",
                  all(labels.get(p) == EXPECTED_LABELS[p] for p in PRODUCTS), str(labels))
    finally:
        db.close()

    frontend = ROOT / "frontend" / "src"
    ops = (frontend / "views" / "Ops.vue").read_text()
    tab_defs = object_block(ops, "TAB_DEFS")
    statuses = object_block(ops, "STATUS_OPTIONS")
    details = object_block(ops, "EXTRA_LABELS")
    fallback = re.search(r"const FALLBACK_TABS = \[(.*?)\]", ops, re.S).group(1)
    check("动态页签使用 API 返回的统一 label",
          "if (p.label) labels[p.product] = p.label" in ops
          and "labels[name] || tabLabel(name)" in ops)
    check("深链仍以 TAB_DEFS 为已知产品白名单",
          "hasOwnProperty.call(TAB_DEFS, String(route.query.product))" in ops)

    zh_ops = (frontend / "i18n" / "locales" / "zh-CN" / "ops.js").read_text()
    en_ops = (frontend / "i18n" / "locales" / "en-US" / "ops.js").read_text()
    zh_common = (frontend / "i18n" / "locales" / "zh-CN" / "common.js").read_text()
    en_common = (frontend / "i18n" / "locales" / "en-US" / "common.js").read_text()
    formatter = (frontend / "utils" / "format.js").read_text()

    for product, expected_label in EXPECTED_LABELS.items():
        check(f"Ops 正式页签/深链 {product}", re.search(rf"\b{product}:", tab_defs) is not None)
        check(f"Ops 静态兜底包含 {product}", f"'{product}'" in fallback)
        fallback_label = re.search(rf"\b{product}: '([^']+)'", tab_defs)
        check(f"Ops 兜底名称与后端一致 {product}",
              fallback_label is not None and fallback_label.group(1) == expected_label,
              fallback_label.group(1) if fallback_label else "missing")

        status_text = product_array(statuses, product)
        actual_statuses = set(re.findall(r"'([^']+)'", status_text))
        check(f"状态筛选覆盖真实采集状态 {product}",
              EXPECTED_STATUSES[product] <= actual_statuses,
              str(sorted(EXPECTED_STATUSES[product] - actual_statuses)))

        detail_text = product_array(details, product)
        pairs = re.findall(r"\['([^']+)', '([^']+)'\]", detail_text)
        actual_fields = {field for field, _key in pairs}
        check(f"专用详情覆盖采集 extra 字段 {product}",
              EXPECTED_DETAIL_FIELDS[product] <= actual_fields,
              str(sorted(EXPECTED_DETAIL_FIELDS[product] - actual_fields)))
        missing_i18n = [key for _field, key in pairs
                        if f"'{key}':" not in zh_ops or f"'{key}':" not in en_ops]
        check(f"详情字段中英文案完整 {product}", not missing_i18n, str(missing_i18n))

        untranslated = []
        for status in EXPECTED_STATUSES[product]:
            value = status.lower()
            match = re.search(rf"^  (?:'{re.escape(value)}'|{re.escape(value)}): "
                              rf"'(common\.asset_status\.[^']+)'", formatter, re.M)
            if not match or f"'{match.group(1)}':" not in zh_common \
                    or f"'{match.group(1)}':" not in en_common:
                untranslated.append(status)
        check(f"状态值可本地化且不裸奔 {product}", not untranslated, str(untranslated))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
