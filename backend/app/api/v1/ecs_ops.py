"""ECS 电源操作：重启/关机/开机。

安全模型：
1. 危险操作要求 body.confirm_value 逐字符等于实例 ID；
2. 实例级互斥锁（10 分钟过期），防止重复下发；
3. 全部落审计；
4. 仅 operator/admin 可执行。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.collector.credential import resolve_aliyun_credential
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.asset import Asset
from app.models.ops_lock import EcsOpsLock
from app.models.user import User
from app.services import audit

logger = logging.getLogger("qianshu.ecs_ops")
router = APIRouter(prefix="/assets", tags=["assets"])

LOCK_TTL_MINUTES = 10


class OpsRequest(BaseModel):
    action: str  # reboot / stop / start
    confirm_value: str  # 必须逐字符等于实例ID


def _ecs_client(ak: str, sk: str, region_id: str):
    from alibabacloud_ecs20140526.client import Client
    from alibabacloud_tea_openapi.models import Config

    return Client(Config(access_key_id=ak, access_key_secret=sk, region_id=region_id))


def _acquire_lock(db: Session, asset: Asset, op: str, username: str) -> Optional[EcsOpsLock]:
    now = datetime.now()
    # 清理过期锁
    db.query(EcsOpsLock).filter(EcsOpsLock.expires_at < now).delete()
    existing = (
        db.query(EcsOpsLock)
        .filter(EcsOpsLock.asset_id == asset.id, EcsOpsLock.expires_at >= now)
        .first()
    )
    if existing:
        return None
    lock = EcsOpsLock(
        asset_id=asset.id,
        instance_id=asset.instance_id,
        op=op,
        locked_by=username,
        expires_at=now + timedelta(minutes=LOCK_TTL_MINUTES),
    )
    db.add(lock)
    db.commit()
    return lock


def _release_lock(db: Session, lock: EcsOpsLock) -> None:
    db.query(EcsOpsLock).filter(EcsOpsLock.id == lock.id).delete()
    db.commit()


@router.post("/{asset_id}/actions")
def execute_action(
    asset_id: int,
    body: OpsRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    if body.action not in ("reboot", "stop", "start"):
        raise HTTPException(400, f"不支持的操作: {body.action}")

    asset = db.get(Asset, asset_id)
    # VMware 虚拟机与 ECS 使用同级门禁；ESXi 宿主机（vmware_host）明确不支持
    if asset is None or asset.product not in ("ecs", "vmware_vm"):
        raise HTTPException(404, "资产不存在")
    if asset.status == "Deleted":
        raise HTTPException(400, "实例已删除")

    # 二次确认：必须逐字符等于实例 ID
    if body.confirm_value != asset.instance_id:
        raise HTTPException(400, "确认值与实例 ID 不一致，操作已拒绝")

    ip = get_client_ip(request)
    is_vmware = asset.product == "vmware_vm"
    status = (asset.status or "").lower()   # ECS=Running/Stopped；VMware=running/stopped/suspended

    # 状态校验：符合预期才执行
    if body.action == "reboot" and status != "running":
        raise HTTPException(400, f"实例当前状态 {asset.status}，只有运行中才能重启")
    if body.action == "stop" and status != "running":
        raise HTTPException(400, f"实例当前状态 {asset.status}，只有运行中才能关机")
    if body.action == "start" and status not in ("stopped", "suspended"):
        raise HTTPException(400, f"实例当前状态 {asset.status}，只有已停止才能开机")

    # VMware：账号/凭据/能力位先行校验（避免加锁后才失败）；凭据只解密不落日志
    provider = account = vm_user = vm_password = None
    if is_vmware:
        from app.core.security import decrypt_secret
        from app.models.cloud_account import CloudAccount
        from app.services.cloud.provider import get_provider

        account = db.get(CloudAccount, asset.account_id or 0) if asset.account_id else None
        if account is None:
            raise HTTPException(400, "资产无归属云账号，无法执行电源操作")
        try:
            provider = get_provider(account.vendor)
        except ValueError:
            raise HTTPException(400, f"云账号厂商 {account.vendor} 不支持电源操作")
        if not provider.capability.power:
            raise HTTPException(400, f"{provider.label} 不支持电源操作")
        try:
            vm_user = decrypt_secret(account.ak_cipher) if account.ak_cipher else ""
            vm_password = decrypt_secret(account.secret_cipher) if account.secret_cipher else ""
        except Exception:
            raise HTTPException(400, "云账号凭据解密失败，请重新保存账号凭据")
        if not vm_user or not vm_password:
            raise HTTPException(400, "云账号缺少 vCenter 用户名/密码，无法执行电源操作")

    # 互斥锁
    lock = _acquire_lock(db, asset, body.action, user.username)
    if lock is None:
        raise HTTPException(409, "该实例有在途操作，请稍后再试")

    audit_action = f"vmware.{body.action}" if is_vmware else f"ecs.{body.action}"
    audit_type = "vmware" if is_vmware else "ecs"

    try:
        if is_vmware:
            result = provider.power_action(account, asset, body.action, vm_user, vm_password)
            new_status = result.get("expected_status") or body.action
            mode = result.get("mode") or ""
            mode_label = result.get("mode_label") or mode
            audit.record(
                db, action=audit_action, user_id=user.id, username=user.username,
                resource_type=audit_type, resource_id=asset.instance_id,
                detail={"name": asset.name, "instance_id": asset.instance_id,
                        "account": account.name, "mode": mode,
                        "tools_running": result.get("tools_running")},
                client_ip=ip,
            )
            db.commit()
            return {
                "ok": True,
                "action": body.action,
                "instance_id": asset.instance_id,
                "name": asset.name,
                "mode": mode,
                "mode_label": mode_label,
                "tools_running": result.get("tools_running"),
                "expected_status": new_status,
                "message": f"已下发「{mode_label}」指令，虚拟机 {asset.instance_id}（{asset.name}）",
            }

        ak, sk, source = resolve_aliyun_credential(db)
        client = _ecs_client(ak, sk, asset.region)
        from alibabacloud_ecs20140526 import models as ecs

        if body.action == "reboot":
            client.reboot_instance(ecs.RebootInstanceRequest(instance_id=asset.instance_id, force_stop=False))
            new_status = "Rebooting"
        elif body.action == "stop":
            client.stop_instance(ecs.StopInstanceRequest(instance_id=asset.instance_id, force_stop=False))
            new_status = "Stopping"
        else:
            client.start_instance(ecs.StartInstanceRequest(instance_id=asset.instance_id))
            new_status = "Starting"

        audit.record(
            db, action=audit_action, user_id=user.id, username=user.username,
            resource_type=audit_type, resource_id=asset.instance_id,
            detail={"name": asset.name, "region": asset.region, "cred_source": source},
            client_ip=ip,
        )
        db.commit()
        return {
            "ok": True,
            "action": body.action,
            "instance_id": asset.instance_id,
            "name": asset.name,
            "expected_status": new_status,
            "message": f"已下发 {body.action} 指令，实例 {asset.instance_id}（{asset.name}）",
        }
    except Exception as e:
        msg = str(e)
        logger.exception("%s 操作失败: %s %s", "VMware" if is_vmware else "ECS",
                         body.action, asset.instance_id)
        # 权限类错误给出明确提示（VMware 的人话已在 vsphere_ops 里给出）
        if is_vmware:
            hint = msg[:240]
        elif "Forbidden" in msg or "NotAuthorized" in msg:
            hint = "当前凭证无此操作权限（需要 ecs:RebootInstance/StopInstance/StartInstance）"
        else:
            hint = msg[:200]
        audit.record(
            db, action=audit_action, user_id=user.id, username=user.username,
            resource_type=audit_type, resource_id=asset.instance_id,
            result="failed", detail={"error": hint, "instance_id": asset.instance_id},
            client_ip=ip,
        )
        db.commit()
        raise HTTPException(502, f"操作下发失败: {hint}")
    finally:
        _release_lock(db, lock)
