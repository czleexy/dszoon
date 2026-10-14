"""鉴权与用户 schema。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.security import Role
from .common import ORMModel, UtcDatetime

__all__ = [
    "ChangePasswordRequest",
    "LoginRequest",
    "TokenResponse",
    "UserCreate",
    "UserOut",
]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(ORMModel):
    id: int
    username: str
    role: str
    display_name: str | None = None
    is_active: bool
    last_login_at: UtcDatetime | None = None

    @property
    def role_label(self) -> str:
        try:
            return Role(self.role).label
        except ValueError:  # pragma: no cover
            return self.role


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: UtcDatetime
    user: UserOut


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=256)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default=Role.VIEWER.value)
    display_name: str | None = Field(default=None, max_length=64)

    @field_validator("role")
    @classmethod
    def _valid_role(cls, value: str) -> str:
        try:
            return Role(value).value
        except ValueError as exc:
            allowed = ", ".join(role.value for role in Role)
            raise ValueError(f"角色必须是以下之一：{allowed}") from exc
