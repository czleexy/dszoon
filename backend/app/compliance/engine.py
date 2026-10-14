"""合规评估引擎：把一份配置对着规则集跑一遍，产出"设备 × 规则"的结果。

**在归档原文上评估**（不是脱敏版）：规则要能判断"community 是不是 public"这类
明文问题，脱敏之后这些行就成 ``******`` 了，规则必然失效。但**违规行摘录要脱敏后
再入库**——否则结果表会变成"口令的第二份明文副本"（见 ``redact`` 参数）。

评估是**纯函数**：不碰数据库、不发网络请求，所以它能被单测逐条钉死（这是 P3 里
最容易写错、也最该被钉死的部分）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .rules import SEVERITIES, RuleSet

__all__ = ["Evaluation", "Violation", "evaluate", "verdict_of"]

#: 判定结果：fail = 有 high；warn = 有 medium/low；pass = 全过
VERDICTS = ("pass", "warn", "fail")


@dataclass(frozen=True, slots=True)
class Violation:
    """一条违规。``line`` 是命中的配置行（**已脱敏**）。"""

    rule_id: str
    severity: str
    description: str
    message: str | None = None
    line: str | None = None

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "description": self.description,
        }
        if self.message:
            data["message"] = self.message
        if self.line:
            data["line"] = self.line
        return data


@dataclass(frozen=True, slots=True)
class Evaluation:
    """一台设备的一次评估结果。"""

    vendor: str
    rules_version: str
    checked: tuple[str, ...]
    violations: tuple[Violation, ...]
    verdict: str

    @property
    def counts(self) -> dict[str, int]:
        counted = {severity: 0 for severity in SEVERITIES}
        for violation in self.violations:
            counted[violation.severity] = counted.get(violation.severity, 0) + 1
        return counted

    def as_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "rules_version": self.rules_version,
            "verdict": self.verdict,
            "checked": len(self.checked),
            "violations": [item.as_dict() for item in self.violations],
            "counts": self.counts,
        }


def verdict_of(violations: Iterable[Violation]) -> str:
    """有 high → fail；有 medium/low → warn；没有 → pass。"""
    severities = {item.severity for item in violations}
    if "high" in severities:
        return "fail"
    if severities:
        return "warn"
    return "pass"


def _first_match_line(text: str, patterns: Sequence) -> str | None:
    """返回命中模式的第一行（用于给人看"到底哪一行不合规"）。"""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            matched = match.group(0)
            # 命中的可能是行内的片段；取它所在的那一整行更有用
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end == -1:
                line_end = len(text)
            line = text[line_start:line_end].strip()
            return line or matched.strip()
    return None


def _first_searched_pattern(text: str, patterns: Sequence) -> str | None:
    """``assert_present`` 里哪个模式没找到（报错信息要用）。"""
    for pattern in patterns:
        if not pattern.search(text):
            return pattern.pattern
    return None


def evaluate(
    text: str,
    ruleset: RuleSet,
    *,
    vendor: str,
    redact: Callable[[str], str] | None = None,
    max_violations: int = 500,
) -> Evaluation:
    """对着规则集评估一份配置。

    :param text: **归档原文**（不是脱敏版，原因见模块说明）。
    :param vendor: 用于筛选 ``vendors`` 规则（大小写不敏感）。
    :param redact: 违规行摘录的脱敏函数（一般是驱动的 ``redact``）；
        不传则原样入库——生产路径必须传。
    :param max_violations: 单台设备最多记多少条违规。配置被整体替换（例如有人把
        一份不带任何加固的配置刷进去）时可能命中几百条，把结果表撑爆没有意义。
    """
    vendor = (vendor or "").lower()
    applicable = ruleset.applicable(vendor)
    violations: list[Violation] = []

    for rule in applicable:
        if len(violations) >= max_violations:
            break

        if rule.absent_re:
            line = _first_match_line(text, rule.absent_re)
            if line is not None:
                violations.append(
                    Violation(
                        rule_id=rule.id,
                        severity=rule.severity,
                        description=rule.desc,
                        message=rule.message,
                        line=_redact(line, redact),
                    )
                )
                continue  # 一条规则最多记一条：同一规则命中 20 行没有额外信息量

        if rule.present_re and _first_searched_pattern(text, rule.present_re) is not None:
            missing = _first_searched_pattern(text, rule.present_re)
            violations.append(
                Violation(
                    rule_id=rule.id,
                    severity=rule.severity,
                    description=rule.desc,
                    message=rule.message,
                    line=None if missing is None else f"未找到匹配：{missing}",
                )
            )

    violations.sort(key=lambda item: (_severity_rank(item.severity), item.rule_id))
    return Evaluation(
        vendor=vendor,
        rules_version=ruleset.version,
        checked=tuple(rule.id for rule in applicable),
        violations=tuple(violations),
        verdict=verdict_of(violations),
    )


def _severity_rank(severity: str) -> int:
    try:
        return SEVERITIES.index(severity)
    except ValueError:  # pragma: no cover - 规则加载时已校验
        return len(SEVERITIES)


def _redact(line: str, redact: Callable[[str], str] | None) -> str:
    if redact is None:
        return line
    try:
        return redact(line)
    except Exception:  # noqa: BLE001 - 脱敏失败宁可少给信息，也不能让评估失败
        return "***"
