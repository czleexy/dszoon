"""厂商驱动包。

内置驱动以 YAML 声明式定义（``huawei.yaml`` / ``h3c.yaml`` / ``ruijie.yaml``），
由 :class:`~..registry.DriverRegistry` 自动发现。新增厂商 = 新增一个 YAML 文件。
"""

from .base import DriverInfo, PrivilegeStep, VendorDriver
from .yamldriver import YamlDriver, load_driver_spec, load_yaml_driver

__all__ = [
    "DriverInfo",
    "PrivilegeStep",
    "VendorDriver",
    "YamlDriver",
    "load_driver_spec",
    "load_yaml_driver",
]
