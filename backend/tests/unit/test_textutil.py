"""终端清洗层单测。

这一层是"备份内容是否可信"的地基，每条规则都对应一个真机上出现过的现象——
所以这里不是覆盖率装饰，而是回归防线。新增坑位时**先加测试，再加实现**。
"""

from __future__ import annotations

import re

import pytest

from app.collector import textutil

PROMPT = re.compile(r"^<[A-Za-z0-9_.\-]{1,63}>\s*$", re.MULTILINE)
#: 严格形态：设备名是字面量。只有这种形态才敢做"粘连提示符"匹配。
STRICT_PROMPT = re.compile(r"^<HW\-CORE\-01>\s*$", re.MULTILINE)


# --------------------------------------------------------------------------
# ANSI / 退格 / 换行
# --------------------------------------------------------------------------
def test_strip_ansi_removes_cursor_and_color_sequences():
    raw = "\x1b[42D<HUAWEI>\x1b[1;32mdisplay version\x1b[0m\r\n"
    assert textutil.strip_ansi(raw) == "<HUAWEI>display version\r\n"


def test_strip_ansi_removes_osc_sequences():
    raw = "\x1b]0;HUAWEI\x07sysname X"
    assert textutil.strip_ansi(raw) == "sysname X"


def test_apply_backspaces_handles_overwrite():
    # 设备重绘提示符时会用退格擦掉旧字符
    assert textutil.apply_backspaces("abc\x08\x08de") == "ade"
    assert textutil.apply_backspaces("\x08\x08abc") == "abc"


