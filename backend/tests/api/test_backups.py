"""备份任务 → 落库 → 版本 → 对比 的端到端测试（用假设备，不碰网络）。"""

from __future__ import annotations

import asyncio
import time

from tests.support.env import (
    HUAWEI_CONFIG,
    HUAWEI_VERSION,
    VIEWER_PASSWORD,
    Env,
    fixture_text,
)
from tests.support.fake_device import FakeDevice, make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}


async def make_device(
    env: Env,
    headers: dict,
    *,
    name: str = "HW-CORE-01",
    host: str = "10.9.0.1",
    vendor: str = "huawei",
    hostname: str = "HW-CORE-01",
    config_fixture: str = HUAWEI_CONFIG,
    version_fixture: str = HUAWEI_VERSION,
) -> tuple[int, FakeDevice]:
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
            "vendor": vendor,
            "credential_profile_id": credential.json()["id"],
        },
    )
    assert created.status_code == 200, created.text
    fake = make_fake_device(
        vendor=vendor,
        hostname=hostname,
        config_text=fixture_text(config_fixture),
        version_text=fixture_text(version_fixture),
    )
    env.factory.register(host, fake)
    return created.json()["id"], fake


async def run_backup(env: Env, headers: dict, device_ids: list[int], *, timeout: float = 30.0) -> dict:
    created = await env.client.post(
        "/api/v1/backups", headers=headers, json={"device_ids": device_ids}
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail["status"] in TERMINAL:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束")


# --------------------------------------------------------------------------
async def test_backup_records_run_and_snapshot(env: Env):
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)

    detail = await run_backup(env, headers, [device_id])
    assert detail["status"] == "success", detail
    assert detail["total"] == 1 and detail["succeeded"] == 1 and detail["failed"] == 0
    assert len(detail["runs"]) == 1
    run = detail["runs"][0]
    assert run["status"] == "success"
    assert run["changed"] is None            # 首次采集
    assert run["snapshot_id"] is not None
    assert run["line_count"] > 10
    assert run["device_name"] == "HW-CORE-01"

    snapshots = (
        await env.client.get(f"/api/v1/devices/{device_id}/snapshots", headers=headers)
    ).json()
    assert snapshots["total"] == 1
    assert snapshots["items"][0]["facts"]["model"] == "S5731-H24T4XC"

    device = (await env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
    assert device["last_status"] == "success"
    assert device["last_backup_at"] is not None
    assert device["model"] == "S5731-H24T4XC"
    assert device["snapshot_count"] == 1


async def test_second_backup_is_marked_unchanged(env: Env):
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)

    await run_backup(env, headers, [device_id])
    second = await run_backup(env, headers, [device_id])

    assert second["status"] == "success"
    assert second["unchanged"] == 1
    assert second["runs"][0]["status"] == "unchanged"
    assert second["runs"][0]["changed"] is False
    # 内容没变 → 不新增版本（同一份内容只存一行）
    snapshots = (
        await env.client.get(f"/api/v1/devices/{device_id}/snapshots", headers=headers)
    ).json()
    assert snapshots["total"] == 1


async def test_changed_config_creates_version_and_diff(env: Env):
    headers = await env.auth()
    device_id, fake = await make_device(env, headers)
    await run_backup(env, headers, [device_id])

    # 模拟设备配置被改，故意做两类不同的变更，顺带把统计口径也测进去：
    #   ① 一行内容被替换（shutdown → description）→ 应记成"修改"
    #   ② 净新增两行（ACL）→ 应记成"新增"
    original = fixture_text(HUAWEI_CONFIG)
    changed = original.replace(
        "interface GigabitEthernet0/0/3\n shutdown",
        "interface GigabitEthernet0/0/3\n description changed-by-test",
    )
    assert changed != original, "夹具内容已变，测试的前置假设失效（需要更新替换目标）"
    changed = changed.replace(
        "\nreturn\n",
        "\nacl number 3999\n rule 5 permit source 10.0.0.0 0.0.0.255\nreturn\n",
    )
    assert "acl number 3999" in changed
    fake.responses["display current-configuration"] = changed

    detail = await run_backup(env, headers, [device_id])
    assert detail["status"] == "success"
    run = detail["runs"][0]
    assert run["changed"] is True
    assert run["status"] == "success"

    snapshots = (
        await env.client.get(f"/api/v1/devices/{device_id}/snapshots", headers=headers)
    ).json()
    assert snapshots["total"] == 2

    diff = (
        await env.client.get(f"/api/v1/devices/{device_id}/diff", headers=headers)
    ).json()
    assert diff["identical"] is False
    # 统计口径见 services/diffs.summarize_changes：1 对 1 替换算"修改"，
    # 净增/净减的行才算"新增/删除"
    assert diff["added"] >= 2
    assert diff["changed"] >= 1
    assert "changed-by-test" in diff["unified"]
    assert "- shutdown" in diff["unified"]
    labels = " ".join(diff["significant"])
    assert "接口" in labels, f"关掉的接口行应被识别为重要变更：{diff['significant']}"
    assert "ACL" in labels, f"新增的 ACL 行应被识别为重要变更：{diff['significant']}"


