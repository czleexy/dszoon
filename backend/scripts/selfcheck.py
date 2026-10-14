#!/usr/bin/env python3
"""一键自检：环境 → 语法 → 导入 → 建库 → 端到端备份 → P2 运营链路。

**为什么要有这个脚本**：`pytest` 一次报 20 个失败时，最花时间的是分辨
"依赖没装好"、"某个模块 import 就炸了"、"建表失败"、还是"接口逻辑错"。
这里把这几层拆成独立步骤逐级验证，每一步失败都能直接指出位置。

用法（在 backend/ 目录下执行）::

    python scripts/selfcheck.py

退出码 0 = 全部通过；非 0 = 第几步失败（见输出）。
"""

from __future__ import annotations

import asyncio
import compileall
import importlib
import importlib.util
import pkgutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

_failures: list[str] = []


def ok(message: str) -> None:
    print(f"{GREEN}  [OK]{RESET} {message}")


def fail(message: str) -> None:
    print(f"{RED}[FAIL]{RESET} {message}")
    _failures.append(message)


def warn(message: str) -> None:
    print(f"{YELLOW}[WARN]{RESET} {message}")


def heading(index: int, title: str) -> None:
    print(f"\n=== {index}. {title} ===")


# --------------------------------------------------------------------------
def step_environment() -> None:
    heading(1, "运行环境")
    print(f"  解释器：{sys.executable}")
    print(f"  Python：{sys.version.split()[0]}")
    print(f"  工作目录：{Path.cwd()}")

    # 采集层硬依赖
    for label, module in (("PyYAML", "yaml"), ("asyncssh", "asyncssh")):
        if importlib.util.find_spec(module) is None:
            fail(f"缺少采集层依赖 {label}")
        else:
            ok(f"{label} 已安装")

    # Web 层依赖（缺了也能跑采集，但接口起不来）
    web_modules = (
        ("fastapi", "FastAPI"),
        ("sqlalchemy", "SQLAlchemy"),
        ("aiosqlite", "aiosqlite"),
        ("pydantic_settings", "pydantic-settings"),
        ("jwt", "PyJWT"),
        ("cryptography", "cryptography"),
        # P2 新增：cron 表达式的解析与下次执行时间都走它。缺了它，
        # services/schedules 会在导入期抛错，而它被路由导入 → 整个 API 层
        # （含全部 API 测试）在收集阶段就崩，看起来像"改坏了核心代码"。
        ("apscheduler", "APScheduler"),
    )
    missing = [label for module, label in web_modules if importlib.util.find_spec(module) is None]
    if missing:
        fail("缺少 Web 依赖：" + "、".join(missing) + '（pip install -e ".[dev,web]"）')
    else:
        ok("Web 依赖齐全")

    if importlib.util.find_spec("httpx") is None:
        warn("未安装 httpx，第 5 步（端到端）会跳过。pip install -e \".[dev]\"")


def step_syntax() -> None:
    heading(2, "语法编译（compileall）")
    for directory in ("app", "tests", "scripts"):
        target = ROOT / directory
        if not target.is_dir():
            warn(f"目录不存在：{directory}")
            continue
        if compileall.compile_dir(str(target), quiet=2, force=True):
            ok(f"{directory}/ 全部编译通过")
        else:
            fail(f"{directory}/ 存在语法错误（见上方输出）")


def step_imports() -> None:
    heading(3, "模块导入（能抓出名字拼错、循环导入）")
    import app

    failed: list[tuple[str, str]] = []

    def _onerror(name: str) -> None:
        # walk_packages 进入子包失败时不会 yield，而是回调这里；
        # 不给 onerror 的话它会直接把异常抛出来，脚本就崩了，看不到是哪一层坏的
        failed.append((name, "子包导入失败（详见上方 traceback）"))

    for module_info in pkgutil.walk_packages(app.__path__, prefix="app.", onerror=_onerror):
        try:
            importlib.import_module(module_info.name)
        except Exception as exc:  # noqa: BLE001
            failed.append((module_info.name, f"{type(exc).__name__}: {exc}"))

    if failed:
        for name, message in failed:
            fail(f"导入 {name} 失败 → {message}")
    else:
        ok("app.* 全部模块导入成功")


