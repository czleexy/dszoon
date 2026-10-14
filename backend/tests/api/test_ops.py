"""P2 运营接口测试：分组 / 定时策略 / 通知渠道 / 保留策略。

这些用例盯住的是"配置类接口"最容易出错的地方：

* **密钥与凭据不外泄**——机器人 URL 里的 access_token、SMTP 口令；
* **改配置不打乱既有节奏**——手工触发不该把 02:00 的班次挪走；
* **权限分层**——读谁都行，改要 operator，删/清理要 admin；
* **写操作留审计**——自动化配置出问题时必须能查到是谁改的。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta

import pytest

from app.db.base import utcnow
from app.db.models import AuditLog, ConfigSnapshot, Device
from app.errors import NotFound
from app.schemas.ops import ChannelCreate
from app.services import notifications
from app.services.notifications import EVENTS, KINDS
from tests.support.env import (
    HUAWEI_CONFIG,
    HUAWEI_VERSION,
    VIEWER_PASSWORD,
    Env,
    fixture_text,
    with_config_change,
)
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}


async def make_device(
    env: Env, headers: dict, *, host: str = "10.9.0.1", name: str = "ops-dev"
) -> int:
    """建好凭据 + 设备，并把假设备注册进会话工厂（这样采集层不会去连真机）。"""
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
            hostname="OPS-SW",
            config_text=fixture_text(HUAWEI_CONFIG),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return created.json()["id"]


async def wait_task(env: Env, headers: dict, task_id: int, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail.get("status") in TERMINAL:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束：{detail}")


async def make_viewer(env: Env) -> dict:
    created = await env.client.post(
        "/api/v1/users",
        headers=await env.auth(),
        json={"username": "reader", "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    return await env.auth("reader", VIEWER_PASSWORD)


# ==========================================================================
# 设备分组
# ==========================================================================
async def test_group_crud_and_membership(env: Env):
    headers = await env.auth()
    first = await make_device(env, headers, host="10.9.0.1", name="g1")
    second = await make_device(env, headers, host="10.9.0.2", name="g2")

    created = await env.client.post(
        "/api/v1/groups",
        headers=headers,
        json={"name": "核心交换机", "description": "A 栋", "color": "#409EFF"},
    )
    assert created.status_code == 200, created.text
    group = created.json()
    assert group["device_count"] == 0

    added = await env.client.put(
        f"/api/v1/groups/{group['id']}/devices",
        headers=headers,
        json={"device_ids": [first, second]},
    )
    assert added.status_code == 200, added.text
    assert added.json() == {"affected": 2, "device_count": 2}

    listing = await env.client.get("/api/v1/groups", headers=headers)
    assert listing.status_code == 200
    assert [item["device_count"] for item in listing.json()] == [2]

    renamed = await env.client.patch(
        f"/api/v1/groups/{group['id']}", headers=headers, json={"name": "核心交换"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "核心交换"

    async with env.database.session() as session:
        device = await session.get(Device, first)
        assert device is not None and device.group_id == group["id"], "设备归属要真的落库"


async def test_group_replace_false_appends(env: Env):
    """``replace=False`` 是追加语义，不会把已有成员踢出去。"""
    headers = await env.auth()
    first = await make_device(env, headers, host="10.9.1.1", name="a1")
    second = await make_device(env, headers, host="10.9.1.2", name="a2")
    group = (
        await env.client.post("/api/v1/groups", headers=headers, json={"name": "追加组"})
    ).json()

    await env.client.put(
        f"/api/v1/groups/{group['id']}/devices", headers=headers, json={"device_ids": [first]}
    )
    appended = await env.client.put(
        f"/api/v1/groups/{group['id']}/devices",
        headers=headers,
        json={"device_ids": [second], "replace": False},
    )
    assert appended.json()["device_count"] == 2

    replaced = await env.client.put(
        f"/api/v1/groups/{group['id']}/devices",
        headers=headers,
        json={"device_ids": [second], "replace": True},
    )
    assert replaced.json()["device_count"] == 1, "replace=True 时其余成员应被移出"


async def test_group_name_conflict(env: Env):
    headers = await env.auth()
    await env.client.post("/api/v1/groups", headers=headers, json={"name": "重名组"})
    again = await env.client.post("/api/v1/groups", headers=headers, json={"name": "重名组"})
    assert again.status_code == 409


async def test_group_rejects_unknown_device(env: Env):
    headers = await env.auth()
    group = (
        await env.client.post("/api/v1/groups", headers=headers, json={"name": "空组"})
    ).json()
    response = await env.client.put(
        f"/api/v1/groups/{group['id']}/devices", headers=headers, json={"device_ids": [4242]}
    )
    assert response.status_code == 404


async def test_delete_group_keeps_devices(env: Env):
    """删分组不能连带删设备——这是运维最怕的事故。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.9.2.1", name="keepme")
    group = (
        await env.client.post("/api/v1/groups", headers=headers, json={"name": "待删组"})
    ).json()
    await env.client.put(
        f"/api/v1/groups/{group['id']}/devices", headers=headers, json={"device_ids": [device_id]}
    )

    removed = await env.client.delete(f"/api/v1/groups/{group['id']}", headers=headers)
    assert removed.status_code == 200, removed.text

    assert (await env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).status_code == 200
    async with env.database.session() as session:
        device = await session.get(Device, device_id)
        assert device is not None and device.group_id is None, "只应解除归属"


