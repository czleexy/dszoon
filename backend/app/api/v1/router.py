"""v1 路由汇总。"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    audit,
    auth,
    backups,
    compliance,
    deploy,
    devices,
    meta,
    metrics,
    ops,
    search,
    snapshots,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(devices.router)
api_router.include_router(backups.router)
api_router.include_router(snapshots.router)
api_router.include_router(ops.router)
api_router.include_router(compliance.router)
api_router.include_router(audit.router)
api_router.include_router(metrics.router)
api_router.include_router(deploy.router)
api_router.include_router(search.router)

__all__ = ["api_router"]
