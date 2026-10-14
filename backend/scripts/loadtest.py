"""1000 台规模压测 / 验收脚本（**不需要真设备**）。

    python scripts/loadtest.py --devices 1000 --latency 0.01 --concurrency 32 --rounds 2

为什么要写它：

* 设计上"能撑 1000 台"和实际"跑 1000 台"是两件事。并发模型、SQLite 写锁、
  正文去重、版本折叠这些在几百台以上才会暴露问题（例如每台一次 commit 会把
  SQLite 拖死；或者同内容反复建版本，把磁盘吃满）。
* 它走的是**真实链路**：``BackupRunner`` → 采集引擎 → 完整性校验 → 内容寻址落盘
  → 数据库。只有 SSH 会话是内存假设备（可以用 ``--latency`` 模拟每台设备的往返
  延时），所以这里测出的并发与吞吐是有意义的。

用法要点：

* ``--latency`` 不要给 0：假设备瞬间返回时，压测只测到"空转"，测不出并发与排队。
* ``--vendor h3c|ruijie`` 会连带换用对应厂商的**版本回显夹具**（见 ``VENDOR_FIXTURES``）：
  型号/版本是驱动按厂商正则抽的，拿华为的回显喂华三驱动会抽不出型号，结果是
  "1000 台全失败"——而真正的问题只是夹具配错了。
* ``--rounds 2`` 会做第二轮：改掉 ``--change-ratio`` 比例设备的配置后重跑。第二轮
  验证两条关键不变量——"没变的设备不产生新版本"（run ≠ snapshot）和
  "正文内容寻址，相同内容只存一份"。
* 阈值（``--max-seconds``、零失败、去重与版本上限）不满足时脚本以非 0 退出，
  因此可以直接进 CI 或验收流程。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 允许直接 `python scripts/loadtest.py` 运行（把仓库根目录加入 import path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.crypto import CredentialCipher, generate_key  # noqa: E402
from app.db.models import BackupRun, BackupTask, ConfigSnapshot, CredentialProfile, Device  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.main import prepare_database  # noqa: E402
from app.schemas.backup import BackupRequest  # noqa: E402
from app.services.backups import BackupRunner  # noqa: E402
from app.services.blobs import BlobStore  # noqa: E402
from app.services.notifications import Notifier  # noqa: E402

# 复用测试用的假设备：它实现的就是引擎真正调用的那几个方法（open/begin/
# send_command/close + 提示符读取）。另写一份只会让"压测过的代码"与"测试过的
# 代码"分叉——那种分叉正是这类脚本给出虚假结论的主要原因。
from tests.support.env import fixture_text, with_config_change  # noqa: E402
from tests.support.fake_device import FakeSessionFactory, make_fake_device  # noqa: E402

TERMINAL = {"success", "partial", "failed", "cancelled"}

#: 第二轮变更用的那**一行**。故意对所有变更设备用同一行：这样正文文件数应当恒为 2
#: （原配置 + 变更配置），"对象目录里的文件数"就能直接证明内容寻址生效。
#:
#: ★ 它必须插在结尾的 return/end **之前**（见 tests.support.env.with_config_change）：
#:   追加到终止符之后会被驱动的 must_end_with 判成 bad_terminator，于是"被改过的设备"
#:   全部采集成失败——压测会报 50 台失败，而真实原因只是变更造得不像真配置。
CHANGE_LINE = "ip route-static 10.99.0.0 255.255.255.0 10.10.10.254"


#: 各厂商的夹具。**必须跟着 --vendor 一起换**：版本回显是驱动用来抽型号/版本的，
#: 拿华为的 `display version` 去喂华三驱动，抽不出型号（甚至被判成内容异常），
#: 压测结果就成了"设备全失败"——而真正的问题只是夹具配错了。
VENDOR_FIXTURES = {
    "huawei": (
        "huawei/vrp8/display_current-configuration.txt",
        "huawei/vrp8/display_version.txt",
    ),
    "h3c": (
        "h3c/comware7/display_current-configuration.txt",
        "h3c/comware7/display_version.txt",
    ),
    "ruijie": (
        "ruijie/rgos11/show_running-config.txt",
        "ruijie/rgos11/show_version.txt",
    ),
}


def vendor_fixtures(vendor: str) -> tuple[str, str]:
    config_path, version_path = VENDOR_FIXTURES[vendor]
    return fixture_text(config_path), fixture_text(version_path)


def host_for(index: int) -> str:
    """10.20.x.y：每 250 台换一个 /24，地址分布接近真实台账（不会全挤在一个段里）。"""
    return f"10.20.{index // 250}.{index % 250 + 1}"


@dataclass
class Report:
    devices: int
    vendor: str
    latency: float
    concurrency: int
    round_seconds: list[float]
    runs: int
    failed: int
    snapshots: int
    objects: int
    object_bytes: int
    db_bytes: int
    duration_p50: int
    duration_p95: int

    def as_dict(self) -> dict:
        total = sum(self.round_seconds)
        return {
            "devices": self.devices,
            "vendor": self.vendor,
            "latency_per_command_s": self.latency,
            "concurrency": self.concurrency,
            "rounds": len(self.round_seconds),
            "round_seconds": [round(value, 2) for value in self.round_seconds],
            "total_seconds": round(total, 2),
            "runs": self.runs,
            "succeeded": self.runs - self.failed,
            "failed": self.failed,
            "snapshots": self.snapshots,
            "objects": self.objects,
            "object_bytes": self.object_bytes,
            "db_bytes": self.db_bytes,
            "duration_p50_ms": self.duration_p50,
            "duration_p95_ms": self.duration_p95,
            "devices_per_second": round(self.devices * len(self.round_seconds) / total, 2)
            if total
            else 0.0,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="dszoon 规模化压测（内存假设备）")
    parser.add_argument("--devices", type=int, default=1000, help="设备台数")
    parser.add_argument(
        "--latency",
        type=float,
        default=0.01,
        help="每条命令的人为延时（秒），模拟设备往返；0 会测不出并发行为",
    )
    parser.add_argument("--concurrency", type=int, default=32, help="设备级并发度")
    parser.add_argument("--rounds", type=int, default=2, help="跑几轮（第二轮会改配置）")
    parser.add_argument(
        "--change-ratio", type=float, default=0.05, help="第二轮变更配置的设备比例"
    )
    parser.add_argument("--max-seconds", type=float, default=300.0, help="单轮墙钟上限（秒）")
    parser.add_argument("--workdir", type=Path, default=Path("var/loadtest"), help="临时数据目录")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（默认跑完删除）")
    parser.add_argument("--report", type=Path, default=None, help="把 JSON 报告写到该路径")
    parser.add_argument("--vendor", default="huawei", choices=["huawei", "h3c", "ruijie"])
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> Settings:
    args.workdir.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(args.workdir / 'loadtest.db').as_posix()}",
        data_dir=args.workdir / "data",
        secret_key=generate_key(),
        jwt_secret="loadtest-jwt-secret-0123456789abcdefghijklmn",
        admin_password="loadtest-12345",
        backup_concurrency=args.concurrency,
        backup_precheck=False,
        # 压测不写会话日志：几万个文件会污染磁盘统计，也会掩盖真正的瓶颈
        keep_session_logs=False,
        scheduler_enabled=False,
    )


def db_path(settings: Settings) -> Path:
    return Path(settings.database_url.split("///", 1)[-1])


async def seed_devices(database: Database, cipher: CredentialCipher, count: int, vendor: str) -> None:
    """直接落库造设备：走 HTTP 造 1000 台会把时间花在接口序列化上，偏离压测目标。"""
    async with database.session() as session:
        profile = CredentialProfile(
            name="loadtest",
            username="netadmin",
            password_enc=cipher.encrypt("loadtest-password"),
        )
        session.add(profile)
        await session.flush()
        for index in range(count):
            session.add(
                Device(
                    name=f"load-{index:05d}",
                    host=host_for(index),
                    vendor=vendor,
                    credential_profile_id=profile.id,
                    site=f"site-{index // 100}",
                    enabled=True,
                )
            )
        await session.commit()


def register_fakes(
    factory: FakeSessionFactory,
    count: int,
    vendor: str,
    *,
    latency: float,
    base_config: str,
    version_text: str,
    changed: set[int] | None = None,
) -> None:
    changed = changed or set()
    # 变更内容对所有变更设备是同一份（好让"对象目录里只有 2 个文件"成为硬事实），
    # 且那一行插在结尾终止符之前（见 CHANGE_LINE 的说明）
    changed_config = with_config_change(base_config, line=CHANGE_LINE)
    for index in range(count):
        config_text = changed_config if index in changed else base_config
        factory.register(
            host_for(index),
            make_fake_device(
                vendor=vendor,
                hostname=f"LOAD-{index:05d}",
                config_text=config_text,
                version_text=version_text,
                delay=latency,
            ),
        )


async def run_round(database: Database, runner: BackupRunner, *, name: str) -> float:
    """发起一次全量任务并等它结束，返回墙钟秒数。"""
    async with database.session() as session:
        task = await runner.create_task(
            session, payload=BackupRequest(all_enabled=True, precheck=False, name=name)
        )
        await session.commit()
        task_id, total = task.id, task.total

    started = time.monotonic()
    runner.start(task_id)
    while runner.is_running(task_id):
        await asyncio.sleep(0.1)
    elapsed = time.monotonic() - started

    async with database.session() as session:
        finished = await session.get(BackupTask, task_id)
        assert finished is not None
        if finished.status not in TERMINAL:
            raise SystemExit(f"任务 #{task_id} 未正常结束：status={finished.status}")
        print(
            f"  任务 #{task_id}：{total} 台，状态 {finished.status}，"
            f"成功 {finished.succeeded} / 无变更 {finished.unchanged} / 失败 {finished.failed}，"
            f"耗时 {elapsed:.1f}s"
        )
    return elapsed


async def collect_metrics(database: Database, settings: Settings, blobs: BlobStore) -> dict:
    async with database.session() as session:
        rows = list((await session.execute(select(BackupRun.duration_ms, BackupRun.status))).all())
        snapshots = await session.scalar(select(func.count()).select_from(ConfigSnapshot))
    durations = sorted(int(row[0] or 0) for row in rows)
    stats = blobs.stats()
    path = db_path(settings)
    return {
        "runs": len(rows),
        "failed": sum(1 for row in rows if row[1] not in ("success", "unchanged")),
        "snapshots": int(snapshots or 0),
        "objects": stats["objects"],
        "object_bytes": stats["object_bytes"],
        "db_bytes": path.stat().st_size if path.is_file() else 0,
        "p50": durations[len(durations) // 2] if durations else 0,
        "p95": durations[int(len(durations) * 0.95)] if durations else 0,
    }


async def sample_failures(
    database: Database, *, limit: int = 3
) -> list[tuple[int, str, str]]:
    """列出几条失败样本。

    压测失败时最需要知道的是"**为什么**失败"，不是"失败了多少台"——是认证、不可达、
    还是内容被判成不可信（夹具配错、驱动解析不对）？不给原因的话，1000 台全失败
    和 1000 台全成功一样没法行动。
    """
    async with database.session() as session:
        rows = (
            await session.execute(
                select(
                    BackupRun.device_id,
                    BackupRun.status,
                    BackupRun.error_code,
                    BackupRun.error_message,
                )
                .where(BackupRun.status.notin_(("success", "unchanged")))
                .order_by(BackupRun.id)
                .limit(limit)
            )
        ).all()
    return [
        (int(row[0]), str(row[1]), str(row[2] or row[3] or "(无错误信息)")[:200])
        for row in rows
    ]


async def main() -> int:
    args = parse_args()
    if args.devices < 1:
        raise SystemExit("--devices 必须 ≥1")
    if args.devices > 250 * 250:
        raise SystemExit("--devices 过大：地址生成规则最多支持 62500 台")

    settings = build_settings(args)
    settings.ensure_dirs()
    database = Database(settings.database_url)
    cipher = CredentialCipher(settings.secret_key)
    blobs = BlobStore(settings.objects_dir)
    factory = FakeSessionFactory()
    notifier = Notifier(database=database, cipher=cipher)
    runner = BackupRunner(
        database=database,
        settings=settings,
        cipher=cipher,
        blobs=blobs,
        session_factory=factory,
        notifier=notifier,
    )

    await prepare_database(database, settings)
    print(
        f"== dszoon 压测：{args.devices} 台（{args.vendor}），"
        f"并发 {args.concurrency}，每命令延时 {args.latency}s =="
    )
    await seed_devices(database, cipher, args.devices, args.vendor)

    base_config, version_text = vendor_fixtures(args.vendor)
    register_fakes(
        factory,
        args.devices,
        args.vendor,
        latency=args.latency,
        base_config=base_config,
        version_text=version_text,
    )

    round_seconds: list[float] = []
    print("第 1 轮：全部设备首次采集")
    round_seconds.append(await run_round(database, runner, name="压测第 1 轮"))

    changed = int(args.devices * args.change_ratio)
    changed_indexes: set[int] = set()
    if args.rounds > 1:
        # 等间隔取样：每次跑变更的是同一批设备，结果可复现
        stride = max(1, args.devices // max(1, changed))
        changed_indexes = set(range(0, args.devices, stride)) if changed else set()
        print(f"第 2 轮：变更 {len(changed_indexes)} 台设备的配置后重采（其余应判为无变更）")
        register_fakes(
            factory,
            args.devices,
            args.vendor,
            latency=args.latency,
            base_config=base_config,
            version_text=version_text,
            changed=changed_indexes,
        )
        round_seconds.append(await run_round(database, runner, name="压测第 2 轮"))

    metrics = await collect_metrics(database, settings, blobs)
    report = Report(
        devices=args.devices,
        vendor=args.vendor,
        latency=args.latency,
        concurrency=args.concurrency,
        round_seconds=round_seconds,
        runs=metrics["runs"],
        failed=metrics["failed"],
        snapshots=metrics["snapshots"],
        objects=metrics["objects"],
        object_bytes=metrics["object_bytes"],
        db_bytes=metrics["db_bytes"],
        duration_p50=metrics["p50"],
        duration_p95=metrics["p95"],
    )
    payload = report.as_dict()
    print("\n== 结果 ==")
    for key, value in payload.items():
        print(f"  {key}: {value}")

    if report.failed:
        print("\n== 失败样本（前几条）==")
        for device_id, status, reason in await sample_failures(database):
            print(f"  设备 #{device_id}：{status} — {reason}")
        print("  提示：若全是 integrity_error，先检查 --vendor 与夹具是否匹配")

    problems: list[str] = []
    if report.failed:
        problems.append(f"有 {report.failed} 次采集失败（压测应当零失败）")
    expected_runs = args.devices * len(round_seconds)
    if report.runs != expected_runs:
        problems.append(f"运行记录数 {report.runs} ≠ 预期 {expected_runs}（每设备每轮一条）")
    for index, seconds in enumerate(round_seconds, start=1):
        if seconds > args.max_seconds:
            problems.append(f"第 {index} 轮耗时 {seconds:.1f}s 超过上限 {args.max_seconds}s")

    # 内容寻址：正文只有两种（原配置 / 变更配置）→ 对象目录里应当只有 ≤2 个文件
    expected_objects = 2 if changed_indexes else 1
    if report.objects > expected_objects:
        problems.append(
            f"正文文件数 {report.objects} 超过预期 {expected_objects}：内容寻址（去重）没生效"
        )

    # run ≠ snapshot：第一轮每台 1 个版本；第二轮只有被改过的设备才有新版本
    expected_snapshots = args.devices + len(changed_indexes)
    if report.snapshots > expected_snapshots + args.devices // 100:
        problems.append(
            f"版本数 {report.snapshots} 明显超过预期 {expected_snapshots}："
            "未变更的设备也在建新版本（run 与 snapshot 混淆了）"
        )
    if report.snapshots < expected_snapshots:
        problems.append(
            f"版本数 {report.snapshots} 少于预期 {expected_snapshots}：有设备的新版本没落库"
        )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {args.report}")

    await runner.shutdown()
    await database.dispose()
    if not args.keep:
        shutil.rmtree(args.workdir, ignore_errors=True)

    if problems:
        print("\n✗ 未通过：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\n✓ 通过：零失败、阈值内完成、去重与版本折叠符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
