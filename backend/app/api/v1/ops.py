"""P2 运营接口：设备分组、定时策略、通知渠道、保留策略。

放在一个模块里是有意的：它们都是"配置类"资源，页面相邻、权限模型一致
（读=任何登录用户，改=operator，删/执行清理=admin），拆成四个文件只会让
路由注册和权限核对变麻烦。

**权限与审计**：每一个写操作都落审计。定时策略与通知渠道属于"改了会影响
所有人的自动化行为"的配置，出问题时必须能查到是谁在什么时候改的。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, select

from ...db.base import utcnow
from ...db.models import BackupTask, Device, NotificationChannel, NotificationLog
from ...errors import AppError, Conflict, ValidationFailed
from ...schemas.backup import BackupRequest, TaskOut
from ...schemas.common import OkResponse, Page
from ...schemas.ops import (
    ChannelCreate,
    ChannelOut,
    ChannelTestResult,
    ChannelUpdate,
    GroupCreate,
    GroupDevicesRequest,
    GroupDevicesResult,
    GroupOut,
    GroupUpdate,
    NotificationLogOut,
    RetentionOut,
    RetentionRunResult,
    RetentionUpdate,
    ScheduleCreate,
    ScheduleOut,
    SchedulePreview,
    ScheduleUpdate,
)
from ...services import audit
from ...services.groups import (
    create_group,
    delete_group,
    get_group,
    list_groups,
    set_devices,
    update_group,
)
from ...services.notifications import (
    DEFAULT_EVENTS,
    EVENTS,
    KIND_LABELS,
    KINDS,
    create_channel,
    delete_channel,
    get_channel,
    list_channels,
    redact_config,
    update_channel,
)
from ...services.retention import (
    enforce,
    ensure_policy,
    policy_summary,
    update_policy,
)
from ...services.schedules import (
    compute_next_run,
    create_schedule,
    delete_schedule,
    describe,
    get_schedule,
    list_schedules,
    update_schedule,
)
from ..deps import (
    AdminUser,
    BlobsDep,
    CipherDep,
    CurrentUser,
    NotifierDep,
    OperatorUser,
    RunnerDep,
    SessionDep,
    client_ip,
)

router = APIRouter()


def _group_out(group) -> GroupOut:
    data = GroupOut.model_validate(group)
    data.device_count = len(group.devices or [])
    return data


def _schedule_out(schedule) -> ScheduleOut:
    data = ScheduleOut.model_validate(schedule)
    # 人话描述由服务层算（cron → "每天 02:00"），前端直接显示，不必自己解析 cron
    data.description = describe(schedule)
    return data


def _channel_out(channel: NotificationChannel) -> ChannelOut:
    return ChannelOut(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        enabled=channel.enabled,
        # ★ 机器人 URL 本身就是凭据（access_token 在里面），必须打码后再出网
        config=redact_config(channel.kind, channel.config),
        events=list(channel.events or []),
        min_failures=channel.min_failures,
        has_secret=bool(channel.secret_enc),
        last_sent_at=channel.last_sent_at,
        last_status=channel.last_status,
        last_error=channel.last_error,
        created_at=channel.created_at,
    )


def _retention_out(policy) -> RetentionOut:
    data = RetentionOut(**policy_summary(policy))
    data.last_run_at = policy.last_run_at
    data.last_result = dict(policy.last_result or {})
    data.updated_at = policy.updated_at
    return data


# ==========================================================================
# 设备分组
# ==========================================================================
@router.get("/groups", response_model=list[GroupOut], summary="分组列表")
async def groups(session: SessionDep, _: CurrentUser) -> list[GroupOut]:
    return [_group_out(group) for group in await list_groups(session)]


@router.post("/groups", response_model=GroupOut, summary="创建分组")
async def add_group(
    payload: GroupCreate,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> GroupOut:
    group = await create_group(
        session, name=payload.name, description=payload.description, color=payload.color
    )
    await audit.record(
        session,
        action="group.create",
        user_id=actor.id,
        username=actor.username,
        target_type="group",
        target_id=group.id,
        detail={"name": group.name},
        client_ip=client_ip(request),
    )
    await session.commit()
    return _group_out(group)


@router.patch("/groups/{group_id}", response_model=GroupOut, summary="修改分组")
async def edit_group(
    group_id: int,
    payload: GroupUpdate,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> GroupOut:
    group = await get_group(session, group_id)
    await update_group(
        session,
        group,
        name=payload.name,
        description=payload.description,
        color=payload.color,
    )
    await audit.record(
        session,
        action="group.update",
        user_id=actor.id,
        username=actor.username,
        target_type="group",
        target_id=group_id,
        detail={"fields": sorted(payload.model_fields_set)},
        client_ip=client_ip(request),
    )
    await session.commit()
    return _group_out(group)


@router.delete("/groups/{group_id}", response_model=OkResponse, summary="删除分组")
async def remove_group(
    group_id: int,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> OkResponse:
    group = await get_group(session, group_id)
    name = group.name
    await delete_group(session, group)
    await audit.record(
        session,
        action="group.delete",
        user_id=actor.id,
        username=actor.username,
        target_type="group",
        target_id=group_id,
        detail={"name": name},
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(message=f"分组 {name} 已删除（成员设备保留，仅解除归属）")


@router.put("/groups/{group_id}/devices", response_model=GroupDevicesResult, summary="调整分组设备")
async def put_group_devices(
    group_id: int,
    payload: GroupDevicesRequest,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> GroupDevicesResult:
    group = await get_group(session, group_id)
    affected = await set_devices(session, group, payload.device_ids, replace=payload.replace)
    await audit.record(
        session,
        action="group.set_devices",
        user_id=actor.id,
        username=actor.username,
        target_type="group",
        target_id=group_id,
        detail={
            "affected": affected,
            "replace": payload.replace,
            "requested": len(payload.device_ids),
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    # 直接用 count 查，不依赖 ORM 关系缓存（commit 后再读集合容易踩身份映射的坑）
    member_count = await session.scalar(
        select(func.count()).select_from(Device).where(Device.group_id == group_id)
    )
    return GroupDevicesResult(affected=affected, device_count=int(member_count or 0))


# ==========================================================================
# 定时策略
# ==========================================================================
@router.get("/schedules", response_model=list[ScheduleOut], summary="定时策略列表")
async def schedules(session: SessionDep, _: CurrentUser) -> list[ScheduleOut]:
    return [_schedule_out(item) for item in await list_schedules(session)]


@router.post("/schedules", response_model=ScheduleOut, summary="创建定时策略")
async def add_schedule(
    payload: ScheduleCreate,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> ScheduleOut:
    schedule = await create_schedule(
        session, payload.model_dump(), created_by=actor.username
    )
    await audit.record(
        session,
        action="schedule.create",
        user_id=actor.id,
        username=actor.username,
        target_type="schedule",
        target_id=schedule.id,
        detail={
            "name": schedule.name,
            "kind": schedule.kind,
            "description": describe(schedule),
            # 范围是审计里最关键的一项："谁把夜间任务改成全量了"必须查得到
            "scope": schedule.scope,
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    return _schedule_out(schedule)


@router.get("/schedules/{schedule_id}", response_model=ScheduleOut, summary="策略详情")
async def schedule_detail(schedule_id: int, session: SessionDep, _: CurrentUser) -> ScheduleOut:
    return _schedule_out(await get_schedule(session, schedule_id))


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut, summary="修改定时策略")
async def edit_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> ScheduleOut:
    schedule = await get_schedule(session, schedule_id)
    await update_schedule(session, schedule, payload.model_dump(exclude_unset=True))
    await audit.record(
        session,
        action="schedule.update",
        user_id=actor.id,
        username=actor.username,
        target_type="schedule",
        target_id=schedule_id,
        detail={
            "fields": sorted(payload.model_fields_set),
            "next_run_at": str(schedule.next_run_at),
            # 改完之后的真实范围（不仅是"改了哪些字段"）：出问题时能直接看出覆盖了谁
            "scope": schedule.scope,
            "enabled": schedule.enabled,
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    return _schedule_out(schedule)


@router.delete("/schedules/{schedule_id}", response_model=OkResponse, summary="删除定时策略")
async def remove_schedule(
    schedule_id: int,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> OkResponse:
    schedule = await get_schedule(session, schedule_id)
    name = schedule.name
    await delete_schedule(session, schedule)
    await audit.record(
        session,
        action="schedule.delete",
        user_id=actor.id,
        username=actor.username,
        target_type="schedule",
        target_id=schedule_id,
        detail={"name": name},
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(message=f"定时策略 {name} 已删除（已产生的任务记录保留）")


@router.get("/schedules/{schedule_id}/preview", response_model=SchedulePreview, summary="预览执行时刻")
async def preview_schedule(
    schedule_id: int,
    session: SessionDep,
    _: CurrentUser,
    count: int = Query(default=5, ge=1, le=50),
) -> SchedulePreview:
    """给出接下来 count 次的执行时刻，让运维在保存前确认"是不是我想要的时间"。"""
    schedule = await get_schedule(session, schedule_id)
    times: list[datetime] = []
    cursor = utcnow()
    for _step in range(count):
        nxt = compute_next_run(schedule, after=cursor, not_before=cursor)
        if nxt is None:
            break
        times.append(nxt)
        cursor = nxt
    return SchedulePreview(kind=schedule.kind, times=times, description=describe(schedule))


@router.post("/schedules/{schedule_id}/run", response_model=TaskOut, summary="立即执行该策略")
async def run_schedule(
    schedule_id: int,
    request: Request,
    session: SessionDep,
    runner: RunnerDep,
    actor: OperatorUser,
) -> TaskOut:
    """手工跑一次策略（范围与参数完全按策略配置）。"""
    schedule = await get_schedule(session, schedule_id)

    # 同一个策略叠两个任务没有意义，而且会互相争设备锁，不如直接拒绝
    if schedule.last_task_id:
        previous = await session.get(BackupTask, schedule.last_task_id)
        if previous is not None and previous.status in ("queued", "running"):
            raise Conflict(f"该策略上一次任务 #{previous.id} 仍在运行，请等它结束后再试")

    try:
        payload = BackupRequest(**{**schedule.scope, **schedule.params})
    except ValidationError as exc:
        raise ValidationFailed(f"策略 {schedule.name} 的范围参数不合法：{exc}") from exc
    if not payload.name:
        payload.name = f"{schedule.name}（手工触发）"

    try:
        task = await runner.create_task(
            session,
            payload=payload,
            user=actor,
            trigger="manual",
            schedule_id=schedule.id,
        )
    except AppError as exc:
        raise ValidationFailed(f"策略 {schedule.name} 发起任务失败：{exc.message}") from exc

    schedule.last_task_id = task.id
    schedule.last_run_at = utcnow()
    schedule.last_status = "queued"
    # ★ 这里**不**推进 next_run_at：手工触发是插队，不应该把每天 02:00 的
    #   班次整体挪到"手工执行后 24 小时"。
    await audit.record(
        session,
        action="schedule.run",
        user_id=actor.id,
        username=actor.username,
        target_type="schedule",
        target_id=schedule_id,
        detail={"name": schedule.name, "task_id": task.id, "total": task.total},
        client_ip=client_ip(request),
    )
    await session.commit()
    runner.start(task.id)
    return TaskOut.model_validate(task)


# ==========================================================================
# 通知渠道
# ==========================================================================
@router.get("/notification-events", summary="可用通知事件")
async def notification_events(_: CurrentUser) -> dict:
    """事件清单给前端渲染复选框，避免前后端各写一份枚举对不上。"""
    return {
        "events": [{"value": key, "label": label} for key, label in EVENTS.items()],
        "kinds": [
            {"value": kind, "label": KIND_LABELS.get(kind, kind)} for kind in KINDS
        ],
        "default_events": list(DEFAULT_EVENTS),
    }


@router.get("/channels", response_model=list[ChannelOut], summary="通知渠道列表")
async def channels(session: SessionDep, _: CurrentUser) -> list[ChannelOut]:
    return [_channel_out(item) for item in await list_channels(session)]


@router.post("/channels", response_model=ChannelOut, summary="创建通知渠道")
async def add_channel(
    payload: ChannelCreate,
    request: Request,
    session: SessionDep,
    cipher: CipherDep,
    actor: OperatorUser,
) -> ChannelOut:
    channel = await create_channel(session, payload.model_dump(), cipher)
    await audit.record(
        session,
        action="channel.create",
        user_id=actor.id,
        username=actor.username,
        target_type="channel",
        target_id=channel.id,
        detail={"name": channel.name, "kind": channel.kind, "events": channel.events},
        client_ip=client_ip(request),
    )
    await session.commit()
    return _channel_out(channel)


@router.patch("/channels/{channel_id}", response_model=ChannelOut, summary="修改通知渠道")
async def edit_channel(
    channel_id: int,
    payload: ChannelUpdate,
    request: Request,
    session: SessionDep,
    cipher: CipherDep,
    actor: OperatorUser,
) -> ChannelOut:
    channel = await get_channel(session, channel_id)
    await update_channel(session, channel, payload.model_dump(exclude_unset=True), cipher)
    await audit.record(
        session,
        action="channel.update",
        user_id=actor.id,
        username=actor.username,
        target_type="channel",
        target_id=channel_id,
        detail={
            "fields": sorted(payload.model_fields_set),
            "secret_changed": bool(payload.secret),
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    return _channel_out(channel)


@router.delete("/channels/{channel_id}", response_model=OkResponse, summary="删除通知渠道")
async def remove_channel(
    channel_id: int,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> OkResponse:
    channel = await get_channel(session, channel_id)
    name = channel.name
    await delete_channel(session, channel)
    await audit.record(
        session,
        action="channel.delete",
        user_id=actor.id,
        username=actor.username,
        target_type="channel",
        target_id=channel_id,
        detail={"name": name},
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(message=f"通知渠道 {name} 已删除（历史发送记录保留）")


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResult, summary="测试通知渠道")
async def test_channel(
    channel_id: int,
    request: Request,
    session: SessionDep,
    notifier: NotifierDep,
    actor: OperatorUser,
) -> ChannelTestResult:
    """真发一条消息出去。渠道不通时返回 ok=false 而不是 500——这是预期结果。"""
    ok, detail = await notifier.send_test(channel_id)
    await audit.record(
        session,
        action="channel.test",
        user_id=actor.id,
        username=actor.username,
        target_type="channel",
        target_id=channel_id,
        detail={"ok": ok, "error": None if ok else detail[:200]},
        client_ip=client_ip(request),
    )
    await session.commit()
    return ChannelTestResult(ok=ok, detail=detail)


@router.get("/notification-logs", response_model=Page[NotificationLogOut], summary="通知发送记录")
async def notification_logs(
    session: SessionDep,
    _: CurrentUser,
    channel_id: int | None = None,
    status: str | None = Query(default=None, description="success | failed"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[NotificationLogOut]:
    conditions = []
    if channel_id is not None:
        conditions.append(NotificationLog.channel_id == channel_id)
    if status:
        conditions.append(NotificationLog.status == status)

    total = await session.scalar(
        select(func.count()).select_from(NotificationLog).where(*conditions)
    )
    result = await session.execute(
        select(NotificationLog)
        .where(*conditions)
        .order_by(NotificationLog.created_at.desc(), NotificationLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return Page[NotificationLogOut](
        items=[NotificationLogOut.model_validate(row) for row in result.scalars().all()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


# ==========================================================================
# 保留策略
# ==========================================================================
@router.get("/retention", response_model=RetentionOut, summary="保留策略")
async def retention(session: SessionDep, _: CurrentUser) -> RetentionOut:
    policy = await ensure_policy(session)
    # 首次访问会建单例行；GET 里落一次库，免得每次读都重新 insert 再回滚
    await session.commit()
    return _retention_out(policy)


@router.put("/retention", response_model=RetentionOut, summary="修改保留策略")
async def put_retention(
    payload: RetentionUpdate,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> RetentionOut:
    changes = payload.model_dump(exclude_unset=True)
    clear_changed = "keep_changed_days" in changes and changes["keep_changed_days"] is None
    policy = await update_policy(session, changes)
    if clear_changed:
        # 服务层把 None 当作"这个字段不修改"（CLI/定时器也这么用），但界面上的
        # "变更版本永久保留"必须能表达出来，所以这里显式清空。
        policy.keep_changed_days = None
        await session.flush()
    await audit.record(
        session,
        action="retention.update",
        user_id=actor.id,
        username=actor.username,
        target_type="retention",
        target_id=1,
        detail={
            "enabled": policy.enabled,
            "keep_changed_days": policy.keep_changed_days,
            "keep_unchanged_daily_days": policy.keep_unchanged_daily_days,
            "keep_min_versions": policy.keep_min_versions,
            "keep_session_log_days": policy.keep_session_log_days,
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    return _retention_out(policy)


@router.post("/retention/run", response_model=RetentionRunResult, summary="立即执行清理")
async def run_retention(
    request: Request,
    session: SessionDep,
    blobs: BlobsDep,
    actor: AdminUser,
) -> RetentionRunResult:
    """手工跑一次清理（页面上"立即清理"按钮）。

    ★ 删除是不可逆的，所以这里只允许 admin；策略本身是保守的（最早/最新版本
    与变更版本默认永久保留），但仍然要留下"谁在什么时候删了多少"的审计。
    """
    policy = await ensure_policy(session)
    summary = await enforce(session, blobs, policy=policy)
    policy.last_run_at = utcnow()
    policy.last_result = summary
    await audit.record(
        session,
        action="retention.run",
        user_id=actor.id,
        username=actor.username,
        target_type="retention",
        target_id=1,
        detail=summary,
        client_ip=client_ip(request),
    )
    await session.commit()
    return RetentionRunResult(summary=summary)
