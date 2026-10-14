"""启动期提示的测试。

启动期的"提醒"看起来不值得测，但它们恰恰是"用户看不到就白做"的东西：写对了没人知道，
写错了也没人知道。这里只钉两条：该提醒时提醒、不该提醒时闭嘴（生产镜像里只有 dist，
没有 web/src，误报会让日志变噪声）。

捕获日志踩过两个坑，都在 ``capture_logs`` 里处理了：

1. **不能用 ``caplog``**：应用里的 ``configure_logging`` 用了 ``basicConfig(force=True)``，
   它会把 root 上的 handler 全清掉，``caplog`` 那种"挂在 root 上"的捕获方式一旦被清
   就再也收不到记录；
2. **要临时把 ``disabled`` 复位**：``logging.config.fileConfig`` 的默认
   ``disable_existing_loggers=True`` 会把 alembic.ini 里没列出的 logger（含 ``app.main``）
   整体禁用。tests/unit/test_migrations.py 会真的跑一次 ``alembic upgrade head``，
   于是同一次会话里排在它后面的用例日志全没了——"该报警"的假失败、"该闭嘴"的假通过。
   （根因已在 ``migrations/env.py`` 里改成 ``disable_existing_loggers=False``，
   这里再加一道，避免以后别的库又干同样的事。）
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from app.main import _warn_if_frontend_is_stale


class Sink(logging.Handler):
    """把指定 logger 的记录收进列表（直接挂在 logger 上，不依赖 root/propagate）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def capture_logs(call) -> list[str]:
    """执行 ``call``，返回它打到 ``app.main`` 上的 WARNING 及以上记录（见模块说明）。"""
    sink = Sink()
    logger = logging.getLogger("app.main")
    was_disabled = logger.disabled
    logger.disabled = False
    logger.addHandler(sink)
    try:
        call()
    finally:
        logger.removeHandler(sink)
        logger.disabled = was_disabled
    return [record.getMessage() for record in sink.records]


def build_tree(tmp_path: Path, *, source_newer: bool) -> Path:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    source = tmp_path / "web" / "src"
    source.mkdir()
    page = source / "App.vue"
    page.write_text("<template></template>", encoding="utf-8")

    now = time.time()
    built_at, source_at = (now - 600, now) if source_newer else (now, now - 600)
    os.utime(index, (built_at, built_at))
    os.utime(page, (source_at, source_at))
    return dist


def test_warns_when_source_is_newer_than_dist(tmp_path: Path) -> None:
    """改了页面却忘了 `npm run build`：症状是"菜单里没有新页面"，而 API 一切正常。"""
    dist = build_tree(tmp_path, source_newer=True)
    messages = capture_logs(lambda: _warn_if_frontend_is_stale(dist))
    assert any("npm run build" in message for message in messages), messages


def test_silent_when_dist_is_up_to_date(tmp_path: Path) -> None:
    dist = build_tree(tmp_path, source_newer=False)
    assert capture_logs(lambda: _warn_if_frontend_is_stale(dist)) == []


def test_silent_when_sources_are_absent(tmp_path: Path) -> None:
    """生产镜像里只 COPY 了 dist（没有 web/src）：不能因此报警。"""
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    assert capture_logs(lambda: _warn_if_frontend_is_stale(dist)) == []
