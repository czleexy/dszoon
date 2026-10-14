"""驱动层单测：命令表、提示符识别、归一化、脱敏、facts 抽取、校验规则。

这份测试相当于"驱动契约"：新增厂商驱动必须能通过同样的一组断言，
否则说明它对采集引擎的约定（提示符形状、末行标记、脱敏覆盖）没对齐。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.collector import integrity
from app.collector.errors import UnknownVendor
from app.collector.registry import default_registry
from app.collector.session import CONFIRM_PROMPT_PATTERN

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
def test_builtin_drivers_are_loaded():
    assert default_registry.names() == ["h3c", "huawei", "ruijie"]


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        ("huawei", "huawei"),
        ("HW", "huawei"),
        ("Huawei VRP", "huawei"),
        ("h3c", "h3c"),
        ("comware", "h3c"),
        ("ruijie", "ruijie"),
        ("red-giant", "ruijie"),
    ],
)
def test_vendor_alias_resolution(vendor, expected):
    assert default_registry.get(vendor).name == expected


def test_unknown_vendor_raises_with_helpful_message():
    with pytest.raises(UnknownVendor) as excinfo:
        default_registry.get("cisco-nexus")
    assert "cisco-nexus" in str(excinfo.value)
    assert "huawei" in str(excinfo.value)


def test_driver_metadata_is_complete():
    for driver in default_registry.all():
        assert driver.name
        assert driver.display_name
        assert driver.running_config_cmd()
        assert driver.spec["prompt_patterns"]


# --------------------------------------------------------------------------
# 提示符：严格模式 vs 泛化模式
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("vendor", "samples"),
    [
        (
            "huawei",
            [
                "<HW-CORE-01>",
                "[HW-CORE-01]",
                "[~HW-CORE-01]",
                "[HW-CORE-01-GigabitEthernet0/0/1]",
            ],
        ),
        (
            "h3c",
            [
                "<H3C-CORE-02>",
                "[H3C-CORE-02]",
                "[H3C-CORE-02-Vlan-interface10]",
            ],
        ),
        (
            "ruijie",
            [
                "RJ-ACCESS-03>",
                "RJ-ACCESS-03#",
                "RJ-ACCESS-03(config)#",
                "RJ-ACCESS-03(config-if-GigabitEthernet 0/1)#",
            ],
        ),
    ],
)
def test_generic_prompt_re_compiles_and_matches(vendor, samples):
    """★ 回归测试：泛化提示符正则必须能编译、能匹配。

    早期实现用"匹配整个命名组"的正则去 sub("(?:")，把收尾括号一起吃掉，生成
    ``^<(?:[A-Za-z0-9_.\\-]{1,63}>\\s*$`` 这种非法正则。后果是**每一台设备、
    每一次连接**都在建立会话之前直接 PatternError，而报错只有一句
    "missing ), unterminated subpattern"，根本看不出问题出在驱动里。

    这里用"能编译 + 能匹配"来断言，而不是数括号——正则里 ``\\)`` 和 ``[^)]``
    都含字面量右括号，朴素计数会误报。
    """
    generic = default_registry.get(vendor).prompt_re(None)  # 编译失败即测试失败
    assert "?P<hostname>" not in generic.pattern
    for sample in samples:
        assert generic.match(sample), f"{vendor} 泛化提示符未匹配 {sample!r}"


def test_strict_prompt_rejects_foreign_hostname():
    """★ 这是"采到一半就以为结束"的主要防线。

    泛化正则 ``^\\[\\w+\\]$`` 会被配置正文里的 ``[HQ-DC1-CORE]`` 命中
    （真机的 header banner 里经常有这种裸括号行），一旦命中，读取循环就会
    在配置中途认为"命令跑完了"。用真实设备名替换通配段之后就不会了。
    """
    driver = default_registry.get("huawei")
    generic = driver.prompt_re(None)
    strict = driver.prompt_re("HW-CORE-01")

    assert generic.match("[HQ-DC1-CORE]")
    assert not strict.match("[HQ-DC1-CORE]")

    assert strict.match("<HW-CORE-01>")
    assert strict.match("[~HW-CORE-01]")
    assert strict.match("[HW-CORE-01-GigabitEthernet0/0/1]")
    assert not strict.match("<HW-CORE-02>")


def test_strict_prompt_is_used_only_when_hostname_known():
    driver = default_registry.get("ruijie")
    generic = driver.prompt_re(None)
    strict = driver.prompt_re("RJ-ACCESS-03")

    assert "?P<hostname>" not in generic.pattern
    assert "?P<hostname>" not in strict.pattern

    for sample in ("RJ-ACCESS-03>", "RJ-ACCESS-03#", "RJ-ACCESS-03(config)#"):
        assert strict.match(sample), sample
    # 严格形态换一台设备就绝不匹配；泛化形态仍然可用
    assert not strict.match("RJ-ACCESS-04>")
    assert generic.match("RJ-ACCESS-04>")


@pytest.mark.parametrize(
    ("vendor", "line", "expected"),
    [
        ("huawei", "<HW-CORE-01>", "HW-CORE-01"),
        ("h3c", "<H3C-CORE-02>", "H3C-CORE-02"),
        ("ruijie", "RJ-ACCESS-03>", "RJ-ACCESS-03"),
        ("ruijie", "RJ-ACCESS-03#", "RJ-ACCESS-03"),
        ("ruijie", "RJ-ACCESS-03(config)#", "RJ-ACCESS-03"),
        (
            "ruijie",
            "RJ-ACCESS-03(config-if-GigabitEthernet 0/1)#",
            "RJ-ACCESS-03",
        ),
    ],
)
def test_match_prompt_extracts_hostname(vendor, line, expected):
    assert default_registry.get(vendor).match_prompt(line) == expected


@pytest.mark.parametrize(
    ("vendor", "line"),
    [
        ("huawei", "[HW-CORE-01-GigabitEthernet0/0/1]"),
        ("huawei", "[HW-CORE-01]"),
        ("huawei", "[~HW-CORE-01]"),
        ("h3c", "[H3C-CORE-02-Vlan-interface10]"),
        ("h3c", "[~H3C-CORE-02]"),
    ],
)
def test_match_prompt_refuses_ambiguous_bracket_prompt(vendor, line):
    """★ 方括号提示符一律不用于反查设备名，因为**切点无从判断**。

    ``[H3C-CORE-02-Vlan-interface10]`` 与 ``[H3C-CORE-02]`` 在正则层面同形：
    设备名和视图名都用连字符连接，且都在同一个字符类里，去掉 `(?:-suffix)?`
    也没用——整个字符串本来就能被 `[A-Za-z0-9_.-]{1,63}` 吃掉。

    宁可返回 None 让引擎回退到泛化提示符（可用，只是少了"防正文误命中"的保护），
    也不要猜一个错的设备名：错的设备名会生成一个**永不匹配**的严格正则，
    那台设备的每条命令都会超时。

    （提示符**匹配**用的正则仍然包含方括号形态——那是"命令是否结束"的判断。）
    """
    assert default_registry.get(vendor).match_prompt(line) is None


@pytest.mark.parametrize(
    ("vendor", "line"),
    [
        ("huawei", "sysname HW-CORE-01"),
        ("huawei", "description [UPLINK-TO-DC-SPINE]"),
        ("ruijie", "! Last configuration change at 2024-03-11"),
        ("ruijie", ""),
    ],
)
def test_match_prompt_returns_none_for_non_prompt_lines(vendor, line):
    assert default_registry.get(vendor).match_prompt(line) is None


# --------------------------------------------------------------------------
# 命令表
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("vendor", "running", "startup", "setup"),
    [
        (
            "huawei",
            "display current-configuration",
            "display saved-configuration",
            ("screen-length 0 temporary", "screen-width 512"),
        ),
        (
            "h3c",
            "display current-configuration",
            "display saved-configuration",
            ("screen-length disable", "screen-width 512"),
        ),
        (
            "ruijie",
            "show running-config",
            "show startup-config",
            ("terminal length 0", "terminal width 512"),
        ),
    ],
)
def test_vendor_command_table(vendor, running, startup, setup):
    driver = default_registry.get(vendor)
    assert driver.running_config_cmd() == running
    assert driver.startup_config_cmd() == startup
    assert tuple(driver.session_setup_cmds()) == setup


@pytest.mark.parametrize("vendor", ["huawei", "h3c", "ruijie"])
def test_drivers_do_not_define_save_command(vendor):
    """备份场景绝不能改设备状态：命令表里不允许出现 save / write。"""
    commands = default_registry.get(vendor).spec["commands"]
    assert "save" not in commands


def test_huawei_requires_no_privilege():
    driver = default_registry.get("huawei")
    assert list(driver.privilege_steps()) == []
    assert driver.requires_enable_password() is False


def test_ruijie_privilege_chain_with_password():
    driver = default_registry.get("ruijie")
    assert driver.requires_enable_password() is True
    steps = list(driver.privilege_steps(enable_password="RGenable123"))
    assert [step.command for step in steps] == ["enable", "RGenable123"]
    assert steps[0].expect and steps[0].optional is True
    assert steps[1].secret is True


def test_ruijie_privilege_chain_without_password_skips_secret_step():
    driver = default_registry.get("ruijie")
    steps = list(driver.privilege_steps(enable_password=None))
    assert [step.command for step in steps] == ["enable"]


# --------------------------------------------------------------------------
# 归一化（供 diff 使用）
# --------------------------------------------------------------------------
def test_huawei_normalize_drops_volatile_lines():
    driver = default_registry.get("huawei")
    normalized = driver.normalize(load("huawei/vrp8/display_current-configuration.txt"))
    assert "!Software Version" not in normalized
    assert "!Last configuration was updated" not in normalized
    assert "!Last configuration was saved" not in normalized
    # 配置语义必须保留
    assert "sysname HW-CORE-01" in normalized
    assert "return" in normalized


def test_ruijie_normalize_drops_volatile_lines():
    driver = default_registry.get("ruijie")
    normalized = driver.normalize(load("ruijie/rgos11/show_running-config.txt"))
    assert "Building configuration..." not in normalized
    assert "Current configuration" not in normalized
    assert "Last configuration change" not in normalized
    assert "hostname RJ-ACCESS-03" in normalized
    assert "end" in normalized


def test_normalize_is_idempotent():
    for vendor, fixture in [
        ("huawei", "huawei/vrp8/display_current-configuration.txt"),
        ("h3c", "h3c/comware7/display_current-configuration.txt"),
        ("ruijie", "ruijie/rgos11/show_running-config.txt"),
    ]:
        driver = default_registry.get(vendor)
        once = driver.normalize(load(fixture))
        assert driver.normalize(once) == once


def test_normalized_config_still_passes_integrity_check():
    """归一化只去噪声，不能把结构标记（#/!、return/end）删掉。"""
    for vendor, fixture in [
        ("huawei", "huawei/vrp8/display_current-configuration.txt"),
        ("h3c", "h3c/comware7/display_current-configuration.txt"),
        ("ruijie", "ruijie/rgos11/show_running-config.txt"),
    ]:
        driver = default_registry.get(vendor)
        normalized = driver.normalize(load(fixture))
        issues = integrity.check_config(normalized, driver.config_spec())
        assert not integrity.has_errors(issues), (vendor, issues)


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------
def test_huawei_redact_masks_secrets():
    driver = default_registry.get("huawei")
    redacted = driver.redact(load("huawei/vrp8/display_current-configuration.txt"))
    assert "%^%#Kj8sLm2QpR7dXw%^%#" not in redacted
    assert "%^%#Qm3xT9vLpR7dN4%^%#" not in redacted
    assert "******" in redacted
    # 结构保留：能看到"这里配了密码"，但看不到密码本身
    assert "local-user netadmin password irreversible-cipher" in redacted
    assert "snmp-agent community read cipher" in redacted


