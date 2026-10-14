"""采集引擎：单台设备的完整采集流程 + 批量调度。

单台流程（顺序有讲究）::

    建立会话 -> begin(同步到提示符) -> 提权 -> 关分页/关折行
    -> 取版本(抽 facts) -> 用真实设备名收紧提示符正则
    -> 取 running-config -> 完整性校验 -> 归一化 -> 比对上一版 -> 归档

关键设计：

* **提示符收紧**：登录后先用泛化提示符，拿到真实设备名后换成字面量正则。
  否则配置正文里某一行碰巧符合提示符形状，就会导致"采到一半就以为结束"。
* **预处理命令失败不算致命，但必须记录**：设备拒绝 ``screen-width`` 时配置仍有
  可能被折行，必须让人看见这条警告，而不是静默成功。
* **同设备串行**：设备 VTY 数量有限（通常 5~16），同一台设备绝不并发采集。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Pattern, Sequence

from . import integrity, textutil
from .drivers.base import VendorDriver
from .errors import (
    AuthError,
    CollectorError,
    CommandTimeout,
    ConnectError,
)
from .integrity import Issue
from .registry import DriverRegistry, get_registry
from .session import InteractiveSession, SessionConfig, create_session
from .storage import ArtifactStore, SnapshotRecord, StoredSnapshot

__all__ = [
    "BackupOutcome",
    "BackupStatus",
    "DeviceTarget",
    "ProbeResult",
    "backup_device",
    "backup_many",
    "probe_device",
]

#: 连接类失败的退避重试间隔（秒）
RETRY_BACKOFF: tuple[float, ...] = (5.0, 15.0)

#: 会话控制命令（关分页/关折行/提权）的超时——这类命令应当秒回
CONTROL_CMD_TIMEOUT = 20.0

#: TCP 可达性预检超时
PRECHECK_TIMEOUT = 3.0


class BackupStatus(str, Enum):
    SUCCESS = "success"                  # 采到内容且校验通过，且与上一版不同
    UNCHANGED = "unchanged"              # 采到内容且校验通过，与上一版一致
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    SESSION_CLOSED = "session_closed"
    INTEGRITY_ERROR = "integrity_error"  # 内容不可信（截断/折行/命令报错）
    UNSUPPORTED = "unsupported"
    ERROR = "error"

    @property
    def ok(self) -> bool:
        return self in (BackupStatus.SUCCESS, BackupStatus.UNCHANGED)


_STATUS_BY_CODE: dict[str, BackupStatus] = {
    "unreachable": BackupStatus.UNREACHABLE,
    "auth_failed": BackupStatus.AUTH_FAILED,
    "timeout": BackupStatus.TIMEOUT,
    "session_closed": BackupStatus.SESSION_CLOSED,
    "integrity_error": BackupStatus.INTEGRITY_ERROR,
    "unsupported_protocol": BackupStatus.UNSUPPORTED,
    "unknown_vendor": BackupStatus.UNSUPPORTED,
    "deploy_not_supported": BackupStatus.UNSUPPORTED,
}


@dataclass(slots=True)
class DeviceTarget:
    """一台待采集设备（设备台账在 P1 由数据库承载，这里先做纯数据对象）。"""

    host: str
    vendor: str
    #: 设备台账主键（P1 起由 Web 层填充，用于把采集结果回写到正确的设备上）
    id: int | None = None
    port: int = 22
    protocol: str = "ssh"
    username: str = ""
    password: str | None = None
    enable_password: str | None = None
    private_key: str | None = None

    #: 台账里的设备名；已知时用于生成严格的提示符正则
    hostname: str | None = None
    name: str | None = None
    site: str | None = None
    group: str | None = None
    labels: tuple[str, ...] = ()

    legacy_algorithms: bool = False
    use_paramiko: bool = False
    encoding: str | None = None

    connect_timeout: float = 15.0
    command_timeout: float = 180.0
    idle_timeout: float = 25.0

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """用于"同设备串行"的去重键。"""
        return f"{self.host}:{self.port}"

    @property
    def display(self) -> str:
        return self.hostname or self.name or self.host

    def session_config(self) -> SessionConfig:
        return SessionConfig(
            host=self.host,
            port=self.port,
            protocol=self.protocol,
            username=self.username,
            password=self.password,
            enable_password=self.enable_password,
            private_key=self.private_key,
            connect_timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            idle_timeout=self.idle_timeout,
            legacy_algorithms=self.legacy_algorithms,
            use_paramiko=self.use_paramiko,
            encoding=self.encoding,
        )


@dataclass(slots=True)
class ProbeResult:
    """"连接测试"结果（供 P1 的 Web 界面按钮使用）。"""

    target: DeviceTarget
    ok: bool
    status: BackupStatus
    hostname: str | None = None
    facts: dict[str, str] = field(default_factory=dict)
    version_output: str = ""
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0


@dataclass(slots=True)
class BackupOutcome:
    """一次采集的完整结果。"""

    target: DeviceTarget
    vendor: str
    status: BackupStatus
    hostname: str | None = None
    running_config: str | None = None
    normalized_config: str | None = None
    startup_config: str | None = None
    facts: dict[str, str] = field(default_factory=dict)
    sha256: str | None = None
    normalized_sha256: str | None = None
    size_bytes: int = 0
    line_count: int = 0
    duration_ms: int = 0
    attempts: int = 1
    changed: bool | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_detail: str | None = None
    issues: list[Issue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stored: StoredSnapshot | None = None
    transcript: str | None = None

    @property
    def ok(self) -> bool:
        return self.status.ok

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_error]

    def normalized(self) -> str | None:
        return self.normalized_config

    def redacted_config(self, driver: VendorDriver) -> str | None:
        if self.running_config is None:
            return None
        return driver.redact(self.running_config)

    def summary(self) -> str:
        if self.ok:
            if self.changed is None:
                state = "首次采集"
            elif self.changed:
                state = "有变更"
            else:
                state = "无变更"
            return f"{self.status.value}/{state}：{self.line_count} 行 / {self.size_bytes} 字节"
        return f"{self.status.value}: {self.error_message or '-'}"


SessionFactory = Callable[[SessionConfig, Pattern[str]], InteractiveSession]


# --------------------------------------------------------------------------
# 单台设备
# --------------------------------------------------------------------------
async def backup_device(
    target: DeviceTarget,
    *,
    driver: VendorDriver | None = None,
    store: ArtifactStore | None = None,
    registry: DriverRegistry | None = None,
    retries: int = 2,
    precheck: bool = False,
    session_factory: SessionFactory | None = None,
) -> BackupOutcome:
    """采集一台设备的配置。

    不抛异常：所有失败都体现为 ``outcome.status``。批量任务因此不会被单台设备
    的异常打断——这是"一台设备坏掉不能影响整体巡检"的基本要求。
    """
    started = time.perf_counter()
    registry = registry or get_registry()

    try:
        active_driver = driver or registry.get(target.vendor)
    except CollectorError as exc:
        return _failure(target, exc, started, vendor=target.vendor)

    attempts = 0
    while True:
        attempts += 1
        try:
            if precheck:
                await _tcp_precheck(target)
            return await _collect(
                target,
                active_driver,
                store=store,
                session_factory=session_factory,
                attempts=attempts,
                started=started,
            )
        except CollectorError as exc:
            if exc.retryable and attempts <= retries:
                delay = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
                await asyncio.sleep(delay)
                continue
            return _failure(
                target, exc, started, vendor=active_driver.name, attempts=attempts
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让单台设备炸掉整批任务
            wrapped = CollectorError(
                f"未预期异常：{type(exc).__name__}", detail=str(exc)
            )
            return _failure(
                target, wrapped, started, vendor=active_driver.name, attempts=attempts
            )


async def probe_device(
    target: DeviceTarget,
    *,
    driver: VendorDriver | None = None,
    registry: DriverRegistry | None = None,
    session_factory: SessionFactory | None = None,
) -> ProbeResult:
    """只登录 + 取版本，用于 Web 界面的"连接测试"。"""
    started = time.perf_counter()
    registry = registry or get_registry()
    try:
        active_driver = driver or registry.get(target.vendor)
    except CollectorError as exc:
        return ProbeResult(
            target=target,
            ok=False,
            status=_status_for(exc),
            error_code=exc.code,
            error_message=exc.message,
            duration_ms=_ms(started),
        )

    session: InteractiveSession | None = None
    try:
        # ★ 必须接住 _open_ready_session 返回的 hostname：它是从提示符反查出来的
        #   真实设备名。丢掉它，"连接测试"就没法把设备名补进台账。
        session, hostname, warnings = await _open_ready_session(
            target, active_driver, session_factory=session_factory
        )
        version_output = ""
        if active_driver.version_cmd():
            version_output = await session.send_command(
                active_driver.version_cmd(), timeout=CONTROL_CMD_TIMEOUT * 3
            )
        facts = active_driver.extract_facts(version_output, "")
        if hostname:
            facts["hostname"] = hostname
        return ProbeResult(
            target=target,
            ok=True,
            status=BackupStatus.SUCCESS,
            hostname=hostname or facts.get("hostname") or target.hostname,
            facts=facts,
            version_output=version_output,
            warnings=warnings,
            duration_ms=_ms(started),
        )
    except CollectorError as exc:
        return ProbeResult(
            target=target,
            ok=False,
            status=_status_for(exc),
            error_code=exc.code,
            error_message=exc.message,
            duration_ms=_ms(started),
        )
    finally:
        if session is not None:
            await session.close()


async def _collect(
    target: DeviceTarget,
    driver: VendorDriver,
    *,
    store: ArtifactStore | None,
    session_factory: SessionFactory | None,
    attempts: int,
    started: float,
) -> BackupOutcome:
    warnings: list[str] = []
    session, hostname, open_warnings = await _open_ready_session(
        target, driver, session_factory=session_factory
    )
    warnings.extend(open_warnings)

    try:
        # 1) 版本信息：用于抽取型号/软件版本，也用于识别命令分支
        version_output = ""
        if driver.version_cmd():
            try:
                version_output = await session.send_command(
                    driver.version_cmd(), timeout=CONTROL_CMD_TIMEOUT * 3
                )
            except CollectorError as exc:
                warnings.append(f"取版本失败（不影响配置采集）：{exc}")

        # 2) 取当前配置——真正的目标
        running = await session.send_command(driver.running_config_cmd())

        # 3) 取下次启动配置（尽力而为：部分设备不支持或需要更高权限）
        startup: str | None = None
        startup_cmd = driver.startup_config_cmd()
        if startup_cmd:
            try:
                startup = await session.send_command(startup_cmd)
            except CollectorError as exc:
                warnings.append(f"取启动配置失败（不影响主配置归档）：{exc.message}")

        transcript = session.transcript_text()
    finally:
        await session.close()

    issues = integrity.check_config(running, driver.config_spec())
    # 采集只允许改会话状态：复查预处理命令有没有被写进配置
    issues.extend(integrity.check_setup_leakage(running, driver.session_setup_cmds()))
    # 非 error 级的发现（疑似折行、预处理命令写入配置）也要让人看见，
    # 否则它们只会安静地躺在 run 记录里
    warnings.extend(issue.message for issue in issues if not issue.is_error)
    normalized = driver.normalize(running)
    sha = textutil.sha256_text(running)
    normalized_sha = textutil.sha256_text(normalized)
    size_bytes = len(running.encode("utf-8"))
    lines = textutil.line_count(running)

    facts = driver.extract_facts(version_output, running)
    if hostname:
        facts["hostname"] = hostname

    status = BackupStatus.INTEGRITY_ERROR if integrity.has_errors(issues) else BackupStatus.SUCCESS

    device_key = hostname or target.hostname or target.host
    previous_sha = store.latest_sha(device_key) if store else None
    changed: bool | None = None
    if previous_sha is not None:
        changed = previous_sha != sha
    if status is BackupStatus.SUCCESS and changed is False:
        status = BackupStatus.UNCHANGED

    stored: StoredSnapshot | None = None
    if store is not None:
        record = SnapshotRecord(
            device_key=device_key,
            host=target.host,
            vendor=driver.name,
            status=status.value,
            running_config=running,
            normalized_config=normalized,
            startup_config=startup,
            facts=facts,
            sha256=sha,
            normalized_sha256=normalized_sha,
            size_bytes=size_bytes,
            line_count=lines,
            changed=changed,
            error_code="integrity_error" if status is BackupStatus.INTEGRITY_ERROR else None,
            error_message=_first_error_message(issues),
            issues=[{"code": i.code, "message": i.message, "severity": i.severity} for i in issues],
            warnings=warnings,
            transcript=transcript,
        )
        stored = store.save(record)

    return BackupOutcome(
        target=target,
        vendor=driver.name,
        status=status,
        hostname=hostname,
        running_config=running,
        normalized_config=normalized,
        startup_config=startup,
        facts=facts,
        sha256=sha,
        normalized_sha256=normalized_sha,
        size_bytes=size_bytes,
        line_count=lines,
        duration_ms=_ms(started),
        attempts=attempts,
        changed=changed,
        error_code="integrity_error" if status is BackupStatus.INTEGRITY_ERROR else None,
        error_message=_first_error_message(issues),
        issues=issues,
        warnings=warnings,
        stored=stored,
        transcript=transcript,
    )


async def _open_ready_session(
    target: DeviceTarget,
    driver: VendorDriver,
    *,
    session_factory: SessionFactory | None,
) -> tuple[InteractiveSession, str | None, list[str]]:
    """建立会话并完成到"可以取配置"的全部准备。"""
    config = target.session_config()
    session = (
        session_factory(config, driver.prompt_re(target.hostname))
        if session_factory is not None
        else create_session(
            config,
            driver.prompt_re(target.hostname),
            force_paramiko=target.use_paramiko,
        )
    )
    warnings: list[str] = []
    try:
        await session.open()
        await session.begin()

        # 从提示符反查真实设备名（台账未登记 hostname 时的唯一来源）
        hostname = target.hostname
        if not hostname:
            line = session.last_received_line()
            if line:
                hostname = driver.match_prompt(line)

        # 收紧提示符：这一步是"采到一半就结束"的主要防线
        session.prompt_re = driver.prompt_re(hostname)
        session.strict_prompt = hostname is not None
        # 让会话能自动回答 [Y/N] 类确认（华为 screen-width 就会弹）
        session.confirm_rules = list(driver.confirm_rules())

        warnings.extend(await _run_privilege(session, driver, target))
        warnings.extend(await _run_session_setup(session, driver))
    except BaseException:
        # 包括 open() 失败的情形：连接可能已建立但会话通道创建失败，必须回收
        with contextlib.suppress(Exception):
            await session.close()
        raise
    return session, hostname, warnings


async def _run_privilege(
    session: InteractiveSession,
    driver: VendorDriver,
    target: DeviceTarget,
) -> list[str]:
    warnings: list[str] = []
    requires = getattr(driver, "requires_enable_password", None)
    if callable(requires) and requires() and not target.enable_password:
        raise AuthError(
            f"驱动 {driver.name} 需要 enable/super 提权密码，但设备台账未配置"
        )

    steps = list(driver.privilege_steps(enable_password=target.enable_password))
    skip_next = False
    for index, step in enumerate(steps):
        # 警告文本会写进 run 记录与前端，绝不能带上密码
        label = "******" if step.secret else step.command
        if skip_next:
            # 上一步（通常是 enable）没有等到密码提示符，说明设备不需要密码。
            # 此时若继续把密码当命令发出去，就会在设备上留一条失败登录记录。
            skip_next = False
            warnings.append(
                f"已跳过提权步骤 {label!r}：上一步未出现预期的密码提示符"
            )
            continue

        expect = re.compile(step.expect, re.MULTILINE) if step.expect else None
        try:
            output = await session.send_command(
                step.command,
                prompt_re=expect,
                secret=step.secret,
                timeout=step.timeout or CONTROL_CMD_TIMEOUT,
            )
        except CommandTimeout as exc:
            if not step.optional:
                raise
            warnings.append(
                f"提权步骤 {label!r} 未出现预期提示符（{exc.message}），"
                "按「设备无需该步」处理"
            )
            skip_next = True
            await session.resync()
            continue

        for issue in integrity.check_command_output(output):
            message = f"提权命令 {label!r} 疑似失败：{issue.message}"
            if step.optional or any(s.secret for s in steps[index + 1:]):
                warnings.append(message)
            else:
                raise AuthError(message)
    return warnings


async def _run_session_setup(
    session: InteractiveSession, driver: VendorDriver
) -> list[str]:
    """执行关分页/关折行等预处理命令。

    这些命令失败**不致命**（SSH pty 已请求 512 列，多数设备会遵守），
    但会显著提高配置被折行的风险，所以必须作为警告回传给用户。
    """
    warnings: list[str] = []
    for command in driver.session_setup_cmds():
        try:
            output = await session.send_command(command, timeout=CONTROL_CMD_TIMEOUT)
        except CollectorError as exc:
            warnings.append(f"会话预处理命令 {command!r} 执行失败：{exc.message}")
            await session.resync()
            continue
        for issue in integrity.check_command_output(output):
            warnings.append(
                f"会话预处理命令 {command!r} 疑似被设备拒绝（{issue.message}），"
                "配置存在折行风险，请人工抽查"
            )
    return warnings


async def _tcp_precheck(target: DeviceTarget, *, timeout: float = PRECHECK_TIMEOUT) -> None:
    """TCP 可达性预检：不可达的设备直接判失败，不占用采集并发位。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.host, target.port), timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ConnectError(
            f"{target.host}:{target.port} 不可达", detail=f"{type(exc).__name__}: {exc}"
        ) from exc
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


