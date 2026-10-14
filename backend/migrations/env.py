"""Alembic 迁移环境（异步引擎）。

几个容易踩的点，都处理了：

* **连接串从应用配置读**，不写在 alembic.ini 里——否则很容易出现"迁移改的是 A 库、
  应用连的是 B 库"。
* URL 里的 ``%`` 必须转义成 ``%%``（configparser 会做插值，密码里带 ``%`` 时会炸）。
* SQLite 需要 ``render_as_batch=True``，否则 autogenerate 生成的 ALTER 语句
  SQLite 根本执行不了（它不支持大部分 ALTER TABLE）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
from app.db import models  # noqa: F401  导入以把模型注册进 Base.metadata

config = context.config
if config.config_file_name is not None:
    # ★ disable_existing_loggers=False 是必须的：fileConfig 的默认值是 True，
    #   它会把 alembic.ini 里**没有列出**的 logger 全部禁用——包括 app.*。
    #   后果很隐蔽：
    #   * 进程内调用 alembic（测试就是这么做的）会让此后应用的所有日志静默消失，
    #     于是"该报警"的断言假失败、"该闭嘴"的断言假通过；
    #   * 而 alembic 命令行是独立进程，所以在真实使用中根本看不出来。
    #   第一次真实运行就踩到了：先跑迁移测试、再跑启动提示测试，日志断言全空。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = get_settings().database_url
    # configparser 会对 % 做插值，密码里带 % 时不转义会直接报错
    return url.replace("%", "%%")


def run_migrations_offline() -> None:
    """生成 SQL 脚本而不连库（alembic upgrade head --sql）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite 不支持大部分 ALTER TABLE，batch 模式会用"建新表+拷数据"绕过去
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
