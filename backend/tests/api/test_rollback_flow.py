"""一键回滚的接口层测试（P4-C）。

回滚内容怎么算，由 ``tests/unit/test_rollback_lines.py`` 覆盖。这里验的是"接进系统
之后还对不对"：**目标版本选得对不对、跳过的版本有没有说清楚、预演有没有真的只读**、
生成的变更单能不能照常走审批，以及最常见的那种变更（只多了一行）必须如实告诉人
"这次要你手工写删除命令"，而不是造一张空单子。
"""

from __future__ import annotations

import asyncio
import time

from tests.support.env import HUAWEI_CONFIG, HUAWEI_VERSION, Env, fixture_text
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}
ADDED_LINE = "ip route-static 10.99.0.0 255.255.255.0 10.10.10.254"


def _drop_block(text: str, prefix: str) -> str:
    """删掉一个块（从 ``prefix`` 开头那行到下一个分隔行），模拟"配置被别人删了"。"""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            skipping = True
            continue
        if skipping:
            if stripped in ("#", "!"):
                skipping = False
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _add_line(text: str) -> str:
    """在终止符之前插一行（追加到末尾会被 must_end_with 判成 bad_terminator）。"""
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() in ("return", "end"):
            lines.insert(index, ADDED_LINE)
            break
    return "\n".join(lines) + "\n"


async def make_device(env: Env, headers: dict, *, host: str, name: str, config: str):
    """建一台设备，返回 ``(device_id, 假设备)``——假设备留着用来换配置。"""
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
    fake = make_fake_device(
        vendor="huawei",
        hostname=f"RB-{name}",
        config_text=config,
        version_text=fixture_text(HUAWEI_VERSION),
    )
    env.factory.register(host, fake)
    return device.json()["id"], fake


async def run_backup(env: Env, headers: dict, device_id: int) -> dict:
    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "回滚测试", "precheck": False},
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


