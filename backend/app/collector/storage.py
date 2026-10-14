"""归档存储：内容寻址 + 每设备运行索引。

为什么要内容寻址（sha256）而不是"每次存一个新文件"：

* 同一配置在 1000 台设备/1000 次巡检里内容相同的概率很高，寻址存储天然去重；
* "内容没变"可以直接由 sha 相等得出结论，不需要逐字节比对；
* 版本链路是只读的，不存在"覆盖历史"这种事故。

P1 会把元数据搬进 PostgreSQL，但**配置正文仍然放在这里**（对象存储或本地盘），
数据库只存指针。所以本模块的接口设计成可以直接被服务层复用。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import textutil

__all__ = ["ArtifactStore", "SnapshotRecord", "StoredSnapshot", "safe_name"]

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-]+")


def safe_name(value: str, *, default: str = "unknown") -> str:
    """把设备名/主机名变成安全的目录名。"""
    cleaned = _UNSAFE_NAME.sub("_", (value or "").strip()).strip("._-")
    return cleaned[:120] or default


@dataclass(slots=True)
class SnapshotRecord:
    """一次采集的完整归档记录（与引擎解耦，便于单独测试）。"""

    device_key: str
    host: str
    vendor: str
    status: str
    running_config: str | None = None
    normalized_config: str | None = None
    startup_config: str | None = None
    facts: dict[str, str] = field(default_factory=dict)
    sha256: str | None = None
    normalized_sha256: str | None = None
    size_bytes: int = 0
    line_count: int = 0
    duration_ms: int = 0
    changed: bool | None = None
    error_code: str | None = None
    error_message: str | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    transcript: str | None = None
    captured_at: str = ""

    def __post_init__(self) -> None:
        if not self.captured_at:
            self.captured_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass(slots=True)
class StoredSnapshot:
    device_key: str
    sha256: str
    object_path: Path
    index_path: Path
    run_meta_path: Path
    session_log_path: Path | None = None
    deduplicated: bool = False
    changed: bool | None = None
    previous_sha256: str | None = None


class ArtifactStore:
    """本地文件系统归档后端。

    目录结构::

        <root>/
          objects/<sha[:2]>/<sha>.cfg            配置正文（内容寻址，去重）
          objects/<sha[:2]>/<sha>.startup.cfg    下次启动配置（可选）
          devices/<device_key>/index.jsonl       每次 run 一行摘要（append-only）
          devices/<device_key>/runs/<ts>.json    单次 run 的完整元数据
          sessions/<device_key>/<ts>.log         原始会话日志（排障用，可设保留期）
    """

    def __init__(
        self,
        root: Path | str,
        *,
        keep_session_logs: bool = True,
    ) -> None:
        self.root = Path(root)
        self.keep_session_logs = keep_session_logs
        self.objects_dir = self.root / "objects"
        self.devices_dir = self.root / "devices"
        self.sessions_dir = self.root / "sessions"

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def object_path(self, sha256: str, *, suffix: str = ".cfg") -> Path:
        return self.objects_dir / sha256[:2] / f"{sha256}{suffix}"

    def device_dir(self, device_key: str) -> Path:
        return self.devices_dir / safe_name(device_key)

    def index_file(self, device_key: str) -> Path:
        return self.device_dir(device_key) / "index.jsonl"

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def save(self, record: SnapshotRecord) -> StoredSnapshot:
        device_dir = self.device_dir(record.device_key)
        device_dir.mkdir(parents=True, exist_ok=True)
        runs_dir = device_dir / "runs"
        runs_dir.mkdir(exist_ok=True)

        previous_sha = self.latest_sha(record.device_key)

        object_path = self.object_path(record.sha256 or "0" * 64)
        deduplicated = False
        if record.running_config is not None and record.sha256:
            object_path.parent.mkdir(parents=True, exist_ok=True)
            if object_path.exists():
                deduplicated = True
            else:
                object_path.write_text(record.running_config, encoding="utf-8", newline="\n")

        if record.startup_config:
            startup_sha = textutil.sha256_text(record.startup_config)
            startup_path = self.object_path(startup_sha, suffix=".startup.cfg")
            startup_path.parent.mkdir(parents=True, exist_ok=True)
            if not startup_path.exists():
                startup_path.write_text(record.startup_config, encoding="utf-8", newline="\n")

        session_log_path: Path | None = None
        if self.keep_session_logs and record.transcript:
            session_dir = self.sessions_dir / safe_name(record.device_key)
            session_dir.mkdir(parents=True, exist_ok=True)
            stamp = _stamp(record.captured_at)
            session_log_path = session_dir / f"{stamp}-{_short(record.sha256)}.log"
            session_log_path.write_text(record.transcript, encoding="utf-8", newline="\n")

        meta = asdict(record)
        meta.pop("running_config", None)  # 正文在 objects 里，索引里不留副本
        meta.pop("startup_config", None)
        meta.pop("normalized_config", None)
        meta.pop("transcript", None)
        meta["object_key"] = str(object_path.relative_to(self.root)) if record.sha256 else None
        meta["session_log"] = (
            str(session_log_path.relative_to(self.root)) if session_log_path else None
        )
        meta["deduplicated"] = deduplicated

        run_meta_path = runs_dir / f"{_stamp(record.captured_at)}-{_short(record.sha256)}.json"
        run_meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )

        with self.index_file(record.device_key).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")

        return StoredSnapshot(
            device_key=record.device_key,
            sha256=record.sha256 or "",
            object_path=object_path,
            index_path=self.index_file(record.device_key),
            run_meta_path=run_meta_path,
            session_log_path=session_log_path,
            deduplicated=deduplicated,
            changed=record.changed,
            previous_sha256=previous_sha,
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def history(self, device_key: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        path = self.index_file(device_key)
        if not path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:  # pragma: no cover - 索引损坏时跳过坏行
                    continue
        if limit is not None:
            return entries[-limit:]
        return entries

    def latest_entry(self, device_key: str) -> dict[str, Any] | None:
        entries = self.history(device_key, limit=1)
        return entries[-1] if entries else None

    def latest_sha(self, device_key: str) -> str | None:
        entry = self.latest_entry(device_key)
        if not entry:
            return None
        return entry.get("sha256")

    def load_config(self, sha256: str) -> str:
        path = self.object_path(sha256)
        if not path.is_file():
            raise FileNotFoundError(f"归档对象不存在：{path}")
        return path.read_text(encoding="utf-8")

    def load_latest_config(self, device_key: str) -> tuple[str, str] | None:
        """返回 (sha256, 配置正文)。"""
        sha = self.latest_sha(device_key)
        if not sha:
            return None
        try:
            return sha, self.load_config(sha)
        except FileNotFoundError:
            return None

    def config_for_entry(self, entry: dict[str, Any]) -> str:
        return self.load_config(str(entry["sha256"]))

    def iter_objects(self) -> Iterator[Path]:
        if not self.objects_dir.is_dir():
            return
        yield from self.objects_dir.glob("*/*")

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        devices = [p for p in self.devices_dir.iterdir()] if self.devices_dir.is_dir() else []
        objects = list(self.iter_objects())
        return {
            "devices": len(devices),
            "objects": len(objects),
            "object_bytes": sum(p.stat().st_size for p in objects if p.is_file()),
        }

    def referenced_shas(self) -> set[str]:
        referenced: set[str] = set()
        if not self.devices_dir.is_dir():
            return referenced
        for index in self.devices_dir.glob("*/index.jsonl"):
            with index.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:  # pragma: no cover
                        continue
                    if entry.get("sha256"):
                        referenced.add(str(entry["sha256"]))
        return referenced

    def prune_objects(self) -> list[Path]:
        """删除没有任何索引引用的对象文件（保留策略执行后的回收步骤）。"""
        referenced = self.referenced_shas()
        removed: list[Path] = []
        for path in self.iter_objects():
            if not path.is_file():
                continue
            sha = path.name.split(".")[0]
            if sha not in referenced:
                path.unlink()
                removed.append(path)
        return removed


def _stamp(captured_at: str) -> str:
    return (captured_at or time.strftime("%Y%m%dT%H%M%S")).replace(":", "").replace("-", "")


def _short(sha: str | None) -> str:
    return (sha or "nosha")[:8]
