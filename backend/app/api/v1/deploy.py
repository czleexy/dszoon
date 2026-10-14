"""配置变更单与下发（P4-B）。

路由只做三件事：鉴权、取对象、调服务层。**安全判定一律不在这里**——
闸门与状态迁移都在 ``services/deploy.py``（纯逻辑）与 ``services/changes.py``
（数据库编排）里，路由里再写一遍 if 就一定会分叉。

★ 本模块**故意没有**"真正把命令发出去"的路由。
下发需要 P4-A 的驱动层（进卷积、逐行回显确认、``commit``），那部分要按真机语法
校准。在它落地之前，唯一能走通的写设备动作是 ``/preview``——预演只做预检、
一行都不发。留一条"看起来能发、实际发不出去或发得不对"的路由，比没有更危险。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...db.models import Device
from ...schemas.common import OkResponse, Page
from ...schemas.deploy import (
    BaselineIn,
    ChangeCancelIn,
    ChangeCreateIn,
    ChangeDecisionIn,
    ChangeDetailOut,
    ChangeLinesIn,
    ChangeOut,
    DeployRunOut,
    PreviewOut,
    RollbackCreateIn,
    RollbackCreateOut,
    RollbackPreviewOut,
)
from ...services import changes
from ..deps import BlobsDep, CurrentUser, OperatorUser, SessionDep, SettingsDep

router = APIRouter(tags=["deploy"])


async def _device_names(session, rows) -> dict[int, str]:
    """一次查出所有涉及的设备名，避免列表页 N+1。"""
    ids = {row.device_id for row in rows}
    if not ids:
        return {}
    result = await session.execute(select(Device.id, Device.name).where(Device.id.in_(ids)))
    return {int(device_id): str(name) for device_id, name in result.all()}


def _out(row, *, device_name: str | None = None) -> ChangeOut:
    return ChangeOut.model_validate(changes.change_payload(row, device_name=device_name))


@router.get("/changes", response_model=Page[ChangeOut], summary="变更单列表")
async def list_changes(
    session: SessionDep,
    _: CurrentUser,
    device_id: int | None = Query(default=None, description="按设备过滤"),
    state: str | None = Query(default=None, description="按状态过滤"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ChangeOut]:
    rows, total = await changes.list_changes(
        session, device_id=device_id, state=state, limit=limit, offset=offset
    )
    names = await _device_names(session, rows)
    return Page[ChangeOut](
        items=[_out(row, device_name=names.get(row.device_id)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/changes", response_model=ChangeOut, summary="创建变更单（草稿）")
async def create_change(
    payload: ChangeCreateIn,
    session: SessionDep,
    user: OperatorUser,
) -> ChangeOut:
    row = await changes.create_change(
        session,
        device_id=payload.device_id,
        raw_lines=payload.lines,
        requested_by=user.id,
        note=payload.note,
    )
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.get("/changes/{change_id}", response_model=ChangeDetailOut, summary="变更单详情")
async def get_change(change_id: int, session: SessionDep, _: CurrentUser) -> ChangeDetailOut:
    row = await changes.get_change(session, change_id)
    device = await session.get(Device, row.device_id)
    runs = await changes.latest_runs(session, change_id)
    payload = changes.change_payload(row, device_name=device.name if device else None)
    return ChangeDetailOut(
        **payload,
        lines=list(row.lines or []),
        runs=[DeployRunOut.model_validate(run) for run in runs],
    )


@router.patch("/changes/{change_id}/lines", response_model=ChangeOut, summary="修改命令正文")
async def update_lines(
    change_id: int,
    payload: ChangeLinesIn,
    session: SessionDep,
    user: OperatorUser,
) -> ChangeOut:
    """改内容。

    ★ 已批准的单子改内容会让**原审批立刻作废**（转「审批已失效」，需重新提交），
    这是"批的是 A、发的是 B"那道防线的一部分，不是可以商量的行为。
    """
    row = await changes.get_change(session, change_id)
    await changes.update_lines(
        session, row, actor=user.id, raw_lines=payload.lines, note=payload.note
    )
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/changes/{change_id}/submit", response_model=ChangeOut, summary="提交审批")
async def submit_change(
    change_id: int,
    session: SessionDep,
    user: OperatorUser,
    payload: ChangeCancelIn | None = None,
) -> ChangeOut:
    row = await changes.get_change(session, change_id)
    await changes.submit_change(session, row, actor=user.id, note=payload.note if payload else None)
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/changes/{change_id}/decision", response_model=ChangeOut, summary="批准 / 驳回")
async def decide_change(
    change_id: int,
    payload: ChangeDecisionIn,
    session: SessionDep,
    user: OperatorUser,
    settings: SettingsDep,
) -> ChangeOut:
    """审批。★ 默认**不能自己批自己提的单子**（见 §8.5.1 第 1 条）。"""
    row = await changes.get_change(session, change_id)
    await changes.decide_change(
        session,
        row,
        approver=user.id,
        approve=payload.approve,
        allow_self_approval=settings.deploy_allow_self_approval,
        note=payload.note,
    )
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/changes/{change_id}/cancel", response_model=ChangeOut, summary="撤回变更单")
async def cancel_change(
    change_id: int,
    session: SessionDep,
    user: OperatorUser,
    payload: ChangeCancelIn | None = None,
) -> ChangeOut:
    row = await changes.get_change(session, change_id)
    await changes.cancel_change(session, row, actor=user.id, note=payload.note if payload else None)
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/changes/{change_id}/baseline", response_model=ChangeOut, summary="登记下发前基线")
async def set_baseline(
    change_id: int,
    payload: BaselineIn,
    session: SessionDep,
    user: OperatorUser,
) -> ChangeOut:
    """登记"下发前采集的版本"（§8.5.1 第 3 条：先备份后下发）。

    采集本身走既有的备份接口/定时策略，这里只把**那一次采集的结果**绑到变更单上。
    """
    row = await changes.get_change(session, change_id)
    await changes.record_baseline(
        session, row, snapshot_id=payload.snapshot_id, actor=user.id
    )
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/changes/{change_id}/preview", response_model=PreviewOut, summary="下发预演")
async def preview_change(
    change_id: int,
    session: SessionDep,
    user: OperatorUser,
    settings: SettingsDep,
) -> PreviewOut:
    """预演：跑完整套下发闸门，但**一行都不发送、也不改变更单状态**。

    这里把 ``confirm`` 当作已给：预演要回答的是"除了那次确认，还差什么"。
    确认本身是真正下发时的动作——预演都把 ``confirm_required`` 列出来，
    只会让这块提示永远亮着，运维很快就学会无视它。
    """
    row = await changes.get_change(session, change_id)
    plan, blockers = changes.guard_for(
        row, enabled=settings.deploy_enabled, confirm=True
    )
    await changes.record_dry_run(session, row, actor=user.id, plan=plan)
    await session.commit()
    await session.refresh(row)
    return PreviewOut(
        change_id=row.id,
        state=row.state,
        content_hash=plan.content_hash,
        can_deploy=not blockers,
        plan=plan.as_dict(),
        blockers=[
            {"code": issue.code, "message": issue.message, "severity": issue.severity}
            for issue in blockers
        ],
    )


@router.get(
    "/changes/{change_id}/runs",
    response_model=list[DeployRunOut],
    summary="变更单的预演/下发记录",
)
async def list_runs(
    change_id: int, session: SessionDep, _: CurrentUser
) -> list[DeployRunOut]:
    await changes.get_change(session, change_id)
    runs = await changes.latest_runs(session, change_id)
    return [DeployRunOut.model_validate(run) for run in runs]


@router.get("/deploy/status", response_model=OkResponse, summary="下发功能开关状态")
async def deploy_status(_: CurrentUser, settings: SettingsDep) -> OkResponse:
    """让前端能明确显示"本环境是否开启了配置下发"，而不是让人去猜为什么按钮是灰的。"""
    if settings.deploy_enabled:
        message = "配置下发已开启（本版本尚未接入真实下发执行器，只能预演）"
    else:
        message = "配置下发未开启（DSZOON_DEPLOY_ENABLED=false）"
    return OkResponse(ok=settings.deploy_enabled, message=message)


# --------------------------------------------------------------------------
# P4-C：一键回滚
# --------------------------------------------------------------------------
@router.get(
    "/devices/{device_id}/rollback/preview",
    response_model=RollbackPreviewOut,
    summary="回滚预演（只读）",
)
async def rollback_preview(
    device_id: int,
    session: SessionDep,
    _: CurrentUser,
    blobs: BlobsDep,
    current_snapshot_id: int | None = Query(
        default=None, description="作为「当前版本」的版本 id；留空取最新版本"
    ),
) -> RollbackPreviewOut:
    """挑出"上一个**可用**版本"并算出要下发什么。

    只读：不建变更单、不发任何命令。**跳过了哪些版本、为什么**也会一并返回——
    运维点回滚时看到的是"回到 8-03 那版"，如果 8-05 是因为内容残缺被跳过的，
    界面必须说出来，否则他会以为工具漏了最新一版。
    """
    preview = await changes.rollback_preview(
        session, blobs, device_id=device_id, current_snapshot_id=current_snapshot_id
    )
    return RollbackPreviewOut.model_validate(preview.as_dict())


@router.post(
    "/devices/{device_id}/rollback",
    response_model=RollbackCreateOut,
    summary="生成回滚变更单（草稿）",
)
async def create_rollback(
    device_id: int,
    session: SessionDep,
    user: OperatorUser,
    blobs: BlobsDep,
    payload: RollbackCreateIn | None = None,
) -> RollbackCreateOut:
    """把"回滚到上一个可用版本"变成一张**草稿变更单**。

    ★ 它不跳过任何闸门：照样要第二人审批、照样要登记下发前基线、照样只能先预演。
    §8.5.1 第 4 条明确不搞"紧急通道"——回滚本身也是一次改配置。

    ★ 若这次回滚**没有可自动生成的内容**（真实世界最常见的"多了一行"就是这种），
    接口会返回 422 并列出需要人工写 undo/no 的那些行，而不是造一张空单子。
    """
    row, preview = await changes.create_rollback(
        session,
        blobs,
        device_id=device_id,
        requested_by=user.id,
        current_snapshot_id=payload.current_snapshot_id if payload else None,
        note=payload.note if payload else None,
    )
    await session.commit()
    await session.refresh(row)
    return RollbackCreateOut(
        change=ChangeOut.model_validate(changes.change_payload(row)),
        preview=RollbackPreviewOut.model_validate(preview.as_dict()),
    )
