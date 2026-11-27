from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# 云账号管理是唯一凭证写入口，不再定义 CredentialCreate/CredentialUpdate。
# 旧 /settings/credentials 写接口返回 410；保留 GET /credentials 只读摘要，
# 用于展示旧凭证表遗留行及 migrated 迁移标记。

class CredentialOut(BaseModel):
    id: int
    name: str
    vendor: str
    access_key_id_masked: str
    remark: str
    is_default: bool
    enabled: bool
    migrated: bool = False   # 已洗入云账号台账（migrate_cloud_accounts_from_legacy）
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    # 留空则由服务端生成一次性初始密码，仅在创建响应里回显一次（与智能体建号口径一致）。
    password: Optional[str] = Field(default=None, min_length=8, max_length=64)
    display_name: str = ""
    role: str = Field(pattern="^(admin|operator|viewer)$")
    # 创建即强制首登改密：无论密码自动生成还是管理员显式给定，默认都为 True；
    # 仅显式传 False 才可关闭（不推荐）。
    must_change_password: bool = True


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = Field(default=None, pattern="^(admin|operator|viewer)$")
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=64)
