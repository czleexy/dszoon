"""配置正文的对象存储（内容寻址）。

为什么不把配置正文直接塞进数据库行：一份配置 10~500KB，一个设备一年可能上百个
版本，几万台设备就是几百 GB——放进关系库会让备份/恢复/迁移都变得很痛苦。
这里沿用 P0 归档的目录约定（``objects/<sha[:2]>/<sha>.cfg``），元数据在数据库里。

**去重是天然的**：同一份配置采 1000 次也只有一个文件。
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "BACKEND_FILESYSTEM",
    "BACKEND_GIT",
    "BlobStore",
    "GitBlobStore",
    "StoredBlob",
    "build_blob_store",
    "git_executable",
    "safe_name",
]

#: 可选的正文存储后端。默认文件系统；``git`` 是在它之上**附加**版本历史，
#: 不是替代（对象仍是同样的目录与文件名，所以两种后端读到的东西完全一样）。
BACKEND_FILESYSTEM = "filesystem"
BACKEND_GIT = "git"

#: 找不到 git 时再探这几个位置。
#:
#: 为什么需要：Windows 上 git 装好了**不等于**当前进程的 PATH 里有它——以服务方式运行时
#: 尤其如此（SYSTEM 账户的 PATH 常常没有 Git\cmd）。而"每个提交都静默失败"这种状态
#: 极难被发现：对象照样落盘、备份照样成功，只有哪天想翻 `git log` 时才发现历史是空的。
_GIT_FALLBACKS = (
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    "/usr/bin/git",
    "/usr/local/bin/git",
)


def git_executable() -> str:
    """定位 git 可执行文件：先按 PATH，再探常见安装位置，最后退回 ``"git"``。"""
    found = shutil.which("git")
    if found:
        return found
    for candidate in _GIT_FALLBACKS:
        if Path(candidate).is_file():
            return candidate
    return "git"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-]+")
#: 配置正文的正文物名（内容寻址）。回收时只认这个形态。
_BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}\.cfg$")


def safe_name(value: str, *, default: str = "unknown") -> str:
    cleaned = _UNSAFE_NAME.sub("_", (value or "").strip()).strip("._-")
    return cleaned[:120] or default


@dataclass(slots=True, frozen=True)
class StoredBlob:
    sha256: str
    object_key: str
    path: Path
    created: bool
    size_bytes: int


class BlobStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def object_path(self, sha256: str, *, suffix: str = ".cfg") -> Path:
        return self.root / sha256[:2] / f"{sha256}{suffix}"

    def key_for(self, sha256: str, *, suffix: str = ".cfg") -> str:
        return f"{sha256[:2]}/{sha256}{suffix}"

    def put_text(self, text: str, *, suffix: str = ".cfg") -> StoredBlob:
        """写入文本并返回内容寻址信息。已存在则直接复用（不重复写盘）。"""
        raw = text.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        path = self.object_path(sha, suffix=suffix)
        created = False
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再改名：避免进程被杀时留下半个文件被当成有效归档
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
            created = True
        return StoredBlob(
            sha256=sha,
            object_key=self.key_for(sha, suffix=suffix),
            path=path,
            created=created,
            size_bytes=len(raw),
        )

    def get_text(self, object_key: str) -> str:
        path = self.root / object_key
        if not path.is_file():
            raise FileNotFoundError(f"归档对象不存在：{path}")
        return path.read_text(encoding="utf-8")

    def put_session_log(self, device_key: str, stamp: str, content: str) -> str:
        """保存原始会话日志（排障用），返回相对路径。"""
        directory = self.root.parent / "sessions" / safe_name(device_key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stamp}.log"
        path.write_text(content, encoding="utf-8", newline="\n")
        return str(path.relative_to(self.root.parent))

    # ------------------------------------------------------------------
    def sessions_root(self) -> Path:
        """会话日志目录（与 objects 同级）。"""
        return self.root.parent / "sessions"

    def prune_unreferenced(
        self, referenced_keys: set[str], *, min_age_seconds: float = 0.0
    ) -> tuple[int, int]:
        """回收没有被任何快照引用的配置正文，返回 ``(文件数, 释放字节)``。

        **只删 ``<64位hex>.cfg`` 形态的文件**：那是运行配置的正文物。其他后缀
        （例如以后可能加的 ``.startup.cfg``、导出件）不在这里的职责范围内，
        宁可漏回收也不能误删。

        ``min_age_seconds`` 是给"回收"和"正在写入的采集"之间留的安全间隙：
        采集是先落正文文件、再插快照行（同一个事务），所以在大批量任务跑到一半时
        触发清理，可能看到"文件已存在、但还没有任何行引用它"——那一刻它看上去
        正是"没人引用的垃圾"，删掉就等于把一个即将写库的版本弄坏。
        本函数默认不留宽限期（0 = 立即回收），实际的宽限期由调用方决定：
        ``retention.enforce`` 传的是 ``BLOB_GRACE_SECONDS``（1 小时，比那毫秒级的
        窗口宽了好几个数量级）。
        """
        cutoff = (time.time() - min_age_seconds) if min_age_seconds > 0 else None
        removed = 0
        freed = 0
        for path in self.root.glob("*/*"):
            if not path.is_file() or not _BLOB_NAME_RE.match(path.name):
                continue
            key = f"{path.parent.name}/{path.name}"
            if key in referenced_keys:
                continue
            try:
                stat = path.stat()
                if cutoff is not None and stat.st_mtime > cutoff:
                    continue
                freed += stat.st_size
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - 被占用/权限问题，跳过而不是中断清理
                continue
        return removed, freed

    def prune_session_logs(self, keep_days: int, *, now: float | None = None) -> int:
        """删除超过保留期的会话日志，返回删除数量。

        用**文件 mtime** 而不是路径里的时间戳：路径格式变过、也可能被人工改名，
        mtime 更可靠。
        """
        if keep_days <= 0:
            return 0
        root = self.sessions_root()
        if not root.is_dir():
            return 0
        cutoff = (now if now is not None else time.time()) - keep_days * 86400
        removed = 0
        for path in root.glob("*/*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:  # pragma: no cover
                continue
        return removed

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        files = [p for p in self.root.glob("*/*") if p.is_file()]
        return {
            "objects": len(files),
            "object_bytes": sum(p.stat().st_size for p in files),
        }


class GitBlobStore(BlobStore):
    """在内容寻址目录之外，把每个新对象提交进一个 Git 仓库（P4）。

    为什么值得做：对象目录里躺的是"裸文件"——谁在什么时候改了什么，只有数据库里的记录
    知道，而记录本身是可以被改的。放进 Git 之后，历史就是**提交历史**：
    ``git log`` 能让"这份配置哪来的、什么时候进来的"脱离本系统独立成立。
    对交付场景（客户方要能自证）这一点比多一个功能值钱。

    两个刻意的克制：

    * **只有新对象才提交**（内容寻址保证同一内容不重复落盘）。没有新对象时一个提交都不
      产生——否则每天几百次轮询采集会把仓库刷成一堆空提交，历史反而没法看；
    * **git 失败不回滚对象写入**，只记日志。正文是主产物、版本库是附加证据；
      因为 git 出问题而让备份失败，等于把附加价值摆到主价值前面。
    """

    def __init__(
        self,
        root: Path | str,
        repo: Path | str,
        *,
        author_name: str = "dszoon",
        author_email: str = "dszoon@localhost",
        init: bool = True,
        git_binary: str | None = None,
    ) -> None:
        super().__init__(Path(root).resolve())
        self.repo = Path(repo).resolve()
        # 显式传入优先；否则按 PATH / 常见位置找（见 git_executable 的说明）
        self.git_binary = git_binary or git_executable()
        try:
            # 对象必须落在仓库工作树里，否则 `git add` 无从下手。
            # 这里**直接报错而不是退回文件系统**：静默降级会让"历史是完整的"这句话变成
            # 假的，而那种错只有在真出事、想翻 git log 的时候才会被发现。
            self.path_in_repo = self.root.relative_to(self.repo)
        except ValueError as exc:
            raise ValueError(
                f"Git 后端的对象目录必须位于仓库内：objects={self.root} repo={self.repo}"
            ) from exc
        self.author_name = author_name
        self.author_email = author_email
        self.repo.mkdir(parents=True, exist_ok=True)
        if init and not self.is_repo():
            self._git("init", "--quiet")

    # ------------------------------------------------------------------
    def is_repo(self) -> bool:
        result = self._git("rev-parse", "--git-dir")
        return result is not None and result.returncode == 0

    def _git(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        """跑一条 git 命令。**不抛异常**：返回 ``None`` 表示连 git 都没跑起来。"""
        try:
            result = subprocess.run(
                [self.git_binary, "-C", str(self.repo), *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:  # git 不在 PATH 上
            logger.warning("调用 git 失败（%s）：%s", args[0], exc)
            return None
        if result.returncode != 0:
            logger.warning(
                "git %s 返回 %s：%s",
                " ".join(args[:2]),
                result.returncode,
                (result.stderr or "").strip()[:300],
            )
        return result

    def put_text(self, text: str, *, suffix: str = ".cfg") -> StoredBlob:
        stored = super().put_text(text, suffix=suffix)
        if stored.created:
            self.commit_object(stored)
        return stored

    def commit_object(self, stored: StoredBlob) -> bool:
        """把刚写入的对象提交一次。返回是否成功（失败不影响备份本身）。"""
        try:
            relative = stored.path.resolve().relative_to(self.repo)
        except ValueError:  # pragma: no cover - 构造时已保证，留作兜底
            return False
        if self._git("add", "--", str(relative)) is None:
            return False
        # 用 -c 传作者信息，而不是要求现场先配好 git config：
        # 内网机器上 git 往往没配 user.name，那样每个提交都会失败
        result = self._git(
            "-c",
            f"user.name={self.author_name}",
            "-c",
            f"user.email={self.author_email}",
            "commit",
            "--quiet",
            "-m",
            f"blob {stored.sha256[:12]} ({stored.size_bytes} bytes)",
            "--",
            str(relative),
        )
        return result is not None and result.returncode == 0

    def commit_count(self) -> int:
        """提交数（自检与测试用）。"""
        result = self._git("rev-list", "--count", "HEAD")
        if result is None or result.returncode != 0:
            return 0
        try:
            return int(result.stdout.strip())
        except ValueError:
            return 0


def build_blob_store(
    objects_dir: Path | str,
    *,
    backend: str = BACKEND_FILESYSTEM,
    repo: Path | str | None = None,
) -> BlobStore:
    """按配置构建正文存储。

    ★ 未知的 ``backend`` **直接报错**而不是悄悄退回文件系统：写错一个字母（``gti``）
    却照常工作，正是"配置写了不生效、而且没人会发现"的典型。
    """
    if backend == BACKEND_GIT:
        if not repo:
            raise ValueError("blob_backend=git 时必须同时设置 git_repo")
        return GitBlobStore(objects_dir, repo)
    if backend != BACKEND_FILESYSTEM:
        raise ValueError(f"未知的 blob_backend：{backend!r}（可选 filesystem / git）")
    return BlobStore(objects_dir)
