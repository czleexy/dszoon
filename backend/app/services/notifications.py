"""通知渠道：邮件 / Webhook / 钉钉 / 企微 / 飞书。

三个设计点：

1. **发送失败绝不能影响备份**。所有外部调用都包在 try 里，失败只写
   ``NotificationLog``，绝不向上抛——备份是主业，通知是附属。
2. **外部依赖留缝**：HTTP 集中在 :func:`_post_json`、SMTP 集中在
   :func:`_send_mail_sync`。测试直接替换这两个函数即可，既不用起 SMTP 服务器，
   也不用打桩网络层。
3. **触发要有节制**：1000 台里 3 台失败时，"每台失败发一条"会把人淹没。
   所以按**任务级事件**聚合发送，并且只有命中渠道配置的事件类型才发。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import smtplib
import ssl
import time
import urllib.parse
from email.message import EmailMessage
from typing import Any, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import CredentialCipher
from ..db.base import utcnow
from ..db.models import (
    BackupRun,
    BackupTask,
    ComplianceResult,
    Device,
    NotificationChannel,
    NotificationLog,
)
from ..errors import Conflict, NotFound, ValidationFailed

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_EVENTS",
    "EVENTS",
    "KINDS",
    "KIND_LABELS",
    "Notifier",
    "create_channel",
    "delete_channel",
    "deliver",
    "get_channel",
    "list_channels",
    "update_channel",
]

KINDS: tuple[str, ...] = ("smtp", "webhook", "dingtalk", "wecom", "feishu")

#: 渠道类型的中文名（前端下拉框直接用它，避免两边各写一份对不上）
KIND_LABELS: dict[str, str] = {
    "smtp": "邮件（SMTP）",
    "webhook": "通用 Webhook",
    "dingtalk": "钉钉机器人",
    "wecom": "企业微信机器人",
    "feishu": "飞书机器人",
}

#: 可订阅的事件（写进渠道配置的 events 字段）
EVENTS: dict[str, str] = {
    "task_failed": "任务整体失败",
    "task_partial": "任务部分失败",
    "task_cancelled": "任务被取消",
    "integrity_error": "采到的配置不可信（截断/折行/命令报错）",
    "significant_change": "有设备配置发生变更",
    "device_failed_streak": "某台设备连续多次失败",
    # P3：合规基线。**默认不订阅**（不加进 DEFAULT_EVENTS）——第一次接入基线时
    # 存量设备大概率一堆不合规，默认订阅会把群炸掉；让人自己决定什么时候开始收。
    "compliance_failed": "有设备配置不合规（基线检查未通过）",
}

DEFAULT_EVENTS: tuple[str, ...] = ("task_failed", "task_partial", "integrity_error")

#: 连续失败多少次算"反复失败"（`device_failed_streak` 事件）
FAILED_STREAK_THRESHOLD = 3

_STATUS_CN = {
    "queued": "排队中",
    "running": "运行中",
    "success": "全部成功",
    "partial": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
}


# --------------------------------------------------------------------------
# 外部依赖（测试替换点）
# --------------------------------------------------------------------------
async def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
    check_business_code: bool = False,
) -> tuple[bool, str]:
    """POST 一个 JSON。返回 ``(是否成功, 说明)``。

    ``check_business_code`` 只对**钉钉/企微/飞书**这类"HTTP 200 + 业务错误码"的平台打开：
    它们的响应体里带 ``errcode``/``code``，只看 HTTP 状态会把"被限流/机器人被移出群"
    记成发送成功。

    ★ 反过来，**通用 webhook 绝不能套这个约定**：响应体格式由对方决定，
    ``{"code": 200}``、``{"code": 1}`` 这类"用 code 表示成功"的写法很常见，
    一律按"非 0 即失败"处理会把**已经投递成功**的通知记成失败——
    运维看到"通知失败：业务错误码 200"，然后开始怀疑整条告警链路。
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        text = response.text[:500]
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}: {text}"
        if not check_business_code:
            return True, "ok"
        # 钉钉/企微/飞书都是 200 + 业务错误码，必须再看一层
        try:
            data = response.json()
        except ValueError:
            # 平台返回了非 JSON（例如网关的 HTML 页面）：没有错误码可看，按成功处理
            return True, text
        code = data.get("errcode", data.get("code", 0))
        if code not in (0, None):
            return False, f"业务错误码 {code}: {data.get('errmsg') or data.get('msg') or text}"
        return True, "ok"


