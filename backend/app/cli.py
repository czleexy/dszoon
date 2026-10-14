"""命令行入口。

Web 界面是主入口，但有几件事命令行做起来更顺手（也是 P2 定时任务的地基）：

* ``init-db``        开发环境建表（生产请用 Alembic 迁移）
* ``gen-key``        生成设备凭据加密密钥
* ``create-user``    建用户
* ``reset-password`` 重置口令（忘记管理员口令时的救命命令）
* ``list-devices``   列出设备台账
* ``backup``         不开界面跑一次备份（cron / 运维脚本可直接调用）

用法（在 backend/ 目录下）::

    python -m app.cli init-db
    python -m app.cli gen-key
    python -m app.cli create-user ops --role operator
    python -m app.cli reset-password admin
    python -m app.cli list-devices
    python -m app.cli backup --all
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

# 允许 `python -m app.cli` 与 `python app/cli.py` 两种方式
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import Settings, configure_logging, get_settings  # noqa: E402
from app.core.crypto import CredentialCipher, generate_key  # noqa: E402
from app.core.security import Role, hash_password  # noqa: E402
from app.db.models import BackupTask, Device, User  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.services.backups import BackupRunner  # noqa: E402
from app.services.blobs import build_blob_store  # noqa: E402

TERMINAL_STATUSES = {"success", "partial", "failed", "cancelled"}


def _database(settings: Settings) -> Database:
    settings.ensure_dirs()
    return Database(settings.database_url)


def _ask_password(prompt: str = "口令: ") -> str:
    password = getpass.getpass(prompt)
    if len(password) < 8:
        print("口令至少 8 位", file=sys.stderr)
        raise SystemExit(2)
    return password


# --------------------------------------------------------------------------
async def cmd_init_db(settings: Settings, _: argparse.Namespace) -> int:
    """建表并创建初始管理员（等价于首次启动服务时做的事）。"""
    from app.main import prepare_database

    database = _database(settings)
    try:
        generated = await prepare_database(database, settings)
    finally:
        await database.dispose()
    print(f"数据库已就绪：{settings.database_url}")
    if generated:
        print(f"初始管理员 {settings.admin_username} 的随机口令（只显示这一次）：{generated}")
    return 0


async def cmd_create_user(settings: Settings, args: argparse.Namespace) -> int:
    database = _database(settings)
    try:
        async with database.session() as session:
            existing = await session.scalar(select(User).where(User.username == args.username))
            if existing is not None:
                print(f"用户已存在：{args.username}", file=sys.stderr)
                return 1
            password = args.password or _ask_password()
            session.add(
                User(
                    username=args.username,
                    password_hash=hash_password(password),
                    role=Role(args.role).value,
                    display_name=args.display_name,
                )
            )
            await session.commit()
    finally:
        await database.dispose()
    print(f"已创建用户 {args.username}（角色 {args.role}）")
    return 0


async def cmd_reset_password(settings: Settings, args: argparse.Namespace) -> int:
    database = _database(settings)
    try:
        async with database.session() as session:
            user = await session.scalar(select(User).where(User.username == args.username))
            if user is None:
                print(f"用户不存在：{args.username}", file=sys.stderr)
                return 1
            password = args.password or _ask_password("新口令: ")
            user.password_hash = hash_password(password)
            if args.activate:
                user.is_active = True
            await session.commit()
    finally:
        await database.dispose()
    print(f"已重置 {args.username} 的口令")
    return 0


async def cmd_list_devices(settings: Settings, args: argparse.Namespace) -> int:
    database = _database(settings)
    try:
        async with database.session() as session:
            conditions = []
            if args.vendor:
                conditions.append(Device.vendor == args.vendor)
            if args.site:
                conditions.append(Device.site == args.site)
            result = await session.execute(
                select(Device).where(*conditions).order_by(Device.name)
            )
            devices = list(result.scalars().all())
    finally:
        await database.dispose()

    if not devices:
        print("没有匹配的设备")
        return 0
    print(f"{'ID':<6}{'名称':<24}{'地址':<18}{'厂商':<10}{'站点':<16}最近状态")
    for device in devices:
        print(
            f"{device.id:<6}{device.name[:23]:<24}{device.host[:17]:<18}"
            f"{device.vendor:<10}{(device.site or '-')[:15]:<16}{device.last_status or '未采集'}"
        )
    return 0


async def cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    """用与 Web 完全相同的编排逻辑跑一次备份（含落库与版本判定）。"""
    from app.schemas.backup import BackupRequest

    settings.ensure_dirs()
    database = _database(settings)
    cipher = CredentialCipher(settings.secret_key)
    runner = BackupRunner(
        database=database,
        settings=settings,
        cipher=cipher,
        blobs=build_blob_store(
            settings.objects_dir, backend=settings.blob_backend, repo=settings.git_repo
        ),
    )
    try:
        async with database.session() as session:
            payload = BackupRequest(
                device_ids=args.device_id or None,
                site=args.site,
                vendor=args.vendor,
                all_enabled=args.all,
                precheck=args.precheck,
                retries=args.retries,
            )
            task = await runner.create_task(session, payload=payload)
            await session.commit()
            task_id = task.id
            total = task.total

        print(f"任务 #{task_id} 已创建，共 {total} 台设备，开始采集…")
        runner.start(task_id)

        # 等任务结束：直接查库，这样即使 CLI 被 Ctrl+C 打断，进度也已经在数据库里
        while True:
            await asyncio.sleep(0.5)
            async with database.session() as session:
                current = await session.get(BackupTask, task_id)
                if current is None:
                    print("任务记录消失，异常退出", file=sys.stderr)
                    return 1
                done = current.succeeded + current.failed
                status = current.status
                print(
                    f"\r  进度 {done}/{current.total}  成功 {current.succeeded}"
                    f"（无变更 {current.unchanged}）  失败 {current.failed}",
                    end="",
                    flush=True,
                )
                if status in TERMINAL_STATUSES:
                    print()
                    print(f"任务结束：{status}")
                    if current.failed:
                        print("失败设备：")
                        from app.db.models import BackupRun

                        result = await session.execute(
                            select(BackupRun).where(
                                BackupRun.task_id == task_id,
                                BackupRun.status.notin_(("success", "unchanged")),
                            )
                        )
                        for run in result.scalars().all():
                            device = await session.get(Device, run.device_id)
                            print(
                                f"  - {(device.name if device else run.device_id)}: "
                                f"{run.status} {run.error_message or ''}"
                            )
                    return 0 if status == "success" else 1
    finally:
        await runner.shutdown()
        await database.dispose()


def cmd_gen_key(_: Settings, __: argparse.Namespace) -> int:
    print(generate_key())
    print(
        "\n把它设成环境变量（并妥善备份）：\n"
        "  Linux/macOS: export DSZOON_SECRET_KEY='<上面的值>'\n"
        "  Windows:     setx DSZOON_SECRET_KEY \"<上面的值>\"\n"
        "注意：更换它会导致已保存的设备密码无法解密。",
        file=sys.stderr,
    )
    return 0


async def cmd_stats(settings: Settings, _: argparse.Namespace) -> int:
    database = _database(settings)
    try:
        async with database.session() as session:
            devices = await session.scalar(select(func.count()).select_from(Device))
            users = await session.scalar(select(func.count()).select_from(User))
            tasks = await session.scalar(select(func.count()).select_from(BackupTask))
    finally:
        await database.dispose()
    print(f"设备 {devices} 台 · 用户 {users} 个 · 备份任务 {tasks} 个")
    print(f"数据库：{settings.database_url}")
    print(f"数据目录：{settings.data_dir}（配置正文与会话日志）")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dszoon", description="dszoon 网络设备配置备份 —— 命令行工具"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="建表并创建初始管理员（开发用）")
    subparsers.add_parser("gen-key", help="生成设备凭据加密密钥")
    subparsers.add_parser("stats", help="打印台账与存储概览")

    create_user = subparsers.add_parser("create-user", help="创建用户")
    create_user.add_argument("username")
    create_user.add_argument(
        "--role", default=Role.VIEWER.value, choices=[role.value for role in Role]
    )
    create_user.add_argument("--password", help="不传则交互式输入")
    create_user.add_argument("--display-name", dest="display_name")

    reset = subparsers.add_parser("reset-password", help="重置用户口令（忘记口令时用）")
    reset.add_argument("username")
    reset.add_argument("--password", help="不传则交互式输入")
    reset.add_argument("--activate", action="store_true", help="顺便把用户设为启用")

    list_devices = subparsers.add_parser("list-devices", help="列出设备台账")
    list_devices.add_argument("--vendor")
    list_devices.add_argument("--site")

    backup = subparsers.add_parser("backup", help="跑一次备份（不开界面）")
    backup.add_argument("--device-id", type=int, action="append", help="可重复指定多台")
    backup.add_argument("--site")
    backup.add_argument("--vendor")
    backup.add_argument("--all", action="store_true", help="全部启用设备")
    backup.add_argument("--precheck", action="store_true", default=None, help="开启 TCP 可达性预检")
    backup.add_argument("--retries", type=int, default=None, help="失败重试次数（0-5）")

    return parser


HANDLERS = {
    "init-db": cmd_init_db,
    "gen-key": cmd_gen_key,
    "stats": cmd_stats,
    "create-user": cmd_create_user,
    "reset-password": cmd_reset_password,
    "list-devices": cmd_list_devices,
    "backup": cmd_backup,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - 配置/密钥问题要给一句人话
        print(f"配置加载失败：{exc}", file=sys.stderr)
        return 2

    handler = HANDLERS[args.command]
    try:
        if asyncio.iscoroutinefunction(handler):
            return asyncio.run(handler(settings, args))
        return handler(settings, args)
    except AppError as exc:
        print(f"失败：{exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
