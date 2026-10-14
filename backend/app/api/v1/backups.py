"""备份任务：发起、查询、取消、SSE 实时进度、运行记录。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.base import utcnow
from ...db.models import BackupRun, BackupTask, Device
from ...errors import NotFound
from ...schemas.backup import BackupRequest, RunOut, TaskDetail, TaskOut
from ...schemas.common import OkResponse, Page
from ...services import audit
from ..deps import CurrentUser, OperatorUser, RunnerDep, SessionDep, client_ip

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx 默认会缓冲响应体，SSE 必须显式关掉，否则前端看不到实时进度
    "X-Accel-Buffering": "no",
}


async def _device_names(session: AsyncSession, device_ids: list[int]) -> dict[int, str]:
    if not device_ids:
        return {}
    result = await session.execute(
        select(Device.id, Device.name).where(Device.id.in_(device_ids))
    )
    return {int(i): str(n) for i, n in result.all()}


@router.post("/backups", response_model=TaskOut, summary="发起批量备份")
async def create_backup(
    payload: BackupRequest,
    request: Request,
    session: SessionDep,
    runner: RunnerDep,
    actor: OperatorUser,
) -> TaskOut:
    task = await runner.create_task(session, payload=payload, user=actor)
    await audit.record(
        session,
        action="backup.start",
        user_id=actor.id,
        username=actor.username,
        target_type="task",
        target_id=task.id,
        detail={
            "total": task.total,
            "scope": {
                "device_ids": payload.device_ids,
                # ★ group_id / include_disabled 必须一起记：按分组发起的批量备份
                #   如果这里只记 device_ids/site/vendor/all_enabled，审计里就是"范围为空"，
                #   等于说不清那次备份到底覆盖了什么。
                "group_id": payload.group_id,
                "site": payload.site,
                "vendor": payload.vendor,
                "all_enabled": payload.all_enabled,
                "include_disabled": payload.include_disabled,
            },
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    runner.start(task.id)
    return TaskOut.model_validate(task)


@router.get("/backups", response_model=Page[TaskOut], summary="任务列表")
async def list_tasks(
    session: SessionDep,
    _: CurrentUser,
    status: str | None = None,
    trigger: str | None = Query(default=None, description="manual | scheduled"),
    schedule_id: int | None = Query(default=None, description="只看某个定时策略发起的任务"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[TaskOut]:
    """任务列表。

    ``schedule_id`` 是 P2 补的：策略行上只留了"上一次任务"，运维真正要问的是
    "这个策略最近几次跑得怎么样"——没有这个过滤条件，界面上就只能给他一个任务号，
    还得自己去任务列表里翻。
    """
    conditions = []
    if status:
        conditions.append(BackupTask.status == status)
    if trigger:
        conditions.append(BackupTask.trigger == trigger)
    if schedule_id is not None:
        conditions.append(BackupTask.schedule_id == schedule_id)

    total = await session.scalar(select(func.count()).select_from(BackupTask).where(*conditions))
    result = await session.execute(
        select(BackupTask)
        .where(*conditions)
        .order_by(BackupTask.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return Page[TaskOut](
        items=[TaskOut.model_validate(t) for t in result.scalars().all()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get("/backups/{task_id}", response_model=TaskDetail, summary="任务详情（含每台设备的结果）")
async def task_detail(task_id: int, session: SessionDep, _: CurrentUser) -> TaskDetail:
    task = await session.get(BackupTask, task_id)
    if task is None:
        raise NotFound(f"任务不存在：id={task_id}")
    result = await session.execute(
        select(BackupRun)
        .where(BackupRun.task_id == task_id)
        .order_by(BackupRun.id)
    )
    runs = list(result.scalars().all())
    names = await _device_names(session, [r.device_id for r in runs])
    detail = TaskDetail.model_validate(task)
    detail.runs = [
        RunOut.model_validate(r).model_copy(update={"device_name": names.get(r.device_id)})
        for r in runs
    ]
    return detail


@router.post("/backups/{task_id}/cancel", response_model=OkResponse, summary="取消任务")
async def cancel_task(
    task_id: int,
    request: Request,
    session: SessionDep,
    runner: RunnerDep,
    actor: OperatorUser,
) -> OkResponse:
    task = await session.get(BackupTask, task_id)
    if task is None:
        raise NotFound(f"任务不存在：id={task_id}")
    cancelled = await runner.cancel(task_id)
    await audit.record(
        session,
        action="backup.cancel",
        user_id=actor.id,
        username=actor.username,
        target_type="task",
        target_id=task_id,
        detail={"accepted": cancelled, "status_before": task.status},
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(
        ok=cancelled,
        message="已请求取消" if cancelled else "任务不在运行中（可能已完成）",
    )


@router.get("/backups/{task_id}/stream", summary="任务进度 SSE 流")
async def stream_task(
    task_id: int,
    request: Request,
    session: SessionDep,
    runner: RunnerDep,
    _: CurrentUser,
) -> StreamingResponse:
    """实时推任务进度。

    先回放历史事件（前端刷新/断线重连不会丢进度），然后持续推送；
    完成后自动结束。15 秒无事件发一次心跳注释，避免中间设备（Nginx/LB）掐连接。
    """
    task = await session.get(BackupTask, task_id)
    if task is None:
        raise NotFound(f"任务不存在：id={task_id}")
    initial = TaskOut.model_validate(task).model_dump(mode="json")

    async def event_stream() -> AsyncIterator[str]:
        yield f"data: {json.dumps({'type': 'state', 'task_id': task_id, 'seq': 0, 'at': utcnow().isoformat() + 'Z', 'data': initial}, ensure_ascii=False)}\n\n"
        if initial["status"] in {"success", "partial", "failed", "cancelled"}:
            yield "event: done\ndata: {}\n\n"
            return
        async with runner.bus.subscribe(task_id) as queue:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") == "task_finished":
                    yield "event: done\ndata: {}\n\n"
                    return

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/runs", response_model=Page[RunOut], summary="采集记录（可按设备/任务/状态过滤）")
async def list_runs(
    session: SessionDep,
    _: CurrentUser,
    device_id: int | None = None,
    task_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[RunOut]:
    conditions = []
    if device_id is not None:
        conditions.append(BackupRun.device_id == device_id)
    if task_id is not None:
        conditions.append(BackupRun.task_id == task_id)
    if status:
        conditions.append(BackupRun.status == status)

    total = await session.scalar(select(func.count()).select_from(BackupRun).where(*conditions))
    result = await session.execute(
        select(BackupRun)
        .where(*conditions)
        .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = list(result.scalars().all())
    names = await _device_names(session, [r.device_id for r in runs])
    return Page[RunOut](
        items=[
            RunOut.model_validate(r).model_copy(update={"device_name": names.get(r.device_id)})
            for r in runs
        ],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )
