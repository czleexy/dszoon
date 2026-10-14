"""首页看板聚合的接口层测试（P3 收尾）。

看板最容易出的错**不是"数字算错"，而是"把两件不同的事并成一个数字"**：
没采过 vs 采失败、没查过合规 vs 查了没问题。这两条各有用例盯着，
因为一旦并了，运维就再也想不起来还有设备没纳管。
"""

from __future__ import annotations

import asyncio
import time

from tests.support.env import HUAWEI_CONFIG, HUAWEI_VERSION, Env, fixture_text
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}
LINES = ["interface GigabitEthernet0/0/1", "description uplink-to-core"]


async def make_device(env: Env, headers: dict, *, host: str, name: str) -> int:
    credential = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": f"cred-{name}", "username": "netadmin", "password": "pw-123456"},
    )
    device = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": name,
            "host": host,
            "vendor": "huawei",
            "credential_profile_id": credential.json()["id"],
        },
    )
    assert device.status_code == 200, device.text
    env.factory.register(
        host,
        make_fake_device(
            vendor="huawei",
            hostname=f"OV-{name}",
            config_text=fixture_text(HUAWEI_CONFIG),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return device.json()["id"]


async def run_backup(env: Env, headers: dict, device_id: int) -> dict:
    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "看板聚合", "precheck": False},
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    deadline = time.monotonic() + 30
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail["status"] in TERMINAL:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未结束：{detail}")


async def fetch(env: Env, headers: dict) -> dict:
    response = await env.client.get("/api/v1/meta/overview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_overview_on_empty_install_has_the_full_shape(env: Env):
    """空库也要给得出完整结构：前端不必为「还没有数据」写一堆分支。"""
    headers = await env.auth()
    body = await fetch(env, headers)

    assert body["devices"] == {"total": 0, "enabled": 0, "by_vendor": {}}
    assert body["versions"] == {"total": 0, "changed_last_7d": 0}
    assert body["runs"]["last_24h"] == {}
    assert body["runs"]["failed_last_24h"] == 0
    # ★ 「还没有数据」不等于「成功率 0%」——后者会让看板一上来就是红的
    assert body["runs"]["success_rate_7d"] is None
    assert body["compliance"] == {"unknown": 0}
    assert body["changes"] == {}
    assert body["recent_runs"] == []
    assert body["storage"] == {"objects": 0, "object_bytes": 0}
    assert body["generated_at"]


async def test_overview_counts_move_and_keep_unknown_separate(env: Env):
    """采一台设备之后：设备数/版本数/最近采集都要动，且「没查过合规」单独报。"""
    headers = await env.auth()
    # 一台只登记、从不采集的设备 → 它应当是"合规未知"的那一台
    await make_device(env, headers, host="10.51.0.1", name="ov-never")
    device_id = await make_device(env, headers, host="10.51.0.2", name="ov-backed")
    await run_backup(env, headers, device_id)

    # 顺手造一张变更单：P4 的状态也要进看板
    change = await env.client.post(
        "/api/v1/changes",
        headers=headers,
        json={"device_id": device_id, "lines": LINES},
    )
    assert change.status_code == 200, change.text

    body = await fetch(env, headers)
    assert body["devices"]["total"] == 2
    assert body["devices"]["by_vendor"] == {"huawei": 2}
    assert body["versions"]["total"] == 1
    assert body["runs"]["last_24h"].get("success") == 1
    assert body["runs"]["failed_last_24h"] == 0
    assert body["runs"]["success_rate_7d"] == 1.0
    assert body["runs"]["total_last_7d"] == 1
    assert body["recent_runs"][0]["device_name"] == "ov-backed"
    assert body["recent_runs"][0]["status"] == "success"
    assert body["changes"] == {"draft": 1}

    # ★ 合规：所有数字必须刚好覆盖全部设备，而"没查过"要单独占一格
    assert sum(body["compliance"].values()) == 2, body["compliance"]
    assert body["compliance"]["unknown"] >= 1, body["compliance"]
