"""元信息：驱动列表、统计、健康检查。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from ...collector.registry import get_registry
from ...db.models import BackupRun, BackupTask, ConfigSnapshot, Device
from ...schemas.common import OkResponse
from ...services.overview import overview as build_overview
from ..deps import BlobsDep, CurrentUser, SessionDep

router = APIRouter()


@router.get("/meta/vendors", summary="支持的厂商驱动")
async def list_vendors(_: CurrentUser) -> list[dict]:
    registry = get_registry()
    return [
        {
            "name": driver.name,
            "display_name": driver.display_name,
            "aliases": list(driver.aliases),
            "description": driver.description,
            "running_config_cmd": driver.running_config_cmd(),
            "setup_commands": list(driver.session_setup_cmds()),
            "supports_deploy": driver.supports_deploy,
        }
        for driver in registry.all()
    ]


@router.get("/meta/stats", summary="总览统计（首页看板）")
async def stats(session: SessionDep, _: CurrentUser) -> dict:
    device_total = await session.scalar(select(func.count()).select_from(Device))
    enabled = await session.scalar(
        select(func.count()).select_from(Device).where(Device.enabled.is_(True))
    )
    snapshot_total = await session.scalar(select(func.count()).select_from(ConfigSnapshot))
    task_total = await session.scalar(select(func.count()).select_from(BackupTask))

    # 最近一次运行的状态分布（列表页用得上）
    status_rows = await session.execute(
        select(BackupRun.status, func.count()).group_by(BackupRun.status)
    )
    status_counts = {str(status): int(count) for status, count in status_rows.all()}

    # 最近 24h 失败数：运维最关心的一个数字
    from ...db.base import utcnow
    from datetime import timedelta

    since = utcnow() - timedelta(hours=24)
    recent_failed = await session.scalar(
        select(func.count())
        .select_from(BackupRun)
        .where(BackupRun.started_at >= since, BackupRun.status.notin_(("success", "unchanged")))
    )

    return {
        "devices": {"total": int(device_total or 0), "enabled": int(enabled or 0)},
        "snapshots": int(snapshot_total or 0),
        "tasks": int(task_total or 0),
        "run_status_counts": status_counts,
        "failed_last_24h": int(recent_failed or 0),
    }


@router.get("/meta/ping", response_model=OkResponse, summary="存活检查（无需登录）")
async def ping() -> OkResponse:
    return OkResponse(message="pong")


@router.get("/meta/overview", summary="首页看板聚合（一次取齐，避免数字互相对不上）")
async def dashboard(session: SessionDep, _: CurrentUser, blobs: BlobsDep) -> dict:
    """设备/版本/采集/合规/变更/存储 + 最近 5 条采集结果。

    与 ``/meta/stats`` 的区别：stats 只给几个原始计数（列表页够用），这里是**看板**要的
    全套聚合，并且刻意包含"还没查过合规的设备数"——把它和"查了没问题"分开报，
    否则运维永远想不起来还有设备没纳管。
    """
    return await build_overview(session, blobs)
