"""规模化行为测试（不需要真设备，也不需要 1000 台的墙钟时间）。

``scripts/loadtest.py`` 负责"1000 台要跑多久"的压测；这里负责**规模下的语义正确性**：
设备一多，最容易坏掉的不是性能，而是这几条不变量——

* **run ≠ snapshot**：第二轮没变的设备只应产生 run，不应产生新版本；
* **内容寻址**：200 台设备同一个配置正文，磁盘上只应有一个文件；
* **并发确实生效**：设备级并发度不是摆设（串行跑 200 台会把一次夜间窗口撑爆）；
* **计数与落库一致**：任务的成功/无变更/失败计数必须和 runs 表对得上，
  否则界面上显示的"成功 200 台"是假的。

用直接落库造设备（而不是调 200 次接口）：接口层的正确性由其它测试覆盖，
这里要的是"设备数量"本身。
"""

from __future__ import annotations

import asyncio
import os
import time

from sqlalchemy import func, select

from app.db.models import BackupRun, BackupTask, ConfigSnapshot, CredentialProfile, Device
from app.schemas.backup import BackupRequest
from tests.support.env import (
    HUAWEI_CONFIG,
    HUAWEI_VERSION,
    Env,
    fixture_text,
    with_config_change,
)
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}

#: 规模化测试的设备数。200 台足够暴露"每台一次提交""忘记去重""计数不清零"这类问题，
#: 又能在几秒内跑完（1000 台的墙钟时间交给 scripts/loadtest.py）。
SCALE = 200


def host_for(index: int) -> str:
    return f"10.30.{index // 250}.{index % 250 + 1}"


async def seed_devices(env: Env, count: int) -> list[int]:
    """直接落库造设备 + 一台凭据，并注册假会话。"""
    cipher = env.app.state.cipher
    base_config = fixture_text(HUAWEI_CONFIG)
    version_text = fixture_text(HUAWEI_VERSION)
    ids: list[int] = []
    async with env.database.session() as session:
        profile = CredentialProfile(
            name="scale", username="netadmin", password_enc=cipher.encrypt("scale-password")
        )
        session.add(profile)
        await session.flush()
        for index in range(count):
            device = Device(
                name=f"scale-{index:04d}",
                host=host_for(index),
                vendor="huawei",
                credential_profile_id=profile.id,
                site=f"site-{index // 50}",
                enabled=True,
            )
            session.add(device)
            await session.flush()
            ids.append(device.id)
            env.factory.register(
                host_for(index),
                make_fake_device(
                    vendor="huawei",
                    hostname=f"SCALE-{index:04d}",
                    config_text=base_config,
                    version_text=version_text,
                ),
            )
        await session.commit()
    return ids


def change_configs(env: Env, count: int, *, changed: int) -> None:
    """把前 ``changed`` 台设备的配置换掉（其余保持原样，应当被判为无变更）。

    变更内容对**所有**变更设备是同一段文本——这样"磁盘上只有 2 个正文文件"就是一个
    可断言的硬事实。变更行插在结尾终止符之前，见 ``with_config_change``。
    """
    base = fixture_text(HUAWEI_CONFIG)
    changed_text = with_config_change(base)
    version_text = fixture_text(HUAWEI_VERSION)
    for index in range(count):
        text = changed_text if index < changed else base
        env.factory.register(
            host_for(index),
            make_fake_device(
                vendor="huawei",
                hostname=f"SCALE-{index:04d}",
                config_text=text,
                version_text=version_text,
            ),
        )


async def run_task(env: Env, name: str) -> tuple[dict, float]:
    """跑一次全量任务，返回 ``(任务终态, 墙钟秒数)``。"""
    async with env.database.session() as session:
        task = await env.runner.create_task(
            session, payload=BackupRequest(all_enabled=True, precheck=False, name=name)
        )
        await session.commit()
        task_id = task.id

    started = time.monotonic()
    env.runner.start(task_id)
    while env.runner.is_running(task_id):
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - started

    async with env.database.session() as session:
        row = await session.get(BackupTask, task_id)
        assert row is not None
        assert row.status in TERMINAL, f"任务未结束：{row.status}"
        return (
            {
                "id": row.id,
                "status": row.status,
                "total": row.total,
                "succeeded": row.succeeded,
                "unchanged": row.unchanged,
                "failed": row.failed,
            },
            elapsed,
        )


