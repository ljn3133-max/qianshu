"""模块级提醒开关，按数据源控制提醒去向。

一张小表承载「哪个模块可以出现在哪里」：

- 提醒总开关（enabled）：关闭只停提醒，不停采集；采集总开关仍是 cloud_accounts.enabled。
- overview / inapp / push：总览待办 / 站内信 / 外部渠道（企微、钉钉等）三个去向。
- 账号级模块还要与该账号的 notify_overview/notify_inapp/notify_push 取 AND。

模块清单的唯一事实源是 services/monitor_policy.MODULES；本表只存用户覆盖值。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MonitorModuleSetting(Base):
    __tablename__ = "monitor_module_settings"

    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True,
                                          server_default=true(), nullable=False)
    overview: Mapped[bool] = mapped_column(Boolean, default=True,
                                           server_default=true(), nullable=False)
    inapp: Mapped[bool] = mapped_column(Boolean, default=True,
                                        server_default=true(), nullable=False)
    push: Mapped[bool] = mapped_column(Boolean, default=True,
                                       server_default=true(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)
