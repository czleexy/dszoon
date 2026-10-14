"""合规基线的服务层：加载规则、评估、存结果、出报表。

三条贯穿全文件的原则：

1. **评估失败绝不影响备份**（与通知同一条不变量）：调用点在 ``BackupRunner`` 的收尾
   路径上，任何异常都必须被吞掉并记日志，否则"备份成功但任务失败"就成了常态。
2. **存的是脱敏后的行摘录**：违规行本身往往就是含密行（``community``、``password``），
   结果表不能变成口令的第二份明文副本。评估用的是归档**原文**（否则规则失效），
   但入库前对摘录调一次驱动脱敏。
3. **"最新一次结果"用窗口函数算**，不给表加冗余标记：历史要留着（趋势、回归），
   而"最新"是查询语义，不是存储语义。
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..compliance import Evaluation, RuleSet, evaluate, load_ruleset
from ..core.config import Settings
from ..db.base import utcnow
from ..db.models import ComplianceResult, Device
from .blobs import BlobStore
from .snapshots import latest_snapshot, load_config, redact_config

__all__ = [
    "evaluate_device",
    "evaluate_text",
    "latest_results",
    "report",
    "reset_rules_cache",
    "ruleset",
    "run_for_devices",
    "summary_for_devices",
    "verdict_label",
]

VERDICT_LABELS = {"pass": "合规", "warn": "有建议项", "fail": "不合规"}


def verdict_label(verdict: str) -> str:
    return VERDICT_LABELS.get(verdict, verdict)


@lru_cache(maxsize=8)
def _cached_ruleset(paths: tuple[str, ...]) -> RuleSet:
    return load_ruleset(list(paths) if paths else None)


def ruleset(settings: Settings | None = None) -> RuleSet:
    """取当前规则集（按配置的路径缓存）。

    缓存键是路径元组：测试里换一组规则文件就能拿到新规则集，不必重启进程；
    真要重新读同一批文件（改了内容）时调 ``reset_rules_cache()``。
    """
    configured = tuple(str(item) for item in (getattr(settings, "compliance_rules", None) or []))
    return _cached_ruleset(configured)


def reset_rules_cache() -> None:
    _cached_ruleset.cache_clear()


def _now() -> datetime:
    return utcnow()


def build_result(
    device: Device,
    evaluation: Evaluation,
    *,
    snapshot_id: int | None = None,
) -> ComplianceResult:
    """把评估结果转成可入库的行（调用方负责 commit）。"""
    counts = evaluation.counts
    return ComplianceResult(
        device_id=device.id,
        snapshot_id=snapshot_id,
        checked_at=_now(),
        vendor=evaluation.vendor or device.vendor,
        rules_version=evaluation.rules_version,
        verdict=evaluation.verdict,
        checked_count=len(evaluation.checked),
        high_count=counts.get("high", 0),
        medium_count=counts.get("medium", 0),
        low_count=counts.get("low", 0),
        violations=[item.as_dict() for item in evaluation.violations],
        checked_rules=list(evaluation.checked),
    )


def evaluate_text(
    device: Device,
    text: str,
    *,
    rules: RuleSet | None = None,
    snapshot_id: int | None = None,
) -> ComplianceResult:
    """评估一段配置正文并返回待入库的行。"""
    active = rules or ruleset()
    evaluation = evaluate(
        text,
        active,
        vendor=device.vendor,
        # 违规行摘录入库前脱敏：含密行本身就该被掩掉
        redact=lambda line: redact_config(line, device.vendor),
    )
    return build_result(device, evaluation, snapshot_id=snapshot_id)


async def evaluate_device(
    session: AsyncSession,
    device: Device,
    *,
    blobs: BlobStore | None = None,
    text: str | None = None,
    rules: RuleSet | None = None,
) -> ComplianceResult | None:
    """评估一台设备。

    * 给了 ``text`` 就直接评估它（采集刚结束时走这条，省一次读盘）；
    * 否则取**最新版本**的正文来评估（"立即检查"走这条）。

    :return: 待入库的行；设备还没有任何版本时返回 ``None``。
    """
    snapshot_id: int | None = None
    if text is None:
        snapshot = await latest_snapshot(session, device.id)
        if snapshot is None:
            return None
        if blobs is None:
            raise ValueError("按最新版本评估时必须提供 BlobStore")
        text = load_config(blobs, snapshot)
        snapshot_id = snapshot.id
    return evaluate_text(device, text, rules=rules, snapshot_id=snapshot_id)


async def run_for_devices(
    session: AsyncSession,
    devices: Sequence[Device],
    *,
    blobs: BlobStore,
    rules: RuleSet | None = None,
) -> tuple[list[ComplianceResult], int]:
    """对一批设备各评估一次（"立即检查"用）。会话内的行由调用方 commit。

    :return: ``(结果列表, 失败台数)``。单台设备评估失败不中断整批，但**必须计数**——
    否则接口会把"10 台里 3 台没评上"报成"全部检查完成"（和 P1 里"计数必须与落库一致"
    是同一条要求）。
    """
    results: list[ComplianceResult] = []
    failed = 0
    for device in devices:
        try:
            result = await evaluate_device(session, device, blobs=blobs, rules=rules)
        except Exception:  # noqa: BLE001 - 单台失败不影响整批，但要计数
            failed += 1
            continue
        if result is None:
            continue
        session.add(result)
        results.append(result)
    return results, failed


def _latest_ranked_subquery():
    """每台设备按 (checked_at, id) 取最新一行的排名子查询（SQLite/PG 都支持窗口函数）。"""
    return (
        select(
            ComplianceResult.id.label("result_id"),
            func.row_number()
            .over(
                partition_by=ComplianceResult.device_id,
                order_by=(ComplianceResult.checked_at.desc(), ComplianceResult.id.desc()),
            )
            .label("rank"),
        )
        .subquery()
    )


async def latest_results(
    session: AsyncSession,
    *,
    device_ids: Sequence[int] | None = None,
) -> dict[int, ComplianceResult]:
    """取每台设备**最新一次**评估结果（没有评估过的设备不在返回里）。"""
    ranked = _latest_ranked_subquery()
    query = (
        select(ComplianceResult)
        .join(ranked, ComplianceResult.id == ranked.c.result_id)
        .where(ranked.c.rank == 1)
    )
    if device_ids is not None:
        ids = [int(item) for item in device_ids]
        if not ids:
            return {}
        query = query.where(ComplianceResult.device_id.in_(ids))
    result = await session.execute(query)
    return {row.device_id: row for row in result.scalars().all()}


async def summary_for_devices(
    session: AsyncSession, device_ids: Sequence[int]
) -> dict[str, Any]:
    """一批设备的最新合规概况（通知与看板用）。"""
    latest = await latest_results(session, device_ids=device_ids)
    failing = [row for row in latest.values() if row.verdict == "fail"]
    return {
        "checked": len(latest),
        "fail": len(failing),
        "warn": sum(1 for row in latest.values() if row.verdict == "warn"),
        "pass": sum(1 for row in latest.values() if row.verdict == "pass"),
        "high_total": sum(row.high_count for row in latest.values()),
        "failing_devices": sorted(row.device_id for row in failing),
    }


async def report(
    session: AsyncSession,
    *,
    group_id: int | None = None,
    vendor: str | None = None,
    site: str | None = None,
    limit_devices: int = 5000,
) -> dict[str, Any]:
    """"设备 × 规则"矩阵 + 合规率统计（报表接口就返回它）。

    没用 ``devices.resolve_devices``：报表要能列出"还没采集过 / 还没评估过"的设备，
    而那个函数的语义是"可采集的目标"。这里自己组条件，把 **所有** 匹配台账的设备
    都带上，缺失评估的格子标成 ``unknown``——运维要看的恰恰是"谁还没查过"。
    """
    conditions = []
    if group_id is not None:
        conditions.append(Device.group_id == group_id)
    if vendor:
        conditions.append(Device.vendor == vendor)
    if site:
        conditions.append(Device.site == site)

    devices = list(
        (
            await session.execute(
                select(Device).where(*conditions).order_by(Device.name).limit(limit_devices)
            )
        )
        .scalars()
        .all()
    )

    active = ruleset()
    latest = await latest_results(session, device_ids=[device.id for device in devices])

    rule_columns = [
        {"id": rule.id, "desc": rule.desc, "severity": rule.severity}
        for rule in active.rules
        if rule.enabled
    ]

    matrix: list[dict[str, Any]] = []
    verdict_counts = {"pass": 0, "warn": 0, "fail": 0, "unknown": 0}
    rule_fail_counts = {rule.id: 0 for rule in active.rules if rule.enabled}

    for device in devices:
        result = latest.get(device.id)
        if result is None:
            verdict_counts["unknown"] += 1
            matrix.append(
                {
                    "device_id": device.id,
                    "device_name": device.name,
                    "vendor": device.vendor,
                    "site": device.site,
                    "group_id": device.group_id,
                    "verdict": "unknown",
                    "checked_at": None,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                    "violations": [],
                    # 未评估也要给 cells（空）：前端按行渲染格子，缺键会直接报错
                    "cells": {},
                }
            )
            continue

        verdict_counts[result.verdict] = verdict_counts.get(result.verdict, 0) + 1
        violated = {item.get("rule_id") for item in (result.violations or [])}
        checked = set(result.checked_rules or [])
        for rule_id in violated:
            if rule_id in rule_fail_counts:
                rule_fail_counts[rule_id] += 1
        matrix.append(
            {
                "device_id": device.id,
                "device_name": device.name,
                "vendor": device.vendor,
                "site": device.site,
                "group_id": device.group_id,
                "verdict": result.verdict,
                "checked_at": result.checked_at,
                "rules_version": result.rules_version,
                "high": result.high_count,
                "medium": result.medium_count,
                "low": result.low_count,
                "checked_rules": sorted(checked),
                "violations": result.violations or [],
                # 供前端直接渲染每个格子：pass / fail / na（不适用）
                "cells": {
                    rule["id"]: (
                        "na"
                        if rule["id"] not in checked
                        else ("fail" if rule["id"] in violated else "pass")
                    )
                    for rule in rule_columns
                },
            }
        )

    total_devices = len(devices)
    scored = verdict_counts["pass"] + verdict_counts["warn"] + verdict_counts["fail"]
    compliance_rate = (verdict_counts["pass"] / scored) if scored else None

    by_vendor: dict[str, dict[str, int]] = {}
    by_site: dict[str, dict[str, int]] = {}
    for row in matrix:
        for bucket, key in ((by_vendor, row["vendor"]), (by_site, row["site"] or "(未分组)")):
            entry = bucket.setdefault(key, {"pass": 0, "warn": 0, "fail": 0, "unknown": 0})
            entry[row["verdict"]] = entry.get(row["verdict"], 0) + 1

    return {
        "rules_version": active.version,
        "rules_source": active.source,
        "rule_columns": rule_columns,
        "matrix": matrix,
        "summary": {
            "devices": total_devices,
            "scored": scored,
            "pass": verdict_counts["pass"],
            "warn": verdict_counts["warn"],
            "fail": verdict_counts["fail"],
            "unknown": verdict_counts["unknown"],
            # 合规率 = 全过 / 已评估（未评估的不算进分母，否则"没采集"会被误读成"不合规"）
            "compliance_rate": compliance_rate,
            "rule_fail_counts": {
                key: value for key, value in rule_fail_counts.items() if value
            },
        },
        "by_vendor": by_vendor,
        "by_site": by_site,
    }


def report_rows(report_data: dict[str, Any]) -> list[list[str]]:
    """把报表拍平成 CSV 行（导出用，纯函数便于测试）。

    列 = 设备信息 + 每个规则的 pass/fail/na + 汇总。空值统一成空字符串，
    免得 Excel 里出现一堆 ``None``。
    """
    columns = report_data.get("rule_columns") or []
    header = ["设备", "厂商", "站点", "判定", "high", "medium", "low", "检查时间"]
    header += [rule["id"] for rule in columns]

    rows: list[list[str]] = []
    for row in report_data.get("matrix") or []:
        cells = row.get("cells") or {}
        line = [
            str(row.get("device_name") or ""),
            str(row.get("vendor") or ""),
            str(row.get("site") or ""),
            verdict_label(str(row.get("verdict") or "")),
            str(row.get("high") or 0),
            str(row.get("medium") or 0),
            str(row.get("low") or 0),
            row["checked_at"].isoformat(sep=" ", timespec="seconds")
            if row.get("checked_at")
            else "",
        ]
        line += [_cell_text(cells.get(rule["id"])) for rule in columns]
        rows.append(line)
    return [header, *rows]


def _cell_text(value: str | None) -> str:
    return {"pass": "PASS", "fail": "FAIL", "na": "-"}.get(value or "", "-")