async def test_device_list_exposes_group_and_filters_by_it(env: Env):
    """★ 设备列表必须带 group_id，并支持按分组过滤。

    否则前端的"成员设备"对话框只能从全量设备里前端过滤：设备上千台时列表被
    limit 截断 → 预选不全 → 一保存"整体替换"就把看不见的成员全踢出分组。
    """
    headers = await env.auth()
    first = await make_device(env, headers, host="10.9.4.1", name="m1")
    await make_device(env, headers, host="10.9.4.2", name="m2")
    group = (
        await env.client.post("/api/v1/groups", headers=headers, json={"name": "筛选组"})
    ).json()
    await env.client.put(
        f"/api/v1/groups/{group['id']}/devices", headers=headers, json={"device_ids": [first]}
    )

    in_group = (
        await env.client.get(f"/api/v1/devices?group_id={group['id']}", headers=headers)
    ).json()
    assert in_group["total"] == 1
    assert in_group["items"][0]["id"] == first
    assert in_group["items"][0]["group_id"] == group["id"], "列表要回显分组，前端才能预选成员"

    all_devices = (await env.client.get("/api/v1/devices", headers=headers)).json()
    assert all_devices["total"] == 2, "不带 group_id 时不应过滤"


# ==========================================================================
# 定时策略
# ==========================================================================
async def test_schedule_create_list_and_describe(env: Env):
    headers = await env.auth()
    created = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "每天凌晨 2 点",
            "kind": "cron",
            "cron": "0 2 * * *",
            "all_enabled": True,
            "jitter_minutes": 0,
        },
    )
    assert created.status_code == 200, created.text
    schedule = created.json()
    assert schedule["description"].startswith("每天 02:00")
    assert schedule["next_run_at"].endswith("Z"), "时间必须带时区标记，否则前端会差 8 小时"
    assert schedule["enabled"] is True

    listing = await env.client.get("/api/v1/schedules", headers=headers)
    assert [item["name"] for item in listing.json()] == ["每天凌晨 2 点"]


async def test_schedule_rejects_bad_cron_and_missing_scope(env: Env):
    headers = await env.auth()
    bad_cron = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={"name": "坏 cron", "kind": "cron", "cron": "每天两点", "all_enabled": True},
    )
    assert bad_cron.status_code == 422
    assert "cron" in bad_cron.json()["detail"]

    no_scope = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={"name": "没范围", "kind": "cron", "cron": "0 2 * * *"},
    )
    assert no_scope.status_code == 422
    assert "范围" in no_scope.json()["detail"]


async def test_schedule_with_impossible_window_is_rejected(env: Env):
    """★ "永远不会有执行时刻"的策略必须在**创建时**就被拒绝。

    窗口 02:00–02:05 配 cron ``0 3 * * *``：每次触发都在窗口外，探测 200 次后放弃。
    不拦的话策略会被"建成功"，界面上"下次执行"显示 `-`，然后**永远不跑**——等到
    有人发现"这个策略好像没执行过"时，已经过去几周了。
    """
    headers = await env.auth()
    response = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "永不执行的策略",
            "kind": "cron",
            "cron": "0 3 * * *",
            "window_start": "02:00",
            "window_end": "02:05",
            "all_enabled": True,
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "永远" in detail and "窗口" in detail, detail

    # 反过来：窗口与 cron 有交集时必须能建成功，并算出下次执行时刻
    ok_response = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "凌晨整点窗口",
            "kind": "cron",
            "cron": "0 3 * * *",
            "window_start": "02:00",
            "window_end": "04:00",
            "all_enabled": True,
        },
    )
    assert ok_response.status_code == 200, ok_response.text
    assert ok_response.json()["next_run_at"], "有交集的策略必须算出下次执行时刻"

    # 停用的策略不参与这个检查（它本来就不该有下次执行时间）
    disabled = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "停用的策略",
            "kind": "cron",
            "cron": "0 3 * * *",
            "window_start": "02:00",
            "window_end": "02:05",
            "all_enabled": True,
            "enabled": False,
        },
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["next_run_at"] is None


