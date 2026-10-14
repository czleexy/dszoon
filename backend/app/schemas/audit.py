"""审计日志的对外契约（P3）。

审计记录从 P1 起就在写，P3 才把它**查得出来、导得出去**——等保验收问的就是
"谁在何时下载了哪台设备的配置"，答不出这一条，前面所有审计写入都等于白写。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import ORMModel, UtcDatetime

__all__ = ["AuditActionOut", "AuditLogOut", "DownloadRecordOut"]


class AuditLogOut(ORMModel):
    id: int
    created_at: UtcDatetime
    user_id: int | None = None
    username: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    client_ip: str | None = None


class AuditActionOut(BaseModel):
    """动作字典（前端筛选下拉用，避免前后端各写一份枚举）。"""

    value: str
    label: str


class DownloadRecordOut(BaseModel):
    """配置下载/明文查看的专用视图（等保追溯的那一问）。"""

    id: int
    created_at: UtcDatetime
    username: str | None = None
    action: str
    action_label: str
    device: str | None = None
    snapshot_id: str | None = None
    client_ip: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
