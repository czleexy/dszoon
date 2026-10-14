"""ORM 模型。

P1 的表设计（7 张表）：

* ``users``               登录用户 + 角色
* ``credential_profiles`` 设备凭据（密码密文），多台设备复用一份
* ``devices``             设备台账
* ``backup_tasks``        一次批量备份任务（前端进度条/SSE 的载体）
* ``backup_runs``         **每台设备每次尝试**的结果（失败也要留痕）
* ``config_snapshots``    **内容寻址的版本**（同一内容只存一行）
* ``audit_logs``          谁在什么时候动了什么

``backup_runs`` 与 ``config_snapshots`` 分开是有意的：run 是"尝试"，
snapshot 是"版本"。同一份配置采 100 次只有 1 个 snapshot，但会有 100 条 run
——这样"无变更"的巡检不会把版本库撑爆，而每一次失败/成功都可追溯。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow

__all__ = [
    "AuditLog",
    "BackupRun",
    "BackupTask",
    "ChangeRequest",
    "ConfigSnapshot",
    "CredentialProfile",
    "DeployRun",
    "Device",
    "DeviceGroup",
    "NotificationChannel",
    "NotificationLog",
    "RetentionPolicy",
    "Schedule",
    "User",
]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------
# 用户
# --------------------------------------------------------------------------
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer", index=True)
    display_name: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} role={self.role}>"


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
class CredentialProfile(Base, TimestampMixin):
    """一组可被多台设备复用的登录凭据。密码只以密文形式落库。"""

    __tablename__ = "credential_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str | None] = mapped_column(Text)
    enable_password_enc: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String(255))
    devices: Mapped[list["Device"]] = relationship(back_populates="credential_profile")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CredentialProfile {self.name} user={self.username}>"


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
class Device(Base, TimestampMixin):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 台账名（人工可读，唯一）
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(128), index=True)
    port: Mapped[int] = mapped_column(Integer, default=22)
    protocol: Mapped[str] = mapped_column(String(8), default="ssh")
    vendor: Mapped[str] = mapped_column(String(32), index=True)

    #: 设备上真实的 hostname（从提示符/配置反查）；用于收紧提示符正则
    hostname: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))

    site: Mapped[str | None] = mapped_column(String(64), index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    credential_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("credential_profiles.id", ondelete="SET NULL")
    )
    credential_profile: Mapped[CredentialProfile | None] = relationship(
        back_populates="devices"
    )

    #: 设备分组（P2）。用独立表而不是自由文本字段：分组要能改名、要能查成员、
    #: 要能作为定时任务的范围条件，自由文本做不到这些。
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_groups.id", ondelete="SET NULL"), index=True
    )
    group: Mapped["DeviceGroup | None"] = relationship(back_populates="devices")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # ---- 采集参数覆盖（None = 用全局默认）----
    legacy_algorithms: Mapped[bool] = mapped_column(Boolean, default=False)
    use_paramiko: Mapped[bool] = mapped_column(Boolean, default=False)
    encoding: Mapped[str | None] = mapped_column(String(16))
    connect_timeout: Mapped[float | None] = mapped_column(Float)
    command_timeout: Mapped[float | None] = mapped_column(Float)
    idle_timeout: Mapped[float | None] = mapped_column(Float)

    # ---- 最近一次采集状态（冗余字段：列表页要展示，避免 N+1）----
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_status: Mapped[str | None] = mapped_column(String(24))
    last_error: Mapped[str | None] = mapped_column(String(512))

    notes: Mapped[str | None] = mapped_column(Text)

    snapshots: Mapped[list["ConfigSnapshot"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list["BackupRun"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def credential_profile_name(self) -> str | None:
        """供 DeviceOut 直接映射（关系需 eager load，否则 async 下会 MissingGreenlet）。"""
        return self.credential_profile.name if self.credential_profile else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Device {self.name} {self.host} vendor={self.vendor}>"


# --------------------------------------------------------------------------
# 版本与运行记录
# --------------------------------------------------------------------------
class ConfigSnapshot(Base, TimestampMixin):
    """一份配置版本（按内容寻址，同内容只存一行）。"""

    __tablename__ = "config_snapshots"
    __table_args__ = (
        UniqueConstraint("device_id", "sha256"),
        Index("ix_config_snapshots_device_captured", "device_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    device: Mapped[Device] = relationship(back_populates="snapshots")

    #: 配置正文的 sha256（内容寻址）
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    #: 归一化后的 sha256——只用于"是否发生了实质变更"的快速判断
    normalized_sha256: Mapped[str | None] = mapped_column(String(64))
    #: 正文在对象目录里的相对路径
    object_key: Mapped[str] = mapped_column(String(255))

    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    model: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ConfigSnapshot device={self.device_id} sha={self.sha256[:8]}>"


class BackupRun(Base):
    """一次采集尝试的结果。失败、超时、内容不可信都要留痕。"""

    __tablename__ = "backup_runs"
    __table_args__ = (Index("ix_backup_runs_device_started", "device_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("backup_tasks.id", ondelete="SET NULL"), index=True
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    device: Mapped[Device] = relationship(back_populates="runs")
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshots.id", ondelete="SET NULL")
    )

    #: manual | scheduled | retry | test
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(24), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)

    #: 与上一版相比是否变化；None 表示这是首次采集
    changed: Mapped[bool | None] = mapped_column(Boolean)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)

    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    #: 完整性校验发现（截断/折行/预处理命令写入配置等）
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    session_log_path: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BackupRun device={self.device_id} status={self.status}>"


class BackupTask(Base, TimestampMixin):
    """一次批量备份任务。"""

    __tablename__ = "backup_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    #: manual | scheduled
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    #: queued | running | success | partial | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)

    device_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    #: 由哪个定时策略发起（P2）。手工发起时为 None。
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), index=True
    )

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BackupTask {self.id} status={self.status} {self.succeeded}/{self.total}>"


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created_action", "created_at", "action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user_id: Mapped[int | None] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    client_ip: Mapped[str | None] = mapped_column(String(64))


# ==========================================================================
# P2：分组 / 定时调度 / 通知 / 保留策略
# ==========================================================================
class DeviceGroup(Base, TimestampMixin):
    """设备分组。定时任务可以按分组圈范围，比逐台选更实用。"""

    __tablename__ = "device_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(255))
    #: 前端展示用的颜色标记（如 "#409eff"），纯展示，不参与逻辑
    color: Mapped[str | None] = mapped_column(String(16))

    #: passive_deletes=True：删分组时依赖数据库的 ON DELETE SET NULL，
    #: 不让 SQLAlchemy 先把成员加载出来再逐个置空——async 下那会触发懒加载，
    #: 直接抛 MissingGreenlet。
    devices: Mapped[list["Device"]] = relationship(
        back_populates="group", passive_deletes=True
    )

    @property
    def device_count(self) -> int:
        """成员数。需要 eager load 或已加载，否则 async 下会 MissingGreenlet。"""
        return len(self.devices) if self.devices is not None else 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DeviceGroup {self.name}>"


class Schedule(Base, TimestampMixin):
    """定时备份策略。

    设计取舍：

    * **kind 只有 cron 与 interval 两种**，不引入更复杂的触发类型——运维看得懂
      ``0 2 * * *``，看不懂 DSL。
    * ``jitter_minutes``（错峰抖动）不是可选项：几百个策略如果都卡在整点触发，
      瞬间就会涌出一大批任务，设备和采集机同时被打满。抖动让每个策略的实际发起
      时刻在窗口内分散。（设备级的分散由并发上限与设备串行保证，不是靠抖动。）
    * ``window_start/window_end`` 是**执行窗口**：窗口外直接跳过本次（例如变更
      冻结期不采集），而不是排队等窗口。
    * ``scope`` 与 ``BackupRequest`` 同构，这样"定时任务"和"手工发起"走同一套
      范围解析逻辑，不会出现两套语义。
    """

    __tablename__ = "schedules"
    __table_args__ = (Index("ix_schedules_enabled_next", "enabled", "next_run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    #: cron | interval
    kind: Mapped[str] = mapped_column(String(16), default="cron")
    #: 标准 5 段 cron："分 时 日 月 周"，如 "0 2 * * *"
    cron: Mapped[str | None] = mapped_column(String(64))
    #: kind=interval 时生效
    interval_minutes: Mapped[int | None] = mapped_column(Integer)

    #: {"device_ids": [...]} | {"group_id": 1} | {"site": "..."} | {"vendor": "..."} | {"all_enabled": true}
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: {"precheck": true, "retries": 2, "concurrency": 10}
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    jitter_minutes: Mapped[int] = mapped_column(Integer, default=5)
    window_start: Mapped[str | None] = mapped_column(String(5))
    window_end: Mapped[str | None] = mapped_column(String(5))

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("backup_tasks.id", ondelete="SET NULL")
    )
    last_status: Mapped[str | None] = mapped_column(String(16))
    created_by: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Schedule {self.name} kind={self.kind} enabled={self.enabled}>"


class NotificationChannel(Base, TimestampMixin):
    """通知渠道。

    敏感字段（SMTP 口令、机器人签名密钥）单独放 ``secret_enc`` 加密存储，
    和 ``config`` 里的非敏感配置分开——这样查配置时不会顺带把密钥读出来。
    """

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: smtp | webhook | dingtalk | wecom | feishu
    kind: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    #: 非敏感配置。smtp: {host,port,username,from_addr,to_addrs,use_tls}
    #: webhook/机器人: {url}
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: 加密后的敏感配置 JSON。smtp: {password}；机器人: {secret}；webhook: {token}
    secret_enc: Mapped[str | None] = mapped_column(Text)

    #: 触发事件：task_failed | task_partial | integrity_error | significant_change | device_failed_streak
    events: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: 仅当本次失败数 ≥ 该值才通知（避免每台失败都刷屏）
    min_failures: Mapped[int] = mapped_column(Integer, default=1)

    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(String(512))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NotificationChannel {self.name} kind={self.kind}>"


class NotificationLog(Base):
    """通知发送记录。发出去没有、失败原因是什么，都要能查。"""

    __tablename__ = "notification_logs"
    __table_args__ = (Index("ix_notification_logs_created_event", "created_at", "event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="SET NULL")
    )
    #: 冗余渠道名：渠道被删掉之后，历史记录仍然可读
    channel_name: Mapped[str | None] = mapped_column(String(64))

    event: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    #: success | failed | skipped
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(Integer)
    device_id: Mapped[int | None] = mapped_column(Integer)


class RetentionPolicy(Base, TimestampMixin):
    """保留策略（单例表，只有 id=1 一行）。

    为什么用单例而不是"多策略 + 作用域"：P2 只需要一个全局默认值就能满足验收
    （1000 台稳定运行、磁盘不无限增长）。多策略会让"这台设备到底适用哪条"变得
    难以回答，等真出现差异化需求再拆。
    """

    __tablename__ = "retention_policies"

    id: Mapped[int] = mapped_column(primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 变更过的版本保留多少天；None = 永久保留（默认，配置变更是审计证据）
    keep_changed_days: Mapped[int | None] = mapped_column(Integer)
    #: 未变更的版本按"每天最多留一个"保留多少天
    keep_unchanged_daily_days: Mapped[int] = mapped_column(Integer, default=90)
    #: 每台设备至少保留几个版本（含最新），避免清理把历史清空
    keep_min_versions: Mapped[int] = mapped_column(Integer, default=2)
    #: 原始会话日志（排障用）保留多少天，通常远短于配置版本
    keep_session_log_days: Mapped[int] = mapped_column(Integer, default=7)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 上一次清理的结果摘要：{scanned, deleted_snapshots, deleted_blobs, freed_bytes, ...}
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------
# 合规基线（P3）
# --------------------------------------------------------------------------
class ComplianceResult(Base):
    """一台设备的一次合规评估结果。

    **保留历史**而不是"每台设备只留最新一行"：合规率的趋势、以及"这次不合规是不是
    上周改出来的"都要靠历史才能回答。查询"每台设备最新一次"用窗口函数（见
    ``services/compliance.py`` 的 ``latest_results``），不给表加冗余字段。

    为什么把 ``checked_rules`` 也存下来：报表要画"设备 × 规则"矩阵，必须能区分
    "这条规则过了"和"这条规则根本不适用于该厂商"——只存违规列表是区分不出来的。
    """

    __tablename__ = "compliance_results"
    __table_args__ = (
        Index("ix_compliance_results_device_checked", "device_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    device: Mapped[Device] = relationship()
    #: 评估的是哪个版本（保留策略删掉版本后置空，结果本身仍然有效）
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshots.id", ondelete="SET NULL")
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    vendor: Mapped[str] = mapped_column(String(32))
    #: 规则集指纹：换了规则之后新旧结果不会混为一谈
    rules_version: Mapped[str] = mapped_column(String(32))

    #: pass | warn | fail（fail = 有 high 级违规）
    verdict: Mapped[str] = mapped_column(String(8), index=True)
    #: 本次适用并检查了多少条规则（厂商不匹配的规则不计入）
    checked_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    medium_count: Mapped[int] = mapped_column(Integer, default=0)
    low_count: Mapped[int] = mapped_column(Integer, default=0)

    #: 违规明细：[{rule_id, severity, description, message?, line?}]，line 已脱敏
    violations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: 本次评估覆盖的规则 id 列表（用于"设备 × 规则"矩阵）
    checked_rules: Mapped[list[str]] = mapped_column(JSON, default=list)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ComplianceResult device={self.device_id} verdict={self.verdict} "
            f"high={self.high_count} medium={self.medium_count} low={self.low_count}>"
        )


# --------------------------------------------------------------------------
# 变更单与下发记录（P4-B）
# --------------------------------------------------------------------------
class ChangeRequest(Base, TimestampMixin):
    """一条配置变更单：**审批的对象、审批人、回滚基线三样都固定在这一行上**。

    为什么要在"下发"之上再加一层变更单，而不是点一下就发：
    P4 唯一的事后追责依据是"谁批的、批的是哪份内容、出事能回到哪一版"。
    这三件事任何一件没落到记录上，事后就只能靠聊天记录回忆。

    ★ **审批绑定的是 ``content_hash``，不是变更单 id**。批准之后再改一行命令，
    哈希就变了，下发前的比对必然失败——这是防"批的是 A、发的是 B"的唯一办法。

    ★ 一条变更单**只对一台设备**生效（``device_id`` 单值）。§8.5.4 明确不做
    "批量下发到全网"：要改 20 台就建 20 条，逐条审批、逐条留痕。
    想批量时人会嫌麻烦，但"一条单子悄悄改了全网"正是最该被拦住的事。
    """

    __tablename__ = "change_requests"
    __table_args__ = (
        Index("ix_change_requests_device_created", "device_id", "created_at"),
        Index("ix_change_requests_state_created", "state", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    device: Mapped[Device] = relationship()

    #: config = 手工写的变更；rollback = 回滚到历史版本（P4-C 生成）
    kind: Mapped[str] = mapped_column(String(16), default="config")
    #: 状态机当前状态，取值见 services/deploy.py 的 CHANGE_TRANSITIONS
    state: Mapped[str] = mapped_column(String(16), default="draft", index=True)

    #: 待下发的命令（规范化后，一行一条）。与 content_hash 一一对应
    lines: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: 正文的 sha256：审批与下发都绑定它
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    #: 预检结论 DeployPlan.as_dict()；有 error 级问题就不允许进入审批
    precheck: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    #: 申请人。★ 审批人必须是另一个人（§8.5.1 第 1 条）
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    approval_note: Mapped[str | None] = mapped_column(Text)

    #: 回滚变更单的目标版本（kind=rollback 时有值）
    target_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshots.id", ondelete="SET NULL")
    )
    #: ★ 下发前的强制采集（回滚基线）：为空一律不允许下发
    before_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshots.id", ondelete="SET NULL")
    )
    #: 基线的采集时间。单独存一份而不是每次 join 版本表：版本可能被保留策略清理掉
    #: （外键 SET NULL），那时只剩 id 就查不到时间，而"基线是不是为这次变更采的"
    #: 正是决定要不要拦下一次下发的依据。
    before_captured_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 下发后自动采集的结果（事后回答"改完变成了什么"）
    after_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshots.id", ondelete="SET NULL")
    )

    #: 时间线（只追加）：[{at, by, action, note}]
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: 驳回 / 中止 / 作废的原因，直接展示给人看
    reason: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ChangeRequest id={self.id} device={self.device_id} "
            f"state={self.state} hash={self.content_hash[:8]}>"
        )


class DeployRun(Base, TimestampMixin):
    """一次**实际**下发尝试的逐行结果。

    与 ``ChangeRequest`` 分开而不是并成一张表：一条变更单可能下发多次
    （中止后人工处置、重新审批后再发），每次都要留痕。"上一次发到第几行"
    必须能逐次回溯——压成一个字段之后，"第二次是不是从头发的"就只能靠猜。

    ``dry_run`` 也进这张表：预演（只预检、不发命令）本身是有意义的记录，
    它证明"在下发之前有人看过会发生什么"。
    """

    __tablename__ = "deploy_runs"
    __table_args__ = (
        Index("ix_deploy_runs_request_created", "change_request_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    change_request_id: Mapped[int] = mapped_column(
        ForeignKey("change_requests.id", ondelete="CASCADE"), index=True
    )
    change_request: Mapped[ChangeRequest] = relationship()
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )

    #: 执行人（与申请人、审批人分开记录）
    executed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    #: success | stopped | rejected（真实下发） | dry_run（只预检、一行未发）
    #: ★ 没有"部分成功"这种取值；中途中止必须记 stopped：设备此刻新旧配置混在一起，
    #: 记成成功或失败都会误导接手的人。
    status: Mapped[str] = mapped_column(String(16), index=True)
    #: true = 只预检、一行都没发
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 本次实际发送内容的哈希（与变更单的 content_hash 比对后才发）
    content_hash: Mapped[str] = mapped_column(String(64))
    applied_lines: Mapped[int] = mapped_column(Integer, default=0)
    #: 第一行失败的位置（None = 没有失败）
    stopped_at: Mapped[int | None] = mapped_column(Integer)

    #: 逐行结果 [{index, line, status, detail?}]；回显只留摘要，不灌整段
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DeployRun id={self.id} request={self.change_request_id} "
            f"status={self.status} applied={self.applied_lines}>"
        )
