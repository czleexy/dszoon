"""配置完整性校验。

解决的问题：**"备份成功但内容是残的"** 是这类系统最危险的失败模式。
设备可能因为没关分页而只吐出前 20 行，可能因为没关折行而把长配置行劈成两半，
可能因为命令拼错而返回 "Error: Unrecognized command"，而采集器如果只看
"有没有拿到字符串"，就会把垃圾当成备份存起来，直到真的需要恢复时才发现。

因此：每次采集后必须过一遍 :func:`check_config`，任何 error 级问题都会让本次
run 状态变成 ``integrity_error``（内容仍然归档，但不计为成功）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Pattern, Sequence

from . import textutil

__all__ = [
    "ConfigSpec",
    "Issue",
    "check_command_output",
    "check_config",
    "check_setup_leakage",
    "has_errors",
    "DEFAULT_ERROR_MARKERS",
]

#: 设备对命令报错的典型回显。只在输出**开头窗口**内扫描（见 error_scan_window），
#: 避免配置里的 header/banner 文本误伤。
DEFAULT_ERROR_MARKERS: tuple[str, ...] = (
    r"^\s*Error:\s",
    r"^\s*Failure:\s",
    r"^\s*%\s*(?:Invalid|Unrecognized|Incomplete|Ambiguous|Wrong|Too many|Bad|Permission)",
    r"^\s*Wrong parameter",
    r"^\s*Unrecognized command",
    r"^\s*Invalid input",
    r"^\s*Incomplete command",
    r"^\s*Ambiguous command",
    r"^\s*Command is not found",
    r"^\s*\^\s*$",
)

#: 终端折行的可疑宽度（配置行极少正好等于这些长度）
SUSPECT_WRAP_WIDTHS = (78, 79, 80, 81, 130, 131, 132)


@dataclass(frozen=True, slots=True)
class Issue:
    """一条校验发现。severity 为 ``error`` 时本次备份不计成功。"""

    code: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(slots=True)
class ConfigSpec:
    """一个厂商的配置"长得对不对"的判定标准（由驱动提供）。"""

    #: 末行必须匹配其中之一（如华为/H3C 的 ``return``、锐捷的 ``end``）
    must_end_with: tuple[str, ...] = ()
    #: 正文必须包含其中之一（如 ``#`` 分隔行）
    must_contain: tuple[str, ...] = ()
    #: 命中即 error 的禁止模式（驱动一般不用，留作特殊设备定制）
    forbid_patterns: tuple[str, ...] = ()
    min_lines: int = 5
    min_bytes: int = 100
    #: 命令报错扫描窗口（行数）
    error_scan_window: int = 15
    error_markers: tuple[str, ...] = DEFAULT_ERROR_MARKERS
    #: 折行启发式开关
    detect_line_wrap: bool = True

    _compiled: dict[str, list[Pattern[str]]] = field(
        init=False, default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self._compiled = {
            "must_end_with": [_compile(p) for p in self.must_end_with],
            "must_contain": [_compile(p) for p in self.must_contain],
            "forbid_patterns": [_compile(p) for p in self.forbid_patterns],
            "error_markers": [_compile(p) for p in self.error_markers],
        }

    def compiled(self, key: str) -> list[Pattern[str]]:
        return self._compiled.get(key, [])

    def merged(self, **overrides: object) -> "ConfigSpec":
        """基于当前 spec 覆盖若干字段（供设备级自定义配置使用）。"""
        return replace(self, **overrides)  # type: ignore[arg-type]


def _compile(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.MULTILINE)


def has_errors(issues: Sequence[Issue]) -> bool:
    return any(issue.is_error for issue in issues)


def check_config(text: str, spec: ConfigSpec | None = None) -> list[Issue]:
    """对清洗后的配置正文做完整性校验。"""
    spec = spec or ConfigSpec()
    issues: list[Issue] = []

    if not text or not text.strip():
        issues.append(Issue("empty", "采集结果为空"))
        return issues

    text = textutil.normalize_newlines(text)
    lines = text.split("\n")
    non_empty = [line for line in lines if line.strip()]

    # 1) 体量下限——最常见的原因是分页没关，只拿到第一屏
    if len(non_empty) < spec.min_lines:
        issues.append(
            Issue(
                "too_short",
                f"有效行数 {len(non_empty)} 少于下限 {spec.min_lines}，"
                "疑似分页未关闭或命令未生效",
            )
        )
    if len(text.encode("utf-8")) < spec.min_bytes:
        issues.append(
            Issue(
                "too_short_bytes",
                f"正文仅 {len(text.encode('utf-8'))} 字节，少于下限 {spec.min_bytes}",
            )
        )

    # 2) 命令报错——只扫开头窗口，避免配置里的 banner 文本误伤
    window = "\n".join(lines[: spec.error_scan_window])
    for pattern in spec.compiled("error_markers"):
        m = pattern.search(window)
        if m:
            issues.append(
                Issue(
                    "device_error",
                    f"设备返回报错：{m.group(0).strip()!r} —— 采集命令未生效",
                )
            )
            break

    # 3) 清洗残留（理论上不应出现，出现说明清洗有 bug 或遇到了新设备行为）
    #    注意：这里必须用"全文出现"而不是 has_more_token —— 后者只在结尾匹配，
    #    而残留的分页符恰恰出现在正文中间。
    if textutil.MORE_TOKEN_RE.search(text):
        issues.append(
            Issue("more_residue", "正文中残留分页提示符，内容可能被截断")
        )
    if "\x1b" in text:
        issues.append(Issue("ansi_residue", "正文中残留 ANSI 转义序列"))

    # 4) 结构特征
    for pattern in spec.compiled("must_end_with"):
        if pattern.search(non_empty[-1]):
            break
    else:
        if spec.must_end_with:
            issues.append(
                Issue(
                    "bad_terminator",
                    f"末行 {non_empty[-1][:60]!r} 不匹配预期结尾 "
                    f"{list(spec.must_end_with)}，配置疑似被截断",
                )
            )

    for pattern in spec.compiled("must_contain"):
        if not pattern.search(text):
            issues.append(
                Issue(
                    "missing_marker",
                    f"正文缺少预期结构标记 {pattern.pattern!r}",
                )
            )

    for pattern in spec.compiled("forbid_patterns"):
        m = pattern.search(text)
        if m:
            issues.append(
                Issue("forbidden", f"命中禁止模式 {pattern.pattern!r}：{m.group(0)!r}")
            )

    # 5) 折行启发式：设备若没设置终端宽度，会在固定列数处把长行劈开。
    #    这只是一个 warning——配置行恰好 80 字符也可能合法（如长 ACL 描述）。
    if spec.detect_line_wrap:
        lengths = Counter(len(line) for line in non_empty)
        suspects = [
            (width, count)
            for width, count in lengths.items()
            if width in SUSPECT_WRAP_WIDTHS and count >= 3
        ]
        if suspects:
            width, count = max(suspects, key=lambda item: item[1])
            issues.append(
                Issue(
                    "possible_line_wrap",
                    f"有 {count} 行长度恰好为 {width} 字符，疑似终端折行导致配置被劈开，"
                    "请人工抽查（确认 screen-width/terminal width 已生效）",
                    severity="warning",
                )
            )

    return issues


_MARKERS_CACHE: dict[tuple[str, ...], tuple[Pattern[str], ...]] = {}


def _compiled_markers(markers: tuple[str, ...]) -> tuple[Pattern[str], ...]:
    cached = _MARKERS_CACHE.get(markers)
    if cached is None:
        cached = tuple(re.compile(p, re.MULTILINE) for p in markers)
        _MARKERS_CACHE[markers] = cached
    return cached


def check_command_output(
    output: str,
    *,
    markers: Sequence[str] | None = None,
    window: int = 8,
) -> list[Issue]:
    """检查一条**会话控制命令**（关分页、关折行、提权）的回显是否报错。

    与 :func:`check_config` 的区别：这里只判断"设备是否拒绝了这条命令"，
    不做配置结构校验。采集引擎用它把"预处理命令失败"记录成警告——这类失败
    很隐蔽，但直接影响后续取到的配置是否完整。
    """
    if not output or not output.strip():
        return []
    text = "\n".join(output.split("\n")[:window])
    for pattern in _compiled_markers(tuple(markers or DEFAULT_ERROR_MARKERS)):
        m = pattern.search(text)
        if m:
            return [Issue("device_error", f"设备返回报错：{m.group(0).strip()!r}")]
    return []


def check_setup_leakage(
    config_text: str, setup_commands: Sequence[str]
) -> list[Issue]:
    """检查"会话预处理命令"是否被写进了配置正文。

    采集器只允许改**会话状态**，不允许改**设备配置**。如果 ``screen-width 512``
    这类命令出现在 running-config 里，说明它其实是持久化配置——那"只读备份"
    就变成了"每次备份都改设备配置"，必须立刻让人看到。

    判据用整行精确匹配（而不是关键字包含），因为 ``screen-length 0`` 完全可能
    是管理员自己配在 ``user-interface vty`` 下的，那种情况不该报警。

    真机实测（VRP5.170 / S5720S）：``screen-width 512`` 会弹
    "This command will change the default screen width. Continue? [Y/N]"，
    但**不会**写进配置，所以这里是 warning 而不是 error。
    """
    if not config_text:
        return []
    issues: list[Issue] = []
    for command in setup_commands:
        command = command.strip()
        if not command:
            continue
        pattern = re.compile(r"^\s*" + re.escape(command) + r"\s*$", re.MULTILINE)
        if pattern.search(config_text):
            issues.append(
                Issue(
                    "setup_leaked_into_config",
                    f"会话预处理命令 {command!r} 出现在配置正文里，"
                    "说明它可能是持久化配置（= 采集动作改变了设备状态），请人工确认",
                    severity="warning",
                )
            )
    return issues
