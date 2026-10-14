"""迁移可用性与 schema 一致性测试。

为什么必须测这个：开发/测试环境走 ``create_all``（快），生产只走 Alembic。两条路
一旦分叉，症状是"开发机上一切正常，客户环境一开门就少一张表/少一个索引"，而且
往往要到某个功能被用到时才炸。这里做两件事：

1. ``alembic upgrade head`` 能在**空库**上真正跑通（全新安装路径）；
2. 迁移建出来的表与索引和 ``Base.metadata`` **完全一致**（没有漂移）。

注意：迁移读的是 ``Settings.database_url``（不写死在 alembic.ini 里，避免"迁移
连一个库、应用连另一个库"），所以这里用环境变量把它指到临时库，并清掉
``get_settings`` 的 lru_cache——不清缓存的话改环境变量不会生效，测试会去动真实库。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.core.config import Settings, get_settings
from app.core.crypto import generate_key
from app.db.base import Base

BACKEND = Path(__file__).resolve().parents[2]


def test_initial_migration_matches_models(tmp_path: Path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "migrated.db"
    monkeypatch.setenv("DSZOON_DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DSZOON_DATA_DIR", str(tmp_path / "data"))
    # 固定密钥：不给的话 Settings 会自动生成 ``.secrets.json`` 落盘（测试里不该写文件）
    monkeypatch.setenv("DSZOON_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    try:
        config = Config(str(BACKEND / "alembic.ini"))
        # alembic.ini 里 script_location 是相对路径（按 CWD 解析），测试可能从
        # 任意目录调用 pytest，这里显式改成绝对路径
        config.set_main_option("script_location", str(BACKEND / "migrations"))
        command.upgrade(config, "head")
    finally:
        # 一定要清：缓存里留着临时库的设置，会污染其它用例
        get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "alembic_version" in tables, "迁移没有登记版本号，说明 upgrade 没真正执行"

    missing = set(Base.metadata.tables) - tables
    assert not missing, f"迁移缺少这些表：{sorted(missing)}"

    # 索引也要一致：漏索引不会让测试变红，只会让规模上来后变慢
    for table in Base.metadata.tables.values():
        expected = {index.name for index in table.indexes if index.name}
        actual = {index["name"] for index in inspector.get_indexes(table.name)}
        assert not (expected - actual), f"{table.name} 缺少索引：{sorted(expected - actual)}"

    engine.dispose()


def test_alembic_ini_is_ascii_only() -> None:
    """★ ``alembic.ini`` 必须是纯 ASCII。

    这不是洁癖：alembic 读这个文件走的是 ``alembic.util.compat.read_config_parser``，
    它把编码**写死成 ``encoding="locale"``**——中文 Windows 上就是 GBK。文件里只要有
    一个中文注释，``alembic upgrade head`` / ``revision`` / ``current`` 就全部抛
    ``UnicodeDecodeError: 'gbk' codec can't decode byte 0xa5``，而报错信息里完全看不出
    是注释惹的祸（第一次真实运行就踩到了这个）。``logging.fileConfig`` 读同一个文件、
    也是 locale 编码，所以连日志配置一起带崩。

    同一理由：``migrations/script.py.mako`` 也保持纯 ASCII（``alembic revision`` 会读它）。
    """
    for relative in ("alembic.ini", "migrations/script.py.mako"):
        raw = (BACKEND / relative).read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AssertionError(
                f"{relative} 含非 ASCII 字符（{exc}）：alembic 用 locale 编码读它，"
                "中文注释会让它在中文 Windows 上直接读不出来。请把注释改成英文。"
            ) from exc


def test_migrations_directory_has_the_baseline() -> None:
    """基线迁移必须在版本目录里（否则 ``alembic upgrade head`` 是个空操作，
    全新安装的库一张表都没有，而这一点不会在别的测试里暴露）。"""
    versions = BACKEND / "migrations" / "versions"
    files = sorted(path.name for path in versions.glob("*.py"))
    assert any("initial" in name for name in files), f"没有找到基线迁移：{files}"


async def test_startup_requires_migrations_when_auto_create_is_off(tmp_path: Path) -> None:
    """生产（Alembic 管表）时启动**不能**偷偷 create_all，缺表要立刻报错。

    否则"应用自己补表"会让迁移历史与真实 schema 分叉：某天 alembic 报
    "表已存在"或"列不存在"时，几乎查不出是哪一步跑偏的。
    """
    import pytest

    from app.core.config import Settings
    from app.db.session import Database
    from app.main import prepare_database

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'guard.db').as_posix()}")
    base = dict(
        database_url=database.url,
        data_dir=tmp_path / "data",
        secret_key=generate_key(),
        auto_create_schema=False,
    )
    try:
        with pytest.raises(RuntimeError) as error:
            await prepare_database(database, Settings(**base))
        assert "alembic upgrade head" in str(error.value), "报错必须给出确切的解决命令"
        assert not await database.has_table("users"), "报错路径下不该顺手把表建出来"

        # 允许自动建表时（开发/单机）应当正常建表并创建初始管理员
        settings = Settings(**{**base, "auto_create_schema": True, "admin_password": "guard-12345"})
        await prepare_database(database, settings)
        assert await database.has_table("users")
    finally:
        await database.dispose()


def _settings_for(database_url: str, tmp_path: Path, **overrides) -> Settings:
    return Settings(
        database_url=database_url,
        data_dir=tmp_path / "data",
        secret_key=generate_key(),
        admin_password="drift-12345",
        **overrides,
    )


async def test_missing_schema_reports_missing_columns_and_tables(tmp_path: Path) -> None:
    """确定性地验证"漂移检测"本身（不依赖 SQLite 能不能删列）。

    下面那条测试要真的删掉 ``devices.group_id``，而 SQLite 不允许删除"被索引或外键
    引用"的列，在某些版本上只能跳过。这条换一种做法：手工造一个**残缺**的库，直接查
    ``Database.missing_schema()`` 报了哪些缺失项——它才是启动期那道守卫的判定函数。
    """
    from sqlalchemy import text

    from app.db.session import Database

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'partial.db').as_posix()}")
    try:
        async with database.engine.begin() as conn:
            # devices 只建了 id/name：缺 group_id、host、vendor……，其余表整张都没有
            await conn.execute(text("CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT)"))
        missing = set(await database.missing_schema())
        assert "devices.group_id" in missing, f"没看出缺列：{sorted(missing)[:10]}"
        assert "devices.host" in missing
        # 其余模型的表整张都不存在，必须全部报出来。
        # ★ 表名从模型里取、不要手写：上一版手写了 "retention_policy"，而实际建的表叫
        #   "retention_policies"（复数），于是这条断言自己红了 —— 手写模型里的名字
        #   等于给自己埋雷，测试应该以模型为准。
        expected_tables = set(Base.metadata.tables) - {"devices"}
        assert expected_tables <= missing, f"漏报的表：{sorted(expected_tables - missing)}"

        # 完整建表之后不该再报任何缺失。
        # 注意要先删掉那张残缺的 devices：create_all **只建缺失的表**，不会补列，
        # 留着它的话 devices.* 仍然会一直报缺失。
        async with database.engine.begin() as conn:
            await conn.execute(text("DROP TABLE devices"))
        await database.create_all()
        assert await database.missing_schema() == []
    finally:
        await database.dispose()


async def test_startup_detects_missing_column_in_existing_table(tmp_path: Path) -> None:
    """★ 旧库 + 新代码：必须在**启动期**报错，并说清缺什么、怎么修。

    这是升级时最容易踩一次的坑：``create_all`` 只补建缺失的表，**不会给已有表加列**。
    真实症状是"应用启动完全正常，一打开设备页整页 500（no such column:
    devices.group_id）"，几乎不可能从日志里一眼看出来。

    注：本用例需要真的删掉一列，而 SQLite 不允许删被索引/外键引用的列，所以在本机
    可能被跳过——判定逻辑由上面那条 ``test_missing_schema_reports_missing_columns_and_tables``
    确定性地覆盖。
    """
    import pytest
    from sqlalchemy import text

    from app.db.session import Database
    from app.main import prepare_database

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'drift.db').as_posix()}")
    try:
        await database.create_all()
        try:
            async with database.engine.begin() as conn:
                # SQLite 不允许删除"被索引引用"的列，而 devices.group_id 带 index=True，
                # 所以要先删索引（没有索引时 IF EXISTS 直接跳过）
                await conn.execute(text("DROP INDEX IF EXISTS ix_devices_group_id"))
                await conn.execute(text("ALTER TABLE devices DROP COLUMN group_id"))
        except Exception as exc:  # noqa: BLE001 - 老 SQLite 不支持 DROP COLUMN
            pytest.skip(f"当前 SQLite 不支持 DROP COLUMN，跳过列级漂移检查：{exc}")

        with pytest.raises(RuntimeError) as error:
            await prepare_database(database, _settings_for(database.url, tmp_path))
        message = str(error.value)
        assert "devices.group_id" in message, "必须点名缺哪一列"
        assert "create_all" in message, "要点明原因，否则使用者会以为是代码问题"
        assert "删掉数据库文件" in message, "要给出开发环境的确切出路"
    finally:
        await database.dispose()


async def test_startup_detects_missing_table_when_schema_is_managed(tmp_path: Path) -> None:
    """schema 交给 Alembic 管（auto_create_schema=false）时，缺表要报错并指向迁移。"""
    import pytest
    from sqlalchemy import text

    from app.db.session import Database
    from app.main import prepare_database

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'drift2.db').as_posix()}")
    try:
        await database.create_all()
        async with database.engine.begin() as conn:
            await conn.execute(text("DROP TABLE notification_logs"))

        settings = _settings_for(database.url, tmp_path, auto_create_schema=False)
        with pytest.raises(RuntimeError) as error:
            await prepare_database(database, settings)
        message = str(error.value)
        assert "notification_logs" in message
        assert "alembic upgrade head" in message, "生产环境的出路是跑迁移"
    finally:
        await database.dispose()
