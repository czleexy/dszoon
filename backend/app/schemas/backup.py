"""备份任务与运行记录 schema。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import ORMModel, UtcDatetime

__all__ = ["BackupRequest", "RunOut", "TaskDetail", "TaskEvent", "TaskOut"]


class BackupRequest(BaseModel):
    """发起批量备份。至少要指定一种设备范围。"""

    model_config = ConfigDict(extra="forbid")

    device_ids: list[int] | None = Field(default=None, description="指定设备")
    group_id: int | None = Field(default=None, description="按设备分组筛选（P2）")
    site: str | None = Field(default=None, description="按站点筛选")
    vendor: str | None = Field(default=None, description="按厂商筛选")
    include_disabled: bool = Field(default=False, description="是否包含已禁用设备")
    all_enabled: bool = Field(default=False, description="备份全部启用设备")

    name: str | None = Field(default=None, max_length=128)
    precheck: bool | None = Field(default=None, description="TCP 可达性预检（默认取全局配置）")
    retries: int | None = Field(default=None, ge=0, le=5)


class TaskOut(ORMModel):
    id: int
    name: str | None = None
    trigger: str
    #: 由哪个定时策略发起（手工发起为 None）。P2 加的任务关联，界面上要能双向查。
    schedule_id: int | None = None
    status: str
    total: int
    succeeded: int
    failed: int
    unchanged: int
    created_by: str | None = None
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    error_message: str | None = None


class RunOut(ORMModel):
    """单台设备的一次采集结果。

    ★ 定义在 TaskDetail **之前**是有意的：这样 TaskDetail.runs 就是直接引用，
    不需要写前向引用字符串，也就不需要在模块末尾调用 ``model_rebuild()``。
    那个调用是 import 期执行的——一旦前向引用解析不了，报错会发生在
    `import app.schemas.backup`，把整条 API 链一起打断。能省掉就省掉。
    """

    id: int
    task_id: int | None = None
    device_id: int
    device_name: str | None = None
    snapshot_id: int | None = None
    trigger: str
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    duration_ms: int = 0
    attempts: int = 1
    changed: bool | None = None
    size_bytes: int = 0
    line_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    issues: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    session_log_path: str | None = None


class TaskDetail(TaskOut):
    device_ids: list[int] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    runs: list[RunOut] = Field(default_factory=list)


class TaskEvent(BaseModel):
    """SSE 事件体。"""

    type: str
    task_id: int
    at: UtcDatetime | None = None
    seq: int = 0
    data: dict = Field(default_factory=dict)
