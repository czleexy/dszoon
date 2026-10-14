"""应用级异常。

服务层只抛这些异常，不感知 HTTP；由 ``app.main`` 统一转成 JSON 响应
（见 ``register_exception_handlers``）。这样服务层可以脱离 Web 框架被 CLI、
定时任务、测试直接复用。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppError",
    "Conflict",
    "Forbidden",
    "NotFound",
    "Unauthorized",
    "ValidationFailed",
]


class AppError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
