"""终端回显清洗。

采集回来的东西不是"配置"，而是"终端会话的字节流"：里面混着 ANSI 控制序列、
命令回显、分页提示、退格覆盖、以及 GBK/UTF-8 编码差异。本模块负责把它还原成
干净文本。这是整个项目最容易出错、也最值得写测试的一层。

清洗顺序（顺序有讲究，不要随意调整）::

    strip_ansi -> apply_backspaces -> normalize_newlines -> strip_control_chars
    -> strip_more_tokens -> drop_echo -> strip_trailing_prompt -> rstrip 行尾

为什么退格必须在删除控制字符之前：``apply_backspaces`` 依赖 ``\\x08`` 本身。
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Pattern, Sequence

__all__ = [
    "MORE_TOKEN_RE",
    "apply_backspaces",
    "apply_redaction",
    "clean_command_output",
    "clean_terminal",
    "collapse_blank_lines",
    "decode_for_matching",
    "decode_output",
    "drop_echo",
    "has_more_token",
    "line_count",
    "normalize_newlines",
    "sha256_text",
    "strip_ansi",
    "strip_control_chars",
    "strip_more_tokens",
    "strip_trailing_prompt",
    "trailing_prompt_start",
]

# --------------------------------------------------------------------------
# 终端控制序列
# --------------------------------------------------------------------------
# OSC: ESC ] ... (BEL | ST)
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# CSI: ESC [ 参数 中间字节 终止字节，覆盖 \x1b[42D / \x1b[1;32m / \x1b[K
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# 其余两字符转义：ESC 7 / ESC 8 / ESC ( B / ESC =
_ANSI_ESC = re.compile(r"\x1b[@-Z\\_0-9()#-][0-9A-Za-z]?")
# 残留控制字符（保留 \n \t，\r 已在换行归一阶段处理）
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# --------------------------------------------------------------------------
# 分页提示
# --------------------------------------------------------------------------
# 覆盖：---- More ---- / --More-- / <--- More ---> / ---- More ----
_MORE_BODY = r"(?:<)?-{2,}\s*More\s*-{2,}(?:>)?"
MORE_TOKEN_RE = re.compile(_MORE_BODY, re.IGNORECASE)

# 情况 A：分页符独占一行 —— 连同它前面的换行一起删除，避免在配置中间留下空行
_MORE_OWN_LINE = re.compile(r"\n[ \t]*" + _MORE_BODY + r"[ \t]*", re.IGNORECASE)
# 情况 B：分页符贴在内容行尾（设备用 \r 回到行首继续输出）—— 只删标记本身
_MORE_INLINE = re.compile(r"[ \t]*" + _MORE_BODY + r"[ \t]*", re.IGNORECASE)
# 判定"缓冲区是否停在分页提示上"：必须锚在结尾
_MORE_AT_END = re.compile(_MORE_BODY + r"[ \t]*$", re.IGNORECASE)

#: 提示符允许的尾部空白（有些设备在提示符后补空格）
PROMPT_TAIL_ALLOWANCE = 4

#: 解码回退顺序：先按调用方指定的编码，再 UTF-8，最后 gb18030（华三/锐捷含中文描述常见）
_DECODE_CHAIN: tuple[str, ...] = ("utf-8", "gb18030")


# --------------------------------------------------------------------------
# 基础清洗
# --------------------------------------------------------------------------
def strip_ansi(text: str) -> str:
    """剥掉所有 ANSI/终端控制序列。

    注意：光标回退序列 ``\\x1b[42D`` 会被整体删除。真实设备用它重绘提示符，删除是
    业界通行做法；它不会影响配置正文（配置正文不依赖光标定位）。
    """
    if "\x1b" not in text:
        return text
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_ESC.sub("", text)
    return text


def apply_backspaces(text: str) -> str:
    """处理退格覆盖：``abc\\x08\\x08de`` -> ``ade``。

    设备在回显里用退格擦除字符（提示符重绘、密码位掩码）时会留下这种序列，
    不处理就会污染配置内容。
    """
    if "\x08" not in text:
        return text
    out: list[str] = []
    for ch in text:
        if ch == "\x08":
            if out:
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


def normalize_newlines(text: str) -> str:
    """统一换行符：``\\r\\n`` 与孤立 ``\\r`` 都归一成 ``\\n``。"""
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_control_chars(text: str) -> str:
    """删除剩余不可打印控制字符（保留 ``\\n`` 与 ``\\t``）。"""
    return _CTRL_CHARS.sub("", text)


def clean_terminal(text: str) -> str:
    """终端层清洗的完整流水线（不含业务层处理）。"""
    text = strip_ansi(text)
    text = apply_backspaces(text)
    text = normalize_newlines(text)
    return strip_control_chars(text)


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------
def decode_output(data: bytes, preferred: str | None = None) -> str:
    """按 首选编码 -> UTF-8 -> gb18030 的顺序解码，全失败则替换式解码。

    只应在"输出已经收完"之后调用。
    """
    chain: list[str] = []
    if preferred:
        chain.append(preferred)
    chain.extend(enc for enc in _DECODE_CHAIN if enc != preferred)
    for enc in chain:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def decode_for_matching(data: bytes) -> str:
    """用于**增量匹配**（提示符/分页符）的解码。

    匹配用的正则全是 ASCII（提示符、More 标记），因此这里用 UTF-8 + replace
    永远不抛异常，且 ASCII 部分 100% 保真——哪怕缓冲区正好截断在一个多字节汉字
    中间，也不会因为解码失败而误判成"设备没回提示符"。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 分页符
# --------------------------------------------------------------------------
def has_more_token(text: str) -> bool:
    """缓冲区是否**停在**分页提示上——是的话需要向设备补发一个空格继续翻页。

    ★ 必须锚定在结尾。早期实现写成 ``MORE_TOKEN_RE.search(text)``，结果缓冲区里
    历史页的分页符会让读取循环误判"还在分页中"，从而对已经结束的输出狂发空格。
    这个坑由 ``test_has_more_token_only_matches_at_end`` 守住。
    """
    if not text:
        return False
    return _MORE_AT_END.search(text.rstrip("\r\n \t")[-128:]) is not None


def strip_more_tokens(text: str) -> str:
    """删除分页提示，且不破坏配置行结构。

    两种情形必须分开处理，否则会在配置中间凭空插入空行或把两行粘起来：

    * 独占一行：``vlan 10\\n  ---- More ----\\nvlan 20`` -> ``vlan 10\\nvlan 20``
    * 贴在行尾：``vlan 10  ---- More ----\\nvlan 20`` -> ``vlan 10\\nvlan 20``
    """
    if not MORE_TOKEN_RE.search(text):
        return text
    text = _MORE_OWN_LINE.sub("", text)
    text = _MORE_INLINE.sub("", text)
    return text


# --------------------------------------------------------------------------
# 回显与提示符
# --------------------------------------------------------------------------
def drop_echo(text: str, command: str) -> str:
    """删掉命令回显所在的那一行（含该行的提示符前缀）。

    设备回显形如 ``<HUAWEI>display current-configuration\\r\\n``，提示符在命令前面，
    所以做法是：在头部窗口里定位命令，切掉它以及该行剩余部分。
    """
    if not command:
        return text
    window = text[: len(command) + 120]
    idx = window.find(command)
    if idx == -1:
        return text
    rest = text[idx + len(command):]
    newline = rest.find("\n")
    return rest[newline + 1:] if newline != -1 else ""


def strip_trailing_prompt(
    text: str, prompt_re: Pattern[str], *, allow_glued: bool = False
) -> str:
    """删掉末尾的提示符行（可选支持"提示符与正文粘在同一行"）。

    :param allow_glued: 是否允许识别 ``return<HUAWEI>`` 这种粘连形态。
        **只在提示符正则是"字面设备名"时才应开启**——泛化正则（如
        ``^\\[\\w+\\]$``）会把正文里的 ``description [UPLINK-TO-DC-SPINE]``
        当成提示符，把这一行截断成 ``description``。宁可留下多余的提示符文本，
        也不能动配置正文。
    """
    if not text:
        return text
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    last = lines.pop()
    start = trailing_prompt_start(last, prompt_re, allow_glued=allow_glued)
    if start is None:
        # 不是提示符行，原样保留
        lines.append(last)
    elif start > 0:
        # 提示符粘在正文行尾：return<HUAWEI>
        head = last[:start].rstrip()
        if head:
            lines.append(head)
    # else: 整行就是提示符，直接丢弃
    return "\n".join(lines)


#: 粘连形态下向前回溯扫描的最大长度（提示符不会比这更长）
GLUED_PROMPT_SCAN = 128


def trailing_prompt_start(
    line: str, prompt_re: Pattern[str], *, allow_glued: bool = False
) -> int | None:
    """返回末尾提示符在 ``line`` 中的起始位置；没有则返回 None。

    两步走：

    1. 常规形态：提示符自己占一整行（``^...$`` 锚定正则直接匹配，允许尾部空白）。
       **不要求它一定在行首**——``<HUAWEI>`` 前面可能有设备回显残留。
    2. 粘连形态（需 ``allow_glued``）：设备没在提示符前换行，于是出现了
       ``return<HUAWEI>``。锚定正则的 ``^`` 永远匹配不到这种位置，所以这里给
       候选片段前面补一个换行符，再用**同一个**锚定正则去匹配——等价于"去掉行首锚点"
       但不会破坏分组结构（拆 ``|`` 去锚点的做法遇到含 ``|`` 的分组就会出错）。
    """
    last_match = None
    for m in prompt_re.finditer(line):
        last_match = m
    if last_match is not None and len(line) - last_match.end() <= PROMPT_TAIL_ALLOWANCE:
        return last_match.start()

    if not allow_glued:
        return None

    stripped = line.rstrip()
    # 廉价过滤：各家提示符都以 > 或 # 结尾，先挡掉绝大多数正文行
    if not stripped or stripped[-1] not in ">#":
        return None

    lower_bound = max(1, len(stripped) - GLUED_PROMPT_SCAN)
    for start in range(len(stripped) - 1, lower_bound - 1, -1):
        candidate = "\n" + stripped[start:]
        m = prompt_re.search(candidate)
        if m is not None and m.start() == 1 and m.end() == len(candidate):
            return start
    return None


# --------------------------------------------------------------------------
# 组合清洗
# --------------------------------------------------------------------------
def clean_command_output(
    raw: bytes | str,
    command: str,
    prompt_re: Pattern[str],
    *,
    collapse_blank: bool = False,
    rstrip_lines: bool = True,
    preferred_encoding: str | None = None,
    allow_glued_prompt: bool = False,
) -> str:
    """把一次命令交互的原始回显变成干净正文。

    :param collapse_blank: 默认 False —— **配置文件里的空行可能有意义**
        （证书块、多行 banner），不要擅自压缩。
    :param preferred_encoding: 设备已知编码（部分华三/锐捷设备为 GBK）。
    :param allow_glued_prompt: 是否允许把 ``return<HUAWEI>`` 里的提示符删掉。
        只在提示符正则是字面设备名时开启（见 :func:`strip_trailing_prompt`）。
    """
    text = (
        decode_output(raw, preferred_encoding)
        if isinstance(raw, (bytes, bytearray))
        else raw
    )
    text = clean_terminal(text)
    text = strip_more_tokens(text)
    text = drop_echo(text, command)
    text = strip_trailing_prompt(text, prompt_re, allow_glued=allow_glued_prompt)
    text = strip_more_tokens(text)  # 提示符删除后可能又暴露出分页残留
    if rstrip_lines:
        text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = text.strip("\n")
    if collapse_blank:
        text = collapse_blank_lines(text)
    return text


def collapse_blank_lines(text: str, max_blank: int = 1) -> str:
    """把连续空行压缩到 ``max_blank`` 行。"""
    out: list[str] = []
    blanks = 0
    for line in text.split("\n"):
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= max_blank:
                out.append(line)
    return "\n".join(out)


def line_count(text: str) -> int:
    return 0 if not text else len(text.split("\n"))


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------
def apply_redaction(
    text: str, rules: Sequence[tuple[Pattern[str], str]]
) -> tuple[str, int]:
    """按 (正则, 替换模板) 列表脱敏，返回 (新文本, 命中次数)。"""
    hits = 0
    for pattern, replacement in rules:
        text, n = pattern.subn(replacement, text)
        hits += n
    return text, hits


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_lines(text: str) -> Iterable[str]:
    return iter(text.split("\n"))
