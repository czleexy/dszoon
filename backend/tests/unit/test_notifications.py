"""通知渠道单测：事件判定、消息拼装、各渠道的请求体形状、钉钉加签。

外部依赖（HTTP、SMTP）集中在两个函数里，测试直接替换它们——既不用起 SMTP
服务器，也不用打桩网络层。

其中**机器人请求体形状**值得钉死：钉钉/企微/飞书三家的 JSON 结构完全不同，
写错了对方只会返回一个错误码，本地完全看不出来。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

import pytest

from app.core.crypto import CredentialCipher, generate_key
from app.db.models import BackupRun, BackupTask, NotificationChannel
from app.services import notifications
from app.services.notifications import (
    build_message,
    detect_events,
    deliver,
)
from app.services.notifications import _dingtalk_signed_url


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


def make_task(**overrides) -> BackupTask:
    data = {
        "id": 7,
        "name": "夜间巡检",
        "trigger": "scheduled",
        "status": "success",
        "total": 3,
        "succeeded": 3,
        "failed": 0,
        "unchanged": 0,
    }
    data.update(overrides)
    return BackupTask(**data)


def make_run(device_id: int, **overrides) -> BackupRun:
    data = {
        "device_id": device_id,
        "status": "success",
        "changed": False,
        "error_message": None,
        "issues": [],
    }
    data.update(overrides)
    return BackupRun(**data)


def make_channel(kind: str, *, config=None, secret=None, cipher=None, events=None) -> NotificationChannel:
    channel = NotificationChannel(
        id=1,
        name=f"test-{kind}",
        kind=kind,
        enabled=True,
        config=config or {},
        events=events or list(notifications.DEFAULT_EVENTS),
        min_failures=1,
    )
    if secret and cipher is not None:
        channel.secret_enc = cipher.encrypt_json(secret)
    return channel


# --------------------------------------------------------------------------
# 事件判定
# --------------------------------------------------------------------------
def test_detect_events_success_is_quiet():
    """全部成功时不该产生任何事件——否则通知会变成噪声。"""
    task = make_task(status="success")
    assert detect_events(task, [make_run(1), make_run(2)]) == set()


def test_detect_events_failed_and_partial():
    assert "task_failed" in detect_events(make_task(status="failed", failed=3), [])
    assert "task_partial" in detect_events(make_task(status="partial", failed=1), [])
    assert "task_cancelled" in detect_events(make_task(status="cancelled"), [])


def test_detect_events_integrity_error_and_change():
    runs = [
        make_run(1, status="integrity_error"),
        make_run(2, changed=True),
    ]
    events = detect_events(make_task(), runs)
    assert {"integrity_error", "significant_change"} <= events


def test_detect_events_ignores_unchanged_runs():
    """无变更不算"重要变更"——否则每天巡检都会发一条无意义的变更通知。"""
    events = detect_events(make_task(), [make_run(1, changed=False), make_run(2, changed=None)])
    assert "significant_change" not in events


# --------------------------------------------------------------------------
# 消息拼装
# --------------------------------------------------------------------------
def test_build_message_lists_failures_with_reason():
    task = make_task(status="partial", total=3, succeeded=2, failed=1)
    runs = [
        make_run(1),
        make_run(2),
        make_run(3, status="auth_failed", error_message="认证失败"),
    ]
    subject, body = build_message(
        task, runs, {1: "核心交换机", 2: "接入交换机", 3: "坏设备"}, events={"task_partial"}
    )
    assert "#7" in subject
    assert "部分失败" in subject
    assert "坏设备" in body
    assert "认证失败" in body
    assert "定时" in body  # 触发方式要能看出来


def test_build_message_lists_changed_devices():
    task = make_task(total=2, succeeded=2)
    runs = [make_run(1, changed=True), make_run(2)]
    _, body = build_message(task, runs, {1: "A", 2: "B"}, events={"significant_change"})
    assert "配置发生变更" in body
    assert "- A" in body
    assert "- B" not in body.replace("- A", ""), "未变更的设备不该出现在变更清单里"


def test_build_message_flags_leaked_setup_command():
    """预处理命令写进配置是高危信号，通知里必须显著提示。"""
    task = make_task(status="partial", total=1, failed=1)
    runs = [
        make_run(
            1,
            status="integrity_error",
            issues=[{"code": "setup_leaked_into_config", "message": "疑似写入配置"}],
        )
    ]
    _, body = build_message(task, runs, {1: "A"}, events={"integrity_error"})
    assert "预处理命令疑似写进了配置" in body


def test_build_message_mentions_failed_streak():
    task = make_task(status="partial", failed=1)
    runs = [make_run(1, status="unreachable", error_message="不可达")]
    _, body = build_message(
        task, runs, {1: "A"}, events={"task_partial"}, streak_devices=["A"]
    )
    assert "连续" in body and "A" in body


# --------------------------------------------------------------------------
# 钉钉加签
# --------------------------------------------------------------------------
def test_dingtalk_sign_follows_documented_algorithm():
    """加签规则：sign = urlencode(base64(hmac_sha256(secret, f"{ts}\\n{secret}")))）。

    少一个步骤钉钉只会回一个 310000，本地完全看不出问题，所以这里按规则重算一遍比对。
    """
    url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    secret = "SEC0123456789"
    signed = _dingtalk_signed_url(url, secret)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(signed).query)
    assert "timestamp" in query and "sign" in query
    assert query["access_token"] == ["abc"], "原有查询参数不能丢"

    timestamp = query["timestamp"][0]
    expected = base64.b64encode(
        hmac.new(
            secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256
        ).digest()
    ).decode()
    assert query["sign"][0] == expected


def test_dingtalk_sign_uses_ampersand_when_url_has_query():
    signed = _dingtalk_signed_url("https://x/send?access_token=t", "s")
    assert "?access_token=t&timestamp=" in signed


# --------------------------------------------------------------------------
# 各渠道的请求体形状
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "expected_keys", "body_path", "subject_path"),
    [
        # 机器人平台的报文形状是固定的，多一个键就是写错了；正文/主题各自落在哪也要钉死——
        # 钉钉把**标题**放在 markdown.title、**正文**放在 markdown.text，两者容易记混
        # （第一版测试就混了：断言"正文"出现在 title 里）。
        ("dingtalk", {"msgtype", "markdown"}, ("markdown", "text"), ("markdown", "title")),
        ("wecom", {"msgtype", "markdown"}, ("markdown", "content"), None),
        ("feishu", {"msg_type", "content"}, ("content", "text"), None),
        # 通用 webhook 是给自研接收端用的：除约定字段外还带 task_id/source（见 README）
        (
            "webhook",
            {"event", "subject", "text", "task_id", "source"},
            ("text",),
            ("subject",),
        ),
    ],
)
async def test_deliver_payload_shape(
    monkeypatch, cipher, kind, expected_keys, body_path, subject_path
):
    captured: dict = {}

    async def fake_post(url, payload, **_kwargs):
        captured["url"] = url
        captured["payload"] = payload
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    channel = make_channel(kind, config={"url": "https://example.invalid/hook"})
    ok, detail = await deliver(channel, cipher, event="task_failed", subject="主题", body="正文")

    assert ok and detail == "ok"
    payload = captured["payload"]
    assert expected_keys <= set(payload), f"{kind} 少了必需字段：{expected_keys - set(payload)}"
    if kind != "webhook":
        assert set(payload) == expected_keys, "机器人平台的报文形状是固定的，不该多出字段"

    def dig(path: tuple[str, ...]):
        value = payload
        for key in path:
            assert key in value, f"{kind} 的报文里没有 {key}（实际：{value}）"
            value = value[key]
        return value

    # 正文必须真的进了报文，而且落在各平台约定的字段里
    assert "正文" in str(dig(body_path))
    if subject_path:
        assert dig(subject_path) == "主题"
    # 飞书是把"主题+正文"拼成一个 text 字段发的，主题也该在里面
    if kind == "feishu":
        assert "主题" in str(dig(body_path))


async def test_deliver_dingtalk_appends_signature_when_secret_present(monkeypatch, cipher):
    captured: dict = {}

    async def fake_post(url, payload, **_kwargs):
        captured["url"] = url
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)
    channel = make_channel(
        "dingtalk",
        config={"url": "https://oapi.dingtalk.com/robot/send?access_token=t"},
        secret={"secret": "SEC1"},
        cipher=cipher,
    )
    await deliver(channel, cipher, event="task_failed", subject="s", body="b")
    assert "timestamp=" in captured["url"] and "sign=" in captured["url"]


async def test_deliver_smtp_uses_decrypted_password(monkeypatch, cipher):
    captured: dict = {}

    def fake_mail(config, password, subject, body):
        captured.update(config=config, password=password, subject=subject, body=body)

    monkeypatch.setattr(notifications, "_send_mail_sync", fake_mail)
    channel = make_channel(
        "smtp",
        config={"host": "smtp.example.com", "port": 465, "to_addrs": ["ops@example.com"]},
        secret={"password": "s3cret"},
        cipher=cipher,
    )
    ok, _ = await deliver(channel, cipher, event="task_failed", subject="主题", body="正文")
    assert ok
    assert captured["password"] == "s3cret", "密码必须从加密字段解出来再传给发信函数"
    assert captured["subject"] == "主题"


async def test_deliver_never_raises_on_transport_failure(monkeypatch, cipher):
    """★ 通知失败绝不能向上抛——它是在备份任务收尾路径上调用的。"""

    async def boom(url, payload, **_kwargs):
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(notifications, "_post_json", boom)
    channel = make_channel("webhook", config={"url": "https://example.invalid/x"})
    ok, detail = await deliver(channel, cipher, event="task_failed", subject="s", body="b")
    assert ok is False
    assert "网络不可达" in detail


async def test_deliver_unknown_kind_is_reported(cipher):
    channel = make_channel("webhook", config={"url": "https://example.invalid/x"})
    channel.kind = "carrier-pigeon"
    ok, detail = await deliver(channel, cipher, event="e", subject="s", body="b")
    assert ok is False and "未知渠道类型" in detail


def test_channel_secret_returns_empty_when_key_changed(cipher):
    """换了 secret_key 之后密钥解不开：宁可少发一次通知，也不能让异常冒出去。"""
    channel = NotificationChannel(
        id=1, name="c", kind="webhook", enabled=True, config={}, events=[],
        secret_enc=CredentialCipher(generate_key()).encrypt_json({"token": "x"}),
    )
    assert notifications.channel_secret(channel, cipher) == {}


# --------------------------------------------------------------------------
# 共用的 HTTP 发送路径（5 种 webhook 类渠道都走它）
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.text = body

    def json(self):
        import json

        return json.loads(self.text)


def patch_http(monkeypatch, *, status_code: int = 200, body: str = "{}"):
    """把 httpx.AsyncClient 换成一个总是返回固定响应的替身。"""
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, json=None):
            calls.append({"url": url, "json": json})
            return FakeResponse(status_code, body)

    monkeypatch.setattr(notifications.httpx, "AsyncClient", FakeClient)
    return calls


async def test_post_json_generic_webhook_does_not_interpret_code(monkeypatch):
    """★ 通用 webhook 的响应体由对方决定：``{"code": 200}`` 是常见写法，不能判失败。

    套用钉钉/企微/飞书的"非 0 即失败"约定，会把**已经投递成功**的通知记成失败，
    运维随后会去怀疑整条告警链路（这是最难查的一类"假故障"）。
    """
    patch_http(monkeypatch, body='{"code": 200, "message": "ok"}')
    ok, detail = await notifications._post_json("https://example.invalid/hook", {}, check_business_code=False)
    assert ok is True and detail == "ok"

    patch_http(monkeypatch, body='{"code": 1, "data": "accepted"}')
    ok, _ = await notifications._post_json("https://example.invalid/hook", {}, check_business_code=False)
    assert ok is True, "code=1 表示成功的自研接收端也应当算成功"


async def test_post_json_robot_platforms_check_business_code(monkeypatch):
    """钉钉/企微/飞书是 200 + 业务错误码：不限流、被踢出群都必须报失败。"""
    patch_http(
        monkeypatch,
        body='{"errcode": 310000, "errmsg": "keywords not in content"}',
    )
    ok, detail = await notifications._post_json("https://example.invalid", {}, check_business_code=True)
    assert ok is False
    assert "310000" in detail and "keywords" in detail, detail

    patch_http(monkeypatch, body='{"errcode": 0, "errmsg": "ok"}')
    ok, detail = await notifications._post_json("https://example.invalid", {}, check_business_code=True)
    assert ok is True and detail == "ok"

    # 成功的写法也见过 "code": 0（飞书新版）
    patch_http(monkeypatch, body='{"code": 0, "msg": "success"}')
    ok, _ = await notifications._post_json("https://example.invalid", {}, check_business_code=True)
    assert ok is True


async def test_post_json_http_error_is_failure_regardless_of_flag(monkeypatch):
    patch_http(monkeypatch, status_code=500, body="内部错误")
    for check in (False, True):
        ok, detail = await notifications._post_json(
            "https://example.invalid", {}, check_business_code=check
        )
        assert ok is False
        assert "HTTP 500" in detail and "内部错误" in detail


async def test_deliver_passes_business_code_flag_per_kind(monkeypatch, cipher):
    """两种约定必须按渠道类型分开：机器人查错误码、通用 webhook 不查。"""
    seen: dict[str, object] = {}

    async def fake_post(url, payload, **kwargs):
        seen.update(kwargs)
        return True, "ok"

    monkeypatch.setattr(notifications, "_post_json", fake_post)

    await deliver(
        make_channel("dingtalk", config={"url": "https://x/y"}), cipher,
        event="task_failed", subject="s", body="b",
    )
    assert seen.get("check_business_code") is True

    await deliver(
        make_channel("webhook", config={"url": "https://x/y"}), cipher,
        event="task_failed", subject="s", body="b",
    )
    assert seen.get("check_business_code") is False


# --------------------------------------------------------------------------
# 邮件发送的传输细节
# --------------------------------------------------------------------------
class FakeSMTP:
    """最小的 smtplib 替身：只记录调用，不做任何网络动作。

    为什么要替身而不是打桩 `_send_mail_sync`：上面那条测试只证明了"密码解出来了"，
    而真正会踩的坑在这里——``use_tls`` 与 ``starttls`` 两个开关怎么映射到
    ``SMTP_SSL`` / ``SMTP().starttls()``。企业邮箱常见组合是"465 走 SSL、
    587 走 STARTTLS"，映射错了对方直接拒连（表现为 SSL 握手错误），
    而本地一切正常。
    """

    instances: list["FakeSMTP"] = []
    #: 其中通过 ``SMTP_SSL`` 建立的那些（子类把 is_ssl 置 True）
    ssl_instances: list["FakeSMTP"] = []
    is_ssl = False

    def __init__(self, host, port, **kwargs) -> None:
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)
        if self.is_ssl:
            FakeSMTP.ssl_instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def starttls(self, **kwargs: object) -> None:
        self.calls.append(("starttls",))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))

    def send_message(self, message) -> None:
        self.calls.append(("send", message))

    # -- 便于断言 --
    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]

    def sent(self):
        return next(call[1] for call in self.calls if call[0] == "send")


@pytest.fixture
def smtp(monkeypatch) -> type[FakeSMTP]:
    """把 ``SMTP`` 与 ``SMTP_SSL`` 换成两个**可区分**的替身。

    为什么要分开：只把两个名字指向同一个类的话，"走了明文的 SMTP 分支"只能靠
    "调用顺序里有没有 ehlo"间接推断。分开之后可以直白地断言
    "SMTP_SSL 一次都没被构造"——这正是 587/STARTTLS 场景最容易写错的地方。
    """

    class FakeSMTPSSL(FakeSMTP):
        is_ssl = True

    FakeSMTP.instances = []
    FakeSMTP.ssl_instances = []
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def ssl_connections(smtp: type[FakeSMTP]) -> int:
    """走 SSL 建立的连接数（见 smtp 夹具的说明）。"""
    return len(smtp.ssl_instances)


def test_mail_defaults_to_ssl_and_fills_headers(smtp):
    """不给 `use_tls` 时按 SSL 连接（与历史配置兼容），收件人/发件人/主题要正确。"""
    notifications._send_mail_sync(
        {
            "host": "smtp.example.com",
            "port": 465,
            "username": "noreply@example.com",
            "from_addr": "备份系统 <noreply@example.com>",
            "to_addrs": ["ops@example.com", "net@example.com"],
        },
        "s3cret",
        "备份失败",
        "正文",
    )
    assert len(smtp.instances) == 1, "只应建立一个连接"
    assert ssl_connections(smtp) == 1, "默认应当走 SMTP_SSL"
    server = smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 465)
    assert server.kinds() == ["login", "send"], "SSL 模式不需要 ehlo/starttls"
    assert ("login", "noreply@example.com", "s3cret") in server.calls

    message = server.sent()
    assert message["To"] == "ops@example.com, net@example.com"
    assert message["From"] == "备份系统 <noreply@example.com>"
    assert message["Subject"] == "备份失败"
    assert "正文" in message.get_content()


def test_mail_starttls_mode_does_not_use_ssl(smtp):
    """★ 587/25 的企业邮箱：必须走明文连接 + STARTTLS，不能走 SMTP_SSL。

    这正是前端"加密方式"三选一里要表达的语义；映射错了客户只能看到
    "SSL 握手失败"，而我们这边完全无法复现。
    """
    notifications._send_mail_sync(
        {
            "host": "smtp.example.com",
            "port": 587,
            "username": "noreply@example.com",
            "to_addrs": "ops@example.com",  # 逗号分隔的字符串也要能处理
            "use_tls": False,
            "starttls": True,
        },
        "s3cret",
        "主题",
        "正文",
    )
    server = smtp.instances[0]
    assert ssl_connections(smtp) == 0, "★ 587 端口绝不能走 SMTP_SSL（客户只会看到 SSL 握手失败）"
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.kinds() == ["ehlo", "starttls", "login", "send"], (
        "顺序必须是 ehlo → starttls → login → send：先登录再升级 TLS 会把口令裸奔"
    )
    assert server.sent()["To"] == "ops@example.com"


def test_mail_skips_login_without_credentials(smtp):
    """内网中继不需要认证：没配用户名时不能去 login（否则报 5xx 反而发不出去）。"""
    notifications._send_mail_sync(
        {"host": "relay.intra", "port": 25, "use_tls": False, "to_addrs": ["ops@intra"]},
        None,
        "主题",
        "正文",
    )
    server = smtp.instances[0]
    assert "login" not in server.kinds()
    # 没配 from_addr/username 时不该凭空造发件人（空值即可，具体形态交给邮件库）
    assert not server.sent()["From"]


def test_mail_without_recipients_is_an_error(smtp):
    """没有收件人是配置错误：必须抛异常（由 deliver 兜住并写进发送记录）。"""
    with pytest.raises(ValueError, match="to_addrs"):
        notifications._send_mail_sync({"host": "smtp.example.com", "to_addrs": []}, None, "s", "b")
    assert smtp.instances == [], "配置不完整时不该去连服务器"
