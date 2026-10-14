"""Prometheus 抓取端点（P3）。

``GET /metrics`` 返回 exposition 文本格式，**不要求登录**（Prometheus 抓取端不带会话；
``/healthz``、``/readyz`` 也是开放的，属同一类）。但"不要求登录"不等于"可以暴露到公网"：
指标里有设备总数、失败次数、不合规数量，等于把内网规模告诉别人。生产建议在 nginx 上
限制来源 IP（见 ``deploy/nginx.conf`` 的说明）。

指标口径与取值取舍见 ``app/services/metrics.py``。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ...services.metrics import collect_metrics, render_prometheus
from ..deps import SessionDep

router = APIRouter()


@router.get("/metrics", summary="Prometheus 指标", include_in_schema=True)
async def metrics(session: SessionDep) -> PlainTextResponse:
    samples = await collect_metrics(session)
    return PlainTextResponse(
        content=render_prometheus(samples),
        # Prometheus 官方建议的 content type（带版本号，便于以后改用 OpenMetrics）
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
