"""审计日志查询与导出（P3）。

P3 的验收标准就是这一条：**"谁在何时下载了哪台设备的配置"必须可追溯**。所以这里的
测试不是在测"接口能返回"，而是在测"问得出答案"：下载动作发生之后，
``/audit-logs/downloads`` 里必须同时有**人、时间、设备**三要素。

另外两条同样是设计不变量，不是细节：
* 读审计要 auditor 起步（日志里写着谁看过哪台设备的明文，比配置本身更敏感）；
* 导出本身也写审计（否则审计能力可以被用来规避审计）。
"""

from __future__ import annotations

from app.services.audit import ACTIONS
from tests.support.env import VIEWER_PASSWORD, Env, fixture_text
from tests.support.env import HUAWEI_CONFIG, HUAWEI_VERSION
from tests.support.fake_device import make_fake_device

AUDITOR_PASSWORD = "auditor-12345"


async def make_device(env: Env, headers: dict, *, host: str = "10.11.0.1") -> int:
    credential = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={"name": "audit-cred", "username": "netadmin", "password": "pw-123456"},
    )
    device = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "audit-dev",
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
            hostname="AUDIT-SW",
            config_text=fixture_text(HUAWEI_CONFIG),
            version_text=fixture_text(HUAWEI_VERSION),
        ),
    )
    return device.json()["id"]


async def run_backup(env: Env, headers: dict, device_id: int) -> int:
    """跑一次备份并返回快照 id。"""
    import asyncio
    import time

    response = await env.client.post(
        "/api/v1/backups",
        headers=headers,
        json={"device_ids": [device_id], "name": "审计用备份", "precheck": False},
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = (await env.client.get(f"/api/v1/backups/{task_id}", headers=headers)).json()
        if detail["status"] in {"success", "partial", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.05)
    assert detail["status"] == "success", detail
    runs = detail["runs"]
    assert runs and runs[0]["snapshot_id"], detail
    return int(runs[0]["snapshot_id"])


async def make_auditor(env: Env) -> dict:
    created = await env.client.post(
        "/api/v1/users",
        headers=await env.auth(),
        json={"username": "auditor", "password": AUDITOR_PASSWORD, "role": "auditor"},
    )
    assert created.status_code == 200, created.text
    return await env.auth("auditor", AUDITOR_PASSWORD)


async def test_download_records_answer_who_took_which_config(env: Env):
    """★ 等保那一问：下载之后必须能查到 谁 / 何时 / 哪台设备。"""
    headers = await env.auth()
    device_id = await make_device(env, headers)
    snapshot_id = await run_backup(env, headers, device_id)

    # ① 下载配置文件（落地成文件的那种）
    download = await env.client.get(
        f"/api/v1/snapshots/{snapshot_id}/content?format=redacted&download=1", headers=headers
    )
    assert download.status_code == 200, download.text
    # ② 在界面上查看明文（更敏感：不落地也能看到口令）
    raw = await env.client.get(
        f"/api/v1/snapshots/{snapshot_id}/content?format=raw", headers=headers
    )
    assert raw.status_code == 200, raw.text

    auditor = await make_auditor(env)
    page = (await env.client.get("/api/v1/audit-logs/downloads", headers=auditor)).json()
    assert page["total"] == 2, page

    actions = {item["action"] for item in page["items"]}
    assert actions == {"snapshot.download", "snapshot.view_plaintext"}
    for item in page["items"]:
        assert item["username"] == "admin", "必须记下是谁"
        assert item["created_at"].endswith("Z"), "时间必须带时区标记"
        assert item["device"] == "audit-dev", "必须记下是哪台设备"
        assert item["snapshot_id"] == str(snapshot_id)
        assert item["action_label"], "要有中文说明，否则报表没法给人看"


async def test_reading_audit_logs_needs_auditor_role(env: Env):
    """读审计不能是"登录就行"：viewer 必须被挡在外面。

    ⚠ 注意角色是**包含式**的（admin > operator > auditor > viewer），所以
    **operator 天然拥有 auditor 的全部权限**——这不是漏判，而是 P1 定下的角色模型。
    真正被挡住的只有 viewer（以及未登录）。若将来要把运维挡在审计之外，
    得引入独立权限点，而不是靠"auditor"这个层级。
    """
    headers = await env.auth()
    await env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "reader", "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    operator = await env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "ops", "password": "operator-12345", "role": "operator"},
    )
    assert operator.status_code == 200, operator.text

    for url in (
        "/api/v1/audit-logs",
        "/api/v1/audit-logs/downloads",
        "/api/v1/audit-logs/actions",
        "/api/v1/audit-logs/export",
    ):
        assert (await env.client.get(url, headers=await env.auth("reader", VIEWER_PASSWORD))).status_code == 403, f"viewer 不该能读 {url}"

    # operator 在包含式角色下是允许的（见 docstring），这里明确断言下来，
    # 免得以后有人"顺手收紧"却不知道会打断运维的排障路径
    ops_token = await env.auth("ops", "operator-12345")
    assert (await env.client.get("/api/v1/audit-logs", headers=ops_token)).status_code == 200

    auditor = await make_auditor(env)
    assert (await env.client.get("/api/v1/audit-logs", headers=auditor)).status_code == 200


