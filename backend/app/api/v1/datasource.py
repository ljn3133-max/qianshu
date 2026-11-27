"""数据源 API：注册表列表 + 连通性探测。

GET  /datasources         全数据源 + 探测结果（底座体检的接入面）
POST /datasources/test    单源测试连通（新增数据源弹窗保存前测试，取真实版本信息）
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user, require_roles
from app.models.user import User
from app.services.datasource import probe_all

logger = logging.getLogger("qianshu.datasource_api")
router = APIRouter(prefix="/datasources", tags=["datasources"])


@router.get("")
def list_datasources(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # db 传入 SLS 等需凭证上下文的探测（resolve_aliyun_credential）
    return probe_all(db)


# 保存前对未保存的表单配置直接探活，语义与注册表探测一致：
# 取到版本/系统信息才算通（只读探测，不写任何配置）。
class DatasourceTestIn(BaseModel):
    name: str = ""
    type: str
    url: str = ""
    headers: Optional[dict] = None
    user: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    verify_ssl: bool = False   # 默认不校验内网自签证书，可显式开启


# 纯日志侧类型（其余按指标源探测；es/http 两侧探测器同实现，归哪侧无差）
_LOG_ONLY_TYPES = ("loki", "sls", "cls", "graylog", "filebeat", "skywalking", "zipkin")


@router.post("/test")
def test_datasource(body: DatasourceTestIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles("admin", "operator"))):
    """测试连通：按类型走真实探测器（版本/系统信息才算通），结果与注册表探测同口径。"""
    from app.services.datasource import probe_log_source, probe_metric_source

    if not (body.type or "").strip():
        raise HTTPException(400, "缺数据源类型")
    # vm 类型允许空地址（builtin-vm 内置语义：探测退 settings.VM_URL 千枢自带指标库），
    # 其余类型必须显式地址
    if not (body.url or "").strip() and body.type.strip() != "vm":
        raise HTTPException(400, "缺访问地址 URL")
    src = {"name": body.name or body.type, "type": body.type.strip(), "url": body.url.strip(),
           "verify_ssl": bool(body.verify_ssl)}   # 传递表单选择的 TLS 校验策略
    if body.headers:
        src["headers"] = body.headers
    for k in ("user", "password", "token"):
        if getattr(body, k):
            src[k] = getattr(body, k)
    probe = probe_log_source if body.type in _LOG_ONLY_TYPES else probe_metric_source
    return probe(src, db)
