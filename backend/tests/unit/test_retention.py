"""保留策略的纯逻辑单测：哪些版本该删、哪些必须留。

``_select_prunable`` 是私有函数，但**直接测它是有意的**：这段逻辑决定"历史版本
留不留得住"，删错一次就是不可恢复的数据丢失，而它又完全与数据库无关，是最适合
用单测钉死的地方。

断言风格：全部指明"删掉的是第几个版本"，而不是"至少还剩几个"。模糊断言会漏掉
"删错了一个"这类 bug——而那恰恰是这里最需要防的。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import ConfigSnapshot, RetentionPolicy
from app.services.retention import DEFAULT_POLICY, _select_prunable

NOW = datetime(2026, 6, 1, 12, 0, 0)


def make_policy(**overrides) -> RetentionPolicy:
    data = dict(DEFAULT_POLICY)
    data.update(overrides)
    return RetentionPolicy(id=1, **data)


def make_snapshot(index: int, *, days_ago: int, sha: str | None = None) -> ConfigSnapshot:
    value = sha or f"{index:064d}"
    return ConfigSnapshot(
        device_id=1,
        sha256=value,
        normalized_sha256=value,
        object_key=f"{value[:2]}/{value}.cfg",
        captured_at=NOW - timedelta(days=days_ago),
        size_bytes=100,
        line_count=10,
    )


def build(*specs: tuple[int, str]) -> list[ConfigSnapshot]:
    """``build((10, "x"), (0, "x"))`` → 两条记录：10 天前内容 x、今天内容 x。

    返回按时间**升序**（旧 → 新），与 ``_select_prunable`` 的前置条件一致。

    两个字段各司其职，别混：
    * ``sha256`` 用下标生成 → 唯一，便于精确断言"删掉的是第几个版本"；
    * ``normalized_sha256`` 用 spec 里的 content → **它才决定"内容变没变"**。
    """
    ordered = sorted(specs, key=lambda item: -item[0])  # days_ago 大的更旧
    snapshots: list[ConfigSnapshot] = []
    for index, (days_ago, content) in enumerate(ordered):
        snapshot = make_snapshot(index, days_ago=days_ago)
        snapshot.normalized_sha256 = content
        snapshots.append(snapshot)
    return snapshots


def idx(*indexes: int) -> list[str]:
    """把"第几个版本"翻译成那个版本的 sha256（用于断言被删清单）。"""
    return [f"{index:064d}" for index in indexes]


def doomed_hashes(snapshots, policy) -> list[str]:
    return [s.sha256 for s in _select_prunable(snapshots, policy, NOW)]


def kept_hashes(snapshots, policy) -> set[str]:
    doomed = {s.sha256 for s in _select_prunable(snapshots, policy, NOW)}
    return {s.sha256 for s in snapshots} - doomed


# --------------------------------------------------------------------------
def test_empty_history_is_safe():
    assert _select_prunable([], make_policy(), NOW) == []


def test_short_history_is_never_pruned():
    """★ 只有 2 个版本、且很旧 —— 也必须都留下。

    清理最怕的就是"把唯一的历史基线删了"，导致再也无法回滚到上一版。
    """
    snapshots = build((400, "old"), (300, "new"))
    assert _select_prunable(snapshots, make_policy(keep_min_versions=2), NOW) == []


def test_keeps_oldest_and_newest_always():
    """最早一版（首次采集基线）与最新一版永久保留——即使保留期设成 0。"""
    snapshots = build((400, "a"), (300, "b"), (200, "c"), (100, "d"), (5, "e"))
    policy = make_policy(
        keep_changed_days=0, keep_unchanged_daily_days=0, keep_min_versions=1
    )
    kept = kept_hashes(snapshots, policy)
    assert snapshots[0].sha256 in kept, "最早一版必须保留（原始基线）"
    assert snapshots[-1].sha256 in kept, "最新一版必须保留（当前配置）"
    assert doomed_hashes(snapshots, policy) == idx(1, 2, 3)


def test_same_day_unchanged_versions_fold_to_one():
    """同一天内内容未变的多次采集 → 只留一个。

    这正是"每天巡检 1000 台、配置其实没动"这个量级问题的解药：
    同一台设备一天跑多次，只占用 1 个版本。
    """
    snapshots = build(*[(0, "same")] * 6)
    policy = make_policy(keep_unchanged_daily_days=90, keep_min_versions=2)
    # 下标 0（最早版本，同时被当作"变更"）与 4、5（最新 2 个）受保护；
    # 当天代表由**第一个未变更版本**（下标 1）认领 → 剩下 2、3 被折叠
    assert doomed_hashes(snapshots, policy) == idx(2, 3)


def test_changed_versions_survive_forever_by_default():
    """每次内容都不同 → 默认（keep_changed_days=None）全部保留。

    配置变更是审计证据，"什么时候改的"本身就是信息，不该被清理掉。
    """
    snapshots = build((300, "a"), (200, "b"), (100, "c"), (10, "d"))
    policy = make_policy(keep_changed_days=None, keep_min_versions=1)
    assert _select_prunable(snapshots, policy, NOW) == []


def test_changed_versions_pruned_when_older_than_limit():
    """配置变更版本也会过期——但始终受 keep_min_versions 保护。"""
    snapshots = build((200, "a"), (150, "b"), (100, "c"), (10, "d"))
    policy = make_policy(
        keep_changed_days=30, keep_unchanged_daily_days=30, keep_min_versions=2
    )
    # 下标 0（最早）与 2、3（最新 2 个）受保护；只有下标 1（150 天前）超期且不受保护
    assert doomed_hashes(snapshots, policy) == idx(1)


def test_unchanged_versions_expire_after_daily_window():
    """内容一直没变时，只有"每个自然日一个"能在窗口内保留。"""
    snapshots = build((400, "same"), (300, "same"), (200, "same"), (100, "same"), (5, "same"))
    policy = make_policy(
        keep_changed_days=None, keep_unchanged_daily_days=90, keep_min_versions=2
    )
    # 下标 3、4 受 keep_min_versions 保护；下标 0 是最早版本；剩下 1、2 超窗被删
    assert doomed_hashes(snapshots, policy) == idx(1, 2)


def test_daily_window_keeps_one_representative_per_day():
    """内容一直没变、每天采多次 → 每天恰好留一个。"""
    # 升序后：10 天前 ×2、3 天前 ×2、今天 ×2，内容全都相同
    snapshots = build((0, "same"), (0, "same"), (3, "same"), (3, "same"), (10, "same"), (10, "same"))
    policy = make_policy(keep_unchanged_daily_days=90, keep_min_versions=1)
    # 下标 0（最早）与 5（最新）受保护；10 天前留 1 个(下标1)、3 天前留 1 个(下标2)、
    # 今天留 1 个(下标4) → 只有"3 天前的第二个"(下标3)该删
    assert doomed_hashes(snapshots, policy) == idx(3)


def test_changed_versions_on_same_day_are_not_folded():
    """同一天内容有变 → 每个变更版本都保留（"按天折叠"只作用于未变更版本）。"""
    snapshots = build((0, "e"), (0, "d"), (0, "c"), (0, "b"), (0, "a"))
    policy = make_policy(keep_unchanged_daily_days=90, keep_min_versions=1)
    assert _select_prunable(snapshots, policy, NOW) == []


def test_normalized_hash_drives_change_detection():
    """判断"变没变"用**归一化后的 sha**：只改了时间戳的采集不算变更。

    这点很关键——设备每次回显都带新的时间戳/运行时长，若按原文 sha 判断，
    每次巡检都会被当成"配置变更"，保留策略就完全失效了。
    """
    older = make_snapshot(0, days_ago=20, sha="raw-a")
    middle = make_snapshot(1, days_ago=15, sha="raw-b")
    newest = make_snapshot(2, days_ago=5, sha="raw-c")
    older.normalized_sha256 = "same"
    middle.normalized_sha256 = "same"  # 与上一版归一化后一致 → 未变更
    newest.normalized_sha256 = "different"

    policy = make_policy(
        keep_changed_days=None, keep_unchanged_daily_days=1, keep_min_versions=1
    )
    doomed = _select_prunable([older, middle, newest], policy, NOW)
    # middle 被判为"未变更"且超出 1 天的按天窗口 → 删除。
    # 若它被误判成"变更版本"，keep_changed_days=None 会把它永久保留 —— 所以这条断言有区分度
    assert [s.sha256 for s in doomed] == ["raw-b"]


@pytest.mark.parametrize("keep_days", [0, 1, 30, 90, 365])
def test_folding_never_touches_the_newest(keep_days):
    """无论保留期多短，最新一版永远保留——它是"当前配置"。"""
    snapshots = build((500, "a"), (400, "b"), (300, "c"))
    policy = make_policy(
        keep_changed_days=keep_days,
        keep_unchanged_daily_days=keep_days,
        keep_min_versions=1,
    )
    assert snapshots[-1].sha256 in kept_hashes(snapshots, policy)


def test_prune_session_logs_only_removes_expired_files(tmp_path):
    """会话日志清理：只删过期的，且**按文件 mtime** 判断。

    为什么按 mtime 而不是路径里的时间戳：路径格式改过、也可能被人工改名，
    mtime 更可靠。这条测试同时钉住"没过期的日志不会被误删"——会话日志是排障
    时唯一的原始证据，删早了就等于没有。
    """
    import os
    import time as time_module

    from app.services.blobs import BlobStore

    blobs = BlobStore(tmp_path / "objects")
    fresh = blobs.put_session_log("HW-CORE-01", "20260601-020000", "会话日志：新的\n")
    stale = blobs.put_session_log("HW-CORE-02", "20260101-020000", "会话日志：旧的\n")
    # put_session_log 返回的是**相对 data_dir** 的路径（形如 sessions/<设备>/<时间戳>.log），
    # 所以绝对路径要挂在它的上一级，不能直接拼到 sessions_root() 下面（会变成 sessions/sessions/…）
    base = blobs.sessions_root().parent
    old_time = time_module.time() - 30 * 86400
    os.utime(base / stale, (old_time, old_time))
    # 把新的那份显式设成"刚刚写入"：不让测试依赖文件系统的写入时间精度
    now_time = time_module.time()
    os.utime(base / fresh, (now_time, now_time))

    assert blobs.prune_session_logs(7) == 1, "只应删掉 30 天前的那一个"
    assert not (base / stale).is_file(), "过期日志应当被删除"
    assert (base / fresh).is_file(), "★ 没过期的日志绝不能被删"

    # 0 或不设保留期 = 不清理（而不是"全删"）
    assert blobs.prune_session_logs(0) == 0
    assert (base / fresh).is_file()


def test_prune_unreferenced_spares_freshly_written_blobs(tmp_path):
    """★ 刚写下的无主正文不能被回收。

    采集的落盘顺序是"先写正文文件 → 再插快照行（同一事务）"：在大批量任务跑到
    一半时触发清理，会看到"文件已存在、但还没有任何行引用它"——那一刻它看上去
    正是"没人引用的垃圾"。删掉它等于把一个**即将写库的版本**弄坏（界面上表现为
    某个版本点开就是"归档对象不存在"）。所以回收要留宽限期。
    """
    import os
    import time as time_module

    from app.services.blobs import BlobStore

    blobs = BlobStore(tmp_path / "objects")
    fresh = blobs.put_text("hostname FRESH\n")
    stale = blobs.put_text("hostname STALE\n")
    old = time_module.time() - 2 * 3600
    os.utime(stale.path, (old, old))

    removed, freed = blobs.prune_unreferenced(set(), min_age_seconds=3600)
    assert removed == 1, "只应回收超过宽限期的那个"
    assert freed == stale.size_bytes
    assert fresh.path.is_file(), "刚写下的正文必须留着——它可能是正在写库的那个版本"
    assert not stale.path.is_file()

    # 不给宽限期时（人工确认过的场景）依然能清干净
    removed_again, _ = blobs.prune_unreferenced(set())
    assert removed_again == 1
    assert not fresh.path.is_file()
