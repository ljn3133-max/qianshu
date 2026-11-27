#!/usr/bin/env python3
"""资产台账 XLSX 导出 mock 全链路。

产品口径：内嵌页面选资产类型 + 逐类勾列；产品列不要，按产品分 sheet；
ECS 必须可导出磁盘/运行状态/操作系统/VPC/交换机。

隔离：一次性 PostgreSQL + 假 MASTER_KEY + ASGI 直连；不碰生产库、不启动后台任务、不调云 API。
运行：cd /opt/qianshu/backend && python3 tests/test_asset_export_xlsx_mock.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import types
import zipfile
from xml.etree import ElementTree as ET

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-xlsx-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("xlsx")
os.environ["MASTER_KEY"] = "ab" * 32

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import app.core.deps as deps  # noqa: E402
import app.main  # noqa: E402,F401
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402

PASS, FAIL = [], []
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _client(role="admin"):
    import app.main as mainmod

    fake = types.SimpleNamespace(id=7, username="xlsxsmoke", role=role, is_active=True)
    mainmod.app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mainmod.app),
                                         base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    return _req, mainmod.app


def _sheets(content: bytes) -> dict[str, list[list[str]]]:
    """解析我们自产的多 sheet XLSX（inlineStr）。"""
    z = zipfile.ZipFile(io.BytesIO(content))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    names = [s.get("name") for s in wb.find("m:sheets", NS)]
    out = {}
    for i, name in enumerate(names, 1):
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml"))
        rows = []
        for row in root.find("m:sheetData", NS):
            vals = []
            for cell in row:
                t = cell.find(".//m:t", NS)
                vals.append(t.text if t is not None and t.text else "")
            rows.append(vals)
        out[name] = rows
    return out


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    req, _app = _client("admin")
    try:
        db.add(CloudAccount(id=1, name="主账号", vendor="aliyun", enabled=True))
        db.add_all([
            Asset(account_id=1, product="ecs", instance_id="i-1", name="web-01",
                  region="cn-hangzhou", zone="cn-hangzhou-h", status="Running",
                  instance_type="ecs.g6.large", extra=json.dumps({
                      "os_name": "CentOS 7.9", "ip_internal": "10.0.0.11",
                      "ip_public": "1.2.3.4", "cpu": 4, "memory_gb": 8,
                      "vpc_id": "vpc-abc", "vswitch_id": "vsw-def",
                      "security_group_ids": ["sg-1", "sg-2"],
                      "disks": [
                          {"disk_id": "d-1", "type": "system", "size_gb": 40,
                           "category": "cloud_essd", "name": "系统盘"},
                          {"disk_id": "d-2", "type": "data", "size_gb": 100,
                           "category": "cloud_essd", "name": "数据盘"},
                      ],
                  })),
            Asset(account_id=1, product="ecs", instance_id="i-2", name="db-01",
                  region="cn-hangzhou", status="Stopped",
                  extra=json.dumps({"os_name": "Ubuntu 22.04", "ip_internal": "10.0.0.12"})),
            Asset(account_id=1, product="vmware_vm", instance_id="vm-1", name="legacy-app",
                  region="", zone="esxi-01", status="poweredOn",
                  extra=json.dumps({
                      "power_state": "poweredOn", "guest_ip": "172.16.0.8",
                      "guest_os": "Windows Server 2019", "cpu_count": 2,
                      "memory_mb": 4096,
                      "disks": [{"label": "Hard disk 1", "size_gb": 60, "type": "disk"}],
                  })),
        ])
        db.commit()

        # ---- A 列目录：产品列表 + ECS 全字段 + 无产品列 ----
        r = req("GET", "/api/assets/export/schema")
        check("schema 200", r.status_code == 200, r.text[:200])
        data = r.json()
        prods = {p["product"]: p for p in data["products"]}
        check("22 类资产可选", len(prods) == 22, len(prods))
        ecs_keys = {c["key"] for c in prods["ecs"]["columns"]}
        for key in ("os_name", "status_label", "ip_internal", "ip_public", "cpu", "memory_gb",
                    "disk_count", "disk_total_gb", "system_disk", "data_disks", "disk_detail",
                    "vpc_id", "vswitch_id", "security_groups"):
            check(f"ECS 可勾选 {key}", key in ecs_keys)
        check("列目录不含产品列", "product" not in ecs_keys and "product" not in
              {c["key"] for c in prods["vmware_vm"]["columns"]})
        check("上限 20000 行", data["max_rows"] == 20000, data.get("max_rows"))

        # ---- B 多产品多 sheet + 勾选列顺序 + 中文表头/值 ----
        body = {
            "products": ["ecs", "vmware_vm"],
            "columns": {
                "ecs": ["name", "status_label", "os_name", "ip_internal", "vpc_id",
                        "vswitch_id", "disk_detail"],
                "vmware_vm": ["name", "power_state_label", "ip_internal", "disk_detail"],
            },
        }
        r = req("POST", "/api/assets/export/xlsx", json=body)
        check("导出 200", r.status_code == 200, r.text[:300])
        check("Content-Type XLSX", "spreadsheetml" in r.headers.get("content-type", ""))
        check("Sheet 按产品划分", list(_sheets(r.content)) == ["云主机", "VMware 虚拟机"],
              list(_sheets(r.content)))
        sheets = _sheets(r.content)
        ecs = sheets["云主机"]
        check("ECS 表头=勾选列且无产品列",
              ecs[0] == ["名称", "运行状态", "操作系统", "内网 IP", "VPC", "交换机", "磁盘明细"], ecs[0])
        check("ECS 数据行数", len(ecs) == 3, len(ecs))
        web = next(row for row in ecs[1:] if row[0] == "web-01")
        check("ECS 运行状态可读", web[1] == "运行中", web)
        check("ECS OS/VPC/交换机", web[2] == "CentOS 7.9" and web[4] == "vpc-abc"
              and web[5] == "vsw-def", web)
        check("ECS 磁盘明细", "40G" in web[6] and "100G" in web[6]
              and "系统盘" in web[6] and "数据盘" in web[6], web[6])
        vm = sheets["VMware 虚拟机"]
        check("VMware 表头=勾选列", vm[0] == ["名称", "电源状态", "虚拟机 IP", "磁盘明细"], vm[0])
        check("VMware 电源状态中文化", vm[1][1] == "运行中", vm[1])
        check("VMware guest IP 归一", vm[1][2] == "172.16.0.8", vm[1])
        check("行数响应头", r.headers.get("x-total-rows") == "3"
              and r.headers.get("x-exported-rows") == "3", dict(r.headers))

        # ---- C 当前页签筛选只作用于本产品，不跨域清空其他 sheet ----
        r = req("POST", "/api/assets/export/xlsx", json={
            **body, "status_product": "ecs", "status": "Running", "keyword": "web",
        })
        sheets = _sheets(r.content)
        check("筛选只作用于当前产品", len(sheets["云主机"]) == 2, len(sheets["云主机"]))
        check("其他产品不被当前页签筛选清空", len(sheets["VMware 虚拟机"]) == 2,
              len(sheets["VMware 虚拟机"]))

        # ---- D 参数校验 ----
        r = req("POST", "/api/assets/export/xlsx", json={"products": [], "columns": {}})
        check("未选资产类型 400", r.status_code == 400, r.text[:120])
        r = req("POST", "/api/assets/export/xlsx", json={
            "products": ["ecs"], "columns": {"ecs": ["name", "nope"]},
        })
        check("非法列 400", r.status_code == 400, r.text[:120])
        r = req("POST", "/api/assets/export/xlsx", json={
            "products": ["ecs"], "columns": {"ecs": []},
        })
        check("未勾列 400", r.status_code == 400, r.text[:120])
        r = req("POST", "/api/assets/export/xlsx", json={
            "products": ["nope"], "columns": {"nope": ["name"]},
        })
        check("非法资产类型 400", r.status_code == 400, r.text[:120])

        # ---- E viewer 可导出 + 审计留痕 ----
        req_v, _ = _client("viewer")
        r = req_v("POST", "/api/assets/export/xlsx", json={
            "products": ["ecs"], "columns": {"ecs": ["name"]},
        })
        check("viewer 可导出", r.status_code == 200, r.text[:120])
        db2 = SessionLocal()
        try:
            logs = db2.query(AuditLog).filter(AuditLog.action == "assets.export").all()
            check("审计 assets.export 落库", len(logs) >= 1, len(logs))
            check("审计记录行数", any((x.detail or "").find('"rows"') >= 0 for x in logs))
        finally:
            db2.close()
    finally:
        db.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
