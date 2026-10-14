"""声明式 YAML 驱动加载器。

目标：**新增一个厂商不需要改引擎代码**。95% 的差异都能用
「提示符正则 + 命令表 + 归一化规则 + 脱敏规则 + 校验规则」描述清楚；
遇到真正古怪的设备，再用 Python 子类覆盖单个方法（见 ``custom_example.py`` 的写法）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Pattern, Sequence

import yaml

from .. import integrity, textutil
from .base import ConfirmRule, PrivilegeStep, VendorDriver

__all__ = ["YamlDriver", "load_driver_spec", "load_yaml_driver"]

#: 匹配**整个**命名组（含收尾括号）——用于替换成字面量设备名
_HOSTNAME_GROUP_RE = re.compile(r"\(\?P<hostname>[^)]*\)")
#: 只匹配命名组的**开头语法**——用于把命名组降级为普通分组。
#: ★ 必须和上面那个区分开：拿"整个组"的正则去 sub("(?:") 会把收尾括号一起吃掉，
#:   生成 `^<(?:[A-Za-z0-9_.\-]{1,63}>\s*$` 这种括号不配对的非法正则，
#:   结果是**每一次连接都直接 PatternError**。这个坑由
#:   test_generic_prompt_re_compiles 守住。
_HOSTNAME_OPEN_RE = re.compile(r"\(\?P<hostname>")

_ENABLE_PASSWORD_PLACEHOLDER = "{enable_password}"


class YamlDriver(VendorDriver):
    """由 YAML 描述驱动的通用实现。"""

    def __init__(self, spec: dict[str, Any], *, source: Path | None = None) -> None:
        if not isinstance(spec, dict):
            raise ValueError("驱动定义必须是一个 YAML 映射")
        if not spec.get("name"):
            raise ValueError("驱动定义缺少 name 字段")

        self.spec = spec
        self.source = source
        self.name = str(spec["name"])
        self.display_name = str(spec.get("display_name", self.name))
        self.aliases = tuple(str(a) for a in spec.get("aliases", ()))
        self.description = str(spec.get("description", ""))
        self.supports_deploy = bool(spec.get("supports_deploy", False))

        self._prompt_patterns: list[str] = [str(p) for p in spec.get("prompt_patterns", ())]
        if not self._prompt_patterns:
            raise ValueError(f"驱动 {self.name} 缺少 prompt_patterns")

        # 用于"反查设备名"的提示符形态：只保留**无歧义**的那些。
        # 华为/H3C 的 `[H3C-CORE-02-Vlan-interface10]` 里，设备名和视图名用同一个
        # 连字符分隔，正则无法判断该切在哪 —— 与其猜错（猜错的后果是用错误的提示符
        # 去匹配配置正文），不如返回 None 让引擎回退到台账里的设备名。
        self._hostname_prompt_patterns: list[str] = [
            str(p) for p in spec.get("hostname_prompt_patterns", ())
        ] or list(self._prompt_patterns)
        self._hostname_prompt_res: list[Pattern[str]] = [
            re.compile(p, re.MULTILINE) for p in self._hostname_prompt_patterns
        ]
        self._prompt_cache: dict[str | None, Pattern[str]] = {}

        self._commands: dict[str, Any] = dict(spec.get("commands") or {})
        self._session_setup: tuple[str, ...] = tuple(spec.get("session_setup") or ())
        self._privilege: list[dict[str, Any]] = list(spec.get("privilege") or ())
        self._confirm_rules: list[ConfirmRule] = [
            (
                re.compile(str(item["pattern"]), re.MULTILINE),
                str(item.get("reply", "Y")),
            )
            for item in (spec.get("confirm_prompts") or ())
            if item.get("pattern")
        ]

        normalize = spec.get("normalize") or {}
        self._normalize_drop: list[Pattern[str]] = [
            re.compile(str(p), re.MULTILINE) for p in normalize.get("drop_patterns", ())
        ]

        self._redact_rules: list[tuple[Pattern[str], str]] = [
            (
                re.compile(str(rule["pattern"]), re.MULTILINE),
                str(rule.get("replacement", r"\1******")),
            )
            for rule in (spec.get("redact") or ())
            if rule.get("pattern")
        ]

        self._fact_patterns: list[tuple[str, Pattern[str]]] = [
            (str(key), re.compile(str(pattern), re.MULTILINE))
            for key, pattern in (spec.get("facts") or {}).items()
        ]
        self._hostname_patterns: list[Pattern[str]] = [
            re.compile(str(p), re.MULTILINE) for p in spec.get("hostname_patterns", ())
        ]

        validate = spec.get("validate") or {}
        self._config_spec = integrity.ConfigSpec(
            must_end_with=tuple(str(p) for p in validate.get("must_end_with", ())),
            must_contain=tuple(str(p) for p in validate.get("must_contain", ())),
            forbid_patterns=tuple(str(p) for p in validate.get("forbid_patterns", ())),
            min_lines=int(validate.get("min_lines", 5)),
            min_bytes=int(validate.get("min_bytes", 100)),
            error_scan_window=int(validate.get("error_scan_window", 15)),
            detect_line_wrap=bool(validate.get("detect_line_wrap", True)),
        )

    # ------------------------------------------------------------------
    # 提示符
    # ------------------------------------------------------------------
    def prompt_re(self, hostname: str | None = None) -> Pattern[str]:
        key = hostname.lower() if hostname else None
        cached = self._prompt_cache.get(key)
        if cached is not None:
            return cached
        parts: list[str] = []
        for raw in self._prompt_patterns:
            if hostname:
                # 用字面量设备名替换整组：杜绝配置正文误命中提示符
                parts.append(_HOSTNAME_GROUP_RE.sub(re.escape(hostname), raw))
            else:
                # 只把命名组降级成普通分组，收尾括号必须保留
                parts.append(_HOSTNAME_OPEN_RE.sub("(?:", raw))
        compiled = re.compile("|".join(parts), re.MULTILINE)
        self._prompt_cache[key] = compiled
        return compiled

    def match_prompt(self, line: str) -> str | None:
        """从一行提示符里反查设备名；**无歧义的形态才会返回结果**。"""
        for pattern in self._hostname_prompt_res:
            m = pattern.search(line)
            if m:
                return m.groupdict().get("hostname")
        return None

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def running_config_cmd(self) -> str:
        cmd = self._commands.get("running_config")
        if not cmd:
            raise ValueError(f"驱动 {self.name} 未定义 commands.running_config")
        return str(cmd)

    def startup_config_cmd(self) -> str | None:
        cmd = self._commands.get("startup_config")
        return str(cmd) if cmd else None

    def version_cmd(self) -> str | None:
        cmd = self._commands.get("version")
        return str(cmd) if cmd else None

    def session_setup_cmds(self) -> Sequence[str]:
        return self._session_setup

    def privilege_steps(self, *, enable_password: str | None = None) -> Sequence[PrivilegeStep]:
        steps: list[PrivilegeStep] = []
        for raw in self._privilege:
            command = str(raw.get("command", ""))
            if _ENABLE_PASSWORD_PLACEHOLDER in command:
                if not enable_password:
                    # 设备需要提权密码但台账里没配 —— 交给调用方决定是否报错，
                    # 这里只是跳过该步，避免把空密码发给设备（会记一条失败登录）
                    continue
                command = command.replace(_ENABLE_PASSWORD_PLACEHOLDER, enable_password)
            steps.append(
                PrivilegeStep(
                    command=command,
                    expect=raw.get("expect"),
                    secret=bool(raw.get("secret", False)),
                    timeout=float(raw["timeout"]) if raw.get("timeout") else None,
                    optional=bool(raw.get("optional", False)),
                )
            )
        return steps

    def requires_enable_password(self) -> bool:
        return any(
            _ENABLE_PASSWORD_PLACEHOLDER in str(raw.get("command", ""))
            for raw in self._privilege
        )

    def confirm_rules(self) -> Sequence[ConfirmRule]:
        return tuple(self._confirm_rules)

    # ------------------------------------------------------------------
    # 后处理
    # ------------------------------------------------------------------
    def config_spec(self) -> integrity.ConfigSpec:
        return self._config_spec

    def normalize(self, text: str) -> str:
        text = textutil.normalize_newlines(text)
        for pattern in self._normalize_drop:
            text = pattern.sub("", text)
        lines = [line.rstrip() for line in text.split("\n")]
        out: list[str] = []
        blanks = 0
        for line in lines:
            if not line.strip():
                blanks += 1
                if blanks <= 1:
                    out.append("")
                continue
            blanks = 0
            out.append(line)
        return "\n".join(out).strip("\n")

    def redact(self, text: str) -> str:
        text, _ = textutil.apply_redaction(text, self._redact_rules)
        return text

    def extract_facts(self, version_output: str, config_text: str) -> dict[str, str]:
        facts: dict[str, str] = {}
        for key, pattern in self._fact_patterns:
            m = pattern.search(version_output or "")
            if m:
                facts[key] = (m.group(1) if m.groups() else m.group(0)).strip()
        hostname = self.extract_hostname(config_text)
        if hostname:
            facts["hostname"] = hostname
        return facts

    def extract_hostname(self, config_text: str) -> str | None:
        for pattern in self._hostname_patterns:
            m = pattern.search(config_text or "")
            if m:
                return m.group(1).strip()
        return None


def load_driver_spec(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是一个合法的驱动定义")
    return data


def load_yaml_driver(path: Path | str) -> YamlDriver:
    path = Path(path)
    return YamlDriver(load_driver_spec(path), source=path)


def iter_driver_files(directory: Path | str) -> Iterable[Path]:
    directory = Path(directory)
    if directory.is_dir():
        yield from sorted(directory.glob("*.yaml"))
        yield from sorted(directory.glob("*.yml"))
