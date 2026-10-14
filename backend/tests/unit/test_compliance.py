"""合规基线引擎单测。

两类断言，缺一不可：

1. **引擎语义**（纯函数）：present/absent 怎么判、多模式是"与"还是"或"、severity 怎么
   合成 verdict、规则不适用时怎么算；
2. **内置基线在三份真实回显上的实际结果**（逐条列出违规 id）。这一类比"能跑通"重要得多：
   基线写错了不会报错，只会静静地放行——比如把 `telnet server enable` 的正则写宽了，
   华三那份**确实开着 telnet** 的配置就会被判成合规。夹具里的预期结果是在写规则时
   逐行核对过的，改规则必须同步改这里的预期。
"""

from __future__ import annotations

import pytest

from app.compliance import (
    Rule,
    RuleError,
    evaluate,
    load_rules,
    load_ruleset,
    verdict_of,
)
from app.compliance.engine import Violation
from tests.support.env import FIXTURES, fixture_text

HUAWEI = "huawei/vrp8/display_current-configuration.txt"
H3C = "h3c/comware7/display_current-configuration.txt"
RUIJIE = "ruijie/rgos11/show_running-config.txt"


@pytest.fixture(scope="module")
def builtin():
    """内置基线（模块级复用；规则集是不可变对象，共享安全）。"""
    return load_ruleset()


def violation_ids(result) -> set[str]:
    return {item.rule_id for item in result.violations}


# --------------------------------------------------------------------------
# 引擎语义
# --------------------------------------------------------------------------
def make_rule(**overrides) -> Rule:
    """直接构造规则（不走 YAML：这里只想测引擎）。"""
    from app.compliance.rules import _parse_rule

    payload = {
        "id": "r1",
        "desc": "测试规则",
        "severity": "high",
        "assert_absent": r"^\s*bad\s*$",
    }
    payload.update(overrides)
    return _parse_rule(payload, source="test", index=0)


def test_absent_violation_reports_the_matched_line():
    rule = make_rule(assert_absent=r"^\s*telnet\s+enable\s*$")
    result = evaluate("sysname A\ntelnet enable\nreturn\n", _ruleset(rule), vendor="huawei")
    assert result.verdict == "fail"
    assert violation_ids(result) == {"r1"}
    assert result.violations[0].line == "telnet enable", "要告诉人具体是哪一行"


def test_absent_rule_passes_when_pattern_missing():
    rule = make_rule()
    result = evaluate("sysname A\nreturn\n", _ruleset(rule), vendor="huawei")
    assert result.verdict == "pass"
    assert result.violations == ()


def test_present_rule_reports_what_is_missing():
    rule = make_rule(assert_absent=None, assert_present=r"^\s*ntp-service\s+unicast-server")
    result = evaluate("sysname A\n", _ruleset(rule), vendor="huawei")
    assert violation_ids(result) == {"r1"}
    assert result.violations[0].line and "未找到" in result.violations[0].line


def test_multiple_present_patterns_are_conjunctive():
    """``assert_present`` 的多个模式是"全部命中才算通过"（想表达"或"就用 |）。"""
    rule = make_rule(
        assert_absent=None,
        assert_present=[r"^\s*ntp-service", r"^\s*info-center\s+loghost"],
    )
    only_ntp = evaluate("ntp-service unicast-server 1.1.1.1\n", _ruleset(rule), vendor="huawei")
    assert violation_ids(only_ntp) == {"r1"}, "少了一半条件也应当算违规"

    both = evaluate(
        "ntp-service unicast-server 1.1.1.1\ninfo-center loghost 2.2.2.2\n",
        _ruleset(rule),
        vendor="huawei",
    )
    assert both.verdict == "pass"


def test_severity_drives_verdict():
    assert verdict_of([Violation("a", "low", "d")]) == "warn"
    assert verdict_of([Violation("a", "medium", "d")]) == "warn"
    assert verdict_of([Violation("a", "high", "d"), Violation("b", "low", "d")]) == "fail"
    assert verdict_of([]) == "pass"


def test_vendor_scoped_rule_is_not_checked_for_other_vendors():
    """厂商不匹配的规则既不算违规、也不算"检查过"（报表里要区分 na 与 pass）。"""
    rule = make_rule(vendors=("ruijie",), assert_absent=r"^\s*whatever\s*$")
    result = evaluate("whatever\n", _ruleset(rule), vendor="huawei")
    assert result.violations == ()
    assert result.checked == ()
    assert result.verdict == "pass"


def test_redact_is_applied_to_violation_line():
    """含密行的摘录必须脱敏后再入库：结果表不能变成口令的第二份明文副本。"""
    rule = make_rule(assert_absent=r"^\s*snmp-agent\s+community\s+read\s+\S+\s*$")
    text = "snmp-agent community read public\n"
    result = evaluate(
        text, _ruleset(rule), vendor="huawei", redact=lambda line: line.replace("public", "***")
    )
    assert result.violations[0].line == "snmp-agent community read ***"