def test_normalize_newlines_unifies_crlf_and_lone_cr():
    assert textutil.normalize_newlines("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_clean_terminal_pipeline_order_keeps_backspace_working():
    # 退格必须在"删除控制字符"之前处理，否则 \x08 被删掉后字符就擦不掉了。
    # 两个退格擦掉 "99"，剩下 "vlan 20"；若顺序反了会得到 "vlan 9920"。
    raw = "vlan 99\x08\x08" + "20\r\n"
    assert textutil.clean_terminal(raw) == "vlan 20\n"


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------
def test_decode_output_prefers_utf8():
    assert textutil.decode_output("办公区接入".encode("utf-8")) == "办公区接入"


def test_decode_output_falls_back_to_gb18030():
    # 华三/锐捷设备常见 GBK：UTF-8 解码失败后必须能回退
    assert textutil.decode_output("办公区接入".encode("gbk")) == "办公区接入"


def test_decode_output_honours_preferred_encoding():
    data = "办公区".encode("gbk")
    assert textutil.decode_output(data, preferred="gbk") == "办公区"


def test_decode_for_matching_survives_truncated_multibyte():
    # 缓冲区正好截断在一个汉字中间时，匹配用解码不能抛异常
    data = "配置".encode("utf-8")[:4]
    text = textutil.decode_for_matching(data)
    assert isinstance(text, str)


# --------------------------------------------------------------------------
# 分页
# --------------------------------------------------------------------------
def test_has_more_token_only_matches_at_end():
    """★ 回归测试。

    早期实现用 ``MORE_TOKEN_RE.search(text)`` 判断"是否还在分页中"，
    结果缓冲区里**历史页**的分页符会让读取循环误判，对已经结束的输出狂发空格。
    """
    assert textutil.has_more_token("vlan 10\r\n  ---- More ----")
    assert textutil.has_more_token("vlan 10\r\n---- More ----\r\n")
    assert not textutil.has_more_token("  ---- More ----\nvlan 20")
    assert not textutil.has_more_token("vlan 10\r\n  ---- More ----\r\nvlan 20\r\nreturn\r\n<SW>")
    assert not textutil.has_more_token("")


def test_strip_more_tokens_own_line_does_not_insert_blank_line():
    text = "vlan 10\n  ---- More ----\nvlan 20"
    assert textutil.strip_more_tokens(text) == "vlan 10\nvlan 20"


def test_strip_more_tokens_inline_keeps_lines_separate():
    text = "vlan 10  ---- More ----\nvlan 20"
    assert textutil.strip_more_tokens(text) == "vlan 10\nvlan 20"


def test_strip_more_tokens_handles_cisco_style():
    assert textutil.strip_more_tokens("line 1\n --More-- \nline 2") == "line 1\nline 2"


# --------------------------------------------------------------------------
# 回显与提示符
# --------------------------------------------------------------------------
def test_drop_echo_removes_prompt_prefixed_command_line():
    text = "<HUAWEI>display current-configuration\n#\nsysname X\n"
    assert textutil.drop_echo(text, "display current-configuration") == "#\nsysname X\n"


def test_drop_echo_is_noop_when_command_absent():
    text = "#\nsysname X\n"
    assert textutil.drop_echo(text, "display current-configuration") == text


def test_strip_trailing_prompt_drops_whole_prompt_line():
    assert textutil.strip_trailing_prompt("#\nreturn\n<HUAWEI>", PROMPT) == "#\nreturn"


def test_strip_trailing_prompt_handles_glued_prompt_with_strict_prompt():
    assert (
        textutil.strip_trailing_prompt(
            "#\nreturn<HW-CORE-01>", STRICT_PROMPT, allow_glued=True
        )
        == "#\nreturn"
    )


def test_strip_trailing_prompt_glue_match_requires_opt_in():
    """粘连匹配必须显式开启。

    泛化提示符下，`sysname X<HUAWEI>` 的结尾形态和真实提示符无法区分，
    默认必须不处理——宁可留下多余文本，也不能误删正文。
    """
    line = "sysname X<HUAWEI>"
    assert textutil.strip_trailing_prompt(line, PROMPT) == line
    assert textutil.strip_trailing_prompt(line, PROMPT, allow_glued=True) == "sysname X"


def test_glued_prompt_does_not_truncate_bracket_config_line():
    """★ 这个用例解释了为什么粘连匹配必须配"严格提示符"才敢用。

    `description [UPLINK-TO-DC-SPINE]` 与提示符 `[HUAWEI]` 形态一致，
    一旦允许粘连匹配，这一行会被截断成 `description`——**静默破坏配置正文**。
    """
    text = "description [UPLINK-TO-DC-SPINE]"
    assert textutil.strip_trailing_prompt(text, PROMPT, allow_glued=True) == text
    assert textutil.trailing_prompt_start(text, PROMPT, allow_glued=True) is None


def test_strip_trailing_prompt_keeps_config_lines():
    assert textutil.strip_trailing_prompt("#\nsysname X\nreturn", PROMPT) == "#\nsysname X\nreturn"


# --------------------------------------------------------------------------
# 组合
# --------------------------------------------------------------------------
def test_clean_command_output_end_to_end():
    raw = (
        b"\x1b[42D<HUAWEI>display current-configuration\r\n"
        b"#\r\n"
        b"!Software Version VRP (R) software, Version 8.180\r\n"
        b"sysname HW-CORE-01\r\n"
        b"  ---- More ----\r\n"
        b"#\r\n"
        b"return\r\n"
        b"<HUAWEI>"
    )
    cleaned = textutil.clean_command_output(
        raw, "display current-configuration", PROMPT
    )
    assert cleaned == (
        "#\n!Software Version VRP (R) software, Version 8.180\nsysname HW-CORE-01\n#\nreturn"
    )


def test_clean_command_output_keeps_meaningful_blank_lines_by_default():
    raw = "cmd\nheader login information \"^\"\n\nHello\n\n\"^\"\nreturn\n<SW>"
    cleaned = textutil.clean_command_output(raw, "cmd", PROMPT)
    assert "\n\nHello\n\n" in cleaned


def test_sha256_text_is_stable():
    assert textutil.sha256_text("abc") == textutil.sha256_text("abc")
    assert textutil.sha256_text("abc") != textutil.sha256_text("abd")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\nb\nc", 3),
        ("", 0),
    ],
)
def test_line_count(text, expected):
    assert textutil.line_count(text) == expected
