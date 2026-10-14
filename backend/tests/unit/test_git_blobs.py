"""Git 正文存储后端的单测（P4）。

这一组要盯的是三件"看起来能用、其实不能用"的事：

* **同一份内容被采 1000 次，仓库里只该有一个提交**。否则每天几百次轮询会把历史刷成
  一堆空提交，git log 就从"证据"变成了"噪声"，没人会去看它；
* **git 出问题时备份必须照常成功**。正文是主产物、版本库是附加证据，顺序不能反；
* **对象目录不在仓库内时要直接报错**，而不是悄悄退回文件系统——那会让"历史是完整的"
  变成一句只有出事时才会被拆穿的假话。

环境里没有 git 时整组跳过（CI 与离线机上可能真的没有），跳过比误报红要好。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.blobs import BlobStore, GitBlobStore, build_blob_store, git_executable

#: 与生产代码用**同一套**定位逻辑（PATH → 常见安装位置）。
#: 直接写 ``"git"`` 会让这组用例在"装了 git 但测试进程的 PATH 里没有"的机器上被静默跳过
#: ——而"跳过"在结果里看起来很像"通过"，实际上一次都没验过。
GIT = git_executable()


def _skip_reason() -> str:
    path = os.environ.get("PATH", "")
    return f"没找到 git（解析结果 {GIT!r}；测试进程 PATH 前 200 字符={path[:200]!r}）"


pytestmark = pytest.mark.skipif(
    GIT == "git" and shutil.which("git") is None, reason=_skip_reason()
)


def make_store(tmp_path: Path, *, nested: bool = True) -> tuple[GitBlobStore, Path]:
    repo = tmp_path / "repo"
    objects = (repo / "objects") if nested else (tmp_path / "outside" / "objects")
    return GitBlobStore(objects, repo), repo


def commit_subjects(repo: Path) -> list[str]:
    result = subprocess.run(
        [GIT, "-C", str(repo), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_identical_content_is_committed_only_once(tmp_path: Path):
    """★ 内容寻址天然去重，仓库历史也必须跟着去重。"""
    store, repo = make_store(tmp_path)

    first = store.put_text("interface GigabitEthernet0/0/1\n")
    second = store.put_text("interface GigabitEthernet0/0/1\n")

    assert first.created is True
    assert second.created is False, "同一内容不该再写一次盘"
    assert second.sha256 == first.sha256
    assert store.commit_count() == 1
    assert len(commit_subjects(repo)) == 1, "没有新对象却产生了提交，历史会被刷成噪声"


def test_new_content_is_committed_with_a_readable_message(tmp_path: Path):
    store, repo = make_store(tmp_path)
    first = store.put_text("sysname A\n")
    second = store.put_text("sysname B\n")

    assert second.created is True
    assert store.commit_count() == 2

    subjects = commit_subjects(repo)
    assert any(first.sha256[:12] in subject for subject in subjects), subjects
    # 提交信息要能看出是哪个对象，否则翻历史时只知道"有东西变了"
    assert all(subject.startswith("blob ") for subject in subjects), subjects


def test_content_round_trips_through_the_git_backend(tmp_path: Path):
    """存储后端换了，读回来的东西必须一模一样（两种后端读的是同一批文件）。"""
    store, _ = make_store(tmp_path)
    text = "sysname CORE-01\ninterface X\n description y\nreturn\n"
    stored = store.put_text(text)
    assert store.get_text(stored.object_key) == text


def test_backup_survives_git_failure(tmp_path: Path, monkeypatch):
    """★ git 挂了也不能让备份失败：正文必须照样落盘、照样能读回来。"""
    store, _ = make_store(tmp_path)
    monkeypatch.setattr(store, "_git", lambda *args, **kwargs: None)

    stored = store.put_text("sysname A\n")

    assert stored.created is True
    assert store.get_text(stored.object_key) == "sysname A\n"
    assert store.commit_count() == 0


def test_objects_outside_the_repo_are_refused(tmp_path: Path):
    """对象目录不在仓库里就无从 add/commit —— 必须报错，不能静默降级。"""
    with pytest.raises(ValueError) as error:
        make_store(tmp_path, nested=False)
    assert "必须位于仓库内" in str(error.value)


def test_unknown_backend_is_refused(tmp_path: Path):
    """★ 写错一个字母（gti）却照常工作，就是"配置写了不生效、没人会发现"的典型。"""
    with pytest.raises(ValueError) as error:
        build_blob_store(tmp_path / "objects", backend="gti")
    assert "未知的 blob_backend" in str(error.value)

    with pytest.raises(ValueError) as missing_repo:
        build_blob_store(tmp_path / "objects", backend="git")
    assert "git_repo" in str(missing_repo.value)

    # 默认仍是文件系统：git 后端是附加，不是替代
    default = build_blob_store(tmp_path / "objects")
    assert isinstance(default, BlobStore)
    assert not isinstance(default, GitBlobStore)
