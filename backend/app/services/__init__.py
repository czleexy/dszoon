"""业务服务层。

这一层不依赖 FastAPI：入参是 domain 对象/简单类型，出参是 ORM 对象，
异常用 ``app.errors`` 里的类型。所有 HTTP 细节（状态码、分页参数校验）在
``app.api`` 里处理。
"""

from .blobs import BlobStore, StoredBlob
from .diffs import diff_snapshots, unified_diff
from .snapshots import redact_config, render_snapshot_content

__all__ = [
    "BlobStore",
    "StoredBlob",
    "diff_snapshots",
    "redact_config",
    "render_snapshot_content",
    "unified_diff",
]
