"""P2 运营类 schema：设备分组 / 定时策略 / 通知渠道 / 保留策略。

这一层只做"形状校验"（必填、类型、枚举、数值范围），**业务语义校验留给服务层**
（例如"范围至少给一种""cron 表达式是否合法""渠道配置字段是否齐全"）。
这样 CLI、定时器、测试可以直接复用服务层，而不是把规则藏在 HTTP schema 里。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..services.notifications import EVENTS
from .common import ORMModel, UtcDatetime

__all__ = [
    "ChannelCreate",
    "ChannelOut",
    "ChannelTestResult",
    "ChannelUpdate",
    "GroupCreate",
    "GroupDevicesRequest",
    "GroupDevicesResult",
    "GroupOut",
    "GroupUpdate",
    "NotificationLogOut",
    "RetentionOut",
    "RetentionRunResult",
    "RetentionUpdate",
    "ScheduleCreate",
    "ScheduleOut",
    "SchedulePreview",
    "ScheduleUpdate",
]


def _check_events(events: list[str]) -> list[str]:
    unknown = sorted(set(events) - set(EVENTS))
    if unknown:
        raise ValueError(f"未知的通知事件：{unknown}（可用值见 GET /notification-events）")
    return list(dict.fromkeys(events))  # 去重保序


# --------------------------------------------------------------------------
# 设备分组
# --------------------------------------------------------------------------
class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=64)
    description: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=16, description="界面上的标签颜色")


class GroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=16)


class GroupOut(ORMModel):
    id: int
    name: str
    description: str | None = None
    color: str | None = None
    device_count: int = 0
    created_at: UtcDatetime | None = None


class GroupDevicesRequest(BaseModel):
    device_ids: list[int] = Field(default_factory=list)
    replace: bool = Field(
        default=True,
        description="True=这些设备成为该组全部成员（其余移出）；False=只追加",
    )


class GroupDevicesResult(BaseModel):
    affected: int
    device_count: int


# --------------------------------------------------------------------------
# 定时策略
# --------------------------------------------------------------------------
class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=128)
    kind: Literal["cron", "interval"] = "cron"
    cron: str | None = Field(default=None, description="5 段表达式，如 ``0 2 * * *``")
    interval_minutes: int | None = Field(default=None, ge=1)
    jitter_minutes: int = Field(default=0, ge=0, le=120, description="错峰抖动上限（分钟）")
    window_start: str | None = Field(default=None, description="执行窗口开始 HH:MM")
    window_end: str | None = Field(default=None, description="执行窗口结束 HH:MM（可跨零点）")
    enabled: bool = True

    # --- 备份范围（与 BackupRequest 同构，服务层转交它） ---
    device_ids: list[int] | None = None
    group_id: int | None = None
    site: str | None = None
    vendor: str | None = None
    include_disabled: bool = False
    all_enabled: bool = False

    # --- 采集参数 ---
    precheck: bool | None = None
    retries: int | None = Field(default=None, ge=0, le=5)


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128)
    kind: Literal["cron", "interval"] | None = None
    cron: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1)
    jitter_minutes: int | None = Field(default=None, ge=0, le=120)
    window_start: str | None = None
    window_end: str | None = None
    enabled: bool | None = None
    device_ids: list[int] | None = None
    group_id: int | None = None
    site: str | None = None
    vendor: str | None = None
    include_disabled: bool | None = None
    all_enabled: bool | None = None
    precheck: bool | None = None
    retries: int | None = Field(default=None, ge=0, le=5)


class ScheduleOut(ORMModel):
    id: int
    name: str
    enabled: bool
    kind: str
    cron: str | None = None
    interval_minutes: int | None = None
    jitter_minutes: int = 0
    window_start: str | None = None
    window_end: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    next_run_at: UtcDatetime | None = None
    last_run_at: UtcDatetime | None = None
    last_status: str | None = None
    last_task_id: int | None = None
    created_by: str | None = None
    created_at: UtcDatetime | None = None


class SchedulePreview(BaseModel):
    """未来若干次执行时刻。

    注意：带错峰抖动的策略**每次计算都会重新抖动**（这是"错峰"生效的前提，
    否则所有设备永远偏移同一个固定值），所以预览只是"可能的时间范围"。
    """

    kind: str
    times: list[UtcDatetime] = Field(default_factory=list)
    description: str = ""


# --------------------------------------------------------------------------
# 通知渠道
# --------------------------------------------------------------------------
class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=128)
    # ★ 邮件渠道的服务层类型名是 "smtp"（见 services/notifications.KINDS），
    #   这里写 "email" 会让所有邮件渠道在建的时候 422。
    kind: Literal["smtp", "webhook", "dingtalk", "wecom", "feishu"] = "webhook"
    config: dict[str, Any] = Field(default_factory=dict)
    secret: dict[str, Any] | None = Field(
        default=None, description="敏感字段（SMTP 口令、钉钉加签密钥等），只写不读"
    )
    events: list[str] = Field(default_factory=list)
    min_failures: int = Field(default=1, ge=1, le=1000)
    enabled: bool = True

    @field_validator("events")
    @classmethod
    def _events(cls, value: list[str]) -> list[str]:
        return _check_events(value)


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128)
    kind: Literal["smtp", "webhook", "dingtalk", "wecom", "feishu"] | None = None
    config: dict[str, Any] | None = None
    secret: dict[str, Any] | None = None
    events: list[str] | None = None
    min_failures: int | None = Field(default=None, ge=1, le=1000)
    enabled: bool | None = None

    @field_validator("events")
    @classmethod
    def _events(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_events(value)


class ChannelOut(ORMModel):
    id: int
    name: str
    kind: str
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    min_failures: int = 1
    has_secret: bool = False
    last_sent_at: UtcDatetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: UtcDatetime | None = None


class ChannelTestResult(BaseModel):
    ok: bool
    detail: str


class NotificationLogOut(ORMModel):
    id: int
    channel_id: int | None = None
    channel_name: str | None = None
    event: str | None = None
    subject: str | None = None
    status: str
    error: str | None = None
    task_id: int | None = None
    created_at: UtcDatetime | None = None


# --------------------------------------------------------------------------
# 保留策略
# --------------------------------------------------------------------------
class RetentionOut(BaseModel):
    enabled: bool
    keep_changed_days: int | None = None
    keep_unchanged_daily_days: int
    keep_min_versions: int
    keep_session_log_days: int
    description: str = ""
    #: 上次清理的时间与结果——界面上要能回答"上次到底删了多少"
    last_run_at: UtcDatetime | None = None
    last_result: dict[str, Any] = Field(default_factory=dict)
    updated_at: UtcDatetime | None = None


class RetentionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    keep_changed_days: int | None = Field(
        default=None, description="留空/不传 = 变更版本永久保留"
    )
    keep_unchanged_daily_days: int | None = Field(default=None, ge=0, le=3650)
    keep_min_versions: int | None = Field(default=None, ge=1, le=1000)
    keep_session_log_days: int | None = Field(default=None, ge=0, le=3650)


class RetentionRunResult(BaseModel):
    summary: dict[str, Any]
