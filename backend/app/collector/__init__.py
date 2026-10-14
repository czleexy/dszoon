"""采集子系统：会话、清洗、驱动、引擎、归档。

这里是**对外出口**：Web 层、CLI、脚本都从 ``app.collector`` 取东西。
加符号时务必同步这个文件——漏一个就会让 import 方在**导入期**直接炸，
而报错位置在调用方（例如 ``app/api/v1/devices.py``），一眼看不出是这里缺了导出。
（真事：``probe_device`` 加进 ``engine.py`` 后忘了加到这里，导致 conftest 导入失败、
全部 API 测试报错。``scripts/selfcheck.py`` 第 3 步会把这类问题一次性列全。）
"""

from .engine import (
    BackupOutcome,
    BackupStatus,
    DeviceTarget,
    ProbeResult,
    backup_device,
    backup_many,
    probe_device,
)
from .errors import (
    AuthError,
    CollectorError,
    CommandTimeout,
    ConfirmationLoop,
    ConnectError,
    DeployNotSupported,
    IdleTimeoutError,
    IntegrityError,
    SessionClosed,
    UnknownVendor,
    UnsupportedProtocol,
)
from .registry import DriverRegistry, default_registry

__all__ = [
    "AuthError",
    "BackupOutcome",
    "BackupStatus",
    "CollectorError",
    "CommandTimeout",
    "ConfirmationLoop",
    "ConnectError",
    "DeployNotSupported",
    "DeviceTarget",
    "DriverRegistry",
    "IdleTimeoutError",
    "IntegrityError",
    "ProbeResult",
    "SessionClosed",
    "UnknownVendor",
    "UnsupportedProtocol",
    "backup_device",
    "backup_many",
    "default_registry",
    "probe_device",
]
