"""设备分组。

分组存在的意义是"定时任务可以按组圈范围"——比逐台勾选实用得多，也比自由文本
标签可靠（能改名、能查成员、能作为外键）。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import Device, DeviceGroup
from ..errors import Conflict, NotFound, ValidationFailed

__all__ = [
    "create_group",
    "delete_group",
    "get_group",
    "list_groups",
    "set_devices",
    "update_group",
]


def _query():
    # 成员要 eager load：列表页显示数量、详情页显示成员，async 下都不能懒加载
    return select(DeviceGroup).options(selectinload(DeviceGroup.devices))


async def list_groups(session: AsyncSession) -> list[DeviceGroup]:
    result = await session.execute(_query().order_by(DeviceGroup.name))
    return list(result.scalars().all())


async def get_group(session: AsyncSession, group_id: int) -> DeviceGroup:
    group = await session.scalar(_query().where(DeviceGroup.id == group_id))
    if group is None:
        raise NotFound(f"设备分组不存在：id={group_id}")
    return group


async def _ensure_name_free(
    session: AsyncSession, name: str, *, exclude_id: int | None = None
) -> None:
    conditions = [DeviceGroup.name == name]
    if exclude_id is not None:
        conditions.append(DeviceGroup.id != exclude_id)
    existing = await session.scalar(select(DeviceGroup).where(*conditions))
    if existing is not None:
        raise Conflict(f"分组名称已存在：{name}")


async def create_group(
    session: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    color: str | None = None,
) -> DeviceGroup:
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("分组名称不能为空")
    await _ensure_name_free(session, name)
    group = DeviceGroup(name=name, description=description, color=color)
    session.add(group)
    await session.flush()
    return await get_group(session, group.id)


async def update_group(
    session: AsyncSession,
    group: DeviceGroup,
    *,
    name: str | None = None,
    description: str | None = None,
    color: str | None = None,
) -> DeviceGroup:
    if name and name != group.name:
        await _ensure_name_free(session, name, exclude_id=group.id)
        group.name = name
    if description is not None:
        group.description = description or None
    if color is not None:
        group.color = color or None
    await session.flush()
    return await get_group(session, group.id)


async def delete_group(session: AsyncSession, group: DeviceGroup) -> None:
    """删除分组。成员设备**不删**，只是解除归属（DB 层 ON DELETE SET NULL）。"""
    await session.delete(group)
    await session.flush()


async def set_devices(
    session: AsyncSession, group: DeviceGroup, device_ids: Sequence[int], *, replace: bool = True
) -> int:
    """把设备划入分组（幂等），返回受影响设备数。

    :param replace: True=这些设备**成为**该组的全部成员（其余成员移出）；
        False=只追加。
    """
    ids = list(dict.fromkeys(int(i) for i in device_ids))
    devices: list[Device] = []
    if ids:
        result = await session.execute(select(Device).where(Device.id.in_(ids)))
        devices = list(result.scalars().all())
        missing = set(ids) - {device.id for device in devices}
        if missing:
            raise NotFound(f"设备不存在：{sorted(missing)}")

    affected = 0
    if replace:
        # 先把"不在名单里的现有成员"移出（在 Python 里过滤，避免 notin_ 空列表这类边角写法）
        wanted = set(ids)
        current = await session.execute(select(Device).where(Device.group_id == group.id))
        for device in current.scalars().all():
            if device.id not in wanted:
                device.group_id = None
                affected += 1

    for device in devices:
        if device.group_id != group.id:
            device.group_id = group.id
            affected += 1
    await session.flush()
    return affected
