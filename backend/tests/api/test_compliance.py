"""合规基线的接口层测试（P3）。

这里测的是"接进系统之后还对不对"，纯逻辑由 ``tests/unit/test_compliance.py`` 覆盖：

* 报表要能回答"谁不合规、谁还没查过"（未评估的设备必须出现，不是被隐藏）；
* 立即检查会写审计；
* 导出 CSV 带 UTF-8 BOM（Excel）且**导出本身留痕**；
* 采集到不合规配置时，订阅了 ``compliance_failed`` 的渠道要真的收到消息
  （这条最容易只做一半：结果算了、事件没进 ``events``）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services import notifications
from tests.support.env import (
    HUAWEI_CONFIG,
    HUAWEI_VERSION,
    Env,
    fixture_text,
    with_config_change,
)
from tests.support.fake_device import make_fake_device

from app.db.models import BackupRun  # noqa: E402 - 诊断函数要用（见 diagnose）

#: 在原厂夹具里插一行 telnet：命中内置基线的 no-telnet-server（high）→ 判定 fail。
#: 插在 return 之前（追加到末尾会被 must_end_with 判成 bad_terminator）。
TELNET_CONFIG = fixture_text(HUAWEI_CONFIG).replace(
    "stelnet server enable", "stelnet server enable\ntelnet server enable"
)

TERMINAL = {"success", "partial", "failed", "cancelled"}


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
            hostname=f"COMP-{name}",
            config_text=config,
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return device.json()["id"]


async def diagnose(env: Env, task_id: int, device_id: int) -> dict:
    """失败时用：把"通知为什么没发出去"需要的原始事实一次取出来。

    前两轮失败时只有一句 ``total: 0``，无法区分是"合规结果没写进库""事件没判定出来"
    还是"发送侧没写日志"，于是白跑了两轮。诊断信息必须由测试自己带出来，
    不能指望人在失败的输出里找日志段落。
    """
    from sqlalchemy import select

    from app.db.models import BackupTask, ComplianceResult, NotificationLog

    async with env.database.session() as session:
        task = await session.get(BackupTask, task_id)
        results = list(
            (
                await session.execute(
                    select(ComplianceResult).where(ComplianceResult.device_id == device_id)
                )
            )
            .scalars()
            .all()
        )
        logs = list((await session.execute(select(NotificationLog))).scalars().all())
        runs = list(
            (
                await session.execute(
                    select(BackupRun).where(BackupRun.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
    return {
        "task": {
            "status": getattr(task, "status", None),
            "created_at": str(getattr(task, "created_at", None)),
            "started_at": str(getattr(task, "started_at", None)),
            "finished_at": str(getattr(task, "finished_at", None)),
        },
        "runs": [
            {
                "device_id": run.device_id,
                "status": run.status,
                "snapshot_id": run.snapshot_id,
                "started_at": str(run.started_at),
            }
            for run in runs
        ],
        "compliance": [
            {
                "id": row.id,
                "verdict": row.verdict,
                "snapshot_id": row.snapshot_id,
                "checked_at": str(row.checked_at),
                "high": row.high_count,
                "rules_version": row.rules_version,
            }
            for row in results
        ],
        "notification_logs": [
            {"channel_id": log.channel_id, "event": log.event, "status": log.status}
            for log in logs
        ],
    }


async def run_backup(env: Env, headers: dict, device_id: int) -> dict:
    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "合规检查用", "precheck": False},
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


async def test_report_includes_unchecked_devices_and_rule_columns(env: Env):
    """★ 报表必须包含"还没查过"的设备——运维要看的恰恰是它们。"""
    headers = await env.auth()
    checked = await make_device(
        env, headers, host="10.21.0.1", name="checked-dev", config=fixture_text(HUAWEI_CONFIG)
    )
    await run_backup(env, headers, checked)
    await make_device(
        env, headers, host="10.21.0.2", name="never-backed-up", config=fixture_text(HUAWEI_CONFIG)
    )

    report = (await env.client.get("/api/v1/compliance/report", headers=headers)).json()
    assert report["rules_version"], report
    assert len(report["rule_columns"]) >= 15, report["rule_columns"]
    assert {rule["severity"] for rule in report["rule_columns"]} <= {"high", "medium", "low"}

    by_name = {row["device_name"]: row for row in report["matrix"]}
    assert set(by_name) == {"checked-dev", "never-backed-up"}
    # 华为原厂夹具只缺空闲超时 → warn（不是 fail）
    assert by_name["checked-dev"]["verdict"] == "warn", by_name["checked-dev"]
    assert by_name["never-backed-up"]["verdict"] == "unknown"
    assert by_name["never-backed-up"]["cells"] == {}
    # 每个格子要是 pass / fail / na 之一，前端直接渲染
    assert set(by_name["checked-dev"]["cells"].values()) <= {"pass", "fail", "na"}
    assert report["summary"]["unknown"] == 1


async def test_run_now_rechecks_and_writes_audit(env: Env):
    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.21.1.1", name="comply-run", config=TELNET_CONFIG
    )
    await run_backup(env, headers, device_id)

    response = await env.client.post(
        "/api/v1/compliance/run", headers=headers, json={"device_ids": [device_id]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checked"] == 1 and body["failed"] == 0, body
    assert body["verdicts"] == {"fail": 1}, body

    auditor = await env.auth()  # admin 也是 auditor 及以上
    logs = (
        await env.client.get("/api/v1/audit-logs?action=compliance.run", headers=auditor)
    ).json()
    assert logs["total"] == 1, logs
    assert logs["items"][0]["detail"]["rules_version"] == body["rules_version"]


async def test_export_reports_csv_and_audits_the_export(env: Env):
    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.21.2.1", name="comply-export", config=TELNET_CONFIG
    )
    await run_backup(env, headers, device_id)

    exported = await env.client.get("/api/v1/compliance/export?fmt=csv", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.content.startswith(b"\xef\xbb\xbf"), "CSV 必须带 BOM，否则 Excel 中文乱码"
    text = exported.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("设备,厂商,站点,判定")
    assert "comply-export" in text

    logs = (
        await env.client.get("/api/v1/audit-logs?action=compliance.export", headers=headers)
    ).json()
    assert logs["total"] == 1, logs
    assert logs["items"][0]["detail"]["devices"] == 1, "导出动作要记下带走了多少行"


async def test_device_view_shows_latest_result_and_history(env: Env):
    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.21.3.1", name="comply-view", config=TELNET_CONFIG
    )
    await run_backup(env, headers, device_id)

    view = (await env.client.get(f"/api/v1/devices/{device_id}/compliance", headers=headers)).json()
    assert view["device_id"] == device_id
    latest = view["latest"]
    assert latest["verdict"] == "fail"
    assert {"no-telnet-server"} <= {item["rule_id"] for item in latest["violations"]}
    # 时间要带时区标记（P1 的时间契约：库存朴素 UTC，出网补 Z）
    assert latest["checked_at"].endswith("Z"), latest["checked_at"]
    assert view["history"], view


async def test_noncompliant_config_triggers_notification(env: Env, monkeypatch: pytest.MonkeyPatch):
    """★ 不合规要能推出去：订阅了 ``compliance_failed`` 的渠道必须真的收到。

    "算了结果但没产生事件"是这类功能最常见的半成品状态——界面上能订阅，
    实际永远不响。
    """
    sent: list[dict] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        sent.append(payload)
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.21.4.1", name="comply-notify", config=TELNET_CONFIG
    )
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "合规告警",
                "kind": "webhook",
                "config": {"url": "https://example.com/compliance"},
                "events": ["compliance_failed"],
            },
        )
    ).json()

    detail = await run_backup(env, headers, device_id)
    assert detail["status"] == "success", detail

    # 通知在任务终态之后派发，等一会
    deadline = time.monotonic() + 10
    page: dict = {"total": 0, "items": []}
    while time.monotonic() < deadline:
        page = (
            await env.client.get(
                f"/api/v1/notification-logs?channel_id={channel['id']}", headers=headers
            )
        ).json()
        if page["total"]:
            break
        await asyncio.sleep(0.1)

    assert page["total"] == 1, (
        f"不合规应当触发一次通知：{page}；诊断={await diagnose(env, detail['id'], device_id)}"
    )
    assert "compliance_failed" in page["items"][0]["event"]
    assert sent and "compliance_failed" in sent[-1]["event"]
    assert "comply-notify" in sent[-1]["text"], "正文要说清是哪台设备不合规"


async def test_compliance_failure_is_not_reported_twice(env: Env, monkeypatch: pytest.MonkeyPatch):
    """不合规只在**本次任务期间**被判定时才告警。

    否则一台设备上个月不合规，之后每次任务都会再报一遍——告警变噪音就没人看了。
    """
    calls: list[dict] = []

    async def fake_post(url: str, payload: dict, **_kwargs) -> tuple[bool, str]:
        calls.append(payload)
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    headers = await env.auth()
    device_id = await make_device(
        env, headers, host="10.21.5.1", name="comply-once", config=TELNET_CONFIG
    )
    channel = (
        await env.client.post(
            "/api/v1/channels",
            headers=headers,
            json={
                "name": "合规告警一次",
                "kind": "webhook",
                "config": {"url": "https://example.com/once"},
                "events": ["compliance_failed"],
            },
        )
    ).json()

    await run_backup(env, headers, device_id)  # 第一次：产生合规结果 → 告警
    second = await run_backup(env, headers, device_id)  # 第二次：内容没变 → 不重新评估 → 不告警

    await asyncio.sleep(0.3)
    page = (
        await env.client.get(
            f"/api/v1/notification-logs?channel_id={channel['id']}", headers=headers
        )
    ).json()
    assert page["total"] == 1, (
        f"同一份不合规配置不该告警两次：{page}；"
        f"诊断={await diagnose(env, second['id'], device_id)}"
    )
