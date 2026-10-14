"""API 测试环境构建（不含 pytest 夹具，避免测试文件互相依赖 conftest）。

要点：

* 用**临时文件 SQLite**（不是 ``:memory:``）：应用在后台任务里会另开 session，
  内存库每个连接一个独立数据库，会把测试变成"时好时坏"。
* 用 ``httpx.ASGITransport`` 而不是 ``TestClient``：前者跑在测试自己的事件循环里，
  后台备份任务（``asyncio.create_task``）才能被测试观察到。
* ASGITransport **不会触发 lifespan**，所以建表要显式调 ``prepare_database``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.crypto import generate_key
from app.db.session import Database
from app.main import create_app, prepare_database
from tests.support.fake_device import FakeSessionFactory

__all__ = [
    "ADMIN_PASSWORD",
    "FIXTURES",
    "VIEWER_PASSWORD",
    "Env",
    "build_env",
    "fixture_text",
]

ADMIN_PASSWORD = "admin-12345"
VIEWER_PASSWORD = "viewer-12345"

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
HUAWEI_CONFIG = "huawei/vrp8/display_current-configuration.txt"
HUAWEI_VERSION = "huawei/vrp8/display_version.txt"
VPR5_CONFIG = "huawei/vrp5/display_current-configuration.txt"
VPR5_VERSION = "huawei/vrp5/display_version.txt"


def fixture_text(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


#: 模拟"配置被改过"时插入的那一行（所有变更设备共用同一行，便于断言正文去重）
CONFIG_CHANGE_LINE = "ip route-static 10.99.0.0 255.255.255.0 10.10.10.254"


def with_config_change(base: str, *, line: str = CONFIG_CHANGE_LINE) -> str:
    """把一行变更插在**结尾终止符之前**（``return`` / ``end``），而不是追加到末尾。

    ★ 为什么不能直接追加：驱动的完整性校验里有 ``must_end_with``（华为要求末行是
    ``return``，锐捷是 ``end``）。追加到终止符之后，整份正文的末行就变成了新那一行，
    于是被判成 ``bad_terminator``（严重级别 error）→ 采集失败、状态 ``integrity_error``。
    第一次全量运行就是这么红的：200 台里"被改过的 20 台"全部失败，而现在才明白
    不是代码错、是"变更"造得不像真配置（真机也不可能有 return 之后的配置行）。

    三个厂商的夹具都以 ``return`` 或 ``end`` 结尾，找不到时退回追加（不静默改变语义）。
    """
    lines = base.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() in ("return", "end"):
            lines.insert(index, line)
            return "\n".join(lines) + "\n"
    return base + line + "\n"


@dataclass
class Env:
    client: AsyncClient
    settings: Settings
    database: Database
    factory: FakeSessionFactory
    app: Any = None

    @property
    def runner(self) -> Any:
        return self.app.state.runner

    async def login(self, username: str = "admin", password: str = ADMIN_PASSWORD) -> dict:
        response = await self.client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def auth(self, username: str = "admin", password: str = ADMIN_PASSWORD) -> dict:
        data = await self.login(username, password)
        return {"Authorization": f"Bearer {data['access_token']}"}

    async def post(self, url: str, headers: dict, **kwargs):
        return await self.client.post(url, headers=headers, **kwargs)

    async def get(self, url: str, headers: dict, **kwargs):
        return await self.client.get(url, headers=headers, **kwargs)


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        secret_key=generate_key(),
        # JWT 密钥必须 ≥32 字节：短密钥会让 PyJWT 抛 InsecureKeyLengthWarning
        # （一次全量测试能刷出 87 条警告），也确实是安全问题
        jwt_secret="test-jwt-secret-please-change-0123456789abcdef",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        backup_concurrency=4,
        backup_precheck=False,
        keep_session_logs=True,
        cors_origins=[],
    )


async def build_env(tmp_path: Path) -> tuple[Env, AsyncClient]:
    settings = build_settings(tmp_path)
    settings.ensure_dirs()
    database = Database(settings.database_url)
    factory = FakeSessionFactory()
    app = create_app(settings=settings, database=database, session_factory=factory)
    await prepare_database(database, settings)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    return (
        Env(client=client, settings=settings, database=database, factory=factory, app=app),
        client,
    )
