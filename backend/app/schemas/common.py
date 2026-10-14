"""通用 schema：分页、时间序列化。

（``ErrorResponse`` / ``Pagination`` 曾经在这里，但没有任何路由使用它们——
错误体由 ``app.main`` 的异常处理器直接构造，分页参数用 FastAPI 的 Query 声明。
没人用的模型只会让 OpenAPI 文档里多出两个不存在的契约，所以删掉了。）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, PlainSerializer

T = TypeVar("T")


def to_utc_iso(value: datetime | None) -> str | None:
    """朴素 UTC 时间 → 带 Z 的 ISO8601。

    库里存的是朴素 UTC（见 ``app.db.base``），直接输出会被前端当成本地时间，
    差 8 小时这种问题非常难查，所以在这里统一补上时区标记。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


#: 对外的时间字段类型。
#:
#: 注意 ``return_type`` 写的是 ``str`` 而不是 ``str | None``：这里只需要告诉
#: Pydantic "JSON 里是字符串"，**可空性由字段自己表达**（写 ``UtcDatetime | None``）。
#: 而 return_type 是类定义期就求值的东西，写成联合类型一旦 Pydantic 不接受，
#: 报错会发生在 import app.schemas.common 时，直接把整条 API 链打断——
#: 零收益、非零风险，所以不写。
UtcDatetime = Annotated[
    datetime, PlainSerializer(to_utc_iso, return_type=str, when_used="json")
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class OkResponse(BaseModel):
    ok: bool = True
    message: str | None = None