async def snapshots(env: Env, headers: dict, device_id: int) -> list[dict]:
    response = await env.client.get(
        f"/api/v1/devices/{device_id}/snapshots", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def preview(env: Env, headers: dict, device_id: int) -> dict:
    response = await env.client.get(
        f"/api/v1/devices/{device_id}/rollback/preview", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# 有可自动恢复的内容
# --------------------------------------------------------------------------
async def test_preview_picks_previous_usable_version_and_explains_skips(env: Env):
    """当前版本被删了配置 → 目标应当是**上一个**可用版本，且要说清为什么跳过最新版。"""
    headers = await env.auth()
    original = fixture_text(HUAWEI_CONFIG)
    device_id, fake = await make_device(
        env, headers, host="10.41.0.1", name="rb-preview", config=original
    )
    await run_backup(env, headers, device_id)
    good = (await snapshots(env, headers, device_id))[0]["id"]

    fake.with_config(_drop_block(original, "interface "))
    await run_backup(env, headers, device_id)
    current = (await snapshots(env, headers, device_id))[0]["id"]
    assert current != good, "换了配置却没产生新版本"

    body = await preview(env, headers, device_id)
    assert body["ok"] is True, body
    assert body["current_snapshot_id"] == current
    assert body["target"]["snapshot_id"] == good
    # ★ 跳过的版本必须列出来：否则运维会以为工具漏了最新那一版
    assert {item["snapshot_id"] for item in body["skipped"]} == {current}
    assert "当前版本" in body["skipped"][0]["reason"]
    # 被删掉的那一块要能整块补回来（含视图入口行）
    restore = body["rollback"]["restore"]
    assert restore, body["rollback"]
    assert restore[0].startswith("interface "), restore
    assert body["rollback"]["truncated"] is False
    # 顺带给版本对比，界面要能直接展示
    assert body["diff"]["from_snapshot_id"] == current
    assert body["diff"]["to_snapshot_id"] == good
    assert body["diff"]["identical"] is False


async def test_preview_is_read_only(env: Env):
    """预演只读：跑完之后不该多出任何变更单。"""
    headers = await env.auth()
    original = fixture_text(HUAWEI_CONFIG)
    device_id, fake = await make_device(
        env, headers, host="10.41.0.2", name="rb-readonly", config=original
    )
    await run_backup(env, headers, device_id)
    fake.with_config(_drop_block(original, "interface "))
    await run_backup(env, headers, device_id)

    await preview(env, headers, device_id)
    listing = (await env.client.get("/api/v1/changes", headers=headers)).json()
    assert listing["total"] == 0, listing


async def test_rollback_draft_still_has_to_pass_the_gates(env: Env):
    """★ 回滚不搞"紧急通道"：生成的是一张普通草稿单，照样要审批、照样只能预演。

    这里特意把夹具里的 `shutdown` 那一行去掉：它是**完全正常的配置行**（接口被管理性
    关闭），但下发的危险命令闸门会（正确地）拒绝它。所以这条用例验的是**审批闸门**，
    危险命令拦截由下一条用例专门验——两件事不要混在一条断言里。
    """
    headers = await env.auth()
    original = fixture_text(HUAWEI_CONFIG).replace(" shutdown\n", "\n")
    device_id, fake = await make_device(
        env, headers, host="10.41.0.3", name="rb-gate", config=original
    )
    await run_backup(env, headers, device_id)
    good = (await snapshots(env, headers, device_id))[0]["id"]
    fake.with_config(_drop_block(original, "interface "))
    await run_backup(env, headers, device_id)

    response = await env.client.post(
        f"/api/v1/devices/{device_id}/rollback", headers=headers, json={"note": "恢复接口配置"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    change = body["change"]
    assert change["kind"] == "rollback"
    assert change["state"] == "draft"
    assert change["target_snapshot_id"] == good
    restore = body["preview"]["rollback"]["restore"]
    assert restore, "这一档应当能自动算出要补的内容"
    assert change["line_count"] == len(restore)

    change_id = change["id"]
    submitted = await env.client.post(
        f"/api/v1/changes/{change_id}/submit", headers=headers, json={}
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "pending"

    # 批准之前不许发：回滚同样要过审批闸门
    blocked = await env.client.post(
        f"/api/v1/changes/{change_id}/preview", headers=headers
    )
    assert blocked.status_code == 200, blocked.text
    codes = {item["code"] for item in blocked.json()["blockers"]}
    assert "not_approved" in codes, codes


# --------------------------------------------------------------------------
# 没有可自动恢复的内容（真实世界最常见的情形）
# --------------------------------------------------------------------------
async def test_added_line_needs_manual_undo_and_is_explained(env: Env):
    """★ 只多了一行时，"一键回滚"必须说清"这次要你手写删除命令"，而不是造空单子。"""
    headers = await env.auth()
    original = fixture_text(HUAWEI_CONFIG)
    device_id, fake = await make_device(
        env, headers, host="10.41.1.1", name="rb-added", config=original
    )
    await run_backup(env, headers, device_id)
    fake.with_config(_add_line(original))
    await run_backup(env, headers, device_id)

    body = await preview(env, headers, device_id)
    assert body["ok"] is True, body
    assert body["rollback"]["restore"] == [], body["rollback"]
    assert ADDED_LINE in body["rollback"]["needs_manual"], body["rollback"]

    refused = await env.client.post(
        f"/api/v1/devices/{device_id}/rollback", headers=headers, json={}
    )
    assert refused.status_code == 422, refused.text
    extra = refused.json()["extra"]
    assert ADDED_LINE in extra["needs_manual"], extra
    assert extra["needs_manual_total"] >= 1


# --------------------------------------------------------------------------
# 没有可用的历史版本
# --------------------------------------------------------------------------
async def test_rollback_without_a_usable_history_is_refused(env: Env):
    """只有一版（就是当前版本）时，没有"上一个"可回滚——要给出可读的原因。"""
    headers = await env.auth()
    device_id, _ = await make_device(
        env, headers, host="10.41.2.1", name="rb-single", config=fixture_text(HUAWEI_CONFIG)
    )
    await run_backup(env, headers, device_id)

    body = await preview(env, headers, device_id)
    assert body["ok"] is False
    assert body["target"] is None
    assert "没有可用的历史版本" in body["reason"], body

    refused = await env.client.post(
        f"/api/v1/devices/{device_id}/rollback", headers=headers, json={}
    )
    assert refused.status_code == 409, refused.text


async def test_rollback_content_is_not_trusted_either(env: Env):
    """★ 从历史版本里读出来的行**同样要过危险命令闸门**。

    这个用例来自一次真实的测试失败：夹具里有一个 `shutdown`（接口被管理性关闭，
    是正常的配置行），回滚要把它所在的块整块补回去，于是闸门拒绝了——
    **这是对的**：重新下发 `shutdown` 可能把一条链路关掉，值得人看一眼。
    所以接口如实返回 422 并点名问题，而不是"因为内容来自历史版本就放行"。

    换句话说：**历史版本不是可信来源，只是候选来源**。谁能发出去，只由闸门决定。
    """
    headers = await env.auth()
    original = fixture_text(HUAWEI_CONFIG)
    device_id, fake = await make_device(
        env, headers, host="10.41.2.2", name="rb-danger", config=original
    )
    await run_backup(env, headers, device_id)
    fake.with_config(_drop_block(original, "interface "))
    await run_backup(env, headers, device_id)

    body = await preview(env, headers, device_id)
    assert body["ok"] is True, body
    assert body["rollback"]["restore"], body["rollback"]

    refused = await env.client.post(
        f"/api/v1/devices/{device_id}/rollback", headers=headers, json={}
    )
    assert refused.status_code == 422, refused.text
    extra = refused.json()["extra"]
    codes = {item["code"] for item in extra}
    assert "dangerous_command" in codes, extra
    assert any("shutdown" in item["message"] for item in extra), extra
