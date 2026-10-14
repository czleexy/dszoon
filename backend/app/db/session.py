"""数据库会话与引擎管理。

开发/单机默认 SQLite（``aiosqlite``），生产用 PostgreSQL（``asyncpg``）。
SQLite 需要显式打开外键约束，否则 ``ondelete=CASCADE`` 是空话。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from .base import Base

__all__ = ["Database", "get_database", "reset_databases"]


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        options: dict[str, Any] = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                # 内存库必须固定连接，否则每次连接都是一个空库
                options["poolclass"] = StaticPool
        self.engine: AsyncEngine = create_async_engine(url, **options)
        self.sessionmaker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        if url.startswith("sqlite"):
            self._enable_sqlite_pragmas()

    def _enable_sqlite_pragmas(self) -> None:
        """SQLite 必须显式打开这几个 pragma，否则行为会"不符合直觉"。

        * ``foreign_keys=ON``：**不加它 ``ondelete=CASCADE`` 就是空话**，
          删设备时子表会留下孤儿行；
        * ``journal_mode=WAL``：默认的 rollback journal 下，一个写事务会挡住所有
          读写。本项目同时存在"请求会话"和"后台备份任务会话"，实测就撞出过
          ``database is locked``（表现为取消任务后收尾写状态失败、任务永远停在
          running）。WAL 允许"多读一写"，是异步多会话场景的标准配置；
        * ``busy_timeout``：写锁等待时长，显式写出来免得以后被误改。
        """

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    # ------------------------------------------------------------------
    async def create_all(self) -> None:
        from . import models  # noqa: F401  导入以注册到 Base.metadata

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        from . import models  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def has_table(self, name: str) -> bool:
        """库里有没有这张表（用于"该不该自动建表"的判断）。"""
        async with self.engine.connect() as conn:
            return bool(
                await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(name))
            )

    async def missing_schema(self) -> list[str]:
        """模型里有、但库里没有的表/列（形如 ``devices.group_id``）。

        **为什么必须查这个**：``create_all`` 只建"缺失的表"，**不会给已有的表加列**。
        所以升级代码后沿用旧库，会出现最难查的一类症状——应用启动完全正常，
        一打开设备页却整页 500（``no such column: devices.group_id``）；
        定时任务则会报 ``no such column: backup_tasks.schedule_id``。
        在启动时查一次并直接说清楚缺什么，比让使用者去翻日志快得多。
        """
        from . import models  # noqa: F401  导入以把全部模型注册进 Base.metadata

        expected = Base.metadata.tables

        def _collect(sync_conn: Any) -> list[str]:
            inspector = inspect(sync_conn)
            existing_tables = set(inspector.get_table_names())
            problems: list[str] = []
            for name, table in expected.items():
                if name not in existing_tables:
                    problems.append(name)
                    continue
                columns = {column["name"] for column in inspector.get_columns(name)}
                problems.extend(
                    f"{name}.{column.name}"
                    for column in table.columns
                    if column.name not in columns
                )
            return sorted(problems)

        async with self.engine.connect() as conn:
            return await conn.run_sync(_collect)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - 健康检查不该抛异常
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session


_databases: dict[str, Database] = {}


def get_database(url: str, *, echo: bool = False) -> Database:
    """按 URL 复用 Database 实例（同一进程内只有一个引擎）。"""
    database = _databases.get(url)
    if database is None:
        database = Database(url, echo=echo)
        _databases[url] = database
    return database


def reset_databases() -> None:
    """仅测试用：清掉实例缓存（不会 dispose，测试自建实例更干净）。"""
    _databases.clear()