def step_registry_and_drivers() -> None:
    heading(4, "驱动注册表与清洗层")
    from app.collector import textutil
    from app.collector.registry import get_registry

    registry = get_registry()
    names = registry.names()
    if names == ["h3c", "huawei", "ruijie"]:
        ok(f"已加载驱动：{', '.join(names)}")
    else:
        fail(f"驱动列表不符合预期：{names}")

    broken: list[str] = []
    for vendor in names:
        driver = registry.get(vendor)
        # 泛化提示符正则必须能编译：历史上这里生成过括号不配对的非法正则，
        # 后果是每台设备每次连接都直接 PatternError
        try:
            driver.prompt_re(None)
            driver.prompt_re("TEST-DEVICE-01")
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{vendor}: {type(exc).__name__}: {exc}")
    if broken:
        for item in broken:
            fail(f"提示符正则无法编译 → {item}")
    else:
        ok("三家驱动的提示符正则（泛化/严格）均可编译")

    checks = {
        "分页符在结尾应被识别": textutil.has_more_token("x\n  ---- More ----"),
        "历史页的分页符不应被识别": not textutil.has_more_token("  ---- More ----\ny"),
        "分页符清理不应插入空行": textutil.strip_more_tokens("vlan 10\n  ---- More ----\nvlan 20")
        == "vlan 10\nvlan 20",
        "ANSI 应被剥离": textutil.strip_ansi("\x1b[42D<SW>") == "<SW>",
        "命令回显应被剥离": textutil.drop_echo("<SW>display version\nbody", "display version")
        == "body",
    }
    for label, passed in checks.items():
        if passed:
            ok(label)
        else:
            fail(f"文本清洗行为异常：{label}")


def step_tooling() -> None:
    heading(5, "命令行与迁移配置")

    # alembic.ini 必须能被 configparser 解析，并且值里不能有"单个 %"：
    # 单 % 会被当成插值引用（如 %(year)d），直接抛 InterpolationMissingOptionError，
    # 报错信息完全不提模板两个字。这个坑我在写 ini 时真踩过，所以固化成检查。
    from configparser import ConfigParser

    parser = ConfigParser()
    ini_path = ROOT / "alembic.ini"
    if not ini_path.is_file():
        fail("缺少 alembic.ini")
    else:
        try:
            if not parser.read(ini_path, encoding="utf-8"):
                fail("alembic.ini 读取为空")
            else:
                for option in ("script_location", "file_template"):
                    parser.get("alembic", option)  # 触发插值
                ok("alembic.ini 可解析（% 转义正确）")
        except Exception as exc:  # noqa: BLE001
            fail(f"alembic.ini 解析失败：{type(exc).__name__}: {exc}")

    # migrations/env.py 必须在位、alembic 必须已安装（缺一 alembic 命令全废）
    try:
        import alembic  # noqa: F401
    except ImportError:
        fail('未安装 alembic（pip install -e ".[dev,web]"）')
    else:
        env_file = ROOT / "migrations" / "env.py"
        if env_file.is_file():
            ok("alembic 已安装，migrations/env.py 就位")
        else:
            fail("缺少 migrations/env.py")

    # CLI 子命令参数能正常解析（argparse 写错会在用户敲命令时才炸）
    try:
        from app.cli import build_parser
    except Exception as exc:  # noqa: BLE001
        fail(f"无法导入 app.cli.build_parser：{type(exc).__name__}: {exc}")
        return

    samples = (
        ["init-db"],
        ["gen-key"],
        ["stats"],
        ["list-devices", "--vendor", "huawei"],
        ["create-user", "ops", "--role", "operator"],
        ["reset-password", "admin"],
        ["backup", "--all"],
        ["backup", "--device-id", "1", "--retries", "2"],
    )
    broken: list[str] = []
    for argv in samples:
        try:
            build_parser().parse_args(argv)
        except SystemExit:
            broken.append(" ".join(argv))
    if broken:
        for item in broken:
            fail(f"CLI 参数解析失败：{item}")
    else:
        ok(f"CLI 子命令解析正常（{len(samples)} 组参数）")


