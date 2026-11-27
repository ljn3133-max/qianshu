"""混合云 Agent 接收端：非云机器上报主机指标/进程/资产。

安全：X-Agent-Token 校验（AGENT_TOKEN）；只接收上报，**没有任何下发执行通道**。
指标落 VM（语义对齐阿里云 CMS，命名 aliyun_ecs_*——诊断/画像工具零改动复用）；
资产落 assets 表（product=host）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session

from app.collector import vm as vm_client
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.asset import Asset

logger = logging.getLogger("qianshu.agent_api")
router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/report")
async def agent_report(request: Request):
    token = request.headers.get("x-agent-token", "")
    if not settings.AGENT_TOKEN or token != settings.AGENT_TOKEN:
        raise HTTPException(403, "agent token 校验失败（配置 AGENT_TOKEN）")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "payload 不是合法 JSON")

    host = payload.get("host") or {}
    procs = payload.get("processes") or []
    hostname = host.get("hostname") or "unknown"
    now = int(payload.get("ts") or __import__("time").time())

    # 1) 主机指标 → VM（对齐 CMS 语义）
    lines = [
        f'aliyun_ecs_cpuutilization{{instance_id="{hostname}",product="host",region="idc"}} {host.get("cpu_pct", 0)}',
        f'aliyun_ecs_memory_usedutilization{{instance_id="{hostname}",product="host",region="idc"}} {host.get("mem_pct", 0)}',
        f'aliyun_ecs_diskusage_utilization{{instance_id="{hostname}",product="host",region="idc"}} {host.get("disk_pct", 0)}',
    ]
    for p in procs[:10]:
        name = str(p.get("name", "")).replace('"', "")[:40]
        lines.append(
            f'aliyun_ecs_process_cpuutilization{{instance_id="{hostname}",process="{name}",product="host"}} {p.get("cpu_pct", 0)}')
    try:
        vm_client.write_prometheus(lines)
    except Exception as e:
        logger.warning("agent 指标写入 VM 失败: %s", str(e)[:80])

    # 2) 资产入库（product=host，幂等 upsert）
    db: Session = SessionLocal()
    try:
        row = db.query(Asset).filter(Asset.product == "host",
                                     Asset.instance_id == hostname).first()
        extra = {"os": host.get("os", ""), "arch": host.get("arch", ""),
                 "mem_total_gb": host.get("mem_total_gb"),
                 "disks": host.get("disks", []), "source": "agent"}
        if row is None:
            row = Asset(product="host", instance_id=hostname, name=hostname,
                        region="idc", status="Running", extra=json.dumps(extra, ensure_ascii=False))
            db.add(row)
        else:
            row.extra = json.dumps(extra, ensure_ascii=False)
            row.status = "Running"
            row.synced_at = datetime.now()
        db.commit()
    finally:
        db.close()

    return {"ok": True, "host": hostname, "processes": len(procs)}