# --------------------------------------------------------------------------
# 批量
# --------------------------------------------------------------------------
ProgressCallback = Callable[[int, int, "BackupOutcome"], Any]


async def backup_many(
    targets: Sequence[DeviceTarget],
    *,
    concurrency: int = 20,
    retries: int = 2,
    precheck: bool = True,
    store: ArtifactStore | None = None,
    registry: DriverRegistry | None = None,
    session_factory: SessionFactory | None = None,
    on_progress: ProgressCallback | None = None,
    per_device_serial: bool = True,
) -> list[BackupOutcome]:
    """批量采集。

    * ``concurrency`` 是**全局**并发上限，默认 20（保守值：设备 VTY 有限，
      核心区设备被打满会直接影响运维）。
    * ``per_device_serial`` 保证同一台设备任何时刻只有一个会话——即使调用方
      在列表里重复放了同一台设备。
    """
    registry = registry or get_registry()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    device_locks: dict[str, asyncio.Lock] = {}
    results: list[BackupOutcome | None] = [None] * len(targets)
    done = 0

    async def run_one(index: int, target: DeviceTarget) -> None:
        nonlocal done
        lock = device_locks.setdefault(target.key, asyncio.Lock())
        if per_device_serial:
            async with lock:
                async with semaphore:
                    outcome = await backup_device(
                        target,
                        store=store,
                        registry=registry,
                        retries=retries,
                        precheck=precheck,
                        session_factory=session_factory,
                    )
        else:
            async with semaphore:
                outcome = await backup_device(
                    target,
                    store=store,
                    registry=registry,
                    retries=retries,
                    precheck=precheck,
                    session_factory=session_factory,
                )
        results[index] = outcome
        done += 1
        if on_progress is not None:
            maybe = on_progress(done, len(targets), outcome)
            if inspect.isawaitable(maybe):
                await maybe

    await asyncio.gather(*(run_one(i, t) for i, t in enumerate(targets)))
    return [r for r in results if r is not None]


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _status_for(exc: CollectorError) -> BackupStatus:
    return _STATUS_BY_CODE.get(exc.code, BackupStatus.ERROR)


def _failure(
    target: DeviceTarget,
    exc: CollectorError,
    started: float,
    *,
    vendor: str | None = None,
    attempts: int = 1,
) -> BackupOutcome:
    return BackupOutcome(
        target=target,
        vendor=vendor or target.vendor,
        status=_status_for(exc),
        hostname=target.hostname,
        duration_ms=_ms(started),
        attempts=attempts,
        error_code=exc.code,
        error_message=exc.message,
        error_detail=exc.detail,
    )


def _first_error_message(issues: Sequence[Issue]) -> str | None:
    for issue in issues:
        if issue.is_error:
            return issue.message
    return None


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
