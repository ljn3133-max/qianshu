from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LinkEdge(Base):
    """链路拓扑边，支持草稿与确认状态。

    一行 = 一条「域名 -> 转发规则/路径 -> 后端」的规则边（带证据量 + 健康指标）。
    真实拓扑是"域名下按 URL/路径/转发规则分流到不同后端"，旧粒度
    （host × upstream）无法区分定制路由。2026-11-04 后的边带规则/路径维度，
    同域名多后端按规则拆成多条规则边；该日期用于区分迁移前的旧数据口径：
    - chain: outer(外层SLB) / inner(内层SLB)
    - host: SLB 日志 http_host（域名）
    - path_pattern: 路由/路径前缀（日志 request_uri 首段，如 /api、/；
      云配置规则边为规则 URL 归一前缀，如 /api）。旧数据迁移后为空串=「全部路径」
    - upstream_addr: SLB 日志 upstream_addr（后端 IP[:port]）
    - rule_name: 命中的云转发规则名（云配置 DescribeRules/DescribeListeners 规则），
      没命中为空（边只有日志推断的路径）
    - rule_source: log(日志流量实证) / cloud(云配置有规则但 24h 无流量)
    - ecs_name: 按内网 IP 关联到的 ECS 资产名（关联不上为空）
    - path_summary: 该规则边内的二级路径 Top 计数（JSON 文本，证据）
    - app_name: 按应用日志 AppName 匹配到的应用名（匹配不上为空）
    - req_count / cnt_4xx / cnt_5xx: 近24h 请求计数与状态码分布（证据+健康）
    - rt_avg_ms: 近24h 平均后端响应时间（毫秒）
    - ecs_cpu: 后端 ECS 近24h CPU 峰值（%，VM）
    - app_error_cnt: 匹配应用近24h ERROR 日志数
    - status: draft(草稿) / confirmed(已确认) / ignored(已忽略)
    重新生成草稿会覆盖 draft，但 confirmed/ignored 的证据字段会刷新、状态保留。
    """

    __tablename__ = "link_edges"
    __table_args__ = (
        UniqueConstraint("chain", "host", "path_pattern", "upstream_addr",
                         name="uq_link_edge"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(16), index=True)      # outer / inner
    host: Mapped[str] = mapped_column(String(255), index=True)
    upstream_addr: Mapped[str] = mapped_column(String(64))          # 192.168.10.80 或 192.168.96.90:9012
    path_pattern: Mapped[str] = mapped_column(String(255), default="")   # 转发规则/路径前缀（''=旧数据全路径）
    rule_name: Mapped[str] = mapped_column(String(255), default="")      # 云转发规则名（未命中为空）
    rule_source: Mapped[str] = mapped_column(String(16), default="log")  # log=流量实证 / cloud=仅配置无流量
    ecs_name: Mapped[str] = mapped_column(String(255), default="")
    app_name: Mapped[str] = mapped_column(String(255), default="")
    req_count: Mapped[int] = mapped_column(Integer, default=0)      # 证据：近24h请求数
    cnt_4xx: Mapped[int] = mapped_column(Integer, default=0)        # 近24h 4xx 数
    cnt_5xx: Mapped[int] = mapped_column(Integer, default=0)        # 近24h 5xx 数
    rt_avg_ms: Mapped[float] = mapped_column(Float, default=0.0)   # 近24h 平均 RT（ms）
    ecs_cpu: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 后端 ECS 24h CPU 峰值%
    app_error_cnt: Mapped[int] = mapped_column(Integer, default=0)  # 匹配应用 24h ERROR 日志数
    path_summary: Mapped[str] = mapped_column(Text, default="")    # JSON: [{path, cnt}]
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    note: Mapped[str] = mapped_column(String(500), default="")      # 人工备注
    # 身份引用：confirmed_by=username 快照，confirmed_by_id=users.id
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(64), default="")
    confirmed_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LinkEdge {self.chain} {self.host} -> {self.upstream_addr} [{self.status}]>"