async def count_rows(env: Env) -> dict[str, int]:
    async with env.database.session() as session:
        return {
            "runs": int(await session.scalar(select(func.count()).select_from(BackupRun)) or 0),
            "snapshots": int(
                await session.scalar(select(func.count()).select_from(ConfigSnapshot)) or 0
            ),
        }


async def test_scale_two_rounds_keep_runs_and_versions_distinct(env: Env):
    """★ 200 台跑两轮：run 200→400，版本数**只有变更的那部分**增长。

    这条不变量是整个数据模型的地基（"多次运行，少量版本"）。少了它，一台设备
    一天采 24 次就是 24 个版本，一年下来 8760 个版本 × 1000 台，磁盘和维护都崩。
    """
    await env.auth()
    await seed_devices(env, SCALE)

    first, _ = await run_task(env, "规模化第 1 轮")
    assert first["status"] == "success", first
    assert first["total"] == SCALE
    assert first["succeeded"] == SCALE, "第一轮每台都应采集成功"
    assert first["unchanged"] == 0, "第一轮没有历史版本，不该出现无变更"
    assert first["failed"] == 0

    after_first = await count_rows(env)
    assert after_first["runs"] == SCALE
    assert after_first["snapshots"] == SCALE, "每台设备一个版本"
    # 200 台设备的正文完全一致 → 内容寻址只应落一个文件
    assert env.app.state.blobs.stats()["objects"] == 1, "相同正文必须去重（内容寻址）"

    second, _ = await run_task(env, "规模化第 2 轮（内容未变）")
    assert second["status"] == "success", second
    # 语义提醒：succeeded 是"采集成功"的总数（含无变更），unchanged 是它的子集。
    # 进度条按 (succeeded + failed) / total 计算，所以这里两者相等是正常的。
    assert second["succeeded"] == SCALE
    assert second["unchanged"] == SCALE, "内容没变就该判为无变更"

    after_second = await count_rows(env)
    assert after_second["runs"] == SCALE * 2, "每次运行都要留运行记录"
    assert after_second["snapshots"] == SCALE, "★ 无变更的设备不该产生新版本"
    assert env.app.state.blobs.stats()["objects"] == 1


async def test_scale_only_changed_devices_get_new_versions(env: Env):
    """第三轮改掉 20 台：只有这 20 台产生新版本，磁盘上只多一个正文文件。"""
    await env.auth()
    await seed_devices(env, SCALE)
    await run_task(env, "基线轮")

    changed = 20
    change_configs(env, SCALE, changed=changed)
    third, _ = await run_task(env, "变更轮")

    assert third["status"] == "success", third
    assert third["succeeded"] == SCALE, "全部设备都采集成功（含无变更的那些）"
    assert third["unchanged"] == SCALE - changed, "只有被改过的设备发生了变更"

    rows = await count_rows(env)
    assert rows["runs"] == SCALE * 2
    assert rows["snapshots"] == SCALE + changed
    assert env.app.state.blobs.stats()["objects"] == 2, "变更后的正文是一份新内容"


async def test_scale_run_counters_match_rows(env: Env):
    """任务上的计数必须是 runs 表算出来的真值，否则界面会骗人。"""
    await env.auth()
    await seed_devices(env, 40)
    # 让其中 5 台连不上：计数里必须体现为失败，而不是被"成功"吞掉
    for index in range(5):
        env.factory.register(
            host_for(index),
            make_fake_device(
                vendor="huawei",
                hostname=f"SCALE-{index:04d}",
                config_text="",
                fail_with="unreachable",
            ),
        )

    detail, _ = await run_task(env, "含失败设备")
    assert detail["status"] == "partial", detail
    assert detail["failed"] == 5
    # succeeded 含无变更（见上），所以不变量是 succeeded + failed == total
    assert detail["succeeded"] + detail["failed"] == detail["total"] == 40
    assert detail["unchanged"] == 0, "没有历史版本时不该有无变更"

    async with env.database.session() as session:
        failed_runs = await session.scalar(
            select(func.count()).select_from(BackupRun).where(BackupRun.status == "unreachable")
        )
    assert int(failed_runs or 0) == 5, "失败设备的运行记录必须落库（否则无法排障）"


