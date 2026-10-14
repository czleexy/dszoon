"""定时调度执行器。

**为什么是"轮询数据库"而不是"给每个策略注册一个 APScheduler job"**：

调度策略是**数据库里的数据**，随时会被增删改启停。用 APScheduler 注册 job 就必须
维护"数据库行 ↔ 内存 job"的双向同步（新增/修改/删除/启停/重启恢复），少同步一处
就出现"改了策略不生效""删了策略还在跑"这类极难查的问题。而轮询方案：

* 唯一事实来源就是数据库，没有同步问题；
* 进程重启后自动恢复（下一轮 tick 就按库里的 next_run_at 执行）；
* 多实例部署时天然是"谁抢到谁执行"的雏形（配合行级锁可以做到互斥）。

代价是精度受轮询间隔限制（默认 30 秒）——对"每天凌晨备份"这种粒度完全够用，
误差远小于错峰抖动的分钟级量级。**APScheduler 仍然在用**：cron 的解析与下次时间
计算走它的 ``CronTrigger``（见 ``services/schedules.py``），只是不拿它当执行器。

``tick()`` 是**可单测的单元**：测试直接构造一个"已到期"的策略再调它，不需要等时钟。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import utcnow
from ..db.models import BackupTask, Schedule
from ..errors import AppError
from ..schemas.backup import BackupRequest
from .backups import BackupRunner
from .retention import enforce, ensure_policy
from .schedules import compute_next_run

logger = logging.getLogger(__name__)

__all__ = ["ScheduleRunner"]

#: 上一次任务还在跑时，把状态标成这个（便于界面上区分"被跳过"和"失败"）
STATUS_BUSY = "skipped_busy"
STATUS_ERROR = "error"


class ScheduleRunner:
    def __init__(
        self,
        *,
        database,
        runner: BackupRunner,
        poll_interval: float = 30.0,
        retention_interval_hours: float = 0.0,
    ) -> None:
        self.database = database
        self.runner = runner
        self.poll_interval = max(5.0, poll_interval)
        #: 0 = 不在后台自动清理（只允许界面/接口手工执行）
        self.retention_interval_hours = max(0.0, retention_interval_hours)
        self._loop_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._loop(), name="dszoon-scheduler")

    async def stop(self) -> None:
        if self._loop_task is None:
            return
        self._loop_task.cancel()
        try:
            await self._loop_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭时不抛
            pass
        self._loop_task = None

    async def _loop(self) -> None:
        logger.info(
            "定时调度器已启动（轮询间隔 %.0fs，保留策略间隔 %.1fh）",
            self.poll_interval,
            self.retention_interval_hours,
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单次 tick 失败绝不能让调度器死掉
                logger.exception("定时调度 tick 失败，将在下一轮重试")
            try:
                await self.maybe_run_retention()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 清理失败同样不能拖死调度器
                logger.exception("保留策略自动清理失败，将在下一轮重试")
            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    async def maybe_run_retention(self, *, now: datetime | None = None) -> dict | None:
        """到点就跑一次保留策略；没到点/已停用则什么都不做。

        **为什么清理要自动跑**：只提供"立即清理"按钮的保留策略，在真实环境里等于
        永远不跑——版本会一直堆积到磁盘满，而那时候已经晚了。

        幂等性靠策略行上的 ``last_run_at``（不是进程内存）：进程重启、多实例部署
        都不会导致重复清理；反过来，停机期间错过的清理会在下一次轮询时补上（清理
        这类操作"补跑"是安全的，与备份任务的"不补跑"相反）。
        """
        if self.retention_interval_hours <= 0:
            return None
        moment = now or utcnow()
        async with self.database.session() as session:
            policy = await ensure_policy(session)
            if not policy.enabled:
                return None
            last = policy.last_run_at
            if last is not None and moment - last < timedelta(
                hours=self.retention_interval_hours
            ):
                return None

            summary = await enforce(session, self.runner.blobs, policy=policy, now=moment)
            policy.last_run_at = moment
            policy.last_result = summary
            await session.commit()
        logger.info(
            "保留策略自动清理完成：扫描 %s 个版本，删除 %s 个，回收 %s 个正文文件",
            summary.get("scanned_snapshots"),
            summary.get("deleted_snapshots"),
            summary.get("deleted_blobs"),
        )
        return summary

    # ------------------------------------------------------------------
    async def tick(self, *, now: datetime | None = None) -> list[int]:
        """检查所有到期策略并执行，返回本次发起的任务 id 列表。"""
        moment = now or utcnow()
        fired: list[int] = []

        async with self.database.session() as session:
            result = await session.execute(
                select(Schedule)
                .where(
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at.is_not(None),
                    Schedule.next_run_at <= moment,
                )
                .order_by(Schedule.next_run_at)
            )
            for schedule in result.scalars().all():
                task_id = await self._fire(session, schedule, moment)
                if task_id is not None:
                    fired.append(task_id)
            await session.commit()

        # 必须等 commit 之后再启动：后台任务会另开 session 去读这一行
        for task_id in fired:
            self.runner.start(task_id)
        return fired

    async def _fire(
        self, session: AsyncSession, schedule: Schedule, now: datetime
    ) -> int | None:
        # 上一次的任务还在跑 → 跳过本次，避免同一个策略把自己的任务叠起来
        # （1000 台一次要跑十几分钟，而策略可能是每 5 分钟一次）
        if schedule.last_task_id:
            previous = await session.get(BackupTask, schedule.last_task_id)
            if previous is not None and previous.status in ("queued", "running"):
                schedule.last_status = STATUS_BUSY
                schedule.next_run_at = compute_next_run(
                    schedule, after=schedule.next_run_at or now
                )
                logger.info(
                    "策略 %s 的上一次任务 #%s 仍在运行，跳过本次", schedule.name, previous.id
                )
                return None

        try:
            payload = BackupRequest(**{**schedule.scope, **schedule.params})
        except Exception as exc:  # noqa: BLE001 - pydantic 校验失败
            schedule.last_status = STATUS_ERROR
            schedule.last_run_at = now
            schedule.next_run_at = compute_next_run(
                schedule, after=schedule.next_run_at or now
            )
            logger.warning("策略 %s 的范围参数不合法：%s", schedule.name, exc)
            return None

        if not payload.name:
            payload.name = f"{schedule.name}（定时）"

        try:
            task = await self.runner.create_task(
                session, payload=payload, trigger="scheduled", schedule_id=schedule.id
            )
        except AppError as exc:
            # 范围解析不出设备（设备被删/全部停用）等：记在策略上，不刷日志也不中断调度
            schedule.last_status = STATUS_ERROR
            schedule.last_run_at = now
            schedule.next_run_at = compute_next_run(
                schedule, after=schedule.next_run_at or now
            )
            logger.warning("策略 %s 发起任务失败：%s", schedule.name, exc)
            return None

        schedule.last_task_id = task.id
        schedule.last_run_at = now
        schedule.last_status = "queued"
        # ★ 从"本次计划时刻"往后推，而不是从 now 推：否则每次 tick 的延迟都会累积，
        #   cron 的固定时刻会越跑越晚；同时 not_before=now 保证错过的班次不补跑
        #   （停机 3 天后不该瞬间涌出几百个任务）。
        schedule.next_run_at = compute_next_run(
            schedule, after=schedule.next_run_at or now, not_before=now
        )
        if schedule.next_run_at is None:
            # 创建/修改时已经拦过"cron 与窗口没有交集"（见 schedules._next_run_or_fail），
            # 但库里的配置被直接改过的话仍可能走到这里。不抛异常（一条坏策略不该打断
            # 整轮 tick），只明确报出来——否则它就此静默停摆，没人知道为什么。
            logger.warning(
                "策略 %s 算不出下一次执行时刻（cron 与执行窗口没有交集？），已停摆，请检查配置",
                schedule.name,
            )
        logger.info("策略 %s 已发起任务 #%s（%s 台设备）", schedule.name, task.id, task.total)
        return task.id
