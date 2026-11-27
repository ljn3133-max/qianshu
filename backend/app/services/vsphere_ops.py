"""VMware 虚拟机电源操作：按 Tools 状态自动选择优雅或强制方式。

语义（一个按钮自适应，不给运维造成"该选哪个"的负担）：
  - 关机：Tools 运行中 -> ShutdownGuest()（优雅关机）；Tools 未运行 -> PowerOffVM_Task()
    （强制断电，确认框已明确警示）
  - 重启：Tools 运行中 -> RebootGuest()；Tools 未运行 -> ResetVM_Task()
  - 开机：PowerOnVM_Task()（含挂起态恢复）

安全边界：
  - 本模块只做「动作下发」，确认门（逐字符实例 ID）、10 分钟互斥锁、状态校验与
    全量审计在 API 层（api/v1/ecs_ops.py）——与 ECS 同一套门禁；
  - 只接受 vmware_vm；ESXi 宿主机（vmware_host）物理断电不在授权范围，由 API 层拒绝；
  - Tools 状态以后端执行时实际读取为准，响应/审计记录真实采用的模式
    （guest_shutdown / power_off / guest_reboot / reset / power_on）；
  - 不静默降级：Tools 报运行但优雅关机失败时如实报错，绝不偷偷改成强制断电。

连接复用 services/vsphere.py 的 ``_account_conn`` + ``session`` seam（mock 测试只换
``session``），凭据由调用方（Provider 层）解密传入。
"""
from __future__ import annotations

import json
import logging

from app.services import vsphere
from app.services.vsphere import VSphereError

logger = logging.getLogger("qianshu.vsphere_ops")

ACTIONS = ("start", "stop", "reboot")
TOOLS_RUNNING = "guestToolsRunning"   # vSphere guest.toolsRunningStatus 的正常值

POWER_STATE_MAP = {"poweredOn": "running", "poweredOff": "stopped", "suspended": "suspended"}
STATE_LABELS = {"running": "运行中", "stopped": "已停止", "suspended": "已挂起"}

# 动作 -> 执行后预期状态（API 回传给前端做即时提示，不伪造已生效）
EXPECTED_STATUS = {"start": "running", "stop": "stopped", "reboot": "running"}

MODE_LABELS = {
    "power_on": "开机",
    "guest_shutdown": "优雅关机（VMware Tools）",
    "power_off": "强制断电",
    "guest_reboot": "优雅重启（VMware Tools）",
    "reset": "强制重启（Reset）",
}


def _attr(obj, name: str):
    try:
        return getattr(obj, name, None)
    except Exception:   # MOR 惰性属性在断连/权限不足时可能抛异常
        return None


def _find_vm(content, asset):
    """按 instanceUuid 优先、moId 兜底定位虚拟机；找不到明确报错，不猜。

    资产同步写入 instance_id = instanceUuid or mo_id，extra.mo_id 也保留 moId。
    """
    from pyVmomi import vim

    instance = (asset.instance_id or "").strip()
    mo_id = ""
    try:
        extra = json.loads(asset.extra or "{}")
        mo_id = str(extra.get("mo_id") or "")
    except (ValueError, TypeError):
        mo_id = ""

    if instance:
        try:
            vm = content.searchIndex.FindByUuid(
                datacenter=None, uuid=instance, vmSearch=True, instanceUuid=True)
        except Exception:
            vm = None
        if vm is not None:
            return vm

    target = {v for v in (instance, mo_id) if v}
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True)
    try:
        for vm in (getattr(view, "view", None) or []):
            if str(_attr(vm, "_moId") or "") in target:
                return vm
            cfg = _attr(vm, "config")
            if str(_attr(cfg, "instanceUuid") or "") in target:
                return vm
    finally:
        try:
            view.Destroy()
        except Exception:
            logger.debug("Destroy 容器视图失败（忽略）", exc_info=True)
    raise VSphereError("虚拟机不存在或已删除（instanceUuid / moId 均未匹配）")


def _friendly_error(err: Exception) -> str:
    """pyvmomi 异常 -> 运维能直接行动的人话（不把 SDK 原文抛给用户）。"""
    name = type(err).__name__
    text = str(err)
    if "NoPermission" in name or "NoPermission" in text:
        return ("vCenter 账号权限不足：需 VirtualMachine.Interact.PowerOn/PowerOff/Reset "
                "与 Guest Shutdown/Reboot 权限（当前账号多为只读采集账号，请联系管理员授权）")
    if "InvalidPowerState" in name or "InvalidState" in name:
        return "虚拟机电源状态已变化，请刷新页面后重试"
    if "ToolsUnavailable" in name or "GuestOperationsUnavailable" in name:
        return "VMware Tools 未就绪，无法优雅操作；请重试（将按 Tools 实际状态选择强制方式）"
    if "NotSupported" in name:
        return "该虚拟机不支持此电源操作"
    return f"vCenter 返回错误：{name}: {text[:160]}"


def power_action(account, asset, action: str, username: str = "",
                 password: str = "") -> dict:
    """执行电源动作 -> {ok, action, mode, tools_running, power_state, expected_status}。

    account/asset 为 ORM 对象；username/password 是 vSphere 账号（Provider 的 ak/sk）。
    调用前 API 层已完成：状态校验、实例 ID 确认、互斥锁；本函数再做一次状态复核，
    防止确认框打开期间状态变化导致误操作。
    """
    if action not in ACTIONS:
        raise VSphereError(f"不支持的操作: {action}")

    conn = vsphere._account_conn(account, username, password)
    with vsphere.session(conn["host"], conn["user"], conn["password"],
                         conn["port"], conn["verify_ssl"]) as content:
        vm = _find_vm(content, asset)

        runtime = _attr(_attr(vm, "summary"), "runtime")
        state_raw = str(_attr(runtime, "powerState") or "")
        state = POWER_STATE_MAP.get(state_raw, state_raw)
        tools_raw = str(_attr(_attr(vm, "guest"), "toolsRunningStatus") or "")
        tools_running = tools_raw == TOOLS_RUNNING

        try:
            if action == "start":
                if state == "running":
                    raise VSphereError("虚拟机已在运行，无需开机")
                vm.PowerOnVM_Task()
                mode = "power_on"
            elif action == "stop":
                if state != "running":
                    raise VSphereError(
                        f"当前状态 {STATE_LABELS.get(state, state_raw or '未知')}，只有运行中才能关机")
                if tools_running:
                    vm.ShutdownGuest()
                    mode = "guest_shutdown"
                else:
                    vm.PowerOffVM_Task()
                    mode = "power_off"
            else:   # reboot
                if state != "running":
                    raise VSphereError(
                        f"当前状态 {STATE_LABELS.get(state, state_raw or '未知')}，只有运行中才能重启")
                if tools_running:
                    vm.RebootGuest()
                    mode = "guest_reboot"
                else:
                    vm.ResetVM_Task()
                    mode = "reset"
        except VSphereError:
            raise
        except Exception as e:   # 权限/状态/工具异常统一转人话
            raise VSphereError(_friendly_error(e)) from e

    logger.info("VMware 电源操作已下发：asset=%s action=%s mode=%s tools=%s",
                asset.instance_id, action, mode, tools_running)
    return {
        "ok": True,
        "action": action,
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "tools_running": tools_running,
        "power_state": state_raw,
        "expected_status": EXPECTED_STATUS[action],
    }
