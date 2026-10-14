"""pytest 夹具。

**这里为什么用"延迟导入"**：``tests.support.env`` 会导入 ``app.main``，也就是整条
API 链（main → api.v1.router → 各路由 → 服务层 → db/models）。

如果把它写在模块级，那么 API 层任何一个模块有导入错误，conftest 就会在**加载阶段**
失败，结果是**所有测试一个都不跑**——包括与 API 完全无关的 100 多个采集层（P0）测试。

真实教训：``app.collector`` 漏导出一个 ``probe_device``，导致 conftest 导入失败，
pytest 只打印一句 ImportError 就退出，既看不到采集层是否健康，也不知道坏在哪一层。

改成在夹具里导入后：

* 采集层测试照常收集、照常跑（它们是已知可用的，能给出可信的对照信号）；
* 只有 ``tests/api/*`` 在**建立夹具时**报错，报错信息就是那句 ImportError。

这样一次 pytest 就能同时回答"哪一层坏了"和"其余部分还好吗"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# 轻量导入：fake_device 只依赖 app.collector.errors，不碰 API 链，放在模块级安全
from tests.support.fake_device import FakeDevice, make_fake_device

if TYPE_CHECKING:  # 仅用于类型标注；with __future__ annotations 不会在运行期求值
    from tests.support.env import Env

__all__ = ["env", "huawei_device"]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    """完整的 API 测试环境（临时 SQLite 库 + 假设备会话 + ASGI 客户端）。

    注意这里的导入是**故意放在函数体内**的：见模块文档串。
    """
    from tests.support.env import build_env

    environment, client = await build_env(tmp_path)
    try:
        yield environment
    finally:
        # 顺序有讲究：先取消可能还在跑的后台备份任务，再关数据库。
        # 反过来的话，后台任务会拿着已释放的连接继续跑，抛出的
        # "Event loop is closed" 之类噪声会盖掉真正的失败原因。
        await environment.runner.shutdown()
        await client.aclose()
        await environment.database.dispose()


@pytest.fixture
def huawei_device() -> FakeDevice:
    from tests.support.env import HUAWEI_CONFIG, HUAWEI_VERSION, fixture_text

    return make_fake_device(
        vendor="huawei",
        hostname="HW-CORE-01",
        config_text=fixture_text(HUAWEI_CONFIG),
        version_text=fixture_text(HUAWEI_VERSION),
    )
