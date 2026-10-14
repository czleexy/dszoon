"""定时策略：cron 校验、下次执行时间、错峰抖动、执行窗口。

**为什么用 APScheduler 的 ``CronTrigger`` 而不是自己写 cron 解析**：字段边界、
``mon-fri`` 别名、``*/5`` 步长、跨月跨年、闰年……自己写必错，而且错了会在
"某个季度末的凌晨"这种地方才暴露。这里只借它做**校验与下次时间计算**，
真正的调度器接入在 ``app/scheduler``。

**时区语义（重要）**：cron 表达式与执行窗口按**服务器本地时间**解释——运维写
``0 2 * * *`` 指的是本地凌晨 2 点。数据库里统一存朴素 UTC（见 ``app.db.base``），
所以出库进库都要转换。前端展示时再转成本地时间。
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import utcnow
from ..db.models import Schedule
from ..errors import Conflict, NotFound, ValidationFailed

try:  # APScheduler 是 P2 才引入的依赖，缺失时给一句可执行的修复提示
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover
    CronTrigger = None  # type: ignore[assignment]

__all__ = [
    "compute_next_run",
    "create_schedule",
    "delete_schedule",
    "describe",
    "get_schedule",
    "in_window",
    "list_schedules",
    "parse_window",
    "validate_fields",
]

#: 计算"窗口内下一次"时最多向后尝试多少个基准时刻
_MAX_WINDOW_PROBES = 200


def _require_apscheduler() -> None:
    if CronTrigger is None:  # pragma: no cover
        raise ValidationFailed(
            "未安装 APScheduler，无法解析 cron 表达式。"
            '请执行：pip install -e ".[web]"'
        )


def _to_local(naive_utc: datetime) -> datetime:
    """朴素 UTC → 本机本地时间。"""
    return naive_utc.replace(tzinfo=timezone.utc).astimezone()


def _to_utc_naive(value: datetime) -> datetime:
    """任意时区 → 朴素 UTC（入库格式）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def parse_window(start: str | None, end: str | None) -> tuple[time | None, time | None]:
    """解析 ``"HH:MM"`` 形式的执行窗口。两个都不填 = 不限窗口。"""
    if not start and not end:
        return None, None
    if bool(start) != bool(end):
        raise ValidationFailed("执行窗口必须同时提供开始与结束时间，或都不提供")
    parsed: list[time] = []
    for label, value in (("开始", start), ("结束", end)):
        try:
            hour, minute = str(value).split(":")
            parsed.append(time(hour=int(hour), minute=int(minute)))
        except (ValueError, TypeError) as exc:
            raise ValidationFailed(
                f"执行窗口{label}时间格式应为 HH:MM（24 小时制），收到 {value!r}"
            ) from exc
    return parsed[0], parsed[1]


def in_window(moment: time, start: time | None, end: time | None) -> bool:
    """``moment`` 是否落在窗口内。

    **支持跨零点窗口**（如 22:00–06:00）：这是运维最常见的写法（夜间窗口），
    如果按 ``start <= t <= end`` 判断会永远为假。
    """
    if start is None or end is None:
        return True
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end


def validate_fields(
    *,
    kind: str,
    cron: str | None,
    interval_minutes: int | None,
    window_start: str | None,
    window_end: str | None,
    jitter_minutes: int,
) -> None:
    """校验一个定时策略的字段组合是否自洽。创建/更新接口都调它。"""
    if kind not in ("cron", "interval"):
        raise ValidationFailed(f"调度类型只能是 cron 或 interval，收到 {kind!r}")

    if kind == "cron":
        if not cron:
            raise ValidationFailed("cron 类型必须提供 cron 表达式")
        _require_apscheduler()
        try:
            CronTrigger.from_crontab(cron)
        except ValueError as exc:
            raise ValidationFailed(
                f"cron 表达式不合法：{cron!r}（应为「分 时 日 月 周」5 段，例如 0 2 * * *）：{exc}"
            ) from exc
    else:
        if not interval_minutes or interval_minutes < 1:
            raise ValidationFailed("interval 类型必须提供 ≥1 的间隔分钟数")
        if interval_minutes > 60 * 24 * 30:
            raise ValidationFailed("间隔不能超过 30 天（更大的周期请用 cron）")

    if jitter_minutes < 0 or jitter_minutes > 120:
        raise ValidationFailed("错峰抖动应在 0~120 分钟之间")

    parse_window(window_start, window_end)

    # 窗口比抖动还短 → 抖动后大概率落到窗口外，等于任务经常被跳过，属于配置错误
    start, end = parse_window(window_start, window_end)
    if start is not None and end is not None and jitter_minutes:
        span = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
        if span < 0:
            span += 24 * 60
        if span <= jitter_minutes and span < 24 * 60:
            raise ValidationFailed(
                f"执行窗口只有 {span} 分钟，却设置了 {jitter_minutes} 分钟错峰抖动，"
                "任务大概率会被跳过；请缩短抖动或放宽窗口"
            )


