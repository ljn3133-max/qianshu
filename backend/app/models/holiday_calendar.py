"""节假日日历：基线分组的"实际作息"依据。

中国的日子不止周一~五/周六日两类：调休上班的周六日是工作日、调休放掉的
周中日是休息日。只按"星期几"分基线会在调休日误报。

数据全自动同步：来源 holiday-cn（镜像国务院放假安排，days 含调休班次
isOffDay=false），jsdelivr CDN 分发，URL 可配置。
普通周末/工作日不在源数据里（只含法定安排的特殊日），缺省按星期几判定。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class HolidayCalendar(Base):
    __tablename__ = "holiday_calendar"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True, index=True)     # 日期
    kind: Mapped[str] = mapped_column(String(16))                   # workday/rest/holiday
    name: Mapped[str] = mapped_column(String(64), default="")       # 节日名，如 春节
    source: Mapped[str] = mapped_column(String(32), default="auto")  # auto/manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
