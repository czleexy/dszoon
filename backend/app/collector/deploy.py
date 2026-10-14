"""配置下发的**前置预检**（P4-A 的纯逻辑部分，不碰设备、不发任何命令）。

P4 与 P0~P3 的风险等级不同：前三阶段只读设备，最坏是"没采到"；下发要往生产设备写配置，
最坏是**把网络改坏**。所以下发链路的每一步都要有"宁可拒绝执行"的检查，而这个模块是
第一道闸门——它决定"这组命令到底允不允许发出去"。

三条刻意的设计：

1. **默认拒绝危险命令，而不是报 warning**。`save`/`reboot`/`reset`/`format`/`erase` 这类
   命令一旦发出去就无法撤回，哪怕只出现一行也直接判为不可下发——这类判断不允许
   有"误判容忍度"。
2. **预检结果绑定内容哈希**（``DeployPlan.content_hash``）。审批是针对**某个具体内容**的：
   没有哈希，审批完再改一行，就成了"批的是 A、发的是 B"。变更单存下这个哈希并与
   实际下发的字节比对，是唯一能防住这件事的办法。
3. **预检只做"结构性"判断**（危险命令、会话命令泄漏、错误回显残留、行长与行数上限），
   **不猜厂商语法**。离线判断"这条命令设备认不认"是不可能的；写一个看着聪明的
   语法猜测器，只会给出大量假阳性和假阴性，把真正的检查淹掉。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Sequence

from .integrity import Issue

__all__ = [
    "DANGEROUS_PATTERNS",
    "DeployPlan",
    "SESSION_SETUP_PATTERNS",
    "check_lines",
    "plan_deploy",
]

#: 一旦下发就无法撤回、或会中断管理的命令：**出现即拒绝**，不做例外。
#:
#: 注意 ``save``：下发场景下"保存当前配置"会把**半成品配置**写进启动配置，
#: 设备重启后直接进入故障状态——比不保存危险得多，所以同样拒绝。
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^\s*(?:save|write\s+memory|copy\s+run(?:ning)?-?config\s+start(?:up)?-?config)\b", "保存配置"),
    (r"^\s*(?:reboot|reload|restart)\b", "重启设备"),
    (r"^\s*(?:reset|format)\b", "复位/格式化"),
    (r"^\s*(?:erase|delete)\s+(?:startup-config|flash:|/force|nvram:)", "删除启动配置/文件"),
    (r"^\s*undo\s+startup\s+saved-configuration", "删除启动配置"),
    (r"^\s*(?:factory-reset|restore\s+factory)\b", "恢复出厂"),
    (r"^\s*(?:shutdown)\s*$", "整机/全局 shutdown（缺少接口上下文时语义不明）"),
    (r"^\s*(?:undo|no)\s+ip\s+route-static\s+0\.0\.0\.0", "删除默认路由（最容易导致失联）"),
)

#: 会话预处理命令（关分页/关折行/提权）。它们**只应存在于采集会话里**，
#: 一旦写进配置就是持久化改动——采集工具越界改设备，这是同一条红线。
SESSION_SETUP_PATTERNS: tuple[str, ...] = (
    r"^\s*screen-length\b",
    r"^\s*screen-width\b",
    r"^\s*terminal\s+(?:length|width)\b",
    r"^\s*enable\s*$",
    r"^\s*system-view\s*$",
)

#: 设备报错的回显：说明这组内容是从终端里粘出来的"回显"而不是"配置"
_ERROR_ECHO = re.compile(
    r"^\s*(?:Error:|Failure:|%\s*(?:Invalid|Unrecognized|Incomplete|Ambiguous|Wrong)|"
    r"Unrecognized command|\^\s*$)",
    re.MULTILINE,
)

#: 单行长度上限：超过它的行极可能是终端折行劈开的半行（P0 就吃过这个亏）
MAX_LINE_LENGTH = 512
#: 单次变更的命令行数上限：P4 **不做全网替换**，一条变更单应该是"改几行"
MAX_LINES = 500


@dataclass(slots=True)
class DeployPlan:
    """一组待下发命令的预检结果。"""

    lines: tuple[str, ...]
    issues: list[Issue] = field(default_factory=list)
    content_hash: str = ""

    @property
    def ok(self) -> bool:
        """只要有一条 error 级问题就不允许下发（warning 不拦截）。"""
        return not any(issue.is_error for issue in self.issues)

    def text(self) -> str:
        """规范化后的正文（用于比对与哈希；行尾统一 LF、去掉首尾空行）。"""
        return "\n".join(self.lines) + "\n" if self.lines else ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "content_hash": self.content_hash,
            "lines": len(self.lines),
            "issues": [
                {"code": issue.code, "message": issue.message, "severity": issue.severity}
                for issue in self.issues
            ],
        }


def _normalize(raw_lines: Sequence[str]) -> list[str]:
    """去空行、去行尾空白、统一行尾。**不去注释**：注释在各厂商语义不同，
    猜错会把有效配置删掉（例如华三的 `#` 是分隔符而不是注释）。"""
    normalized: list[str] = []
    for line in raw_lines:
        line = line.replace("\r\n", "\n").replace("\r", "\n").rstrip()
        # 一行里混进了换行（从网页/终端粘贴的常见形态）→ 拆开
        normalized.extend(line.split("\n"))
    return [line for line in (item.strip() for item in normalized) if line]


def check_lines(lines: Sequence[str]) -> list[Issue]:
    """逐行与整组检查，返回问题列表（纯函数，便于单测）。"""
    issues: list[Issue] = []

    if not lines:
        issues.append(Issue("empty", "没有要下发的命令"))
        return issues

    if len(lines) > MAX_LINES:
        issues.append(
            Issue(
                "too_many_lines",
                f"命令有 {len(lines)} 行，超过单次变更上限 {MAX_LINES} 行——"
                "P4 只做小范围变更（改几行），不做整份配置替换",
            )
        )

    text = "\n".join(lines) + "\n"

    if _ERROR_ECHO.search(text):
        issues.append(
            Issue("error_echo", "内容里含设备报错回显（Error:/% Invalid 等），"
                  "看起来是从终端粘出来的回显而不是配置")
        )

    for pattern, label in DANGEROUS_PATTERNS:
        match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        if match:
            issues.append(
                Issue(
                    "dangerous_command",
                    f"包含不可撤销的{label}命令 {match.group(0).strip()!r}——"
                    "下发链路一律拒绝，请改由人工在维护窗口执行",
                )
            )

    for pattern in SESSION_SETUP_PATTERNS:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            issues.append(
                Issue(
                    "session_command_in_deploy",
                    f"包含会话预处理命令 {match.group(0).strip()!r}——"
                    "它只应存在于采集会话，写进配置就是越界改设备",
                )
            )

    for index, line in enumerate(lines, start=1):
        if len(line) > MAX_LINE_LENGTH:
            issues.append(
                Issue(
                    "line_too_long",
                    f"第 {index} 行 {len(line)} 字符，超过 {MAX_LINE_LENGTH}——"
                    "可能是终端折行劈开的半行",
                )
            )
        if "\x1b" in line:
            issues.append(Issue("ansi_residue", f"第 {index} 行残留 ANSI 转义序列"))

    return issues


def plan_deploy(raw_lines: Sequence[str]) -> DeployPlan:
    """把用户/变更单里的命令整理成一份可审批、可下发、可校验的计划。

    调用方（``services/deploy.py``）应当：
    1. 只允许 ``plan.ok`` 为真的计划进入审批；
    2. 审批通过后把 ``content_hash`` 写进变更单；
    3. 真正下发前**重新计算哈希并比对**——不一致就拒绝（"批的是 A、发的是 B"）。
    """
    lines = _normalize(raw_lines)
    issues = check_lines(lines)
    text = "\n".join(lines) + "\n" if lines else ""
    return DeployPlan(
        lines=tuple(lines),
        issues=issues,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


# --------------------------------------------------------------------------
# 逐行执行结果：中止时要说清"设备停在哪一行"
# --------------------------------------------------------------------------
#: 单行的执行结果
LINE_OK = "ok"
LINE_FAILED = "failed"
LINE_SKIPPED = "skipped"  # 因前一行失败而未发送

#: 整次下发的结论
DEPLOY_SUCCESS = "success"
DEPLOY_STOPPED = "stopped"  # 中途中止（**设备处于部分变更状态**，需要人接管）
DEPLOY_REJECTED = "rejected"  # 预检就没过，一行都没发


@dataclass(slots=True)
class LineResult:
    """一行命令的执行结果（设备回显只留摘要，避免把整段回显灌进数据库）。"""

    index: int
    line: str
    status: str
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "index": self.index,
            "line": self.line,
            "status": self.status,
        }
        if self.detail:
            data["detail"] = self.detail[:200]
        return data


@dataclass(slots=True)
class DeployOutcome:
    """一次下发的完整结论。

    ★ ``status`` 绝不允许出现"部分成功"这样的模糊说法：只有三种取值。
    尤其是**中途中止必须报 ``stopped``**——设备此刻是新旧配置混在一起的状态，
    把它记成"成功"或"失败"都会让接手的人做出错误判断。
    """

    device_id: int | None
    status: str
    results: tuple[LineResult, ...] = ()
    content_hash: str = ""
    error: str | None = None

    @property
    def applied(self) -> tuple[str, ...]:
        """**已生效**的行（设备上真的执行过的）。"""
        return tuple(item.line for item in self.results if item.status == LINE_OK)

    @property
    def stopped_at(self) -> LineResult | None:
        """第一行失败的位置（``None`` = 没有失败）。"""
        return next((item for item in self.results if item.status == LINE_FAILED), None)

    def summary(self) -> str:
        """给审计、通知和界面用的一句话。"""
        if self.status == DEPLOY_REJECTED:
            return f"下发被预检拒绝，未发送任何命令：{self.error or '存在危险命令'}"
        if self.status == DEPLOY_STOPPED:
            spot = self.stopped_at
            where = f"第 {spot.index} 行（{spot.line}）" if spot else "未知位置"
            return (
                f"下发在{where}中止，已生效 {len(self.applied)} 行——"
                "设备处于部分变更状态，请按操作手册人工接管"
            )
        return f"下发完成，共 {len(self.applied)} 行"

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "status": self.status,
            "content_hash": self.content_hash,
            "summary": self.summary(),
            "applied_lines": len(self.applied),
            "stopped_at": self.stopped_at.index if self.stopped_at else None,
            "error": self.error,
            "results": [item.as_dict() for item in self.results],
        }


def build_outcome(
    device_id: int | None,
    plan: DeployPlan,
    *,
    failed_index: int | None = None,
    error: str | None = None,
) -> DeployOutcome:
    """把"预检结果 + 执行到哪一行失败"整理成完整结论（纯函数，下发引擎与测试共用）。

    这是 P4-A 的收口：真正的执行器只需要"逐行发送、遇到错误回调一次"，
    剩下"后面那些行标成 skipped、整次结论定成 stopped"的判断都在这里，
    于是它可以被单测穷举，而不必靠真机去试。
    """
    if not plan.ok:
        return DeployOutcome(
            device_id=device_id,
            status=DEPLOY_REJECTED,
            content_hash=plan.content_hash,
            error=error or "；".join(issue.message for issue in plan.issues if issue.is_error),
            results=tuple(
                LineResult(index=index, line=line, status=LINE_SKIPPED)
                for index, line in enumerate(plan.lines, start=1)
            ),
        )

    results: list[LineResult] = []
    for index, line in enumerate(plan.lines, start=1):
        if failed_index is not None and index > failed_index:
            results.append(LineResult(index=index, line=line, status=LINE_SKIPPED))
        elif failed_index is not None and index == failed_index:
            results.append(LineResult(index=index, line=line, status=LINE_FAILED, detail=error))
        else:
            results.append(LineResult(index=index, line=line, status=LINE_OK))

    return DeployOutcome(
        device_id=device_id,
        status=DEPLOY_STOPPED if failed_index is not None else DEPLOY_SUCCESS,
        content_hash=plan.content_hash,
        error=error,
        results=tuple(results),
    )
