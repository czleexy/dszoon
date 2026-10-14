"""Prometheus 指标（P3）。

设计上只做两件事：**从数据库算**、**按 exposition 格式渲染**。刻意不引入
``prometheus_client``：

* 我们不需要它的直方图/注册表/多进程模式——指标是"当前状态快照"，每次抓取重算一遍
  最不容易出错（没有"进程内累加器重启后归零"这类问题）；
* 依赖越少，私有化交付时的离线包越小。

两处刻意的取舍：

1. **不暴露直方图，只给 sum/count/max**。分位数需要分桶存储，而我们的表里只有原始记录；
   硬造一个"假直方图"会让面板显示错误的 P95。需要分位数请用 PromQL 的
   ``quantile_over_time`` 或在界面上看 P50/P95（P2 的压测脚本就是那么算的）。
2. **标签基数受控**：状态、判定、动作这些字段取值有限；设备名/IP 绝不进标签——
   1000 台设备会直接把 Prometheus 的时间序列打爆。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import utcnow
from ..db.models import (
    BackupRun,
    BackupTask,
    ComplianceResult,
    ConfigSnapshot,
    Device,
    NotificationLog,
    Schedule,
)

__all__ = ["collect_metrics", "render_prometheus", "Sample"]

#: 指标名 → (类型, 说明)。放在一处便于审阅：加指标等于加长期契约。
METRIC_HELP: dict[str, tuple[str, str]] = {
    "dszoon_devices_total": ("gauge", "设备台账数量（按是否启用分组）"),
    "dszoon_snapshots_total": ("gauge", "配置版本总数"),
    "dszoon_backup_tasks_total": ("gauge", "备份任务数量（按状态）"),
    "dszoon_backup_runs_total": ("counter", "采集次数（按结果状态；unreachable 即设备不可达）"),
    "dszoon_backup_run_duration_seconds_sum": ("counter", "采集耗时总和（秒）"),
    "dszoon_backup_run_duration_seconds_count": ("counter", "参与耗时代数统计的采集次数"),
    "dszoon_backup_run_duration_seconds_max": ("gauge", "单次采集最长耗时（秒）"),
    "dszoon_schedules_enabled_total": ("gauge", "启用中的定时策略数"),
    "dszoon_schedules_due_total": ("gauge", "已到期但还没被调度的策略数（持续 >0 说明调度器没在跑）"),
    "dszoon_compliance_results_total": ("gauge", "合规评估结果数（按判定）"),
    "dszoon_compliance_high_findings_total": ("gauge", "最新一轮评估里的 high 级违规总数"),
    "dszoon_notifications_total": ("counter", "通知发送次数（按结果）"),
    "dszoon_up": ("gauge", "1 = 数据库可读（能算出这些指标就说明是 1）"),
}


class Sample:
    """一条样本：名字 + 标签 + 值。"""

    __slots__ = ("name", "labels", "value")

    def __init__(self, name: str, value: float | int, labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.value = value
        self.labels = labels or {}

    def line(self) -> str:
        if not self.labels:
            return f"{self.name} {_number(self.value)}"
        rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(self.labels.items()))
        return f"{self.name}{{{rendered}}} {_number(self.value)}"


def _number(value: float | int) -> str:
    """Prometheus 只接受十进制；整数不要渲染成 ``1.0``（不好看也浪费带宽）。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        return "0"
    return f"{value:.6g}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


async def _count_by(session: AsyncSession, column, model) -> dict[Any, int]:
    """按列分组计数。**返回原始键**（不做 str 转换）：布尔列在 SQLite 上是 1/0、
    在 PostgreSQL 上是 True/False，转成字符串会得到 "1"/"True" 两种写法。"""
    result = await session.execute(select(column, func.count()).group_by(column))
    return {row[0]: int(row[1]) for row in result.all()}


def _flag(value: Any) -> str:
    """布尔列 → Prometheus 的 true/false（统一 SQLite 的 1/0 与 PG 的 True/False）。"""
    return "true" if value in (True, 1, "1", "True", "true") else "false"


async def collect_metrics(session: AsyncSession) -> list[Sample]:
    """从数据库算出当前指标快照。"""
    now = utcnow()
    samples: list[Sample] = []

    devices = await _count_by(session, Device.enabled, Device)
    for enabled, count in devices.items():
        samples.append(Sample("dszoon_devices_total", count, {"enabled": _flag(enabled)}))

    samples.append(
        Sample(
            "dszoon_snapshots_total",
            int(await session.scalar(select(func.count()).select_from(ConfigSnapshot)) or 0),
        )
    )

    for status, count in (await _count_by(session, BackupTask.status, BackupTask)).items():
        samples.append(Sample("dszoon_backup_tasks_total", count, {"status": str(status)}))

    for status, count in (await _count_by(session, BackupRun.status, BackupRun)).items():
        samples.append(Sample("dszoon_backup_runs_total", count, {"status": str(status)}))

    duration = (
        await session.execute(
            select(
                func.count(BackupRun.duration_ms),
                func.coalesce(func.sum(BackupRun.duration_ms), 0),
                func.coalesce(func.max(BackupRun.duration_ms), 0),
            )
        )
    ).one()
    samples.append(Sample("dszoon_backup_run_duration_seconds_count", int(duration[0] or 0)))
    samples.append(Sample("dszoon_backup_run_duration_seconds_sum", float(duration[1] or 0) / 1000.0))
    samples.append(Sample("dszoon_backup_run_duration_seconds_max", float(duration[2] or 0) / 1000.0))

    samples.append(
        Sample(
            "dszoon_schedules_enabled_total",
            int(
                await session.scalar(
                    select(func.count()).select_from(Schedule).where(Schedule.enabled.is_(True))
                )
                or 0
            ),
        )
    )
    samples.append(
        Sample(
            "dszoon_schedules_due_total",
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(Schedule)
                    .where(
                        Schedule.enabled.is_(True),
                        Schedule.next_run_at.is_not(None),
                        Schedule.next_run_at <= now,
                    )
                )
                or 0
            ),
        )
    )

    for verdict, count in (await _count_by(session, ComplianceResult.verdict, ComplianceResult)).items():
        samples.append(Sample("dszoon_compliance_results_total", count, {"verdict": str(verdict)}))
    samples.append(
        Sample(
            "dszoon_compliance_high_findings_total",
            int(await session.scalar(select(func.coalesce(func.sum(ComplianceResult.high_count), 0))) or 0),
        )
    )

    for status, count in (await _count_by(session, NotificationLog.status, NotificationLog)).items():
        samples.append(Sample("dszoon_notifications_total", count, {"status": str(status)}))

    samples.append(Sample("dszoon_up", 1))
    return samples


def render_prometheus(samples: Iterable[Sample]) -> str:
    """渲染成 Prometheus exposition 文本格式（纯函数，便于单测）。"""
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.name, []).append(sample)

    lines: list[str] = []
    for name in sorted(grouped):
        kind, help_text = METRIC_HELP.get(name, ("gauge", ""))
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        sample: Any
        for sample in sorted(grouped[name], key=lambda item: sorted(item.labels.items())):
            lines.append(sample.line())
    return "\n".join(lines) + "\n"