async def test_schedule_duplicate_name_conflicts(env: Env):
    headers = await env.auth()
    body = {"name": "重复策略", "kind": "cron", "cron": "0 3 * * *", "all_enabled": True}
    assert (await env.client.post("/api/v1/schedules", headers=headers, json=body)).status_code == 200
    assert (await env.client.post("/api/v1/schedules", headers=headers, json=body)).status_code == 409


async def test_disabling_schedule_clears_next_run(env: Env):
    headers = await env.auth()
    schedule = (
        await env.client.post(
            "/api/v1/schedules",
            headers=headers,
            json={"name": "可停用", "kind": "interval", "interval_minutes": 60, "all_enabled": True},
        )
    ).json()
    disabled = await env.client.patch(
        f"/api/v1/schedules/{schedule['id']}", headers=headers, json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["next_run_at"] is None, "停用后不该留着下一次执行时间"


async def test_schedule_preview_returns_future_times_in_order(env: Env):
    headers = await env.auth()
    schedule = (
        await env.client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "每 15 分钟",
                "kind": "cron",
                "cron": "*/15 * * * *",
                "all_enabled": True,
                "jitter_minutes": 0,
            },
        )
    ).json()

    preview = await env.client.get(
        f"/api/v1/schedules/{schedule['id']}/preview?count=3", headers=headers
    )
    assert preview.status_code == 200, preview.text
    times = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in preview.json()["times"]]
    assert len(times) == 3
    # 预览必须是"接下来"的时刻，不能把过去的时间给界面
    assert times[0] >= datetime.now(tz=times[0].tzinfo) - timedelta(minutes=1)
    assert times == sorted(times), "预览时刻必须递增"
    assert times[1] - times[0] == timedelta(minutes=15)


async def test_window_narrows_preview_into_window(env: Env):
    """执行窗口要真的生效：只在 02:00–03:00 之间挑时刻。"""
    headers = await env.auth()
    schedule = (
        await env.client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "窗口内",
                "kind": "cron",
                "cron": "0 * * * *",
                "window_start": "02:00",
                "window_end": "03:00",
                "all_enabled": True,
                "jitter_minutes": 0,
            },
        )
    ).json()
    preview = await env.client.get(
        f"/api/v1/schedules/{schedule['id']}/preview?count=2", headers=headers
    )
    assert preview.status_code == 200, preview.text
    hours = {
        datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone().hour
        for t in preview.json()["times"]
    }
    # 窗口 02:00–03:00 是闭区间，每小时一次的触发落在 02:00 与 03:00 两个点上
    assert hours == {2, 3}, f"窗口外的时刻不该出现：{hours}"


async def test_manual_run_creates_task_without_moving_next_run(env: Env):
    """★ 手工触发是"插队"，不能把每天 02:00 的班次整体推后。

    如果实现里顺手复用了调度器的 next_run_at 推进逻辑，"立即执行一次"就会
    悄悄把定时节奏改掉——这种问题几乎无法从界面上看出来。
    """
    headers = await env.auth()
    device_id = await make_device(env, headers)
    created = await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "手工触发",
            "kind": "cron",
            "cron": "0 2 * * *",
            "device_ids": [device_id],
            "precheck": False,
        },
    )
    assert created.status_code == 200, created.text
    schedule = created.json()

    triggered = await env.client.post(
        f"/api/v1/schedules/{schedule['id']}/run", headers=headers
    )
    assert triggered.status_code == 200, triggered.text
    task = triggered.json()
    assert task["trigger"] == "manual"
    assert task["total"] == 1

    detail = await wait_task(env, headers, task["id"])
    assert detail["status"] == "success", detail

    after = (
        await env.client.get(f"/api/v1/schedules/{schedule['id']}", headers=headers)
    ).json()
    assert after["next_run_at"] == schedule["next_run_at"], "手工执行不该改变既定班次"
    assert after["last_task_id"] == task["id"]
    assert after["last_status"] == "success"


async def test_manual_run_refused_while_previous_task_running(env: Env):
    """同一个策略叠两个任务没有意义，还会互相争设备锁 → 直接 409。"""
    headers = await env.auth()
    device_id = await make_device(env, headers)
    schedule = (
        await env.client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "别叠加",
                "kind": "cron",
                "cron": "0 4 * * *",
                "device_ids": [device_id],
            },
        )
    ).json()

    # 造一个"仍在排队"的任务：只建不启动
    async with env.database.session() as session:
        from app.schemas.backup import BackupRequest

        task = await env.runner.create_task(
            session, payload=BackupRequest(device_ids=[device_id])
        )
        from app.db.models import Schedule

        row = await session.get(Schedule, schedule["id"])
        assert row is not None
        row.last_task_id = task.id
        await session.commit()

    blocked = await env.client.post(f"/api/v1/schedules/{schedule['id']}/run", headers=headers)
    assert blocked.status_code == 409
    assert "仍在运行" in blocked.json()["detail"]


