"""设备与凭据 schema。

安全约定：凭据 profile 的响应模型里**没有** password 字段，只有
``has_password`` / ``has_enable_password`` 布尔标记；请求模型里的 password 是
只写字段（写进去就加密落库，永远不会再读出来）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import ORMModel, UtcDatetime

__all__ = [
    "CredentialProfileCreate",
    "CredentialProfileOut",
    "CredentialProfileUpdate",
    "DeviceCreate",
    "DeviceDetail",
    "DeviceImportResult",
    "DeviceOut",
    "DeviceTestResult",
    "DeviceUpdate",
    "ImportRow",
]


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
class CredentialProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=256)
    enable_password: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=255)


class CredentialProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    #: 传 None 表示"不改"；传空串表示"清空"
    password: str | None = Field(default=None, max_length=256)
    enable_password: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=255)


class CredentialProfileOut(ORMModel):
    id: int
    name: str
    username: str
    description: str | None = None
    has_password: bool = False
    has_enable_password: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=128)
    vendor: str = Field(min_length=1, max_length=32)
    port: int = Field(default=22, ge=1, le=65535)
    protocol: str = Field(default="ssh", pattern="^(ssh|telnet)$")

    credential_profile_id: int | None = None
    hostname: str | None = Field(default=None, max_length=128)
    site: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    legacy_algorithms: bool = False
    use_paramiko: bool = False
    encoding: str | None = Field(default=None, max_length=16)
    connect_timeout: float | None = Field(default=None, gt=0, le=600)
    command_timeout: float | None = Field(default=None, gt=0, le=3600)
    idle_timeout: float | None = Field(default=None, gt=0, le=600)

    notes: str | None = None


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=128)
    vendor: str | None = Field(default=None, min_length=1, max_length=32)
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: str | None = Field(default=None, pattern="^(ssh|telnet)$")
    credential_profile_id: int | None = None
    hostname: str | None = Field(default=None, max_length=128)
    site: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = None
    enabled: bool | None = None
    legacy_algorithms: bool | None = None
    use_paramiko: bool | None = None
    encoding: str | None = Field(default=None, max_length=16)
    connect_timeout: float | None = Field(default=None, gt=0, le=600)
    command_timeout: float | None = Field(default=None, gt=0, le=3600)
    idle_timeout: float | None = Field(default=None, gt=0, le=600)
    notes: str | None = None


class DeviceOut(ORMModel):
    id: int
    name: str
    host: str
    port: int
    protocol: str
    vendor: str
    hostname: str | None = None
    model: str | None = None
    os_version: str | None = None
    site: str | None = None
    tags: list[str] = Field(default_factory=list)
    credential_profile_id: int | None = None
    credential_profile_name: str | None = None
    #: 所属分组（P2）；分组管理界面要靠它回显成员，漏了会静默丢成员
    group_id: int | None = None
    enabled: bool
    legacy_algorithms: bool = False
    use_paramiko: bool = False
    encoding: str | None = None
    # ★ 这三个字段必须返回：DeviceUpdate 接受它们，若不回显，前端编辑页只能提交 null，
    #   而 null 会被当成"显式设置"写库 —— 结果是"在界面上改个名字就把超时覆盖清掉了"。
    #   读接口与写接口的字段集合不一致，是这类静默数据丢失的常见成因。
    connect_timeout: float | None = None
    command_timeout: float | None = None
    idle_timeout: float | None = None
    last_backup_at: UtcDatetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    notes: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class DeviceDetail(DeviceOut):
    """详情页额外带上最近一次采集与版本统计，避免前端多次请求。"""

    snapshot_count: int = 0
    latest_snapshot_id: int | None = None
    latest_sha256: str | None = None
    latest_captured_at: UtcDatetime | None = None


class DeviceTestResult(BaseModel):
    ok: bool
    status: str
    hostname: str | None = None
    facts: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    version_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0


# --------------------------------------------------------------------------
# 批量导入
# --------------------------------------------------------------------------
class ImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    vendor: str
    port: int = 22
    username: str | None = None
    password: str | None = None
    enable_password: str | None = None
    credential_profile: str | None = None
    site: str | None = None
    tags: str | None = None
    hostname: str | None = None


class DeviceImportResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    credential_profiles_created: list[str] = Field(default_factory=list)
