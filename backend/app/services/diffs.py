"""配置版本对比。

两个关键设计：

1. **比对前先归一化**：设备每次回显都会变的东西（``!Last configuration was
   updated at ...``、``Building configuration...``、字节数统计）必须在比对前去掉，
   否则每天都会报"全网变更"，工具就没人看了。归一化由驱动提供
   （``VendorDriver.normalize``），这里只负责调用。
2. **除了 diff 还给"重要变更"**：ACL、接口开关、路由、AAA、SNMP、NTP、日志服务器
   这些行的增删单独拎出来，用于告警与人工复核。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..collector.registry import get_registry
from ..db.models import ConfigSnapshot, Device
from .blobs import BlobStore
from .snapshots import load_config

__all__ = [
    "RISK_LEVELS",
    "SIGNIFICANT_PATTERNS",
    "DiffResult",
    "RiskHit",
    "classify_change",
    "diff_snapshots",
    "find_significant",
    "risk_level_of",
    "summarize_changes",
    "unified_diff",
]

#: "重要变更"关注清单：命中即高优先级提示（P3 会做成可配置规则）
SIGNIFICANT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^\s*(?:acl\s+(?:number|name|advanced|basic)|rule\s+\d+)", "ACL"),
    (r"^\s*(?:shutdown|undo\s+shutdown)", "接口开关"),
    (r"^\s*(?:port\s+(?:link-type|default\s+vlan|trunk)|switchport\s+)", "接口配置"),
    (
        r"^\s*(?:ip\s+route(?:-static)?|route-static|ospf|bgp|isis|rip|"
        r"ip\s+routing|router\s+(?:ospf|bgp|isis))",
        "路由",
    ),
    (r"^\s*(?:aaa|local-user|authentication|authorization|accounting)", "AAA/认证"),
    (r"^\s*(?:snmp-agent|snmp-server|community)", "SNMP"),
    (r"^\s*(?:ntp-service|ntp\s+server|ntp\s+authentication)", "NTP"),
    (r"^\s*(?:info-center\s+loghost|logging\s+(?:host|server))", "日志服务器"),
    (
        r"^\s*(?:stelnet|ssh\s+(?:server|user)|telnet\s+server|"
        r"user-interface|line\s+vty|protocol\s+inbound)",
        "管理面访问",
    ),
    (r"^\s*(?:radius|hwtacacs|tacacs)", "外部认证服务器"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), label) for pattern, label in SIGNIFICANT_PATTERNS
)

#: 关注清单的**风险等级**（P3 的分级告警依据）。
#:
#: 划分标准是"改错了会怎样"：直接失去管理面、或放开访问控制的算 high；
#: 影响业务转发/可观测性的算 medium。这个映射就是告警灵敏度的全部依据——
#: 调它等于调告警噪音，请连同 tests/unit/test_diffs_risk.py 的预期一起改。
RISK_LEVELS: dict[str, str] = {
    "管理面访问": "high",
    "AAA/认证": "high",
    "ACL": "high",
    "SNMP": "high",
    "外部认证服务器": "high",
    "接口开关": "medium",
    "路由": "medium",
    "接口配置": "medium",
    "NTP": "medium",
    "日志服务器": "medium",
}


@dataclass(frozen=True, slots=True)
class RiskHit:
    """一条值得告警的变更行。"""

    label: str
    level: str
    line: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "level": self.level, "line": self.line}


def _changed_lines(from_text: str, to_text: str) -> list[str]:
    """本次变更里**新增或删除**的行（未变动的行不算）。

    删除侧也要看：删掉一条 ACL 同样是高危（等于放开访问），不能只盯新增。
    """
    old, new = _lines(from_text), _lines(to_text)
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    changed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed.extend(new[j1:j2])  # 新增 / 修改后的行
        changed.extend(old[i1:i2])  # 删除 / 修改前的行
    return changed


def classify_change(from_text: str, to_text: str, *, limit: int = 50) -> list[RiskHit]:
    """把两个版本之间的变更按关注清单分类，并给出风险等级。

    :return: 命中的行（去重、按 high→medium 排序）；没有命中就是空列表。
        大范围替换整份配置时会命中很多行，``limit`` 用来封顶——告警里列 500 行没有意义。
    """
    hits: list[RiskHit] = []
    seen: set[tuple[str, str]] = set()
    for line in _changed_lines(from_text, to_text):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern, label in _COMPILED:
            if pattern.match(line):
                key = (label, stripped)
                if key in seen:
                    break
                seen.add(key)
                hits.append(
                    RiskHit(
                        label=label,
                        level=RISK_LEVELS.get(label, "medium"),
                        line=stripped,
                    )
                )
                break
        if len(hits) >= limit:
            break
    hits.sort(key=lambda item: (0 if item.level == "high" else 1, item.label, item.line))
    return hits


def risk_level_of(hits: Sequence[RiskHit]) -> str | None:
    """一组命中合成一个等级（有 high 就是 high）。"""
    if any(hit.level == "high" for hit in hits):
        return "high"
    return "medium" if hits else None


def _lines(text: str) -> list[str]:
    return text.split("\n") if text else []


def unified_diff(
    from_text: str,
    to_text: str,
    *,
    from_label: str = "before",
    to_label: str = "after",
    context: int = 3,
) -> str:
    diff = difflib.unified_diff(
        _lines(from_text),
        _lines(to_text),
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
        n=max(0, context),
    )
    return "\n".join(diff)


def summarize_changes(from_text: str, to_text: str) -> dict[str, int]:
    """统计新增/删除/修改行数（修改 = replace 块两侧行数的较小值）。"""
    matcher = difflib.SequenceMatcher(None, _lines(from_text), _lines(to_text), autojunk=False)
    added = removed = changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            changed += min(i2 - i1, j2 - j1)
            added += max(0, (j2 - j1) - (i2 - i1))
            removed += max(0, (i2 - i1) - (j2 - j1))
    return {"added": added, "removed": removed, "changed": changed}


def find_significant(text: str, *, limit: int = 200) -> list[str]:
    """从一份文本里挑出"重要变更"行（去重、保序）。"""
    found: list[str] = []
    seen: set[str] = set()
    for line in _lines(text):
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        for pattern, label in _COMPILED:
            if pattern.search(line):
                found.append(f"[{label}] {stripped}")
                seen.add(stripped)
                break
        if len(found) >= limit:
            break
    return found


@dataclass(slots=True)
class DiffResult:
    from_snapshot_id: int
    to_snapshot_id: int
    from_sha256: str
    to_sha256: str
    identical: bool
    added: int = 0
    removed: int = 0
    changed: int = 0
    unified: str = ""
    significant: list[str] = field(default_factory=list)
    normalized: bool = True


async def diff_snapshots(
    session: AsyncSession,
    blobs: BlobStore,
    *,
    device: Device,
    from_snapshot: ConfigSnapshot,
    to_snapshot: ConfigSnapshot,
    context: int = 3,
    normalize: bool = True,
    max_lines: int = 5000,
) -> DiffResult:
    """对比两个版本。内容一致时短路返回（省掉一次 diff 计算）。"""
    from_text = load_config(blobs, from_snapshot)
    to_text = load_config(blobs, to_snapshot)

    if from_snapshot.sha256 == to_snapshot.sha256:
        return DiffResult(
            from_snapshot_id=from_snapshot.id,
            to_snapshot_id=to_snapshot.id,
            from_sha256=from_snapshot.sha256,
            to_sha256=to_snapshot.sha256,
            identical=True,
            normalized=normalize,
        )

    driver = get_registry().try_get(device.vendor)
    if normalize and driver is not None:
        from_text = driver.normalize(from_text)
        to_text = driver.normalize(to_text)

    # 只对"新增/删除/修改"的行做重要变更识别：没动过的行不是变更
    matcher = difflib.SequenceMatcher(None, _lines(from_text), _lines(to_text), autojunk=False)
    from_lines = _lines(from_text)
    to_lines = _lines(to_text)
    changed_lines: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            changed_lines.extend(to_lines[j1:j2])
        elif tag == "delete":
            changed_lines.extend(from_lines[i1:i2])
        elif tag == "replace":
            changed_lines.extend(from_lines[i1:i2])
            changed_lines.extend(to_lines[j1:j2])

    stats = summarize_changes(from_text, to_text)
    unified = unified_diff(
        from_text,
        to_text,
        from_label=f"snapshot-{from_snapshot.id}",
        to_label=f"snapshot-{to_snapshot.id}",
        context=context,
    )
    lines = unified.split("\n")
    truncated = len(lines) > max_lines
    if truncated:
        unified = "\n".join(lines[:max_lines]) + f"\n... （差异过大，已截断，共 {len(lines)} 行）"

    return DiffResult(
        from_snapshot_id=from_snapshot.id,
        to_snapshot_id=to_snapshot.id,
        from_sha256=from_snapshot.sha256,
        to_sha256=to_snapshot.sha256,
        identical=False,
        added=stats["added"],
        removed=stats["removed"],
        changed=stats["changed"],
        unified=unified,
        significant=find_significant("\n".join(changed_lines)),
        normalized=normalize,
    )
