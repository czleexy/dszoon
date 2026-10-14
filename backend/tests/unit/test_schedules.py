"""定时策略的纯逻辑单测：cron 校验、下次执行时间、错峰抖动、执行窗口。

这些都是**与数据库无关**的纯函数，值得单独钉死——尤其是时区与跨零点窗口这两个
地方，写错了会表现为"任务在错误的时刻跑"或"任务从来不跑"，而这类问题在测试环境
里几乎不会被发现（通常要等到生产上某个凌晨）。
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone

import pytest

from app.db.models import Schedule
from app.errors import ValidationFailed
from app.services.schedules import (
    compute_next_run,
    describe,
    in_window,
    parse_window,
    validate_fields,
)


def make_schedule(**overrides) -> Schedule:
    """构造一个不入库的 Schedule（纯函数测试不需要数据库）。"""
    data = {
        "name": "nightly",
        "kind": "cron",
        "cron": "0 2 * * *",
        "interval_minutes": None,
        "jitter_minutes": 0,
        "window_start": None,
        "window_end": None,
        "enabled": True,
        "scope": {"all_enabled": True},
        "params": {},
    }
    data.update(overrides)
    return Schedule(**data)


def to_local(value: datetime) -> datetime:
    """朴素 UTC → 本机本地时间（测试里要断言"本地几点跑"，而不依赖机器时区）。"""
    return value.replace(tzinfo=timezone.utc).astimezone()


# --------------------------------------------------------------------------
# 执行窗口
# --------------------------------------------------------------------------
def test_parse_window_accepts_hh_mm():
    assert parse_window("02:30", "04:00") == (time(2, 30), time(4, 0))


def test_parse_window_empty_means_no_limit():
    assert parse_window(None, None) == (None, None)


def test_parse_window_requires_both_ends():
    with pytest.raises(ValidationFailed):
        parse_window("02:00", None)


@pytest.mark.parametrize("bad", ["25:00", "2", "ab:cd", "02-00"])
def test_parse_window_rejects_bad_format(bad):
    with pytest.raises(ValidationFailed):
        parse_window(bad, "04:00")


def test_in_window_simple_range():
    assert in_window(time(3, 0), time(2, 0), time(4, 0))
    assert not in_window(time(5, 0), time(2, 0), time(4, 0))


def test_in_window_across_midnight():
    """★ 跨零点窗口（22:00–06:00）是运维最常见的写法。

    如果按 ``start <= t <= end`` 判断，这种窗口**永远为假**——夜间任务一次都不会跑，
    而且没有任何报错。
    """
    start, end = time(22, 0), time(6, 0)
    assert in_window(time(23, 30), start, end)
    assert in_window(time(2, 0), start, end)
    assert not in_window(time(12, 0), start, end)


# --------------------------------------------------------------------------
# 字段校验
# --------------------------------------------------------------------------
def test_validate_accepts_daily_cron():
    validate_fields(
        kind="cron", cron="0 2 * * *", interval_minutes=None,
        window_start=None, window_end=None, jitter_minutes=5,
    )


@pytest.mark.parametrize("bad", ["* * *", "0 2 * * * *", "99 2 * * *", "abc"])
def test_validate_rejects_bad_cron(bad):
    with pytest.raises(ValidationFailed):
        validate_fields(
            kind="cron", cron=bad, interval_minutes=None,
            window_start=None, window_end=None, jitter_minutes=0,
        )


def test_validate_rejects_unknown_kind():
    with pytest.raises(ValidationFailed):
        validate_fields(
            kind="every-day", cron=None, interval_minutes=None,
            window_start=None, window_end=None, jitter_minutes=0,
        )


@pytest.mark.parametrize("minutes", [0, -5, None])
def test_validate_rejects_bad_interval(minutes):
    with pytest.raises(ValidationFailed):
        validate_fields(
            kind="interval", cron=None, interval_minutes=minutes,
            window_start=None, window_end=None, jitter_minutes=0,
        )


def test_validate_rejects_window_shorter_than_jitter():
    """窗口比抖动还短 → 抖动后大概率落到窗口外，任务频繁被跳过，属于配置错误。"""
    with pytest.raises(ValidationFailed):
        validate_fields(
            kind="cron", cron="0 2 * * *", interval_minutes=None,
            window_start="02:00", window_end="02:03", jitter_minutes=10,
        )


def test_validate_rejects_huge_jitter():
    with pytest.raises(ValidationFailed):
        validate_fields(
            kind="cron", cron="0 2 * * *", interval_minutes=None,
            window_start=None, window_end=None, jitter_minutes=999,
        )


# --------------------------------------------------------------------------
# 下次执行时间
# --------------------------------------------------------------------------
def test_next_run_daily_cron_lands_on_local_two_am():
    """cron 按**服务器本地时间**解释：0 2 * * * 就是本地 02:00。

    这里断言的是本地小时数，所以与测试机器的时区无关。
    """
    schedule = make_schedule(cron="0 2 * * *", jitter_minutes=0)
    nxt = compute_next_run(schedule, after=datetime(2026, 1, 1, 0, 0, 0))
    assert nxt is not None
    local = to_local(nxt)
    assert (local.hour, local.minute) == (2, 0)


def test_next_run_interval_kind():
    schedule = make_schedule(kind="interval", cron=None, interval_minutes=30)
    after = datetime(2026, 1, 1, 0, 0, 0)
    assert compute_next_run(schedule, after=after) == datetime(2026, 1, 1, 0, 30, 0)


def test_jitter_delays_within_configured_bound():
    schedule = make_schedule(cron="0 2 * * *", jitter_minutes=5)
    nxt = compute_next_run(schedule, after=datetime(2026, 1, 1, 0, 0, 0))
    assert nxt is not None
    local = to_local(nxt)
    assert local.hour == 2
    assert 0 <= local.minute <= 5


def test_jitter_is_deterministic_with_seeded_rng():
    """注入固定随机源时结果可复现——否则这个测试本身就会变成随机失败的噪声源。"""
    schedule = make_schedule(cron="0 2 * * *", jitter_minutes=5)
    first = compute_next_run(
        schedule, after=datetime(2026, 1, 1, 0, 0, 0), rng=random.Random(42)
    )
    second = compute_next_run(
        schedule, after=datetime(2026, 1, 1, 0, 0, 0), rng=random.Random(42)
    )
    assert first == second


def test_base_next_time_is_strictly_after_a_fire_time():
    """★ 从"某个触发时刻"往后推进必须真的前进（严格晚于它）。

    ``get_next_fire_time(None, X)`` 在 X 本身符合 cron 时会返回 X —— 原来的写法就是
    这样，于是"推进"这一步原地不动，转满 200 次探测后返回 None。真实表现（第一次全量
    运行抓到）：
      * 预览接口只返回 1 个时刻（期望 3 个）；
      * 只要 cron 的下一次触发落在执行窗口之外，创建策略直接 422
        （报"永远不会有执行时刻"），窗口功能等于不可用。
    """
    schedule = make_schedule(cron="0 * * * *", jitter_minutes=0)
    first = compute_next_run(schedule, after=datetime(2026, 1, 1, 10, 0, 0))
    assert first is not None
    second = compute_next_run(schedule, after=first)
    assert second is not None and second > first, "从触发时刻往后推进没有前进"


def test_window_advances_until_inside_window():
    """第一次触发落在窗口外时，必须逐次推进直到落进窗口。

    （cron 每小时、窗口 02:00–03:00：从 00:30 起算要推进两次才会落进窗口。）
    """
    schedule = make_schedule(
        cron="0 * * * *", jitter_minutes=0, window_start="02:00", window_end="03:00"
    )
    nxt = compute_next_run(schedule, after=datetime(2026, 1, 1, 0, 30, 0))
    assert nxt is not None, "窗口内的时刻是存在的，不该返回 None"
    local = to_local(nxt)
    assert (local.hour, local.minute) == (2, 0), local


def test_window_outside_cron_returns_none():
    """cron 落在窗口外 → 在可预见的未来没有可执行时刻，返回 None。

    调用方应当把它当成"暂时不执行"而不是错误。
    """
    schedule = make_schedule(
        cron="0 2 * * *", jitter_minutes=0, window_start="03:00", window_end="04:00"
    )
    assert compute_next_run(schedule, after=datetime(2026, 1, 1, 0, 0, 0)) is None


def test_overnight_window_keeps_late_night_run():
    schedule = make_schedule(
        cron="30 23 * * *", jitter_minutes=0, window_start="22:00", window_end="06:00"
    )
    nxt = compute_next_run(schedule, after=datetime(2026, 1, 1, 0, 0, 0))
    assert nxt is not None
    assert to_local(nxt).hour == 23


def test_window_skips_to_next_occurrence_inside_window():
    """cron 02:00、窗口 01:00–05:00、抖动 5 分钟 → 应该落在窗口内而不是被跳过。"""
    schedule = make_schedule(
        cron="0 2 * * *", jitter_minutes=5, window_start="01:00", window_end="05:00"
    )
    nxt = compute_next_run(schedule, after=datetime(2026, 1, 1, 0, 0, 0))
    assert nxt is not None
    local = to_local(nxt)
    assert time(1, 0) <= local.time() <= time(5, 0)


# --------------------------------------------------------------------------
# 错过的班次不补跑（not_before）
# --------------------------------------------------------------------------
def test_not_before_skips_missed_interval_runs():
    """★ 停机 3 天后，下一次必须落在将来，而不是"从 3 天前逐班追"。

    少了这个约束，每个 tick 都会发现"下一次仍在过去"，于是连续发任务直到追上——
    几百上千个任务瞬间涌出，比不备份危害更大。
    """
    schedule = make_schedule(kind="interval", cron=None, interval_minutes=30)
    long_ago = datetime(2026, 1, 1, 0, 0, 0)
    now = datetime(2026, 1, 4, 0, 0, 0)
    nxt = compute_next_run(schedule, after=long_ago, not_before=now)
    assert nxt == now + timedelta(minutes=30)


def test_not_before_keeps_cron_alignment():
    """跳过错过的班次后仍对齐在原本的 cron 时刻上，而不是"从 now 重新计时"。"""
    schedule = make_schedule(cron="0 2 * * *", jitter_minutes=0)
    # 计划时刻是 1 月 3 日 02:00（本地），而现在已是 1 月 4 日 03:00 —— 班次已错过
    missed = datetime(2026, 1, 3, 2, 0, 0)
    now = datetime(2026, 1, 4, 3, 0, 0)
    nxt = compute_next_run(schedule, after=missed, not_before=now)
    assert nxt is not None
    local = to_local(nxt)
    assert (local.hour, local.minute) == (2, 0), "仍应落在本地 02:00 这个班次上"
    assert to_local(nxt) > to_local(now), "必须落在将来"


def test_without_not_before_it_would_return_a_past_time():
    """反证：不传 not_before 时，算出来的下一次确实可能在过去——所以调用方必须传。"""
    schedule = make_schedule(kind="interval", cron=None, interval_minutes=30)
    long_ago = datetime(2026, 1, 1, 0, 0, 0)
    nxt = compute_next_run(schedule, after=long_ago)
    assert nxt is not None and nxt < datetime(2026, 1, 4, 0, 0, 0)


# --------------------------------------------------------------------------
# 人话描述
# --------------------------------------------------------------------------
def test_describe_daily_cron():
    text = describe(make_schedule(cron="0 2 * * *", jitter_minutes=5))
    assert "每天 02:00" in text
    assert "错峰" in text


def test_describe_interval_hours():
    assert "每 2 小时" in describe(
        make_schedule(kind="interval", cron=None, interval_minutes=120)
    )


def test_describe_mentions_window():
    text = describe(
        make_schedule(cron="0 2 * * *", window_start="22:00", window_end="06:00")
    )
    assert "22:00" in text and "06:00" in text