async def test_manual_run_with_unmatched_scope_is_422(env: Env):
    """范围解析不出设备时报 422 而不是 500，前端才能给出可读提示。"""
    headers = await env.auth()
    schedule = (
        await env.client.post(
            "/api/v1/schedules",
            headers=headers,
            json={
                "name": "空范围手工跑",
                "kind": "cron",
                "cron": "0 5 * * *",
                "site": "查无此站",
            },
        )
    ).json()
    response = await env.client.post(f"/api/v1/schedules/{schedule['id']}/run", headers=headers)
    assert response.status_code == 422, response.text


async def test_delete_schedule(env: Env):
    headers = await env.auth()
    schedule = (
        await env.client.post(
            "/api/v1/schedules",
            headers=headers,
            json={"name": "待删策略", "kind": "interval", "interval_minutes": 120, "all_enabled": True},
        )
    ).json()
    assert (
        await env.client.delete(f"/api/v1/schedules/{schedule['id']}", headers=headers)
    ).status_code == 200
    assert (
        await env.client.get(f"/api/v1/schedules/{schedule['id']}", headers=headers)
    ).status_code == 404


# ==========================================================================
# 通知渠道
# ==========================================================================
def test_channel_kind_literal_matches_service_enum():
    """schema 里的 Literal 必须与服务层的 KINDS 完全一致。

    写错一个词（例如把 smtp 写成 email）不会在导入期报错，但会让该类渠道
    在建的时候全部 422——用一条断言把这种漂移钉死在测试里。
    """
    from typing import get_args

    literal = set(get_args(ChannelCreate.model_fields["kind"].annotation))
    assert literal == set(KINDS)


async def test_channel_webhook_url_is_redacted(env: Env):
    """★ 机器人 URL 本身就是凭据，列表接口不能原样吐出来。"""
    headers = await env.auth()
    created = await env.client.post(
        "/api/v1/channels",
        headers=headers,
        json={
            "name": "告警群",
            "kind": "dingtalk",
            "config": {"url": "https://oapi.dingtalk.com/robot/send?access_token=SECRET123"},
            "secret": {"secret": "SIGNKEY"},
            "events": ["task_failed"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert "SECRET123" not in body["config"]["url"]
    assert body["config"]["url"].endswith("access_token=***")
    assert body["has_secret"] is True
    assert "SIGNKEY" not in created.text

    listing = await env.client.get("/api/v1/channels", headers=headers)
    assert "SECRET123" not in listing.text


async def test_channel_update_with_masked_url_keeps_real_url(env: Env):
    """前端回传的是打码 URL，改别的字段不能把真实地址写坏。"""
    headers = await env.auth()
    real_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REALKEY"
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={"name": "企微群", "kind": "wecom", "config": {"url": real_url}},
        )
    ).json()
    assert channel["config"]["url"].endswith("key=***")

    updated = await env.client.patch(
        f"/api/v1/channels/{channel['id']}",
        headers=headers,
        json={"config": {"url": channel["config"]["url"]}, "events": ["task_partial"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["events"] == ["task_partial"]

    async with env.database.session() as session:
        from app.db.models import NotificationChannel

        row = await session.get(NotificationChannel, channel["id"])
        assert row is not None
        assert row.config["url"] == real_url, "库里的真实地址必须被保留"


async def test_channel_validation(env: Env):
    headers = await env.auth()
    no_url = await env.client.post(
        "/api/v1/channels",
        headers=headers,
        json={"name": "缺地址", "kind": "webhook", "config": {}},
    )
    assert no_url.status_code == 422

    bad_event = await env.client.post(
        "/api/v1/channels",
        headers=headers,
        json={
            "name": "坏事件",
            "kind": "webhook",
            "config": {"url": "https://example.com/hook"},
            "events": ["task_exploded"],
        },
    )
    assert bad_event.status_code == 422, bad_event.text


async def test_notification_events_endpoint(env: Env):
    headers = await env.auth()
    response = await env.client.get("/api/v1/notification-events", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["value"] for item in body["events"]} == set(EVENTS)
    assert {item["value"] for item in body["kinds"]} == set(KINDS)
    assert all(item["label"] for item in body["kinds"]), "前端下拉框需要中文名"
    assert set(body["default_events"]) <= set(EVENTS)


async def test_channel_test_endpoint_sends_and_logs(env: Env, monkeypatch: pytest.MonkeyPatch):
    """点"测试"要真发出去，并把结果记进发送记录与渠道状态。"""
    sent: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        sent.append((url, payload))
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "测试渠道",
                "kind": "wecom",
                "config": {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K"},
            },
        )
    ).json()

    result = await env.client.post(f"/api/v1/channels/{channel['id']}/test", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True
    assert sent and "key=K" in sent[0][0], "发出去时用的是真实地址（不是打码后的）"

    logs = await env.client.get(
        f"/api/v1/notification-logs?channel_id={channel['id']}", headers=headers
    )
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]
    assert items and items[0]["event"] == "test" and items[0]["status"] == "success"

    refreshed = (await env.client.get("/api/v1/channels", headers=headers)).json()[0]
    assert refreshed["last_status"] == "success"
    assert refreshed["last_error"] is None


async def test_channel_test_failure_is_reported_not_raised(env: Env, monkeypatch: pytest.MonkeyPatch):
    """渠道不通是预期结果，不该变成 500：接口要能告诉运维"为什么不通"。"""

    async def failing_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        return False, "ConnectError: 目标不可达"

    monkeypatch.setattr(notifications, "_post_json", failing_post)

    headers = await env.auth()
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "不通的渠道",
                "kind": "feishu",
                "config": {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"},
            },
        )
    ).json()

    result = await env.client.post(f"/api/v1/channels/{channel['id']}/test", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is False
    assert "ConnectError" in result.json()["detail"]

    refreshed = (await env.client.get("/api/v1/channels", headers=headers)).json()[0]
    assert refreshed["last_status"] == "failed"
    assert refreshed["last_error"], "失败原因要留在渠道上，便于排查"


async def test_deleting_channel_keeps_notification_history(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    """渠道删了，历史发送记录还要能查——排查"当时到底发出去没有"全靠它。"""

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "短命渠道",
                "kind": "webhook",
                "config": {"url": "https://example.com/hook?token=T"},
            },
        )
    ).json()
    await env.client.post(f"/api/v1/channels/{channel['id']}/test", headers=headers)
    assert (await env.client.delete(f"/api/v1/channels/{channel['id']}", headers=headers)).status_code == 200

    logs = (await env.client.get("/api/v1/notification-logs", headers=headers)).json()
    assert logs["total"] == 1, "记录不该跟着渠道一起消失"
    assert logs["items"][0]["channel_name"] == "短命渠道", "渠道名冗余存了一份，删了也读得到"
    assert logs["items"][0]["channel_id"] is None, "外键应被置空（ON DELETE SET NULL）"


