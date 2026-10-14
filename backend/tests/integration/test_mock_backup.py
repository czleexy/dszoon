"""端到端集成测试：真采集引擎 × 假设备。

覆盖的都是"真机上很难稳定复现、但一定会遇到"的场景：
分页（两种分页符位置）、命令被拒绝、GBK + ANSI、提权两种分支、认证失败、
设备卡死、会话中途断连、内容截断、版本去重与变更检测。

之所以能在 CI 里跑，是因为设备是剧本回放的（见 tests/mock_device/server.py）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.collector import BackupStatus, DeviceTarget, backup_device, backup_many
from app.collector.storage import ArtifactStore
from tests.mock_device.server import FIXTURE_ROOT, start_mock

HUAWEI_CONFIG = "huawei/vrp8/display_current-configuration.txt"
H3C_CONFIG = "h3c/comware7/display_current-configuration.txt"
RUIJIE_CONFIG = "ruijie/rgos11/show_running-config.txt"


def expected_text(relative: str) -> str:
    """夹具内容 → 期望的清洗结果（行尾空白被规整、首尾空行被去掉）。"""
    raw = (FIXTURE_ROOT / relative).read_text(encoding="utf-8")
    return "\n".join(line.rstrip() for line in raw.strip("\n").split("\n"))


def make_target(server, vendor: str, **overrides) -> DeviceTarget:
    data: dict = {
        "host": "127.0.0.1",
        "port": server.port,
        "vendor": vendor,
        "username": "admin",
        "password": "admin123",
        "connect_timeout": 5.0,
        "command_timeout": 30.0,
        "idle_timeout": 6.0,
    }
    data.update(overrides)
    return DeviceTarget(**data)


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------
async def test_huawei_happy_path(tmp_path):
    async with start_mock("huawei-vrp8") as server:
        outcome = await backup_device(
            make_target(server, "huawei"), store=ArtifactStore(tmp_path / "archive")
        )

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text(HUAWEI_CONFIG)
    assert outcome.hostname == "HW-CORE-01"          # 从提示符反查得到
    assert outcome.facts["model"] == "S5731-H24T4XC"
    assert outcome.facts["software_version"] == "8.180"
    assert outcome.facts["hostname"] == "HW-CORE-01"
    assert outcome.warnings == []                    # 预处理命令全部成功
    assert outcome.changed is None                   # 首次采集
    assert outcome.stored is not None
    # 下次启动配置是尽力而为的附加产物，取不到也不影响主配置
    assert outcome.startup_config and "sysname HW-CORE-01" in outcome.startup_config


async def test_huawei_paging_stuck_still_gets_full_config(tmp_path):
    """★ 关分页命令被设备拒绝时，必须靠"响应分页提示符"把配置取全。

    这是最容易出大事故的分支：命令报错被忽略 + 分页没处理 = 只备份了第一屏，
    而文件看起来"有内容"，直到需要恢复时才发现是残的。
    """
    async with start_mock("huawei-vrp8-paging-stuck") as server:
        outcome = await backup_device(make_target(server, "huawei"))

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text(HUAWEI_CONFIG)
    # 预处理命令失败必须被明确暴露，而不是静默成功
    assert len(outcome.warnings) == 2
    assert all("screen" in w for w in outcome.warnings)


async def test_huawei_inline_more_token_does_not_corrupt_config():
    """分页符贴在内容行尾时，不能把两行粘起来，也不能凭空多出空行。"""
    async with start_mock("huawei-vrp8-more-inline") as server:
        outcome = await backup_device(make_target(server, "huawei"))

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text(HUAWEI_CONFIG)


async def test_screen_width_confirmation_is_auto_answered():
    """★ 真机回归（VRP5.170 / S5720S 实测回放）。

    `screen-width 512` 会弹 "This command will change the default screen width.
    Continue? [Y/N]:"。不自动回答的话会话一直干等——空闲探活发的是空格，
    设备只认 Y/N——最终报"会话空闲 N 秒无数据"，实测白等 65 秒才失败，
    而且报错完全指不到真正原因。
    """
    async with start_mock("huawei-vrp5-screen-width-confirm") as server:
        outcome = await backup_device(make_target(server, "huawei"))

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text("huawei/vrp5/display_current-configuration.txt")
    assert outcome.warnings == []
    # 型号后面跟着 "Routing Switch" 描述，也必须抓得到
    assert outcome.facts["model"] == "S5720S-28X-SI-AC"
    assert outcome.facts["software_version"] == "5.170"
    assert outcome.hostname == "HW-S5720-01"


async def test_h3c_gbk_with_ansi_noise(tmp_path):
    async with start_mock("h3c-comware7-gbk") as server:
        outcome = await backup_device(
            make_target(server, "h3c"), store=ArtifactStore(tmp_path / "archive")
        )

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text(H3C_CONFIG)
    # 中文描述必须完整（GBK 回退解码生效）
    assert " description 办公区接入" in outcome.running_config
    assert "\x1b" not in outcome.running_config
    # ★ 提示符前面带 ANSI 转义序列时也必须能反查出设备名。
    #   查不出来 → strict_prompt 关闭 → 后续每条命令都会等到超时，
    #   而错误表现只是"会话空闲 N 秒无数据"，完全看不出是提示符匹配的问题。
    assert outcome.hostname == "H3C-CORE-02"
    assert outcome.facts["model"] == "S5560X-30C-EI"
    assert outcome.warnings == []


async def test_ruijie_enable_password_flow():
    async with start_mock("ruijie-rgos11-enable") as server:
        outcome = await backup_device(
            make_target(server, "ruijie", enable_password="RGenable123")
        )

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text(RUIJIE_CONFIG)
    assert outcome.hostname == "RJ-ACCESS-03"
    assert outcome.facts["model"] == "S6220-48XS4QXS"
    # 密码绝不能出现在任何回传文本里
    assert all("RGenable123" not in w for w in outcome.warnings)
    assert "RGenable123" not in (outcome.transcript or "")


async def test_ruijie_without_enable_password_recovers_and_continues():
    """设备没设 enable 密码时：enable 直接成功、不弹提示。

    采集器必须识别"没弹密码提示 = 不需要密码"，resync 后继续，
    而不是把密码当命令发出去（那会在设备上留失败登录记录）。
    """
    async with start_mock("ruijie-rgos11-no-enable-password") as server:
        outcome = await backup_device(
            make_target(server, "ruijie", enable_password="RGenable123")
        )

    assert outcome.status is BackupStatus.SUCCESS, outcome.summary()
    assert outcome.running_config == expected_text(RUIJIE_CONFIG)
    assert any("未出现预期提示符" in w for w in outcome.warnings)
    assert any("已跳过提权步骤" in w for w in outcome.warnings)
    # 被跳过的密码步骤不能泄露密码明文
    assert all("RGenable123" not in w for w in outcome.warnings)


# --------------------------------------------------------------------------
# 失败路径：每种失败必须落到明确的状态上，且可定位
# --------------------------------------------------------------------------
async def test_auth_failure_is_not_retried():
    async with start_mock("huawei-auth-fail") as server:
        outcome = await backup_device(make_target(server, "huawei"), retries=2)

    assert outcome.status is BackupStatus.AUTH_FAILED
    assert outcome.error_code == "auth_failed"
    assert outcome.attempts == 1          # 认证失败重试会锁账号，绝不能重试


async def test_unreachable_device_is_reported_quickly():
    target = DeviceTarget(
        host="127.0.0.1",
        port=1,               # 必然连不上
        vendor="huawei",
        username="admin",
        password="admin123",
        connect_timeout=1.0,
        command_timeout=2.0,
    )
    outcome = await backup_device(target, retries=0)
    assert outcome.status is BackupStatus.UNREACHABLE
    assert outcome.error_code == "unreachable"


async def test_tcp_precheck_skips_session_setup():
    target = DeviceTarget(
        host="127.0.0.1", port=1, vendor="huawei", connect_timeout=1.0
    )
    outcome = await backup_device(target, retries=0, precheck=True)
    assert outcome.status is BackupStatus.UNREACHABLE


async def test_hung_device_times_out_via_idle_probe():
    async with start_mock("huawei-hang") as server:
        target = make_target(server, "huawei", idle_timeout=2.0, command_timeout=6.0)
        outcome = await backup_device(target, retries=0)

    assert outcome.status is BackupStatus.TIMEOUT
    assert outcome.error_code == "timeout"


async def test_session_dropped_mid_output_is_reported():
    async with start_mock("huawei-close-mid") as server:
        outcome = await backup_device(make_target(server, "huawei"), retries=0)

    assert outcome.status is BackupStatus.SESSION_CLOSED
    assert outcome.error_code == "session_closed"


async def test_truncated_config_is_archived_but_not_successful(tmp_path):
    """内容残了：不判成功，但**仍要归档**用于人工排障。"""
    store = ArtifactStore(tmp_path / "archive")
    async with start_mock("huawei-truncated") as server:
        outcome = await backup_device(make_target(server, "huawei"), store=store)

    assert outcome.status is BackupStatus.INTEGRITY_ERROR
    assert outcome.error_code == "integrity_error"
    assert "bad_terminator" in {issue.code for issue in outcome.issues}
    assert outcome.stored is not None
    assert store.latest_sha("HW-TRUNC") == outcome.sha256


async def test_unknown_vendor_is_reported_without_connecting():
    target = DeviceTarget(host="127.0.0.1", port=1, vendor="cisco-nxos")
    outcome = await backup_device(target)
    assert outcome.status is BackupStatus.UNSUPPORTED
    assert outcome.error_code == "unknown_vendor"


# --------------------------------------------------------------------------
# 存储：去重、变更检测、历史
# --------------------------------------------------------------------------
async def test_store_dedup_and_change_detection(tmp_path):
    store = ArtifactStore(tmp_path / "archive")
    async with start_mock("huawei-vrp8") as server:
        target = make_target(server, "huawei")
        first = await backup_device(target, store=store)
        second = await backup_device(target, store=store)

    assert first.status is BackupStatus.SUCCESS
    assert first.changed is None
    assert second.status is BackupStatus.UNCHANGED
    assert second.changed is False
    assert second.stored is not None and second.stored.deduplicated is True
    assert first.sha256 == second.sha256

    history = store.history("HW-CORE-01")
    assert len(history) == 2
    assert history[0]["changed"] is None
    assert history[1]["changed"] is False

    latest = store.load_latest_config("HW-CORE-01")
    assert latest is not None
    assert latest[1] == expected_text(HUAWEI_CONFIG)
    assert store.load_config(second.sha256) == expected_text(HUAWEI_CONFIG)


async def test_session_transcript_is_saved_for_troubleshooting(tmp_path):
    store = ArtifactStore(tmp_path / "archive")
    async with start_mock("huawei-vrp8") as server:
        outcome = await backup_device(make_target(server, "huawei"), store=store)

    assert outcome.stored is not None
    log_path = outcome.stored.session_log_path
    assert log_path is not None and Path(log_path).is_file()
    log = Path(log_path).read_text(encoding="utf-8")
    assert "display current-configuration" in log


# --------------------------------------------------------------------------
# 批量调度
# --------------------------------------------------------------------------
async def test_backup_many_runs_all_devices_and_reports_progress(tmp_path):
    async with (
        start_mock("huawei-vrp8") as huawei,
        start_mock("h3c-comware7-gbk") as h3c,
        start_mock("ruijie-rgos11-enable") as ruijie,
    ):
        targets = [
            make_target(huawei, "huawei"),
            make_target(h3c, "h3c"),
            make_target(ruijie, "ruijie", enable_password="RGenable123"),
        ]
        progress: list[tuple[int, int, str]] = []

        def on_progress(done: int, total: int, outcome) -> None:
            progress.append((done, total, outcome.status.value))

        outcomes = await backup_many(
            targets,
            store=ArtifactStore(tmp_path / "archive"),
            concurrency=3,
            on_progress=on_progress,
        )

    assert len(outcomes) == 3
    assert all(o.ok for o in outcomes), [o.summary() for o in outcomes]
    assert sorted(item[0] for item in progress) == [1, 2, 3]
    assert all(item[1] == 3 for item in progress)


async def test_one_bad_device_does_not_break_the_batch(tmp_path):
    """一台设备失败不能影响整批巡检——这是最基本的要求。"""
    async with (
        start_mock("huawei-vrp8") as good,
        start_mock("huawei-auth-fail") as bad,
    ):
        targets = [
            make_target(good, "huawei"),
            make_target(bad, "huawei"),
        ]
        outcomes = await backup_many(
            targets,
            store=ArtifactStore(tmp_path / "archive"),
            concurrency=2,
            retries=0,
            precheck=False,
        )

    assert [o.status for o in outcomes] == [
        BackupStatus.SUCCESS,
        BackupStatus.AUTH_FAILED,
    ]
    assert outcomes[0].ok and not outcomes[1].ok


async def test_same_device_never_runs_two_sessions_concurrently(tmp_path):
    """同一台设备任何时刻只能有一个会话——设备 VTY 数量有限。"""
    store = ArtifactStore(tmp_path / "archive")
    async with start_mock("huawei-vrp8") as server:
        target = make_target(server, "huawei")
        first, second = await backup_many(
            [target, target], concurrency=4, retries=0, store=store
        )

    # 串行执行的结果：一次采到新内容，一次判定"无变更"
    # ★ 失败时必须自带诊断：这条用例传了 retries=0（就是为了不让重试掩盖串行化问题），
    #   所以机器负载一高，一次瞬时连接失败就会直接表现为 unreachable。
    #   "串行化失效"与"采集期间设备不可达"是两件完全不同的事——前者是产品 bug、
    #   后者是环境抖动——失败输出里必须能直接分辨，否则每次都要重新查一遍。
    #   （串行化本身的实现见 app/collector/engine.py 的 device_locks）
    assert {first.status, second.status} == {
        BackupStatus.SUCCESS,
        BackupStatus.UNCHANGED,
    }, (
        "同设备串行化没生效、或采集期间设备不可达："
        f"first={first.status.value}({first.error_code}: {first.summary()})；"
        f"second={second.status.value}({second.error_code}: {second.summary()})"
    )


@pytest.mark.parametrize(
    ("scenario", "vendor"),
    [("huawei-vrp8", "huawei"), ("h3c-comware7-gbk", "h3c")],
)
async def test_running_config_has_no_echo_or_prompt_leftovers(scenario, vendor):
    """清洗层的底线：正文里不能残留命令回显、提示符、ANSI、分页符。"""
    async with start_mock(scenario) as server:
        outcome = await backup_device(make_target(server, vendor))

    config = outcome.running_config or ""
    assert "display current-configuration" not in config
    assert "---- More ----" not in config
    assert "\x1b" not in config
    assert not config.startswith("<")
