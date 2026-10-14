"""P2/P3/P4 冒烟子集：35 项用例（其中 17 项是整文件），按"炸了以后影响面"排序，最后几项最慢。

用法::

    cd backend
    python scripts/smoke.py

为什么要有它：第一次跑完整套（约 400 条）如果一片红，很难分辨是"环境/导入链问题"还是
"某个子系统写错了"。这个子集每条压一个不同的子系统，几分钟内就能把范围缩小到某一个
模块；确认它们都绿了，再去跑全量（``python -m pytest -q``）。

**它只是 pytest 的薄封装**，不做任何额外判断——避免"冒烟脚本自己成了新的不确定源"。
如果名单里某条被改名，pytest 会因为"找不到用例"直接报错（这是想要的：名单必须跟着测试走）。

名单的分工：P2 那部分与 ``docs/P2_ACCEPTANCE.md`` 步骤 1a 的表格一一对应；
P3/P4 追加的条目就直接看下面每个分组前的注释——**不要拿 P2 那张表来核对整份名单**，
它只覆盖 P2 那一段。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 顺序即"影响面从大到小"：导入链 → 迁移/启动 → 纯逻辑 → 接口 → 规模化
NODE_IDS = (
    # ① 导入链：它挂了，其它失败多半是连锁反应
    "tests/unit/test_package_exports.py",
    "tests/unit/test_migrations.py",
    "tests/unit/test_startup.py",
    # ①.5 P3 纯逻辑（秒级，且都是"改了规则/等级才需要动"的高价值断言）
    "tests/unit/test_compliance.py",
    "tests/unit/test_diffs_risk.py",
    "tests/unit/test_metrics.py",
    # ①.6 P4 下发链路的纯逻辑：前置预检（危险命令拦截）、回滚目标选择、
    #      变更单状态机（审批/下发闸门）、回滚内容计算——全是秒级且"改错就会改坏设备"的断言
    "tests/unit/test_deploy_guard.py",
    "tests/unit/test_rollback.py",
    "tests/unit/test_change_flow.py",
    "tests/unit/test_rollback_lines.py",
    # ①.7 P4 Git 正文后端：同一内容只提交一次、git 挂了备份照常成功（环境无 git 时自动跳过）
    "tests/unit/test_git_blobs.py",
    # ② 定时调度的时间语义（cron 解析、时区、"错过的班次不补跑"）
    "tests/unit/test_schedules.py::test_next_run_daily_cron_lands_on_local_two_am",
    "tests/unit/test_schedules.py::test_not_before_skips_missed_interval_runs",
    # ③ 保留策略：删错就是不可恢复的数据丢失
    "tests/unit/test_retention.py::test_keeps_oldest_and_newest_always",
    "tests/unit/test_retention.py::test_prune_unreferenced_spares_freshly_written_blobs",
    # ④ 通知：报文形状 + 邮件加密方式映射
    "tests/unit/test_notifications.py::test_deliver_payload_shape",
    "tests/unit/test_notifications.py::test_mail_starttls_mode_does_not_use_ssl",
    "tests/unit/test_notifications.py::test_post_json_generic_webhook_does_not_interpret_code",
    # ⑤ 运营接口：分组 / 策略 / 渠道脱敏 / 通知主链路 / 清理执行
    "tests/api/test_ops.py::test_group_crud_and_membership",
    "tests/api/test_ops.py::test_schedule_create_list_and_describe",
    "tests/api/test_ops.py::test_manual_run_creates_task_without_moving_next_run",
    "tests/api/test_ops.py::test_channel_webhook_url_is_redacted",
    "tests/api/test_ops.py::test_failed_task_triggers_channel_and_writes_log",
    # 触发规则与配置校验：这两个是"界面上能配、实际不生效"的典型
    "tests/api/test_ops.py::test_device_failed_streak_event_fires_only_after_repeated_failures",
    "tests/api/test_ops.py::test_schedule_with_impossible_window_is_rejected",
    "tests/api/test_ops.py::test_retention_run_prunes_and_reclaims_blobs",
    # ⑥ 跨模块：分组圈范围、调度器发任务
    "tests/api/test_group_scope.py::test_batch_backup_by_group_only_touches_members",
    "tests/api/test_scheduler.py::test_tick_fires_due_schedule_and_reports_final_status",
    # ⑦ 规模化不变量（run ≠ snapshot + 内容寻址），慢一些但值得
    "tests/api/test_scale.py::test_scale_two_rounds_keep_runs_and_versions_distinct",
    # ⑧ P3 接口层：合规报表/立即检查/导出留痕 + 审计追溯（等保那一问）
    #    放在最后是有意的：它们最慢（要真跑备份），前面全绿再花时间跑它们
    "tests/api/test_compliance.py",
    "tests/api/test_audit.py",
    # ⑨ P4 接口层：变更单全链路（权限/状态落库/审计/预演不改状态）+ 一键回滚
    #    同样慢：每个用例都要真跑备份来造版本
    "tests/api/test_deploy_flow.py",
    "tests/api/test_rollback_flow.py",
    # ⑩ 看板聚合：一次取齐的数字必须互相对得上（未知合规要单独占一格）
    "tests/api/test_overview.py",
    # ⑪ 跨设备检索：全库明文检索的权限、留痕、以及"被 limit 截断要说出来"
    "tests/api/test_search.py",
)


def main() -> int:
    print(f"P2/P3/P4 冒烟：{len(NODE_IDS)} 项（其中 17 项是整文件，约 5~7 分钟）\n")
    command = [sys.executable, "-m", "pytest", "-q", *NODE_IDS]
    # 用 python -m pytest 而不是裸 pytest：两者可能不是同一个解释器（README 里踩过）
    return subprocess.call(command, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