async def wait_logs(env: Env, headers: dict, channel_id: int, *, timeout: float = 10.0) -> dict:
    """等发送记录出现。

    通知是在任务结束**之后**才派发的（另开一个会话，见 ``BackupRunner._notify``），
    所以"任务已终态"并不等于"通知已发出"，这里必须等一下。
    """
    deadline = time.monotonic() + timeout
    page: dict = {"total": 0, "items": []}
    while time.monotonic() < deadline:
        page = (
            await env.client.get(
                f"/api/v1/notification-logs?channel_id={channel_id}", headers=headers
            )
        ).json()
        if page["total"]:
            return page
        await asyncio.sleep(0.1)
    return page


async def test_failed_task_triggers_channel_and_writes_log(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    """★ 触发规则要真的接在"任务结果"上：任务失败 → 自动发通知 → 落发送记录。

    这是 P2 通知功能的**主链路**（另外两个测试只覆盖"手工点测试"）。
    漏了这条自动链路，渠道配好了也永远收不到告警，而且界面上看不出问题。
    """
    sent: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        sent.append((url, payload))
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    device_id = await make_device(env, headers)
    # 把这台设备换成"连不上"：任务会整体失败，命中 task_failed 事件
    env.factory.register(
        "10.9.0.1",
        make_fake_device(
            vendor="huawei", hostname="OPS-SW", config_text="", fail_with="unreachable"
        ),
    )

    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "失败告警群",
                "kind": "webhook",
                "config": {"url": "https://example.com/hook?token=T"},
                "events": ["task_failed"],
                "min_failures": 1,
            },
        )
    ).json()

    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "会失败的任务", "precheck": False},
    )
    assert response.status_code == 200, response.text
    detail = await wait_task(env, headers, response.json()["id"])
    assert detail["status"] == "failed", detail

    page = await wait_logs(env, headers, channel["id"])
    assert page["total"] == 1, f"任务失败后应当自动发一条通知并留记录：{page}"
    entry = page["items"][0]
    assert "task_failed" in entry["event"]
    assert entry["status"] == "success"
    assert entry["task_id"] == response.json()["id"], "记录要能对上是哪次任务"
    assert sent, "发送函数没有被调用，说明自动触发链路没接上"
    assert "task_failed" in sent[0][1]["event"]

    refreshed = (await env.client.get("/api/v1/channels", headers=headers)).json()[0]
    assert refreshed["last_status"] == "success"


