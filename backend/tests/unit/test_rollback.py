"""回滚目标选择的单测（P4-C 的地基）。

为什么这一条值得单独钉死：回滚是"用一个已知好状态覆盖当前状态"，如果基线选错了
（例如选了一个内容残缺的版本），**回滚这个动作本身会把设备改坏**——比不回滚更糟。
所以这里的断言重点不是"能选出来"，而是"该排除的都排除了，且理由能说清"。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.deploy import RollbackCandidate, select_rollback_target

NOW = datetime(2026, 8, 10, 12, 0, 0)


def candidate(snapshot_id: int, *, days_ago: int, status: str | None = "success", issues=()):
    return RollbackCandidate(
        snapshot_id=snapshot_id,
        captured_at=NOW - timedelta(days=days_ago),
        run_status=status,
        issue_codes=tuple(issues),
        line_count=61,
        sha256=f"{snapshot_id:064d}",
    )


def test_picks_the_most_recent_trusted_version_excluding_current():
    """标准情形：当前是第 100 版，回滚目标是上一个可信版本（第 99 版）。"""
    decision = select_rollback_target(
        [
            candidate(100, days_ago=0),
            candidate(99, days_ago=1),
            candidate(98, days_ago=2),
        ],
        current_snapshot_id=100,
    )
    assert decision.ok and decision.target is not None
    assert decision.target.snapshot_id == 99
    assert decision.skipped == [(100, "这就是当前版本")]


def test_skips_untrusted_versions_and_explains_why():
    """★ 最新一版内容残缺时必须跳过它，并说明原因（否则界面会被当成工具出错）。"""
    decision = select_rollback_target(
        [
            candidate(103, days_ago=0, issues=["integrity_error"]),
            candidate(102, days_ago=1, status="timeout"),
            candidate(101, days_ago=2, status="unchanged"),
            candidate(100, days_ago=3),
        ]
    )
    assert decision.target is not None and decision.target.snapshot_id == 100
    reasons = dict(decision.skipped)
    assert "完整性" in reasons[103]
    assert "timeout" in reasons[102]
    # ★ 理由要给出**实际状态**而不是笼统的"不是成功"：unchanged 不是失败，
    #   是"配置没变"。写成"不是成功"会让运维以为那次采集出错了。
    assert "unchanged" in reasons[101] and "无变更" in reasons[101]


def test_unchanged_is_not_a_rollback_target():
    """unchanged 确认的是"旧状态还在"，不是一个新产生的可用状态。"""
    decision = select_rollback_target(
        [candidate(2, days_ago=0, status="unchanged"), candidate(1, days_ago=5)]
    )
    assert decision.target is not None and decision.target.snapshot_id == 1


def test_line_wrap_warning_also_disqualifies():
    """可能被终端折行劈开的版本同样不可信——回滚一份被劈开的配置会直接改坏设备。"""
    decision = select_rollback_target(
        [candidate(5, days_ago=0, issues=["possible_line_wrap"]), candidate(4, days_ago=1)]
    )
    assert decision.target is not None and decision.target.snapshot_id == 4


def test_no_candidates_at_all():
    decision = select_rollback_target([])
    assert not decision.ok
    assert decision.reason == "该设备还没有任何配置版本，无法回滚"
    assert decision.as_dict()["target_snapshot_id"] is None


def test_all_candidates_unusable():
    decision = select_rollback_target(
        [
            candidate(2, days_ago=0, status="auth_failed"),
            candidate(1, days_ago=1, issues=["device_error"]),
        ]
    )
    assert not decision.ok
    assert "没有可用的历史版本" in (decision.reason or "")
    assert len(decision.skipped) == 2


def test_without_current_snapshot_the_latest_trusted_wins():
    """不排除任何版本时（手工指定基准），取最新可信版本。"""
    decision = select_rollback_target(
        [candidate(9, days_ago=0), candidate(8, days_ago=1)], current_snapshot_id=None
    )
    assert decision.target is not None and decision.target.snapshot_id == 9


def test_decision_dict_is_reviewable():
    """界面要能把"目标 + 跳过了谁 + 为什么"一次展示出来。"""
    decision = select_rollback_target(
        [candidate(2, days_ago=0, issues=["integrity_error"]), candidate(1, days_ago=1)],
        current_snapshot_id=1,
    )
    data = decision.as_dict()
    assert data["ok"] is False, "唯一可信的版本正是当前版本 → 没有可回滚的目标"
    assert data["target_snapshot_id"] is None
    assert {item["snapshot_id"] for item in data["skipped"]} == {1, 2}
    assert data["reason"]
