"""审计日志查询与导出（P3）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/audit-logs` | auditor | 分页查询（动作/用户/对象/时间范围） |
| GET | `/audit-logs/actions` | auditor | 动作字典（筛选下拉用） |
| GET | `/audit-logs/downloads` | auditor | **等保那一问**：谁在何时下载了哪台设备的配置 |
| GET | `/audit-logs/export` | auditor | 导出 CSV/JSON（本身也写审计） |

三个刻意的决定：

1. **读审计要 auditor 起步**（不是"登录就行"）：审计日志里写着谁在什么时候看过哪台
   设备的明文配置，本身就是敏感信息——它比配置本身更该受限。
2. **导出也写审计**：导出意味着数据离开了系统边界，"谁把审计导走了"必须留痕，
   否则审计能力可以被用来规避审计。
3. **CSV 用 utf-8-sig**：报表的主要读者是拿 Excel 打开的人，不带 BOM 的中文在 Excel 里
   就是乱码（P1 已经在这个坑上栽过一次）。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, or_, select

from ...db.models import AuditLog
from ...schemas.audit import AuditActionOut, AuditLogOut, DownloadRecordOut
from ...schemas.common import Page
from ...services import audit
from ..deps import AuditorUser, SessionDep, client_ip

router = APIRouter()

#: "谁下载了配置"这一类动作（等保追溯的对象）
DOWNLOAD_ACTIONS = ("snapshot.download", "snapshot.view_plaintext")


def _conditions(
    *,
    action: str | None,
    actions: tuple[str, ...] | None = None,
    username: str | None,
    target_type: str | None,
    target_id: str | None,
    q: str | None,
    since: datetime | None,
    until: datetime | None,
):
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if actions:
        conditions.append(AuditLog.action.in_(list(actions)))
    if username:
        conditions.append(AuditLog.username == username)
    if target_type:
        conditions.append(AuditLog.target_type == target_type)
    if target_id:
        conditions.append(AuditLog.target_id == str(target_id))
    if since is not None:
        conditions.append(AuditLog.created_at >= since)
    if until is not None:
        conditions.append(AuditLog.created_at <= until)
    if q:
        # 只搜**普通列**：detail 是 JSON 列，SQLite 存 TEXT、PostgreSQL 是 jsonb，
        # 用同一句 LIKE 去搜它在两种库上行为不同（PG 需要类型转换）。宁可少一个搜索
        # 维度，也不要写一句"在 SQLite 上能跑、在 PG 上报错"的查询。
        like = f"%{q}%"
        conditions.append(
            or_(
                AuditLog.username.ilike(like),
                AuditLog.action.ilike(like),
                AuditLog.target_id.ilike(like),
                AuditLog.target_type.ilike(like),
            )
        )
    return conditions


@router.get("/audit-logs/actions", response_model=list[AuditActionOut], summary="审计动作字典")
async def list_actions(_: AuditorUser) -> list[AuditActionOut]:
    return [AuditActionOut(value=key, label=label) for key, label in audit.ACTIONS.items()]


@router.get("/audit-logs/downloads", response_model=Page[DownloadRecordOut], summary="配置下载记录")
async def download_records(
    session: SessionDep,
    _: AuditorUser,
    username: str | None = None,
    since: datetime | None = Query(default=None, description="起始时间（UTC，含）"),
    until: datetime | None = Query(default=None, description="结束时间（UTC，含）"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[DownloadRecordOut]:
    """等保验收问的那一问：**谁在何时下载了哪台设备的配置**。

    ``snapshot.download`` = 下载文件，``snapshot.view_plaintext`` = 在界面上看了明文
    （后者更敏感：不需要落地文件也能看到口令）。
    """
    conditions = _conditions(
        action=None,
        actions=DOWNLOAD_ACTIONS,
        username=username,
        target_type=None,
        target_id=None,
        q=None,
        since=since,
        until=until,
    )
    total = await session.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    result = await session.execute(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        DownloadRecordOut(
            id=row.id,
            created_at=row.created_at,
            username=row.username,
            action=row.action,
            action_label=audit.ACTIONS.get(row.action, row.action),
            # 设备名在 P1 写审计时就存进 detail 了（见 app/api/v1/snapshots.py）
            device=(row.detail or {}).get("device"),
            snapshot_id=row.target_id,
            client_ip=row.client_ip,
            detail=row.detail or {},
        )
        for row in result.scalars().all()
    ]
    return Page[DownloadRecordOut](items=items, total=int(total or 0), limit=limit, offset=offset)


@router.get("/audit-logs", response_model=Page[AuditLogOut], summary="审计日志")
async def list_audit_logs(
    session: SessionDep,
    _: AuditorUser,
    action: str | None = None,
    username: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    q: str | None = Query(default=None, description="按用户/动作/对象模糊搜索"),
    since: datetime | None = Query(default=None, description="起始时间（UTC，含）"),
    until: datetime | None = Query(default=None, description="结束时间（UTC，含）"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[AuditLogOut]:
    conditions = _conditions(
        action=action,
        username=username,
        target_type=target_type,
        target_id=target_id,
        q=q,
        since=since,
        until=until,
    )
    total = await session.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    result = await session.execute(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return Page[AuditLogOut](
        items=[AuditLogOut.model_validate(row) for row in result.scalars().all()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get("/audit-logs/export", summary="导出审计日志（CSV/JSON）")
async def export_audit_logs(
    request: Request,
    session: SessionDep,
    actor: AuditorUser,
    action: str | None = None,
    username: str | None = None,
    target_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    fmt: str = Query(default="csv", pattern="^(csv|json)$"),
    limit: int = Query(default=5000, ge=1, le=50000),
) -> Response:
    conditions = _conditions(
        action=action,
        username=username,
        target_type=target_type,
        target_id=None,
        q=None,
        since=since,
        until=until,
    )
    result = await session.execute(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())

    # ★ 导出本身也要留痕：否则"审计能力"可以被用来规避审计
    await audit.record(
        session,
        action="audit.export",
        user_id=actor.id,
        username=actor.username,
        target_type="audit",
        detail={"rows": len(rows), "format": fmt, "action": action, "username": username},
        client_ip=client_ip(request),
    )
    await session.commit()

    records = [
        {
            "id": row.id,
            "created_at": row.created_at.isoformat(sep=" ", timespec="seconds"),
            "username": row.username or "",
            "action": row.action,
            "action_label": audit.ACTIONS.get(row.action, row.action),
            "target_type": row.target_type or "",
            "target_id": row.target_id or "",
            "client_ip": row.client_ip or "",
            "detail": json.dumps(row.detail or {}, ensure_ascii=False),
        }
        for row in rows
    ]

    if fmt == "json":
        return Response(
            content=json.dumps(records, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit-logs.json"'},
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["时间", "用户", "动作", "动作说明", "对象类型", "对象", "来源 IP", "详情"]
    )
    for record in records:
        writer.writerow(
            [
                record["created_at"],
                record["username"],
                record["action"],
                record["action_label"],
                record["target_type"],
                record["target_id"],
                record["client_ip"],
                record["detail"],
            ]
        )
    # utf-8-sig：Excel 直接双击打开时，不带 BOM 的中文是乱码
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-logs.csv"'},
    )
