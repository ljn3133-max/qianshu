"""行为画像：记录正常构成并识别变化。

本表存"正常是什么样"的记忆：
- samples  日构成快照序列（top 成员+占比），滚动窗口
- baseline 学习出的正常构成（成员+占比区间+出现频次）
- 检测：最新快照 vs baseline 的**构成漂移**（top1 变了/陌生成员进 top3/占比突变），
  事件 type=profile_drift 进 cost_anomalies 生命周期
- 人工确认（accept）=把当前构成为新常态，吸收进画像
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class BehaviorProfile(Base):
    __tablename__ = "behavior_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))          # process/cdn_traffic/bill_product/slb_uri/log_app
    subject: Mapped[str] = mapped_column(String(128), default="")   # 实例ID或 *
    label: Mapped[str] = mapped_column(String(128), default="")    # 中文名
    learned: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否已形成正常画像
    status: Mapped[str] = mapped_column(String(16), default="observing")  # observing/normal/drift
    samples: Mapped[str] = mapped_column(Text, default="[]")     # JSON: [{date, sample_kind:"daily", members, total, snapshots:[...]}]
    baseline: Mapped[str] = mapped_column(Text, default="{}")    # JSON: {members, top1, learned_at, n}
    latest: Mapped[str] = mapped_column(Text, default="{}")      # JSON: 最新快照
    drift_note: Mapped[str] = mapped_column(Text, default="")    # 最新漂移说明（人话）
    # 身份引用：confirmed_by=username 快照，confirmed_by_id=users.id
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # ----「已知道」状态+分级静默（2026-11-04，旧库由 app.core.migrate 幂等补列） ----
    known_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)   # 最近一次「知道了」
    silence_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 静默截止（到期恢复提醒）
    notify_state: Mapped[str] = mapped_column(String(16), default="")   # ''=正常流转 / silenced=静默中 / remind=静默到期恢复提醒
    # ---- 快层事件样本：JSON [{ts, sample_kind:"event", source, ref_id, title, members, total}] ----
    event_samples: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
