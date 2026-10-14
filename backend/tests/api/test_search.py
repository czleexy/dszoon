"""跨设备配置检索的接口层测试（P3 收尾）。

这一组要盯的其实不是"能不能搜到"，而是三件容易做错的事：

* 搜到的是**最新版本**（而不是某个早被覆盖的历史版本）；
* 权限与留痕：这是全库明文检索，viewer 不行、每次都要进审计；
* 结果必须如实说明"扫了多少台、有没有被截断"，否则用了 ``limit`` 却不说，
  运维会以为"全网就这一台有"。
"""

from __future__ import annotations

import asyncio
import time

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
MARKER = "10.99.0.0"


async def make_device(env: Env, headers: dict, *, host: str, name: str, config: str) -> int:
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
            hostname=f"SE-{name}",
            config_text=config,
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return device.json()["id"]


async def run_backup(env: Env, headers: dict, device_id: int) -> dict:
    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "检索", "precheck": False},
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


async def find(env: Env, headers: dict, q: str, **params) -> dict:
    query = {"q": q, **params}
    response = await env.client.get("/api/v1/search/config", headers=headers, params=query)
    assert response.status_code == 200, response.text
    return response.json()


async def test_search_hits_the_devices_that_actually_have_it(env: Env):
    headers = await env.auth()
    plain = fixture_text(HUAWEI_CONFIG)
    changed = with_config_change(plain)
    only_one = await make_device(
        env, headers, host="10.61.0.1", name="se-changed", config=changed
    )
    await make_device(env, headers, host="10.61.0.2", name="se-plain", config=plain)
    await run_backup(env, headers, only_one)
    other = (await env.client.get("/api/v1/devices", headers=headers)).json()["items"]
    for item in other:
        if item["name"] == "se-plain":
            await run_backup(env, headers, item["id"])

    # 只有那一台有这行
    body = await find(env, headers, MARKER)
    assert body["matched_devices"] == 1, body
    assert {hit["device_name"] for hit in body["hits"]} == {"se-changed"}
    assert body["hits"][0]["line_no"] > 0
    assert MARKER in body["hits"][0]["line"], body["hits"][0]
    assert body["scanned_devices"] == 2, "两台设备都应被扫到"
    assert body["truncated"] is False

    # 两台都有的词：命中设备数应当是 2（用 interface 而不是 sysname：
    # 断言要压在"夹具里一定有"的内容上，否则测试会因为夹具换了一行就变红）
    both = await find(env, headers, "interface")
    assert both["matched_devices"] == 2, both


async def test_search_reports_truncation_instead_of_hiding_it(env: Env):
    """★ 用了 limit 就必须说：否则运维会以为"全网就这一台有"。"""
    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.61.1.1", name="se-trunc", config=fixture_text(HUAWEI_CONFIG)
    )
    await run_backup(env, headers, device_id)

    body = await find(env, headers, "interface", limit=1)
    assert len(body["hits"]) == 1, body
    assert body["truncated"] is True, "被 limit 截断了却不说，是最容易误导人的一种结果"
    assert body["limit"] == 1


async def test_search_requires_auditor_and_is_always_audited(env: Env):
    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.61.2.1", name="se-perm", config=fixture_text(HUAWEI_CONFIG)
    )
    await run_backup(env, headers, device_id)

    created = await env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "reader", "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    viewer = await env.auth("reader", VIEWER_PASSWORD)
    denied = await env.client.get(
        "/api/v1/search/config", headers=viewer, params={"q": "sysname"}
    )
    assert denied.status_code == 403, denied.text

    assert (await env.client.get("/api/v1/search/config", headers=headers)).status_code == 422

    # 管理员（auditor 及以上）能搜，而且这一搜必须留痕
    await find(env, headers, "sysname")
    logs = (
        await env.client.get("/api/v1/audit-logs?limit=100", headers=headers)
    ).json()["items"]
    entries = [item for item in logs if item["action"] == "search.config"]
    assert entries, "全库明文检索必须写审计"
    assert entries[0]["detail"]["query"] == "sysname", entries[0]
