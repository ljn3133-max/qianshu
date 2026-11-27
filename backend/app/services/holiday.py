"""节假日日历同步与作息判定。

自动同步：holiday-cn 镜像国务院放假安排，每年一份 JSON：
    {"days": [{"name": "春节", "date": "2026-02-14", "isOffDay": false}, ...]}
isOffDay=false 的条目就是**调休上班日**（含落在周六日的），这是本服务存在的理由——
按"星期几"分基线在调休日必然误报。

判定规则 day_kind(d)：
- 源数据有该日：isOffDay=False -> workday（调休上班，哪怕是周日）；
                 isOffDay=True  -> holiday（法定假期，哪怕是周三）
- 源数据没有 -> 按星期几：周一~五 workday，周六日 rest（普通周末不进源数据）

源 URL 可配置（HOLIDAY_URL_TEMPLATE）；拉不到时返回 false，调用方继续用
星期几兜底 + 基线引擎的数据自校验，不会因为日历缺失就误报。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.holiday_calendar import HolidayCalendar

logger = logging.getLogger("qianshu.holiday")

# 进程内缓存：{date: kind}，同步后清空
_kind_cache: dict = {}

KIND_WORKDAY = "workday"
KIND_REST = "rest"
KIND_HOLIDAY = "holiday"


def sync_holidays(db: Session, years: list | None = None) -> dict:
    """拉取并落库节假日安排。返回 {year: rows}；单年失败不阻断其他年。"""
    today = date.today()
    years = years or [today.year, today.year + 1]
    out: dict = {}
    for year in years:
        url = settings.HOLIDAY_URL_TEMPLATE.format(year=year)
        try:
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            days = (resp.json() or {}).get("days") or []
        except Exception as e:
            logger.warning("节假日 %s 拉取失败 %s: %s", year, url, str(e)[:120])
            out[year] = 0
            continue
        n = 0
        for d in days:
            try:
                day = datetime.strptime(str(d.get("date")), "%Y-%m-%d").date()
            except ValueError:
                continue
            kind = KIND_WORKDAY if d.get("isOffDay") is False else KIND_HOLIDAY
            row = db.query(HolidayCalendar).filter(HolidayCalendar.day == day).first()
            if row is None:
                row = HolidayCalendar(day=day)
                db.add(row)
            row.kind = kind
            row.name = str(d.get("name") or "")[:64]
            row.source = "auto"
            n += 1
        out[year] = n
        logger.info("节假日 %s 同步 %d 条（含调休上班 %d 条）",
                    year, n, sum(1 for d in days if d.get("isOffDay") is False))
    db.commit()
    _kind_cache.clear()
    return out


def need_sync(db: Session) -> bool:
    """本年或次年缺数据 -> 需要同步（每日定时用）。"""
    today = date.today()
    for year in (today.year, today.year + 1):
        has = db.query(HolidayCalendar.day).filter(
            HolidayCalendar.day >= date(year, 1, 1),
            HolidayCalendar.day <= date(year, 12, 31),
        ).first()
        if has is None:
            return True
    return False


def day_kind(db: Session, d: date) -> str:
    """某天的实际作息：workday / rest / holiday（详见模块 docstring）。"""
    if d in _kind_cache:
        return _kind_cache[d]
    row = db.query(HolidayCalendar).filter(HolidayCalendar.day == d).first()
    if row is not None:
        kind = KIND_WORKDAY if row.kind == KIND_WORKDAY else (
            KIND_HOLIDAY if row.kind == KIND_HOLIDAY else KIND_REST)
    else:
        kind = KIND_REST if d.weekday() >= 5 else KIND_WORKDAY
    _kind_cache[d] = kind
    return kind


def kind_label(kind: str) -> str:
    return {KIND_WORKDAY: "工作日", KIND_REST: "休息日", KIND_HOLIDAY: "法定假日"}.get(kind, kind)


def explain(db: Session, d: date) -> str:
    """给人看的一句话（异常文案里用）：今天算什么日子、依据是什么。"""
    row = db.query(HolidayCalendar).filter(HolidayCalendar.day == d).first()
    if row is not None:
        if row.kind == KIND_WORKDAY:
            return f"{d.isoformat()} 是调休上班日（{row.name or '节假日调休'}，日历自动同步）"
        return f"{d.isoformat()} 是法定假日（{row.name or '节假日'}）"
    wd = "一二三四五六日"[d.weekday()]
    return f"{d.isoformat()} 星期{wd}（按普通{'休息日' if d.weekday() >= 5 else '工作日'}）"
