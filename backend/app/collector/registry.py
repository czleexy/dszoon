"""驱动注册表。

职责：发现并加载驱动，把"设备台账里的 vendor 字符串"解析成驱动对象。
额外目录（``extra_dirs``）用于放客户私有驱动，不动内置目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Sequence

from .drivers.base import VendorDriver
from .drivers.yamldriver import YamlDriver, iter_driver_files, load_driver_spec
from .errors import UnknownVendor

__all__ = ["DriverRegistry", "default_registry", "get_registry", "BUILTIN_DRIVER_DIR"]

BUILTIN_DRIVER_DIR = Path(__file__).resolve().parent / "drivers"


class DriverRegistry:
    """驱动集合，支持 vendor 名与别名的双向解析。"""

    def __init__(self, extra_dirs: Sequence[Path | str] = ()) -> None:
        self._drivers: dict[str, VendorDriver] = {}
        self._alias_index: dict[str, str] = {}
        self._extra_dirs = [Path(d) for d in extra_dirs]
        self._loaded = False

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self, *, builtin_dir: Path | str | None = None) -> "DriverRegistry":
        directories: list[Path] = []
        if builtin_dir is not None:
            directories.append(Path(builtin_dir))
        elif BUILTIN_DRIVER_DIR.is_dir():
            directories.append(BUILTIN_DRIVER_DIR)
        directories.extend(self._extra_dirs)

        for directory in directories:
            for path in iter_driver_files(directory):
                try:
                    spec = load_driver_spec(path)
                except Exception as exc:  # noqa: BLE001 - 驱动文件坏了要明确报出来
                    raise ValueError(f"加载驱动文件 {path} 失败：{exc}") from exc
                if spec.get("enabled", True) is False:
                    continue
                self.register(YamlDriver(spec, source=path))
        self._loaded = True
        return self

    # ------------------------------------------------------------------
    # 增删查
    # ------------------------------------------------------------------
    def register(self, driver: VendorDriver, *, replace: bool = False) -> None:
        key = driver.name.lower()
        if key in self._drivers and not replace:
            raise ValueError(f"驱动 {driver.name!r} 重复注册")
        self._drivers[key] = driver
        for alias in driver.aliases:
            self._alias_index[alias.lower()] = key

    def unregister(self, vendor: str) -> bool:
        key = vendor.strip().lower()
        if key in self._alias_index:
            key = self._alias_index.pop(key)
        return self._drivers.pop(key, None) is not None

    def get(self, vendor: str) -> VendorDriver:
        driver = self.try_get(vendor)
        if driver is None:
            raise UnknownVendor(
                f"未知厂商 {vendor!r}；已加载驱动：{', '.join(self.names()) or '<无>'}"
            )
        return driver

    def try_get(self, vendor: str) -> VendorDriver | None:
        key = (vendor or "").strip().lower()
        if key in self._drivers:
            return self._drivers[key]
        if key in self._alias_index:
            return self._drivers[self._alias_index[key]]
        # 兜底：允许 "Huawei VRP" / "huawei-vrp" 这类混合写法
        for name, driver in self._drivers.items():
            if name in key or key in name:
                return driver
        return None

    def all(self) -> list[VendorDriver]:
        return list(self._drivers.values())

    def names(self) -> list[str]:
        return sorted(self._drivers)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def __contains__(self, vendor: object) -> bool:
        return isinstance(vendor, str) and self.try_get(vendor) is not None

    def __iter__(self) -> Iterator[VendorDriver]:
        return iter(self._drivers.values())

    def __len__(self) -> int:
        return len(self._drivers)


#: 进程内默认注册表（导入时加载内置驱动，YAML 有问题立即暴露）
default_registry = DriverRegistry().load()


def get_registry() -> DriverRegistry:
    return default_registry
