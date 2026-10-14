"""示例：YAML 描述不够用时，如何用 Python 子类只覆盖需要改的那一个方法。

使用方式（例如放在客户私有驱动目录里，由 ``DriverRegistry(extra_dirs=[...])`` 加载）::

    from app.collector.registry import DriverRegistry
    from app.collector.drivers.custom_example import LegacyHuaweiDriver

    registry = DriverRegistry(extra_dirs=["/opt/dszoon/drivers"]).load()
    registry.register(LegacyHuaweiDriver(), replace=True)

设计意图：**优先扩展 YAML**。只有当差异无法用
「提示符正则 + 命令表 + 归一化 + 脱敏 + 校验」描述时，才写 Python。
这样新增厂商的维护成本才能保持在"改配置"而不是"改代码"的量级。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .. import textutil
from .yamldriver import YamlDriver, load_driver_spec

#: 复用内置华为驱动作为基底
_BASE_SPEC = Path(__file__).resolve().parent / "huawei.yaml"


class LegacyHuaweiDriver(YamlDriver):
    """老旧 VRP5 设备的示例驱动。

    差异点（纯示例，需实测确认）：
    * 不支持 ``screen-width``，只支持 ``screen-length``；
    * 提示符在配置回显中间可能带 ``\\x1b[42D`` 重绘序列（清洗层已处理，这里只是演示）；
    * 版本输出里没有 "uptime is" 行，型号要从另一个字段抓。
    """

    def __init__(self) -> None:
        spec = load_driver_spec(_BASE_SPEC)
        spec["name"] = "huawei-legacy"
        spec["display_name"] = "华为 VRP5（老旧设备）"
        spec["aliases"] = ["vrp5"]
        spec["description"] = "VRP5 老设备分支：去掉 screen-width，型号从 display version 其它字段抓取"
        # 覆盖 YAML 里的命令表
        spec["session_setup"] = ["screen-length 0 temporary"]
        spec["facts"]["model"] = r"^(?:HUAWEI|Huawei)\s+(\S+)\s+Routing Switch"
        super().__init__(spec, source=_BASE_SPEC)

    def session_setup_cmds(self) -> Sequence[str]:
        """额外兜底：先尝试关分页，再补一条宽度命令（失败会被记为 setup 警告）。"""
        return ("screen-length 0 temporary", "screen-width 512")

    def normalize(self, text: str) -> str:
        text = super().normalize(text)
        # 老设备会在配置里插入运行时长行（每次都不一样），必须去掉，否则天天报变更
        text = textutil.normalize_newlines(text)
        lines = [line for line in text.split("\n") if not line.startswith("!Running time:")]
        return "\n".join(lines)
