"""配置下发与回滚的服务层（P4）。

当前只落地**回滚目标选择**这一块纯逻辑，因为它是 P4-C 的地基，而且可以被穷举测试：

* "上一个可用版本"不是"上一个版本"。可用的定义必须写死成可复核的规则，
  否则一定会出现"回滚到了一个残缺配置"——那是比不回滚更糟的结果。
* 选择过程要**同时给出"跳过了哪些版本、为什么"**。运维点"回滚"时看到的是
  "回滚到 8-03 那版"，而如果 8-05 那版就是因为内容残缺被跳过的，界面必须说出来，
  否则他会以为是工具漏了最新的一版。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..collector.deploy import (
    DEPLOY_REJECTED,
    DEPLOY_STOPPED,
    DEPLOY_SUCCESS,
    MAX_LINES,
    DeployPlan,
)
from ..collector.integrity import Issue

__all__ = [
    "BLOCK_SEPARATORS",
    "CHANGE_APPROVED",
    "CHANGE_CANCELLED",
    "CHANGE_DEPLOYING",
    "CHANGE_DRAFT",
    "CHANGE_EXPIRED",
    "CHANGE_PENDING",
    "CHANGE_REJECTED",
    "CHANGE_STATE_LABELS",
    "CHANGE_STOPPED",
    "CHANGE_SUCCEEDED",
    "CHANGE_TRANSITIONS",
    "ChangeView",
    "NON_COMMAND_LINES",
    "ROLLBACK_MIN_RUN_STATUS",
    "RollbackCandidate",
    "RollbackDecision",
    "RollbackPlan",
    "TERMINAL_CHANGE_STATES",
    "can_transition",
    "check_approval_allowed",
    "check_deploy_allowed",
    "check_submit_allowed",
    "rollback_lines",
    "select_rollback_target",
    "state_after_outcome",
]

#: 只有"采集成功"的版本才可作为回滚目标。
#:
#: 为什么不接受 ``unchanged``：这个状态的含义是"本次采到的内容与上一版相同"，
#: 它确认的是**旧状态还在**，而不是"产生了一个新的好状态"。把它当回滚目标，
#: 等于把设备滚回一个更早的版本，与"返回上一版"的字面意思不符，也容易误导操作人。
ROLLBACK_MIN_RUN_STATUS = ("success",)

#: 出现这些 issue 的采集结果，其内容不可信，不作为回滚基线
UNTRUSTED_ISSUE_CODES = (
    "integrity_error",
    "device_error",
    "more_residue",
    "ansi_residue",
    "bad_terminator",
    "empty",
    "too_short",
    "too_short_bytes",
    "missing_marker",
    "possible_line_wrap",
)


@dataclass(frozen=True, slots=True)
class RollbackCandidate:
    """一个候选回滚版本：快照 + 产生它的那次采集结果。"""

    snapshot_id: int
    captured_at: datetime
    run_status: str | None = None
    issue_codes: tuple[str, ...] = ()
    line_count: int = 0
    sha256: str = ""

    @property
    def trusted(self) -> bool:
        """内容是否可信（采集成功、且没有任何完整性告警）。"""
        if self.run_status not in ROLLBACK_MIN_RUN_STATUS:
            return False
        return not (set(self.issue_codes) & set(UNTRUSTED_ISSUE_CODES))

    def reason_untrusted(self) -> str | None:
        """不可用的原因（可用时返回 ``None``），直接给人看。"""
        if self.run_status is None:
            return "没有对应的采集记录"
        if self.run_status not in ROLLBACK_MIN_RUN_STATUS:
            if self.run_status == "unchanged":
                # 单独说清：这不是"失败"，而是"没有产生新状态"——运维看到"不是成功"
                # 会以为那次采集出错了，而实际上设备好好的、只是配置没变。
                return "采集结果是无变更（unchanged）：它只确认旧状态还在，不是新产生的可用状态"
            return f"采集结果不是成功（{self.run_status}）"
        bad = sorted(set(self.issue_codes) & set(UNTRUSTED_ISSUE_CODES))
        if bad:
            return f"内容完整性校验未通过（{', '.join(bad)}）"
        return None


@dataclass(slots=True)
class RollbackDecision:
    """回滚目标选择的结果 + 被跳过版本的说明。"""

    target: RollbackCandidate | None = None
    skipped: list[tuple[int, str]] = field(default_factory=list)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.target is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "target_snapshot_id": self.target.snapshot_id if self.target else None,
            "target_captured_at": self.target.captured_at.isoformat(sep=" ")
            if self.target
            else None,
            "reason": self.reason,
            "skipped": [
                {"snapshot_id": snapshot_id, "reason": reason}
                for snapshot_id, reason in self.skipped
            ],
        }


def select_rollback_target(
    candidates: Iterable[RollbackCandidate],
    *,
    current_snapshot_id: int | None = None,
) -> RollbackDecision:
    """挑出"上一个可用版本"。

    规则（按顺序，全部可复核）：

    1. 排除**当前版本**——要的是"上一个"，把自己当目标等于空操作；
    2. 按采集时间**从新到旧**扫描，取第一个内容可信的版本
       （可信 = 采集成功，且没有完整性类 issue）；
    3. 其余版本连同**被跳过的原因**一并返回，便于界面如实展示；
    4. 一个都没有时 ``target`` 为 ``None``，``reason`` 说明为什么。

    :param current_snapshot_id: 设备当前版本；``None`` 表示"不排除任何版本"
        （例如手工指定基准时）。
    """
    skipped: list[tuple[int, str]] = []
    ordered: Sequence[RollbackCandidate] = sorted(
        candidates, key=lambda item: (item.captured_at, item.snapshot_id), reverse=True
    )

    for candidate in ordered:
        if current_snapshot_id is not None and candidate.snapshot_id == current_snapshot_id:
            skipped.append((candidate.snapshot_id, "这就是当前版本"))
            continue
        reason = candidate.reason_untrusted()
        if reason is not None:
            skipped.append((candidate.snapshot_id, reason))
            continue
        return RollbackDecision(target=candidate, skipped=skipped)

    if not ordered:
        reason = "该设备还没有任何配置版本，无法回滚"
    elif len(skipped) == len(ordered):
        reason = "没有可用的历史版本（全部因完整性或采集状态被排除）"
    else:  # pragma: no cover - 只有 candidates 为空时才会走到，留作兜底
        reason = "没有找到可用版本"
    return RollbackDecision(target=None, skipped=skipped, reason=reason)


# --------------------------------------------------------------------------
# P4-B：变更单状态机（纯逻辑：不碰设备、不碰数据库、不碰 HTTP）
# --------------------------------------------------------------------------
# 为什么把状态机做成"一张迁移表 + 几个纯函数"，而不是写进 ORM 方法或路由里：
# 下发链路的正确性几乎全在"什么时候允许往下走"上，而这类判断一旦埋进数据库会话里，
# 就只能靠"连库跑一遍"来验证。抽出来之后它可以被穷举——尤其是"未知状态"、
# "终态再流转"、"批 A 发 B"、"没有基线就下发"这些**恰恰不会在真机上复现**的分支。
CHANGE_DRAFT = "draft"
CHANGE_PENDING = "pending"
CHANGE_APPROVED = "approved"
CHANGE_DEPLOYING = "deploying"
CHANGE_SUCCEEDED = "succeeded"
CHANGE_STOPPED = "stopped"
CHANGE_REJECTED = "rejected"
CHANGE_CANCELLED = "cancelled"
CHANGE_EXPIRED = "expired"

CHANGE_STATE_LABELS: dict[str, str] = {
    CHANGE_DRAFT: "草稿",
    CHANGE_PENDING: "待审批",
    CHANGE_APPROVED: "已批准",
    CHANGE_DEPLOYING: "下发中",
    CHANGE_SUCCEEDED: "已生效",
    CHANGE_STOPPED: "中途中止",
    CHANGE_REJECTED: "已驳回",
    CHANGE_CANCELLED: "已撤回",
    CHANGE_EXPIRED: "审批已失效",
}

#: 状态迁移表。**这张表就是"不允许跳步"的唯一定义**，改动它等于改安全边界，
#: 所以它是一份数据而不是散落在各处的 if。
CHANGE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    CHANGE_DRAFT: (CHANGE_PENDING, CHANGE_CANCELLED),
    CHANGE_PENDING: (CHANGE_APPROVED, CHANGE_REJECTED, CHANGE_CANCELLED),
    # 批准之后只有两条路：下发，或者因内容被改动而作废、重新审批。
    # 没有"就地改一行继续发"——那正是"批的是 A、发的是 B"。
    CHANGE_APPROVED: (CHANGE_DEPLOYING, CHANGE_EXPIRED, CHANGE_CANCELLED),
    # 真发出第一行之前仍可能发现内容与审批不一致（→ rejected，见 build_outcome）
    CHANGE_DEPLOYING: (CHANGE_SUCCEEDED, CHANGE_STOPPED, CHANGE_REJECTED),
    # ★ 中止后**不允许直接重发**：设备此刻新旧配置混在一起，下一次下发是一次全新的
    #   决定，必须重新走审批（回到待审批）。
    CHANGE_STOPPED: (CHANGE_PENDING, CHANGE_CANCELLED),
    # 审批失效（内容被改过）后同样要重新提交——原审批已经作废，不能复活
    CHANGE_EXPIRED: (CHANGE_PENDING, CHANGE_CANCELLED),
    CHANGE_SUCCEEDED: (),
    CHANGE_REJECTED: (),
    CHANGE_CANCELLED: (),
}

#: 终态：不会再流转。驳回后想改内容请**新建一张变更单**——
#: 就地复用会把"谁在什么时候驳回了什么"这件事抹掉。
TERMINAL_CHANGE_STATES: tuple[str, ...] = tuple(
    state for state, allowed in CHANGE_TRANSITIONS.items() if not allowed
)


def can_transition(current: str, target: str) -> bool:
    """状态是否允许流转。**未知状态一律 False**（fail closed）。"""
    return target in CHANGE_TRANSITIONS.get(current, ())


@dataclass(slots=True, frozen=True)
class ChangeView:
    """判定所需的变更单快照（从 ORM 行读出后构造）。

    只带判定要用的字段，于是状态机不依赖数据库会话，单测也不必建表——
    这是"能穷举"的前提。
    """

    device_id: int
    state: str
    content_hash: str
    requested_by: int
    id: int | None = None
    approved_by: int | None = None
    before_snapshot_id: int | None = None
    before_captured_at: datetime | None = None
    created_at: datetime | None = None


def check_submit_allowed(plan: DeployPlan) -> list[Issue]:
    """提交审批前的闸门：预检没过的内容**不允许进入审批队列**。

    为什么不让"有问题的内容"先进审批再拦：审批人看到的必须是一份可下发的计划。
    把危险命令塞进审批队列，等于让"审批"去替预检背锅，而且审批人未必看得懂命令。
    """
    if plan.ok:
        return []
    errors = [
        Issue("precheck_failed", f"预检未通过，不能提交审批：{issue.message}")
        for issue in plan.issues
        if issue.is_error
    ]
    return errors or [Issue("precheck_failed", "预检未通过，不能提交审批")]


def check_approval_allowed(
    change: ChangeView,
    *,
    approver_id: int,
    allow_self_approval: bool = False,
) -> list[Issue]:
    """审批闸门。

    ★ 必须分开的是**审批权**：申请与审批同为一人，等于没有审批
    （§8.5.1 第 1 条"申请人以外的第二人审批通过"）。

    注意这里**不禁止申请人自己执行**：现场常态就是"申请人提单 → 主管批准 →
    申请人执行"，他熟悉这台设备和维护窗口；把执行也一并禁掉，只会逼出
    "用别人的账号点一下"这种绕行，反而更难追责。
    """
    issues: list[Issue] = []
    if change.state != CHANGE_PENDING:
        issues.append(
            Issue(
                "not_pending",
                f"变更单当前是「{CHANGE_STATE_LABELS.get(change.state, change.state)}」，"
                "只有「待审批」才能批准或驳回",
            )
        )
    if approver_id == change.requested_by and not allow_self_approval:
        issues.append(
            Issue(
                "self_approval",
                "审批人与申请人是同一个人：请由第二个人审批"
                "（确需自批时由管理员打开 DSZOON_DEPLOY_ALLOW_SELF_APPROVAL，并留下书面说明）",
            )
        )
    return issues


def check_deploy_allowed(
    change: ChangeView,
    plan: DeployPlan,
    *,
    enabled: bool,
    confirm: bool,
) -> list[Issue]:
    """下发前的闸门（第 2 道；第 1 道是 ``plan_deploy`` 的预检）。

    返回空列表 = 允许下发。**任何一条不满足都是拒绝，不是"警告后继续"**：
    这条链路上一旦出现"知道有风险但还是发了"，事后就无法追责，也没法解释
    为什么工具明知危险还照做。
    """
    issues: list[Issue] = []

    # ---- 约束 1：默认关闭 + 必须显式确认 ----
    if not enabled:
        issues.append(
            Issue("deploy_disabled", "配置下发功能未开启（DSZOON_DEPLOY_ENABLED=false）")
        )
    if not confirm:
        issues.append(
            Issue("confirm_required", "未显式确认（confirm=true）：下发是写生产设备的动作")
        )

    # ---- 约束 1：审批必须真实存在，且不是自批 ----
    if change.state != CHANGE_APPROVED:
        issues.append(
            Issue(
                "not_approved",
                f"变更单当前是「{CHANGE_STATE_LABELS.get(change.state, change.state)}」，"
                "只有「已批准」才能下发",
            )
        )
    if change.approved_by is None:
        issues.append(Issue("no_approver", "没有审批人记录，拒绝下发"))
    elif change.approved_by == change.requested_by:
        issues.append(Issue("self_approved", "审批人与申请人是同一个人，拒绝下发"))

    # ---- "批的是 A、发的是 B"：审批绑哈希，下发前重算比对 ----
    if plan.content_hash != change.content_hash:
        issues.append(
            Issue(
                "content_changed",
                "待下发的命令与审批时的内容不一致（哈希不同）：请重新提交审批，"
                "不要就地改完继续发",
            )
        )
    if not plan.ok:
        issues.append(
            Issue(
                "precheck_failed",
                "预检未通过：" + "；".join(i.message for i in plan.issues if i.is_error),
            )
        )

    # ---- 约束 3：先备份后下发（没有基线就没有回滚）----
    if change.before_snapshot_id is None:
        issues.append(
            Issue(
                "no_baseline",
                "没有下发前的采集基线：采不到就不允许下发，否则回滚没有依据",
            )
        )
    elif (
        change.created_at is not None
        and change.before_captured_at is not None
        and change.before_captured_at < change.created_at
    ):
        issues.append(
            Issue(
                "stale_baseline",
                "下发前的采集早于本变更单创建时间，不能作为本次变更的回滚基线："
                "请在提交变更单之后重新采集一次",
            )
        )

    return issues


def state_after_outcome(current: str, status: str) -> str | None:
    """把一次下发的结论映射成变更单的下一个状态（``None`` = 该流转不被允许）。

    ★ 特别注意 ``DEPLOY_STOPPED`` 映射到 ``stopped`` 而不是任何"完成"类状态：
    设备此刻处于**部分变更**状态，把它记成成功会让接手的人对现场做出错误判断
    （以为配置已完整生效，于是不去接管）。
    """
    target = {
        DEPLOY_SUCCESS: CHANGE_SUCCEEDED,
        DEPLOY_STOPPED: CHANGE_STOPPED,
        DEPLOY_REJECTED: CHANGE_REJECTED,
    }.get(status)
    if target is None or not can_transition(current, target):
        return None
    return target


# --------------------------------------------------------------------------
# P4-C：从"目标版本 vs 当前版本"算出回滚内容（纯逻辑）
# --------------------------------------------------------------------------
#: 配置里的**块分隔行**：华为/华三用 ``#``，锐捷用 ``!``（三家夹具实测都是这个形态）。
#:
#: 为什么必须有它：回滚要重新下发的行**必须带着上下文**。``description uplink-to-core``
#: 离开 ``interface GigabitEthernet0/0/1`` 就不再是"给那个接口加描述"，甚至可能被写进
#: 别的视图里。这三家都用分隔行把"一个视图下的若干命令"切成块，于是
#: "块内只要缺一行，就整块重发"是一条**可复核**的规则——比猜"哪一行是命令头"
#: 可靠得多（猜错就是往生产设备上发错命令）。
BLOCK_SEPARATORS = frozenset({"#", "!"})

#: 不该当作命令下发到设备上的行。
#: ``return`` / ``end`` 会让设备退出配置模式，直接打断"逐行发送、逐行确认回显"的流程；
#: ``quit`` / ``exit`` 同理；分隔行本身也不该被当成命令。
NON_COMMAND_LINES = BLOCK_SEPARATORS | {"return", "end", "quit", "exit"}


@dataclass(slots=True)
class RollbackPlan:
    """回滚到某个历史版本所需的内容（纯计算结果，一行都不会被发出去）。"""

    target_snapshot_id: int | None = None
    #: 可以自动生成的行：目标版本里有、当前没有的内容（**整块**重发，带上下文）
    restore: tuple[str, ...] = ()
    #: 必须人工处理的行：当前有、目标没有 → 删除类，各家写法不同，不猜
    needs_manual: tuple[str, ...] = ()
    #: 因为整块重发而顺带包含的、当前已经存在的行（配置命令基本幂等，重复下发无害）
    context_lines: int = 0
    #: 目标配置里找不到块分隔行 → 上下文无法确定，界面必须提示人工核对
    context_uncertain: bool = False
    #: 行数超过单次变更上限而被截断（§8.5.4：不做整份配置替换）
    truncated: bool = False

    @property
    def lines(self) -> tuple[str, ...]:
        """可以直接进变更单的行。"""
        return self.restore

    @property
    def empty(self) -> bool:
        """没有任何可自动生成的内容——这时回滚只能靠人工编写命令。"""
        return not self.restore

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_snapshot_id": self.target_snapshot_id,
            "restore_lines": len(self.restore),
            "needs_manual_lines": len(self.needs_manual),
            "context_lines": self.context_lines,
            "context_uncertain": self.context_uncertain,
            "truncated": self.truncated,
            "restore": list(self.restore),
            "needs_manual": list(self.needs_manual),
        }


def _blocks(text: str) -> tuple[list[list[str]], bool]:
    """按分隔行把配置切成块，返回 ``(块列表, 是否见过分隔行)``。"""
    blocks: list[list[str]] = []
    current_block: list[str] = []
    separated = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in BLOCK_SEPARATORS:
            separated = True
            if current_block:
                blocks.append(current_block)
                current_block = []
            continue
        if line in NON_COMMAND_LINES:  # return / end / quit / exit
            continue
        current_block.append(line)
    if current_block:
        blocks.append(current_block)
    return blocks, separated


def rollback_lines(
    target_text: str,
    current_text: str,
    *,
    target_snapshot_id: int | None = None,
    limit: int = MAX_LINES,
) -> RollbackPlan:
    """算出"回滚到 ``target_text``"需要下发的内容。

    两条规则，都是刻意保守的：

    1. **只自动补回"目标有、当前没有"的内容**（把丢掉的配置加回来），并且**整块重发**，
       保证命令落在正确的视图里；
    2. **删除类一律不自动生成**。要从设备上删掉的东西，各厂商要写成 ``undo xxx`` /
       ``no xxx``，还得先进入对应视图，同一语义在不同版本上写法都可能不同。离线猜这个
       等于往生产设备上发一段没人验证过的命令——这个项目最不能接受的就是这个。
       这类差异全部列进 ``needs_manual``，由人在变更单里补。

    于是"回滚"的语义是清楚的：**能自动恢复的是"被删掉/被改坏的内容"，
    需要人工确认的是"当前多出来的内容"**。
    """
    target_blocks, separated = _blocks(target_text)
    current_blocks, _ = _blocks(current_text)

    current_lines = {line for block in current_blocks for line in block}

    restore: list[str] = []
    context_lines = 0
    for block in target_blocks:
        missing = [line for line in block if line not in current_lines]
        if not missing:
            continue
        # 整块重发：块内只要有缺失，就把这一块连同它的头一行（视图入口）一起发出去
        restore.extend(block)
        context_lines += len(block) - len(missing)

    truncated = len(restore) > limit
    if truncated:
        restore = restore[:limit]

    target_lines = {line for block in target_blocks for line in block}
    needs_manual: list[str] = []
    for block in current_blocks:
        for line in block:
            if line not in target_lines and line not in needs_manual:
                needs_manual.append(line)

    return RollbackPlan(
        target_snapshot_id=target_snapshot_id,
        restore=tuple(restore),
        needs_manual=tuple(needs_manual),
        context_lines=context_lines,
        # 没有分隔行时整份配置就是"一块"，上下文无从确定，必须让人核对
        context_uncertain=not separated,
        truncated=truncated,
    )
