"""首页看板的聚合（P3 收尾）。

为什么要单独做一个聚合接口，而不是让前端连着发五六个请求：
除了慢，更要紧的是**那五六个数字不是同一时刻的**。运维看到"设备 100 台、失败 3 台、
成功率 99%"互相对不上时，第一反应不是"读数的时刻差了几秒"，而是**不敢信这个工具**。
在一个请求里算完，至少这批数字是同一时刻的。

口味上的取舍：这里只做**能一眼看懂的汇总**，不往里面塞图表数据。看板上的每个数字
都要能回答"我接下来该干什么"——"最近 24 小时失败数""当前有几张变更单在等审批"是这类；
"过去 30 天耗时分位"不是，它属于监控系统（``/api/v1/metrics``）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import utcnow
from ..db.models import BackupRun, ChangeRequest, ComplianceResult, ConfigSnapshot, Device
from .blobs import BlobStore

__all__ = ["GOOD_RUN_STATUSES", "count_by", "overview"]

#: "这台设备这次是好的"的状态。``unchanged`` 也是好的——配置没变不等于采集失败，
#: 把它算进失败率是最常见的一种误报，会让运维开始无视这个数字。
GOOD_RUN_STATUSES = ("success", "unchanged")


async def count_by(
    session: AsyncSession, column, model, *, where: Sequence[Any] = ()
) -> dict[str, int]:
    """按某列分组计数。键统一成字符串（SQLite 与 PostgreSQL 的类型不同）。"""
    result = await session.execute(
        select(column, func.count()).select_from(model).where(*where).group_by(column)
    )
    return {str(key): int(count) for key, count in result.all()}


async def overview(session: AsyncSession, blobs: BlobStore) -> dict[str, Any]:
    """一次算完首页要的全部数字。"""
    now = utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    device_total = await session.scalar(select(func.count()).select_from(Device))
    device_enabled = await session.scalar(
        select(func.count()).select_from(Device).where(Device.enabled.is_(True))
    )
    by_vendor = await count_by(session, Device.vendor, Device)

    version_total = await session.scalar(select(func.count()).select_from(ConfigSnapshot))
    changed_week = await session.scalar(
        select(func.count())
        .select_from(BackupRun)
        .where(BackupRun.changed.is_(True), BackupRun.started_at >= week_ago)
    )

    last_day = await count_by(
        session, BackupRun.status, BackupRun, where=(BackupRun.started_at >= day_ago,)
    )
    week_total = await session.scalar(
        select(func.count()).select_from(BackupRun).where(BackupRun.started_at >= week_ago)
    )
    week_good = await session.scalar(
        select(func.count())
        .select_from(BackupRun)
        .where(BackupRun.started_at >= week_ago, BackupRun.status.in_(GOOD_RUN_STATUSES))
    )

    # 每台设备**最新一次**合规结论。
    # 用 ``max(id)`` 而不是窗口函数：id 单调递增，"最新"就是最大 id，
    # 换数据库也不用改写法（``latest_results`` 那套窗口函数留给报表用）。
    latest_ids = (
        select(func.max(ComplianceResult.id))
        .group_by(ComplianceResult.device_id)
        .scalar_subquery()
    )
    compliance_rows = await session.execute(
        select(ComplianceResult.verdict, func.count())
        .where(ComplianceResult.id.in_(latest_ids))
        .group_by(ComplianceResult.verdict)
    )
    compliance = {str(verdict): int(count) for verdict, count in compliance_rows.all()}
    # ★ 没查过的设备必须单独报出来（"还没查"和"查了没问题"是两件事，
    #   看板上把它们并成一个数字，运维就永远想不起来还有设备没纳管）
    compliance["unknown"] = max(int(device_total or 0) - sum(compliance.values()), 0)

    changes = await count_by(session, ChangeRequest.state, ChangeRequest)

    recent_rows = await session.execute(
        select(
            BackupRun.id,
            BackupRun.device_id,
            Device.name,
            BackupRun.status,
            BackupRun.started_at,
            BackupRun.changed,
        )
        .join(Device, Device.id == BackupRun.device_id)
        .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
        .limit(5)
    )
    recent = [
        {
            "run_id": int(run_id),
            "device_id": int(device_id),
            "device_name": device_name,
            "status": str(status),
            "changed": changed,
            "started_at": started_at.isoformat(sep=" ") if started_at else None,
        }
        for run_id, device_id, device_name, status, started_at, changed in recent_rows.all()
    ]

    storage = blobs.stats()
    success_rate = (
        round(int(week_good or 0) / int(week_total or 1), 4) if week_total else None
    )

    return {
        "generated_at": now.isoformat(sep=" "),
        "devices": {
            "total": int(device_total or 0),
            "enabled": int(device_enabled or 0),
            "by_vendor": by_vendor,
        },
        "versions": {
            "total": int(version_total or 0),
            "changed_last_7d": int(changed_week or 0),
        },
        "runs": {
            "last_24h": last_day,
            # 失败 = 24 小时里既不是 success 也不是 unchanged 的那些
            "failed_last_24h": sum(
                count for status, count in last_day.items() if status not in GOOD_RUN_STATUSES
            ),
            "total_last_7d": int(week_total or 0),
            # 没有任何采集时返回 None 而不是 0：0% 和"还没有数据"是两回事
            "success_rate_7d": success_rate,
        },
        "compliance": compliance,
        "changes": changes,
        "storage": {"objects": storage["objects"], "object_bytes": storage["object_bytes"]},
        "recent_runs": recent,
    }