def _send_mail_sync(config: dict[str, Any], password: str | None, subject: str, body: str) -> None:
    """同步发信（在子线程里跑）。失败直接抛异常，由调用方记录。"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.get("from_addr") or config.get("username", "")
    to_addrs = config.get("to_addrs") or []
    if isinstance(to_addrs, str):
        to_addrs = [item.strip() for item in to_addrs.split(",") if item.strip()]
    if not to_addrs:
        raise ValueError("SMTP 渠道未配置收件人 to_addrs")
    message["To"] = ", ".join(to_addrs)
    message.set_content(body)

    host = config.get("host")
    if not host:
        raise ValueError("SMTP 渠道未配置 host")
    port = int(config.get("port") or 25)
    use_tls = bool(config.get("use_tls", True))

    if use_tls:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as server:
            if config.get("username") and password:
                server.login(config["username"], password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            if config.get("starttls"):
                server.starttls(context=ssl.create_default_context())
            if config.get("username") and password:
                server.login(config["username"], password)
            server.send_message(message)


def _dingtalk_signed_url(url: str, secret: str) -> str:
    """按钉钉的加签规则拼 URL。

    规则：``sign = urlencode(base64(hmac_sha256(secret, f"{timestamp}\\n{secret}")))``，
    ``timestamp`` 是毫秒。少一个步骤钉钉就返回 310000。
    """
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}timestamp={timestamp}&sign={sign}"


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
async def list_channels(session: AsyncSession) -> list[NotificationChannel]:
    result = await session.execute(
        select(NotificationChannel).order_by(NotificationChannel.name)
    )
    return list(result.scalars().all())


async def get_channel(session: AsyncSession, channel_id: int) -> NotificationChannel:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None:
        raise NotFound(f"通知渠道不存在：id={channel_id}")
    return channel


def _validate(kind: str, config: dict[str, Any], events: Sequence[str]) -> None:
    if kind not in KINDS:
        raise ValidationFailed(f"渠道类型只能是 {'/'.join(KINDS)}，收到 {kind!r}")
    unknown = [event for event in events if event not in EVENTS]
    if unknown:
        raise ValidationFailed(
            f"未知事件类型 {unknown}；可用：{', '.join(EVENTS)}"
        )
    if kind in ("webhook", "dingtalk", "wecom", "feishu") and not config.get("url"):
        raise ValidationFailed(f"{kind} 渠道必须配置 url")
    if kind == "smtp":
        if not config.get("host"):
            raise ValidationFailed("SMTP 渠道必须配置 host")
        if not (config.get("to_addrs") or []):
            raise ValidationFailed("SMTP 渠道必须配置收件人 to_addrs")


async def create_channel(
    session: AsyncSession,
    payload: dict[str, Any],
    cipher: CredentialCipher,
) -> NotificationChannel:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValidationFailed("渠道名称不能为空")
    if await session.scalar(select(NotificationChannel).where(NotificationChannel.name == name)):
        raise Conflict(f"通知渠道名称已存在：{name}")

    kind = payload.get("kind") or "webhook"
    config = dict(payload.get("config") or {})
    events = list(payload.get("events") or DEFAULT_EVENTS)
    _validate(kind, config, events)

    secret = payload.get("secret") or {}
    channel = NotificationChannel(
        name=name,
        kind=kind,
        enabled=bool(payload.get("enabled", True)),
        config=config,
        events=events,
        min_failures=int(payload.get("min_failures") or 1),
        # 敏感字段单独加密：这样查 config 时不会顺带把口令读出来
        secret_enc=cipher.encrypt_json(secret) if secret else None,
    )
    session.add(channel)
    await session.flush()
    return channel


async def update_channel(
    session: AsyncSession,
    channel: NotificationChannel,
    payload: dict[str, Any],
    cipher: CredentialCipher,
) -> NotificationChannel:
    if payload.get("name") and payload["name"] != channel.name:
        if await session.scalar(
            select(NotificationChannel).where(NotificationChannel.name == payload["name"])
        ):
            raise Conflict(f"通知渠道名称已存在：{payload['name']}")
        channel.name = payload["name"]
    if payload.get("kind"):
        channel.kind = payload["kind"]
    if payload.get("config") is not None:
        new_config = dict(payload["config"])
        # 前端拿到的 URL 是打码过的；如果原样回传，必须保留库里的真实地址，
        # 否则"改个收件人"就会把 webhook 地址写成 https://...&access_token=***
        if is_masked_url(new_config.get("url")):
            new_config["url"] = (channel.config or {}).get("url")
        channel.config = new_config
    if payload.get("events") is not None:
        channel.events = list(payload["events"])
    if payload.get("min_failures") is not None:
        channel.min_failures = max(1, int(payload["min_failures"]))
    if payload.get("enabled") is not None:
        channel.enabled = bool(payload["enabled"])
    if payload.get("secret"):
        # 只覆盖传进来的键，避免"改个 url 把密钥清掉"
        current = channel_secret(channel, cipher)
        current.update(payload["secret"])
        channel.secret_enc = cipher.encrypt_json(current)

    _validate(channel.kind, channel.config, channel.events)
    await session.flush()
    return channel


async def delete_channel(session: AsyncSession, channel: NotificationChannel) -> None:
    await session.delete(channel)
    await session.flush()


def channel_secret(channel: NotificationChannel, cipher: CredentialCipher) -> dict[str, Any]:
    """解密渠道的敏感配置。解密失败返回空字典（宁可少发一次通知也不能炸）。"""
    try:
        return cipher.decrypt_json(channel.secret_enc)
    except Exception:  # noqa: BLE001
        logger.warning("渠道 %s 的密钥解密失败，请检查 secret_key 是否被换过", channel.name)
        return {}


#: API 响应里被替换掉的部分
_MASK = "***"


def mask_url(url: str) -> str:
    """把 URL 里 query 部分的值全部打码，只留主机与路径。

    ★ 机器人/Webhook 的地址**本身就是凭据**：钉钉的
    ``https://oapi.dingtalk.com/robot/send?access_token=xxx``、企微/飞书的
    ``.../hook?key=xxx``——拿到它任何人都能往公司群里发消息。所以 GET 渠道
    不能把原始 URL 原样吐出来。
    """
    if not url:
        return url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    masked = "&".join(
        f"{key}={_MASK}"
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    )
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, masked, ""))


def redact_config(kind: str, config: dict[str, Any] | None) -> dict[str, Any]:
    """给 API 响应用的安全配置（SMTP 的 host/port 不是秘密，URL 里的 token 是）。"""
    data = dict(config or {})
    if isinstance(data.get("url"), str):
        data["url"] = mask_url(data["url"])
    return data


def is_masked_url(url: Any) -> bool:
    """判断客户端回传的是不是打码后的 URL。"""
    return isinstance(url, str) and _MASK in url


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------
async def deliver(
    channel: NotificationChannel,
    cipher: CredentialCipher,
    *,
    event: str,
    subject: str,
    body: str,
    task_id: int | None = None,
) -> tuple[bool, str]:
    """按渠道类型发一条通知。返回 ``(是否成功, 说明)``，**永不抛异常**。"""
    secret = channel_secret(channel, cipher)
    config = channel.config or {}
    try:
        if channel.kind == "smtp":
            await asyncio.to_thread(
                _send_mail_sync, config, secret.get("password"), subject, body
            )
            return True, "ok"

        if channel.kind == "webhook":
            url = config["url"]
            payload = {
                "event": event,
                "subject": subject,
                "text": body,
                "task_id": task_id,
                "source": "dszoon",
            }
            token = secret.get("token")
            if token:
                payload["token"] = token
            # 通用 webhook：响应体格式由对方决定，不看业务错误码（见 _post_json 的说明）
            return await _post_json(url, payload, check_business_code=False)

        if channel.kind == "dingtalk":
            url = config["url"]
            if secret.get("secret"):
                url = _dingtalk_signed_url(url, secret["secret"])
            return await _post_json(
                url,
                {"msgtype": "markdown", "markdown": {"title": subject, "text": body}},
                check_business_code=True,
            )

        if channel.kind == "wecom":
            return await _post_json(
                config["url"],
                {"msgtype": "markdown", "markdown": {"content": body}},
                check_business_code=True,
            )

        if channel.kind == "feishu":
            return await _post_json(
                config["url"],
                {"msg_type": "text", "content": {"text": f"{subject}\n{body}"}},
                check_business_code=True,
            )
    except Exception as exc:  # noqa: BLE001 - 通知失败不能影响主流程
        logger.warning("通知发送失败（渠道 %s）：%s", channel.name, exc)
        return False, f"{type(exc).__name__}: {exc}"
    return False, f"未知渠道类型：{channel.kind}"


# --------------------------------------------------------------------------
# 事件判定与聚合发送
# --------------------------------------------------------------------------
def detect_events(task: BackupTask, runs: Sequence[BackupRun]) -> set[str]:
    """从任务结果里判定命中了哪些事件。"""
    events: set[str] = set()
    if task.status == "failed":
        events.add("task_failed")
    elif task.status == "partial":
        events.add("task_partial")
    elif task.status == "cancelled":
        events.add("task_cancelled")
    if any(run.status == "integrity_error" for run in runs):
        events.add("integrity_error")
    if any(run.changed for run in runs):
        events.add("significant_change")
    return events


def build_message(
    task: BackupTask,
    runs: Sequence[BackupRun],
    device_names: dict[int, str],
    *,
    events: set[str],
    streak_devices: Sequence[str] = (),
    compliance: Sequence[tuple[str, int]] = (),
) -> tuple[str, str]:
    """拼通知主题与正文（纯函数，便于单测）。"""
    trigger_cn = "定时" if task.trigger == "scheduled" else "手工"
    subject = f"[dszoon] 备份任务 #{task.id} {_STATUS_CN.get(task.status, task.status)}"

    lines = [
        f"任务：#{task.id} {task.name or ''}".rstrip(),
        f"触发：{trigger_cn}",
        f"状态：{_STATUS_CN.get(task.status, task.status)}",
        # 「无变更」是「成功」的子集（成功 = 采集成功的台数，含内容未变的），
        # 写成"其中"免得收到通知的人以为两类要相加
        f"结果：共 {task.total} 台，成功 {task.succeeded}（其中无变更 {task.unchanged}），"
        f"失败 {task.failed}",
    ]
    if task.error_message:
        lines.append(f"任务错误：{task.error_message}")

    failures = [run for run in runs if run.status not in ("success", "unchanged")]
    if failures:
        lines.append("")
        lines.append(f"失败设备（{len(failures)} 台）：")
        for run in failures[:20]:
            name = device_names.get(run.device_id, f"#{run.device_id}")
            detail = run.error_message or run.status
            lines.append(f"- {name}：{detail}")
        if len(failures) > 20:
            lines.append(f"- …… 另有 {len(failures) - 20} 台，详见任务详情")

    changed = [run for run in runs if run.changed]
    if changed:
        lines.append("")
        lines.append(f"配置发生变更（{len(changed)} 台）：")
        for run in changed[:20]:
            name = device_names.get(run.device_id, f"#{run.device_id}")
            lines.append(f"- {name}")
        if len(changed) > 20:
            lines.append(f"- …… 另有 {len(changed) - 20} 台")

    if streak_devices:
        lines.append("")
        lines.append(
            f"连续 {FAILED_STREAK_THRESHOLD} 次以上失败的设备：" + "、".join(streak_devices[:20])
        )

    if compliance:
        # 只说"不合规"没有可行动性：把设备名和 high 级条数一起给出，值班的人才知道先看谁
        lines.append("")
        lines.append(f"⚠ 合规基线未通过（{len(compliance)} 台）：")
        for name, high in list(compliance)[:20]:
            suffix = f"（{high} 项高危）" if high else ""
            lines.append(f"- {name}{suffix}")
        if len(compliance) > 20:
            lines.append(f"- …… 另有 {len(compliance) - 20} 台，详见合规报表")
        lines.append("报表：合规基线 → 导出 CSV，或调 GET /api/v1/compliance/report")

    leaked = [
        run
        for run in runs
        if any(issue.get("code") == "setup_leaked_into_config" for issue in (run.issues or []))
    ]
    if leaked:
        lines.append("")
        lines.append("⚠ 以下设备的采集预处理命令疑似写进了配置，请人工确认：")
        for run in leaked[:20]:
            lines.append(f"- {device_names.get(run.device_id, f'#{run.device_id}')}")

    lines.append("")
    lines.append(f"事件：{', '.join(sorted(events)) or '无'}")
    return subject, "\n".join(lines)


class Notifier:
    """把任务结果按渠道配置发出去。"""

    def __init__(self, *, database: Any, cipher: CredentialCipher) -> None:
        self.database = database
        self.cipher = cipher

    async def dispatch_task_result(self, task_id: int) -> int:
        """任务结束后调用。返回实际发送成功的条数。**永不抛异常。**"""
        try:
            return await self._dispatch(task_id)
        except Exception:  # noqa: BLE001
            logger.exception("通知派发失败（task=%s），已忽略以免影响备份", task_id)
            return 0

    async def _dispatch(self, task_id: int) -> int:
        async with self.database.session() as session:
            task = await session.get(BackupTask, task_id)
            if task is None:
                return 0
            runs = list(
                (
                    await session.execute(
                        select(BackupRun).where(BackupRun.task_id == task_id)
                    )
                )
                .scalars()
                .all()
            )
            events = detect_events(task, runs)

            # ★ 所有事件源都要在"没有事件就提前返回"**之前**算完。
            #   这里踩过两次：`device_failed_streak` 漏进正文、`compliance_failed` 干脆
            #   永远算不到——因为"首次采集成功"这种安静任务里 detect_events 返回空集
            #   （首采 run.changed 是 None，按 P1 的设计不算变更），于是在这一行就返回了，
            #   后面的合规判定根本没机会执行。安静任务恰恰是合规检查最该覆盖的场景。
            device_names = await self._device_names(session, [run.device_id for run in runs])
            streak = await self._devices_with_failed_streak(session, runs)
            if streak:
                events.add("device_failed_streak")

            # P3：本次任务**产出的版本**里判为不合规的设备（合规引擎挂在备份收尾路径上）。
            # 时间下界用 created_at：任务行先建、设备后采，所以本次任务写下的合规结果
            # 必然晚于它；第二次采集内容没变时复用同一条结果，checked_at 早于本次
            # created_at，于是不会重复告警。
            compliance = await self._failing_compliance(session, runs, since=task.created_at)
            if compliance:
                events.add("compliance_failed")

            if not events:
                return 0

            channels = list(
                (
                    await session.execute(
                        select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            if not channels:
                return 0

            logger.info(
                "任务 #%s 通知判定：事件=%s，订阅渠道 %s 个",
                task.id,
                sorted(events),
                len(channels),
            )

            sent = 0
            for channel in channels:
                matched = events & set(channel.events or [])
                if not matched:
                    continue
                # 失败数没到阈值就不打扰（例如渠道配置"只在我这边失败 ≥3 台时通知"）
                if task.failed and task.failed < channel.min_failures:
                    continue
                subject, body = build_message(
                    task,
                    runs,
                    device_names,
                    events=matched,
                    streak_devices=streak,
                    compliance=compliance,
                )
                ok, detail = await deliver(
                    channel,
                    self.cipher,
                    event=",".join(sorted(matched)),
                    subject=subject,
                    body=body,
                    task_id=task.id,
                )
                channel.last_sent_at = utcnow()
                channel.last_status = "success" if ok else "failed"
                channel.last_error = None if ok else detail[:500]
                session.add(
                    NotificationLog(
                        channel_id=channel.id,
                        channel_name=channel.name,
                        event=",".join(sorted(matched)),
                        subject=subject,
                        status="success" if ok else "failed",
                        error=None if ok else detail,
                        task_id=task.id,
                    )
                )
                if ok:
                    sent += 1
            await session.commit()
            return sent

    async def send_test(self, channel_id: int) -> tuple[bool, str]:
        """给某个渠道发一条测试消息（界面上的"测试"按钮）。"""
        async with self.database.session() as session:
            channel = await session.get(NotificationChannel, channel_id)
            if channel is None:
                raise NotFound(f"通知渠道不存在：id={channel_id}")
            ok, detail = await deliver(
                channel,
                self.cipher,
                event="test",
                subject="[dszoon] 通知渠道测试",
                body="如果你看到这条消息，说明该渠道配置可用。",
            )
            channel.last_sent_at = utcnow()
            channel.last_status = "success" if ok else "failed"
            channel.last_error = None if ok else detail[:500]
            session.add(
                NotificationLog(
                    channel_id=channel.id,
                    channel_name=channel.name,
                    event="test",
                    subject="通知渠道测试",
                    status="success" if ok else "failed",
                    error=None if ok else detail,
                )
            )
            await session.commit()
            return ok, detail

    @staticmethod
    async def _failing_compliance(
        session: AsyncSession,
        runs: Sequence[BackupRun],
        *,
        since: Any = None,
    ) -> list[tuple[str, int]]:
        """本次任务**新产生的**不合规结果：``[(设备名, high 级条数), ...]``。

        判定条件只有一个：``checked_at >= since``（``since`` = 任务的 ``created_at``）。
        它的语义是"这条合规结果是在本任务开始之后写下的"，正好等于"本次任务新产生的"：

        * 任务行先建、设备后采，所以本次任务写下的结果必然 >= created_at；
        * 第二次采集内容没变 → 复用同一个 snapshot → 合规结果还是上一次那条，
          ``checked_at`` 早于本次任务的 created_at → 不重复告警。

        刻意**不**再加 ``snapshot_id`` 过滤：多一个条件就多一个"某个字段恰好为空、
        条件永不成立"的静默失败面（第一版就栽在时间字段上，第二版又栽在版本过滤上）。
        代价是"两个任务时间上重叠"时可能重复报一次——重复告警的代价远小于永不告警。
        """
        device_ids = list({run.device_id for run in runs})
        if not device_ids or since is None:
            return []
        rows = (
            await session.execute(
                select(ComplianceResult, Device.name)
                .join(Device, Device.id == ComplianceResult.device_id)
                .where(
                    ComplianceResult.device_id.in_(device_ids),
                    ComplianceResult.verdict == "fail",
                    ComplianceResult.checked_at >= since,
                )
                .order_by(ComplianceResult.checked_at.desc())
            )
        ).all()
        logger.info(
            "合规告警判定：设备 %s 台、since=%s、命中不合规结果 %s 条",
            len(device_ids),
            since,
            len(rows),
        )
        # 同一台设备在一次任务里可能有多次评估（多版本），只留最新那条
        seen: dict[int, tuple[str, int]] = {}
        for result, name in rows:
            seen.setdefault(result.device_id, (name or f"#{result.device_id}", result.high_count))
        return list(seen.values())

    @staticmethod
    async def _device_names(session: AsyncSession, device_ids: Sequence[int]) -> dict[int, str]:
        ids = list({int(i) for i in device_ids})
        if not ids:
            return {}
        result = await session.execute(
            select(Device.id, Device.name).where(Device.id.in_(ids))
        )
        return {int(row[0]): str(row[1]) for row in result.all()}

    @staticmethod
    async def _devices_with_failed_streak(
        session: AsyncSession, runs: Sequence[BackupRun]
    ) -> list[str]:
        """挑出"最近连续失败 ≥ FAILED_STREAK_THRESHOLD 次"的设备名。"""
        failed_ids = [
            run.device_id for run in runs if run.status not in ("success", "unchanged")
        ]
        if not failed_ids:
            return []
        names: list[str] = []
        for device_id in dict.fromkeys(failed_ids):
            recent = list(
                (
                    await session.execute(
                        select(BackupRun)
                        .where(BackupRun.device_id == device_id)
                        .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
                        .limit(FAILED_STREAK_THRESHOLD)
                    )
                )
                .scalars()
                .all()
            )
            if len(recent) >= FAILED_STREAK_THRESHOLD and all(
                run.status not in ("success", "unchanged") for run in recent
            ):
                device = await session.get(Device, device_id)
                names.append(device.name if device else f"#{device_id}")
        return names
