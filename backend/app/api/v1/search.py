"""跨设备配置检索接口（P3 收尾）。

这是全库明文检索，权限与留痕的要求都比普通读接口高：

* 需要 **auditor 及以上**（viewer 连自己的设备列表都能看，但不该能全库搜明文）；
* **每次搜索都写审计**（``search.config``），查询词、命中设备数、命中行数都进 detail——
  "谁在什么时候搜了什么是需要留痕的信息"本身也是等保要问的；
* 返回的是**脱敏后的行**（见 ``services/search.py`` 的说明：在原文上匹配、脱敏后返回）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...services import audit
from ...services.search import search_configs
from ..deps import AuditorUser, BlobsDep, SessionDep, client_ip

router = APIRouter(tags=["search"])


@router.get("/search/config", summary="跨设备检索配置（明文匹配，返回脱敏行）")
async def search_config(
    request: Request,
    session: SessionDep,
    user: AuditorUser,
    blobs: BlobsDep,
    q: str = Query(min_length=1, max_length=200, description="搜索词（子串、不区分大小写）"),
    device_id: list[int] | None = Query(default=None, description="限定设备，可重复传"),
    limit: int = Query(default=50, ge=1, le=500, description="最多返回多少行"),
    max_devices: int = Query(default=200, ge=1, le=2000, description="最多扫描多少台设备"),
) -> dict:
    """在每台设备的**最新版本**里搜一段文本。

    查的是最新版本而不是全部历史：运维问"哪台设备还有这个配置"时，答案几乎总是
    "当前还在用的那些"。历史版本要逐版搜，那是另一个场景（也慢得多），
    真需要时再单独开接口，不要把它混进来让这个接口变得又慢又难解释。
    """
    result = await search_configs(
        session, blobs, query=q, device_ids=device_id, limit=limit, max_devices=max_devices
    )
    await audit.record(
        session,
        action="search.config",
        user_id=user.id,
        username=user.username,
        target_type="config",
        detail={
            "query": result["query"],
            "scanned_devices": result["scanned_devices"],
            "matched_devices": result["matched_devices"],
            "hits": len(result["hits"]),
            "truncated": result["truncated"],
        },
        client_ip=client_ip(request),
    )
    await session.commit()
    return result
