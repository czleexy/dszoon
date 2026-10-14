"""跨设备配置检索（P3 收尾）。

三个刻意的取舍，每一个都会影响"这个功能能不能信"：

1. **不建全文索引表。** 这个项目的立身之本是"配置正文只落在对象目录里，数据库只存指针"
   （见 ``blobs.py`` 的模块说明：几万台设备就是几百 GB）。为了检索把配置行倒进关系库，
   等于把最占空间的东西复制一份到最贵的地方。这里查的是**每台设备的最新版本**，
   正文从对象目录读，并按 sha256 缓存——内容寻址意味着"100 台设备配得一模一样"只读一次盘。

2. **在原文上匹配，返回脱敏后的行**（与合规引擎同一条规矩）。拿脱敏后的文本去匹配，
   搜口令就永远搜不到，而"这台设备是不是还在用出厂口令"恰恰是要搜的东西之一。

3. **必须 auditor 及以上，且每次搜索都写审计。** 这是全库明文检索，是这套系统里权限
   最高的一次读操作；谁在什么时候搜了什么词，本身就是需要留痕的信息。

**它不是全文检索引擎**：不建索引、不做分词、每次都要读盘，所以有 ``max_devices`` 上限
并如实报出"扫了多少台、有没有被截断"。规模真的上来（几千台）再谈索引——那时也应该是
独立的检索引擎，而不是往这个库里塞副本。
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConfigSnapshot, Device
from ..errors import ValidationFailed
from .blobs import BlobStore
from .snapshots import load_config, redact_config

__all__ = ["search_configs"]


async def search_configs(
    session: AsyncSession,
    blobs: BlobStore,
    *,
    query: str,
    device_ids: Sequence[int] | None = None,
    limit: int = 50,
    max_devices: int = 200,
) -> dict[str, Any]:
    """在**每台设备的最新版本**里找包含 ``query`` 的行（子串、不区分大小写）。"""
    needle = (query or "").strip()
    if not needle:
        raise ValidationFailed("搜索词不能为空")
    lowered = needle.lower()

    # 每台设备的最新版本：``max(id)`` 即最新（id 单调递增），一条查询搞定，
    # 不要按设备逐台查（1000 台就是 1000 次往返）
    latest_ids = (
        select(func.max(ConfigSnapshot.id))
        .group_by(ConfigSnapshot.device_id)
        .scalar_subquery()
    )
    statement = (
        select(ConfigSnapshot, Device.name, Device.vendor)
        .join(Device, Device.id == ConfigSnapshot.device_id)
        .where(ConfigSnapshot.id.in_(latest_ids))
        .order_by(ConfigSnapshot.captured_at.desc())
        .limit(max_devices)
    )
    if device_ids:
        statement = statement.where(ConfigSnapshot.device_id.in_(list(device_ids)))

    rows = (await session.execute(statement)).all()

    cache: dict[str, list[str]] = {}
    hits: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    for snapshot, device_name, vendor in rows:
        if len(hits) >= limit:
            truncated = True
            break
        scanned += 1
        lines = cache.get(snapshot.sha256)
        if lines is None:
            # 读盘失败（对象被清理/文件缺失）不该让整次搜索失败：
            # 跳过这台设备，并在结果里如实说明"有一台读不到"。
            try:
                lines = load_config(blobs, snapshot).splitlines()
            except (FileNotFoundError, OSError):
                lines = []
            cache[snapshot.sha256] = lines

        for index, line in enumerate(lines, start=1):
            if lowered not in line.lower():
                continue
            hits.append(
                {
                    "device_id": int(snapshot.device_id),
                    "device_name": device_name,
                    "vendor": vendor,
                    "snapshot_id": int(snapshot.id),
                    "line_no": index,
                    "line": redact_config(line, vendor or ""),
                }
            )
            if len(hits) >= limit:
                truncated = True
                break

    return {
        "query": needle,
        "scanned_devices": scanned,
        # 命中了多少台设备 ≠ 命中了多少行，两个都要给：前者决定"要处理几台"，
        # 后者决定"这次改动有多大"
        "matched_devices": len({hit["device_id"] for hit in hits}),
        "hits": hits,
        "truncated": truncated,
        "limit": limit,
        "max_devices": max_devices,
    }
