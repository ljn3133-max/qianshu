"""AI 场景×模型绑定。
每场景可独立绑定模型连接（ai_settings）；未绑定/未启用 → 兜底第一个启用连接。"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, Integer, String

from app.core.db import Base


class AiScenarioModel(Base):
    __tablename__ = "ai_scenario_models"

    scenario = Column(String(32), primary_key=True)   # ask / inspection / diagnosis / copilot
    setting_id = Column(Integer, default=0)           # 绑定 ai_settings.id
    enabled = Column(Boolean, default=True)           # False=场景入口隐藏
