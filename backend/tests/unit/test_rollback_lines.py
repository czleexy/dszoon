"""回滚内容计算的单测（P4-C 的地基）。

这一层的每一条规则都对应一个**会改坏设备**的可能：

* 把 ``description xxx`` 单独发出去（丢了 ``interface`` 上下文）→ 描述落到别的视图；
* 自动猜一个 ``undo xxx`` 出来 → 发一段没人验证过的命令到生产设备；
* 把 ``return`` / ``end`` 当命令发 → 设备退出配置模式，逐行确认流程当场断掉；
* 把整份配置当成"回滚内容" → 违反 §8.5.4"不做整份配置替换"。

所以这里的断言不是"能算出东西"，而是"该算的算、不该算的一个都不算"。
"""

from __future__ import annotations

from pathlib import Path

from app.services.deploy import RollbackPlan, rollback_lines

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HUAWEI_CONFIG = FIXTURES / "huawei" / "vrp8" / "display_current-configuration.txt"

#: 造两版差异最小的配置：一个 interface 块 + 一个 ospf 块
BEFORE = """#
sysname CORE-01
#
interface GigabitEthernet0/0/1
 description uplink-to-core
 ip address 10.0.0.1 255.255.255.0
#
ospf 1
 area 0.0.0.0
#
return
"""

#: 当前版本：interface 块**整块消失**，ospf 块多了一行
AFTER = """#
sysname CORE-01
#
ospf 1
 area 0.0.0.0
 import-route static
#
return
"""


def test_missing_block_is_restored_with_its_context():
    """★ 整块重发：只补内容而不补 ``interface`` 头，命令会落到错误的视图里。"""
    plan = rollback_lines(BEFORE, AFTER, target_snapshot_id=7)

    assert plan.target_snapshot_id == 7
    assert "interface GigabitEthernet0/0/1" in plan.restore, plan.restore
    assert "description uplink-to-core" in plan.restore
    assert "ip address 10.0.0.1 255.255.255.0" in plan.restore
    # 顺序必须保持原样（视图入口在前）
    assert plan.restore.index("interface GigabitEthernet0/0/1") < plan.restore.index(
        "description uplink-to-core"
    )


def test_added_line_is_never_auto_removed():
    """★ 只自动补回、不自动删除：删一行要写 ``undo``/``no``，各家写法不同，不能猜。"""
    plan = rollback_lines(BEFORE, AFTER)

    assert "import-route static" not in plan.restore
    assert "import-route static" in plan.needs_manual


def test_no_undo_or_no_command_is_ever_generated():
    """把上一条钉死成"绝不会出现"：生成的结果里不允许有删除类命令。"""
    plan = rollback_lines(BEFORE, AFTER)
    for line in plan.restore:
        lowered = line.lower()
        assert not lowered.startswith("undo "), line
        assert not lowered.startswith("no "), line


def test_identical_versions_need_nothing():
    plan = rollback_lines(BEFORE, BEFORE)
    assert plan.restore == ()
    assert plan.needs_manual == ()
    assert plan.empty


def test_separators_and_terminators_never_become_commands():
    """``#`` / ``!`` 是分隔行，``return`` / ``end`` 会退出配置模式——都不能被下发。"""
    plan = rollback_lines(BEFORE, AFTER)
    for bad in ("#", "!", "return", "end", "quit", "exit"):
        assert bad not in plan.restore, bad


def test_context_lines_are_counted():
    """整块重发会顺带带上当前已存在的行，数量要如实报出来（界面上要解释"为什么多发了"）。"""
    plan = rollback_lines(BEFORE, AFTER)
    # 被恢复的那一块共 3 行（interface + description + ip address），全都缺 → 上下文 0 行
    assert plan.context_lines == 0

    # 只缺一行时：整块 3 行都要发，于是有 2 行属于"上下文"
    partial = BEFORE.replace("description uplink-to-core\n", "")
    plan2 = rollback_lines(BEFORE, partial)
    assert plan2.restore.count("description uplink-to-core") == 1
    assert plan2.context_lines == 2


def test_config_without_separators_is_flagged_uncertain():
    """没有分隔行时整份配置就是"一块"，上下文无从确定——必须让界面提示人工核对。"""
    plan = rollback_lines("sysname A\ninterface X\n description y\n", "sysname A\n")
    assert plan.context_uncertain is True

    certain = rollback_lines(BEFORE, AFTER)
    assert certain.context_uncertain is False


def test_huge_restore_is_truncated():
    """§8.5.4：不做整份配置替换。差异大到超过上限时必须截断并标记。"""
    target = "sysname A\n" + "".join(f"line-{i}\n" for i in range(600))
    plan = rollback_lines(target, "sysname A\n", limit=100)
    assert plan.truncated is True
    assert len(plan.restore) == 100

    small = rollback_lines(target, "sysname A\n", limit=1000)
    assert small.truncated is False


def test_plan_dict_is_reviewable():
    """界面要一次看到"补什么、还差什么人工处理、有没有截断"。"""
    data = rollback_lines(BEFORE, AFTER, target_snapshot_id=3).as_dict()
    assert data["target_snapshot_id"] == 3
    assert data["restore_lines"] == len(data["restore"])
    assert data["needs_manual_lines"] == len(data["needs_manual"])
    assert data["truncated"] is False
    assert data["context_uncertain"] is False
    assert isinstance(RollbackPlan().as_dict()["restore"], list)


# --------------------------------------------------------------------------
# 真实夹具：拿华为 VRP8 的实际配置做一遍
# --------------------------------------------------------------------------
def _fixture() -> str:
    return HUAWEI_CONFIG.read_text(encoding="utf-8")


def _drop_block(text: str, prefix: str) -> str:
    """把某个块（从 ``prefix`` 开头那行到下一个分隔行）整块删掉，模拟"配置被别人删了"。"""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            skipping = True
            continue
        if skipping:
            if stripped in ("#", "!"):
                skipping = False
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def test_real_fixture_deleted_block_is_restored_verbatim():
    """被删掉的块要能**原样**补回来（连头行一起），且不夹带分隔行与 ``return``。"""
    original = _fixture()
    damaged = _drop_block(original, "interface ")

    plan = rollback_lines(original, damaged, target_snapshot_id=99)

    assert plan.restore, "整块被删掉却什么都没算出来"
    assert plan.restore[0].startswith("interface "), "块的第一行必须是视图入口"
    assert "return" not in plan.restore
    assert "#" not in plan.restore
    assert plan.needs_manual == (), "当前版本没有多出任何行，不该有需要人工删除的内容"


def test_real_fixture_added_line_lands_in_needs_manual():
    """真实场景里最常见的变更就是"多了一行"——它必须落到 needs_manual 而不是被猜着删掉。"""
    original = _fixture()
    added = "ip route-static 10.99.0.0 255.255.255.0 10.10.10.254"
    lines = original.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() in ("return", "end"):
            lines.insert(index, added)
            break
    changed = "\n".join(lines) + "\n"

    plan = rollback_lines(original, changed)
    assert added in plan.needs_manual
    assert plan.empty, "只有「多一行」的变更不该自动生成任何下发内容"
