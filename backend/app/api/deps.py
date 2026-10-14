"""API 依赖：数据库会话、当前用户、角色校验、应用级单例。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.crypto import CredentialCipher
from ..core.security import Role, TokenError, decode_access_token, role_at_least
from ..db.models import User
from ..db.session import Database
from ..errors import Forbidden, Unauthorized
from ..services.backups import BackupRunner
from ..services.blobs import BlobStore
from ..services.notifications import Notifier
from ..services.scheduler import ScheduleRunner

__all__ = [
    "AdminUser",
    "AuditorUser",
    "BlobsDep",
    "CipherDep",
    "CurrentUser",
    "NotifierDep",
    "OperatorUser",
    "RunnerDep",
    "SchedulerDep",
    "SessionDep",
    "SessionFactoryDep",
    "SettingsDep",
    "get_blobs",
    "get_cipher",
    "get_notifier",
    "get_runner",
    "get_scheduler",
    "get_session",
    "get_settings_dep",
    "require_role",
]

_bearer = HTTPBearer(auto_error=False, description="登录后获得的 Bearer 令牌")


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_cipher(request: Request) -> CredentialCipher:
    return request.app.state.cipher


def get_blobs(request: Request) -> BlobStore:
    return request.app.state.blobs


def get_runner(request: Request) -> BackupRunner:
    return request.app.state.runner


def get_session_factory(request: Request) -> Any:
    """会话工厂（生产为 None = 用真实 SSH）。

    测试注入假设备时，**所有**走采集层的入口都必须拿到它：备份任务走
    ``BackupRunner``，而"连接测试"直接调 ``probe_device``——漏掉任何一处，
    那条路径就会去发真实连接。
    """
    return getattr(request.app.state, "session_factory", None)


def get_notifier(request: Request) -> Notifier:
    return request.app.state.notifier


def get_scheduler(request: Request) -> ScheduleRunner:
    return request.app.state.scheduler


def get_database_dep(request: Request) -> Database:
    return request.app.state.database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.database
    async with database.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
CipherDep = Annotated[CredentialCipher, Depends(get_cipher)]
BlobsDep = Annotated[BlobStore, Depends(get_blobs)]
RunnerDep = Annotated[BackupRunner, Depends(get_runner)]
SessionFactoryDep = Annotated[Any, Depends(get_session_factory)]
NotifierDep = Annotated[Notifier, Depends(get_notifier)]
SchedulerDep = Annotated[ScheduleRunner, Depends(get_scheduler)]


async def get_current_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """解析 Bearer 令牌并取出用户。

    ``_bearer`` 用 ``auto_error=False``，所以"没带令牌"不会由 FastAPI 直接抛 403，
    而是走到这里统一抛 401（我们的 AppError），前端拦截器才能一致地跳登录页。
    """
    if credentials is None or not credentials.credentials:
        raise Unauthorized("未提供访问令牌")
    settings: Settings = request.app.state.settings
    try:
        payload = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise Unauthorized(str(exc)) from exc
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Unauthorized("令牌内容异常") from exc
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("用户不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: Role):
    """生成一个"至少需要某角色"的依赖。角色是包含式的：admin > operator > auditor > viewer。"""

    async def _dependency(user: CurrentUser) -> User:
        if not role_at_least(user.role, minimum):
            raise Forbidden(f"该操作需要 {minimum.label}（{minimum.value}）及以上权限")
        return user

    return _dependency


OperatorUser = Annotated[User, Depends(require_role(Role.OPERATOR))]
AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]
AuditorUser = Annotated[User, Depends(require_role(Role.AUDITOR))]


def client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host
