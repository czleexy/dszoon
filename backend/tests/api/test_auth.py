"""鉴权与权限测试。"""

from __future__ import annotations

from tests.support.env import ADMIN_PASSWORD, VIEWER_PASSWORD, Env


async def test_login_success_returns_token_and_user(env: Env):
    data = await env.login()
    assert data["access_token"]
    assert data["user"]["username"] == "admin"
    assert data["user"]["role"] == "admin"
    assert data["expires_at"].endswith("Z")  # 统一输出 UTC


async def test_login_with_wrong_password_is_rejected(env: Env):
    response = await env.client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_login_failure_is_audited(env: Env):
    """撞库要留痕——这是审计的基本要求。"""
    await env.client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
    )
    from sqlalchemy import select

    from app.db.models import AuditLog

    async with env.database.session() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert any(row.action == "auth.login_failed" for row in rows)


async def test_me_requires_token(env: Env):
    assert (await env.client.get("/api/v1/auth/me")).status_code == 401
    headers = await env.auth()
    response = await env.client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


async def test_invalid_token_is_rejected(env: Env):
    response = await env.client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_change_password_then_old_one_stops_working(env: Env):
    headers = await env.auth()
    response = await env.client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"old_password": ADMIN_PASSWORD, "new_password": "new-secret-123"},
    )
    assert response.status_code == 200, response.text

    assert (await env.login(password="new-secret-123"))["user"]["username"] == "admin"
    bad = await env.client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
    )
    assert bad.status_code == 401


async def test_change_password_rejects_wrong_old_password(env: Env):
    headers = await env.auth()
    response = await env.client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"old_password": "nope", "new_password": "whatever-123"},
    )
    assert response.status_code == 422


async def test_admin_can_create_viewer_and_viewer_is_limited(env: Env):
    admin = await env.auth()
    created = await env.client.post(
        "/api/v1/users",
        headers=admin,
        json={"username": "reader", "password": VIEWER_PASSWORD, "role": "viewer"},
    )
    assert created.status_code == 200, created.text

    viewer = await env.auth("reader", VIEWER_PASSWORD)
    # 只读角色能看设备列表
    assert (await env.client.get("/api/v1/devices", headers=viewer)).status_code == 200
    # 但不能新增设备、不能建用户
    forbidden = await env.client.post(
        "/api/v1/devices",
        headers=viewer,
        json={"name": "X", "host": "10.0.0.1", "vendor": "huawei"},
    )
    assert forbidden.status_code == 403
    assert (await env.client.get("/api/v1/users", headers=viewer)).status_code == 403


async def test_duplicate_username_conflicts(env: Env):
    admin = await env.auth()
    payload = {"username": "dup", "password": "password-123", "role": "viewer"}
    assert (
        await env.client.post("/api/v1/users", headers=admin, json=payload)
    ).status_code == 200
    assert (
        await env.client.post("/api/v1/users", headers=admin, json=payload)
    ).status_code == 409


async def test_health_endpoints_are_open(env: Env):
    assert (await env.client.get("/healthz")).json()["status"] == "ok"
    ready = await env.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["database"] is True


async def test_meta_vendors_lists_three_drivers(env: Env):
    headers = await env.auth()
    response = await env.client.get("/api/v1/meta/vendors", headers=headers)
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert names == {"huawei", "h3c", "ruijie"}
    huawei = next(item for item in response.json() if item["name"] == "huawei")
    assert huawei["running_config_cmd"] == "display current-configuration"
    assert huawei["supports_deploy"] is False  # v1.0 只读
