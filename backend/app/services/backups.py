"""批量备份任务编排：并发采集 → 落库 → SSE 事件推送。

架构取舍（写清楚，免得后面误改）：

* **事件总线是进程内的**（dict + asyncio.Queue）。单机部署完全够用，也不需要
  Redis。横向扩展时把 :class:`EventBus` 换成 Redis pub/sub、把 :class:`BackupRunner`
  换成 Celery，接口不用动。
* **所有落库都走同一个 session + 一把 asyncio.Lock**。采集是并发的（网络 I/O），
  但写库串行化——SQLite 上并发写会直接 database is locked，而 PostgreSQL 上
  并发写同一个 task 行也需要行锁。串行化的代价可以忽略（每台设备几十毫秒）。
* **不把 store 交给引擎**：P0 引擎自带的 ``ArtifactStore`` 会写 JSONL 索引，
  而 Web 层的唯一事实来源是数据库。所以这里 ``backup_many(store=None)``，
  "是否变更"由本模块基于数据库里的上一版快照判断，避免两套索引打架。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..collector import BackupOutcome, backup_many
from ..collector.registry import DriverRegistry
from ..core.config import Settings
from ..core.crypto import CredentialCipher
from ..db.base import utcnow
from ..db.models import BackupRun, BackupTask, ConfigSnapshot, Device, Schedule, User
from ..db.session import Database
from ..errors import Conflict, NotFound
from ..schemas.backup import BackupRequest
from .blobs import BlobStore
from .compliance import evaluate_text, ruleset
from .devices import build_targets, resolve_devices

__all__ = ["BackupRunner", "EventBus", "final_status"]

logger = logging.getLogger(__name__)


class EventBus:
    """进程内任务事件总线，带历史回放（前端刷新后还能看到进度）。"""

    def __init__(self, *, history: int = 500, queue_size: int = 1000) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[int, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=history)
        )
        self._seq: defaultdict[int, int] = defaultdict(int)
        self._queue_size = queue_size

    def publish(self, task_id: int, event_type: str, **data: Any) -> dict[str, Any]:
        self._seq[task_id] += 1
        event = {
            "type": event_type,
            "task_id": task_id,
            "seq": self._seq[task_id],
            "at": utcnow().isoformat() + "Z",
            "data": data,
        }
        self._history[task_id].append(event)
        for queue in list(self._subscribers.get(task_id, ())):
            # 慢消费者不能拖住采集：满了就丢最旧的
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
                continue
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return event

    def history(self, task_id: int) -> list[dict[str, Any]]:
        return list(self._history.get(task_id, ()))

    @asynccontextmanager
    async def subscribe(
        self, task_id: int, *, replay: bool = True
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        if replay:
            for event in self._history.get(task_id, ()):
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
        self._subscribers[task_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[task_id].discard(queue)
            if not self._subscribers[task_id]:
                self._subscribers.pop(task_id, None)


class BackupRunner:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        cipher: CredentialCipher,
        blobs: BlobStore,
        registry: DriverRegistry | None = None,
        session_factory: Any = None,
        notifier: Any = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.cipher = cipher
        self.blobs = blobs
        self.registry = registry
        #: 测试注入用：``(config, prompt_re) -> InteractiveSession``
        self.session_factory = session_factory
        #: 任务结束后派发通知（P2）。用 Any 而不是具体类型，避免备份层反向依赖通知层。
        self.notifier = notifier
        self.bus = EventBus()
        self._handles: dict[int, asyncio.Task[None]] = {}

    async def _notify(self, task_id: int) -> None:
        """任务结束后派发通知。**通知是附属功能，任何失败都不能影响备份。**"""
        if self.notifier is None:
            return
        try:
            await self.notifier.dispatch_task_result(task_id)
        except Exception:  # noqa: BLE001
            logger.exception("派发通知失败（task=%s），已忽略", task_id)

    def _check_compliance(
        self,
        session: AsyncSession,
        device: Device,
        text: str,
        snapshot_id: int,
    ) -> None:
        """采集到**新版本**后顺手评估一次合规基线（P3）。

        * 只在产生新版本时跑：内容没变的版本，合规结论也不会变——否则 1000 台每天
          巡检一次就会白跑 1000 次评估；
        * 结果与版本走**同一个事务**：不会出现"有版本但没结果"的中间态；
        * **任何异常都吞掉**：合规是增值功能，绝不能影响备份（与通知同一条不变量）。
        """
        if not getattr(self.settings, "compliance_enabled", False):
            return
        try:
            result = evaluate_text(
                device,
                text,
                rules=ruleset(self.settings),
                snapshot_id=snapshot_id,
            )
            session.add(result)
        except Exception:  # noqa: BLE001 - 合规失败不能影响备份
            logger.exception("合规基线评估失败（device=%s），已忽略", device.id)

    @staticmethod
    async def _sync_schedule(session: AsyncSession, task: BackupTask) -> None:
        """把任务终态回写到发起它的定时策略上。

        不做这一步，策略上的 ``last_status`` 会永远停在 "queued"——界面上看不出
        这次定时到底成功还是失败了。
        """
        if not task.schedule_id:
            return
        schedule = await session.get(Schedule, task.schedule_id)
        if schedule is not None:
            schedule.last_status = task.status
            schedule.last_run_at = task.finished_at or schedule.last_run_at

    # ------------------------------------------------------------------
    @property
    def running_task_ids(self) -> list[int]:
        return [tid for tid, handle in self._handles.items() if not handle.done()]

    def is_running(self, task_id: int) -> bool:
        handle = self._handles.get(task_id)
        return handle is not None and not handle.done()

    # ------------------------------------------------------------------
    async def create_task(
        self,
        session: AsyncSession,
        *,
        payload: BackupRequest,
        user: User | None = None,
        trigger: str = "manual",
        schedule_id: int | None = None,
    ) -> BackupTask:
        """解析范围并建任务（不启动）。调用方 commit 后再调 :meth:`start`。

        :param trigger: ``manual`` | ``scheduled``
        :param schedule_id: 由哪个定时策略发起（手工发起为 None）
        """
        devices = await resolve_devices(
            session,
            device_ids=payload.device_ids,
            group_id=payload.group_id,
            site=payload.site,
            vendor=payload.vendor,
            include_disabled=payload.include_disabled,
            all_enabled=payload.all_enabled,
        )
        if not devices:
            raise NotFound("没有匹配到任何设备，请检查筛选条件")

        task = BackupTask(
            name=payload.name,
            trigger=trigger,
            schedule_id=schedule_id,
            status="queued",
            total=len(devices),
            device_ids=[d.id for d in devices],
            params={
                "precheck": payload.precheck,
                "retries": payload.retries,
                "device_names": [d.name for d in devices][:50],
            },
            created_by=user.username if user else None,
            created_by_id=user.id if user else None,
        )
        session.add(task)
        await session.flush()
        return task

    def start(self, task_id: int) -> None:
        if self.is_running(task_id):
            raise Conflict(f"任务 {task_id} 已在运行")
        handle = asyncio.create_task(self._run(task_id), name=f"backup-task-{task_id}")
        self._handles[task_id] = handle

    async def cancel(self, task_id: int, *, wait: float = 5.0) -> bool:
        """取消任务。

        ``wait`` 秒内**等它真的收尾完毕**再返回。为什么不等不行：``cancel()`` 只是
        投递取消信号，收尾（写 cancelled 状态）还在异步进行——界面此刻刷新会看到
        任务仍是"运行中"，测试里还会出现清理竞态。等一小会儿，返回就代表
        "已经取消并且终态已落库"。

        :return: 是否确实取消了一个正在运行的任务
        """
        handle = self._handles.get(task_id)
        if handle is None or handle.done():
            return False
        handle.cancel()
        if wait > 0:
            # 用 asyncio.wait 而不是 wait_for：wait_for 会把"被取消的任务"抛出的
            # CancelledError 与"当前协程被取消"混在一起，容易误吞真实的取消信号
            await asyncio.wait({handle}, timeout=wait)
        return True

    async def shutdown(self) -> None:
        pending = [h for h in self._handles.values() if not h.done()]
        for handle in pending:
            handle.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    async def _run(self, task_id: int) -> None:
        try:
            await self._execute(task_id)
        except asyncio.CancelledError:
            logger.info("备份任务 %s 被取消", task_id)
            await self._finalize(task_id, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常丢给事件循环
            logger.exception("备份任务 %s 异常结束", task_id)
            await self._finalize(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._handles.pop(task_id, None)

    async def _execute(self, task_id: int) -> None:
        async with self.database.session() as session:
            task = await session.get(BackupTask, task_id)
            if task is None:  # pragma: no cover - 任务被删掉了
                logger.warning("备份任务 %s 不存在，放弃执行", task_id)
                return

            task.status = "running"
            task.started_at = utcnow()
            device_ids = list(task.device_ids or [])
            await session.commit()
            self.bus.publish(
                task_id, "task_started", total=task.total, status=task.status
            )

            devices = (
                list(
                    (
                        await session.execute(
                            select(Device)
                            .options(selectinload(Device.credential_profile))
                            .where(Device.id.in_(device_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                if device_ids
                else []
            )
            targets, unbuildable = await build_targets(
                session, devices, settings=self.settings, cipher=self.cipher
            )

            # ★ 缺凭据的设备单独记一条失败，**不拖垮整批**：
            #   50 台里 1 台没配好凭据，绝不该让另外 49 台也不采。
            if unbuildable:
                for device, reason in unbuildable:
                    await self._persist_unbuildable(session, task, device, reason)
                    task.failed += 1
                await session.commit()
                for device, reason in unbuildable:
                    self.bus.publish(
                        task_id,
                        "device_finished",
                        done=task.failed,
                        total=task.total,
                        succeeded=task.succeeded,
                        failed=task.failed,
                        unchanged=task.unchanged,
                        device_id=device.id,
                        device_name=device.name,
                        run_status="error",
                        changed=None,
                        snapshot_id=None,
                        line_count=0,
                        error_code="no_credential",
                        error_message=reason,
                        warnings=[],
                        significant=[],
                    )

            lock = asyncio.Lock()

            async def on_progress(done: int, total: int, outcome: BackupOutcome) -> None:
                async with lock:
                    info = await self._persist_outcome(session, task, outcome)
                    _apply_counters(task, outcome, info["changed"])
                    await session.commit()
                    self.bus.publish(
                        task_id,
                        "device_finished",
                        done=done,
                        total=total,
                        succeeded=task.succeeded,
                        failed=task.failed,
                        unchanged=task.unchanged,
                        **info,
                    )

            params = dict(task.params or {})
            await backup_many(
                targets,
                concurrency=self.settings.backup_concurrency,
                retries=int(params.get("retries") if params.get("retries") is not None else self.settings.backup_retries),
                precheck=bool(params.get("precheck") if params.get("precheck") is not None else self.settings.backup_precheck),
                registry=self.registry,
                session_factory=self.session_factory,
                on_progress=on_progress,
            )

            task.status = final_status(task)
            task.finished_at = utcnow()
            await self._sync_schedule(session, task)
            await session.commit()
            self.bus.publish(
                task_id,
                "task_finished",
                status=task.status,
                total=task.total,
                succeeded=task.succeeded,
                failed=task.failed,
                unchanged=task.unchanged,
            )
        # 放在 session 块之外：通知要另开 session，此时外层事务已提交、连接空闲
        await self._notify(task_id)

    async def _persist_unbuildable(
        self, session: AsyncSession, task: BackupTask, device: Device, reason: str
    ) -> None:
        """给"连采集目标都构造不出来"的设备留痕（典型：没绑凭据）。

        这类失败没有任何采集结果，但**必须出现在界面上**——否则运维看到的只是
        "任务部分失败"，却不知道是哪台、为什么。
        """
        now = utcnow()
        session.add(
            BackupRun(
                task_id=task.id,
                device_id=device.id,
                snapshot_id=None,
                trigger=task.trigger,
                status="error",
                started_at=now,
                finished_at=now,
                duration_ms=0,
                attempts=1,
                changed=None,
                size_bytes=0,
                line_count=0,
                error_code="no_credential",
                error_message=reason,
                issues=[],
                warnings=[],
            )
        )
        device.last_backup_at = now
        device.last_status = "error"
        device.last_error = reason[:500]

    async def _finalize(self, task_id: int, status: str, *, error: str | None = None) -> None:
        """异常/取消后的收尾。用**独立 session**，因为原事务可能已经不可用。"""
        try:
            async with self.database.session() as session:
                task = await session.get(BackupTask, task_id)
                if task is None:
                    return
                task.status = status
                task.finished_at = utcnow()
                if error:
                    task.error_message = error[:1000]
                await self._sync_schedule(session, task)
                await session.commit()
                self.bus.publish(
                    task_id,
                    "task_finished",
                    status=status,
                    total=task.total,
                    succeeded=task.succeeded,
                    failed=task.failed,
                    unchanged=task.unchanged,
                    error=error,
                )
        except Exception:  # noqa: BLE001 - 收尾失败只能记日志
            logger.exception("任务 %s 收尾失败", task_id)
            return
        # 取消/异常也要通知——"任务挂了没人知道"恰恰是最需要通知的场景
        await self._notify(task_id)

    # ------------------------------------------------------------------
    async def _persist_outcome(
        self, session: AsyncSession, task: BackupTask, outcome: BackupOutcome
    ) -> dict[str, Any]:
        device_id = outcome.target.id
        if device_id is None:  # pragma: no cover - 调用方保证有 id
            raise ValueError("采集目标缺少设备 id，无法落库")
        device = await session.get(Device, device_id)
        device_name = device.name if device else outcome.target.display

        finished = utcnow()
        started = finished - timedelta(milliseconds=max(0, outcome.duration_ms))
        changed: bool | None = None
        snapshot: ConfigSnapshot | None = None
        session_log_path: str | None = None

        if outcome.running_config is not None and outcome.sha256:
            blob = self.blobs.put_text(outcome.running_config)
            previous = await session.scalar(
                select(ConfigSnapshot)
                .where(ConfigSnapshot.device_id == device_id)
                .order_by(ConfigSnapshot.captured_at.desc(), ConfigSnapshot.id.desc())
                .limit(1)
            )
            changed = None if previous is None else previous.sha256 != outcome.sha256
            snapshot = await session.scalar(
                select(ConfigSnapshot).where(
                    ConfigSnapshot.device_id == device_id,
                    ConfigSnapshot.sha256 == outcome.sha256,
                )
            )
            if snapshot is None:
                snapshot = ConfigSnapshot(
                    device_id=device_id,
                    sha256=outcome.sha256,
                    normalized_sha256=outcome.normalized_sha256,
                    object_key=blob.object_key,
                    size_bytes=outcome.size_bytes,
                    line_count=outcome.line_count,
                    captured_at=finished,
                    model=outcome.facts.get("model"),
                    os_version=outcome.facts.get("software_version"),
                    facts=dict(outcome.facts),
                )
                session.add(snapshot)
                await session.flush()
                # P3：有了新版本就顺手评估一次合规基线（同一事务，失败只记日志）
                if device is not None:
                    self._check_compliance(session, device, outcome.running_config, snapshot.id)
                    # TODO(P3 剩余项：risky_change 分级告警) 在这里、且**仅在这里**接最合适：
                    #   * 此刻手里同时有"新正文"和"上一版"（previous.object_key 可用 blobs 读回），
                    #     而这是唯一能拿到两侧文本的位置；
                    #   * 需要先给 backup_runs 加两列：risk_level(str|None) 与 risk_hits(JSON)，
                    #     否则告警阶段要重新读盘算 diff（Notifier 里没有 BlobStore）；
                    #   * 分类函数已经写好并有 10 条单测：services/diffs.py::classify_change /
                    #     risk_level_of；告警侧只需在 notifications.detect_events 读 run.risk_level，
                    #     并仿照 compliance_failed 的处理在 _dispatch 里加事件（别只写进正文，
                    #     否则"单订阅 risky_change"的渠道永远不响——那个 bug 在 P2 出现过一次）。
            if self.settings.keep_session_logs and outcome.transcript and device is not None:
                session_log_path = self.blobs.put_session_log(
                    device.name, finished.strftime("%Y%m%dT%H%M%S"), outcome.transcript
                )

        status = "unchanged" if (outcome.ok and changed is False) else outcome.status.value
        run = BackupRun(
            task_id=task.id,
            device_id=device_id,
            snapshot_id=snapshot.id if snapshot else None,
            trigger=task.trigger,
            status=status,
            started_at=started,
            finished_at=finished,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            changed=changed,
            size_bytes=outcome.size_bytes,
            line_count=outcome.line_count,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            issues=[
                {"code": i.code, "message": i.message, "severity": i.severity}
                for i in outcome.issues
            ],
            warnings=list(outcome.warnings),
            session_log_path=session_log_path,
        )
        session.add(run)

        if device is not None:
            device.hostname = outcome.hostname or device.hostname
            device.model = outcome.facts.get("model") or device.model
            device.os_version = outcome.facts.get("software_version") or device.os_version
            device.last_backup_at = finished
            device.last_status = status
            device.last_error = None if outcome.ok else (outcome.error_message or "")[:500]

        return {
            "device_id": device_id,
            "device_name": device_name,
            "run_status": status,
            "changed": changed,
            "snapshot_id": snapshot.id if snapshot else None,
            "line_count": outcome.line_count,
            "error_code": outcome.error_code,
            "error_message": outcome.error_message,
            "warnings": list(outcome.warnings),
            "significant": [
                i.message for i in outcome.issues if i.code == "setup_leaked_into_config"
            ],
        }


# --------------------------------------------------------------------------
def _apply_counters(task: BackupTask, outcome: BackupOutcome, changed: bool | None) -> None:
    if outcome.ok:
        task.succeeded += 1
        if changed is False:
            task.unchanged += 1
    else:
        task.failed += 1


def final_status(task: BackupTask) -> str:
    """任务终态判定。

    ``partial`` 专门留给"有些设备根本没跑到"（被取消、中途异常）——这种情况
    既不是成功也不是失败，必须让人看见。
    """
    if task.total <= 0:
        return "success"
    done = task.succeeded + task.failed
    if done < task.total:
        return "partial"
    if task.failed == 0:
        return "success"
    if task.succeeded == 0:
        return "failed"
    return "partial"