async def test_device_failed_streak_event_fires_only_after_repeated_failures(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    """★ "某台设备连续多次失败"必须是一个**真的会触发**的事件。

    它曾经只被当成邮件/消息正文里的一段内容传下去，从不进 ``events``：后果是
    "只订阅 device_failed_streak"的渠道永远收不到消息——界面上能选、实际不生效，
    比没有这个选项更糟（运维会以为有人在盯）。
    """
    sent: list[dict] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        sent.append(payload)
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    device_id = await make_device(env, headers)
    env.factory.register(
        "10.9.0.1",
        make_fake_device(
            vendor="huawei", hostname="OPS-SW", config_text="", fail_with="unreachable"
        ),
    )
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "只看连续失败",
                "kind": "webhook",
                "config": {"url": "https://example.com/streak"},
                # 只订阅这一个事件：任何"顺带触发"都会让这条测试失去区分度
                "events": ["device_failed_streak"],
            },
        )
    ).json()

    async def run_once() -> None:
        response = await env.client.post(
            "/api/v1/backups",
            headers=headers,
            json={"device_ids": [device_id], "name": "连续失败", "precheck": False},
        )
        assert response.status_code == 200, response.text
        detail = await wait_task(env, headers, response.json()["id"])
        assert detail["status"] == "failed", detail

    # 第 1、2 次：还没到"连续 3 次"，不该打扰
    await run_once()
    await run_once()
    logs = (
        await env.client.get(
            f"/api/v1/notification-logs?channel_id={channel['id']}", headers=headers
        )
    ).json()
    assert logs["total"] == 0, f"连续 2 次失败还不到阈值，不该发通知：{logs}"
    assert sent == [], "未达到连续失败阈值时不该调用发送函数"

    # 第 3 次：连续 3 次失败 → 事件成立
    await run_once()
    page = await wait_logs(env, headers, channel["id"])
    assert page["total"] == 1, f"连续 3 次失败后应当触发一次：{page}"
    assert "device_failed_streak" in page["items"][0]["event"]
    assert sent, "到达阈值后应当真的调用发送函数"
    assert "device_failed_streak" in sent[-1]["event"]
    # 正文要说清是哪台设备在连续失败，否则收到通知的人还得自己去查
    assert "ops-dev" in sent[-1]["text"]


async def test_channel_event_subscription_is_respected(env: Env, monkeypatch: pytest.MonkeyPatch):
    """只有**订阅了**该事件的渠道才收到消息（没订阅的渠道不该被打扰）。

    ★ 必须是**第二次**采集且内容真的变了：首次采集时 ``run.changed`` 是 ``None``
    （"没有上一版可比"），``detect_events`` 不把它当变更——这是有意的设计，
    否则每台设备第一次备份都会发一条"配置发生变更"。第一版测试拿首次采集当"变更"，
    于是订阅者收不到消息而红了。
    """
    sent: list[str] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        sent.append(url)
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    device_id = await make_device(env, headers)
    subscriber = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "只订阅失败",
                "kind": "webhook",
                "config": {"url": "https://example.com/fail"},
                "events": ["task_failed"],
            },
        )
    ).json()
    bystander = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "订阅变更",
                "kind": "webhook",
                "config": {"url": "https://example.com/change"},
                "events": ["significant_change"],
            },
        )
    ).json()

    # 首次采集：建立基线（此时不应产生"配置变更"事件）
    first = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "基线采集", "precheck": False},
    )
    await wait_task(env, headers, first.json()["id"])

    # 把设备配置改掉（变更行插在结尾终止符之前，否则会被判成 bad_terminator）
    env.factory.register(
        "10.9.0.1",
        make_fake_device(
            vendor="huawei",
            hostname="OPS-SW",
            config_text=with_config_change(fixture_text(HUAWEI_CONFIG)),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    second = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "变更采集", "precheck": False},
    )
    detail = await wait_task(env, headers, second.json()["id"])
    assert detail["status"] == "success", detail

    # 订阅"变更"的渠道应当收到；订阅"失败"的渠道不该收到
    page = await wait_logs(env, headers, bystander["id"])
    assert page["total"] == 1, "首次采集属于配置变更，订阅者应当收到"
    assert "significant_change" in page["items"][0]["event"]

    quiet = (
        await env.client.get(
            f"/api/v1/notification-logs?channel_id={subscriber['id']}", headers=headers
        )
    ).json()
    assert quiet["total"] == 0, "没订阅该事件的渠道不该收到消息"
    assert any("change" in url for url in sent), "订阅者应当真的被调用到"
    assert all("fail" not in url for url in sent), "不该往未订阅的渠道发"


