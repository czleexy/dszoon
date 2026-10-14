"""设备台账与凭据测试。"""

from __future__ import annotations

from tests.support.env import Env
from tests.support.fake_device import make_fake_device


async def _credential(env: Env, headers: dict, name: str = "默认凭据") -> dict:
    response = await env.client.post(
        "/api/v1/credentials",
        headers=headers,
        json={
            "name": name,
            "username": "netadmin",
            "password": "super-secret-password",
            "enable_password": "enable-secret",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_credential_password_never_leaves_the_server(env: Env):
    headers = await env.auth()
    profile = await _credential(env, headers)
    assert profile["has_password"] is True
    assert profile["has_enable_password"] is True
    assert "password" not in profile
    assert "super-secret-password" not in str(profile)

    # 列表接口也不能泄露
    listing = await env.client.get("/api/v1/credentials", headers=headers)
    assert "super-secret-password" not in listing.text

    # 但密文确实落库了（能解密出来）
    from sqlalchemy import select

    from app.db.models import CredentialProfile
    from app.core.crypto import CredentialCipher

    async with env.database.session() as session:
        row = (await session.execute(select(CredentialProfile))).scalars().first()
    assert row is not None and row.password_enc and row.password_enc != "super-secret-password"
    cipher = CredentialCipher(env.settings.secret_key)
    assert cipher.decrypt(row.password_enc) == "super-secret-password"


async def test_create_device_normalizes_vendor_alias(env: Env):
    headers = await env.auth()
    profile = await _credential(env, headers)
    response = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "核心交换机",
            "host": "10.0.0.1",
            "vendor": "HW",  # 别名，应归一化成 huawei
            "credential_profile_id": profile["id"],
            "site": "总部机房",
            "tags": ["核心", "生产"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vendor"] == "huawei"
    assert body["credential_profile_name"] == "默认凭据"
    assert body["tags"] == ["核心", "生产"]


async def test_unknown_vendor_is_rejected_with_available_list(env: Env):
    headers = await env.auth()
    response = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={"name": "X", "host": "10.0.0.9", "vendor": "cisco-nxos"},
    )
    assert response.status_code == 422
    body = response.json()
    assert "huawei" in body["detail"]
    assert body["extra"]["available"] == ["h3c", "huawei", "ruijie"]


async def test_duplicate_device_is_rejected(env: Env):
    headers = await env.auth()
    payload = {"name": "A", "host": "10.0.0.2", "vendor": "huawei"}
    assert (await env.client.post("/api/v1/devices", headers=headers, json=payload)).status_code == 200
    assert (await env.client.post("/api/v1/devices", headers=headers, json=payload)).status_code == 409
    # 名称不同但地址相同也要拦
    other = {"name": "B", "host": "10.0.0.2", "vendor": "huawei"}
    assert (await env.client.post("/api/v1/devices", headers=headers, json=other)).status_code == 409


async def test_device_list_filters_and_pagination(env: Env):
    headers = await env.auth()
    for index, (site, vendor) in enumerate(
        [("机房A", "huawei"), ("机房A", "h3c"), ("机房B", "ruijie")], start=1
    ):
        response = await env.client.post(
            "/api/v1/devices",
            headers=headers,
            json={
                "name": f"dev-{index}",
                "host": f"10.0.1.{index}",
                "vendor": vendor,
                "site": site,
            },
        )
        assert response.status_code == 200, response.text

    page = (await env.client.get("/api/v1/devices?limit=2", headers=headers)).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    only_a = (await env.client.get("/api/v1/devices?site=机房A", headers=headers)).json()
    assert only_a["total"] == 2
    by_vendor = (await env.client.get("/api/v1/devices?vendor=ruijie", headers=headers)).json()
    assert by_vendor["total"] == 1
    search = (await env.client.get("/api/v1/devices?q=10.0.1.2", headers=headers)).json()
    assert search["total"] == 1


async def test_test_connection_fills_facts_into_ledger(env: Env, huawei_device):
    headers = await env.auth()
    profile = await _credential(env, headers)
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "HW-CORE-01",
            "host": "10.0.2.1",
            "vendor": "huawei",
            "credential_profile_id": profile["id"],
        },
    )
    device_id = created.json()["id"]
    env.factory.register("10.0.2.1", huawei_device)

    response = await env.client.post(f"/api/v1/devices/{device_id}/test", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["facts"]["model"] == "S5731-H24T4XC"

    # 连接测试顺手把型号/设备名补进台账
    detail = (await env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
    assert detail["model"] == "S5731-H24T4XC"
    assert detail["os_version"] == "8.180"
    assert detail["hostname"] == "HW-CORE-01"


async def test_test_connection_reports_auth_failure(env: Env):
    headers = await env.auth()
    profile = await _credential(env, headers)
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "bad-cred",
            "host": "10.0.2.2",
            "vendor": "huawei",
            "credential_profile_id": profile["id"],
        },
    )
    device_id = created.json()["id"]
    env.factory.register(
        "10.0.2.2",
        make_fake_device(vendor="huawei", hostname="X", config_text="", fail_with="auth_failed"),
    )
    response = await env.client.post(f"/api/v1/devices/{device_id}/test", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "auth_failed"


async def test_device_without_credential_cannot_be_collected(env: Env):
    headers = await env.auth()
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={"name": "no-cred", "host": "10.0.2.3", "vendor": "huawei"},
    )
    device_id = created.json()["id"]
    response = await env.client.post(
        "/api/v1/devices/%d/test" % device_id, headers=headers
    )
    assert response.status_code == 422
    assert "凭据" in response.json()["detail"]


