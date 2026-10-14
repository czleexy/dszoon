"""保留策略：版本折叠、blob 回收、会话日志清理。

**为什么必须有这个模块**：一次全量巡检会给 1000 台设备各写一条 run，但只有配置
真变了的那些才产生新版本。如果什么都不清，两年后 `objects/` 会变成几百 GB，
而其中 99% 是"和上一版一模一样"的重复内容。反过来，如果清得太狠，就会出现
"想回滚却发现历史版本没了"——这比占磁盘严重得多。

所以策略是**保守**的：

* 每台设备的**最早一版**（首次采集 = 原始基线）与**最新一版**永久保留；
* **变更过的版本**默认永久保留（配置变更是审计证据，时间点本身就是信息）；
* **未变更的版本**按"每天最多留一个"保留 N 天（默认 90 天），这直接命中
  "每天巡检但配置没动"这个最常见的量级问题；
* 任何情况下每台设备至少保留 ``keep_min_versions`` 个版本。

``_select_prunable`` 是纯函数，因为"哪些该删"是这里最容易出错、也最值钱的部分，
必须能脱离数据库单测。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import utcnow
from ..db.models import ConfigSnapshot, RetentionPolicy
from ..errors import ValidationFailed
from .blobs import BlobStore

__all__ = [
    "BLOB_GRACE_SECONDS",
    "DEFAULT_POLICY",
    "enforce",
    "ensure_policy",
    "get_policy",
    "policy_summary",
    "update_policy",
]

#: 单例策略的默认值（首次访问时落库）
DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    # None = 变更版本永久保留
    "keep_changed_days": None,
    "keep_unchanged_daily_days": 90,
    "keep_min_versions": 2,
    "keep_session_log_days": 7,
}

_POLICY_FIELDS = ("enabled", "keep_changed_days", "keep_unchanged_daily_days",
                  "keep_min_versions", "keep_session_log_days")


# --------------------------------------------------------------------------
# 策略读写
# --------------------------------------------------------------------------
async def get_policy(session: AsyncSession) -> RetentionPolicy | None:
    return await session.get(RetentionPolicy, 1)


async def ensure_policy(session: AsyncSession) -> RetentionPolicy:
    """取单例策略，没有就按默认值建一条（id 固定为 1）。"""
    policy = await session.get(RetentionPolicy, 1)
    if policy is None:
        policy = RetentionPolicy(id=1, **DEFAULT_POLICY)
        session.add(policy)
        await session.flush()
    return policy


async def update_policy(session: AsyncSession, payload: dict[str, Any]) -> RetentionPolicy:
    policy = await ensure_policy(session)
    for field in _POLICY_FIELDS:
        if field in payload and payload[field] is not None:
            value = payload[field]
            if field in ("keep_unchanged_daily_days", "keep_min_versions", "keep_session_log_days"):
                value = int(value)
                if value < 0:
                    raise ValidationFailed(f"{field} 不能为负")
            if field == "keep_changed_days" and value is not None:
                value = int(value)
                if value < 1:
                    raise ValidationFailed("变更版本保留天数要么留空（永久），要么 ≥1")
            setattr(policy, field, value)
    if policy.keep_min_versions < 1:
        raise ValidationFailed("每台设备至少保留 1 个版本")
    await session.flush()
    return policy


def policy_summary(policy: RetentionPolicy) -> dict[str, Any]:
    changed = (
        "永久保留"
        if policy.keep_changed_days is None
        else f"保留 {policy.keep_changed_days} 天"
    )
    return {
        "enabled": policy.enabled,
        "keep_changed_days": policy.keep_changed_days,
        "keep_unchanged_daily_days": policy.keep_unchanged_daily_days,
        "keep_min_versions": policy.keep_min_versions,
        "keep_session_log_days": policy.keep_session_log_days,
        "description": (
            f"变更版本{changed}；未变更版本按每天 1 个保留 "
            f"{policy.keep_unchanged_daily_days} 天；每台设备至少保留 "
            f"{policy.keep_min_versions} 个版本；会话日志保留 "
            f"{policy.keep_session_log_days} 天"
        ),
    }


# --------------------------------------------------------------------------
# 该删哪些（纯函数，重点单测对象）
# --------------------------------------------------------------------------
def _select_prunable(
    snapshots: Sequence[ConfigSnapshot],
    policy: RetentionPolicy,
    now: datetime,
) -> list[ConfigSnapshot]:
    """返回应当删除的版本。

    :param snapshots: 必须按时间**升序**（旧 → 新）。
    """
    total = len(snapshots)
    if total == 0:
        return []

    keep: set[int] = set()
    # 1) 最新 N 个 + 最早 1 个
    for index in range(max(0, total - max(1, policy.keep_min_versions)), total):
        keep.add(index)
    keep.add(0)

    # 2) 逐版判断"相比上一版内容变没变"（变更 = 归一化后的 sha 不同）
    changed_flags: list[bool] = []
    previous_key: str | None = None
    for snapshot in snapshots:
        key = snapshot.normalized_sha256 or snapshot.sha256
        changed_flags.append(previous_key is None or key != previous_key)
        previous_key = key

    # 3) 变更版本按 keep_changed_days；未变更版本按"每天留一个"，超出天数才删
    seen_days: set[Any] = set()
    for index, snapshot in enumerate(snapshots):
        age_days = max(0, (now - snapshot.captured_at).days)
        if changed_flags[index]:
            if policy.keep_changed_days is None or age_days <= policy.keep_changed_days:
                keep.add(index)
            continue
        if age_days <= policy.keep_unchanged_daily_days:
            day = snapshot.captured_at.date()
            if day not in seen_days:
                # 每天只留一个：升序遍历 → 留当天最早那个，结果确定可复现
                seen_days.add(day)
                keep.add(index)

    return [snapshot for index, snapshot in enumerate(snapshots) if index not in keep]


# --------------------------------------------------------------------------
# 执行清理
# --------------------------------------------------------------------------
#: 回收正文文件时的宽限期（秒）。
#:
#: 采集的落盘顺序是"先写正文文件 → 再插快照行（同一事务）"，所以在大批量任务
#: 进行中触发清理，可能把"文件已写、行还没提交"的那一份当成无主垃圾删掉，
#: 结果是某个即将写库的版本正文丢失。1 小时远大于那毫秒级的窗口。
BLOB_GRACE_SECONDS = 3600


async def enforce(
    session: AsyncSession,
    blobs: BlobStore,
    *,
    now: datetime | None = None,
    policy: RetentionPolicy | None = None,
    prune_blobs: bool = True,
) -> dict[str, Any]:
    """执行一次保留策略，返回可写进审计/界面展示的摘要。"""
    policy = policy or await ensure_policy(session)
    moment = now or utcnow()
    summary: dict[str, Any] = {
        "scanned_devices": 0,
        "scanned_snapshots": 0,
        "deleted_snapshots": 0,
        "deleted_blobs": 0,
        "freed_bytes": 0,
        "deleted_session_logs": 0,
        "skipped": not policy.enabled,
    }
    if not policy.enabled:
        return summary

    device_ids = list(
        (await session.execute(select(ConfigSnapshot.device_id).distinct())).scalars().all()
    )
    for device_id in device_ids:
        result = await session.execute(
            select(ConfigSnapshot)
            .where(ConfigSnapshot.device_id == device_id)
            .order_by(ConfigSnapshot.captured_at.asc(), ConfigSnapshot.id.asc())
        )
        snapshots = list(result.scalars().all())
        summary["scanned_devices"] += 1
        summary["scanned_snapshots"] += len(snapshots)
        for snapshot in _select_prunable(snapshots, policy, moment):
            await session.delete(snapshot)
            summary["deleted_snapshots"] += 1

    await session.flush()

    if prune_blobs and summary["deleted_snapshots"]:
        # blob 是跨设备共享的（内容寻址），所以必须**全局**回收：
        # 先删行，再按"仍然被引用的 key 集合"清理磁盘。
        referenced = set(
            (await session.execute(select(ConfigSnapshot.object_key))).scalars().all()
        )
        removed, freed = blobs.prune_unreferenced(
            referenced, min_age_seconds=BLOB_GRACE_SECONDS
        )
        summary["deleted_blobs"] = removed
        summary["freed_bytes"] = freed

    summary["deleted_session_logs"] = blobs.prune_session_logs(policy.keep_session_log_days)
    return summary
