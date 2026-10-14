"""登录、当前用户、口令修改、用户管理。

口令相关的一切都在这里：只存 scrypt 哈希，登录失败也写审计（撞库要留痕）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from ...core.security import Role, create_access_token, hash_password, verify_password
from ...db.base import utcnow
from ...db.models import User
from ...errors import Conflict, NotFound, Unauthorized, ValidationFailed
from ...schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    TokenResponse,
    UserCreate,
    UserOut,
)
from ...schemas.common import OkResponse, Page
from ...services import audit
from ..deps import AdminUser, CurrentUser, SessionDep, SettingsDep, client_ip

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse, summary="登录")
async def login(
    payload: LoginRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenResponse:
    user = await session.scalar(select(User).where(User.username == payload.username))
    ip = client_ip(request)
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        # 不区分"用户不存在"和"口令错误"，避免用户名枚举
        await audit.record(
            session,
            action="auth.login_failed",
            username=payload.username,
            client_ip=ip,
        )
        await session.commit()
        raise Unauthorized("用户名或口令错误")

    token, expires_at = create_access_token(
        subject=user.id,
        role=user.role,
        username=user.username,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl_minutes=settings.access_token_ttl_minutes,
    )
    user.last_login_at = utcnow()
    await audit.record(
        session, action="auth.login", user_id=user.id, username=user.username, client_ip=ip
    )
    await session.commit()
    return TokenResponse(
        access_token=token, expires_at=expires_at, user=UserOut.model_validate(user)
    )


@router.get("/auth/me", response_model=UserOut, summary="当前用户")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/auth/change-password", response_model=OkResponse, summary="修改自己的口令")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> OkResponse:
    if not verify_password(payload.old_password, user.password_hash):
        raise ValidationFailed("原口令不正确")
    if payload.old_password == payload.new_password:
        raise ValidationFailed("新口令不能与原口令相同")
    user.password_hash = hash_password(payload.new_password)
    await audit.record(
        session,
        action="auth.change_password",
        user_id=user.id,
        username=user.username,
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(message="口令已更新")


# --------------------------------------------------------------------------
# 用户管理（仅管理员）
# --------------------------------------------------------------------------
@router.get("/users", response_model=Page[UserOut], summary="用户列表")
async def list_users(
    session: SessionDep,
    _: AdminUser,
    limit: int = 50,
    offset: int = 0,
) -> Page[UserOut]:
    total = await session.scalar(select(func.count()).select_from(User))
    result = await session.execute(
        select(User).order_by(User.username).limit(limit).offset(offset)
    )
    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in result.scalars().all()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.post("/users", response_model=UserOut, summary="创建用户")
async def create_user(
    payload: UserCreate,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> UserOut:
    existing = await session.scalar(select(User).where(User.username == payload.username))
    if existing is not None:
        raise Conflict(f"用户名已存在：{payload.username}")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=payload.display_name,
    )
    session.add(user)
    await session.flush()
    await audit.record(
        session,
        action="user.create",
        user_id=actor.id,
        username=actor.username,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "role": user.role},
        client_ip=client_ip(request),
    )
    await session.commit()
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut, summary="修改用户（角色/启停/重置口令）")
async def update_user(
    user_id: int,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
    role: str | None = None,
    is_active: bool | None = None,
    display_name: str | None = None,
    new_password: str | None = None,
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound(f"用户不存在：id={user_id}")
    if user.id == actor.id and is_active is False:
        raise ValidationFailed("不能停用自己")
    if user.id == actor.id and role and role != Role.ADMIN.value:
        raise ValidationFailed("不能降低自己的角色")
    if role is not None:
        try:
            user.role = Role(role).value
        except ValueError as exc:
            raise ValidationFailed(f"角色不合法：{role}") from exc
    if is_active is not None:
        user.is_active = is_active
    if display_name is not None:
        user.display_name = display_name
    if new_password:
        if len(new_password) < 8:
            raise ValidationFailed("口令至少 8 位")
        user.password_hash = hash_password(new_password)
    await audit.record(
        session,
        action="user.update",
        user_id=actor.id,
        username=actor.username,
        target_type="user",
        target_id=user.id,
        detail={"role": user.role, "is_active": user.is_active, "reset_password": bool(new_password)},
        client_ip=client_ip(request),
    )
    await session.commit()
    return UserOut.model_validate(user)
