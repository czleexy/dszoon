"""包级导出与模块公开 API 的一致性检查。

**为什么需要这组测试**：`probe_device` 加进 `app/collector/engine.py` 之后，
忘了同步加到 `app/collector/__init__.py`。后果是 `tests/conftest.py` 导入失败
→ **全部** API 测试报错，而报错位置指向调用方 `app/api/v1/devices.py`：

    ImportError: cannot import name 'probe_device' from 'app.collector'

一眼看不出是"这里少了一行导出"。这类"给模块加了符号、忘了更新出口"的问题在
静态阅读时几乎不可见（`__init__.py` 和 `engine.py` 分开看都是对的），
但会让整个测试收集阶段崩掉，所以固化成测试。
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import app

#: app.collector 对外的公开 API（Web 层 / CLI / 脚本依赖这些名字）
COLLECTOR_PUBLIC_API = (
    # 引擎
    "BackupOutcome",
    "BackupStatus",
    "DeviceTarget",
    "ProbeResult",
    "backup_device",
    "backup_many",
    "probe_device",
    # 异常
    "CollectorError",
    "AuthError",
    "ConnectError",
    "CommandTimeout",
    "ConfirmationLoop",
    "DeployNotSupported",
    "IdleTimeoutError",
    "IntegrityError",
    "SessionClosed",
    "UnknownVendor",
    "UnsupportedProtocol",
    # 驱动注册表
    "DriverRegistry",
    "default_registry",
)


def _iter_app_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []
    for module_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        modules.append(importlib.import_module(module_info.name))
    return modules


def test_every_module_declared_export_exists():
    """每个模块 __all__ 里的名字都必须真实存在。

    最常见的是"列了一个已改名/已删除的符号"——通读代码看不出来，
    但 `from module import *` 会直接抛 AttributeError。
    """
    problems: list[str] = []
    for module in _iter_app_modules():
        declared = getattr(module, "__all__", None)
        if not declared:
            continue
        for name in declared:
            if not hasattr(module, name):
                problems.append(f"{module.__name__}.{name}")
    assert not problems, f"__all__ 里声明了不存在的符号：{problems}"


def test_collector_package_exports_public_api():
    """★ 回归测试：采集层的对外出口不能漏符号。

    漏一个的名字，就会让调用方在**导入期**直接炸（不是运行期报错，是 import 失败），
    而 pytest 会停在 conftest 加载阶段，所有测试一起报错。
    """
    collector = importlib.import_module("app.collector")
    missing_attr = [name for name in COLLECTOR_PUBLIC_API if not hasattr(collector, name)]
    missing_all = [name for name in COLLECTOR_PUBLIC_API if name not in collector.__all__]
    assert not missing_attr, f"app.collector 未导出：{missing_attr}"
    assert not missing_all, f"app.collector.__all__ 缺少：{missing_all}"


def test_engine_exports_match_package_exports():
    """engine 的 __all__ 必须被包级出口覆盖（否则就是漏了一层）。

    这条能直接抓住"加了新函数但忘了加导出"——engine 是采集层唯一被外部
    直接使用的模块，它的 __all__ 就是对外的承诺。
    """
    engine = importlib.import_module("app.collector.engine")
    collector = importlib.import_module("app.collector")
    missing = [name for name in engine.__all__ if not hasattr(collector, name)]
    assert not missing, f"engine.__all__ 里有 {missing} 未从 app.collector 导出"