def test_h3c_redact_masks_secrets():
    driver = default_registry.get("h3c")
    redacted = driver.redact(load("h3c/comware7/display_current-configuration.txt"))
    assert "%$%$7Xk2Lm9Q%$%$" not in redacted


def test_ruijie_redact_masks_secrets_and_keeps_community_permission():
    driver = default_registry.get("ruijie")
    redacted = driver.redact(load("ruijie/rgos11/show_running-config.txt"))
    assert "$1$abc$Kd93mZq1" not in redacted
    assert "02030F4E5B1C" not in redacted
    assert "snmp-server community public RO" not in redacted
    # community 名字掩掉，但读/写权限保留（运维需要知道这是只读还是读写）
    assert "snmp-server community ****** RO" in redacted


def test_redact_is_idempotent():
    for vendor, fixture in [
        ("huawei", "huawei/vrp8/display_current-configuration.txt"),
        ("ruijie", "ruijie/rgos11/show_running-config.txt"),
    ]:
        driver = default_registry.get(vendor)
        once = driver.redact(load(fixture))
        assert driver.redact(once) == once


# --------------------------------------------------------------------------
# 交互确认（真机踩出来的坑）
# --------------------------------------------------------------------------
def test_drivers_declare_confirm_rules_for_interactive_prompts():
    """★ 真机回归：`screen-width 512` 在 VRP5 上会弹 [Y/N] 确认。

    不自动回答的话，会话一直干等——空闲探活发的是空格，设备只认 Y/N——
    最终报"会话空闲 25s 无数据"（实测白等 65 秒），报错完全指不到真正原因。
    """
    assert CONFIRM_PROMPT_PATTERN  # 会话层提供的通用形态，驱动 YAML 直接复用
    for vendor in ("huawei", "h3c", "ruijie"):
        rules = list(default_registry.get(vendor).confirm_rules())
        assert rules, f"{vendor} 未声明交互确认规则"
        pattern, reply = rules[0]
        assert reply == "Y"
        # 真机原话：Warning: This command will change the default screen width. Continue? [Y/N]:
        assert pattern.search("Continue? [Y/N]: ")
        assert pattern.search("Are you sure to continue? (y/n)")
        assert pattern.search("Save the configuration? [yes/no]:")
        # 普通提示符/配置行不能被误判成确认
        assert not pattern.search("<HW-CORE-01>")
        assert not pattern.search(" description [UPLINK-TO-DC-SPINE]")