# --------------------------------------------------------------------------
# 下次执行时间
# --------------------------------------------------------------------------
def compute_next_run(
    schedule: Schedule,
    *,
    after: datetime | None = None,
    rng: random.Random | None = None,
    not_before: datetime | None = None,
) -> datetime | None:
    """算下一次执行时间（朴素 UTC），已含错峰抖动与窗口过滤。

    :param after: 从哪个时刻往后找。**应当传"上一次的计划时刻"而不是 now**，
        这样 cron 的固定时刻不会被 tick 延迟推着越跑越晚。
    :param not_before: 早于该时刻的班次直接跳过（**错过的班次不补跑**）。

        这一条很关键：如果采集机停机 3 天，按"从计划时刻往后推"算出来的下一次
        仍然在过去，于是每个 tick 都会触发一次任务，直到追上为止——几百上千个
        任务瞬间涌出，比不备份危害更大。加上它之后，停机期间错过的班次被丢掉，
        只保留"下一次将来临的时刻"，同时又不破坏原有的节拍对齐。

    :return: ``None`` 表示"在可预见的未来没有任何一次能落在窗口内"——调用方应当把
        它当成"暂时不会执行"而不是错误（例如变更冻结期窗口设得很窄）。
    """
    now = after or utcnow()
    rng = rng or random.Random()
    start, end = parse_window(schedule.window_start, schedule.window_end)

    base = _base_next_time(schedule, now)
    for _ in range(_MAX_WINDOW_PROBES):
        if base is None:
            return None
        candidate = base
        if schedule.jitter_minutes:
            # 每次算都重新抖动：这样"错峰"是真的分散，而不是固定偏移
            candidate = candidate + timedelta(
                minutes=rng.uniform(0, schedule.jitter_minutes)
            )

        if not_before is not None and candidate <= not_before:
            # 这一班已经错过了 → 直接以 not_before 为基准跳到下一班（不补跑）。
            # 用 max() 而不是逐班前进：停机 3 天后也能一次跳到未来，探测次数恒定。
            base = _base_next_time(schedule, max(base, not_before))
            continue

        if in_window(_to_local(candidate).time(), start, end):
            return _to_utc_naive(candidate)
        base = _base_next_time(schedule, base)
    return None


def _base_next_time(schedule: Schedule, after: datetime) -> datetime | None:
    if schedule.kind == "interval":
        minutes = schedule.interval_minutes or 0
        if minutes <= 0:
            return None
        return after + timedelta(minutes=minutes)

    _require_apscheduler()
    assert CronTrigger is not None  # _require_apscheduler 已保证
    trigger = CronTrigger.from_crontab(schedule.cron or "")
    local_after = _to_local(after)
    # ★ previous_fire_time 必须传，而且与 now 传同一个值。
    #   get_next_fire_time(None, X) 是**包含 X** 的语义（X 本身符合 cron 时就返回 X），
    #   于是"从某个触发时刻往后推进"这一步会原地不动：窗口推进、预览、tick 在整点
    #   触发时的下一次时间计算都会转满 _MAX_WINDOW_PROBES 次探测后返回 None。
    #   传了它之后 APScheduler 会把起点挪到 X+1µs，即严格晚于 X。
    fire = trigger.get_next_fire_time(local_after, local_after)
    if fire is None:
        return None
    return _to_utc_naive(fire)


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def describe(schedule: Schedule) -> str:
    """人话描述，给界面用（比让运维读 cron 友好）。"""
    if schedule.kind == "interval":
        minutes = schedule.interval_minutes or 0
        if minutes % 1440 == 0:
            text = f"每 {minutes // 1440} 天"
        elif minutes % 60 == 0:
            text = f"每 {minutes // 60} 小时"
        else:
            text = f"每 {minutes} 分钟"
    else:
        fields = (schedule.cron or "").split()
        if len(fields) != 5:
            text = f"cron {schedule.cron!r}"
        else:
            minute, hour, dom, month, dow = fields
            clock = (
                f"{hour.zfill(2)}:{minute.zfill(2)}"
                if hour.isdigit() and minute.isdigit()
                else f"{hour}:{minute}"
            )
            if (dom, month, dow) == ("*", "*", "*"):
                text = f"每天 {clock}"
            else:
                extras: list[str] = []
                if month != "*":
                    extras.append(f"{month} 月")
                if dom != "*":
                    extras.append(f"{dom} 日")
                if dow != "*":
                    extras.append(f"周字段 {dow}")
                text = f"{'、'.join(extras)} {clock}"

    if schedule.jitter_minutes:
        text += f"，错峰 ±{schedule.jitter_minutes} 分钟"
    if schedule.window_start and schedule.window_end:
        text += f"，仅限 {schedule.window_start}–{schedule.window_end} 执行"
    return text


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
def _scope_of(payload: dict[str, Any]) -> dict[str, Any]:
    """从 BackupRequest 风格的入参里抽出范围部分。"""
    scope = {
        key: payload[key]
        for key in ("device_ids", "group_id", "site", "vendor", "include_disabled", "all_enabled")
        if payload.get(key) not in (None, [], False)
    }
    if not scope:
        raise ValidationFailed("定时任务必须指定备份范围（设备、分组、站点、厂商或全部启用设备）")
    return scope


