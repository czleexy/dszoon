"""配置版本：列表、查看、下载、脱敏、版本对比。

安全模型（P1 的最小实现）：

* 默认返回**脱敏**内容，任何登录用户都能看。
* ``format=raw``（明文）需要 auditor 及以上角色，并且**每次都写审计**——
  "谁在什么时候看了哪台设备的明文配置"必须可追溯。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from ...core.security import Role, role_at_least
from ...db.models import ConfigSnapshot
from ...errors import Forbidden, ValidationFailed
from ...schemas.common import Page
from ...schemas.snapshot import DiffOut, SnapshotContent, SnapshotOut
from ...services import audit
from ...services.diffs import diff_snapshots
from ...services.devices import get_device
from ...services.snapshots import (
    get_snapshot,
    list_snapshots,
    load_config,
    redact_config,
)
from ..deps import BlobsDep, CurrentUser, SessionDep, client_ip

router = APIRouter()


@router.get(
    "/devices/{device_id}/snapshots",
    response_model=Page[SnapshotOut],
    summary="设备的历史版本",
)
async def device_snapshots(
    device_id: int,
    session: SessionDep,
    _: CurrentUser,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[SnapshotOut]:
    await get_device(session, device_id)
    items, total = await list_snapshots(session, device_id, limit=limit, offset=offset)
    return Page[SnapshotOut](
        items=[SnapshotOut.model_validate(s) for s in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotOut, summary="版本元信息")
async def snapshot_detail(snapshot_id: int, session: SessionDep, _: CurrentUser) -> SnapshotOut:
    return SnapshotOut.model_validate(await get_snapshot(session, snapshot_id))


@router.get("/snapshots/{snapshot_id}/content", summary="查看/下载配置正文")
async def snapshot_content(
    snapshot_id: int,
    request: Request,
    session: SessionDep,
    blobs: BlobsDep,
    user: CurrentUser,
    format: str = Query(default="redacted", pattern="^(raw|redacted)$"),
    download: bool = Query(default=False),
):
    snapshot = await get_snapshot(session, snapshot_id)
    device = await get_device(session, snapshot.device_id)
    text = load_config(blobs, snapshot)

    if format == "raw":
        if not role_at_least(user.role, Role.AUDITOR):
            raise Forbidden("查看配置明文需要审计（auditor）及以上角色")
        await audit.record(
            session,
            action="snapshot.view_plaintext",
            user_id=user.id,
            username=user.username,
            target_type="snapshot",
            target_id=snapshot.id,
            detail={"device": device.name, "download": download},
            client_ip=client_ip(request),
        )
        await session.commit()
    else:
        text = redact_config(text, device.vendor)

    if not download:
        return SnapshotContent(
            snapshot_id=snapshot.id,
            device_id=snapshot.device_id,
            sha256=snapshot.sha256,
            format=format,
            content=text,
            size_bytes=snapshot.size_bytes,
            line_count=snapshot.line_count,
            redacted=format != "raw",
        )

    await audit.record(
        session,
        action="snapshot.download",
        user_id=user.id,
        username=user.username,
        target_type="snapshot",
        target_id=snapshot.id,
        detail={"device": device.name, "format": format},
        client_ip=client_ip(request),
    )
    await session.commit()
    filename = f"{device.name}-{snapshot.captured_at:%Y%m%d-%H%M%S}-{snapshot.sha256[:8]}.cfg"
    if format != "raw":
        filename = filename.replace(".cfg", ".redacted.cfg")
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/devices/{device_id}/diff", response_model=DiffOut, summary="版本对比")
async def device_diff(
    device_id: int,
    session: SessionDep,
    blobs: BlobsDep,
    _: CurrentUser,
    from_snapshot_id: int | None = Query(default=None, description="留空则与倒数第二版比"),
    to_snapshot_id: int | None = Query(default=None, description="留空则取最新版"),
    context: int = Query(default=3, ge=0, le=50),
    normalize: bool = Query(default=True, description="是否先按驱动规则归一化再比对"),
) -> DiffOut:
    device = await get_device(session, device_id)
    items, total = await list_snapshots(session, device_id, limit=2, offset=0)
    if total < 2 and (to_snapshot_id is None or from_snapshot_id is None):
        raise ValidationFailed("该设备还没有两个版本可供对比")

    to_snapshot = (
        await get_snapshot(session, to_snapshot_id) if to_snapshot_id else items[0]
    )
    if from_snapshot_id:
        from_snapshot = await get_snapshot(session, from_snapshot_id)
    else:
        previous = next((s for s in items if s.id != to_snapshot.id), None)
        if previous is None:
            raise ValidationFailed("找不到可对比的上一版")
        from_snapshot = previous

    for snapshot in (from_snapshot, to_snapshot):
        if snapshot.device_id != device_id:
            raise ValidationFailed(f"版本 {snapshot.id} 不属于设备 {device.name}")

    result = await diff_snapshots(
        session,
        blobs,
        device=device,
        from_snapshot=from_snapshot,
        to_snapshot=to_snapshot,
        context=context,
        normalize=normalize,
    )
    return DiffOut(
        device_id=device_id,
        from_snapshot_id=result.from_snapshot_id,
        to_snapshot_id=result.to_snapshot_id,
        from_sha256=result.from_sha256,
        to_sha256=result.to_sha256,
        identical=result.identical,
        added=result.added,
        removed=result.removed,
        changed=result.changed,
        unified=result.unified,
        significant=result.significant,
        normalized=result.normalized,
        from_captured_at=from_snapshot.captured_at,
        to_captured_at=to_snapshot.captured_at,
    )


@router.delete("/snapshots/{snapshot_id}", summary="删除某个版本（谨慎）")
async def delete_snapshot(
    snapshot_id: int,
    session: SessionDep,
    _: CurrentUser,
) -> dict:
    """只删元数据行，不动物理对象——内容寻址的 blob 可能被其他设备/版本引用。

    物理回收交给定期维护任务（P2 的保留策略）。
    """
    snapshot = await get_snapshot(session, snapshot_id)
    referenced = await session.scalar(
        select(func.count()).select_from(ConfigSnapshot).where(ConfigSnapshot.sha256 == snapshot.sha256)
    )
    await session.delete(snapshot)
    await session.commit()
    return {"ok": True, "shared_by_other_snapshots": int(referenced or 0) - 1}
