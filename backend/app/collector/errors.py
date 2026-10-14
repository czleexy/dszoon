"""采集异常定义。

异常到备份状态的映射集中在 :meth:`CollectorError.code`，引擎据此写 run 记录，
前端/报表可以直接按 code 聚合失败原因，而不是靠字符串匹配错误消息。
"""

from __future__ import annotations

from typing import Sequence

from .integrity import Issue

__all__ = [
    "AuthError",
    "CollectorError",
    "CommandTimeout",
    "ConfirmationLoop",
    "ConnectError",
    "DeployNotSupported",
    "IdleTimeoutError",
    "IntegrityError",
    "SessionClosed",
    "UnknownVendor",
    "UnsupportedProtocol",
]


class CollectorError(Exception):
    """采集流程所有可预期异常的基类。"""

    code: str = "error"
    #: 是否值得原样重试（网络抖动类为 True，认证/配置类为 False）
    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        return self.message if not self.detail else f"{self.message} ({self.detail})"


class ConnectError(CollectorError):
    """TCP/协议层无法建立会话（不可达、端口关闭、算法协商失败）。"""

    code = "unreachable"
    retryable = True


class AuthError(CollectorError):
    """认证失败或设备要求改密。绝不重试，避免锁账号。"""

    code = "auth_failed"
    retryable = False


class UnsupportedProtocol(CollectorError):
    code = "unsupported_protocol"
    retryable = False


class CommandTimeout(CollectorError):
    """命令执行超过总超时。"""

    code = "timeout"
    retryable = True


class IdleTimeoutError(CommandTimeout):
    """长时间无数据且探活无效，会话疑似卡死。

    继承 CommandTimeout 以便复用 retryable=True：卡死通常重连即可恢复。
    """

    code = "timeout"


class SessionClosed(CollectorError):
    """对端在命令执行过程中关闭了连接。"""

    code = "session_closed"
    retryable = True


class IntegrityError(CollectorError):
    """采到了内容但内容不可信（疑似折行/截断/命令报错）。

    注意：这类结果**仍然会被归档**，用于人工排障，只是状态被标为异常，
    绝不静默当作"备份成功"。
    """

    code = "integrity_error"
    retryable = False

    def __init__(self, message: str, issues: Sequence[Issue]) -> None:
        super().__init__(message)
        self.issues: list[Issue] = list(issues)


class DeployNotSupported(CollectorError):
    """厂商/驱动不支持配置下发（v1.0 全部驱动都是这个状态）。"""

    code = "deploy_not_supported"
    retryable = False


class UnknownVendor(CollectorError):
    """设备台账里的 vendor 在驱动注册表里找不到。"""

    code = "unknown_vendor"
    retryable = False


class ConfirmationLoop(CollectorError):
    """设备反复弹交互确认，自动回答后仍未回到提示符。

    采集器会自动回答 ``[Y/N]`` 类确认（例如华为 ``screen-width`` 的
    "This command will change the default screen width. Continue? [Y/N]:"）。
    正常情况一次就够；连续多次说明会话已经跑偏，再继续回答只会掩盖问题。
    """

    code = "confirmation_loop"
    retryable = False
