"""SQLAlchemy 声明基类与时间约定。

**时间约定（重要）**：全库统一存**朴素 UTC 时间**（``datetime`` 不带时区）。
SQLite 不保存时区信息，混用 aware/naive 会在"同一份代码在 SQLite 上对、在
PostgreSQL 上错"这种问题上浪费大量时间。对外输出时由 schema 层统一补 ``Z``
（见 ``app.schemas.common.UtcDatetime``）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: 统一命名约定：让 Alembic 自动生成的约束名稳定、可读
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict[str, Any]:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


def utcnow() -> datetime:
    """朴素 UTC 当前时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