async def test_scale_runs_are_actually_concurrent(env: Env):
    """设备级并发必须真的生效。

    ★ 延时必须**明显大于**本机的"单台开销"才有判别力。第一版用 20ms：
    20 台 × 5 条命令 × 20ms = 串行约 2 秒，而实测这台机器上单台设备的落库/事件开销
    就有 ~0.2 秒（20 台 / 并发 4 × 0.2s ≈ 1 秒），阈值（串行的一半 = 1 秒）正好卡在
    噪声里。改成 100ms 后串行估计 10 秒、并发期望 ~2.5~3.5 秒，阈值 5 秒——
    要证明的是"并发没被写死成串行"，不是测出精确性能。
    """
    await env.auth()
    devices = 20
    delay = 0.1
    await seed_devices(env, devices)
    version_text = fixture_text(HUAWEI_VERSION)
    config_text = fixture_text(HUAWEI_CONFIG)
    for index in range(devices):
        env.factory.register(
            host_for(index),
            make_fake_device(
                vendor="huawei",
                hostname=f"SCALE-{index:04d}",
                config_text=config_text,
                version_text=version_text,
                delay=delay,
            ),
        )

    serial_estimate = devices * 5 * delay
    detail, elapsed = await run_task(env, "并发验证")
    assert detail["status"] == "success", detail
    assert elapsed < serial_estimate / 2, (
        f"耗时 {elapsed:.2f}s，接近串行估计 {serial_estimate:.2f}s：设备级并发没有生效"
    )


async def test_scale_retention_sweep_is_bounded(env: Env):
    """保留策略在规模化下也必须收敛：每台设备每天只留 1 个无变更版本。

    做法：200 台设备各造 4 个"归一化后相同"的版本（12/9/6/3 天前），把未变更保留
    天数设成 0，清理后每台应剩"最早 1 个 + 最新 2 个" = 3 个，即**正好删掉中间那一个**。

    ★ 为什么必须是 4 个版本而不是 3 个：``keep_min_versions=2`` 会保留"最新 2 个"，
    再加"最早 1 个"；3 个版本时这三条规则正好覆盖全部，一个都删不掉——第一版测试
    用了 3 个版本却照抄了 4 个版本的预期，于是红在"该删 1 个却删了 0 个"。
    """
    headers = await env.auth()
    await seed_devices(env, SCALE)
    blobs = env.app.state.blobs

    from app.db.base import utcnow
    from datetime import timedelta

    async with env.database.session() as session:
        devices = (
            (await session.execute(select(Device.id))).scalars().all()
        )
        for device_id in devices:
            for offset in (12, 9, 6, 3):
                captured = utcnow() - timedelta(days=offset)
                stored = blobs.put_text(f"hostname SCALE\n# rev {device_id}-{offset}\n")
                # 正文文件的 mtime 要跟着版本日期走：清理对"刚写下的文件"有宽限期
                # （见 retention.BLOB_GRACE_SECONDS），否则这些正文一个都不会被回收
                stamp = captured.timestamp()
                os.utime(stored.path, (stamp, stamp))
                session.add(
                    ConfigSnapshot(
                        device_id=device_id,
                        sha256=stored.sha256,
                        normalized_sha256="n" * 64,  # 归一化后相同 = 无实质变更
                        object_key=stored.object_key,
                        size_bytes=stored.size_bytes,
                        line_count=2,
                        captured_at=captured,
                    )
                )
        await session.commit()

    before = await count_rows(env)
    assert before["snapshots"] == SCALE * 4

    await env.client.put(
        "/api/v1/retention",
        headers=headers,
        json={"keep_unchanged_daily_days": 0, "keep_min_versions": 2},
    )
    response = await env.client.post("/api/v1/retention/run", headers=headers)
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    assert summary["scanned_devices"] == SCALE
    assert summary["deleted_snapshots"] == SCALE, (
        f"每台只该删掉中间那一个版本，实际删了 {summary['deleted_snapshots']} 个"
    )
    assert summary["deleted_blobs"] == SCALE, "对应的正文文件要一起回收"

    after = await count_rows(env)
    assert after["snapshots"] == SCALE * 3
