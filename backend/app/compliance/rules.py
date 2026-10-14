"""合规基线规则：YAML 定义 + 加载与校验。

**规则是数据，不是代码**：运维不改 Python 就能调整基线（加一条"禁止 telnet"、
把 NTP 从 medium 提到 high、按站点启用不同规则）。格式（见 ``baselines/baseline.yaml``）::

    - id: no-telnet-server            # 唯一且稳定：结果表里存的就是它
      vendors: [huawei, h3c, ruijie]  # 不给 = 所有厂商
      desc: 禁止开启 Telnet 服务
      severity: high                  # high | medium | low
      assert_absent: '^\\s*telnet\\s+(?:server\\s+)?enable\\s*$'   # 命中即违规
      message: 请关闭 telnet 或限制源地址并改用 SSH                   # 可选，整改提示
      enabled: true                   # 可选，默认 true

三条刻意的设计决定：

1. **加载即编译、编译失败即报错**。正则写错时如果只在评估时静默跳过，运维会以为
   "这条规则生效了"，而实际上它什么都没检查——这类"看着有、其实没有"的规则比没有
   更危险（和 P2 里 device_failed_streak 那个 bug 是同一类）。
2. **``assert_present`` 的多个模式是"与"**：列表里全部命中才算通过；想表达"或"就把
   备选写进同一个正则的 ``|`` 里（计划文档里的示例就是这么写的）。
3. **规则集带版本指纹**（``RuleSet.version`` = 规则内容的 sha256）：结果表里存下它，
   "换了规则之后新旧结果混在一起"这件事就看得出来了。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Pattern, Sequence

import yaml

__all__ = [
    "DEFAULT_BASELINE_DIR",
    "Rule",
    "RuleError",
    "RuleSet",
    "load_rules",
    "load_ruleset",
]

#: 内置规则目录
DEFAULT_BASELINE_DIR = Path(__file__).resolve().parent / "baselines"

#: 允许的严重级别（顺序即排序权重，报表按它排序）
SEVERITIES = ("high", "medium", "low")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}


class RuleError(ValueError):
    """规则文件本身有问题（写法错、id 重复、正则编译失败）。"""


def _compile(pattern: str, *, rule_id: str, field_name: str) -> Pattern[str]:
    try:
        # 规则是逐行匹配配置的，统一用 MULTILINE：写 ^...$ 才符合直觉
        return re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise RuleError(
            f"规则 {rule_id!r} 的 {field_name} 正则无法编译：{pattern!r} —— {exc}"
        ) from exc


def _as_patterns(value: Any, *, rule_id: str, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        patterns: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise RuleError(f"规则 {rule_id!r} 的 {field_name} 里有非字符串项：{item!r}")
            patterns.append(item)
        return tuple(patterns)
    raise RuleError(f"规则 {rule_id!r} 的 {field_name} 必须是字符串或字符串列表")


@dataclass(frozen=True, slots=True)
class Rule:
    """一条基线规则。``patterns`` 里的正则在加载时就编译好。"""

    id: str
    desc: str
    severity: str
    vendors: tuple[str, ...] = ()
    assert_present: tuple[str, ...] = ()
    assert_absent: tuple[str, ...] = ()
    message: str | None = None
    enabled: bool = True
    present_re: tuple[Pattern[str], ...] = field(default=(), repr=False, compare=False)
    absent_re: tuple[Pattern[str], ...] = field(default=(), repr=False, compare=False)

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, len(SEVERITIES))

    def applies_to(self, vendor: str) -> bool:
        """本规则是否适用于该厂商（``vendors`` 为空 = 通用规则）。"""
        return not self.vendors or vendor in self.vendors

    def fingerprint(self) -> str:
        """规则内容的稳定指纹（用于合成规则集版本）。"""
        payload = {
            "id": self.id,
            "severity": self.severity,
            "vendors": sorted(self.vendors),
            "present": sorted(self.assert_present),
            "absent": sorted(self.assert_absent),
            "enabled": self.enabled,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class RuleSet:
    """一组规则 + 版本指纹。"""

    rules: tuple[Rule, ...]
    version: str
    source: str = "builtin"

    def applicable(self, vendor: str) -> list[Rule]:
        """按厂商筛出启用的规则（保持文件里的顺序，报表列顺序稳定）。"""
        return [rule for rule in self.rules if rule.enabled and rule.applies_to(vendor)]

    def by_id(self) -> dict[str, Rule]:
        return {rule.id: rule for rule in self.rules}

    @property
    def rule_ids(self) -> list[str]:
        return [rule.id for rule in self.rules]


def _parse_rule(raw: Any, *, source: str, index: int) -> Rule:
    if not isinstance(raw, dict):
        raise RuleError(f"{source} 第 {index + 1} 条规则不是字典：{raw!r}")

    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        raise RuleError(f"{source} 第 {index + 1} 条规则缺少 id")

    severity = str(raw.get("severity") or "medium").strip().lower()
    if severity not in SEVERITIES:
        raise RuleError(
            f"规则 {rule_id!r} 的 severity 只能是 {'/'.join(SEVERITIES)}，收到 {severity!r}"
        )

    present = _as_patterns(raw.get("assert_present"), rule_id=rule_id, field_name="assert_present")
    absent = _as_patterns(raw.get("assert_absent"), rule_id=rule_id, field_name="assert_absent")
    if not present and not absent:
        raise RuleError(
            f"规则 {rule_id!r} 既没有 assert_present 也没有 assert_absent —— "
            "这样的规则永远通过，等于没写（只想提示不想判违规时，请用 severity: low）"
        )

    vendors_raw = raw.get("vendors") or []
    if isinstance(vendors_raw, str):
        vendors_raw = [vendors_raw]
    vendors = tuple(str(item).strip().lower() for item in vendors_raw if str(item).strip())

    return Rule(
        id=rule_id,
        desc=str(raw.get("desc") or rule_id).strip(),
        severity=severity,
        vendors=vendors,
        assert_present=present,
        assert_absent=absent,
        message=(str(raw["message"]).strip() if raw.get("message") else None),
        enabled=bool(raw.get("enabled", True)),
        present_re=tuple(
            _compile(pattern, rule_id=rule_id, field_name="assert_present") for pattern in present
        ),
        absent_re=tuple(
            _compile(pattern, rule_id=rule_id, field_name="assert_absent") for pattern in absent
        ),
    )


def load_rules(path: str | Path) -> list[Rule]:
    """读一个 YAML 规则文件（顶层是规则列表）。"""
    path = Path(path)
    try:
        # ★ 显式 utf-8：靠 locale 编码读会像 alembic.ini 那样在中文 Windows 上炸掉
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuleError(f"规则文件不存在：{path}") from exc
    except yaml.YAMLError as exc:
        raise RuleError(f"规则文件不是合法 YAML：{path} —— {exc}") from exc

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuleError(f"规则文件的顶层必须是列表：{path}")
    return [_parse_rule(item, source=str(path), index=index) for index, item in enumerate(raw)]


def load_ruleset(paths: Sequence[str | Path] | None = None, *, source: str | None = None) -> RuleSet:
    """加载并合并多个规则文件。

    * 不传路径 = 用内置基线（``baselines/*.yaml``，按文件名排序）；
    * 传了路径 = 只用这些文件（**替换**而不是叠加）：运维想完全接管基线时不必
      先想办法把内置规则"关掉"；
    * id 重复直接报错（静默覆盖会让"我改的那条为什么不生效"变得无从解释）。
    """
    if paths:
        files = [Path(item) for item in paths]
        source_name = source or "custom"
    else:
        files = sorted(DEFAULT_BASELINE_DIR.glob("*.yaml"))
        source_name = source or "builtin"

    rules: list[Rule] = []
    seen: dict[str, str] = {}
    for path in files:
        for rule in load_rules(path):
            if rule.id in seen:
                raise RuleError(
                    f"规则 id 重复：{rule.id!r} 同时出现在 {seen[rule.id]} 与 {path}"
                )
            seen[rule.id] = str(path)
            rules.append(rule)

    rules.sort(key=lambda item: (item.rank, item.id))
    fingerprint = hashlib.sha256(
        "\n".join(rule.fingerprint() for rule in rules).encode("utf-8")
    ).hexdigest()[:16]
    return RuleSet(rules=tuple(rules), version=fingerprint, source=source_name)


def describe_rules(rules: Iterable[Rule]) -> list[dict[str, Any]]:
    """给 API/前端用的规则摘要（**不含编译后的正则对象**）。"""
    return [
        {
            "id": rule.id,
            "desc": rule.desc,
            "severity": rule.severity,
            "vendors": list(rule.vendors),
            "enabled": rule.enabled,
            "assert_present": list(rule.assert_present),
            "assert_absent": list(rule.assert_absent),
            "message": rule.message,
        }
        for rule in rules
    ]
