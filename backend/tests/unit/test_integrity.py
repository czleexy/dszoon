"""完整性校验单测。

核心命题：**"拿到了内容"不等于"拿到了正确的配置"**。
这里逐条验证每种"内容不可信"的场景都能被识别出来。
"""

from __future__ import annotations

from pathlib import Path

from app.collector import integrity
from app.collector.registry import default_registry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


# --------------------------------------------------------------------------
# 正常配置必须零误报
# --------------------------------------------------------------------------
def test_huawei_fixture_passes():
    spec = default_registry.get("huawei").config_spec()
    issues = integrity.check_config(
        load("huawei/vrp8/display_current-configuration.txt"), spec
    )
    assert not integrity.has_errors(issues), issues


def test_h3c_fixture_passes():
    spec = default_registry.get("h3c").config_spec()
    issues = integrity.check_config(
        load("h3c/comware7/display_current-configuration.txt"), spec
    )
    assert not integrity.has_errors(issues), issues


def test_ruijie_fixture_passes():
    spec = default_registry.get("ruijie").config_spec()
    issues = integrity.check_config(load("ruijie/rgos11/show_running-config.txt"), spec)
    assert not integrity.has_errors(issues), issues


# --------------------------------------------------------------------------
# 截断 / 分页 / 报错
# --------------------------------------------------------------------------
def test_truncated_config_is_rejected():
    """★ 最重要的用例：采到一半的配置绝不能被当成备份成功。"""
    spec = default_registry.get("huawei").config_spec()
    issues = integrity.check_config(
        load("huawei/vrp8/display_current-configuration_truncated.txt"), spec
    )
    assert integrity.has_errors(issues)
    assert "bad_terminator" in codes(issues)


def test_more_residue_is_rejected():
    spec = default_registry.get("huawei").config_spec()
    text = "#\nsysname X\n  ---- More ----\nreturn"
    issues = integrity.check_config(text, spec)
    assert "more_residue" in codes(issues)


def test_ansi_residue_is_rejected():
    spec = default_registry.get("huawei").config_spec()
    text = "#\n\x1b[42Dsysname X\nreturn"
    # ANSI 残留意味着清洗层漏了东西，绝不能放过
    issues = integrity.check_config(text, spec)
    assert "ansi_residue" in codes(issues)


def test_device_error_in_prefix_is_rejected():
    spec = default_registry.get("huawei").config_spec()
    text = (
        "Error: Unrecognized command found at '^' position.\n"
        "#\nsysname X\nreturn"
    )
    issues = integrity.check_config(text, spec)
    assert "device_error" in codes(issues)


def test_device_error_outside_scan_window_is_ignored():
    """配置正文里的 banner 可能包含 "Error:" 之类字样，不能误伤。"""
    spec = default_registry.get("huawei").config_spec()
    body = "\n".join(
        f"#\ninterface GigabitEthernet0/0/{i}\n description link-{i}"
        for i in range(1, 12)
    )
    text = f"#\nsysname X\n{body}\n Error: 这是 banner 里的一行说明\n#\nreturn"
    issues = integrity.check_config(text, spec)
    assert "device_error" not in codes(issues)
    assert not integrity.has_errors(issues), issues


def test_too_short_is_rejected():
    spec = default_registry.get("huawei").config_spec()
    issues = integrity.check_config("#\nreturn", spec)
    assert "too_short" in codes(issues)
    assert "too_short_bytes" in codes(issues)


def test_empty_output_is_rejected():
    assert codes(integrity.check_config("", None)) == {"empty"}
    assert codes(integrity.check_config("   \n  ", None)) == {"empty"}


# --------------------------------------------------------------------------
# 折行启发式
# --------------------------------------------------------------------------
def test_line_wrap_heuristic_warns_but_not_errors():
    spec = integrity.ConfigSpec(min_lines=1, min_bytes=1)
    prefix = "ip route-static 10.10."
    long_line = prefix + "0" * (80 - len(prefix))  # 正好 80 字符
    assert len(long_line) == 80
    text = "\n".join([long_line] * 4 + ["return"])
    issues = integrity.check_config(text, spec)
    assert "possible_line_wrap" in codes(issues)
    assert not integrity.has_errors(issues)  # 只是警告，不阻断归档


def test_line_wrap_heuristic_can_be_disabled():
    spec = integrity.ConfigSpec(min_lines=1, min_bytes=1, detect_line_wrap=False)
    long_line = "x" * 80
    issues = integrity.check_config("\n".join([long_line] * 4 + ["return"]), spec)
    assert "possible_line_wrap" not in codes(issues)


# --------------------------------------------------------------------------
# 会话控制命令的报错检测
# --------------------------------------------------------------------------
def test_check_command_output_detects_rejection():
    issues = integrity.check_command_output(
        "% Unrecognized command found at '^' position."
    )
    assert len(issues) == 1
    assert issues[0].code == "device_error"


def test_check_command_output_ignores_empty_and_normal_output():
    assert integrity.check_command_output("") == []
    assert integrity.check_command_output("\n\n") == []
    # 关分页命令的正常回显只有提示符或被清洗成空串
    assert integrity.check_command_output("") == []
