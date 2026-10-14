"""设备台账、凭据、连接测试、单设备备份。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from ...collector import probe_device
from ...db.models import ConfigSnapshot, Device
from ...errors import ValidationFailed
from ...schemas.backup import BackupRequest, TaskOut
from ...schemas.common import OkResponse, Page
from ...schemas.device import (
    CredentialProfileCreate,
    CredentialProfileOut,
    CredentialProfileUpdate,
    DeviceCreate,
    DeviceDetail,
    DeviceImportResult,
    DeviceOut,
    DeviceTestResult,
    DeviceUpdate,
)
from ...services import audit
from ...services.devices import (
    build_targets,
    create_device,
    create_profile,
    delete_device,
    delete_profile,
    get_device,
    get_profile,
    import_devices,
    list_devices,
    list_profiles,
    update_device,
    update_profile,
)
from ...services.snapshots import latest_snapshot
from ..deps import (
    AdminUser,
    CipherDep,
    CurrentUser,
    OperatorUser,
    RunnerDep,
    SessionDep,
    SessionFactoryDep,
    SettingsDep,
    client_ip,
)

router = APIRouter()


def _profile_out(profile) -> CredentialProfileOut:
    return CredentialProfileOut(
        id=profile.id,
        name=profile.name,
        username=profile.username,
        description=profile.description,
        has_password=bool(profile.password_enc),
        has_enable_password=bool(profile.enable_password_enc),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
@router.get("/credentials", response_model=list[CredentialProfileOut], summary="凭据列表")
async def credentials(session: SessionDep, _: CurrentUser) -> list[CredentialProfileOut]:
    return [_profile_out(p) for p in await list_profiles(session)]


@router.post("/credentials", response_model=CredentialProfileOut, summary="创建凭据")
async def add_credential(
    payload: CredentialProfileCreate,
    request: Request,
    session: SessionDep,
    cipher: CipherDep,
    actor: OperatorUser,
) -> CredentialProfileOut:
    profile = await create_profile(session, payload, cipher)
    await audit.record(
        session,
        action="credential.create",
        user_id=actor.id,
        username=actor.username,
        target_type="credential",
        target_id=profile.id,
        detail={"name": profile.name, "username": profile.username},
        client_ip=client_ip(request),
    )
    await session.commit()
    return _profile_out(profile)


@router.patch("/credentials/{profile_id}", response_model=CredentialProfileOut, summary="修改凭据")
async def edit_credential(
    profile_id: int,
    payload: CredentialProfileUpdate,
    request: Request,
    session: SessionDep,
    cipher: CipherDep,
    actor: OperatorUser,
) -> CredentialProfileOut:
    profile = await get_profile(session, profile_id)
    await update_profile(session, profile, payload, cipher)
    await audit.record(
        session,
        action="credential.update",
        user_id=actor.id,
        username=actor.username,
        target_type="credential",
        target_id=profile.id,
        detail={"name": profile.name, "password_changed": "password" in payload.model_fields_set},
        client_ip=client_ip(request),
    )
    await session.commit()
    return _profile_out(profile)


@router.delete("/credentials/{profile_id}", response_model=OkResponse, summary="删除凭据")
async def remove_credential(
    profile_id: int,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> OkResponse:
    profile = await get_profile(session, profile_id)
    await delete_profile(session, profile)
    await audit.record(
        session,
        action="credential.delete",
        user_id=actor.id,
        username=actor.username,
        target_type="credential",
        target_id=profile_id,
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(message="凭据已删除")


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
@router.get("/devices", response_model=Page[DeviceOut], summary="设备列表")
async def devices(
    session: SessionDep,
    _: CurrentUser,
    q: str | None = Query(default=None, description="按名称/IP/设备名模糊搜索"),
    vendor: str | None = None,
    site: str | None = None,
    enabled: bool | None = None,
    group_id: int | None = Query(default=None, description="只看某个分组的设备"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[DeviceOut]:
    items, total = await list_devices(
        session,
        q=q,
        vendor=vendor,
        site=site,
        enabled=enabled,
        group_id=group_id,
        limit=limit,
        offset=offset,
    )
    return Page[DeviceOut](
        items=[DeviceOut.model_validate(d) for d in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/devices", response_model=DeviceOut, summary="新增设备")
async def add_device(
    payload: DeviceCreate,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> DeviceOut:
    device = await create_device(session, payload)
    await audit.record(
        session,
        action="device.create",
        user_id=actor.id,
        username=actor.username,
        target_type="device",
        target_id=device.id,
        detail={"name": device.name, "host": device.host, "vendor": device.vendor},
        client_ip=client_ip(request),
    )
    await session.commit()
    return DeviceOut.model_validate(device)


@router.get("/devices/{device_id}", response_model=DeviceDetail, summary="设备详情")
async def device_detail(device_id: int, session: SessionDep, _: CurrentUser) -> DeviceDetail:
    device = await get_device(session, device_id)
    total_snapshots = await session.scalar(
        select(func.count())
        .select_from(ConfigSnapshot)
        .where(ConfigSnapshot.device_id == device_id)
    )
    latest = await latest_snapshot(session, device_id)
    detail = DeviceDetail.model_validate(device)
    detail.snapshot_count = int(total_snapshots or 0)
    if latest is not None:
        detail.latest_snapshot_id = latest.id
        detail.latest_sha256 = latest.sha256
        detail.latest_captured_at = latest.captured_at
    return detail


@router.patch("/devices/{device_id}", response_model=DeviceOut, summary="修改设备")
async def edit_device(
    device_id: int,
    payload: DeviceUpdate,
    request: Request,
    session: SessionDep,
    actor: OperatorUser,
) -> DeviceOut:
    device = await get_device(session, device_id)
    await update_device(session, device, payload)
    await audit.record(
        session,
        action="device.update",
        user_id=actor.id,
        username=actor.username,
        target_type="device",
        target_id=device.id,
        detail={"fields": sorted(payload.model_fields_set)},
        client_ip=client_ip(request),
    )
    await session.commit()
    return DeviceOut.model_validate(device)


@router.delete("/devices/{device_id}", response_model=OkResponse, summary="删除设备")
async def remove_device(
    device_id: int,
    request: Request,
    session: SessionDep,
    actor: AdminUser,
) -> OkResponse:
    device = await get_device(session, device_id)
    name = device.name
    await delete_device(session, device)
    await audit.record(
        session,
        action="device.delete",
        user_id=actor.id,
        username=actor.username,
        target_type="device",
        target_id=device_id,
        detail={"name": name},
        client_ip=client_ip(request),
    )
    await session.commit()
    return OkResponse(message=f"设备 {name} 及其历史已删除")


@router.post("/devices/import", response_model=DeviceImportResult, summary="批量导入设备（JSON 行）")
async def import_device_rows(
    rows: list[dict],
    request: Request,
    session: SessionDep,
    cipher: CipherDep,
    actor: OperatorUser,
) -> DeviceImportResult:
    """接收前端解析好的 CSV 行（前端解析可以即时反馈格式错误，也免了 multipart 依赖）。"""
    result = await import_devices(session, rows, cipher=cipher)
    await audit.record(
        session,
        action="device.import",
        user_id=actor.id,
        username=actor.username,
        detail=result.model_dump(),
        client_ip=client_ip(request),
    )
    await session.commit()
    return result


@router.post("/devices/{device_id}/test", response_model=DeviceTestResult, summary="连接测试")
async def test_device(
    device_id: int,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    cipher: CipherDep,
    session_factory: SessionFactoryDep,
    actor: OperatorUser,
) -> DeviceTestResult:
    device = await get_device(session, device_id)
    targets, unbuildable = await build_targets(
        session, [device], settings=settings, cipher=cipher
    )
    if not targets:
        # 单设备场景：凭据有问题直接 422，不要建一个注定失败的任务
        raise ValidationFailed(unbuildable[0][1])
    # ★ 必须把会话工厂传下去：漏了它这里会去发真实 SSH 连接
    #   （测试里表现为 15 秒连接超时后返回 unreachable，而不是预期的错误码）
    result = await probe_device(targets[0], session_factory=session_factory)
    await audit.record(
        session,
        action="device.test",
        user_id=actor.id,
        username=actor.username,
        target_type="device",
        target_id=device.id,
        detail={"ok": result.ok, "status": result.status.value, "error": result.error_message},
        client_ip=client_ip(request),
    )
    if result.ok:
        # 连接测试顺手把型号/版本/设备名补进台账——省得人工录
        device.hostname = result.hostname or device.hostname
        device.model = result.facts.get("model") or device.model
        device.os_version = result.facts.get("software_version") or device.os_version
    await session.commit()
    return DeviceTestResult(
        ok=result.ok,
        status=result.status.value,
        hostname=result.hostname,
        facts=result.facts,
        warnings=result.warnings,
        version_output=result.version_output or None,
        error_code=result.error_code,
        error_message=result.error_message,
        duration_ms=result.duration_ms,
    )


@router.post("/devices/{device_id}/backup", response_model=TaskOut, summary="立即备份单台设备")
async def backup_one(
    device_id: int,
    request: Request,
    session: SessionDep,
    runner: RunnerDep,
    actor: OperatorUser,
    precheck: bool | None = None,
    retries: int | None = None,
) -> TaskOut:
    device = await get_device(session, device_id)
    task = await runner.create_task(
        session,
        payload=BackupRequest(
            device_ids=[device_id],
            name=f"单设备备份：{device.name}",
            precheck=precheck,
            retries=retries,
        ),
        user=actor,
    )
    await audit.record(
        session,
        action="backup.start",
        user_id=actor.id,
        username=actor.username,
        target_type="task",
        target_id=task.id,
        detail={"device": device.name, "trigger": "manual"},
        client_ip=client_ip(request),
    )
    await session.commit()
    runner.start(task.id)
    return TaskOut.model_validate(task)


@router.get("/devices/{device_id}/runs", summary="设备的历史运行记录")
async def device_runs(
    device_id: int,
    session: SessionDep,
    _: CurrentUser,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    from ...db.models import BackupRun
    from ...schemas.backup import RunOut

    await get_device(session, device_id)  # 不存在直接 404
    total = await session.scalar(
        select(func.count()).select_from(BackupRun).where(BackupRun.device_id == device_id)
    )
    result = await session.execute(
        select(BackupRun)
        .where(BackupRun.device_id == device_id)
        .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [RunOut.model_validate(r).model_dump() for r in result.scalars().all()],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }
