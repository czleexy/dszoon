"""登录口令哈希、JWT 与角色。

口令哈希用标准库 ``hashlib.scrypt``，不引入 passlib/bcrypt——少一个依赖，
少一类"bcrypt 版本不兼容"的部署问题。scrypt 的参数写进哈希串，将来调参可以
平滑兼容旧记录。

格式：``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import jwt

__all__ = [
    "ROLE_RANK",
    "Role",
    "TokenError",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "role_at_least",
    "verify_password",
]

# scrypt 参数：n=2**14 / r=8 / p=1 ≈ 16MB 内存、几十毫秒——抗暴力与登录体验的平衡点
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16


class Role(str, Enum):
    """角色。权限是"包含式"的：admin > operator > auditor > viewer。"""

    ADMIN = "admin"
    OPERATOR = "operator"
    AUDITOR = "auditor"
    VIEWER = "viewer"

    @property
    def label(self) -> str:
        return {
            Role.ADMIN: "管理员",
            Role.OPERATOR: "运维",
            Role.AUDITOR: "审计",
            Role.VIEWER: "只读",
        }[self]


ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 10,
    Role.AUDITOR: 20,
    Role.OPERATOR: 30,
    Role.ADMIN: 40,
}

#: 可以查看配置明文的角色（脱敏之外的"看真话"权限）
PLAINTEXT_ROLES = (Role.ADMIN, Role.AUDITOR)


def role_at_least(role: Role | str, minimum: Role | str) -> bool:
    try:
        left = Role(role) if not isinstance(role, Role) else role
        right = Role(minimum) if not isinstance(minimum, Role) else minimum
    except ValueError:
        return False
    return ROLE_RANK[left] >= ROLE_RANK[right]


# --------------------------------------------------------------------------
# 口令
# --------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """常数时间校验口令。哈希串损坏时返回 False，不抛异常。"""
    if not password or not encoded:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, hash_raw = encoded.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_raw),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(_unb64(hash_raw)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, _unb64(hash_raw))


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
class TokenError(Exception):
    """令牌无效或已过期。"""


def create_access_token(
    *,
    subject: str | int,
    role: Role | str,
    username: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_minutes: int = 720,
) -> tuple[str, datetime]:
    """签发访问令牌，返回 (token, 过期时间)。"""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=max(1, ttl_minutes))
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role.value if isinstance(role, Role) else str(role),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(8),
    }
    token = jwt.encode(payload, secret, algorithm=algorithm)
    if isinstance(token, bytes):  # pragma: no cover - PyJWT 2.x 返回 str
        token = token.decode("ascii")
    return token, expires_at


def decode_access_token(token: str, *, secret: str, algorithm: str = "HS256") -> dict[str, Any]:
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("令牌无效") from exc
    if "sub" not in payload:
        raise TokenError("令牌缺少 sub")
    return dict(payload)
