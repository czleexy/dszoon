"""下发前置预检的单测（P4-A 的纯逻辑部分）。

这一层的判断标准是"宁可拒绝执行"：漏放一条 `save` 或 `reboot` 出去的后果，是设备重启后
拿着半成品配置起不来——所以在测试里，每一条**危险命令都必须被抓到**，
而且抓不到就算失败（而不是"少抓一条也无所谓"）。
"""

from __future__ import annotations

import pytest

from app.collector.deploy import (
    MAX_LINES,
    MAX_LINE_LENGTH,
    build_outcome,
    check_lines,
    plan_deploy,
)


def codes(plan) -> set[str]:
    return {issue.code for issue in plan.issues}


# --------------------------------------------------------------------------
# 危险命令：出现即拒绝，没有例外
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "save",
        "save force",
        "write memory",
        "copy running-config startup-config",
        "reboot",
        "reload",
        "reset saved-configuration",
        "format flash:",
        "erase startup-config",
        "undo startup saved-configuration",
        "factory-reset",
        "shutdown",
        "undo ip route-static 0.0.0.0 0.0.0.0 10.0.0.1",
    ],
)
def test_dangerous_commands_are_rejected(line):
    plan = plan_deploy(["sysname SW-01", line])
    assert not plan.ok, f"{line!r} 必须被拒绝"
    assert "dangerous_command" in codes(plan)


def test_dangerous_command_is_rejected_even_with_valid_lines():
    """危险命令混在正常变更里同样要拦——真实的下发请求就是这么长的。"""
    plan = plan_deploy(
        [
            "interface GigabitEthernet0/0/1",
            " description UPLINK",
            " quit",
            "save",
        ]
    )
    assert not plan.ok
    assert "dangerous_command" in codes(plan)


def test_normal_change_is_accepted():
    plan = plan_deploy(
        [
            "interface GigabitEthernet0/0/1",
            " port link-type trunk",
            " port trunk allow-pass vlan 10 20",
            " quit",
        ]
    )
    assert plan.ok, plan.as_dict()
    assert plan.issues == []
    assert plan.lines[0] == "interface GigabitEthernet0/0/1"


# --------------------------------------------------------------------------
# 会话命令不得写进配置（与采集层的只读红线同源）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line", ["screen-length 0 temporary", "screen-width 512", "terminal length 0", "enable", "system-view"]
)
def test_session_setup_commands_are_rejected(line):
    plan = plan_deploy([line])
    assert not plan.ok
    assert "session_command_in_deploy" in codes(plan)


# --------------------------------------------------------------------------
# 内容形态检查
# --------------------------------------------------------------------------
def test_error_echo_is_rejected():
    """从终端粘回显是实际会发生的操作失误：里面常带报错行。"""
    plan = plan_deploy(
        [
            "interface GigabitEthernet0/0/1",
            "% Invalid input detected at '^' marker.",
            " quit",
        ]
    )
    assert not plan.ok
    assert "error_echo" in codes(plan)


def test_empty_change_is_rejected():
    plan = plan_deploy(["", "   ", "\n"])
    assert not plan.ok
    assert codes(plan) == {"empty"}


def test_too_many_lines_is_rejected():
    plan = plan_deploy([f"interface Vlanif{index}" for index in range(MAX_LINES + 1)])
    assert not plan.ok
    assert "too_many_lines" in codes(plan)


def test_overlong_line_is_rejected():
    plan = plan_deploy(["description " + "x" * MAX_LINE_LENGTH])
    assert not plan.ok
    assert "line_too_long" in codes(plan)


def test_ansi_residue_is_rejected():
    plan = plan_deploy(["description \x1b[1mUPLINK"])
    assert "ansi_residue" in codes(plan)


# --------------------------------------------------------------------------
# 规范化与内容哈希
# --------------------------------------------------------------------------
def test_normalization_drops_blank_lines_and_trailing_spaces():
    plan = plan_deploy(["  interface Vlanif10  ", "", "ip address 10.0.0.1 255.255.255.0\t"])
    assert plan.lines == ("interface Vlanif10", "ip address 10.0.0.1 255.255.255.0")


