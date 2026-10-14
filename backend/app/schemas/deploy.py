"""变更单与下发的对外契约（P4-B）。

约定与其它 schema 一致：时间一律走 ``UtcDatetime``（朴素 UTC → 带 Z 的 ISO），
可空性由字段自己表达。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from .common import ORMModel, UtcDatetime

__all__ = [
    "BaselineIn",
    "ChangeCancelIn",
    "ChangeCreateIn",
    "ChangeDecisionIn",
    "ChangeDetailOut",
    "ChangeLinesIn",
    "ChangeOut",
    "DeployRunOut",
    "PreviewOut",
    "RollbackCreateIn",
    "RollbackCreateOut",
    "RollbackPreviewOut",
]


class ChangeCreateIn(BaseModel):
    device_id: int
    #: 待下发的命令。既接受数组，也接受**多行字符串**——"从前端 textarea 直接粘一段"
    #: 和"逐行编辑"是同一件事的两种输入形态，逼前端先切分只会多一处出错的地方。
    lines: list[str] | str
    note: str | None = Field(default=None, max_length=500)

    @field_validator("lines", mode="before")
    @classmethod
    def _split_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.splitlines()
        return value


class ChangeLinesIn(BaseModel):
    lines: list[str] | str
    note: str | None = Field(default=None, max_length=500)

    @field_validator("lines", mode="before")
    @classmethod
    def _split_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.splitlines()
        return value


class ChangeDecisionIn(BaseModel):
    #: true = 批准，false = 驳回（驳回必须写 note，理由由服务层校验）
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class ChangeCancelIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class BaselineIn(BaseModel):
    #: 下发前的采集版本 id（必须先对这台设备采集一次）
    snapshot_id: int


class ChangeOut(ORMModel):
    id: int
    device_id: int
    device_name: str | None = None
    kind: str = "config"
    state: str
    state_label: str = ""
    content_hash: str = ""
    line_count: int = 0
    requested_by: int
    approved_by: int | None = None
    approved_at: UtcDatetime | None = None
    approval_note: str | None = None
    target_snapshot_id: int | None = None
    before_snapshot_id: int | None = None
    before_captured_at: UtcDatetime | None = None
    after_snapshot_id: int | None = None
    reason: str | None = None
    precheck: dict[str, Any] = Field(default_factory=dict)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class DeployRunOut(ORMModel):
    id: int
    change_request_id: int
    device_id: int
    executed_by: int | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    #: success | stopped | rejected | dry_run
    status: str
    dry_run: bool = False
    content_hash: str = ""
    applied_lines: int = 0
    stopped_at: int | None = None
    results: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class ChangeDetailOut(ChangeOut):
    """详情：多带命令正文与最近的预演/下发记录。"""

    lines: list[str] = Field(default_factory=list)
    runs: list[DeployRunOut] = Field(default_factory=list)


class PreviewOut(BaseModel):
    """预演结果（只预检，**不发送任何命令、不改变更单状态**）。"""

    change_id: int
    state: str
    content_hash: str
    dry_run: bool = True
    #: 是否满足下发条件（开关、审批、哈希、基线全部就位）
    can_deploy: bool = False
    #: DeployPlan.as_dict()：行数、预检问题
    plan: dict[str, Any] = Field(default_factory=dict)
    #: 阻挡下发的原因（闸门输出）；为空才允许真正下发
    blockers: list[dict[str, str]] = Field(default_factory=list)


class RollbackPreviewOut(BaseModel):
    """"一键回滚"的预演（只读：不建单、不发命令）。"""

    device_id: int
    ok: bool = False
    current_snapshot_id: int | None = None
    reason: str | None = None
    #: 选中的目标版本 {snapshot_id, captured_at, line_count, sha256}
    target: dict[str, Any] | None = None
    #: 跳过了哪些版本、为什么——不列出来，运维会以为工具漏了最新那一版
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    #: RollbackPlan.as_dict()：要补的行、要人工处理的行、是否截断
    rollback: dict[str, Any] | None = None
    #: 版本对比摘要（含 unified diff）
    diff: dict[str, Any] | None = None


class RollbackCreateIn(BaseModel):
    """回滚建单请求。

    ★ 这里**故意没有 device_id**：设备是路径参数（``POST /devices/{id}/rollback``）。
    请求体里再放一份会造成两种后果，后一种更常见也更烦人——"路径说 A、body 说 B"
    的歧义，以及 FastAPI 一律要求 body 里必须有 device_id，于是按路径调用的人
    **永远建不了单**（422 且报错指向 body.device_id，看不出是接口设计冲突）。
    """

    #: 留空 = 以设备最新版本为"当前版本"
    current_snapshot_id: int | None = None
    note: str | None = Field(default=None, max_length=500)


class RollbackCreateOut(BaseModel):
    """回滚变更单 + 它是怎么算出来的（界面要一起展示，便于复核）。"""

    change: ChangeOut
    preview: RollbackPreviewOut