def _params_of(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("precheck", "retries", "name")
        if payload.get(key) is not None
    }


async def list_schedules(session: AsyncSession) -> list[Schedule]:
    result = await session.execute(select(Schedule).order_by(Schedule.name))
    return list(result.scalars().all())


async def get_schedule(session: AsyncSession, schedule_id: int) -> Schedule:
    schedule = await session.get(Schedule, schedule_id)
    if schedule is None:
        raise NotFound(f"定时策略不存在：id={schedule_id}")
    return schedule


def _next_run_or_fail(schedule: Schedule) -> datetime | None:
    """算下次执行时间；**启用中**的策略若"永远不会有下次执行"直接报 422。

    两种真实会遇到的配置错误都会走到这里：

    * cron 与执行窗口永不相交（例如窗口 02:00–02:05、cron ``0 3 * * *``）——
      每次探测都落在窗口外，200 次探测后放弃；
    * cron 的日期根本不存在（例如 ``0 0 31 2 *``，2 月没有 31 日）。

    不做这个检查的后果很隐蔽：策略会被"建成功"，界面上显示"下次执行：-"（或者根本
    没人注意这一列），然后**永远不跑**。等到有人发现"这个策略好像没执行过"时，
    已经过去几周了。
    """
    if not schedule.enabled:
        return None
    nxt = compute_next_run(schedule)
    if nxt is None:
        window = (
            f"{schedule.window_start}–{schedule.window_end}"
            if schedule.window_start and schedule.window_end
            else "（未限制）"
        )
        raise ValidationFailed(
            "这个策略永远不会有执行时刻：cron "
            f"{schedule.cron or schedule.interval_minutes} 与执行窗口 {window} "
            f"在未来 {_MAX_WINDOW_PROBES} 次触发内没有交集。"
            "请放宽执行窗口，或把 cron 的时刻挪进窗口。"
        )
    return nxt


async def create_schedule(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    created_by: str | None = None,
) -> Schedule:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValidationFailed("定时策略必须有名称")
    existing = await session.scalar(select(Schedule).where(Schedule.name == name))
    if existing is not None:
        raise Conflict(f"定时策略名称已存在：{name}")

    kind = payload.get("kind") or "cron"
    validate_fields(
        kind=kind,
        cron=payload.get("cron"),
        interval_minutes=payload.get("interval_minutes"),
        window_start=payload.get("window_start"),
        window_end=payload.get("window_end"),
        jitter_minutes=int(payload.get("jitter_minutes") or 0),
    )

    schedule = Schedule(
        name=name,
        enabled=bool(payload.get("enabled", True)),
        kind=kind,
        cron=payload.get("cron"),
        interval_minutes=payload.get("interval_minutes"),
        scope=_scope_of(payload),
        params=_params_of(payload),
        jitter_minutes=int(payload.get("jitter_minutes") or 0),
        window_start=payload.get("window_start"),
        window_end=payload.get("window_end"),
        created_by=created_by,
    )
    schedule.next_run_at = _next_run_or_fail(schedule)
    session.add(schedule)
    await session.flush()
    return schedule


async def update_schedule(
    session: AsyncSession, schedule: Schedule, payload: dict[str, Any]
) -> Schedule:
    if "name" in payload and payload["name"] and payload["name"] != schedule.name:
        existing = await session.scalar(
            select(Schedule).where(Schedule.name == payload["name"])
        )
        if existing is not None:
            raise Conflict(f"定时策略名称已存在：{payload['name']}")
        schedule.name = payload["name"]

    for field in ("kind", "cron", "interval_minutes", "window_start", "window_end"):
        if field in payload:
            setattr(schedule, field, payload[field])
    if "jitter_minutes" in payload and payload["jitter_minutes"] is not None:
        schedule.jitter_minutes = int(payload["jitter_minutes"])
    if "enabled" in payload and payload["enabled"] is not None:
        schedule.enabled = bool(payload["enabled"])
    if any(key in payload for key in ("device_ids", "group_id", "site", "vendor", "all_enabled")):
        schedule.scope = _scope_of(payload)
    if any(key in payload for key in ("precheck", "retries")):
        schedule.params = _params_of(payload)

    validate_fields(
        kind=schedule.kind,
        cron=schedule.cron,
        interval_minutes=schedule.interval_minutes,
        window_start=schedule.window_start,
        window_end=schedule.window_end,
        jitter_minutes=schedule.jitter_minutes,
    )
    schedule.next_run_at = _next_run_or_fail(schedule)
    await session.flush()
    return schedule


async def delete_schedule(session: AsyncSession, schedule: Schedule) -> None:
    await session.delete(schedule)
    await session.flush()