async def test_diff_returns_identical_when_nothing_changed(env: Env):
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)
    await run_backup(env, headers, [device_id])

    # 手工再造一个完全相同内容的版本：直接用同一版的两侧比较
    snapshots = (
        await env.client.get(f"/api/v1/devices/{device_id}/snapshots", headers=headers)
    ).json()
    snapshot_id = snapshots["items"][0]["id"]
    diff = (
        await env.client.get(
            f"/api/v1/devices/{device_id}/diff"
            f"?from_snapshot_id={snapshot_id}&to_snapshot_id={snapshot_id}",
            headers=headers,
        )
    ).json()
    assert diff["identical"] is True
    assert diff["unified"] == ""


async def test_diff_requires_two_versions(env: Env):
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)
    await run_backup(env, headers, [device_id])
    response = await env.client.get(f"/api/v1/devices/{device_id}/diff", headers=headers)
    assert response.status_code == 422
    assert "两个版本" in response.json()["detail"]


async def test_snapshot_content_is_redacted_by_default_raw_needs_auditor(env: Env):
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)
    await run_backup(env, headers, [device_id])
    snapshot_id = (
        await env.client.get(f"/api/v1/devices/{device_id}/snapshots", headers=headers)
    ).json()["items"][0]["id"]

    redacted = (
        await env.client.get(f"/api/v1/snapshots/{snapshot_id}/content", headers=headers)
    ).json()
    assert redacted["redacted"] is True
    assert "%^%#Kj8sLm2QpR7dXw%^%#" not in redacted["content"]
    assert "irreversible-cipher ******" in redacted["content"]

    raw = (
        await env.client.get(
            f"/api/v1/snapshots/{snapshot_id}/content?format=raw", headers=headers
        )
    ).json()
    assert raw["redacted"] is False
    assert "%^%#Kj8sLm2QpR7dXw%^%#" in raw["content"]

    # 只读用户看不到明文
    await env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "reader", "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    viewer = await env.auth("reader", VIEWER_PASSWORD)
    forbidden = await env.client.get(
        f"/api/v1/snapshots/{snapshot_id}/content?format=raw", headers=viewer
    )
    assert forbidden.status_code == 403
    # 但脱敏内容可以看
    assert (
        await env.client.get(
            f"/api/v1/snapshots/{snapshot_id}/content", headers=viewer
        )
    ).status_code == 200


async def test_viewing_plaintext_and_download_are_audited(env: Env):
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)
    await run_backup(env, headers, [device_id])
    snapshot_id = (
        await env.client.get(f"/api/v1/devices/{device_id}/snapshots", headers=headers)
    ).json()["items"][0]["id"]

    await env.client.get(f"/api/v1/snapshots/{snapshot_id}/content?format=raw", headers=headers)
    download = await env.client.get(
        f"/api/v1/snapshots/{snapshot_id}/content?download=1", headers=headers
    )
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]

    from sqlalchemy import select

    from app.db.models import AuditLog

    async with env.database.session() as session:
        actions = {row.action for row in (await session.execute(select(AuditLog))).scalars()}
    assert "snapshot.view_plaintext" in actions
    assert "snapshot.download" in actions


async def test_device_without_credential_fails_alone_without_killing_the_batch(env: Env):
    """★ 回归：一台设备没绑凭据，不能让整批任务失败。

    原实现里 ``build_targets()`` 对整批抛异常 → 50 台里 1 台没配好凭据，
    另外 49 台也一台都不采，任务直接 failed。这违背了"一台设备坏掉不能影响整体"。

    现在：缺凭据的设备各记一条失败记录（界面上能看到是哪台、为什么），
    其余设备照常采集。
    """
    headers = await env.auth()
    good_id, _ = await make_device(env, headers, name="good", host="10.9.8.1")
    # 只建设备、不绑凭据
    orphan = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={"name": "no-cred", "host": "10.9.8.2", "vendor": "huawei"},
    )
    orphan_id = orphan.json()["id"]

    detail = await run_backup(env, headers, [good_id, orphan_id])
    assert detail["status"] == "partial", detail
    assert detail["succeeded"] == 1 and detail["failed"] == 1

    by_device = {run["device_id"]: run for run in detail["runs"]}
    assert by_device[good_id]["status"] == "success"
    assert by_device[orphan_id]["status"] == "error"
    assert by_device[orphan_id]["error_code"] == "no_credential"
    assert "凭据" in by_device[orphan_id]["error_message"]


