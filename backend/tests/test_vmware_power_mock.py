#!/usr/bin/env python3
"""VMware 虚拟机电源操作 mock 测试（智能软关）。

隔离口径：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为一次性 PostgreSQL；
``app.services.vsphere.session`` 整体替换为假 content，**绝不连接真实 vCenter、
绝不执行任何真实电源动作**。

验证项：
  A 智能软关矩阵：Tools 运行 -> ShutdownGuest/RebootGuest；未运行 -> PowerOff/Reset；
    开机 -> PowerOnVM_Task；另一个方法绝不被调用；
  B 状态门：运行中开机、停止态关机/重启 -> 400（API 层拦截，不建立连接）；
  C 确认门：confirm_value 不一致 -> 400；在途锁 -> 409；
  D 账号门：无归属账号 / 凭据缺失 / 解密失败 -> 400；
  E 范围门：vmware_host（ESXi 宿主机）-> 404；
  F 失败路径：Tools 报运行但优雅关机 NoPermission -> 502 人话，且**不降级**为强制断电；
  G 审计：成功/失败都落 vmware.* 且 detail 带真实 mode/tools_running；
  H VM 定位兜底：instanceUuid 未命中时按 extra.mo_id 容器视图匹配。

用法：cd /opt/qianshu/backend && python3 tests/test_vmware_power_mock.py（退出码 0=全过）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

TMPDIR = tempfile.mkdtemp(prefix="qianshu-vmware-power-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ.setdefault("MASTER_KEY", "0123456789abcdef0123456789abcdef")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine, get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.main import app  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.ops_lock import EcsOpsLock  # noqa: E402
from app.services import vsphere  # noqa: E402

Base.metadata.create_all(engine)
db = SessionLocal()

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假 vSphere ----------------
class FakeVM:
    def __init__(self, mo_id: str, uuid: str, power: str = "poweredOn",
                 tools: str = "guestToolsRunning", fail: str = ""):
        self.calls: list = []
        self._moId = mo_id
        self.config = SimpleNamespace(instanceUuid=uuid)
        self.summary = SimpleNamespace(runtime=SimpleNamespace(powerState=power))
        self.guest = SimpleNamespace(toolsRunningStatus=tools)
        self.fail = fail

    def _rec(self, name: str):
        if self.fail == name:
            raise Exception("NoPermission: user not authorized for this operation")
        self.calls.append(name)

    def PowerOnVM_Task(self):
        self._rec("PowerOnVM_Task")

    def PowerOffVM_Task(self):
        self._rec("PowerOffVM_Task")

    def ShutdownGuest(self):
        self._rec("ShutdownGuest")

    def RebootGuest(self):
        self._rec("RebootGuest")

    def ResetVM_Task(self):
        self._rec("ResetVM_Task")


STATE: dict = {"vm": None, "fallback": [], "sessions": 0}


@contextmanager
def fake_session(host: str, user: str = "", password: str = "", port: int = 443,
                 verify_ssl: bool = False):
    STATE["sessions"] += 1
    vm = STATE["vm"]

    def _find_by_uuid(**kw):
        return vm if vm is not None and kw.get("uuid") == vm.config.instanceUuid else None

    content = SimpleNamespace(
        searchIndex=SimpleNamespace(FindByUuid=_find_by_uuid),
        viewManager=SimpleNamespace(
            CreateContainerView=lambda *a, **k: SimpleNamespace(
                view=STATE["fallback"], Destroy=lambda: None)),
        rootFolder=SimpleNamespace(),
    )
    yield content


vsphere.session = fake_session

# ---------------- 造数（只写临时库） ----------------
vc = CloudAccount(name="内网vCenter", vendor="vcenter",
                  ak_cipher=encrypt_secret("ops-admin"),
                  secret_cipher=encrypt_secret("ops-secret"),
                  extra_json=json.dumps({"host": "vc.example.local", "port": 443}))
vc_nocred = CloudAccount(name="只读无凭据", vendor="vcenter", extra_json="{}")
ali = CloudAccount(name="阿里生产", vendor="aliyun", region="cn-hangzhou")
db.add_all([vc, vc_nocred, ali])
db.commit()


def asset(acc_id, prod, iid, name, status="running", extra=None):
    return Asset(account_id=acc_id, product=prod, instance_id=iid, name=name,
                 region="", status=status, extra=json.dumps(extra or {}))


vm_run = asset(vc.id, "vmware_vm", "uuid-run", "订单虚机-1", "running")
vm_raw = asset(vc.id, "vmware_vm", "uuid-raw", "订单虚机-2", "running")
vm_stop = asset(vc.id, "vmware_vm", "uuid-stop", "订单虚机-3", "stopped")
vm_noacc = asset(0, "vmware_vm", "uuid-noacc", "无账号虚机", "running")
vm_nocred = asset(vc_nocred.id, "vmware_vm", "uuid-nocred", "无凭据虚机", "running")
host = asset(vc.id, "vmware_host", "host-1", "ESXi-01", "running")
db.add_all([vm_run, vm_raw, vm_stop, vm_noacc, vm_nocred, host])
db.commit()

# ---------------- API 客户端 ----------------
STUB = SimpleNamespace(id=1, username="tester", role="admin", is_active=True)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: STUB


def post(asset_id: int, action: str, confirm: str):
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.post(f"/api/assets/{asset_id}/actions",
                               json={"action": action, "confirm_value": confirm})
    return asyncio.run(_go())


def fresh_vm(power="poweredOn", tools="guestToolsRunning", fail="", uuid="uuid-run"):
    vm = FakeVM("vm-1", uuid, power=power, tools=tools, fail=fail)
    STATE.update(vm=vm, fallback=[], sessions=0)
    return vm


# ---------------- A 智能软关矩阵 ----------------
print("A 智能软关矩阵")

vm = fresh_vm()
r = post(vm_run.id, "stop", "uuid-run")
body = r.json()
check("Tools 运行 + 关机 -> 200", r.status_code == 200, str(body))
check("Tools 运行 -> ShutdownGuest", vm.calls == ["ShutdownGuest"], str(vm.calls))
check("响应 mode=guest_shutdown", body.get("mode") == "guest_shutdown", str(body))
check("响应 tools_running=true", body.get("tools_running") is True, str(body))
check("响应 expected_status=stopped", body.get("expected_status") == "stopped", str(body))

vm = fresh_vm(tools="guestToolsNotRunning")
r = post(vm_run.id, "stop", "uuid-run")
body = r.json()
check("Tools 未运行 + 关机 -> 200", r.status_code == 200, str(body))
check("Tools 未运行 -> PowerOffVM_Task", vm.calls == ["PowerOffVM_Task"], str(vm.calls))
check("响应 mode=power_off", body.get("mode") == "power_off", str(body))
check("响应 tools_running=false", body.get("tools_running") is False, str(body))

vm = fresh_vm(tools="guestToolsNotRunning")
r = post(vm_run.id, "reboot", "uuid-run")
body = r.json()
check("Tools 未运行 + 重启 -> ResetVM_Task", vm.calls == ["ResetVM_Task"], str(vm.calls))
check("响应 mode=reset", body.get("mode") == "reset", str(body))
check("重启后 expected_status=running", body.get("expected_status") == "running", str(body))

vm = fresh_vm()
r = post(vm_run.id, "reboot", "uuid-run")
body = r.json()
check("Tools 运行 + 重启 -> RebootGuest", vm.calls == ["RebootGuest"], str(vm.calls))
check("响应 mode=guest_reboot", body.get("mode") == "guest_reboot", str(body))

vm = fresh_vm(power="poweredOff", tools="guestToolsNotRunning", uuid="uuid-stop")
r = post(vm_stop.id, "start", "uuid-stop")
body = r.json()
check("停止态 + 开机 -> PowerOnVM_Task", vm.calls == ["PowerOnVM_Task"], str(vm.calls))
check("响应 mode=power_on", body.get("mode") == "power_on", str(body))
check("开机 expected_status=running", body.get("expected_status") == "running", str(body))

# ---------------- B 状态门（API 层，不建连接） ----------------
print("B 状态门")
vm = fresh_vm()
before = STATE["sessions"]
r = post(vm_run.id, "start", "uuid-run")
check("运行中开机 -> 400", r.status_code == 400, str(r.json()))
check("状态门不建立 vCenter 连接", STATE["sessions"] == before, str(STATE["sessions"]))
check("状态门不下发任何动作", vm.calls == [], str(vm.calls))

vm = fresh_vm(power="poweredOff")
r = post(vm_stop.id, "stop", "uuid-stop")
check("停止态关机 -> 400", r.status_code == 400, str(r.json()))
r = post(vm_stop.id, "reboot", "uuid-stop")
check("停止态重启 -> 400", r.status_code == 400, str(r.json()))
check("停止态动作未下发", vm.calls == [], str(vm.calls))

# ---------------- C 确认门 / 互斥锁 ----------------
print("C 确认门 / 互斥锁")
vm = fresh_vm()
r = post(vm_run.id, "stop", "wrong-id")
check("confirm_value 不一致 -> 400", r.status_code == 400, str(r.json()))
check("确认门不建立连接、不下发", vm.calls == [] and STATE["sessions"] == 0)

lock = EcsOpsLock(asset_id=vm_run.id, instance_id=vm_run.instance_id, op="stop",
                  locked_by="other", expires_at=datetime.now() + timedelta(minutes=5))
db.add(lock)
db.commit()
r = post(vm_run.id, "stop", "uuid-run")
check("在途锁 -> 409", r.status_code == 409, str(r.json()))
check("锁冲突不下发", vm.calls == [])
db.delete(lock)
db.commit()

# ---------------- D 账号门 ----------------
print("D 账号门")
r = post(vm_noacc.id, "stop", "uuid-noacc")
check("无归属账号 -> 400", r.status_code == 400, str(r.json()))
check("无账号提示明确", "无归属云账号" in str(r.json()), str(r.json()))
r = post(vm_nocred.id, "stop", "uuid-nocred")
check("凭据缺失 -> 400", r.status_code == 400, str(r.json()))
check("缺凭据提示明确", "用户名/密码" in str(r.json()), str(r.json()))
check("账号门不建立连接", STATE["sessions"] == 0, str(STATE["sessions"]))

# ---------------- E 范围门 ----------------
print("E 范围门")
r = post(host.id, "stop", "host-1")
check("vmware_host -> 404", r.status_code == 404, str(r.json()))

# ---------------- F 失败路径（不静默降级） ----------------
print("F 失败路径")
vm = fresh_vm(fail="ShutdownGuest")
r = post(vm_run.id, "stop", "uuid-run")
body = r.json()
check("优雅关机失败 -> 502", r.status_code == 502, str(body))
check("失败提示为人话权限说明", "权限不足" in str(body), str(body))
check("失败不降级为强制断电", vm.calls == [], str(vm.calls))

# ---------------- G 审计 ----------------
print("G 审计")
ok_rows = (db.query(AuditLog).filter(AuditLog.action == "vmware.stop")
           .order_by(AuditLog.id.desc()).all())
check("vmware.stop 审计已落库", ok_rows, str(len(ok_rows)))
modes = [json.loads(x.detail or "{}").get("mode") for x in ok_rows]
check("审计记录真实 mode（guest_shutdown/power_off）",
      "guest_shutdown" in modes and "power_off" in modes, str(modes))
tools = [json.loads(x.detail or "{}").get("tools_running") for x in ok_rows]
check("审计记录 tools_running", True in tools and False in tools, str(tools))
check("审计含账号与实例 ID",
      all(json.loads(x.detail or "{}").get("instance_id") for x in ok_rows))
fail_rows = (db.query(AuditLog).filter(AuditLog.action == "vmware.stop",
                                       AuditLog.result == "failed").all())
check("失败动作也落审计", fail_rows, str(len(fail_rows)))
reb = db.query(AuditLog).filter(AuditLog.action == "vmware.reboot").count()
check("vmware.reboot 审计已登记", reb >= 2, str(reb))

# ---------------- H VM 定位兜底（mo_id） ----------------
print("H VM 定位兜底")
mo_vm = FakeVM("vm-777", "uuid-different", power="poweredOn", tools="guestToolsRunning")
STATE.update(vm=mo_vm, fallback=[mo_vm], sessions=0)
mo_asset = asset(vc.id, "vmware_vm", "mo-777", "moId虚机", "running",
                 {"mo_id": "vm-777"})
db.add(mo_asset)
db.commit()
r = post(mo_asset.id, "reboot", "mo-777")
check("instanceUuid 未命中 -> moId 兜底成功", r.status_code == 200, str(r.json()))
check("兜底后调用 RebootGuest", mo_vm.calls == ["RebootGuest"], str(mo_vm.calls))

db.close()
print(f"\n{'=' * 60}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
