"""变更单与下发链路的接口层测试（P4-B）。

纯逻辑（状态迁移表、下发/审批闸门）已由 ``tests/unit/test_change_flow.py`` 穷举。
这里只验"接进系统之后还对不对"：权限、状态真的落到库里、审计真的写了、
预演真的没有改状态，以及**生产上最危险的那几条路径**——审批之后内容被改、
没有基线、基线太旧时，预演必须明确说"不许发"。

★ ``deploy_enabled`` 默认是 false（§8.5.1 第 1 条），所以需要的用例会**显式**打开它：
``env.settings.deploy_enabled = True``。默认关闭本身也有一条用例盯着。
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
)
from tests.support.fake_device import make_fake_device

TERMINAL = {"success", "partial", "failed", "cancelled"}
OPERATOR_PASSWORD = "operator-12345"
LINES = ["interface GigabitEthernet0/0/1", "description uplink-to-core"]


# --------------------------------------------------------------------------
# 夹具小工具
# --------------------------------------------------------------------------
async def make_device(env: Env, headers: dict, *, host: str, name: str) -> int:
    credential = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": f"cred-{name}", "username": "netadmin", "password": "pw-123456"},
    )
    assert credential.status_code == 200, credential.text
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
            hostname=f"CHG-{name}",
            config_text=fixture_text(HUAWEI_CONFIG),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return device.json()["id"]


async def run_backup(env: Env, headers: dict, device_id: int) -> dict:
    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "变更前基线", "precheck": False},
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


async def newest_snapshot(env: Env, headers: dict, device_id: int) -> int:
    response = await env.client.get(
        f"/api/v1/devices/{device_id}/snapshots", headers=headers
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "这次采集没有产生任何版本"
    return int(items[0]["id"])


async def make_operator(env: Env, headers: dict, username: str = "approver") -> dict:
    """建第二个操作员（审批必须由申请人以外的人做）。"""
    response = await env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": username, "password": OPERATOR_PASSWORD, "role": "operator"},
    )
    assert response.status_code == 200, response.text
    return await env.auth(username, OPERATOR_PASSWORD)


async def create_change(env: Env, headers: dict, device_id: int, lines=None) -> dict:
    response = await env.client.post(
        "/api/v1/changes",
        headers=headers,
        json={"device_id": device_id, "lines": lines or LINES, "note": "对端扩容"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def advance(env: Env, url: str, headers: dict, payload: dict | None = None) -> dict:
    response = await env.client.post(url, headers=headers, json=payload or {})
    assert response.status_code == 200, response.text
    return response.json()


async def blocked_codes(env: Env, headers: dict, change_id: int) -> set[str]:
    response = await env.client.post(
        f"/api/v1/changes/{change_id}/preview", headers=headers
    )
    assert response.status_code == 200, response.text
    return {item["code"] for item in response.json()["blockers"]}


# --------------------------------------------------------------------------
# 正常链路
# --------------------------------------------------------------------------
async def test_full_chain_create_submit_approve_baseline_preview(env: Env):
    """建单 → 提交 → 第二人批准 → 登记基线 → 预演通过，且**预演不改状态**。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.0.1", name="chg-full")
    change = await create_change(env, headers, device_id)
    change_id = change["id"]

    assert change["state"] == "draft"
    assert change["state_label"] == "草稿"
    assert change["line_count"] == len(LINES)
    assert change["content_hash"], "没有内容哈希就没法把审批绑到具体内容上"

    submitted = await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    assert submitted["state"] == "pending"

    # 申请人自己批准 → 拒绝（必须第二人）
    own = await env.client.post(
        f"/api/v1/changes/{change_id}/decision", headers=headers, json={"approve": True}
    )
    assert own.status_code == 409, own.text
    assert "self_approval" in {item["code"] for item in own.json()["extra"]}, own.text

    approver = await make_operator(env, headers)
    approved = await advance(
        env, f"/api/v1/changes/{change_id}/decision", approver, {"approve": True, "note": "同意"}
    )
    assert approved["state"] == "approved"
    assert approved["approved_by"] is not None
    assert approved["approved_at"], "审批时间必须落库（事后追责要看它）"

    # 先备份后下发：基线在**提交之后**采
    await run_backup(env, headers, device_id)
    snapshot_id = await newest_snapshot(env, headers, device_id)
    with_baseline = await advance(
        env, f"/api/v1/changes/{change_id}/baseline", headers, {"snapshot_id": snapshot_id}
    )
    assert with_baseline["before_snapshot_id"] == snapshot_id
    assert with_baseline["before_captured_at"], "基线时间要单独存一份"

    env.settings.deploy_enabled = True
    preview = await env.client.post(
        f"/api/v1/changes/{change_id}/preview", headers=headers
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["dry_run"] is True
    assert body["can_deploy"] is True, body["blockers"]
    assert body["blockers"] == []
    assert body["plan"]["lines"] == len(LINES)

    # ★ 预演一行都没发出去，所以**不能**把变更单改成"已生效"
    detail = (await env.client.get(f"/api/v1/changes/{change_id}", headers=headers)).json()
    assert detail["state"] == "approved", "预演不得改变更单状态"
    assert detail["runs"], "预演也要留记录"
    latest = detail["runs"][0]
    assert latest["dry_run"] is True
    assert latest["status"] == "dry_run"
    assert latest["applied_lines"] == 0
    assert latest["results"] == [], "一行都没发，不该编出逐行结果"

    # 审计链完整
    logs = (
        await env.client.get("/api/v1/audit-logs?limit=200", headers=headers)
    ).json()["items"]
    actions = {item["action"] for item in logs}
    for expected in ("deploy.create", "deploy.submit", "deploy.approve", "deploy.baseline", "deploy.dry_run"):
        assert expected in actions, f"缺少审计动作 {expected}：{sorted(actions)}"


async def test_dangerous_lines_are_refused_at_creation(env: Env):
    """危险命令**进不了系统**，而不是"先存下来、提交时再拦"。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.0.2", name="chg-danger")
    response = await env.client.post(
        "/api/v1/changes",
        headers=headers,
        json={"device_id": device_id, "lines": ["save", "interface GigabitEthernet0/0/1"]},
    )
    assert response.status_code == 422, response.text
    codes = {item["code"] for item in response.json()["extra"]}
    assert "dangerous_command" in codes, response.text


async def test_multiline_string_is_accepted(env: Env):
    """从前端 textarea 直接粘一段多行文本，应当与传数组等价。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.0.3", name="chg-multiline")
    change = await create_change(
        env, headers, device_id, lines="\n".join(LINES) + "\n"
    )
    assert change["line_count"] == len(LINES)
    assert change["content_hash"]


async def test_self_approval_allowed_only_when_switch_is_on(env: Env):
    """单人值守环境需要一条明路，但默认必须是关的。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.0.4", name="chg-solo")
    change_id = (await create_change(env, headers, device_id))["id"]
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)

    env.settings.deploy_allow_self_approval = True
    approved = await advance(
        env, f"/api/v1/changes/{change_id}/decision", headers, {"approve": True}
    )
    assert approved["state"] == "approved"
    assert approved["approved_by"] == approved["requested_by"]


# --------------------------------------------------------------------------
# 下发闸门：这几条是 P4 真正要拦住的东西
# --------------------------------------------------------------------------
async def test_preview_is_blocked_without_baseline(env: Env):
    """没有下发前的采集 → 不许下发（否则回滚没有依据）。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.1.1", name="chg-nobase")
    change_id = (await create_change(env, headers, device_id))["id"]
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    approver = await make_operator(env, headers, "approver-b")
    await advance(
        env, f"/api/v1/changes/{change_id}/decision", approver, {"approve": True}
    )

    env.settings.deploy_enabled = True
    codes = await blocked_codes(env, headers, change_id)
    assert "no_baseline" in codes, codes


async def test_baseline_older_than_the_change_is_refused(env: Env):
    """★ 拿变更单创建**之前**的采集当基线，回滚会滚到错误的版本上。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.1.2", name="chg-stale")
    await run_backup(env, headers, device_id)          # 先采集
    old_snapshot = await newest_snapshot(env, headers, device_id)

    change_id = (await create_change(env, headers, device_id))["id"]   # 后建单
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    approver = await make_operator(env, headers, "approver-c")
    await advance(
        env, f"/api/v1/changes/{change_id}/decision", approver, {"approve": True}
    )
    await advance(
        env, f"/api/v1/changes/{change_id}/baseline", headers, {"snapshot_id": old_snapshot}
    )

    env.settings.deploy_enabled = True
    codes = await blocked_codes(env, headers, change_id)
    assert "stale_baseline" in codes, codes


async def test_disabled_switch_blocks_everything_by_default(env: Env):
    """默认关闭：即使单子完美，也明确告诉界面"功能没开"。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.1.3", name="chg-off")
    change_id = (await create_change(env, headers, device_id))["id"]
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    approver = await make_operator(env, headers, "approver-d")
    await advance(
        env, f"/api/v1/changes/{change_id}/decision", approver, {"approve": True}
    )
    await run_backup(env, headers, device_id)
    snapshot_id = await newest_snapshot(env, headers, device_id)
    await advance(
        env, f"/api/v1/changes/{change_id}/baseline", headers, {"snapshot_id": snapshot_id}
    )

    codes = await blocked_codes(env, headers, change_id)
    assert "deploy_disabled" in codes, codes

    status = (await env.client.get("/api/v1/deploy/status", headers=headers)).json()
    assert status["ok"] is False
    assert "未开启" in status["message"]


async def test_editing_approved_change_invalidates_the_approval(env: Env):
    """★ 批的是 A、发的是 B：改一个字符，原审批立刻作废。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.2.1", name="chg-edit")
    change_id = (await create_change(env, headers, device_id))["id"]
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    approver = await make_operator(env, headers, "approver-e")
    await advance(
        env, f"/api/v1/changes/{change_id}/decision", approver, {"approve": True}
    )

    edited = await env.client.patch(
        f"/api/v1/changes/{change_id}/lines",
        headers=headers,
        json={"lines": [*LINES, "mtu 9000"]},
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["state"] == "expired", "改了内容还留着原审批 = 审批形同虚设"
    assert body["approved_by"] is None
    assert body["line_count"] == len(LINES) + 1

    env.settings.deploy_enabled = True
    codes = await blocked_codes(env, headers, change_id)
    assert "not_approved" in codes, codes


async def test_reject_requires_a_reason(env: Env):
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.2.2", name="chg-reject")
    change_id = (await create_change(env, headers, device_id))["id"]
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    approver = await make_operator(env, headers, "approver-f")

    without = await env.client.post(
        f"/api/v1/changes/{change_id}/decision", headers=approver, json={"approve": False}
    )
    assert without.status_code == 422, without.text

    rejected = await advance(
        env,
        f"/api/v1/changes/{change_id}/decision",
        approver,
        {"approve": False, "note": "本周冻结变更"},
    )
    assert rejected["state"] == "rejected"
    assert rejected["reason"] == "本周冻结变更"
    assert rejected["timeline"][-1]["action"] == "reject"


async def test_cancelled_change_cannot_be_resubmitted(env: Env):
    """驳回/撤回是终态：想改内容请新建一张，就地复活会抹掉"谁驳回了什么"。"""
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.2.3", name="chg-cancel")
    change_id = (await create_change(env, headers, device_id))["id"]
    await advance(env, f"/api/v1/changes/{change_id}/submit", headers)
    cancelled = await advance(
        env, f"/api/v1/changes/{change_id}/cancel", headers, {"note": "另有安排"}
    )
    assert cancelled["state"] == "cancelled"

    again = await env.client.post(
        f"/api/v1/changes/{change_id}/submit", headers=headers, json={}
    )
    assert again.status_code == 409, again.text


# --------------------------------------------------------------------------
# 权限
# --------------------------------------------------------------------------
async def test_viewer_can_read_but_not_change(env: Env):
    headers = await env.auth()
    device_id = await make_device(env, headers, host="10.31.3.1", name="chg-perm")
    change_id = (await create_change(env, headers, device_id))["id"]

    created = await env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "reader", "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text
    viewer = await env.auth("reader", VIEWER_PASSWORD)

    assert (await env.client.get("/api/v1/changes", headers=viewer)).status_code == 200
    assert (
        await env.client.get(f"/api/v1/changes/{change_id}", headers=viewer)
    ).status_code == 200
    write = await env.client.post(
        "/api/v1/changes", headers=viewer, json={"device_id": device_id, "lines": LINES}
    )
    assert write.status_code == 403, write.text
    assert (
        await env.client.post(
            f"/api/v1/changes/{change_id}/preview", headers=viewer
        )
    ).status_code == 403