async def test_backup_where_every_device_lacks_credential_reports_failed(env: Env):
    """全部设备都没绑凭据 → 任务 failed，且每台都有可读的失败原因。"""
    headers = await env.auth()
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={"name": "no-cred", "host": "10.9.9.9", "vendor": "huawei"},
    )
    device_id = created.json()["id"]

    detail = await run_backup(env, headers, [device_id])
    assert detail["status"] == "failed", detail
    assert detail["failed"] == 1 and detail["succeeded"] == 0
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["error_code"] == "no_credential"
    assert "凭据" in detail["runs"][0]["error_message"]

    # 设备列表上也要能看到这条失败（否则运维只知道"任务失败"，不知道哪台）
    device = (await env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
    assert device["last_status"] == "error"
    assert "凭据" in (device["last_error"] or "")


async def test_batch_backup_reports_partial_failure(env: Env):
    headers = await env.auth()
    good_id, _ = await make_device(env, headers, name="good", host="10.9.1.1")
    bad_id, _ = await make_device(env, headers, name="bad", host="10.9.1.2")
    env.factory.register(
        "10.9.1.2", make_fake_device(vendor="huawei", hostname="BAD", config_text="", fail_with="auth_failed")
    )

    detail = await run_backup(env, headers, [good_id, bad_id])
    assert detail["status"] == "partial"
    assert detail["succeeded"] == 1 and detail["failed"] == 1
    statuses = {run["device_id"]: run["status"] for run in detail["runs"]}
    assert statuses[good_id] == "success"
    assert statuses[bad_id] == "auth_failed"

    # 失败设备的历史运行记录也能查到（失败也要留痕）
    runs = (await env.client.get(f"/api/v1/runs?device_id={bad_id}", headers=headers)).json()
    assert runs["total"] == 1
    assert runs["items"][0]["error_code"] == "auth_failed"


async def test_task_events_are_recorded_for_sse_replay(env: Env):
    """SSE 断线重连依赖事件历史，这里验证事件确实被记录下来。"""
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)
    detail = await run_backup(env, headers, [device_id])

    runner = env.runner
    events = runner.bus.history(detail["id"])
    types = [event["type"] for event in events]
    assert types[0] == "task_started"
    assert "device_finished" in types
    assert types[-1] == "task_finished"
    finished = events[-1]
    assert finished["data"]["status"] == "success"


async def test_sse_stream_delivers_events_until_task_finishes(env: Env):
    """SSE 端到端：这是前端的核心交互，也是全项目唯一手写的协议。

    `ASGITransport` 会把响应体收完再返回，所以这里的语义正好是
    "订阅一个正在运行的任务，直到它结束"——拿到的就是完整事件流。
    为让"任务还在跑"有一个可观测窗口，给假设备加了人为延时。
    """
    headers = await env.auth()
    device_id, fake = await make_device(env, headers)
    fake.delay = 0.15  # 5 条命令 ≈ 0.75s，足够让流请求在任务结束前到达

    created = await env.client.post(
        "/api/v1/backups", headers=headers, json={"device_ids": [device_id]}
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    response = await env.client.get(f"/api/v1/backups/{task_id}/stream", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("x-accel-buffering") == "no"  # 反代缓冲必须关掉

    # json.dumps 默认带空格，这里去掉空格再断言，避免被格式细节绊住
    body = response.text.replace(" ", "")
    assert '"type":"state"' in body
    assert '"type":"task_started"' in body
    assert '"type":"device_finished"' in body
    assert '"type":"task_finished"' in body
    # 前端靠这一帧判定"读完了"，缺了它 SSE 连接会一直挂着
    assert "event:done" in body
    assert body.rstrip().endswith("data:{}")

    # 任务确实已经结束（流是跟着任务结束而结束的）
    detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
    assert detail["status"] == "success"


async def test_stream_on_finished_task_returns_immediately(env: Env):
    """订阅一个已经结束的任务：只回状态帧 + 结束帧，不能挂住。"""
    headers = await env.auth()
    device_id, _ = await make_device(env, headers)
    detail = await run_backup(env, headers, [device_id])

    response = await env.client.get(f"/api/v1/backups/{detail['id']}/stream", headers=headers)
    assert response.status_code == 200
    body = response.text.replace(" ", "")
    assert '"type":"state"' in body
    assert "event:done" in body


async def test_cancel_running_task_ends_as_cancelled(env: Env):
    """取消：被中断的任务必须落到 cancelled，而不是永远停在 running。

    这条覆盖的是 `asyncio.Task.cancel()` → ``CancelledError`` → 独立 session
    收尾这条链——很容易写成"取消了但状态还是 running"。
    """
    headers = await env.auth()
    device_id, fake = await make_device(env, headers)
    # 延时给足，确保取消一定发生在任务跑完之前
    fake.delay = 0.5

    created = await env.client.post(
        "/api/v1/backups", headers=headers, json={"device_ids": [device_id]}
    )
    task_id = created.json()["id"]

    # 等它真的开始跑，避免"取消一个还没启动的任务"这种不确定场景
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail["status"] == "running":
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("任务未在 10s 内进入 running 状态")

    response = await env.client.post(f"/api/v1/backups/{task_id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail["status"] in TERMINAL:
            break
        await asyncio.sleep(0.05)
    assert detail["status"] == "cancelled", detail
    assert detail["finished_at"] is not None, "取消后必须写结束时间，否则界面上会一直显示运行中"

    # 再取消一次应当是"没在运行"，而不是报错
    again = await env.client.post(f"/api/v1/backups/{task_id}/cancel", headers=headers)
    assert again.status_code == 200
    assert again.json()["ok"] is False