def test_setup_leakage_check_flags_exact_command_but_not_lookalike():
    """采集只允许改会话状态。若预处理命令出现在配置里，必须报警。

    但 ``screen-length 0`` 完全可能是管理员自己配的，不能误报。
    """
    real_like = "user-interface vty 0 4\n authentication-mode aaa\n screen-length 0\n#\nreturn"
    setup = ("screen-length 0 temporary", "screen-width 512")
    assert integrity.check_setup_leakage(real_like, setup) == []

    leaked = real_like + "\n screen-width 512\n"
    issues = integrity.check_setup_leakage(leaked, setup)
    assert [issue.code for issue in issues] == ["setup_leaked_into_config"]
    assert issues[0].severity == "warning"  # 不阻断备份，但要让人看见
# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------
def test_huawei_extract_facts():
    driver = default_registry.get("huawei")
    facts = driver.extract_facts(
        load("huawei/vrp8/display_version.txt"),
        load("huawei/vrp8/display_current-configuration.txt"),
    )
    assert facts["model"] == "S5731-H24T4XC"
    assert facts["software_version"] == "8.180"
    assert "VRP" in facts["platform"]
    assert facts["hostname"] == "HW-CORE-01"


def test_huawei_extract_facts_vrp5_with_product_descriptor():
    """★ 真机实测发现的格式差异：VRP5 的型号后面还跟着产品描述。

    真机回显是 ``HUAWEI S5720S-28X-SI-AC Routing Switch uptime is 1 week, ...``，
    而早期正则要求型号紧跟 ``uptime is``，于是这台设备的 facts 里**根本没有 model 键**
    ——备份本身是成功的，所以这个缺失极难被察觉。

    夹具来自真机回显（ESN 已脱敏）。
    """
    driver = default_registry.get("huawei")
    facts = driver.extract_facts(load("huawei/vrp5/display_version.txt"), "")
    assert facts["model"] == "S5720S-28X-SI-AC"
    assert facts["software_version"] == "5.170"