def test_pasted_multiline_string_is_split():
    """从网页/终端粘进来的内容经常一行里带多个换行。"""
    plan = plan_deploy(["interface Vlanif10\n description MGMT\n quit\n"])
    assert plan.lines == ("interface Vlanif10", "description MGMT", "quit")
    assert plan.ok


def test_content_hash_is_stable_and_content_sensitive():
    """★ 审批绑定内容：哈希必须稳定（同内容同哈希），且对内容敏感（差一行就不同）。

    没有它，"审批的是 A、下发的是 B"就无法被发现——这是下发链路里最容易出事的漏洞。
    """
    first = plan_deploy([" description A"])
    same = plan_deploy([" description A"])
    different = plan_deploy([" description B"])
    assert first.content_hash == same.content_hash
    assert first.content_hash != different.content_hash
    assert len(first.content_hash) == 64

    # 空白与空行的差异不该改变哈希（规范化之后是同一份内容）
    padded = plan_deploy(["", "  description A  ", ""])
    assert padded.content_hash == first.content_hash


def test_plan_dict_is_reviewable():
    """变更单要能把预检结果原样呈现给人看（前端直接渲染它）。"""
    plan = plan_deploy(["save"])
    data = plan.as_dict()
    assert data["ok"] is False and data["lines"] == 1
    assert data["issues"][0]["severity"] == "error"
    assert data["issues"][0]["code"] == "dangerous_command"


# --------------------------------------------------------------------------
# 逐行执行结果：中止时必须能说清"设备停在哪一行"
# --------------------------------------------------------------------------
def test_rejected_plan_sends_nothing():
    """预检没过 → 一行都不发，且所有行标成 skipped（不是 failed）。"""
    plan = plan_deploy(["sysname SW-01", "save"])
    outcome = build_outcome(1, plan)
    assert outcome.status == "rejected"
    assert outcome.applied == ()
    assert {item.status for item in outcome.results} == {"skipped"}
    assert "未发送任何命令" in outcome.summary()


def test_successful_deploy_marks_every_line_applied():
    plan = plan_deploy(["interface Vlanif10", "description MGMT"])
    outcome = build_outcome(1, plan)
    assert outcome.status == "success"
    assert outcome.applied == ("interface Vlanif10", "description MGMT")
    assert outcome.stopped_at is None
    assert "共 2 行" in outcome.summary()


def test_stopped_deploy_reports_the_exact_line_and_remaining_are_skipped():
    """★ 中途中止：结论必须是 stopped，且要能说清"已生效几行、停在哪一行"。

    设备此刻是新旧混合状态——把它记成 success/failed 都会让人判断错，
    所以这一条不变量比"能不能下发"更重要。
    """
    plan = plan_deploy(["interface Vlanif10", "description MGMT", "quit", "interface Vlanif20"])
    outcome = build_outcome(1, plan, failed_index=3, error="% Invalid input at '^' marker")
    assert outcome.status == "stopped", "部分变更绝不能报成 success"
    assert outcome.applied == ("interface Vlanif10", "description MGMT")
    assert outcome.stopped_at is not None and outcome.stopped_at.index == 3
    assert [item.status for item in outcome.results] == ["ok", "ok", "failed", "skipped"]
    assert "第 3 行" in outcome.summary() and "已生效 2 行" in outcome.summary()
    assert "人工接管" in outcome.summary(), "必须提示需要人介入"


def test_outcome_dict_carries_content_hash_for_audit():
    """下发记录要能对回审批时的那份内容（哈希一致才算同一件事）。"""
    plan = plan_deploy(["description A"])
    outcome = build_outcome(7, plan)
    data = outcome.as_dict()
    assert data["device_id"] == 7
    assert data["content_hash"] == plan.content_hash
    assert data["stopped_at"] is None
    assert {item["status"] for item in data["results"]} == {"ok"}


def test_outcome_supports_no_device_for_dry_run():
    """dry_run 场景没有目标设备（device_id=None），结论照样要能成型。"""
    outcome = build_outcome(None, plan_deploy(["description A"]))
    assert outcome.as_dict()["device_id"] is None
    assert outcome.status == "success"
