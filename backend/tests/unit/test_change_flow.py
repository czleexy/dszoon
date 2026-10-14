"""变更单状态机与下发闸门的单测（P4-B 的地基）。

为什么这一组值得单独钉死：下发链路的正确性几乎全在"什么时候允许往下走"上，
而真机上**永远不会复现**的分支——未知状态、终态再流转、批 A 发 B、没有基线就下发、
中止之后被追认成功——恰恰是出事之后最需要解释清楚的部分。
它们必须被穷举测试，而不是等真机撞上再来补。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.collector.deploy import (
    DEPLOY_REJECTED,
    DEPLOY_STOPPED,
    DEPLOY_SUCCESS,
    build_outcome,
    plan_deploy,
)
from app.services.deploy import (
    CHANGE_APPROVED,
    CHANGE_CANCELLED,
    CHANGE_DEPLOYING,
    CHANGE_DRAFT,
    CHANGE_EXPIRED,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    CHANGE_STOPPED,
    CHANGE_SUCCEEDED,
    CHANGE_TRANSITIONS,
    TERMINAL_CHANGE_STATES,
    ChangeView,
    can_transition,
    check_approval_allowed,
    check_deploy_allowed,
    check_submit_allowed,
    state_after_outcome,
)

NOW = datetime(2026, 8, 12, 9, 0, 0)
BENIGN_LINES = ("interface GigabitEthernet0/0/1", "description uplink-to-core")


def make_plan(lines=BENIGN_LINES):
    return plan_deploy(lines)


def make_change(**overrides) -> ChangeView:
    """一份"本来可以下发"的变更单；各用例只覆盖它的一处让它不合法。"""
    data = {
        "id": 42,
        "device_id": 7,
        "state": CHANGE_APPROVED,
        "content_hash": make_plan().content_hash,
        "requested_by": 1,
        "approved_by": 2,
        "before_snapshot_id": 100,
        "before_captured_at": NOW - timedelta(minutes=5),
        "created_at": NOW - timedelta(hours=1),
    }
    data.update(overrides)
    return ChangeView(**data)


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


# --------------------------------------------------------------------------
# 闸门：允许的情形
# --------------------------------------------------------------------------
def test_approved_change_with_baseline_is_allowed():
    assert check_deploy_allowed(make_change(), make_plan(), enabled=True, confirm=True) == []


def test_requester_may_execute_after_second_person_approves():
    """★ 刻意**不**禁止申请人执行自己提的变更。

    现场常态是"申请人提单 → 主管批准 → 申请人执行"：他熟悉这台设备和维护窗口。
    必须分开的只有**审批权**（见下面的自批用例）。这条测试存在的意义是拦住
    "以后顺手加一条 self_deploy 校验"——那会把正常流程堵死，逼出用别人账号
    点一下的绕行，反而让追责链更假。
    """
    assert check_deploy_allowed(make_change(requested_by=1), make_plan(), enabled=True, confirm=True) == []


# --------------------------------------------------------------------------
# 闸门：逐条拒绝的理由
# --------------------------------------------------------------------------
def test_disabled_switch_refuses_even_when_everything_else_is_fine():
    """默认关闭是**硬开关**：单子再完美，开关没开也不许发。"""
    issues = check_deploy_allowed(make_change(), make_plan(), enabled=False, confirm=True)
    assert "deploy_disabled" in codes(issues)


def test_missing_confirm_refuses():
    issues = check_deploy_allowed(make_change(), make_plan(), enabled=True, confirm=False)
    assert "confirm_required" in codes(issues)


def test_not_approved_refuses():
    issues = check_deploy_allowed(
        make_change(state=CHANGE_PENDING), make_plan(), enabled=True, confirm=True
    )
    assert "not_approved" in codes(issues)


def test_missing_approver_refuses():
    issues = check_deploy_allowed(
        make_change(approved_by=None), make_plan(), enabled=True, confirm=True
    )
    assert "no_approver" in codes(issues)


def test_self_approved_change_refuses_to_deploy():
    """自批过的单子即使状态是"已批准"也不许发——状态字段可以被写错，审批人不会被写对。"""
    issues = check_deploy_allowed(
        make_change(requested_by=1, approved_by=1), make_plan(), enabled=True, confirm=True
    )
    assert "self_approved" in codes(issues)


def test_content_changed_after_approval_refuses():
    """★ 批的是 A、发的是 B：批准后改了一个字，哈希就不同了。"""
    issues = check_deploy_allowed(
        make_change(content_hash="f" * 64), make_plan(), enabled=True, confirm=True
    )
    assert "content_changed" in codes(issues)


def test_dangerous_plan_refuses_even_if_hash_matches():
    """哈希一致也不够：内容本身要过预检。

    注意这条的哈希**故意与 plan 一致**——它证明"哈希校验通过"不等于"可以下发"，
    两道闸门查的是不同的东西（前者查"是不是批过的那份"，后者查"这份本身能不能发"）。
    """
    bad = make_plan(["save", "interface GigabitEthernet0/0/1"])
    assert not bad.ok, "夹具前提：save 属于危险命令"
    issues = check_deploy_allowed(
        make_change(content_hash=bad.content_hash), bad, enabled=True, confirm=True
    )
    assert "precheck_failed" in codes(issues)
    assert "content_changed" not in codes(issues)


def test_missing_baseline_refuses():
    """没有下发前的采集 = 出事时回不去，一律不发（§8.5.1 第 3 条）。"""
    issues = check_deploy_allowed(
        make_change(before_snapshot_id=None), make_plan(), enabled=True, confirm=True
    )
    assert "no_baseline" in codes(issues)


def test_baseline_older_than_the_change_request_refuses():
    """基线必须是为**这次**变更采的：拿三天前的采集当回滚点，回滚会滚错版本。"""
    issues = check_deploy_allowed(
        make_change(before_captured_at=NOW - timedelta(days=3)),
        make_plan(),
        enabled=True,
        confirm=True,
    )
    assert "stale_baseline" in codes(issues)


def test_unknown_state_refuses_to_deploy():
    """库里被人手工改出一个没见过的状态时，必须拒绝而不是"看起来不像终态就放行"。"""
    issues = check_deploy_allowed(
        make_change(state="who-knows"), make_plan(), enabled=True, confirm=True
    )
    assert "not_approved" in codes(issues)


def test_all_refusals_are_errors_not_warnings():
    """拒绝必须是 error 级：warning 会被上层当成"可以继续"。"""
    issues = check_deploy_allowed(
        make_change(state=CHANGE_PENDING, approved_by=None, before_snapshot_id=None),
        make_plan(["save"]),
        enabled=False,
        confirm=False,
    )
    assert issues and all(issue.is_error for issue in issues)
    assert len(codes(issues)) >= 6


# --------------------------------------------------------------------------
# 审批闸门
# --------------------------------------------------------------------------
def test_self_approval_refused_by_default():
    issues = check_approval_allowed(
        make_change(state=CHANGE_PENDING, requested_by=1), approver_id=1
    )
    assert "self_approval" in codes(issues)


def test_self_approval_allowed_only_when_explicitly_enabled():
    """单人值守环境需要一条明路，但不能是默认值。"""
    issues = check_approval_allowed(
        make_change(state=CHANGE_PENDING, requested_by=1),
        approver_id=1,
        allow_self_approval=True,
    )
    assert issues == []


def test_second_person_approval_is_accepted():
    assert check_approval_allowed(
        make_change(state=CHANGE_PENDING, requested_by=1), approver_id=2
    ) == []


def test_approval_requires_pending_state():
    for state in (CHANGE_DRAFT, CHANGE_APPROVED, CHANGE_SUCCEEDED, CHANGE_CANCELLED):
        issues = check_approval_allowed(make_change(state=state), approver_id=2)
        assert "not_pending" in codes(issues), state


def test_submit_requires_clean_precheck():
    assert check_submit_allowed(make_plan()) == []
    issues = check_submit_allowed(make_plan(["reboot"]))
    assert "precheck_failed" in codes(issues)


# --------------------------------------------------------------------------
# 状态迁移表本身
# --------------------------------------------------------------------------
def test_happy_path_walks_through_the_whole_chain():
    chain = [CHANGE_DRAFT, CHANGE_PENDING, CHANGE_APPROVED, CHANGE_DEPLOYING, CHANGE_SUCCEEDED]
    for current, target in zip(chain, chain[1:], strict=False):
        assert can_transition(current, target), f"{current} → {target}"
    assert CHANGE_SUCCEEDED in TERMINAL_CHANGE_STATES


def test_terminal_states_accept_nothing():
    """终态必须一动不动。驳回后想改内容请**新建**变更单——
    就地复活会把"谁在什么时候驳回了什么"这件事抹掉。"""
    assert CHANGE_REJECTED in TERMINAL_CHANGE_STATES, "驳回是终态：改完内容要新建一张单子"
    for state in TERMINAL_CHANGE_STATES:
        assert CHANGE_TRANSITIONS[state] == (), state
        for target in CHANGE_TRANSITIONS:
            assert not can_transition(state, target), f"{state} → {target}"


def test_unknown_state_transitions_nowhere():
    assert not can_transition("who-knows", CHANGE_PENDING)
    assert not can_transition(CHANGE_DRAFT, "who-knows")


def test_stopped_cannot_resend_without_new_approval():
    """★ 中止之后设备是新旧混合状态，下一次下发必须是**重新审批**过的新决定。"""
    assert not can_transition(CHANGE_STOPPED, CHANGE_DEPLOYING)
    assert can_transition(CHANGE_STOPPED, CHANGE_PENDING)
    assert CHANGE_STOPPED not in TERMINAL_CHANGE_STATES


def test_expired_approval_cannot_be_revived_in_place():
    """内容改过 → 原审批作废 → 只能重新提交，不能"接着用旧的批准"。"""
    assert can_transition(CHANGE_APPROVED, CHANGE_EXPIRED)
    assert not can_transition(CHANGE_EXPIRED, CHANGE_DEPLOYING)
    assert can_transition(CHANGE_EXPIRED, CHANGE_PENDING)
    assert not can_transition(CHANGE_EXPIRED, CHANGE_APPROVED)


def test_draft_cannot_jump_straight_to_deploying():
    assert not can_transition(CHANGE_DRAFT, CHANGE_DEPLOYING)
    assert not can_transition(CHANGE_PENDING, CHANGE_DEPLOYING)


# --------------------------------------------------------------------------
# 下发结论 → 变更单状态
# --------------------------------------------------------------------------
def test_outcome_mapping_needs_the_deploying_state():
    assert state_after_outcome(CHANGE_DEPLOYING, DEPLOY_SUCCESS) == CHANGE_SUCCEEDED
    assert state_after_outcome(CHANGE_DEPLOYING, DEPLOY_STOPPED) == CHANGE_STOPPED
    assert state_after_outcome(CHANGE_DEPLOYING, DEPLOY_REJECTED) == CHANGE_REJECTED
    # 没进"下发中"就不能冒出"已生效"——状态机不允许跳步
    assert state_after_outcome(CHANGE_APPROVED, DEPLOY_SUCCESS) is None


def test_stopped_outcome_can_never_be_recorded_as_success():
    """★ 这条是整个 P4 最要紧的一条断言。

    真实的中止结论经过 build_outcome 之后，唯一被接受的状态是 ``stopped``；
    任何"追认成功"的写法都必须返回 None，让调用方没法把部分变更记成完成。
    """
    plan = make_plan()
    outcome = build_outcome(7, plan, failed_index=2, error="% Invalid input detected")
    assert outcome.status == DEPLOY_STOPPED
    target = state_after_outcome(CHANGE_DEPLOYING, outcome.status)
    assert target == CHANGE_STOPPED
    assert target != CHANGE_SUCCEEDED
    assert state_after_outcome(CHANGE_STOPPED, DEPLOY_SUCCESS) is None


def test_unknown_outcome_status_maps_to_nothing():
    assert state_after_outcome(CHANGE_DEPLOYING, "partial") is None
    assert state_after_outcome(CHANGE_DEPLOYING, "") is None