def test_huawei_vrp5_prompt_and_commands_still_apply():
    """VRP5 与 VRP8 共用同一套提示符/命令表（实测确认）。"""
    driver = default_registry.get("huawei")
    assert driver.match_prompt("<HW-S5720-01>") == "HW-S5720-01"
    assert driver.running_config_cmd() == "display current-configuration"
    issues = integrity.check_config(
        load("huawei/vrp5/display_current-configuration.txt"), driver.config_spec()
    )
    assert not integrity.has_errors(issues), issues


def test_h3c_extract_facts():
    driver = default_registry.get("h3c")
    facts = driver.extract_facts(
        load("h3c/comware7/display_version.txt"),
        load("h3c/comware7/display_current-configuration.txt"),
    )
    assert facts["model"] == "S5560X-30C-EI"
    assert facts["software_version"] == "7.1.070"
    assert facts["release"] == "6126P02"
    assert facts["hostname"] == "H3C-CORE-02"


def test_ruijie_extract_facts():
    driver = default_registry.get("ruijie")
    facts = driver.extract_facts(
        load("ruijie/rgos11/show_version.txt"),
        load("ruijie/rgos11/show_running-config.txt"),
    )
    assert facts["model"] == "S6220-48XS4QXS"
    assert facts["software_version"] == "RGOS 11.4(1)B70P2"
    assert facts["hostname"] == "RJ-ACCESS-03"


def test_extract_facts_tolerates_empty_input():
    for driver in default_registry.all():
        assert driver.extract_facts("", "") == {}


# --------------------------------------------------------------------------
# 预留的配置下发接口
# --------------------------------------------------------------------------
async def test_deploy_is_explicitly_unsupported_in_v1():
    from app.collector.errors import DeployNotSupported

    for driver in default_registry.all():
        assert driver.supports_deploy is False
        with pytest.raises(DeployNotSupported):
            await driver.deploy(session=None, config_text="sysname X")  # type: ignore[arg-type]
