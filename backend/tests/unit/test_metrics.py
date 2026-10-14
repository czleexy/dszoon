"""Prometheus 指标渲染单测（纯函数，不需要数据库）。

指标最容易出的不是"算错数"，而是**格式不合规导致 Prometheus 整条抓取失败**：
少一个 ``# TYPE``、标签值里带未转义的引号、值是 ``nan``——对方的报错只会说
"invalid metric name/format"，不会告诉你是哪一行。所以这里逐行钉住格式。
"""

from __future__ import annotations

import re

from app.services.metrics import METRIC_HELP, Sample, render_prometheus

NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def test_render_includes_help_and_type_for_every_metric():
    text = render_prometheus(
        [Sample("dszoon_devices_total", 3, {"enabled": "true"}), Sample("dszoon_up", 1)]
    )
    lines = text.splitlines()
    assert "# HELP dszoon_devices_total " in text
    assert "# TYPE dszoon_devices_total gauge" in text
    assert "# TYPE dszoon_up gauge" in text
    assert lines[-1] == "" or text.endswith("\n"), "必须以换行结尾（Prometheus 要求）"


def test_labels_are_rendered_sorted_and_escaped():
    text = render_prometheus(
        [Sample("m", 1, {"b": 'he said "hi"', "a": "back\\slash"})]
    )
    assert 'm{a="back\\\\slash",b="he said \\"hi\\""} 1' in text


def test_numbers_are_plain_decimals():
    text = render_prometheus(
        [
            Sample("m_int", 3),
            Sample("m_float", 12.5),
            Sample("m_seconds", 1500 / 1000.0),
            Sample("m_bool", True),
        ]
    )
    assert "m_int 3" in text, "整数不要渲染成 3.0"
    assert "m_float 12.5" in text
    assert "m_seconds 1.5" in text
    assert "m_bool 1" in text


def test_nan_and_inf_become_zero():
    """NaN/Inf 会让 Prometheus 抓取失败，而它们的来源（空均值等）本就没有意义。"""
    text = render_prometheus(
        [Sample("m_nan", float("nan")), Sample("m_inf", float("inf"))]
    )
    assert "m_nan 0" in text and "m_inf 0" in text
    assert "nan" not in text.lower().replace("m_nan", "")


def test_every_emitted_metric_has_help_text():
    """没写说明的指标等于没文档：METRIC_HELP 必须覆盖全部指标名。"""
    for name in METRIC_HELP:
        assert NAME_RE.match(name), f"指标名不合规：{name}"
    text = render_prometheus([Sample(name, 0) for name in METRIC_HELP])
    for name in METRIC_HELP:
        assert f"# HELP {name} " in text


def test_type_is_one_of_the_prometheus_kinds():
    for name, (kind, _help) in METRIC_HELP.items():
        assert kind in {"gauge", "counter", "histogram", "summary"}, f"{name} 的类型非法"