async def test_audit_filters_and_actions_dictionary(env: Env):
    headers = await env.auth()
    device_id = await make_device(env, headers)
    await run_backup(env, headers, device_id)
    auditor = await make_auditor(env)

    actions = (await env.client.get("/api/v1/audit-logs/actions", headers=auditor)).json()
    values = {item["value"] for item in actions}
    assert "backup.start" in values and "snapshot.download" in values
    assert values == set(ACTIONS), "字典要和后端注册表完全一致（前端下拉依赖它）"
    assert all(item["label"] for item in actions), "每个动作都要有中文说明"

    filtered = (
        await env.client.get("/api/v1/audit-logs?action=backup.start", headers=auditor)
    ).json()
    assert filtered["total"] >= 1
    assert {item["action"] for item in filtered["items"]} == {"backup.start"}
    assert filtered["items"][0]["detail"]["scope"]["device_ids"] == [device_id]

    # 按用户过滤：admin 的动作都在，不存在的用户返回空
    by_user = (
        await env.client.get("/api/v1/audit-logs?username=admin", headers=auditor)
    ).json()
    assert by_user["total"] >= 1
    unknown = (
        await env.client.get("/api/v1/audit-logs?username=nobody", headers=auditor)
    ).json()
    assert unknown["total"] == 0


async def test_audit_export_writes_its_own_audit_row(env: Env):
    """★ 导出本身就是敏感动作：必须留痕，否则审计能力可以被用来规避审计。"""
    headers = await env.auth()
    device_id = await make_device(env, headers)
    await run_backup(env, headers, device_id)
    auditor = await make_auditor(env)

    exported = await env.client.get("/api/v1/audit-logs/export?fmt=csv", headers=auditor)
    assert exported.status_code == 200, exported.text
    # ★ Excel 直接双击打开时，不带 BOM 的中文是乱码
    assert exported.content.startswith(b"\xef\xbb\xbf"), "CSV 必须带 UTF-8 BOM"
    text = exported.content.decode("utf-8-sig")
    assert "时间,用户,动作" in text.splitlines()[0]
    assert "backup.start" in text

    # 导出动作自己也进了审计
    again = (
        await env.client.get("/api/v1/audit-logs?action=audit.export", headers=auditor)
    ).json()
    assert again["total"] == 1, again
    assert again["items"][0]["username"] == "auditor"
    assert again["items"][0]["detail"]["format"] == "csv"


async def test_audit_export_json_round_trip(env: Env):
    headers = await env.auth()
    auditor = await make_auditor(env)
    response = await env.client.get("/api/v1/audit-logs/export?fmt=json", headers=auditor)
    assert response.status_code == 200, response.text
    import json

    rows = json.loads(response.content.decode("utf-8"))
    assert isinstance(rows, list) and rows, rows
    assert {"created_at", "username", "action", "detail"} <= set(rows[0])
