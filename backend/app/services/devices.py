"""设备台账与凭据的服务层。

两条硬约束：

1. **凭据明文只在 ``build_target()`` 里出现一次**——加密落库，读取即用，
   不进日志、不进响应。
2. **厂商名一律归一化成驱动名**（``get_registry().get(vendor).name``），
   这样台账里写 "HW" / "Huawei VRP" 都能工作，且采集时不需要再猜。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..collector import DeviceTarget
from ..collector.registry import get_registry
from ..core.config import Settings
from ..core.crypto import CredentialCipher
from ..db.models import CredentialProfile, Device
from ..errors import Conflict, NotFound, ValidationFailed
from ..schemas.device import (
    CredentialProfileCreate,
    CredentialProfileUpdate,
    DeviceCreate,
    DeviceImportResult,
    DeviceUpdate,
)

__all__ = [
    "build_targets",
    "create_device",
    "create_profile",
    "delete_device",
    "delete_profile",
    "get_device",
    "get_profile",
    "import_devices",
    "list_devices",
    "list_profiles",
    "normalize_vendor",
    "resolve_devices",
    "update_device",
    "update_profile",
]


def normalize_vendor(vendor: str) -> str:
    """把厂商名/别名归一化成驱动名，未知则报 422（并列出可用值）。"""
    registry = get_registry()
    driver = registry.try_get(vendor)
    if driver is None:
        raise ValidationFailed(
            f"未知厂商 {vendor!r}；可用：{', '.join(registry.names())}",
            detail={"vendor": vendor, "available": registry.names()},
        )
    return driver.name


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
def _device_query():
    return select(Device).options(selectinload(Device.credential_profile))


async def list_devices(
    session: AsyncSession,
    *,
    q: str | None = None,
    vendor: str | None = None,
    site: str | None = None,
    enabled: bool | None = None,
    group_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Device], int]:
    conditions = []
    if q:
        like = f"%{q}%"
        conditions.append(
            or_(Device.name.ilike(like), Device.host.ilike(like), Device.hostname.ilike(like))
        )
    if vendor:
        conditions.append(Device.vendor == normalize_vendor(vendor))
    if site:
        conditions.append(Device.site == site)
    if enabled is not None:
        conditions.append(Device.enabled.is_(enabled))
    if group_id is not None:
        # 分组管理界面要用它把"该组成员"一次取全（而不是靠前端从全量设备里过滤——
        # 设备上千台时全量取会被 limit 截断，导致界面看不到真实成员）
        conditions.append(Device.group_id == group_id)

    total = await session.scalar(
        select(func.count()).select_from(Device).where(*conditions)
    )
    result = await session.execute(
        _device_query().where(*conditions).order_by(Device.name).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), int(total or 0)


async def get_device(session: AsyncSession, device_id: int) -> Device:
    device = await session.scalar(_device_query().where(Device.id == device_id))
    if device is None:
        raise NotFound(f"设备不存在：id={device_id}")
    return device


async def _ensure_unique(session: AsyncSession, *, name: str, host: str, exclude_id: int | None = None) -> None:
    conditions = [or_(Device.name == name, Device.host == host)]
    if exclude_id is not None:
        conditions.append(Device.id != exclude_id)
    existing = await session.scalar(select(Device).where(*conditions).limit(1))
    if existing is not None:
        field = "名称" if existing.name == name else "管理地址"
        raise Conflict(f"设备{field}已被占用：{existing.name} ({existing.host})")


async def _ensure_profile(session: AsyncSession, profile_id: int | None) -> None:
    if profile_id is None:
        return
    profile = await session.get(CredentialProfile, profile_id)
    if profile is None:
        raise ValidationFailed(f"凭据不存在：id={profile_id}")


async def create_device(session: AsyncSession, payload: DeviceCreate) -> Device:
    vendor = normalize_vendor(payload.vendor)
    await _ensure_unique(session, name=payload.name, host=payload.host)
    await _ensure_profile(session, payload.credential_profile_id)
    data = payload.model_dump()
    data["vendor"] = vendor
    device = Device(**data)
    session.add(device)
    await session.flush()
    return await get_device(session, device.id)


async def update_device(session: AsyncSession, device: Device, payload: DeviceUpdate) -> Device:
    data = payload.model_dump(exclude_unset=True)
    if "vendor" in data and data["vendor"]:
        data["vendor"] = normalize_vendor(data["vendor"])
    if "name" in data or "host" in data:
        await _ensure_unique(
            session,
            name=data.get("name", device.name),
            host=data.get("host", device.host),
            exclude_id=device.id,
        )
    if "credential_profile_id" in data:
        await _ensure_profile(session, data["credential_profile_id"])
    for key, value in data.items():
        setattr(device, key, value)
    await session.flush()
    return await get_device(session, device.id)


async def delete_device(session: AsyncSession, device: Device) -> None:
    await session.delete(device)
    await session.flush()


async def resolve_devices(
    session: AsyncSession,
    *,
    device_ids: Sequence[int] | None = None,
    group_id: int | None = None,
    site: str | None = None,
    vendor: str | None = None,
    include_disabled: bool = False,
    all_enabled: bool = False,
) -> list[Device]:
    """把"备份范围"解析成设备列表。至少要有一种范围，否则报 422。

    **在任务创建时解析并冻结设备列表**：定时任务发出后，即使期间有人改了分组
    归属或删了设备，这一次任务的采集目标也不会变——比在采集过程中动态求值可预期。
    """
    if device_ids:
        result = await session.execute(_device_query().where(Device.id.in_(list(device_ids))))
        devices = list(result.scalars().all())
        missing = set(device_ids) - {d.id for d in devices}
        if missing:
            raise NotFound(f"设备不存在：{sorted(missing)}")
        return sorted(devices, key=lambda d: d.name)

    if not all_enabled and not site and not vendor and group_id is None:
        raise ValidationFailed(
            "必须指定 device_ids、group_id、site、vendor 中的至少一个，或 all_enabled=true"
        )

    conditions = []
    if not include_disabled:
        conditions.append(Device.enabled.is_(True))
    if group_id is not None:
        conditions.append(Device.group_id == group_id)
    if site:
        conditions.append(Device.site == site)
    if vendor:
        conditions.append(Device.vendor == normalize_vendor(vendor))
    result = await session.execute(_device_query().where(*conditions).order_by(Device.name))
    return list(result.scalars().all())


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
async def list_profiles(session: AsyncSession) -> list[CredentialProfile]:
    result = await session.execute(select(CredentialProfile).order_by(CredentialProfile.name))
    return list(result.scalars().all())


async def get_profile(session: AsyncSession, profile_id: int) -> CredentialProfile:
    profile = await session.get(CredentialProfile, profile_id)
    if profile is None:
        raise NotFound(f"凭据不存在：id={profile_id}")
    return profile


async def create_profile(
    session: AsyncSession, payload: CredentialProfileCreate, cipher: CredentialCipher
) -> CredentialProfile:
    existing = await session.scalar(
        select(CredentialProfile).where(CredentialProfile.name == payload.name)
    )
    if existing is not None:
        raise Conflict(f"凭据名称已存在：{payload.name}")
    profile = CredentialProfile(
        name=payload.name,
        username=payload.username,
        description=payload.description,
        password_enc=cipher.encrypt(payload.password),
        enable_password_enc=cipher.encrypt(payload.enable_password),
    )
    session.add(profile)
    await session.flush()
    return profile


async def update_profile(
    session: AsyncSession,
    profile: CredentialProfile,
    payload: CredentialProfileUpdate,
    cipher: CredentialCipher,
) -> CredentialProfile:
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] and data["name"] != profile.name:
        existing = await session.scalar(
            select(CredentialProfile).where(CredentialProfile.name == data["name"])
        )
        if existing is not None:
            raise Conflict(f"凭据名称已存在：{data['name']}")
    if "password" in data:
        # 语义：未传=不改（exclude_unset 已挡住）；null=不改；空串=清空；非空=覆盖
        value = data.pop("password")
        if value is not None:
            profile.password_enc = cipher.encrypt(value) if value else None
    if "enable_password" in data:
        value = data.pop("enable_password")
        if value is not None:
            profile.enable_password_enc = cipher.encrypt(value) if value else None
    for key, value in data.items():
        setattr(profile, key, value)
    await session.flush()
    return profile


async def delete_profile(session: AsyncSession, profile: CredentialProfile) -> None:
    in_use = await session.scalar(
        select(func.count()).select_from(Device).where(Device.credential_profile_id == profile.id)
    )
    if in_use:
        raise Conflict(f"仍有 {in_use} 台设备在使用该凭据，请先改绑或删除设备")
    await session.delete(profile)
    await session.flush()


# --------------------------------------------------------------------------
# 构造采集目标
# --------------------------------------------------------------------------
def build_target(
    device: Device,
    profile: CredentialProfile | None,
    *,
    settings: Settings,
    cipher: CredentialCipher,
) -> DeviceTarget:
    """把台账记录 + 凭据解密成采集目标。**明文只在此处短暂存在。**"""
    return DeviceTarget(
        id=device.id,
        host=device.host,
        port=device.port,
        protocol=device.protocol,
        vendor=device.vendor,
        username=profile.username if profile else "",
        password=cipher.decrypt(profile.password_enc) if profile else None,
        enable_password=cipher.decrypt(profile.enable_password_enc) if profile else None,
        hostname=device.hostname or None,
        name=device.name,
        site=device.site,
        labels=tuple(device.tags or ()),
        legacy_algorithms=device.legacy_algorithms,
        use_paramiko=device.use_paramiko,
        encoding=device.encoding,
        connect_timeout=device.connect_timeout or settings.connect_timeout,
        command_timeout=device.command_timeout or settings.command_timeout,
        idle_timeout=device.idle_timeout or settings.idle_timeout,
    )


async def build_targets(
    session: AsyncSession,
    devices: Sequence[Device],
    *,
    settings: Settings,
    cipher: CredentialCipher,
) -> tuple[list[DeviceTarget], list[tuple[Device, str]]]:
    """批量构造采集目标，返回 ``(可采集的目标, [(设备, 原因), ...])``。

    ★ **不抛异常是有意的**：批量备份时"一台设备没绑凭据"绝不能让整批任务失败
    （那等于 50 台里 1 台没配好就一台都不采）。调用方给这些设备各记一条失败记录，
    其余照常采集。单设备场景（连接测试）由调用方检查返回值并报 422。
    """
    unbuildable: list[tuple[Device, str]] = []
    targets: list[DeviceTarget] = []
    for device in devices:
        profile = device.credential_profile
        if profile is None and device.credential_profile_id:
            profile = await session.get(CredentialProfile, device.credential_profile_id)
        if profile is None:
            unbuildable.append((device, f"设备 {device.name} 未绑定凭据，无法采集"))
            continue
        if not profile.username:
            unbuildable.append(
                (device, f"设备 {device.name} 绑定的凭据 {profile.name} 缺少用户名，无法登录")
            )
            continue
        targets.append(build_target(device, profile, settings=settings, cipher=cipher))
    return targets, unbuildable


# --------------------------------------------------------------------------
# 批量导入
# --------------------------------------------------------------------------
async def import_devices(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
    *,
    cipher: CredentialCipher,
) -> DeviceImportResult:
    """按行导入设备；逐行报错，不因为一行坏掉整批失败。

    每行用一个 SAVEPOINT（``begin_nested``）包住：坏行只回滚自己，
    已处理成功的行不受影响——直接 ``rollback()`` 会把前面所有行一起丢掉，
    这是"导入 500 台，第 300 行有个错，结果一台都没进去"的经典事故。
    """
    result = DeviceImportResult()
    profile_cache: dict[str, CredentialProfile] = {
        p.name: p for p in await list_profiles(session)
    }

    for index, raw in enumerate(rows, start=2):  # 表头算第 1 行，便于用户对照 Excel
        created_profile: str | None = None
        try:
            async with session.begin_nested():
                name = _text(raw.get("name"))
                host = _text(raw.get("host"))
                vendor_raw = _text(raw.get("vendor"))
                if not name or not host or not vendor_raw:
                    raise ValidationFailed("name / host / vendor 为必填列")
                vendor = normalize_vendor(vendor_raw)

                profile_name = _text(raw.get("credential_profile"))
                profile = profile_cache.get(profile_name) if profile_name else None
                if profile_name and profile is None:
                    username = _text(raw.get("username"))
                    if not username:
                        raise ValidationFailed(
                            f"凭据 {profile_name!r} 不存在，且未提供 username 用于创建"
                        )
                    profile = CredentialProfile(
                        name=profile_name,
                        username=username,
                        password_enc=cipher.encrypt(_text(raw.get("password"))),
                        enable_password_enc=cipher.encrypt(_text(raw.get("enable_password"))),
                    )
                    session.add(profile)
                    await session.flush()
                    created_profile = profile_name

                existing = await session.scalar(select(Device).where(Device.host == host))
                tags = [t.strip() for t in (_text(raw.get("tags")) or "").split(",") if t.strip()]
                fields = {
                    "name": name,
                    "host": host,
                    "vendor": vendor,
                    "port": int(raw.get("port") or 22),
                    "site": _text(raw.get("site")),
                    "tags": tags,
                    "hostname": _text(raw.get("hostname")),
                    "credential_profile_id": profile.id if profile is not None else None,
                }
                if existing is None:
                    session.add(Device(**fields))
                    result.created += 1
                else:
                    for key, value in fields.items():
                        setattr(existing, key, value)
                    result.updated += 1
                await session.flush()

            if created_profile and profile is not None:
                profile_cache[created_profile] = profile
                result.credential_profiles_created.append(created_profile)
        except Exception as exc:  # noqa: BLE001 - 逐行报错，不中断整批
            result.skipped += 1
            result.errors.append(f"第 {index} 行：{_message(exc)}")

    return result


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _message(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc)
