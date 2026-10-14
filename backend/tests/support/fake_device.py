"""内存假设备会话：让 API 测试不碰网络、不依赖 SSH。

为什么不用 P0 的 mock SSH 服务器：那是用来测**采集层**的（分页/ANSI/断连），
而 API 测试关心的是"接口 → 服务 → 落库"这条链路。用假会话把这层隔开，
测试快且完全确定；真设备的端到端能力由 P0 的测试与探针负责。

假会话只实现引擎真正调用的那几个方法（``open/begin/send_command/close`` +
``last_received_line/transcript_text`` + 属性 ``prompt_re/strict_prompt/confirm_rules``）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Pattern

from app.collector.errors import AuthError, ConnectError

__all__ = ["FakeDevice", "FakeSessionFactory", "make_fake_device"]


@dataclass(slots=True)
class FakeDevice:
    hostname: str
    vendor: str
    responses: dict[str, str] = field(default_factory=dict)
    #: "auth_failed" / "unreachable" → open() 抛对应异常
    fail_with: str | None = None
    #: 每条命令的人为延时（秒）。用来测"任务还在跑"时的行为：
    #: SSE 订阅、任务取消——不加延时的话假设备瞬间跑完，
    #: 流请求到达时任务已经结束，测试就会变得不确定。
    delay: float = 0.0

    def with_config(self, config_text: str) -> "FakeDevice":
        key = _config_command(self.vendor)
        self.responses[key] = config_text
        return self


def _config_command(vendor: str) -> str:
    return {
        "huawei": "display current-configuration",
        "h3c": "display current-configuration",
        "ruijie": "show running-config",
    }.get(vendor, "display current-configuration")


def make_fake_device(
    *,
    vendor: str = "huawei",
    hostname: str = "FAKE-SW-01",
    config_text: str,
    version_text: str = "",
    fail_with: str | None = None,
    delay: float = 0.0,
) -> FakeDevice:
    """按厂商把命令表填好，测试只需要给配置内容。"""
    if vendor == "huawei":
        responses = {
            "screen-length 0 temporary": "",
            "screen-width 512": "",
            "display version": version_text,
            "display current-configuration": config_text,
            "display saved-configuration": "",
        }
    elif vendor == "h3c":
        responses = {
            "screen-length disable": "",
            "screen-width 512": "",
            "display version": version_text,
            "display current-configuration": config_text,
            "display saved-configuration": "",
        }
    else:
        responses = {
            "enable": "",
            "terminal length 0": "",
            "terminal width 512": "",
            "show version": version_text,
            "show running-config": config_text,
            "show startup-config": "",
        }
    return FakeDevice(
        hostname=hostname,
        vendor=vendor,
        responses=responses,
        fail_with=fail_with,
        delay=delay,
    )


class FakeSession:
    def __init__(self, config: Any, prompt_re: Pattern[str], device: FakeDevice) -> None:
        self.config = config
        self.prompt_re = prompt_re
        self.device = device
        self.strict_prompt = False
        self.confirm_rules: list[Any] = []
        self.confirmation_count = 0
        self.sent: list[str] = []

    async def open(self) -> None:
        if self.device.fail_with == "auth_failed":
            raise AuthError("假设备：认证失败")
        if self.device.fail_with == "unreachable":
            raise ConnectError("假设备：不可达")

    async def begin(self) -> None:
        return None

    async def send_command(
        self,
        command: str,
        *,
        timeout: float | None = None,
        prompt_re: Pattern[str] | None = None,
        secret: bool = False,
    ) -> str:
        if self.device.delay:
            # 让"任务还在跑"有一个可观测的窗口（见 FakeDevice.delay 注释）
            await asyncio.sleep(self.device.delay)
        self.sent.append("******" if secret else command)
        return self.device.responses.get(command, "")

    async def resync(self, *, timeout: float = 15.0) -> bool:
        return True

    async def close(self) -> None:
        return None

    def last_received_line(self) -> str | None:
        # 按厂商给出真实形态的提示符：华为/华三是 <HOST>，锐捷是 HOST> / HOST#
        if self.device.vendor == "ruijie":
            return f"{self.device.hostname}>"
        return f"<{self.device.hostname}>"

    def transcript_text(self) -> str:
        lines = [f"$ {cmd}" for cmd in self.sent]
        return "\n".join([f"# 假会话 {self.device.hostname}", *lines, ""])


class FakeSessionFactory:
    """``session_factory`` 的实现，按主机名分发到对应的假设备。"""

    def __init__(
        self,
        devices: dict[str, FakeDevice] | None = None,
        *,
        default: FakeDevice | None = None,
    ) -> None:
        self.devices: dict[str, FakeDevice] = dict(devices or {})
        self.default = default
        self.calls: list[str] = []

    def __call__(self, config: Any, prompt_re: Pattern[str]) -> FakeSession:
        self.calls.append(config.host)
        device = self.devices.get(config.host) or self.default
        if device is None:
            raise ConnectError(f"假设备未配置：{config.host}")
        return FakeSession(config, prompt_re, device)

    def register(self, host: str, device: FakeDevice) -> None:
        self.devices[host] = device
