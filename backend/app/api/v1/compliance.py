"""合规基线接口（P3）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET  | `/compliance/rules`     | 登录 | 当前生效的规则集 + 指纹 |
| GET  | `/compliance/summary`   | 登录 | 全场合规概况（看板用） |
| GET  | `/compliance/report`    | 登录 | "设备 × 规则"矩阵 + 合规率 |
| GET  | `/compliance/export`    | 登录 | 导出报表（CSV/JSON，写审计） |
| POST | `/compliance/run`       | operator | 立即检查（不采集，只评估已有版本） |
| GET  | `/devices/{id}/compliance` | 登录 | 单台设备的最新结果与历史 |

**为什么导出要写审计**：报表虽然不是配置全文，但"哪些设备开了 telnet、哪些用了默认
团体名"本身就是攻击面清单。等保要的正是"谁在何时拿走了这份信息"，所以导出动作必须留痕
（与 P1 的"下载配置"同一处理）。
"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from ...compliance import SEVERITIES, describe_rules
from ...db.models import ComplianceResult
from ...schemas.common import OkResponse, to_utc_iso
from ...schemas.compliance import (
    ComplianceResultOut,
    ComplianceRunRequest,
    ComplianceRunResult,
    ComplianceSummary,
    RuleOut,
    RuleSetOut,
)
from ...services import audit
from ...services.compliance import (
    latest_results,
    report,
    report_rows,
    ruleset,
    run_for_devices,
    summary_for_devices,
)
from ...services.devices import resolve_devices
from ..deps import (
    BlobsDep,
    CurrentUser,
    OperatorUser,
    SessionDep,
    SettingsDep,
    client_ip,
)

router = APIRouter()


def _summary_out(data: dict) -> ComplianceSummary:
    return ComplianceSummary(
        devices=data.get("devices", 0),
        checked=data.get("checked", 0),
        **{
            "pass": data.get("pass", 0),
            "warn": data.get("warn", 0),
            "fail": data.get("fail", 0),
            "unknown": data.get("unknown", 0),
            "high_total": data.get("high_total", 0),
            "compliance_rate": data.get("compliance_rate"),
        },
    )


@router.get("/compliance/rules", response_model=RuleSetOut, summary="当前合规规则集")
async def list_rules(_: CurrentUser, settings: SettingsDep) -> RuleSetOut:
    active = ruleset(settings)
    return RuleSetOut(
        version=active.version,
        source=active.source,
        rules=[RuleOut(**item) for item in describe_rules(active.rules)],
        severities=list(SEVERITIES),
        enabled=bool(settings.compliance_enabled),
    )


@router.get("/compliance/summary", response_model=ComplianceSummary, summary="合规概况")
async def compliance_summary(session: SessionDep, _: CurrentUser) -> ComplianceSummary:
    from ...db.models import Device  # 局部导入：只在需要时拉台账

    device_ids = list((await session.execute(select(Device.id))).scalars().all())
    data = await summary_for_devices(session, device_ids)
    data["devices"] = len(device_ids)
    return _summary_out(data)


@router.get("/compliance/report", summary="合规报表（设备 × 规则矩阵）")
async def compliance_report(
    session: SessionDep,
    _: CurrentUser,
    settings: SettingsDep,
    group_id: int | None = None,
    vendor: str | None = None,
    site: str | None = None,
) -> dict:
    """返回矩阵 + 汇总 + 按厂商/站点分组的合规率。

    注意：**没有评估过的设备也会出现在矩阵里**（``verdict = "unknown"``）——
    运维要看的恰恰是"谁还没查过"，把它们藏起来才是误导。
    """
    data = await report(session, group_id=group_id, vendor=vendor, site=site)
    # 前端要能区分"规则为空/被停用"与"接口出问题"
    data["enabled"] = bool(settings.compliance_enabled)
    return data


@router.get("/compliance/export", summary="导出合规报表（CSV/JSON）")
async def export_report(
    request: Request,
    session: SessionDep,
    actor: CurrentUser,
    group_id: int | None = None,
    vendor: str | None = None,
    site: str | None = None,
    fmt: str = Query(default="csv", pattern="^(csv|json)$"),
) -> Response:
    data = await report(session, group_id=group_id, vendor=vendor, site=site)
    await audit.record(
        session,
        action="compliance.export",
        user_id=actor.id,
        username=actor.username,
        target_type="compliance",
        detail={"format": fmt, "devices": len(data.get("matrix") or [])},
        client_ip=client_ip(request),
    )
    await session.commit()

    if fmt == "json":
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        return Response(
            content=payload,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="compliance.json"'},
        )

    rows = report_rows(data)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    # ★ utf-8-sig（带 BOM）：Excel 打开不带 BOM 的 CSV 会把中文显示成乱码，
    #   而这份报表的主要读者就是拿 Excel 看的人
    content = buffer.getvalue().encode("utf-8-sig")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="compliance.csv"'},
    )


@router.post("/compliance/run", response_model=ComplianceRunResult, summary="立即合规检查")
async def run_compliance(
    payload: ComplianceRunRequest,
    request: Request,
    session: SessionDep,
    blobs: BlobsDep,
    actor: OperatorUser,
    settings: SettingsDep,
) -> ComplianceRunResult:
    """对指定范围内的设备**重新评估**（用它们各自最新的版本，不重新采集）。"""
    devices = await resolve_devices(
        session,
        device_ids=payload.device_ids,
        group_id=payload.group_id,
        site=payload.site,
        vendor=payload.vendor,
        include_disabled=payload.include_disabled,
        # 全量检查：与备份接口同样的范围语义，避免"一键全查"在合规这边做不出来
        all_enabled=payload.all_enabled,
    )
    active = ruleset(settings)
    results, failed = await run_for_devices(session, devices, blobs=blobs, rules=active)
    await audit.record(
        session,
        action="compliance.run",
        user_id=actor.id,
        username=actor.username,
        target_type="compliance",
        detail={
            "devices": len(devices),
            "checked": len(results),
            "failed": failed,
            "rules_version": active.version,
        },
        client_ip=client_ip(request),
    )
    await session.commit()

    verdicts: dict[str, int] = {}
    for row in results:
        verdicts[row.verdict] = verdicts.get(row.verdict, 0) + 1
    return ComplianceRunResult(
        checked=len(results),
        skipped_no_snapshot=len(devices) - len(results) - failed,
        failed=failed,
        verdicts=verdicts,
        rules_version=active.version,
    )


@router.get(
    "/devices/{device_id}/compliance",
    summary="单台设备的合规结果（最新 + 历史）",
)
async def device_compliance(
    device_id: int,
    session: SessionDep,
    _: CurrentUser,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    from ...services.devices import get_device

    await get_device(session, device_id)  # 不存在直接 404
    latest = (await latest_results(session, device_ids=[device_id])).get(device_id)
    history = list(
        (
            await session.execute(
                select(ComplianceResult)
                .where(ComplianceResult.device_id == device_id)
                .order_by(ComplianceResult.checked_at.desc(), ComplianceResult.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "device_id": device_id,
        # ★ mode="json" 才会走 UtcDatetime 的序列化器（补上 Z）；用默认的 python 模式
        #   会返回裸 datetime，FastAPI 再编码出来就**没有时区标记**，前端会差 8 小时
        "latest": ComplianceResultOut.model_validate(latest).model_dump(mode="json")
        if latest is not None
        else None,
        "history": [
            {
                "id": row.id,
                "checked_at": to_utc_iso(row.checked_at),
                "verdict": row.verdict,
                "high": row.high_count,
                "medium": row.medium_count,
                "low": row.low_count,
                "rules_version": row.rules_version,
            }
            for row in history
        ],
    }


@router.post("/compliance/cache/reset", response_model=OkResponse, summary="重新加载规则文件")
async def reset_cache(_: OperatorUser) -> OkResponse:
    """改完规则文件后调它，不必重启进程（规则集是按路径缓存的）。"""
    from ...services.compliance import reset_rules_cache

    reset_rules_cache()
    return OkResponse(message="规则缓存已清空，下一次评估会重新读取规则文件")
