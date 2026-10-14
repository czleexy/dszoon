"""FastAPI 应用工厂。

启动顺序（``lifespan``）：建表 → 首次运行时创建管理员 → 提供服务 → 优雅关闭
（取消仍在跑的备份任务、释放数据库连接）。

**建表用 ``create_all`` 只适合开发/单机**：生产环境请用 Alembic 迁移
（``alembic revision --autogenerate`` + ``alembic upgrade head``），
两者不要混着用，否则 schema 会漂移。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import __version__
from .api.v1.router import api_router
from .core.config import Settings, configure_logging, get_settings
from .core.crypto import CredentialCipher
from .core.security import Role, hash_password
from .db.models import User
from .db.session import Database, get_database
from .errors import AppError
from .services.backups import BackupRunner
from .services.blobs import build_blob_store
from .services.notifications import Notifier
from .services.scheduler import ScheduleRunner

logger = logging.getLogger(__name__)

__all__ = ["create_app", "prepare_database"]


async def prepare_database(database: Database, settings: Settings) -> str | None:
    """建表 + 首次运行时创建管理员。返回"新生成的管理员口令"（若生成了）。

    表的归属只有一个：要么应用自动建（``auto_create_schema=true``，开发/单机），
    要么 Alembic 建（生产）。两者混用会让迁移历史与真实 schema 分叉，所以生产
    环境下这里只做检查，缺表就直接报错并给出确切的命令。

    最后还有一道**schema 漂移检查**：``create_all`` 只建缺失的表、**不会给已有表加列**。
    沿用旧库 + 新代码的典型症状是"启动正常，一打开设备页全是 500
    （no such column: devices.group_id）"，非常难归因；这里直接把它变成一个
    启动期的、带着解决办法的报错。
    """
    if settings.auto_create_schema:
        await database.create_all()
    elif not await database.has_table("users"):
        raise RuntimeError(
            "数据库里没有 users 表，但 DSZOON_AUTO_CREATE_SCHEMA=false。"
            "生产环境请先执行 `alembic upgrade head` 建表；"
            "开发/单机环境可把 DSZOON_AUTO_CREATE_SCHEMA 设回 true。"
        )

    drift = await database.missing_schema()
    if drift:
        preview = "、".join(drift[:8]) + ("…" if len(drift) > 8 else "")
        if settings.auto_create_schema:
            # 不把 database_url 打进报错里：它会进日志、被贴进工单，而
            # postgresql+asyncpg://user:口令@host/db 是带口令的。
            remedy = (
                "开发/单机（SQLite）可以删掉数据库文件后重启，应用会按当前模型重建；"
                "PostgreSQL 或需要保留数据的库请改用 Alembic 迁移"
            )
        else:
            remedy = "请执行 `alembic upgrade head` 应用未跑到的迁移"
        raise RuntimeError(
            f"数据库 schema 比代码旧：缺少 {preview}。"
            "注意 create_all 只会补建缺失的表，**不会给已有表加列**。"
            f"{remedy}。"
        )

    async with database.session() as session:
        existing = await session.scalar(select(func.count()).select_from(User))
        if existing:
            return None
        password = settings.admin_password
        generated = None
        if not password:
            import secrets

            password = secrets.token_urlsafe(12)
            generated = password
        session.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(password),
                role=Role.ADMIN.value,
                display_name="初始管理员",
            )
        )
        await session.commit()
    if generated:
        logger.warning(
            "已创建初始管理员 %s，随机口令（只显示这一次）：%s  —— 请立即登录修改",
            settings.admin_username,
            generated,
        )
    else:
        logger.info("已创建初始管理员 %s（口令来自 DSZOON_ADMIN_PASSWORD）", settings.admin_username)
    return generated


def _warn_if_frontend_is_stale(dist: Path) -> None:
    """前端产物比源码旧时提醒一句。

    症状是"菜单里没有新页面 / 页面还是老样子"，而 API 一切正常——很容易往别处查。
    只在源码目录存在时才判断：生产镜像里只 COPY 了 dist，没有 web/src。
    """
    source = dist.parent / "src"
    if not source.is_dir():
        return
    try:
        built = (dist / "index.html").stat().st_mtime
        newest = max(
            (path.stat().st_mtime for path in source.rglob("*") if path.is_file()),
            default=0.0,
        )
    except OSError:  # pragma: no cover - 权限/竞态；提醒失败不值得影响启动
        return
    if newest > built:
        logger.warning(
            "前端产物（%s）比 web/src 旧：请在 web/ 下执行 npm run build，"
            "否则页面上看不到最新改动（后端 API 不受影响）",
            dist,
        )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code, "extra": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "请求参数校验失败",
                "code": "validation_failed",
                "extra": [
                    {"loc": list(err.get("loc", ())), "msg": err.get("msg")}
                    for err in exc.errors()
                ],
            },
        )


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    session_factory: Any = None,
) -> FastAPI:
    """构建应用。``session_factory`` 用于测试注入假设备会话。"""
    settings = settings or get_settings()
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)
    settings.ensure_dirs()

    database = database or get_database(settings.database_url, echo=settings.debug)
    cipher = CredentialCipher(settings.secret_key)
    # 正文存储：默认文件系统；配了 DSZOON_BLOB_BACKEND=git 时，每个新对象额外提交一次，
    # 于是"这份配置哪来的"有了一份独立于数据库的证据
    blobs = build_blob_store(
        settings.objects_dir, backend=settings.blob_backend, repo=settings.git_repo
    )
    #: 通知派发（P2）。备份层只通过 notifier.dispatch_task_result() 调用它，
    #: 且所有失败都在内部吞掉——通知坏了不能拖累备份。
    notifier = Notifier(database=database, cipher=cipher)
    runner = BackupRunner(
        database=database,
        settings=settings,
        cipher=cipher,
        blobs=blobs,
        session_factory=session_factory,
        notifier=notifier,
    )
    #: 定时调度（P2）。轮询数据库而不是注册内存 job，见 services/scheduler.py 的说明。
    scheduler = ScheduleRunner(
        database=database,
        runner=runner,
        poll_interval=settings.scheduler_poll_seconds,
        retention_interval_hours=settings.retention_interval_hours,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await prepare_database(database, settings)
        if settings.scheduler_enabled:
            await scheduler.start()
        logger.info("%s 已启动（数据库：%s）", settings.app_name, settings.database_url)
        try:
            yield
        finally:
            # 顺序：先停调度（不再产生新任务）→ 再取消在跑的任务 → 最后关数据库
            await scheduler.stop()
            await runner.shutdown()
            await database.dispose()

    app = FastAPI(
        title="dszoon 网络设备配置备份",
        version=__version__,
        description="华为 / 华三 / 锐捷 配置备份与版本对比",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.cipher = cipher
    app.state.blobs = blobs
    app.state.runner = runner
    app.state.notifier = notifier
    app.state.scheduler = scheduler
    #: 测试注入用的会话工厂（生产为 None）。连接测试也走它——
    #: 曾经这里没暴露，导致"连接测试"绕过注入去发真实 SSH 连接，
    #: 在测试里表现为 15 秒超时后返回 unreachable。
    app.state.session_factory = session_factory

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/healthz", tags=["health"], summary="存活检查")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"], summary="就绪检查（含数据库）")
    async def readyz() -> JSONResponse:
        ok = await database.ping()
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "database": ok, "backend": "sqlite" if settings.is_sqlite else "sqlalchemy"},
        )

    # 前端产物（web/ 构建到 web/dist）。放在最后 mount，避免吞掉 API 路由。
    # 这里列多个候选目录，是因为"从哪个目录启动 uvicorn"会改变相对路径的解析结果。
    for candidate in (Path("web/dist"), Path("../frontend/dist"), Path("frontend/dist")):
        if candidate.is_dir():
            app.mount("/", StaticFiles(directory=str(candidate), html=True), name="frontend")
            logger.info("已挂载前端静态资源：%s", candidate.resolve())
            _warn_if_frontend_is_stale(candidate)
            break
    else:
        logger.info("未找到前端产物（web/dist），仅提供 API；/docs 可直接联调")

    return app


# 启动方式（注意是 --factory，create_app 是工厂函数）：
#   uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