async def step_database(tmp_dir: Path) -> None:
    heading(6, "建库与初始管理员")
    from sqlalchemy import func, select

    from app.core.config import Settings
    from app.core.crypto import generate_key
    from app.db.models import User
    from app.db.session import Database
    from app.main import prepare_database
    from app.services import audit

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_dir / 'selfcheck.db').as_posix()}",
        data_dir=tmp_dir / "data",
        secret_key=generate_key(),
        jwt_secret="selfcheck-jwt-secret-0123456789abcdef",
        admin_username="admin",
        admin_password="admin-12345",
        cors_origins=[],
    )
    settings.ensure_dirs()
    database = Database(settings.database_url)
    try:
        try:
            await prepare_database(database, settings)
        except Exception as exc:  # noqa: BLE001
            fail(f"建表/建管理员失败：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            return
        ok("建表成功，初始管理员已创建")

        async with database.session() as session:
            count = await session.scalar(select(func.count()).select_from(User))
        if count == 1:
            ok("users 表可查询，管理员记录数为 1")
        else:
            fail(f"users 表记录数异常：{count}")

        async with database.session() as session:
            await audit.record(session, action="auth.login", username="admin")
            await session.commit()
        ok("审计写入正常")
    finally:
        # 必须关掉连接：Windows 上临时目录清理时如果 SQLite 文件还被占用会报错，
        # 那个报错会盖掉真正的失败原因
        await database.dispose()


async def step_end_to_end(tmp_dir: Path) -> None:
    heading(7, "端到端：登录 → 建设备 → 备份 → 落库 → diff")
    if importlib.util.find_spec("httpx") is None:
        warn("跳过（未安装 httpx）")
        return

    from httpx import ASGITransport, AsyncClient

    from app.core.config import Settings
    from app.core.crypto import generate_key
    from app.db.session import Database
    from app.main import create_app, prepare_database
    from tests.support.fake_device import make_fake_device

    fixtures = ROOT / "tests" / "fixtures"
    config_text = (fixtures / "huawei" / "vrp8" / "display_current-configuration.txt").read_text(
        encoding="utf-8"
    )
    version_text = (fixtures / "huawei" / "vrp8" / "display_version.txt").read_text(encoding="utf-8")

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_dir / 'e2e.db').as_posix()}",
        data_dir=tmp_dir / "e2e-data",
        secret_key=generate_key(),
        jwt_secret="selfcheck-jwt-secret-0123456789abcdef",
        admin_username="admin",
        admin_password="admin-12345",
        backup_precheck=False,
        backup_concurrency=2,
        cors_origins=[],
    )
    settings.ensure_dirs()
    database = Database(settings.database_url)

    class _Factory:
        """最小假设备工厂：按主机名返回固定回显。"""

        def __init__(self) -> None:
            self.device = make_fake_device(
                vendor="huawei",
                hostname="HW-CORE-01",
                config_text=config_text,
                version_text=version_text,
            )

        def __call__(self, config, prompt_re):
            from tests.support.fake_device import FakeSession

            return FakeSession(config, prompt_re, self.device)

    app = create_app(settings=settings, database=database, session_factory=_Factory())
    await prepare_database(database, settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://selfcheck") as client:
        # 登录
        response = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin-12345"}
        )
        if response.status_code != 200:
            fail(f"登录失败：{response.status_code} {response.text[:300]}")
            await database.dispose()
            return
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        ok("登录成功，拿到令牌")

        # 凭据 + 设备
        response = await client.post(
            "/api/v1/credentials",
            headers=headers,
            json={"name": "自检凭据", "username": "netadmin", "password": "pw-123456"},
        )
        if response.status_code != 200:
            fail(f"创建凭据失败：{response.status_code} {response.text[:300]}")
            await database.dispose()
            return
        profile_id = response.json()["id"]
        if "password" in response.text and "pw-123456" in response.text:
            fail("凭据接口泄露了口令明文！")
        else:
            ok("凭据创建成功且响应里没有明文口令")

        response = await client.post(
            "/api/v1/devices",
            headers=headers,
            json={
                "name": "HW-CORE-01",
                "host": "10.0.0.1",
                "vendor": "HW",
                "credential_profile_id": profile_id,
            },
        )
        if response.status_code != 200:
            fail(f"创建设备失败：{response.status_code} {response.text[:300]}")
            await database.dispose()
            return
        device = response.json()
        if device["vendor"] != "huawei":
            fail(f"厂商别名未归一化：{device['vendor']}")
        else:
            ok("设备创建成功，厂商别名 HW → huawei")
        device_id = device["id"]

        # ★ 连接测试走的也是采集层，但入口与备份任务不同（直接调 probe_device）。
        #   这一步专门验证"注入的会话工厂有没有被所有入口都用上"——
        #   曾经这里漏传 session_factory，导致连接测试绕过注入去发真实 SSH 连接，
        #   在测试里表现为 15 秒超时后返回 unreachable。
        probe = await client.post(f"/api/v1/devices/{device_id}/test", headers=headers)
        if probe.status_code != 200 or not probe.json().get("ok"):
            fail(f"连接测试失败：{probe.status_code} {probe.text[:300]}")
        else:
            facts = probe.json().get("facts") or {}
            if facts.get("model") != "S5731-H24T4XC":
                fail(f"连接测试未回填型号：{facts}")
            else:
                ok(f"连接测试通过（设备名 {probe.json().get('hostname')}，型号 {facts['model']}）")

        # 发起备份并等待
        response = await client.post(
            "/api/v1/backups", headers=headers, json={"device_ids": [device_id]}
        )
        if response.status_code != 200:
            fail(f"发起备份失败：{response.status_code} {response.text[:300]}")
            await database.dispose()
            return
        task_id = response.json()["id"]

        detail = None
        for _ in range(200):  # 最多等 20 秒
            await asyncio.sleep(0.1)
            detail = (await client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
            if detail["status"] in {"success", "partial", "failed", "cancelled"}:
                break
        if detail is None or detail["status"] != "success":
            fail(f"备份任务未成功：{detail}")
            await database.dispose()
            return
        ok(f"备份任务完成：成功 {detail['succeeded']} / 失败 {detail['failed']}")

        run = detail["runs"][0]
        if run["snapshot_id"] is None:
            fail("运行记录没有关联快照")
        else:
            ok(f"已生成配置版本 #{run['snapshot_id']}（{run['line_count']} 行）")

        # 版本正文与脱敏
        snapshot_id = run["snapshot_id"]
        redacted = (
            await client.get(f"/api/v1/snapshots/{snapshot_id}/content", headers=headers)
        ).json()
        if "%^%#Kj8sLm2QpR7dXw%^%#" in redacted["content"]:
            fail("脱敏未生效，密文出现在响应里")
        else:
            ok("脱敏生效（密文已被掩码）")

        raw = (
            await client.get(
                f"/api/v1/snapshots/{snapshot_id}/content?format=raw", headers=headers
            )
        ).json()
        if "sysname HW-CORE-01" not in raw["content"]:
            fail("明文内容不完整")
        else:
            ok("明文查看正常（管理员有审计权限）")

        # 第二次采集应判定"无变更"
        response = await client.post(
            "/api/v1/backups", headers=headers, json={"device_ids": [device_id]}
        )
        task_id = response.json()["id"]
        for _ in range(200):
            await asyncio.sleep(0.1)
            detail = (await client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
            if detail["status"] in {"success", "partial", "failed", "cancelled"}:
                break
        if detail["unchanged"] == 1:
            ok("第二次采集判定为无变更（内容寻址去重生效）")
        else:
            fail(f"第二次采集未判定无变更：{detail}")

        # 版本对比（同一份内容 → identical）
        diff = (
            await client.get(
                f"/api/v1/devices/{device_id}/diff"
                f"?from_snapshot_id={snapshot_id}&to_snapshot_id={snapshot_id}",
                headers=headers,
            )
        ).json()
        if diff.get("identical") is True:
            ok("版本对比：相同内容短路返回 identical")
        else:
            fail(f"版本对比异常：{diff}")

        # 未授权访问必须被拒
        unauthorized = await client.get("/api/v1/devices")
        if unauthorized.status_code == 401:
            ok("未带令牌访问被拒绝（401）")
        else:
            fail(f"未带令牌访问返回了 {unauthorized.status_code}，应为 401")

    await database.dispose()


async def step_p2_ops(tmp_dir: Path) -> None:
    """P2 运营链路：分组 → 定时策略（含手工执行）→ 通知渠道 → 保留策略。

    与第 7 步分开建环境是有意的：P2 出问题不该让"P1 端到端能不能跑"这个结论
    变得不可信（那是排查时最先要看的信息）。
    """
    heading(8, "P2：分组 / 定时策略 / 通知渠道 / 保留策略")
    if importlib.util.find_spec("httpx") is None:
        warn("跳过（未安装 httpx）")
        return

    from httpx import ASGITransport, AsyncClient

    from app.core.config import Settings
    from app.core.crypto import generate_key
    from app.db.session import Database
    from app.main import create_app, prepare_database
    from app.services import notifications
    from app.services.audit import ACTIONS
    from tests.support.fake_device import make_fake_device

    fixtures = ROOT / "tests" / "fixtures"
    config_text = (fixtures / "huawei" / "vrp8" / "display_current-configuration.txt").read_text(
        encoding="utf-8"
    )
    version_text = (fixtures / "huawei" / "vrp8" / "display_version.txt").read_text(encoding="utf-8")

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_dir / 'p2.db').as_posix()}",
        data_dir=tmp_dir / "p2-data",
        secret_key=generate_key(),
        jwt_secret="selfcheck-jwt-secret-0123456789abcdef",
        admin_username="admin",
        admin_password="admin-12345",
        backup_precheck=False,
        backup_concurrency=2,
        # 自检不启动后台调度循环：这里直接调接口，避免"定时器同时在跑"造成的不确定
        scheduler_enabled=False,
        cors_origins=[],
    )
    settings.ensure_dirs()
    database = Database(settings.database_url)
    fake = make_fake_device(
        vendor="huawei",
        hostname="P2-SW-01",
        config_text=config_text,
        version_text=version_text,
    )

    def factory(config, prompt_re):
        from tests.support.fake_device import FakeSession

        return FakeSession(config, prompt_re, fake)

    app = create_app(settings=settings, database=database, session_factory=factory)
    await prepare_database(database, settings)

    # 通知发送打桩：自检不该真往群里/webhook 发消息
    sent: list[str] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        sent.append(url)
        return True, "ok"

    original_post = notifications._post_json
    notifications._post_json = fake_post  # type: ignore[assignment]

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://selfcheck"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin-12345"}
            )
            if login.status_code != 200:
                fail(f"登录失败：{login.status_code} {login.text[:200]}")
                return
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            profile = await client.post(
                "/api/v1/credentials",
                headers=headers,
                json={"name": "P2 凭据", "username": "netadmin", "password": "pw-123456"},
            )
            if profile.status_code != 200:
                # 不先判状态码的话，下面 profile.json()["id"] 会抛 KeyError，
                # 报告出来是"未预期异常"，反而看不出真正的原因
                fail(f"建凭据失败：{profile.status_code} {profile.text[:200]}")
                return
            device = await client.post(
                "/api/v1/devices",
                headers=headers,
                json={
                    "name": "P2 设备",
                    "host": "10.99.0.1",
                    "vendor": "huawei",
                    "credential_profile_id": profile.json()["id"],
                },
            )
            if device.status_code != 200:
                fail(f"建设备失败：{device.status_code} {device.text[:200]}")
                return
            device_id = device.json()["id"]

            # ---- 分组 ----
            group = await client.post(
                "/api/v1/groups", headers=headers, json={"name": "自检分组"}
            )
            if group.status_code != 200:
                fail(f"建分组失败：{group.status_code} {group.text[:200]}")
                return
            group_id = group.json()["id"]
            members = await client.put(
                f"/api/v1/groups/{group_id}/devices",
                headers=headers,
                json={"device_ids": [device_id]},
            )
            if members.status_code != 200 or members.json()["device_count"] != 1:
                fail(f"设置分组成员异常：{members.status_code} {members.text[:200]}")
            else:
                ok("分组创建与成员设置正常")

            filtered = await client.get(
                f"/api/v1/devices?group_id={group_id}", headers=headers
            )
            if filtered.status_code != 200 or filtered.json()["total"] != 1:
                fail(f"按分组过滤设备异常：{filtered.status_code} {filtered.text[:200]}")
            else:
                ok("设备列表支持按分组过滤（前端预选成员依赖它）")

            # ---- 定时策略 ----
            schedule = await client.post(
                "/api/v1/schedules",
                headers=headers,
                json={
                    "name": "自检策略",
                    "kind": "cron",
                    "cron": "0 2 * * *",
                    "jitter_minutes": 0,
                    "group_id": group_id,
                    "precheck": False,
                },
            )
            if schedule.status_code != 200:
                fail(f"建定时策略失败：{schedule.status_code} {schedule.text[:200]}")
                return
            data = schedule.json()
            if "02:00" not in data["description"] or not (
                data["next_run_at"] or ""
            ).endswith("Z"):
                fail(f"策略描述/下次时间异常：{data['description']!r} {data['next_run_at']!r}")
            else:
                ok(f"定时策略已创建（{data['description']}），下次 {data['next_run_at']}")

            preview = await client.get(
                f"/api/v1/schedules/{data['id']}/preview?count=3", headers=headers
            )
            if preview.status_code != 200 or len(preview.json()["times"]) != 3:
                fail(f"执行时刻预览异常：{preview.status_code} {preview.text[:200]}")
            else:
                ok(f"预览接下来 3 次执行时刻：{preview.json()['times'][0]} …")

            run = await client.post(f"/api/v1/schedules/{data['id']}/run", headers=headers)
            if run.status_code != 200:
                fail(f"手工触发策略失败：{run.status_code} {run.text[:200]}")
            else:
                task_id = run.json()["id"]
                for _ in range(200):  # 最多等 20 秒
                    detail = await client.get(f"/api/v1/backups/{task_id}", headers=headers)
                    if detail.json()["status"] in ("success", "partial", "failed", "cancelled"):
                        break
                    await asyncio.sleep(0.1)
                if detail.json()["status"] != "success":
                    fail(f"手工触发的任务未成功：{detail.json()}")
                else:
                    ok(f"手工触发策略跑通（任务 #{task_id} 成功）")
                after = await client.get(f"/api/v1/schedules/{data['id']}", headers=headers)
                if after.json()["next_run_at"] != data["next_run_at"]:
                    fail("手工触发改变了 next_run_at（插队不该改班次）")
                else:
                    ok("手工触发没有改动既定班次（next_run_at 未变）")

            # ---- 通知渠道 ----
            channel = await client.post(
                "/api/v1/channels",
                headers=headers,
                json={
                    "name": "自检渠道",
                    "kind": "dingtalk",
                    "config": {"url": "https://oapi.dingtalk.com/robot/send?access_token=SECRET123"},
                    "secret": {"secret": "SIGNKEY"},
                    "events": ["task_failed"],
                },
            )
            if channel.status_code != 200:
                fail(f"建通知渠道失败：{channel.status_code} {channel.text[:200]}")
                return
            if "SECRET123" in channel.text:
                fail("通知渠道接口泄露了机器人 access_token！")
            else:
                ok("通知渠道已创建，且响应里机器人地址已打码")

            test = await client.post(
                f"/api/v1/channels/{channel.json()['id']}/test", headers=headers
            )
            if test.status_code != 200 or not test.json()["ok"]:
                fail(f"通知渠道测试失败：{test.status_code} {test.text[:200]}")
            elif not sent:
                fail("通知渠道测试没有真正走到发送环节")
            else:
                ok("通知渠道测试发送正常（发送函数已打桩，未真发）")
            logs = await client.get("/api/v1/notification-logs", headers=headers)
            if logs.status_code != 200 or logs.json()["total"] < 1:
                fail(f"发送记录异常：{logs.status_code} {logs.text[:200]}")
            else:
                ok("发送记录已写入（渠道/结果/原因可查）")

            # ---- 保留策略 ----
            policy = await client.put(
                "/api/v1/retention",
                headers=headers,
                json={"keep_unchanged_daily_days": 30, "keep_min_versions": 2},
            )
            if policy.status_code != 200 or policy.json()["keep_unchanged_daily_days"] != 30:
                fail(f"修改保留策略异常：{policy.status_code} {policy.text[:200]}")
            else:
                ok(f"保留策略已更新：{policy.json()['description']}")
            cleaned = await client.post("/api/v1/retention/run", headers=headers)
            if cleaned.status_code != 200:
                fail(f"执行清理失败：{cleaned.status_code} {cleaned.text[:200]}")
            else:
                summary = cleaned.json()["summary"]
                ok(
                    f"清理执行正常（扫描 {summary['scanned_snapshots']} 个版本，"
                    f"删除 {summary['deleted_snapshots']} 个）"
                )

            # ---- 审计 ----
            missing = [
                action
                for action in ("group.create", "schedule.create", "channel.create", "retention.run")
                if action not in ACTIONS
            ]
            if missing:
                fail(f"审计动作表缺少 P2 动作：{missing}")
            else:
                ok("P2 写操作均有审计动作定义")
    finally:
        notifications._post_json = original_post  # type: ignore[assignment]
        await database.dispose()


