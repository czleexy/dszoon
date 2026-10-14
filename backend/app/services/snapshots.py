"""快照查询与配置正文渲染（含脱敏）。

脱敏的位置很关键：**原文永远按原样归档**（审计需要），脱敏只在"给谁看"这一步做。
所以 ``raw`` 与 ``redacted`` 是同一份内容的两种渲染，而不是两份存储。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..collector.registry import get_registry
from ..db.models import ConfigSnapshot
from ..errors import NotFound
from .blobs import BlobStore

__all__ = [
    "get_snapshot",
    "list_snapshots",
    "load_config",
    "redact_config",
    "render_snapshot_content",
]


async def get_snapshot(session: AsyncSession, snapshot_id: int) -> ConfigSnapshot:
    snapshot = await session.get(ConfigSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFound(f"配置版本不存在：id={snapshot_id}")
    return snapshot


async def list_snapshots(
    session: AsyncSession,
    device_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ConfigSnapshot], int]:
    total = await session.scalar(
        select(func.count()).select_from(ConfigSnapshot).where(ConfigSnapshot.device_id == device_id)
    )
    result = await session.execute(
        select(ConfigSnapshot)
        .where(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.captured_at.desc(), ConfigSnapshot.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def latest_snapshot(session: AsyncSession, device_id: int) -> ConfigSnapshot | None:
    return await session.scalar(
        select(ConfigSnapshot)
        .where(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.captured_at.desc(), ConfigSnapshot.id.desc())
        .limit(1)
    )


def load_config(blobs: BlobStore, snapshot: ConfigSnapshot) -> str:
    return blobs.get_text(snapshot.object_key)


def redact_config(text: str, vendor: str) -> str:
    """按厂商驱动脱敏。驱动不存在时**原样返回**（不猜规则，避免漏掩）。"""
    driver = get_registry().try_get(vendor)
    if driver is None:
        return text
    return driver.redact(text)


def render_snapshot_content(
    blobs: BlobStore,
    snapshot: ConfigSnapshot,
    *,
    vendor: str,
    redacted: bool = True,
) -> str:
    text = load_config(blobs, snapshot)
    return redact_config(text, vendor) if redacted else text
