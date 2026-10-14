"""变更单的数据库编排（P4-B）。

分层理由：``services/deploy.py`` 只管**纯逻辑**（状态迁移表、下发/审批闸门、
回滚目标选择），本模块负责把它接到数据库与审计上。

为什么非要分开：纯逻辑那一层要能被穷举测试。"批 A 发 B""没有基线就下发""中止后
被追认成功"这些分支在真机上永远不会自然出现，只能靠构造数据去验；一旦这些判断
和 ``AsyncSession`` 混在一个文件里，单测就得连库跑，最后一定验不全。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..collector.deploy import DeployOutcome, DeployPlan, plan_deploy
from ..collector.integrity import Issue
from ..db.base import utcnow
from ..db.models import BackupRun, ChangeRequest, ConfigSnapshot, DeployRun, Device
from ..errors import Conflict, NotFound, ValidationFailed
from . import audit
from .blobs import BlobStore
from .deploy import (
    CHANGE_APPROVED,
    CHANGE_CANCELLED,
    CHANGE_DEPLOYING,
    CHANGE_DRAFT,
    CHANGE_EXPIRED,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    CHANGE_STATE_LABELS,
    ChangeView,
    RollbackCandidate,
    RollbackDecision,
    RollbackPlan,
    can_transition,
    check_approval_allowed,
    check_deploy_allowed,
    check_submit_allowed,
    rollback_lines,
    select_rollback_target,
    state_after_outcome,
)
from .diffs import diff_snapshots
from .snapshots import latest_snapshot, load_config

__all__ = [
    "DRY_RUN",
    "RollbackPreview",
    "cancel_change",
    "change_payload",
    "change_view",
    "create_change",
    "create_rollback",
    "decide_change",
    "get_change",
    "guard_for",
    "latest_runs",
    "list_changes",
    "mark_deploying",
    "plan_of",
    "record_baseline",
    "record_deploy_run",
    "record_dry_run",
    "rollback_candidates",
    "rollback_preview",
    "submit_change",
    "update_lines",
]

#: 预演记录的 run 状态。
#:
#: 刻意**不放进** ``collector/deploy.py`` 的结论词表（那里只有 success/stopped/rejected）：
#: 那三个是"真实下发"的结论，预演一行都没发出去，混进去会让"这次到底发了没有"
#: 变成要再看一个字段才能回答的问题。
DRY_RUN = "dry_run"


def _issues_detail(issues: Sequence[Issue]) -> list[dict[str, str]]:
    return [
        {"code": issue.code, "message": issue.message, "severity": issue.severity}
        for issue in issues
    ]


def change_view(row: ChangeRequest) -> ChangeView:
    """ORM 行 → 纯逻辑用的视图。"""
    return ChangeView(
        id=row.id,
        device_id=row.device_id,
        state=row.state,
        content_hash=row.content_hash,
        requested_by=row.requested_by,
        approved_by=row.approved_by,
        before_snapshot_id=row.before_snapshot_id,
        before_captured_at=row.before_captured_at,
        created_at=row.created_at,
    )


def plan_of(row: ChangeRequest) -> DeployPlan:
    """按**当前存储的命令**重算预检与哈希。

    ★ 永远从库里的 ``lines`` 现算，而不是信任 ``content_hash`` 字段：
    哈希字段只是"审批那一刻的快照"，现算才能发现"批完又被人改过"。
    """
    return plan_deploy(row.lines or [])


def _add_timeline(
    row: ChangeRequest, action: str, *, by: int | None, note: str | None = None
) -> None:
    """追加时间线。

    ★ 必须**整体重新赋值**而不是 ``row.timeline.append(...)``：JSON 列不跟踪原地
    修改，append 之后 SQLAlchemy 认为这一行没变、commit 时不会写库——表现是
    "界面上时间线永远是空的"，而且完全不报错。
    """
    row.timeline = [
        *(row.timeline or []),
        {"at": utcnow().isoformat(sep=" "), "action": action, "by": by, "note": note},
    ]


async def get_change(session: AsyncSession, change_id: int) -> ChangeRequest:
    row = await session.get(ChangeRequest, change_id)
    if row is None:
        raise NotFound(f"变更单不存在：{change_id}")
    return row


def change_payload(
    row: ChangeRequest, *, device_name: str | None = None
) -> dict[str, Any]:
    """对外输出用的字典（补上状态中文名与命令行数）。

    放在服务层而不是 schema 里：状态词表与中文名是**业务语义**，
    它属于 ``services/deploy.py``；schema 只负责校验与序列化。
    """
    return {
        "id": row.id,
        "device_id": row.device_id,
        "device_name": device_name,
        "kind": row.kind,
        "state": row.state,
        "state_label": CHANGE_STATE_LABELS.get(row.state, row.state),
        "content_hash": row.content_hash,
        "line_count": len(row.lines or []),
        "requested_by": row.requested_by,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "approval_note": row.approval_note,
        "target_snapshot_id": row.target_snapshot_id,
        "before_snapshot_id": row.before_snapshot_id,
        "before_captured_at": row.before_captured_at,
        "after_snapshot_id": row.after_snapshot_id,
        "reason": row.reason,
        "precheck": row.precheck or {},
        "timeline": row.timeline or [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def update_lines(
    session: AsyncSession,
    row: ChangeRequest,
    *,
    actor: int,
    raw_lines: Sequence[str],
    note: str | None = None,
) -> ChangeRequest:
    """修改命令正文。

    ★ 已经批准的单子被改内容时，**审批立刻作废**（转 ``expired``），而不是"改完接着发"：
    批准是对**某一份具体内容**的批准。允许改完继续发，等于把审批变成一个摆设，
    而且事后看记录只会看到"批准过"，看不到"批的其实是别的东西"。
    """
    if row.state == CHANGE_APPROVED:
        # 允许改，但代价是重新走一遍审批
        pass
    elif not can_transition(row.state, CHANGE_PENDING) and row.state != CHANGE_DRAFT:
        raise Conflict(
            f"当前状态「{CHANGE_STATE_LABELS.get(row.state, row.state)}」不能修改内容："
            "待审批的单子请先撤回再改、或等它被批准/驳回；"
            "下发中与已结束的单子请新建一张"
        )

    plan = plan_deploy(raw_lines)
    if not plan.ok:
        raise ValidationFailed(
            "修改后的命令未通过预检",
            detail=_issues_detail([i for i in plan.issues if i.is_error]),
        )

    previous_state = row.state
    row.lines = list(plan.lines)
    row.content_hash = plan.content_hash
    row.precheck = plan.as_dict()
    if previous_state == CHANGE_APPROVED:
        row.state = CHANGE_EXPIRED
        row.approved_by = None
        row.approved_at = None
        row.approval_note = None
        _add_timeline(row, "expire", by=actor, note="内容被修改，原审批作废")
    _add_timeline(row, "edit", by=actor, note=note)
    await audit.record(
        session,
        action="deploy.edit",
        user_id=actor,
        target_type="change_request",
        target_id=row.id,
        detail={
            "device_id": row.device_id,
            "hash": plan.content_hash[:12],
            "from_state": previous_state,
            "state": row.state,
        },
    )
    return row


async def _insert_change(
    session: AsyncSession,
    *,
    device_id: int,
    plan: DeployPlan,
    requested_by: int,
    note: str | None = None,
    kind: str = "config",
    target_snapshot_id: int | None = None,
) -> ChangeRequest:
    """建单的公共路径（普通变更与回滚共用）。

    预检不通过时**直接拒绝创建**，而不是"先存下来、提交时再拦"：
    危险命令不进系统，就不存在"草稿被谁复制出去手工执行"这条路径。
    回滚也走这一条，所以**回滚内容同样要过危险命令检查**——从历史版本里读出来的行
    也不天然可信。
    """
    if not plan.ok:
        raise ValidationFailed(
            "待下发的命令未通过预检，已拒绝创建变更单",
            detail=_issues_detail([i for i in plan.issues if i.is_error]),
        )

    row = ChangeRequest(
        device_id=device_id,
        kind=kind,
        state=CHANGE_DRAFT,
        lines=list(plan.lines),
        content_hash=plan.content_hash,
        precheck=plan.as_dict(),
        requested_by=requested_by,
        target_snapshot_id=target_snapshot_id,
        timeline=[],
    )
    _add_timeline(row, "create", by=requested_by, note=note)
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        action="deploy.create",
        user_id=requested_by,
        target_type="change_request",
        target_id=row.id,
        detail={
            "device_id": device_id,
            "kind": kind,
            "lines": len(plan.lines),
            "hash": plan.content_hash[:12],
            "target_snapshot_id": target_snapshot_id,
        },
    )
    return row


async def create_change(
    session: AsyncSession,
    *,
    device_id: int,
    raw_lines: Sequence[str],
    requested_by: int,
    note: str | None = None,
) -> ChangeRequest:
    """创建草稿变更单（手工写的配置变更）。"""
    device = await session.get(Device, device_id)
    if device is None:
        raise NotFound(f"设备不存在：{device_id}")

    return await _insert_change(
        session,
        device_id=device_id,
        plan=plan_deploy(raw_lines),
        requested_by=requested_by,
        note=note,
    )


async def submit_change(
    session: AsyncSession,
    row: ChangeRequest,
    *,
    actor: int,
    note: str | None = None,
) -> ChangeRequest:
    """提交审批：草稿 → 待审批（中止/审批失效的单子也走这里重新提交）。"""
    if not can_transition(row.state, CHANGE_PENDING):
        raise Conflict(
            f"当前状态「{CHANGE_STATE_LABELS.get(row.state, row.state)}」不能提交审批"
        )

    plan = plan_of(row)
    issues = check_submit_allowed(plan)
    if issues:
        raise Conflict("预检未通过，不能提交审批", detail=_issues_detail(issues))

    row.state = CHANGE_PENDING
    # 重新提交以**当前内容**为准更新哈希：上一轮审批已经作废，不能让它复活
    row.content_hash = plan.content_hash
    row.precheck = plan.as_dict()
    row.approved_by = None
    row.approved_at = None
    row.approval_note = None
    _add_timeline(row, "submit", by=actor, note=note)
    await audit.record(
        session,
        action="deploy.submit",
        user_id=actor,
        target_type="change_request",
        target_id=row.id,
        detail={"device_id": row.device_id, "hash": plan.content_hash[:12]},
    )
    return row


async def decide_change(
    session: AsyncSession,
    row: ChangeRequest,
    *,
    approver: int,
    approve: bool,
    allow_self_approval: bool = False,
    note: str | None = None,
) -> ChangeRequest:
    """批准或驳回。★ 驳回必须写理由——"为什么不许改"是下一个人唯一的线索。"""
    issues = check_approval_allowed(
        change_view(row), approver_id=approver, allow_self_approval=allow_self_approval
    )
    if issues:
        raise Conflict("不能审批这张变更单", detail=_issues_detail(issues))
    if not approve and not (note or "").strip():
        raise ValidationFailed("驳回必须写明理由")

    row.state = CHANGE_APPROVED if approve else CHANGE_REJECTED
    row.approved_by = approver
    row.approved_at = utcnow()
    row.approval_note = note
    if not approve:
        row.reason = note
    _add_timeline(row, "approve" if approve else "reject", by=approver, note=note)
    await audit.record(
        session,
        action="deploy.approve" if approve else "deploy.reject",
        user_id=approver,
        target_type="change_request",
        target_id=row.id,
        detail={"device_id": row.device_id, "note": note},
    )
    return row


async def cancel_change(
    session: AsyncSession, row: ChangeRequest, *, actor: int, note: str | None = None
) -> ChangeRequest:
    if not can_transition(row.state, CHANGE_CANCELLED):
        raise Conflict(
            f"当前状态「{CHANGE_STATE_LABELS.get(row.state, row.state)}」不能撤回"
        )
    row.state = CHANGE_CANCELLED
    if note:
        row.reason = note
    _add_timeline(row, "cancel", by=actor, note=note)
    await audit.record(
        session,
        action="deploy.cancel",
        user_id=actor,
        target_type="change_request",
        target_id=row.id,
        detail={"device_id": row.device_id},
    )
    return row


async def record_baseline(
    session: AsyncSession, row: ChangeRequest, *, snapshot_id: int, actor: int
) -> ChangeRequest:
    """登记"下发前的采集基线"（§8.5.1 第 3 条：先备份后下发）。

    只接受**本设备**的版本：拿别的设备的采集当基线，等于没有回滚点。
    （"采集时间必须晚于变更单创建时间"由下发闸门 ``stale_baseline`` 判定，
    这里只记录事实，不代替闸门做决定。）
    """
    snapshot = await session.get(ConfigSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFound(f"配置版本不存在：{snapshot_id}")
    if snapshot.device_id != row.device_id:
        raise ValidationFailed("基线必须是同一台设备的配置版本")

    row.before_snapshot_id = snapshot.id
    # 时间戳单独存一份：版本可能被保留策略清理掉（外键 SET NULL），
    # 那时如果只剩 id 就查不到时间了，"基线是不是为这次变更采的"会永远无法回答。
    row.before_captured_at = snapshot.captured_at
    _add_timeline(row, "baseline", by=actor, note=f"snapshot={snapshot.id}")
    await audit.record(
        session,
        action="deploy.baseline",
        user_id=actor,
        target_type="change_request",
        target_id=row.id,
        detail={"device_id": row.device_id, "snapshot_id": snapshot.id},
    )
    return row


def guard_for(
    row: ChangeRequest, *, enabled: bool, confirm: bool
) -> tuple[DeployPlan, list[Issue]]:
    """下发前的闸门（路由与执行器都走这一个入口，避免两处判定分叉）。"""
    plan = plan_of(row)
    return plan, check_deploy_allowed(
        change_view(row), plan, enabled=enabled, confirm=confirm
    )


async def mark_deploying(
    session: AsyncSession, row: ChangeRequest, *, actor: int
) -> ChangeRequest:
    """进入「下发中」。真正的逐行发送由 P4-A 的执行器接在这之后。"""
    if not can_transition(row.state, CHANGE_DEPLOYING):
        raise Conflict(
            f"当前状态「{CHANGE_STATE_LABELS.get(row.state, row.state)}」不能下发"
        )
    row.state = CHANGE_DEPLOYING
    _add_timeline(row, "deploy_start", by=actor)
    return row


async def record_dry_run(
    session: AsyncSession, row: ChangeRequest, *, actor: int, plan: DeployPlan
) -> DeployRun:
    """记录一次**预演**：只做预检，一行都没发出去。

    ★ 预演**不改变更单状态**，也绝不走 ``state_after_outcome``。
    如果让它走，一条"预检通过"的预演会被映射成「已生效」——那是谎报，
    而且是最危险的那种：接手的人会以为配置已经改上去了。
    """
    run = DeployRun(
        change_request_id=row.id,
        device_id=row.device_id,
        executed_by=actor,
        started_at=utcnow(),
        finished_at=utcnow(),
        status=DRY_RUN if plan.ok else "rejected",
        dry_run=True,
        content_hash=plan.content_hash,
        applied_lines=0,
        stopped_at=None,
        # 没有逐行结果：一行都没发，编出一份"每行都 ok"的表才是误导
        results=[],
        error=None if plan.ok else "；".join(i.message for i in plan.issues if i.is_error),
    )
    session.add(run)
    _add_timeline(
        row,
        "dry_run",
        by=actor,
        note=f"预检{'通过' if plan.ok else '未通过'}，未发送任何命令",
    )
    await session.flush()
    await audit.record(
        session,
        action="deploy.dry_run",
        user_id=actor,
        target_type="change_request",
        target_id=row.id,
        detail={
            "device_id": row.device_id,
            "run_id": run.id,
            "ok": plan.ok,
            "content_hash": plan.content_hash[:12],
        },
    )
    return run


async def record_deploy_run(
    session: AsyncSession,
    row: ChangeRequest,
    *,
    actor: int,
    outcome: DeployOutcome,
    after_snapshot_id: int | None = None,
) -> DeployRun:
    """把一次**真实下发**的结论写库（逐行结果 + 状态迁移 + 审计）。

    ★ 状态映射失败时**拒绝写库**（``Conflict``），而不是"挑一个像样的状态存进去"：
    写进去之后就再也分不清"设备到底停在哪一步"了。
    """
    target = state_after_outcome(row.state, outcome.status)
    if target is None:
        raise Conflict(
            f"下发结论 {outcome.status!r} 不能从状态 "
            f"「{CHANGE_STATE_LABELS.get(row.state, row.state)}」迁移，已拒绝写库"
        )

    run = DeployRun(
        change_request_id=row.id,
        device_id=row.device_id,
        executed_by=actor,
        started_at=utcnow(),
        finished_at=utcnow(),
        status=outcome.status,
        dry_run=False,
        content_hash=outcome.content_hash,
        applied_lines=len(outcome.applied),
        stopped_at=outcome.stopped_at.index if outcome.stopped_at else None,
        results=[item.as_dict() for item in outcome.results],
        error=outcome.error,
    )
    session.add(run)

    row.state = target
    row.reason = outcome.summary()
    if after_snapshot_id is not None:
        row.after_snapshot_id = after_snapshot_id
    _add_timeline(row, f"deploy_{outcome.status}", by=actor, note=outcome.summary())
    await session.flush()

    # 约束 5：审计里必须带 before/after 版本 id——这是事后唯一能追责的依据
    await audit.record(
        session,
        action="deploy.run",
        user_id=actor,
        target_type="change_request",
        target_id=row.id,
        detail={
            "device_id": row.device_id,
            "run_id": run.id,
            "status": outcome.status,
            "applied_lines": run.applied_lines,
            "stopped_at": run.stopped_at,
            "content_hash": outcome.content_hash[:12],
            "before_snapshot_id": row.before_snapshot_id,
            "after_snapshot_id": row.after_snapshot_id,
        },
    )
    return run


async def list_changes(
    session: AsyncSession,
    *,
    device_id: int | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ChangeRequest], int]:
    conditions: list[Any] = []
    if device_id is not None:
        conditions.append(ChangeRequest.device_id == device_id)
    if state is not None:
        conditions.append(ChangeRequest.state == state)

    total = await session.scalar(
        select(func.count()).select_from(ChangeRequest).where(*conditions)
    )
    result = await session.execute(
        select(ChangeRequest)
        .where(*conditions)
        .order_by(ChangeRequest.created_at.desc(), ChangeRequest.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def latest_runs(session: AsyncSession, change_id: int) -> list[DeployRun]:
    result = await session.execute(
        select(DeployRun)
        .where(DeployRun.change_request_id == change_id)
        .order_by(DeployRun.created_at.desc(), DeployRun.id.desc())
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------
# P4-C：一键回滚到上一个可用版本
# --------------------------------------------------------------------------
async def rollback_candidates(
    session: AsyncSession, device_id: int
) -> list[RollbackCandidate]:
    """把设备的历史版本整理成回滚候选（含"这个版本可不可信"的判定材料）。

    ★ 采集状态取的是**创建这个版本的那次采集**（该版本最早的一条 run）：
    同一份内容之后还会被 ``unchanged`` 的采集反复引用，随便取一条会把它误判成
    "无变更"，于是一个完全可用的版本会被排除掉。

    ★ 同时把这个版本**所有** run 的 issue 码合并起来，取偏保守的那一侧：
    只要有一次采集说它的内容可疑（截断、折行、命令报错回显……），这个版本就
    不该被拿来当回滚基线——回滚的基线错了，等于用一个坏状态去覆盖现场。
    """
    rows = (
        await session.execute(
            select(
                ConfigSnapshot.id,
                ConfigSnapshot.captured_at,
                ConfigSnapshot.sha256,
                ConfigSnapshot.line_count,
                BackupRun.status,
                BackupRun.issues,
            )
            .outerjoin(BackupRun, BackupRun.snapshot_id == ConfigSnapshot.id)
            .where(ConfigSnapshot.device_id == device_id)
            # 先按版本从新到旧，再按采集从旧到新 → 每个版本看到的第一条就是"创建它的那次"
            .order_by(
                ConfigSnapshot.captured_at.desc(),
                BackupRun.started_at.asc(),
                BackupRun.id.asc(),
            )
        )
    ).all()

    grouped: dict[int, dict[str, Any]] = {}
    for snapshot_id, captured_at, sha256, line_count, status, issues in rows:
        item = grouped.setdefault(
            int(snapshot_id),
            {
                "captured_at": captured_at,
                "sha256": sha256,
                "line_count": int(line_count or 0),
                "status": None,
                "issues": [],
            },
        )
        if status is not None and item["status"] is None:
            item["status"] = str(status)
        for issue in issues or []:
            code = issue.get("code") if isinstance(issue, dict) else None
            if code and code not in item["issues"]:
                item["issues"].append(str(code))

    return [
        RollbackCandidate(
            snapshot_id=snapshot_id,
            captured_at=item["captured_at"],
            run_status=item["status"],
            issue_codes=tuple(item["issues"]),
            line_count=item["line_count"],
            sha256=item["sha256"] or "",
        )
        for snapshot_id, item in grouped.items()
    ]


@dataclass(slots=True)
class RollbackPreview:
    """"一键回滚"的预演结果：**只计算，不建单、不发命令**。"""

    device_id: int
    current_snapshot_id: int | None
    decision: RollbackDecision
    plan: RollbackPlan | None = None
    diff: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.decision.ok and self.plan is not None

    def as_dict(self) -> dict[str, Any]:
        target = self.decision.target
        return {
            "device_id": self.device_id,
            "ok": self.ok,
            "current_snapshot_id": self.current_snapshot_id,
            "reason": self.decision.reason,
            "target": (
                {
                    "snapshot_id": target.snapshot_id,
                    "captured_at": target.captured_at.isoformat(sep=" "),
                    "line_count": target.line_count,
                    "sha256": target.sha256[:12],
                }
                if target is not None
                else None
            ),
            # 跳过了谁、为什么——不列出来，运维会以为工具漏了最新那一版
            "skipped": [
                {"snapshot_id": snapshot_id, "reason": reason}
                for snapshot_id, reason in self.decision.skipped
            ],
            "rollback": self.plan.as_dict() if self.plan is not None else None,
            "diff": self.diff,
        }


async def rollback_preview(
    session: AsyncSession,
    blobs: BlobStore,
    *,
    device_id: int,
    current_snapshot_id: int | None = None,
) -> RollbackPreview:
    """算"回滚到上一个可用版本"要做的事：目标是哪个版本、跳过了谁、补什么、还差什么人工处理。"""
    device = await session.get(Device, device_id)
    if device is None:
        raise NotFound(f"设备不存在：{device_id}")

    current = (
        await session.get(ConfigSnapshot, current_snapshot_id)
        if current_snapshot_id is not None
        else await latest_snapshot(session, device_id)
    )
    candidates = await rollback_candidates(session, device_id)
    decision = select_rollback_target(
        candidates, current_snapshot_id=current.id if current is not None else None
    )
    preview = RollbackPreview(
        device_id=device_id,
        current_snapshot_id=current.id if current is not None else None,
        decision=decision,
    )
    if not decision.ok or decision.target is None:
        return preview

    target = await session.get(ConfigSnapshot, decision.target.snapshot_id)
    if target is None:  # 竞态：刚被保留策略清理掉
        preview.decision = RollbackDecision(
            target=None,
            skipped=decision.skipped,
            reason="选中的版本已经不存在（可能刚被保留策略清理），请刷新后重试",
        )
        return preview

    preview.plan = rollback_lines(
        load_config(blobs, target),
        load_config(blobs, current) if current is not None else "",
        target_snapshot_id=target.id,
    )
    if current is not None:
        result = await diff_snapshots(
            session, blobs, device=device, from_snapshot=current, to_snapshot=target
        )
        preview.diff = {
            "from_snapshot_id": result.from_snapshot_id,
            "to_snapshot_id": result.to_snapshot_id,
            "identical": result.identical,
            "added": result.added,
            "removed": result.removed,
            "changed": result.changed,
            "significant": list(result.significant),
            "unified": result.unified,
        }
    return preview


async def create_rollback(
    session: AsyncSession,
    blobs: BlobStore,
    *,
    device_id: int,
    requested_by: int,
    current_snapshot_id: int | None = None,
    note: str | None = None,
) -> tuple[ChangeRequest, RollbackPreview]:
    """生成一张 ``kind=rollback`` 的变更单（草稿）。

    ★ 它**不跳过任何闸门**：同样过预检、同样要第二人审批、同样要登记下发前基线、
    同样只能预演。§8.5.1 第 4 条明确说了不搞"紧急通道"——回滚本身也是一次改配置。
    """
    preview = await rollback_preview(
        session, blobs, device_id=device_id, current_snapshot_id=current_snapshot_id
    )
    if not preview.ok or preview.plan is None:
        raise Conflict(
            preview.decision.reason or "没有可用的回滚目标",
            detail=preview.decision.as_dict(),
        )

    plan = preview.plan
    if plan.empty:
        # 这是**主路径**而不是边角：真实世界最常见的变更就是"多了一行"，
        # 而删掉它要写 undo/no，不能猜。如实告诉人"这次要你手写删除命令"，
        # 比造一个空变更单让人对着发呆好。
        raise ValidationFailed(
            "这次回滚没有可自动生成的内容：当前版本里有目标版本没有的行，"
            "从设备上删掉它们要写 undo/no 命令，必须人工确认后填写",
            detail={
                "target_snapshot_id": plan.target_snapshot_id,
                "needs_manual": list(plan.needs_manual[:50]),
                "needs_manual_total": len(plan.needs_manual),
            },
        )

    row = await _insert_change(
        session,
        device_id=device_id,
        plan=plan_deploy(plan.lines),
        requested_by=requested_by,
        note=note,
        kind="rollback",
        target_snapshot_id=plan.target_snapshot_id,
    )
    return row, preview
