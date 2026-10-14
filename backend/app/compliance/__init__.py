"""合规基线检查（P3）。

对外出口分两层：

* **规则**：``Rule`` / ``RuleSet`` / ``load_ruleset`` / ``describe_rules``（YAML → 数据）
* **评估**：``evaluate`` / ``Evaluation`` / ``Violation`` / ``verdict_of``（纯函数）

引擎刻意不碰数据库、不发网络请求：评估逻辑是 P3 里最容易写错、也最该被逐条钉死的
部分（``tests/unit/test_compliance.py``）。
"""

from __future__ import annotations

from .engine import Evaluation, Violation, evaluate, verdict_of
from .rules import (
    DEFAULT_BASELINE_DIR,
    SEVERITIES,
    Rule,
    RuleError,
    RuleSet,
    describe_rules,
    load_rules,
    load_ruleset,
)

__all__ = [
    "DEFAULT_BASELINE_DIR",
    "SEVERITIES",
    "Evaluation",
    "Rule",
    "RuleError",
    "RuleSet",
    "Violation",
    "describe_rules",
    "evaluate",
    "load_rules",
    "load_ruleset",
    "verdict_of",
]
