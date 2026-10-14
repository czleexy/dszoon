"""审计日志写入。

P1 先在关键动作上落表（登录、凭据增删改、设备增删改、发起备份、下载配置、
查看明文）。完整的审计查询界面属于 P3，但**记录从第一天就要开始**——
事后补记录是不可能的。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AuditLog
from ..db.session import Database

__all__ = ["ACTIONS", "record", "record_detached"]

logger = logging.getLogger(__name__)

#: 便于前端/报表按动作过滤
ACTIONS: dict[str, str] = {
    "auth.login": "登录",
    "auth.login_failed": "登录失败",
    "auth.change_password": "修改口令",
    "user.create": "创建用户",
    "user.update": "修改用户",
    "credential.create": "创建凭据",
    "credential.update": "修改凭据",
    "credential.delete": "删除凭据",
    "device.create": "创建设备",
    "device.update": "修改设备",
    "device.delete": "删除设备",
    "device.import": "批量导入设备",
    "device.test": "连接测试",
    "backup.start": "发起备份",
    "backup.cancel": "取消备份",
    "snapshot.download": "下载配置",
    "snapshot.view_plaintext": "查看配置明文",
    # P2
    "group.create": "创建设备分组",
    "group.update": "修改设备分组",
    "group.delete": "删除设备分组",
    "group.set_devices": "调整分组设备",
    "schedule.create": "创建定时策略",
    "schedule.update": "修改定时策略",
    "schedule.delete": "删除定时策略",
    "schedule.run": "手工触发定时策略",
    "channel.create": "创建通知渠道",
    "channel.update": "修改通知渠道",
    "channel.delete": "删除通知渠道",
    "channel.test": "测试通知渠道",
    "retention.update": "修改保留策略",
    "retention.run": "执行保留策略清理",
    # P3
    "compliance.run": "执行合规检查",
    "compliance.export": "导出合规报表",
    "audit.export": "导出审计日志",
    "search.config": "搜索配置明文",
    # P4
    "deploy.create": "创建变更单",
    "deploy.edit": "修改变更单内容",
    "deploy.submit": "提交变更单审批",
    "deploy.approve": "批准变更单",
    "deploy.reject": "驳回变更单",
    "deploy.cancel": "撤回变更单",
    "deploy.baseline": "登记下发前基线",
    "deploy.dry_run": "下发预演（未发送任何命令）",
    "deploy.run": "执行配置下发",
}


async def record(
    session: AsyncSession,
    *,
    action: str,
    user_id: int | None = None,
    username: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    client_ip: str | None = None,
) -> None:
    """在当前事务里追加一条审计记录（不 commit，交给调用方）。"""
    session.add(
        AuditLog(
            action=action,
            user_id=user_id,
            username=username,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail or {},
            client_ip=client_ip,
        )
    )


async def record_detached(database: Database, **kwargs: Any) -> None:
    """独立事务写审计（用于后台任务等没有请求会话的场景）。"""
    try:
        async with database.session() as session:
            await record(session, **kwargs)
            await session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能影响主流程，但必须留日志
        logger.exception("写审计日志失败：%s", kwargs.get("action"))
