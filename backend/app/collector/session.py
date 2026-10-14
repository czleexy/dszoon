"""设备会话层：把"一个 TCP 连接 + 终端交互"抽象成可复用的对象。

设计要点
--------
1. **提示符驱动**：所有读取都以"读到提示符"为结束条件，而不是固定 sleep。
2. **双层超时**：命令总超时（设备真的慢）+ 空闲超时（会话疑似卡死）。
   空闲时可选补发一个空格探活，这是救回大量"假死"会话的关键一招。
3. **原始字节留档**：``raw_transcript`` 保存收发原始字节，排障时可以直接看
   设备到底吐了什么，而不是猜。
4. **两套实现**：asyncssh 为主（高并发），paramiko 兜底（老设备算法协商）。
   两者共享同一套读取状态机，行为一致。
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Pattern

from . import textutil
from .errors import (
    AuthError,
    CollectorError,
    CommandTimeout,
    ConfirmationLoop,
    ConnectError,
    IdleTimeoutError,
    SessionClosed,
    UnsupportedProtocol,
)

try:  # pragma: no cover - 依赖缺失时给出明确报错
    import asyncssh
except ImportError:  # pragma: no cover
    asyncssh = None  # type: ignore[assignment]

try:  # pragma: no cover
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None  # type: ignore[assignment]

__all__ = [
    "AsyncSSHSession",
    "CONFIRM_PROMPT_PATTERN",
    "ConfirmRule",
    "InteractiveSession",
    "ParamikoSession",
    "PASSWORD_PROMPT_PATTERN",
    "SessionConfig",
    "create_session",
]

# --------------------------------------------------------------------------
# 老旧设备算法白名单（见开发计划 R2）
# --------------------------------------------------------------------------
LEGACY_KEX_ALGS = (
    "curve25519-sha256",
    "diffie-hellman-group-exchange-sha256",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group1-sha1",
)
LEGACY_ENCRYPTION_ALGS = (
    "aes128-ctr",
    "aes192-ctr",
    "aes256-ctr",
    "aes128-cbc",
    "aes192-cbc",
    "aes256-cbc",
    "3des-cbc",
)
LEGACY_HOST_KEY_ALGS = (
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
    "ssh-dss",
)
LEGACY_MAC_ALGS = (
    "hmac-sha2-256",
    "hmac-sha1",
    "hmac-sha1-96",
    "hmac-md5",
)

#: 设备向我们要 enable/super 密码时的提示
PASSWORD_PROMPT_PATTERN = r"(?i)^\s*(?:password|passwd|enter\s+password)\s*:?\s*$"

#: 交互确认提示（正则, 自动回复）。形如 "[Y/N]:" / "(y/n)" / "[yes/no]:"
CONFIRM_PROMPT_PATTERN = (
    r"[\[\(]\s*(?:[Yy](?:es)?|[Nn]o?)\s*/\s*(?:[Yy](?:es)?|[Nn]o?)\s*[\]\)]\s*:?\s*$"
)

#: (确认提示正则, 回复内容)
ConfirmRule = tuple[Pattern[str], str]

#: 登录阶段出现即判定需要人工介入（改密流程会打断自动化，且改密本身是变更操作）
_PASSWORD_EXPIRED_RE = re.compile(
    r"(?i)(?:password\s+(?:has\s+)?expired"
    r"|password\s+needs\s+to\s+be\s+changed"
    r"|must\s+change\s+(?:the\s+)?password"
    r"|change\s+now\s*\?"  # The password needs to be changed. Change now? [Y/N]
    r"|首次登录|修改密码|密码已过期)"
)

_TAIL_WINDOW = 8192


@dataclass(slots=True)
class SessionConfig:
    """一次会话的全部参数。"""

    host: str
    port: int = 22
    protocol: str = "ssh"  # ssh | telnet
    username: str = ""
    password: str | None = None
    private_key: str | None = None
    private_key_passphrase: str | None = None
    enable_password: str | None = None

    connect_timeout: float = 15.0
    #: 单条命令的总超时（大配置在慢设备上可能 60s+）
    command_timeout: float = 180.0
    #: 无数据多久判定为空闲
    idle_timeout: float = 25.0
    #: 空闲时是否补发一个空格探活（能救回大量"假死"会话）
    idle_probe: bool = True

    legacy_algorithms: bool = False
    use_paramiko: bool = False
    host_key_check: bool = False
    known_hosts: str | None = None

    term_type: str = "vt100"
    term_width: int = 512
    term_height: int = 200

    #: 已知设备编码（华三/锐捷常见 GBK）。None = 自动探测
    encoding: str | None = None

    #: 预留：经跳板机连接（P1 实现）
    proxy_jump: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    def sanitized(self) -> dict[str, Any]:
        """用于日志/审计的脱敏副本。"""
        import dataclasses

        data = dataclasses.asdict(self)
        for key in ("password", "enable_password", "private_key_passphrase"):
            if data.get(key):
                data[key] = "******"
        return data


class InteractiveSession(ABC):
    """交互式终端会话。子类只需实现 open/close/收发原语。"""

    def __init__(
        self,
        config: SessionConfig,
        prompt_re: Pattern[str],
        *,
        encoding: str | None = None,
    ) -> None:
        self.config = config
        self.prompt_re = prompt_re
        self.encoding = encoding or config.encoding
        #: 提示符正则是否用的是"字面设备名"（严格形态）。
        #: 严格形态下才允许识别 `return<HUAWEI>` 这类"提示符与正文粘连"的形态——
        #: 泛化正则会把正文里的 `description [UPLINK-TO-DC-SPINE]` 当成提示符，
        #: 从而在配置中途"以为命令跑完了"。默认 False（宁可保守）。
        self.strict_prompt = False
        #: 需要自动回答的交互确认（如 "[Y/N]:"）。**只应由驱动声明**，
        #: 而且只在我们主动发送的、语义明确的命令上生效——配置下发的审批流
        #: 绝不能靠这个机制自动放行。
        self.confirm_rules: list[ConfirmRule] = []
        #: 单条命令最多允许自动确认几次（防"设备反复追问"死循环）
        self.max_confirmations = 5
        #: 本次会话累计自动确认次数（供探针/日志展示）
        self.confirmation_count = 0
        self._raw_transcript: list[tuple[str, bytes]] = []

    # ---------------- 子类实现 ----------------
    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def _write_raw(self, payload: bytes) -> None: ...

    @abstractmethod
    async def _fetch_once(self, timeout: float) -> bytes | None:
        """读一段数据。

        :return: bytes（有数据）、``b""``（对端关闭）、``None``（本次超时无数据）
        """

    # ---------------- 上下文管理 ----------------
    async def __aenter__(self) -> "InteractiveSession":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ---------------- 收发 ----------------
    async def _write(self, data: str, *, secret: bool = False) -> None:
        payload = data.encode(self.encoding or "utf-8")
        self._raw_transcript.append((">>>", b"******" if secret else payload))
        await self._write_raw(payload)

    async def begin(self) -> None:
        """登录后同步到 shell 提示符。

        有些设备连上就给提示符，有些必须敲一次回车才吐 banner + 提示符，
        这里两段式处理，避免为了兼容后者而无条件多发一次回车。
        """
        try:
            await self._read_until_prompt(
                self.prompt_re, timeout=3.0, login_phase=True
            )
            return
        except CommandTimeout:
            pass
        await self._write("\n")
        await self._read_until_prompt(
            self.prompt_re,
            timeout=self.config.command_timeout,
            login_phase=True,
        )

    async def send_command(
        self,
        command: str,
        *,
        timeout: float | None = None,
        prompt_re: Pattern[str] | None = None,
        secret: bool = False,
    ) -> str:
        """执行一条命令并返回清洗后的正文。

        :param prompt_re: 结束条件提示符，默认用主提示符。提权时传"密码提示符"。
        :param secret: 密码类输入，不写入任何日志/转储。
        """
        expect = prompt_re or self.prompt_re
        effective_timeout = self.config.command_timeout if timeout is None else timeout
        await self._write(command, secret=secret)
        await self._write("\n")
        raw = await self._read_until_prompt(expect, timeout=effective_timeout)
        text = textutil.clean_command_output(
            raw,
            "" if secret else command,
            expect,
            preferred_encoding=self.encoding,
            allow_glued_prompt=self.strict_prompt,
        )
        self._raw_transcript.append(
            ("###", f"$ {command if not secret else '******'}".encode("utf-8"))
        )
        return text

    async def resync(self, *, timeout: float = 15.0) -> bool:
        """把会话从"卡在某个交互提示上"的状态拉回主提示符。

        典型场景：设备没配置 enable 密码，``enable`` 直接成功而没有 ``Password:``
        提示，此时等密码提示的读取会超时——会话其实没坏，回车同步一下即可继续。
        """
        try:
            await self._write("\n")
            await self._read_until_prompt(
                self.prompt_re, timeout=timeout, idle_timeout=max(5.0, timeout / 2)
            )
            return True
        except CollectorError:
            return False

    # ---------------- 读取状态机 ----------------
    async def _read_until_prompt(
        self,
        prompt_re: Pattern[str],
        *,
        timeout: float,
        idle_timeout: float | None = None,
        login_phase: bool = False,
    ) -> bytes:
        loop = asyncio.get_running_loop()
        idle = self.config.idle_timeout if idle_timeout is None else idle_timeout
        buf = bytearray()
        deadline = loop.time() + timeout
        last_data = loop.time()
        probed = False
        #: 上次为翻页发送空格时的缓冲区长度，避免对停止输出的设备连续灌空格
        last_more_at = -1
        #: 本命令已自动确认次数 / 上次确认时的缓冲区长度
        confirmations = 0
        confirm_answered_at = -1

        while True:
            now = loop.time()
            if now >= deadline:
                raise CommandTimeout(
                    f"等待提示符超时（{timeout:.0f}s，已收到 {len(buf)} 字节）",
                    detail=self._tail_hint(buf),
                )
            if now - last_data > idle:
                if self.config.idle_probe and not probed:
                    probed = True
                    # 发空格比发回车安全：回车会被当成"空命令"而插入空行
                    await self._write(" ")
                    last_data = loop.time()
                else:
                    raise IdleTimeoutError(
                        f"会话空闲 {idle:.0f}s 无任何数据，疑似卡死",
                        detail=self._tail_hint(buf),
                    )

            chunk = await self._fetch_once(min(1.0, max(0.05, deadline - now)))
            if chunk is None:
                continue
            if chunk == b"":
                raise SessionClosed("对端关闭了连接", detail=self._tail_hint(buf))

            buf.extend(chunk)
            self._raw_transcript.append(("<<<", chunk))
            last_data = loop.time()
            probed = False

            # ★ 匹配前必须剥掉 ANSI：真实设备（尤其是带 ansi_noise 行为的设备）
            #   会在重绘提示符时先发 \x1b[42D 之类的光标控制序列，那一行就成了
            #   "\x1b[42D<H3C-CORE-02>"，带 ^ 锚点的提示符正则永远匹配不到，
            #   结果就是"设备明明回了提示符，客户端却一直等到超时"。
            tail = textutil.clean_terminal(
                textutil.decode_for_matching(bytes(buf[-_TAIL_WINDOW:]))
            )

            # 顺序很重要：先处理分页，再处理交互确认，最后判断提示符
            if textutil.has_more_token(tail):
                if len(buf) != last_more_at:
                    last_more_at = len(buf)
                    await self._write(" ")
                continue

            # 交互确认（"Continue? [Y/N]:"）必须自动回答。
            # 这是真机踩出来的：华为 VRP5 执行 `screen-width 512` 会弹
            #   Warning: This command will change the default screen width. Continue? [Y/N]:
            # 而空闲探活发的是空格——设备只认 Y/N，于是白等到超时，
            # 且报错只是含糊的"会话空闲 N 秒无数据"，完全指不到真正原因。
            confirm_reply = self._match_confirmation(tail, len(buf), confirm_answered_at)
            if confirm_reply is not None:
                confirm_answered_at = len(buf)
                confirmations += 1
                if confirmations > self.max_confirmations:
                    raise ConfirmationLoop(
                        f"设备连续 {confirmations} 次要求交互确认，疑似陷入循环",
                        detail=self._tail_hint(buf),
                    )
                await self._write(confirm_reply + "\n")
                self.confirmation_count += 1
                continue

            if login_phase:
                m = _PASSWORD_EXPIRED_RE.search(tail)
                if m:
                    raise AuthError(
                        "设备要求修改密码或密码已过期，需人工处理后重试",
                        detail=m.group(0),
                    )
            if self._prompt_at_end(tail, prompt_re):
                return bytes(buf)

    def _prompt_at_end(self, tail: str, prompt_re: Pattern[str]) -> bool:
        """缓冲区是否已经停在提示符上（= 这条命令跑完了）。

        常规形态：提示符自己占一整行，或后面只跟少量空白/换行。
        粘连形态：设备没在提示符前换行（``return<HUAWEI>``）——此时行首锚点匹配
        不到任何东西，必须补一个换行再匹配。**只在严格提示符下启用**，原因见
        :attr:`strict_prompt`。
        """
        last_match = None
        for m in prompt_re.finditer(tail):
            last_match = m
        if last_match is not None and len(tail) - last_match.end() <= textutil.PROMPT_TAIL_ALLOWANCE:
            return True

        if not self.strict_prompt:
            return False
        line = tail.split("\n")[-1]
        if not line.strip():
            return False
        return (
            textutil.trailing_prompt_start(line, prompt_re, allow_glued=True) is not None
        )

    def _match_confirmation(
        self, tail: str, buffer_size: int, answered_at: int
    ) -> str | None:
        """缓冲区是否停在需要自动确认的提示上；是则返回要回复的内容。

        :param answered_at: 上次回答确认时的缓冲区长度——设备没吐新数据时
            不能反复灌同一个答复（否则会把"设备真的卡住"伪装成"一直在确认"）。
        """
        if not self.confirm_rules or buffer_size == answered_at:
            return None
        for pattern, reply in self.confirm_rules:
            last_match = None
            for m in pattern.finditer(tail):
                last_match = m
            if (
                last_match is not None
                and len(tail) - last_match.end() <= textutil.PROMPT_TAIL_ALLOWANCE
            ):
                return reply
        return None

    def _tail_hint(self, buf: bytes | bytearray, size: int = 240) -> str:
        if not buf:
            return "<无任何输出>"
        tail = textutil.decode_for_matching(bytes(buf[-size:]))
        return repr(tail)

    # ---------------- 排障留档 ----------------
    @property
    def raw_transcript(self) -> list[tuple[str, bytes]]:
        return self._raw_transcript

    def transcript_text(self) -> str:
        """可读会话日志（已自动脱敏密码）。"""
        parts: list[str] = []
        for marker, payload in self._raw_transcript:
            if marker == ">>>":
                parts.append(f"\n$ {textutil.decode_for_matching(payload)}\n")
            elif marker == "###":
                continue
            else:
                parts.append(textutil.decode_for_matching(payload))
        return "".join(parts)

    def last_received_line(self, *, window: int = 4096) -> str | None:
        """返回最近接收数据里的最后一个非空行。

        用途：设备台账未登记设备名时，从提示符反查真实 hostname —— 有了真实设备名，
        引擎才能把提示符正则收紧成字面量，避免配置正文误命中提示符导致"采到一半
        就以为结束"。
        """
        collected = bytearray()
        for marker, payload in reversed(self._raw_transcript):
            if marker != "<<<":
                continue
            collected[:0] = payload
            if len(collected) >= window:
                break
        if not collected:
            return None
        # 同样要先剥 ANSI：否则返回的是 "\x1b[42D<H3C-CORE-02>"，
        # match_prompt 的 ^ 锚点匹配不到，设备名就反查不出来。
        text = textutil.clean_terminal(
            textutil.decode_for_matching(bytes(collected[-window:]))
        )
        for line in reversed(text.split("\n")):
            stripped = line.strip()
            if stripped:
                return stripped
        return None


class AsyncSSHSession(InteractiveSession):
    """asyncssh 实现（默认）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._conn: Any = None
        self._proc: Any = None

    async def open(self) -> None:
        if asyncssh is None:
            raise UnsupportedProtocol("未安装 asyncssh，无法使用 SSH 会话")
        cfg = self.config
        if cfg.protocol not in ("ssh", ""):
            raise UnsupportedProtocol(f"asyncssh 会话不支持协议 {cfg.protocol!r}")

        known_hosts: Any = None
        if cfg.host_key_check:
            known_hosts = cfg.known_hosts or os.path.expanduser("~/.ssh/known_hosts")

        kwargs: dict[str, Any] = {
            "host": cfg.host,
            "port": cfg.port,
            "username": cfg.username,
            "known_hosts": known_hosts,
            "connect_timeout": cfg.connect_timeout,
            "login_timeout": cfg.connect_timeout,
        }
        if cfg.password:
            kwargs["password"] = cfg.password
        if cfg.private_key:
            kwargs["client_keys"] = [cfg.private_key]
            if cfg.private_key_passphrase:
                kwargs["passphrase"] = cfg.private_key_passphrase
        if cfg.legacy_algorithms:
            kwargs.update(
                kex_algs=LEGACY_KEX_ALGS,
                encryption_algs=LEGACY_ENCRYPTION_ALGS,
                server_host_key_algs=LEGACY_HOST_KEY_ALGS,
                mac_algs=LEGACY_MAC_ALGS,
            )

        try:
            self._conn = await asyncssh.connect(**kwargs)
        except asyncssh.PermissionDenied as exc:  # type: ignore[union-attr]
            raise AuthError("SSH 认证失败（用户名/密码/密钥被拒绝）", detail=str(exc)) from exc
        except asyncssh.KeyImportError as exc:  # type: ignore[union-attr]
            raise AuthError("私钥无法加载", detail=str(exc)) from exc
        except asyncssh.HostKeyNotVerifiable as exc:  # type: ignore[union-attr]
            raise ConnectError("主机密钥校验失败", detail=str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise ConnectError(
                f"连接 {cfg.host}:{cfg.port} 超时（{cfg.connect_timeout:.0f}s）"
            ) from exc
        except (asyncssh.Error, OSError) as exc:  # type: ignore[union-attr]
            raise ConnectError(
                f"无法连接 {cfg.host}:{cfg.port}", detail=f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            self._proc = await self._conn.create_process(
                term_type=cfg.term_type,
                term_size=(cfg.term_width, cfg.term_height),
                encoding=None,  # 自己解码，便于编码回退
            )
        except (asyncssh.Error, OSError) as exc:  # type: ignore[union-attr]
            raise ConnectError("无法建立交互式会话通道", detail=str(exc)) from exc

    async def _write_raw(self, payload: bytes) -> None:
        if self._proc is None:
            raise SessionClosed("会话尚未建立")
        try:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        except (BrokenPipeError, OSError, asyncssh.Error) as exc:  # type: ignore[union-attr]
            raise SessionClosed("写数据失败，连接已断开", detail=str(exc)) from exc

    async def _fetch_once(self, timeout: float) -> bytes | None:
        if self._proc is None:
            raise SessionClosed("会话尚未建立")
        try:
            return await asyncio.wait_for(self._proc.stdout.read(65536), timeout)
        except asyncio.TimeoutError:
            return None
        except (asyncssh.Error, OSError) as exc:  # type: ignore[union-attr]
            raise SessionClosed("读数据失败，连接已断开", detail=str(exc)) from exc

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.close()
            except Exception:  # pragma: no cover - 关闭失败不影响主流程
                pass
            self._proc = None
        if self._conn is not None:
            try:
                self._conn.close()
                await self._conn.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._conn = None


class ParamikoSession(InteractiveSession):
    """paramiko 兜底实现。

    用于协商不了现代算法的老旧设备（见开发计划 R2）。所有 paramiko 调用都串行
    跑在同一个单线程 executor 上——paramiko 的对象不是线程安全的，这样最稳。
    """

    #: 单次 recv 的阻塞上限（外层 deadline 负责总超时）
    _RECV_TIMEOUT = 0.5

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: Any = None
        self._chan: Any = None
        self._executor: ThreadPoolExecutor | None = None

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        assert self._executor is not None
        return await loop.run_in_executor(self._executor, func, *args)

    async def open(self) -> None:
        if paramiko is None:
            raise UnsupportedProtocol("未安装 paramiko，无法使用兜底会话")
        if self.config.protocol not in ("ssh", ""):
            raise UnsupportedProtocol(f"paramiko 会话不支持协议 {self.config.protocol!r}")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paramiko")
        await self._run(self._open_blocking)

    def _open_blocking(self) -> None:
        cfg = self.config
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if not cfg.host_key_check else paramiko.RejectPolicy()
        )
        kwargs: dict[str, Any] = {
            "hostname": cfg.host,
            "port": cfg.port,
            "username": cfg.username,
            "timeout": cfg.connect_timeout,
            "banner_timeout": cfg.connect_timeout,
            "auth_timeout": cfg.connect_timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if cfg.password:
            kwargs["password"] = cfg.password
        if cfg.private_key:
            kwargs["key_filename"] = cfg.private_key
            if cfg.private_key_passphrase:
                kwargs["passphrase"] = cfg.private_key_passphrase

        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            raise AuthError("SSH 认证失败（用户名/密码/密钥被拒绝）", detail=str(exc)) from exc
        except paramiko.BadHostKeyException as exc:
            raise ConnectError("主机密钥校验失败", detail=str(exc)) from exc
        except (paramiko.SSHException, OSError, socket.error) as exc:
            raise ConnectError(
                f"无法连接 {cfg.host}:{cfg.port}", detail=f"{type(exc).__name__}: {exc}"
            ) from exc

        chan = client.invoke_shell(
            term=cfg.term_type, width=cfg.term_width, height=cfg.term_height
        )
        chan.settimeout(self._RECV_TIMEOUT)
        self._client = client
        self._chan = chan

    async def _write_raw(self, payload: bytes) -> None:
        if self._chan is None:
            raise SessionClosed("会话尚未建立")
        try:
            await self._run(self._chan.sendall, payload)
        except (OSError, paramiko.SSHException) as exc:  # type: ignore[union-attr]
            raise SessionClosed("写数据失败，连接已断开", detail=str(exc)) from exc

    async def _fetch_once(self, timeout: float) -> bytes | None:
        if self._chan is None:
            raise SessionClosed("会话尚未建立")

        def _recv() -> bytes | None:
            try:
                return self._chan.recv(65536)
            except socket.timeout:
                return None

        try:
            return await self._run(_recv)
        except (OSError, paramiko.SSHException) as exc:  # type: ignore[union-attr]
            raise SessionClosed("读数据失败，连接已断开", detail=str(exc)) from exc

    async def close(self) -> None:
        if self._chan is not None:
            try:
                await self._run(self._chan.close)
            except Exception:  # pragma: no cover
                pass
            self._chan = None
        if self._client is not None:
            try:
                await self._run(self._client.close)
            except Exception:  # pragma: no cover
                pass
            self._client = None
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None


SessionFactory = Callable[[SessionConfig, Pattern[str]], InteractiveSession]


def create_session(
    config: SessionConfig,
    prompt_re: Pattern[str],
    *,
    force_paramiko: bool = False,
    encoding: str | None = None,
) -> InteractiveSession:
    """按配置选择会话实现。

    Telnet 尚未实现（计划 P1），这里显式报错而不是静默降级——静默降级会让人
    以为"备份成功"，实际上什么都没采到。
    """
    if config.protocol == "telnet":
        raise UnsupportedProtocol(
            "Telnet 会话尚未实现（计划 P1）。请为该设备改用 SSH，"
            "或在驱动中显式声明 telnet 支持。"
        )
    if force_paramiko or config.use_paramiko:
        return ParamikoSession(config, prompt_re, encoding=encoding)
    return AsyncSSHSession(config, prompt_re, encoding=encoding)