async def _run_step(coro, name: str) -> None:
    """跑一个步骤；未预期异常也要报告出来，而不是让整个脚本崩在半路。

    这样做的原因：脚本里任何一处笔误（比如我引用了一个不存在的字段）都会抛
    KeyError/AttributeError。如果直接崩，临时目录清理还会因为 SQLite 文件被占用
    再抛一个 PermissionError，把真正的原因盖掉。
    """
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        fail(f"{name} 抛出未预期异常：{type(exc).__name__}: {exc}")
        traceback.print_exc()


async def main_async() -> int:
    step_environment()
    if _failures:
        print(f"\n{RED}环境检查未通过，先解决上面的问题再继续。{RESET}")
        return 1

    step_syntax()
    step_imports()
    step_registry_and_drivers()
    step_tooling()
    if _failures:
        print(f"\n{RED}代码层面有问题（语法/导入/驱动/CLI/迁移配置），先修这些。{RESET}")
        return 1

    with tempfile.TemporaryDirectory(prefix="dszoon-selfcheck-") as tmp:
        await _run_step(step_database(Path(tmp)), "第 6 步（建库）")
        if _failures:
            return 1
        await _run_step(step_end_to_end(Path(tmp)), "第 7 步（端到端）")
        if _failures:
            return 1
        await _run_step(step_p2_ops(Path(tmp)), "第 8 步（P2 运营链路）")

    print()
    if _failures:
        print(f"{RED}自检失败：{len(_failures)} 项{RESET}")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print(f"{GREEN}自检全部通过。接下来可以跑 python -m pytest -q 做完整回归。{RESET}")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
