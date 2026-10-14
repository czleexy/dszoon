"""合规基线的对外契约（P3）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import ORMModel, UtcDatetime

__all__ = [
    "ComplianceResultOut",
    "ComplianceRunRequest",
    "ComplianceRunResult",
    "ComplianceSummary",
    "RuleOut",
    "RuleSetOut",
]


class RuleOut(BaseModel):
    id: str
    desc: str
    severity: str
    vendors: list[str] = Field(default_factory=list)
    enabled: bool = True
    assert_present: list[str] = Field(default_factory=list)
    assert_absent: list[str] = Field(default_factory=list)
    message: str | None = None


class RuleSetOut(BaseModel):
    """当前生效的规则集（含指纹，便于确认"跑的是哪一版规则"）。"""

    version: str
    source: str
    rules: list[RuleOut] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)
    enabled: bool = True


class ComplianceResultOut(ORMModel):
    id: int
    device_id: int
    snapshot_id: int | None = None
    checked_at: UtcDatetime
    vendor: str
    rules_version: str
    verdict: str
    checked_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    violations: list[dict[str, Any]] = Field(default_factory=list)


class ComplianceSummary(BaseModel):
    """看板/通知用的聚合结果。"""

    devices: int = 0
    checked: int = 0
    pass_: int = Field(default=0, alias="pass")
    warn: int = 0
    fail: int = 0
    unknown: int = 0
    high_total: int = 0
    compliance_rate: float | None = None

    model_config = {"populate_by_name": True}


class ComplianceRunRequest(BaseModel):
    """立即执行一次合规检查（不采配置，只评估已有版本）。"""

    device_ids: list[int] | None = None
    group_id: int | None = None
    site: str | None = None
    vendor: str | None = None
    #: 检查全部启用设备。少了它就只能"逐个点名"或"按组"，界面上做不出"一键全查"
    all_enabled: bool = False
    include_disabled: bool = False


class ComplianceRunResult(BaseModel):
    checked: int
    skipped_no_snapshot: int
    failed: int
    verdicts: dict[str, int] = Field(default_factory=dict)
    rules_version: str