# ==========================================================================
# 保留策略
# ==========================================================================
async def test_retention_defaults_are_conservative(env: Env):
    headers = await env.auth()
    response = await env.client.get("/api/v1/retention", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["keep_changed_days"] is None, "变更版本默认永久保留"
    assert body["keep_min_versions"] >= 1
    assert "永久保留" in body["description"]


async def test_retention_update_and_clear_back_to_permanent(env: Env):
    headers = await env.auth()
    updated = await env.client.put(
        "/api/v1/retention", headers=headers, json={"keep_changed_days": 30}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["keep_changed_days"] == 30

    # 显式传 null = 改回"永久保留"（服务层把 None 当"不修改"，所以这里必须显式清空）
    cleared = await env.client.put(
        "/api/v1/retention", headers=headers, json={"keep_changed_days": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["keep_changed_days"] is None


async def test_retention_rejects_invalid_min_versions(env: Env):
    headers = await env.auth()
    response = await env.client.put("/api/v1/retention", headers=headers, json={"keep_min_versions": 0})
    assert response.status_code == 422


async def add_snapshot_history(env: Env, device_id: int, *, days_ago: list[int]) -> list[int]:
    """直接落库造历史版本：sha 各不相同（模拟"每次采到的正文都不同"），
    但 ``normalized_sha256`` 全部相同（模拟"归一化后没有实质变更"）。

    这正是保留策略里"未变更版本按天折叠"要处理的形态。
    """
    blobs = env.app.state.blobs
    ids: list[int] = []
    async with env.database.session() as session:
        for offset in days_ago:
            captured = utcnow() - timedelta(days=offset)
            stored = blobs.put_text(f"hostname OPS-SW\n# rev {offset}\n")
            # 把正文文件的 mtime 也拨回对应日期：它代表的就是"几天前那个版本"。
            # 清理会给"刚写下的文件"留宽限期（见 retention.BLOB_GRACE_SECONDS），
            # 不拨回去的话这些正文会被当成"可能正在写库"而放过。
            stamp = captured.timestamp()
            os.utime(stored.path, (stamp, stamp))
            snapshot = ConfigSnapshot(
                device_id=device_id,
                sha256=stored.sha256,
                normalized_sha256="n" * 64,
                object_key=stored.object_key,
                size_bytes=stored.size_bytes,
                line_count=2,
                captured_at=captured,
            )
            session.add(snapshot)
            await session.flush()
            ids.append(snapshot.id)
        await session.commit()
    return ids


async def test_retention_run_prunes_and_reclaims_blobs(env: Env):
    """执行清理要真的删版本、并回收没人引用的正文文件。"""
    headers = await env.auth()
    device_id = await make_device(env, headers)
    snapshot_ids = await add_snapshot_history(env, device_id, days_ago=[10, 8, 6, 4])

    blobs = env.app.state.blobs
    before = blobs.stats()["objects"]

    await env.client.put(
        "/api/v1/retention",
        headers=headers,
        json={"keep_unchanged_daily_days": 0, "keep_min_versions": 2},
    )
    result = await env.client.post("/api/v1/retention/run", headers=headers)
    assert result.status_code == 200, result.text
    summary = result.json()["summary"]
    assert summary["scanned_devices"] == 1
    assert summary["scanned_snapshots"] == 4
    assert summary["deleted_snapshots"] == 1, "最早 1 个 + 最新 2 个保留，中间那个该删"
    assert summary["deleted_blobs"] == 1, "没人引用的正文文件要一起回收"
    assert summary["freed_bytes"] > 0
    assert blobs.stats()["objects"] == before - 1

    async with env.database.session() as session:
        from sqlalchemy import select

        remaining = list(
            (
                await session.execute(
                    select(ConfigSnapshot.id).where(ConfigSnapshot.device_id == device_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(remaining) == 3
    assert snapshot_ids[1] not in remaining

    policy = (await env.client.get("/api/v1/retention", headers=headers)).json()
    assert policy["description"], "改完策略后描述要跟着更新"
    assert policy["last_run_at"] is not None, "上次清理时间要回显，否则界面上看不出跑过没有"
    assert policy["last_result"]["deleted_snapshots"] == 1


async def test_retention_disabled_does_nothing(env: Env):
    """停用后连"扫一遍"都不做——避免误以为还在清理。"""
    headers = await env.auth()
    device_id = await make_device(env, headers)
    await add_snapshot_history(env, device_id, days_ago=[10, 8, 6, 4])
    await env.client.put(
        "/api/v1/retention", headers=headers, json={"enabled": False, "keep_unchanged_daily_days": 0}
    )
    result = await env.client.post("/api/v1/retention/run", headers=headers)
    summary = result.json()["summary"]
    assert summary["skipped"] is True
    assert summary["deleted_snapshots"] == 0


# ==========================================================================
# 权限与审计
# ==========================================================================
async def test_reader_can_look_but_not_touch(env: Env):
    headers = await env.auth()
    viewer = await make_viewer(env)
    group = (
        await env.client.post("/api/v1/groups", headers=headers, json={"name": "权限组"})
    ).json()

    for url in (
        "/api/v1/groups",
        "/api/v1/schedules",
        "/api/v1/channels",
        "/api/v1/retention",
        "/api/v1/notification-logs",
        "/api/v1/notification-events",
    ):
        assert (await env.client.get(url, headers=viewer)).status_code == 200, url

    assert (
        await env.client.post("/api/v1/groups", headers=viewer, json={"name": "偷偷建组"})
    ).status_code == 403
    assert (
        await env.client.post(
            "/api/v1/channels",
            headers=viewer,
            json={"name": "偷偷建渠道", "kind": "webhook", "config": {"url": "https://x/y"}},
        )
    ).status_code == 403
    # 删除与清理是不可逆动作，viewer 连 operator 都不够
    assert (await env.client.delete(f"/api/v1/groups/{group['id']}", headers=viewer)).status_code == 403
    assert (await env.client.post("/api/v1/retention/run", headers=viewer)).status_code == 403


async def test_ops_writes_are_audited(env: Env):
    """自动化配置的改动必须可追溯到人。"""
    headers = await env.auth()
    await env.client.post("/api/v1/groups", headers=headers, json={"name": "审计组"})
    await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={"name": "审计策略", "kind": "cron", "cron": "0 6 * * *", "all_enabled": True},
    )
    await env.client.post("/api/v1/retention/run", headers=headers)

    async with env.database.session() as session:
        from sqlalchemy import select

        rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        )
    actions = [row.action for row in rows]
    for expected in ("group.create", "schedule.create", "retention.run"):
        assert expected in actions, f"缺少审计动作 {expected}：{actions}"
    assert all(row.username == "admin" for row in rows if row.action in actions)
    assert all(row.client_ip for row in rows if row.action.startswith(("group.", "schedule.")))


async def test_audit_records_the_real_backup_scope(env: Env):
    """★ 审计必须记下"这次备份覆盖了谁"。

    按分组发起的批量备份，如果审计只记 device_ids/site/vendor/all_enabled，
    那一条记录的范围就是空的——事后查"昨晚那次全量是谁发起的、覆盖了哪些设备"
    时会一无所知。定时策略的范围同理（谁把夜间任务改成全量了）。
    """
    from sqlalchemy import select

    headers = await env.auth()
    device_id = await make_device(env, headers)
    created_group = await env.client.post(
        "/api/v1/groups", headers=headers, json={"name": "审计范围组"}
    )
    assert created_group.status_code == 200, created_group.text
    group_id = created_group.json()["id"]
    added = await env.client.put(
        f"/api/v1/groups/{group_id}/devices",
        headers=headers,
        json={"device_ids": [device_id]},
    )
    assert added.status_code == 200, added.text

    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"group_id": group_id, "name": "按组审计", "precheck": False},
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    await wait_task(env, headers, task_id)

    await env.client.post(
        "/api/v1/schedules",
        headers=headers,
        json={
            "name": "审计范围策略",
            "kind": "cron",
            "cron": "0 2 * * *",
            "device_ids": [device_id],
        },
    )

    async with env.database.session() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action.in_(("backup.start", "schedule.create")))
                    .order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )

    backup_row = next(row for row in rows if row.action == "backup.start")
    assert backup_row.detail["scope"]["group_id"] == group_id, backup_row.detail
    assert backup_row.detail["scope"]["include_disabled"] is False

    schedule_row = next(row for row in rows if row.action == "schedule.create")
    assert schedule_row.detail["scope"] == {"device_ids": [device_id]}, schedule_row.detail


async def test_schedule_and_channel_404s(env: Env):
    headers = await env.auth()
    assert (await env.client.get("/api/v1/schedules/999", headers=headers)).status_code == 404
    assert (await env.client.get("/api/v1/channels/999", headers=headers)).status_code == 404
    assert (await env.client.get("/api/v1/groups/999", headers=headers)).status_code == 404
    with pytest.raises(NotFound):
        async with env.database.session() as session:
            from app.services.groups import get_group

            await get_group(session, 999)
