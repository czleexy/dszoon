"""分组作为"备份范围"的端到端测试：分组 × 立即备份 × 定时策略。

分组的唯一用途就是"给任务圈范围"，所以真正要守住的不变量是**范围不越界**：

* 按组备份只碰组内设备（组外设备一条采集记录都不该有）；
* 按组的定时策略在 tick 时只对组内设备发起任务，并且任务能追溯到该策略
  （``schedule_id``）——否则界面上"这次定时是谁发起的"就断了；
* 组里被停用的设备默认不采（运维临时停用一台设备，不该在夜间窗口被重新连上），
  但 ``include_disabled=true`` 时要把它们带上（"我要强制采一遍，包括停用的"）。

这些是跨模块行为（分组 → 设备解析 → 任务编排 → 调度器），单模块测试都覆盖不到。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from app.db.base import utcnow
from app.db.models import BackupTask, Schedule
from app.services.schedules import create_schedule
from app.services.scheduler import ScheduleRunner
from tests.support.env import HUAWEI_CONFIG, HUAWEI_VERSION, Env, fixture_text
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}


async def make_device(env: Env, headers: dict, *, index: int) -> int:
    host = f"10.41.0.{index}"
    credential = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": f"cred-g{index}", "username": "netadmin", "password": "pw-123456"},
    )
    assert credential.status_code == 200, credential.text
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": f"grp-dev-{index}",
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
            hostname=f"GRP-SW-{index}",
            config_text=fixture_text(HUAWEI_CONFIG),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return created.json()["id"]


async def make_group(env: Env, headers: dict, name: str, device_ids: list[int]) -> int:
    group = await env.client.post("/api/v1/groups", headers=headers, json={"name": name})
    assert group.status_code == 200, group.text
    group_id = group.json()["id"]
    members = await env.client.put(
        f"/api/v1/groups/{group_id}/devices",
        headers=headers,
        json={"device_ids": device_ids},
    )
    assert members.status_code == 200, members.text
    return group_id


async def wait_task(env: Env, headers: dict, task_id: int, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail.get("status") in TERMINAL:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束：{detail}")


async def test_batch_backup_by_group_only_touches_members(env: Env):
    """★ 按组备份只碰组内设备——组外设备一条采集记录都不该有。"""
    headers = await env.auth()
    member_a = await make_device(env, headers, index=1)
    member_b = await make_device(env, headers, index=2)
    outsider = await make_device(env, headers, index=3)
    group_id = await make_group(env, headers, "核心组", [member_a, member_b])

    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"group_id": group_id, "name": "按组备份", "precheck": False},
    )
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["total"] == 2, "只应包含组内两台设备"

    # ★ device_ids 只在**任务详情**里（TaskOut 是列表用的精简形状，没有它）——
    #   第一版测试直接读列表响应的 device_ids，于是 KeyError
    detail = await wait_task(env, headers, task["id"])
    assert detail["status"] == "success", detail
    assert sorted(detail["device_ids"]) == sorted([member_a, member_b])
    assert outsider not in detail["device_ids"], "组外设备绝不能进范围"

    for device_id in (member_a, member_b):
        runs = (await env.client.get(f"/api/v1/devices/{device_id}/runs", headers=headers)).json()
        assert runs["total"] == 1, f"组内设备 {device_id} 应当有一次采集记录"
    outsider_runs = (
        await env.client.get(f"/api/v1/devices/{outsider}/runs", headers=headers)
    ).json()
    assert outsider_runs["total"] == 0, "组外设备不该被采集"


async def test_schedule_scoped_by_group_fires_members_and_records_link(env: Env):
    """★ 按组的定时策略：tick 只对组内设备发起，并留下可追溯的 schedule_id。"""
    headers = await env.auth()
    member_b = await make_device(env, headers, index=12)
    outsider = await make_device(env, headers, index=13)
    group_id = await make_group(env, headers, "夜间组", [member_b])

    async with env.database.session() as session:
        schedule = await create_schedule(
            session,
            {
                "name": "按组夜间备份",
                "kind": "cron",
                "cron": "0 2 * * *",
                "group_id": group_id,
                "jitter_minutes": 0,
                "precheck": False,
            },
        )
        # 直接把它设成"已到期"：tick 只处理到期的策略，这样测试不必等时钟
        schedule.next_run_at = utcnow() - timedelta(minutes=1)
        await session.commit()
        schedule_id = schedule.id

    runner = ScheduleRunner(database=env.database, runner=env.runner, poll_interval=1.0)
    fired = await runner.tick()
    assert len(fired) == 1, "到期策略应当发起一个任务"

    detail = await wait_task(env, headers, fired[0])
    assert detail["status"] == "success", detail
    assert detail["total"] == 1, "只应包含组内那一台"
    assert detail["trigger"] == "scheduled"
    assert outsider not in detail["device_ids"]

    async with env.database.session() as session:
        task = await session.get(BackupTask, fired[0])
        assert task is not None
        assert task.schedule_id == schedule_id, "任务必须能追溯到发起它的策略"

        row = await session.get(Schedule, schedule_id)
        assert row is not None
        assert row.last_task_id == fired[0]
        # 任务终态与策略回写是同一个事务提交的（见 BackupRunner._finalize），
        # 所以这里不需要轮询等待
        assert row.last_status == "success", "任务终态要回写到策略上"


async def test_group_scope_skips_disabled_devices_unless_asked(env: Env):
    """组里被停用的设备默认不采，但 ``include_disabled=true`` 要能强制带上。

    运维临时停用一台设备（搬迁/维修）时，夜间定时任务不该再去连它；
    而"我就是要强制采一遍"也必须是能表达的。
    """
    headers = await env.auth()
    alive = await make_device(env, headers, index=21)
    parked = await make_device(env, headers, index=22)
    group_id = await make_group(env, headers, "混合组", [alive, parked])

    disabled = await env.client.patch(
        f"/api/v1/devices/{parked}", headers=headers, json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text

    default_scope = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"group_id": group_id, "name": "默认范围", "precheck": False},
    )
    assert default_scope.status_code == 200, default_scope.text
    assert default_scope.json()["total"] == 1, "停用设备默认不该进范围"
    default_detail = await wait_task(env, headers, default_scope.json()["id"])
    assert default_detail["device_ids"] == [alive], default_detail

    forced = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={
            "group_id": group_id,
            "name": "强制包含停用",
            "precheck": False,
            "include_disabled": True,
        },
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["total"] == 2, "include_disabled=true 时要把停用设备带上"
    detail = await wait_task(env, headers, forced.json()["id"])
    assert detail["status"] == "success", detail

    parked_runs = (
        await env.client.get(f"/api/v1/devices/{parked}/runs", headers=headers)
    ).json()
    assert parked_runs["total"] == 1, "强制采集后停用设备应当有记录"


async def test_group_scope_with_no_members_reports_clearly(env: Env):
    """空分组的范围解析不出设备：报 404 且信息可读，而不是建一个 0 台的任务。"""
    headers = await env.auth()
    group_id = await make_group(env, headers, "空组", [])

    response = await env.client.post(
        "/api/v1/backups", headers=headers, json={"group_id": group_id, "name": "空组备份"}
    )
    assert response.status_code == 404, response.text
    assert "没有匹配到任何设备" in response.json()["detail"]