def test_one_rule_records_at_most_one_violation():
    """同一规则命中多行只记一条：20 行同样的 telnet 配置没有额外信息量。"""
    rule = make_rule(assert_absent=r"^\s*telnet\s+enable\s*$")
    text = "telnet enable\ntelnet enable\ntelnet enable\n"
    result = evaluate(text, _ruleset(rule), vendor="huawei")
    assert len(result.violations) == 1


def test_max_violations_caps_the_result():
    rules = [make_rule(id=f"r{index}") for index in range(10)]
    result = evaluate("bad\n", _ruleset(*rules), vendor="huawei", max_violations=3)
    assert len(result.violations) == 3, "结果要有上限，否则一份裸配置能塞几百条"


def _ruleset(*rules: Rule):
    from app.compliance import RuleSet

    return RuleSet(rules=tuple(rules), version="test", source="test")


# --------------------------------------------------------------------------
# 规则加载
# --------------------------------------------------------------------------
def test_load_rejects_rule_without_assertion(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- id: empty\n  desc: 什么都不检查\n", encoding="utf-8")
    with pytest.raises(RuleError, match="assert_present"):
        load_rules(path)


def test_load_rejects_bad_regex(tmp_path):
    """正则写错必须在**加载时**报错：静默跳过会让运维以为规则生效了。"""
    path = tmp_path / "bad.yaml"
    path.write_text(
        "- id: broken\n  desc: 正则写错\n  assert_absent: '([unclosed'\n", encoding="utf-8"
    )
    with pytest.raises(RuleError, match="无法编译"):
        load_rules(path)


def test_load_rejects_unknown_severity(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "- id: x\n  desc: d\n  severity: critical\n  assert_absent: 'a'\n", encoding="utf-8"
    )
    with pytest.raises(RuleError, match="severity"):
        load_rules(path)


def test_load_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(
        "- id: same\n  desc: a\n  assert_absent: 'a'\n- id: same\n  desc: b\n  assert_absent: 'b'\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError, match="重复"):
        load_ruleset([path])


def test_custom_rules_replace_builtin(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("- id: only-mine\n  desc: 只有我这条\n  assert_absent: 'x'\n", encoding="utf-8")
    ruleset = load_ruleset([path])
    assert ruleset.rule_ids == ["only-mine"]
    assert ruleset.source == "custom"


def test_builtin_rules_load_and_are_unique(builtin):
    ids = builtin.rule_ids
    assert len(ids) == len(set(ids)) >= 15, f"内置规则数量异常：{len(ids)}"
    assert builtin.version and builtin.source == "builtin"
    # 严重级别只允许三档，且 high 排在前面（报表列顺序稳定）
    assert {rule.severity for rule in builtin.rules} <= {"high", "medium", "low"}


# --------------------------------------------------------------------------
# 内置基线在三份真实回显上的结果（逐条列出预期）
# --------------------------------------------------------------------------
def test_builtin_on_huawei_fixture(builtin):
    """华为夹具：整体是加固过的，只缺空闲超时 → warn。"""
    result = evaluate(fixture_text(HUAWEI), builtin, vendor="huawei")
    assert result.verdict == "warn", [item.as_dict() for item in result.violations]
    assert violation_ids(result) == {"idle-timeout-configured"}
    assert len(result.checked) == 14, f"适用规则数变了：{len(result.checked)}"


def test_builtin_on_h3c_fixture_flags_enabled_telnet(builtin):
    """华三夹具**确实开着 telnet** → 必须被判 fail（这是基线最该抓的东西）。"""
    result = evaluate(fixture_text(H3C), builtin, vendor="h3c")
    assert result.verdict == "fail"
    assert violation_ids(result) == {
        "no-telnet-server",
        "logging-host-configured",
        "idle-timeout-configured",
        "banner-configured",
    }
    telnet = next(item for item in result.violations if item.rule_id == "no-telnet-server")
    assert telnet.severity == "high" and telnet.line == "telnet server enable"


def test_builtin_on_ruijie_fixture_flags_default_communities(builtin):
    """锐捷夹具用了 public/private 默认团体名 + type-7 口令 → fail。"""
    result = evaluate(fixture_text(RUIJIE), builtin, vendor="ruijie")
    assert result.verdict == "fail"
    assert violation_ids(result) == {
        "ssh-server-enabled",
        "no-default-snmp-community",
        "no-type7-password",
        "logging-host-configured",
        "idle-timeout-configured",
        "banner-configured",
    }
    community = next(
        item for item in result.violations if item.rule_id == "no-default-snmp-community"
    )
    assert community.line == "snmp-server community public RO"


def test_fixtures_directory_is_where_we_think(builtin):
    """夹具路径写错时上面三条会"传过"（因为读到的是空文件）——这里守住前提。"""
    for relative in (HUAWEI, H3C, RUIJIE):
        text = (FIXTURES / relative).read_text(encoding="utf-8")
        assert len(text.splitlines()) >= 40, f"{relative} 内容异常（{len(text)} 字节）"
