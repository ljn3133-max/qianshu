"""链路确认与业务映射 API 的请求模型。"""
from pydantic import BaseModel, Field


class EdgeStatusIn(BaseModel):
    """批量设置边状态（确认/忽略/恢复草稿）。"""
    ids: list[int] = Field(min_length=1, max_length=500)
    status: str = Field(pattern="^(confirmed|ignored|draft)$")
    note: str = Field(default="", max_length=500)


class BusinessIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=500)


class IdentifierIn(BaseModel):
    ident_type: str = Field(pattern="^(host|app_name|db_name|ecs_name|rds_instance|ip|instance)$")
    ident_value: str = Field(min_length=1, max_length=255)
    note: str = Field(default="", max_length=500)