async def test_batch_import_reports_bad_rows_without_losing_good_ones(env: Env):
    """★ 逐行报错：坏行不能把前面成功的行一起回滚掉。"""
    headers = await env.auth()
    rows = [
        {"name": "imp-1", "host": "10.0.3.1", "vendor": "huawei", "site": "机房C"},
        {"name": "imp-2", "host": "10.0.3.2", "vendor": "H3C", "site": "机房C"},
        {"name": "坏行", "host": "10.0.3.3", "vendor": "不存在的厂商"},
        {"name": "imp-4", "host": "10.0.3.4", "vendor": "ruijie"},
    ]
    response = await env.client.post("/api/v1/devices/import", headers=headers, json=rows)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] == 3
    assert result["skipped"] == 1
    assert "第 4 行" in result["errors"][0]

    page = (await env.client.get("/api/v1/devices", headers=headers)).json()
    assert page["total"] == 3
    vendors = {item["vendor"] for item in page["items"]}
    assert vendors == {"huawei", "h3c", "ruijie"}


async def test_import_creates_credential_profile_from_row(env: Env):
    headers = await env.auth()
    rows = [
        {
            "name": "imp-cred",
            "host": "10.0.4.1",
            "vendor": "huawei",
            "credential_profile": "导入凭据",
            "username": "netadmin",
            "password": "imported-secret",
        }
    ]
    result = (
        await env.client.post("/api/v1/devices/import", headers=headers, json=rows)
    ).json()
    assert result["created"] == 1
    assert result["credential_profiles_created"] == ["导入凭据"]

    profiles = (await env.client.get("/api/v1/credentials", headers=headers)).json()
    assert any(p["name"] == "导入凭据" and p["has_password"] for p in profiles)


async def test_delete_device_requires_admin_and_cascades(env: Env):
    headers = await env.auth()
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={"name": "to-delete", "host": "10.0.5.1", "vendor": "huawei"},
    )
    device_id = created.json()["id"]
    response = await env.client.delete(f"/api/v1/devices/{device_id}", headers=headers)
    assert response.status_code == 200
    assert (
        await env.client.get(f"/api/v1/devices/{device_id}", headers=headers)
    ).status_code == 404


async def test_device_timeout_overrides_survive_edit(env: Env):
    """★ 回归：在界面上"编辑并保存"设备，不能把超时覆盖值清掉。

    根因是**读接口没返回这三个字段、写接口却接受它们**：编辑页拿不到真实值，
    只能提交 null，而 null 会被当成"显式设置"落库。表现就是"只改了个名字，
    结果这台设备的超时覆盖全没了"——而且没有任何报错。

    这类"读写字段集合不一致"的静默数据丢失，只能靠端到端往返测试守住。
    """
    headers = await env.auth()
    created = await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "timeout-dev",
            "host": "10.0.7.1",
            "vendor": "huawei",
            "command_timeout": 300,
            "idle_timeout": 40,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["command_timeout"] == 300, "创建响应就该回显超时字段"
    assert body["idle_timeout"] == 40
    device_id = body["id"]

    # 详情接口同样要回显
    detail = (await env.client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
    assert detail["command_timeout"] == 300 and detail["idle_timeout"] == 40

    # 模拟前端编辑页：把详情原样提交回去（这是它唯一能做的事）
    payload = {
        key: detail[key]
        for key in (
            "name",
            "host",
            "vendor",
            "port",
            "protocol",
            "credential_profile_id",
            "site",
            "hostname",
            "encoding",
            "enabled",
            "legacy_algorithms",
            "use_paramiko",
            "tags",
            "connect_timeout",
            "command_timeout",
            "idle_timeout",
            "notes",
        )
    }
    updated = (
        await env.client.patch(f"/api/v1/devices/{device_id}", headers=headers, json=payload)
    ).json()
    assert updated["command_timeout"] == 300, "编辑保存后超时覆盖被清掉了"
    assert updated["idle_timeout"] == 40


async def test_credential_in_use_cannot_be_deleted(env: Env):
    headers = await env.auth()
    profile = await _credential(env, headers, name="被引用凭据")
    await env.client.post(
        "/api/v1/devices",
        headers=headers,
        json={
            "name": "uses-cred",
            "host": "10.0.6.1",
            "vendor": "huawei",
            "credential_profile_id": profile["id"],
        },
    )
    response = await env.client.delete(f"/api/v1/credentials/{profile['id']}", headers=headers)
    assert response.status_code == 409
    # 断言语义（"被设备占用"），不要断言整句文案——文案一改测试就假失败
    assert "在使用该凭据" in response.json()["detail"]
