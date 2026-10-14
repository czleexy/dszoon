"""配置版本与差异对比 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import ORMModel, UtcDatetime

__all__ = ["DiffOut", "SnapshotContent", "SnapshotOut"]


class SnapshotOut(ORMModel):
    id: int
    device_id: int
    sha256: str
    normalized_sha256: str | None = None
    size_bytes: int = 0
    line_count: int = 0
    captured_at: UtcDatetime
    model: str | None = None
    os_version: str | None = None
    facts: dict[str, str] = Field(default_factory=dict)


class SnapshotContent(BaseModel):
    snapshot_id: int
    device_id: int
    sha256: str
    #: raw = 原样（需要"查看明文"权限）；redacted = 已脱敏（默认）
    format: str = "redacted"
    content: str
    size_bytes: int = 0
    line_count: int = 0
    redacted: bool = True


class DiffOut(BaseModel):
    device_id: int
    from_snapshot_id: int
    to_snapshot_id: int
    from_sha256: str
    to_sha256: str
    identical: bool = False
    added: int = 0
    removed: int = 0
    changed: int = 0
    unified: str = ""
    significant: list[str] = Field(default_factory=list)
    #: 是否已按驱动规则归一化（去掉时间戳等噪声）后再比对
    normalized: bool = True
    from_captured_at: UtcDatetime | None = None
    to_captured_at: UtcDatetime | None = None
