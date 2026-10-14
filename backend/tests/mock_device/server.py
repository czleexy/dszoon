"""假设备服务器：按 YAML 剧本回放 SSH 交互。

为什么需要它（这是本项目在测试上最重要的一个决定）：

* CI 里跑不了 eNSP / HCL / EVE-NG，也不该跑——虚拟真机慢、重、不确定；
* 采集层 90% 的 bug 都在"异常路径"上：分页没关、输出被折行、设备中途断连、
  认证失败、命令被拒绝、GBK 乱码。这些用真机很难稳定复现，用剧本可以精确复现；
* 有了它，新增厂商驱动可以"先写夹具 + 剧本 → 跑绿 → 再上真机"，迭代速度快一个量级。

支持模拟：分页（两种分页符位置）、ANSI 噪声、GBK 编码、认证失败、命令报错、
会话中途断连、设备卡死（无响应）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # pragma: no cover
    import asyncssh
except ImportError:  # pragma: no cover
    asyncssh = None  # type: ignore[assignment]

__all__ = [
    "FIXTURE_ROOT",
    "MockDeviceServer",
    "Rule",
    "Scenario",
    "load_scenarios",
    "start_mock",
]

HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE.parent / "fixtures"
DEFAULT_SCENARIO_FILE = HERE / "scenarios.yaml"

#: 设备收到无法识别的命令时的默认回显（会被完整性校验识别为设备报错）
DEFAULT_OUTPUT = "% Unrecognized command found at '^' position."

_ECHO_OFF = object()


@dataclass(slots=True)
class Rule:
    pattern: re.Pattern[str]
    output: str | None = None
    output_file: str | None = None
    prompt: bool = True
    echo: Any = _ECHO_OFF  # _ECHO_OFF 表示沿用场景默认值
    state: dict[str, Any] = field(default_factory=dict)
    behavior: str | None = None  # hang | close_mid_output


@dataclass(slots=True)
class Scenario:
    name: str
    hostname: str
    vendor: str
    username: str = "admin"
    password: str = "admin123"
    reject_auth: bool = False
    banner: str = ""
    encoding: str = "utf-8"
    ansi_noise: bool = False
    echo: bool = True
    prompts: dict[str, str] = field(default_factory=lambda: {"user": "<{hostname}>"})
    paging_enabled: bool = True
    page_lines: int = 12
    more_token: str = "  ---- More ----"
    more_style: str = "own_line"  # own_line | inline
    default_output: str = DEFAULT_OUTPUT
    rules: list[Rule] = field(default_factory=list)

    def match(self, command: str) -> Rule | None:
        for rule in self.rules:
            if rule.pattern.match(command):
                return rule
        return None


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
def load_scenarios(path: Path | str | None = None) -> dict[str, Scenario]:
    path = Path(path) if path else DEFAULT_SCENARIO_FILE
    raw_list = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    scenarios: dict[str, Scenario] = {}
    for raw in raw_list:
        scenario = _build_scenario(raw)
        scenarios[scenario.name] = scenario
    return scenarios


def _build_scenario(raw: dict[str, Any]) -> Scenario:
    paging = raw.get("paging") or {}
    rules = [
        Rule(
            pattern=re.compile(str(item["pattern"])),
            output=item.get("output"),
            output_file=item.get("output_file"),
            prompt=bool(item.get("prompt", True)),
            echo=item.get("echo", _ECHO_OFF),
            state=dict(item.get("set") or {}),
            behavior=item.get("behavior"),
        )
        for item in (raw.get("rules") or [])
    ]
    return Scenario(
        name=str(raw["name"]),
        hostname=str(raw.get("hostname", "MOCK")),
        vendor=str(raw.get("vendor", "unknown")),
        username=str(raw.get("username", "admin")),
        password=str(raw.get("password", "admin123")),
        reject_auth=bool(raw.get("reject_auth", False)),
        banner=str(raw.get("banner", "")),
        encoding=str(raw.get("encoding", "utf-8")),
        ansi_noise=bool(raw.get("ansi_noise", False)),
        echo=bool(raw.get("echo", True)),
        prompts=dict(raw.get("prompts") or {"user": "<{hostname}>"}),
        paging_enabled=bool(paging.get("enabled", True)),
        page_lines=int(paging.get("page_lines", 12)),
        more_token=str(paging.get("token", "  ---- More ----")),
        more_style=str(paging.get("style", "own_line")),
        default_output=str(raw.get("default_output", DEFAULT_OUTPUT)),
        rules=rules,
    )


# --------------------------------------------------------------------------
# 设备状态机
# --------------------------------------------------------------------------
class MockDevice:
    """一台假设备的终端行为。"""

    def __init__(self, scenario: Scenario, fixture_root: Path = FIXTURE_ROOT) -> None:
        self.scenario = scenario
        self.fixture_root = Path(fixture_root)
        self.reset()

    def reset(self) -> None:
        self.mode = "user"
        self.paging_enabled = self.scenario.paging_enabled
        self.echo = self.scenario.echo
        self.encoding = self.scenario.encoding
        self.ansi_noise = self.scenario.ansi_noise
        self.term_width = 80
        self.term_height = 24
        self.buffer = bytearray()
        self.pending_lines: list[str] = []
        self.awaiting_more = False
        self.closed = False
        self._page_prefix = ""
        self._at_line_start = True

    # ---------------- 输出 ----------------
    @property
    def prompt(self) -> str:
        template = self.scenario.prompts.get(self.mode) or self.scenario.prompts.get(
            "user", "<{hostname}>"
        )
        return template.format(hostname=self.scenario.hostname)

    def write(self, chan: Any, text: str) -> None:
        payload = text.encode(self.encoding, errors="replace")
        if self.ansi_noise:
            # 真实设备会用光标控制序列重绘提示符，客户端必须能剥掉它们
            payload = b"\x1b[42D" + payload
        try:
            chan.write(payload)
        except (TypeError, ValueError):  # pragma: no cover - 通道若被配成文本模式
            chan.write(payload.decode(self.encoding, errors="replace"))
        self._at_line_start = text.endswith("\n") or text.endswith("\r")

    def write_prompt(self, chan: Any) -> None:
        if not self._at_line_start:
            self.write(chan, "\r\n")
        self.write(chan, self.prompt)

    def on_connect(self, chan: Any) -> None:
        if self.scenario.banner:
            self.write(chan, self.scenario.banner.rstrip("\n") + "\r\n")
        self.write_prompt(chan)

    # ---------------- 输入 ----------------
    def feed(self, data: bytes, chan: Any) -> None:
        if self.closed or not data:
            return

        if self.awaiting_more:
            index = next(
                (i for i, byte in enumerate(data) if byte in (0x20, 0x0D, 0x0A)),
                None,
            )
            if index is None:
                return  # 不是"继续"键，忽略
            self.awaiting_more = False
            self.emit_page(chan)
            data = data[:index] + data[index + 1:]

        self.buffer.extend(data)
        while not self.closed:
            match = re.search(rb"[\r\n]", self.buffer)
            if match is None:
                break
            separator = bytes(self.buffer[match.start():match.end()])
            line = bytes(self.buffer[: match.start()])
            del self.buffer[: match.end()]
            if separator == b"\r" and self.buffer[:1] == b"\n":
                del self.buffer[:1]
            self.handle_line(line.decode(self.encoding, errors="replace"), chan)

    def handle_line(self, line: str, chan: Any) -> None:
        command = line.strip()
        rule = self.scenario.match(command)

        echo_default = self.scenario.echo
        echo = echo_default if rule is None or rule.echo is _ECHO_OFF else bool(rule.echo)
        if echo:
            self.write(chan, line + "\r\n")

        if rule is None:
            self.emit_output(chan, self.scenario.default_output)
            return

        if rule.behavior == "hang":
            # 设备收到命令后不再吐任何数据：用于验证空闲探活与超时
            return

        for key, value in rule.state.items():
            setattr(self, key, value)

        text = self._output_text(rule)

        if rule.behavior == "close_mid_output":
            head = "\r\n".join(text.split("\n")[:5])
            self.write(chan, head + "\r\n")
            self.closed = True
            chan.close()
            return

        self.emit_output(chan, text, prompt=rule.prompt)

    def _output_text(self, rule: Rule) -> str:
        if rule.output is not None:
            return rule.output
        if rule.output_file:
            path = self.fixture_root / rule.output_file
            return path.read_text(encoding="utf-8")
        return ""

    # ---------------- 分页 ----------------
    def emit_output(self, chan: Any, text: str, *, prompt: bool = True) -> None:
        lines = [line for line in text.replace("\r\n", "\n").rstrip("\n").split("\n")] if text else []
        if not self.paging_enabled or len(lines) <= self.scenario.page_lines:
            if lines:
                self.write(chan, "\r\n".join(lines) + "\r\n")
            if prompt:
                self.write_prompt(chan)
            return
        self.pending_lines = lines
        self._page_prefix = ""
        self.emit_page(chan, prompt=prompt)

    def emit_page(self, chan: Any, *, prompt: bool = True) -> None:
        page = self.pending_lines[: self.scenario.page_lines]
        self.pending_lines = self.pending_lines[self.scenario.page_lines:]
        body = self._page_prefix + "\r\n".join(page)
        if self.scenario.more_style == "own_line":
            self.write(chan, body + "\r\n" + self.scenario.more_token)
        else:
            self.write(chan, body + self.scenario.more_token)
        self._page_prefix = "\r\n"

        if self.pending_lines:
            self.awaiting_more = True
        elif prompt:
            self.write_prompt(chan)


# --------------------------------------------------------------------------
# SSH 服务端
# --------------------------------------------------------------------------
if asyncssh is not None:

    class _MockSSHServer(asyncssh.SSHServer):
        def __init__(self, scenario: Scenario, fixture_root: Path) -> None:
            self.scenario = scenario
            self.fixture_root = fixture_root

        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            if self.scenario.reject_auth:
                return False
            return username == self.scenario.username and password == self.scenario.password

        def public_key_auth_supported(self) -> bool:
            return False

        def kbdint_auth_supported(self) -> bool:
            return False

        def session_requested(self) -> Any:
            return _MockSession(self.scenario, self.fixture_root)

    class _MockSession(asyncssh.SSHServerSession):
        def __init__(self, scenario: Scenario, fixture_root: Path) -> None:
            super().__init__()
            self.device = MockDevice(scenario, fixture_root)
            self._mock_chan: Any = None

        def connection_made(self, chan: Any) -> None:
            self._mock_chan = chan
            self.device.on_connect(chan)

        def pty_requested(
            self, term_type: str, term_size: Any, term_modes: Any
        ) -> bool:
            if term_size:
                self.device.term_width = int(term_size[0])
                self.device.term_height = int(term_size[1])
            return True

        def shell_requested(self) -> bool:
            return True

        def exec_requested(self, command: str) -> bool:
            return False

        def data_received(self, data: Any, datatype: Any) -> None:
            if isinstance(data, str):
                data = data.encode(self.device.encoding, errors="replace")
            self.device.feed(bytes(data), self._mock_chan)

        def eof_received(self) -> bool:
            return False

        def connection_lost(self, exc: Any) -> None:
            self.device.closed = True


class MockDeviceServer:
    """把一台假设备暴露到 127.0.0.1:<随机端口>。"""

    def __init__(
        self,
        scenario: Scenario,
        *,
        fixture_root: Path | str = FIXTURE_ROOT,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if asyncssh is None:  # pragma: no cover
            raise RuntimeError("未安装 asyncssh，无法启动 mock 设备")
        self.scenario = scenario
        self.fixture_root = Path(fixture_root)
        self.host = host
        self.port = port
        self._listener: Any = None
        self._host_key: Any = None

    async def start(self) -> int:
        self._host_key = asyncssh.generate_private_key("ssh-ed25519")
        self._listener = await asyncssh.listen(
            self.host,
            self.port,
            server_factory=lambda: _MockSSHServer(self.scenario, self.fixture_root),
            server_host_keys=[self._host_key],
            encoding=None,
        )
        self.port = self._listener.get_port()
        return self.port

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
            try:
                await self._listener.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._listener = None

    async def __aenter__(self) -> "MockDeviceServer":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()


class start_mock:
    """异步上下文管理器：``async with start_mock("huawei-vrp8") as server: ...``"""

    def __init__(
        self,
        scenario_name: str,
        *,
        scenarios_file: Path | str | None = None,
        fixture_root: Path | str = FIXTURE_ROOT,
        **overrides: Any,
    ) -> None:
        self.scenarios = load_scenarios(scenarios_file)
        if scenario_name not in self.scenarios:
            raise KeyError(
                f"未找到剧本 {scenario_name!r}；可用：{', '.join(sorted(self.scenarios))}"
            )
        scenario = self.scenarios[scenario_name]
        for key, value in overrides.items():
            setattr(scenario, key, value)
        self._server = MockDeviceServer(scenario, fixture_root=fixture_root)

    async def __aenter__(self) -> MockDeviceServer:
        await self._server.start()
        return self._server

    async def __aexit__(self, *exc_info: object) -> None:
        await self._server.stop()
