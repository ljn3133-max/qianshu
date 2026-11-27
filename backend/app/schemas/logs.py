"""日志查询 API 的请求模型。"""
from pydantic import BaseModel, Field


class SavedQueryCreate(BaseModel):
    # 日志查询多源统一：source=日志源名（空=旧行语义 SLS），
    # target=CLS 主题/ES 索引；project/logstore 仍为 SLS 目标（旧客户端照旧传）
    name: str = Field(min_length=1, max_length=64)
    project: str = Field(default="", max_length=128)
    logstore: str = Field(default="", max_length=128)
    source: str = Field(default="", max_length=64)
    target: str = Field(default="", max_length=256)
    query: str = Field(default="", max_length=4000)
    time_range: str = Field(default="1h", pattern="^(15m|30m|1h|4h|12h|24h|3d|7d)$")
