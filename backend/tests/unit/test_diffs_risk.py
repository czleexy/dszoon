"""重要变更的分级识别（P3）。

规则只有一条值得反复说：**分级必须建立在"改错了会怎样"上，而不是"这条看着眼熟"**。
失去管理面（telnet/user-interface）、放开访问控制（ACL/SNMP 团体名/AAA/外部认证）
是 high；接口开关、路由、NTP、日志这些影响业务或可观测性的是 medium。

这套等级同时喂给两处：任务收尾时的告警（``risky_change`` 事件）与界面上的变更清单。
"""

from __future__ import annotations

from app.services.diffs import RiskHit, classify_change, risk_level_of

#: 一份"基线"配置，用来当变更前/后的对照物
BASE = """\
sysname SW-01
#
vlan batch 10 20
#
interface GigabitEthernet0/0/1
 port link-type trunk
 port trunk allow-pass vlan 10 20
#
snmp-agent community read cipher %^%#AAAA%^%#
#
ntp-service unicast-server 10.0.0.1
#
user-interface vty 0 4
 authentication-mode aaa
 protocol inbound ssh
#
return
"""


def levels(hits: list[RiskHit]) -> set[str]:
    return {hit.level for hit in hits}


def labels(hits: list[RiskHit]) -> set[str]:
    return {hit.label for hit in hits}


def test_no_change_no_hits():
    assert classify_change(BASE, BASE) == []
    assert risk_level_of([]) is None


def test_noise_only_change_is_not_a_risk():
    """只改了易变行（时间戳）不算风险——否则每天都会告警，工具就没人看了。"""
    after = BASE.replace("sysname SW-01", "sysname SW-01\n!Last configuration was updated at 2026-01-01")
    assert classify_change(BASE, after) == []


def test_enabling_telnet_is_high_risk():
    """管理面变更 = high：多一行 telnet 就是"把大门打开了"。"""
    after = BASE.replace("sysname SW-01", "sysname SW-01\ntelnet server enable")
    hits = classify_change(BASE, after)
    assert labels(hits) == {"管理面访问"}
    assert levels(hits) == {"high"}
    assert risk_level_of(hits) == "high"
    assert "telnet server enable" in hits[0].line


def test_removing_an_acl_is_high_risk_too():
    """★ 删除侧也要看：删掉一条 ACL 等于放开访问，不能只盯新增行。"""
    before = BASE + "acl number 3000\n rule 5 deny tcp destination-port eq 23\n"
    hits = classify_change(before, BASE)
    assert labels(hits) == {"ACL"}
    assert risk_level_of(hits) == "high"


def test_snmp_and_ntp_are_different_levels():
    """SNMP（可能放开读权限）是 high；NTP（影响时间）是 medium。"""
    after = BASE.replace(
        "ntp-service unicast-server 10.0.0.1",
        "ntp-service unicast-server 10.0.0.2\nsnmp-agent community read public",
    )
    hits = classify_change(BASE, after)
    assert labels(hits) == {"NTP", "SNMP"}
    by_label = {hit.label: hit.level for hit in hits}
    assert by_label == {"SNMP": "high", "NTP": "medium"}
    assert risk_level_of(hits) == "high", "有 high 就是 high"


def test_high_risk_sorts_first():
    after = BASE.replace(
        "ntp-service unicast-server 10.0.0.1",
        "ntp-service unicast-server 10.0.0.2\nip route-static 0.0.0.0 0 10.0.0.254",
    )
    # 同时引入一条 high（管理面）与两条 medium
    after = after.replace("sysname SW-01", "sysname SW-01\ntelnet server enable")
    hits = classify_change(BASE, after)
    assert hits[0].level == "high", [hit.as_dict() for hit in hits]
    assert {hit.level for hit in hits} == {"high", "medium"}


def test_hits_are_deduplicated():
    """同一行出现两次只记一条（大范围替换时同一个模式会命中很多次）。"""
    after = BASE.replace(
        "user-interface vty 0 4",
        "user-interface vty 0 4\n protocol inbound telnet",
    )
    hits = classify_change(BASE, after)
    assert len(hits) == len({(hit.label, hit.line) for hit in hits})


def test_limit_caps_the_hit_list():
    """整份配置被替换时会命中很多行，告警里列几百行没有意义。"""
    after = "\n".join(f"acl number {3000 + index}" for index in range(200))
    hits = classify_change(BASE, after, limit=10)
    assert len(hits) == 10


def test_risk_hit_serializes():
    hits = classify_change(BASE, BASE + "telnet server enable\n")
    assert hits[0].as_dict() == {
        "label": "管理面访问",
        "level": "high",
        "line": "telnet server enable",
    }
