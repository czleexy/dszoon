"""定时调度执行器测试。

**直接测 `tick()` 而不等时钟**：它本身就是"检查所有到期策略并执行"的语义，
测试里把 ``next_run_at`` 设成过去即可确定性触发，不需要 sleep，也不需要操纵时钟。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from app.db.base import utcnow
from app.db.models import Schedule
from app.schemas.backup import BackupRequest
from app.services.schedules import create_schedule
from app.services.scheduler import STATUS_BUSY, STATUS_ERROR, ScheduleRunner
from tests.support.env import HUAWEI_CONFIG, HUAWEI_VERSION, Env, fixture_text
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}


async def make_device(
    env: Env, headers: dict, *, host: str = "10.7.0.1", name: str = "sched-dev"
) -> int:
    credential = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": f"cred-{name}", "username": "netadmin", "password": "pw-123456"},
    )
    assert credential.status_code == 200, credential.text
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": name,
            "host": host,
            "vendor": "huawei",
            "credential_profile_id": credential.json()["id"],
        },
    )
    assert created.status_code == 200, created.text
    env.factory.register(
        host,
        make_fake_device(
            vendor="huawei",
            hostname="SCHED-SW",
            config_text=fixture_text(HUAWEI_CONFIG),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return created.json()["id"]


async def add_schedule(env: Env, *, scope: dict | None = None, **overrides) -> int:
    """建一个**已到期**的策略（tick 只处理已到期的）。

    ★ 范围字段（``device_ids``/``group_id``/``site``/``vendor``/``all_enabled``）与
    ``precheck``/``retries`` 都是**顶层字段**：服务层是按 ``BackupRequest`` 的形状读取的。
    第一版把范围塞进了 ``{"scope": {...}}``，结果 ``_scope_of`` 找不到任何范围而报
    "定时任务必须指定备份范围"。
    """
    payload: dict = {
        "name": "夜间备份",
        "kind": "cron",
        "cron": "0 2 * * *",
        "jitter_minutes": 0,
        "enabled": True,
        "precheck": False,
    }
    payload.update(scope or {"all_enabled": True})
    payload.update(overrides)
    async with env.database.session() as session:
        schedule = await create_schedule(session, payload)
        schedule.next_run_at = utcnow() - timedelta(minutes=1)
        await session.commit()
        return schedule.id


def make_runner(env: Env) -> ScheduleRunner:
    return ScheduleRunner(database=env.database, runner=env.runner, poll_interval=1.0)


async def wait_task(env: Env, headers: dict, task_id: int, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail.get("status") in TERMINAL:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束：{detail}")


async def load_schedule(env: Env, schedule_id: int) -> Schedule:
    async with env.database.session() as session:
        schedule = await session.get(Schedule, schedule_id)
        assert schedule is not None
        return schedule


# --------------------------------------------------------------------------
async def test_tick_fires_due_schedule_and_reports_final_status(env: Env):
    headers = await env.auth()
    device_id = await make_device(env, headers)
    schedule_id = await add_schedule(env, scope={"device_ids": [device_id]})

    fired = await make_runner(env).tick()
    assert len(fired) == 1, "到期策略应当发起一个任务"
    task_id = fired[0]

    detail = await wait_task(env, headers, task_id)
    assert detail["status"] == "success", detail
    assert detail["trigger"] == "scheduled", "触发方式必须标成定时，界面上要能区分"

    schedule = await load_schedule(env, schedule_id)
    assert schedule.last_task_id == task_id
    assert schedule.last_status == "success", "任务终态要回写到策略上（否则界面永远显示排队中）"
    assert schedule.next_run_at is not None and schedule.next_run_at > utcnow()


async def test_fired_task_records_its_schedule_id(env: Env):
    """任务要能追溯到是哪个策略发起的。"""
    headers = await env.auth()
    device_id = await make_device(env, headers)
    schedule_id = await add_schedule(env, scope={"device_ids": [device_id]})

    fired = await make_runner(env).tick()
    async with env.database.session() as session:
        from app.db.models import BackupTask

        task = await session.get(BackupTask, fired[0])
        assert task is not None and task.schedule_id == schedule_id


async def test_tick_skips_when_previous_task_still_running(env: Env):
    """上一次任务还在跑 → 跳过本次，避免同一个策略把自己的任务叠起来。

    1000 台一次要跑十几分钟，而策略可能是每 5 分钟一次——不拦就会雪崩。
    """
    headers = await env.auth()
    device_id = await make_device(env, headers)
    schedule_id = await add_schedule(env, scope={"device_ids": [device_id]})

    # 造一个"仍在排队"的任务：只建不启动，状态会一直停在 queued
    async with env.database.session() as session:
        task = await env.runner.create_task(
            session, payload=BackupRequest(device_ids=[device_id])
        )
        schedule = await session.get(Schedule, schedule_id)
        assert schedule is not None
        schedule.last_task_id = task.id
        await session.commit()

    fired = await make_runner(env).tick()
    assert fired == []
    schedule = await load_schedule(env, schedule_id)
    assert schedule.last_status == STATUS_BUSY
    assert schedule.next_run_at > utcnow(), "跳过也要推进下一次，否则每轮都会重试"


async def test_tick_marks_error_when_scope_matches_nothing(env: Env):
    """范围解析不出设备（站点写错/设备被删）：记在策略上，不刷日志也不中断调度。"""
    schedule_id = await add_schedule(
        env, name="空范围", scope={"site": "根本不存在的站点"}
    )
    fired = await make_runner(env).tick()
    assert fired == []
    schedule = await load_schedule(env, schedule_id)
    assert schedule.last_status == STATUS_ERROR
    assert schedule.next_run_at > utcnow()


async def test_disabled_schedule_is_not_fired(env: Env):
    headers = await env.auth()
    await make_device(env, headers)
    await add_schedule(env, name="停用的策略", enabled=False)
    assert await make_runner(env).tick() == []


async def test_schedule_without_next_run_is_not_fired(env: Env):
    """没算过下一次执行时间的策略不该被执行（例如刚创建就被停用）。"""
    schedule_id = await add_schedule(env, name="无计划时间")
    async with env.database.session() as session:
        schedule = await session.get(Schedule, schedule_id)
        assert schedule is not None
        schedule.next_run_at = None
        await session.commit()
    assert await make_runner(env).tick() == []


async def test_missed_runs_are_not_caught_up(env: Env):
    """★ 停机 3 天后不该瞬间涌出几百个任务。

    这是"从计划时刻往后推"这个做法的必要配套：只对齐节拍、不补跑。
    少了 not_before，每次 tick 都会发现"下一次仍在过去"，于是连续发任务直到追上。
    """
    headers = await env.auth()
    device_id = await make_device(env, headers)
    schedule_id = await add_schedule(
        env,
        name="每 30 分钟",
        scope={"device_ids": [device_id]},
        kind="interval",
        cron=None,
        interval_minutes=30,
    )
    async with env.database.session() as session:
        schedule = await session.get(Schedule, schedule_id)
        assert schedule is not None
        schedule.next_run_at = utcnow() - timedelta(days=3)  # 停机 3 天
        await session.commit()

    fired = await make_runner(env).tick()
    assert len(fired) == 1, "错过的班次不补跑：一次 tick 只应发起一个任务"

    schedule = await load_schedule(env, schedule_id)
    assert schedule.next_run_at > utcnow(), "下一次必须落在将来，否则每轮 tick 都会再发一次"
    assert schedule.next_run_at < utcnow() + timedelta(hours=1)


async def test_tick_handles_multiple_due_schedules(env: Env):
    headers = await env.auth()
    device_id = await make_device(env, headers)
    await add_schedule(env, name="策略A", scope={"device_ids": [device_id]})
    await add_schedule(env, name="策略B", scope={"device_ids": [device_id]})

    fired = await make_runner(env).tick()
    assert len(fired) == 2


async def test_retention_runs_automatically_once_per_interval(env: Env):
    """★ 保留策略必须能**在后台自动跑**，否则等于永远不跑。

    只提供"立即清理"按钮的保留策略，在真实环境里的结局是版本一直堆到磁盘满——
    这种"设计上有、实际上没有"的功能比没做更危险，因为大家以为有。
    """
    headers = await env.auth()
    device_id = await make_device(env, headers)
    # 先造一个真实版本，再收紧策略
    async with env.database.session() as session:
        task = await env.runner.create_task(
            session,
            payload=BackupRequest(device_ids=[device_id], precheck=False),
        )
        await session.commit()
    env.runner.start(task.id)
    await wait_task(env, headers, task.id)

    async with env.database.session() as session:
        from app.services.retention import update_policy

        await update_policy(session, {"keep_unchanged_daily_days": 0, "keep_min_versions": 1})
        await session.commit()

    runner = ScheduleRunner(
        database=env.database,
        runner=env.runner,
        poll_interval=1.0,
        retention_interval_hours=12.0,
    )
    first = await runner.maybe_run_retention()
    assert first is not None, "首次应当执行一次清理"
    assert first["scanned_devices"] >= 1

    assert await runner.maybe_run_retention() is None, "同一个间隔内不该重复清理"

    # 把时间往后拨 13 小时（超过 12 小时的间隔）→ 应当再跑一次
    later = await runner.maybe_run_retention(now=utcnow() + timedelta(hours=13))
    assert later is not None, "过了间隔就应当再次清理"

    # 结果要落在策略上（界面"上次清理"读的就是它）
    policy = (await env.client.get("/api/v1/retention", headers=headers)).json()
    assert policy["last_run_at"] is not None
    assert policy["last_result"]["scanned_devices"] >= 1

    # 关掉自动清理（interval=0）后不再执行
    manual_only = ScheduleRunner(
        database=env.database, runner=env.runner, poll_interval=1.0, retention_interval_hours=0.0
    )
    assert await manual_only.maybe_run_retention() is None


async def test_task_list_can_filter_by_schedule_and_trigger(env: Env):
    """★ 任务关联要能**反向查**：策略行上只留了"上一次任务"，而运维真正要问的是
    "这个策略最近几次跑得怎么样"。没有这个过滤条件，界面上只能给他一个任务号，
    还得自己去任务列表里翻。
    """
    headers = await env.auth()
    device_id = await make_device(env, headers)
    schedule_a = await add_schedule(env, name="按策略查-A", scope={"device_ids": [device_id]})
    await add_schedule(env, name="按策略查-B", scope={"device_ids": [device_id]})

    fired = await make_runner(env).tick()
    assert len(fired) == 2
    for task_id in fired:
        await wait_task(env, headers, task_id)

    # 再来一个手工任务（不属于任何策略），用来证明过滤真的在起作用
    manual = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "手工任务", "precheck": False},
    )
    assert manual.status_code == 200, manual.text
    await wait_task(env, headers, manual.json()["id"])

    by_schedule = (
        await env.client.get(f"/api/v1/backups?schedule_id={schedule_a}", headers=headers)
    ).json()
    assert by_schedule["total"] == 1, "只应返回该策略发起的任务"
    assert by_schedule["items"][0]["schedule_id"] == schedule_a
    assert by_schedule["items"][0]["trigger"] == "scheduled"

    scheduled_only = (
        await env.client.get("/api/v1/backups?trigger=scheduled", headers=headers)
    ).json()
    assert scheduled_only["total"] == 2

    manual_only = (
        await env.client.get("/api/v1/backups?trigger=manual", headers=headers)
    ).json()
    assert manual_only["total"] == 1
    assert manual_only["items"][0]["schedule_id"] is None, "手工任务不关联策略"
