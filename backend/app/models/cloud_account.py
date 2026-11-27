"""云账号：多厂商多账号并存的账号台账。

定位：资产/运维模块化多云的账号维度主表——每个云厂商可挂 N 个账号（混合云）。
AK/SK 双加密（ak_cipher/secret_cipher 均为 AES-256-GCM 密文，master key 只在环境变量），
区别于旧凭证表 cloud_credentials（AK 明文）：本表按"密钥只进不出"口径全密文。

旧凭证表与环境变量由凭证回退链兼容；本表凭证经 /clouds/sync、
/clouds/accounts/{id}/test 及 collector/credential.py 生效。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CloudAccount(Base):
    __tablename__ = "cloud_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    vendor: Mapped[str] = mapped_column(String(16), default="aliyun")   # aliyun/tencent/volc/baidu/huawei/aws/azure/onprem
    ak_cipher: Mapped[str] = mapped_column(Text, default="")            # AccessKey 密文（自建可空）
    secret_cipher: Mapped[str] = mapped_column(Text, default="")        # Secret 密文（自建可空）
    region: Mapped[str] = mapped_column(String(64), default="")         # 默认区域（多区域走采集器遍历，此处记主区域）
    extra_json: Mapped[str] = mapped_column(Text, default="{}")         # 厂商私有扩展（endpoint/子账号/备注等）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # untested/probing/syncing/seeding/ok/error/unsupported；前三个是接入非终态
    status: Mapped[str] = mapped_column(String(16), default="untested")
    last_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 自动告警规则播种时间：一次性标记——首次同步成功后
    # 按本账号实际资产播种常见规则；之后用户删改不再重建（不复活已删除规则）
    alert_seeded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 最近一次完整成功同步时间（失败/部分失败不推进，避免把失败显示为已同步）
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # ---- 监控策略（按数据源分级） ----
    # 优先级决定采集/评估节奏与重复提醒默认值；默认 high = 升级后行为不变。
    priority: Mapped[str] = mapped_column(String(8), default="high",
                                          server_default="high", nullable=False)
    # 提醒去向（与模块开关取 AND；账号级总开关仍是 enabled=false 停采）
    notify_overview: Mapped[bool] = mapped_column(Boolean, default=True,
                                                  server_default=true(), nullable=False)
    notify_inapp: Mapped[bool] = mapped_column(Boolean, default=True,
                                               server_default=true(), nullable=False)
    notify_push: Mapped[bool] = mapped_column(Boolean, default=True,
                                              server_default=true(), nullable=False)
    # NULL=继承优先级默认；0=永不重申；N=确认/忽略后 N 天再提示
    repeat_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 到期驱动采集的游标（见 services/monitor_policy.collect_due）
    last_metrics_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_alert_eval_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_host_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_cost_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
