"""部署配置的一致性检查。

部署文件里的问题有两个共同特点：**单机开发永远暴露不出来**，以及**表现是"静默失效"**
而不是报错。所以它们值得用测试钉住：

* ``docker-compose.yml`` 用的是**显式列环境变量**（不是 ``env_file: .env``）。
  于是"往 ``.env.example`` 加了一个开关、忘了在 compose 里透传"的后果是：
  文档里写了、代码支持了、容器里根本收不到。
  两个真实例子：``DSZOON_SCHEDULER_ENABLED`` 收不到 → 多实例部署时每个实例都开
  调度器，同一个策略被重复执行（重复备份）；``DSZOON_AUTO_CREATE_SCHEMA`` 收不到 →
  在 ``.env`` 里设成 false 也没用，应用照样 ``create_all``，与"生产用 Alembic
  管 schema"的约定直接冲突。
* 镜像里必须带上 ``alembic.ini`` 与 ``migrations/``：README 让生产用
  ``alembic upgrade head`` 建表，而没带这两个文件时容器里**根本没有 alembic** 可用。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

BACKEND = Path(__file__).resolve().parents[2]
DEPLOY = BACKEND / "deploy"

#: Settings 的字段名 → 环境变量名（env_prefix="DSZOON_"）
VALID_ENV_KEYS = {f"DSZOON_{name.upper()}" for name in Settings.model_fields}

#: README 的环境变量表：| `DSZOON_XXX` | 默认值 | 说明 |
README_ROW = re.compile(r"^\|\s*`(DSZOON_[A-Z0-9_]+)`")


def test_env_example_keys_are_wired_into_compose() -> None:
    """``.env.example`` 里出现的每个 ``DSZOON_*`` 开关都必须真的传进容器。"""
    example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")

    declared = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip().startswith("DSZOON_")
    }
    assert declared, "没有从 .env.example 里解析到任何 DSZOON_* 开关（文件格式变了？）"

    missing = sorted(key for key in declared if key not in compose)
    assert not missing, (
        f"这些开关写在 .env.example 里、却没在 docker-compose.yml 里透传：{missing}。"
        "容器用的是显式 environment 列表，漏了就等于这个开关不存在。"
    )


def test_dockerfile_ships_migrations_and_frontend() -> None:
    """镜像里要带 alembic 配置与前端产物，否则文档里的部署步骤根本执行不了。"""
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    for needed in ("alembic.ini", "migrations", "web/dist"):
        assert needed in dockerfile, f"Dockerfile 没有 COPY {needed}"


def test_offline_bundle_includes_migrations() -> None:
    """离线包同样要能建表：只有 app/ 而没有 migrations/ 的话，现场只能 create_all。"""
    script = (DEPLOY / "offline" / "make_bundle.sh").read_text(encoding="utf-8")
    assert "alembic.ini" in script and "migrations" in script, script


def test_documented_env_keys_are_real_settings() -> None:
    """文档里写的环境变量必须真的是配置项。

    写错一个字母（``DSZOON_RETENTION_INTERVAL_HOUR``）不会有任何报错：pydantic-settings
    直接忽略未知变量，应用照默认值跑。于是文档说的和行为不一致，而两边单看都没问题——
    这正是"文档/代码/部署三处说法不一致"这类缺陷的典型形态。
    """
    readme = (BACKEND / "README.md").read_text(encoding="utf-8")
    documented = {
        match.group(1)
        for match in (README_ROW.match(line) for line in readme.splitlines())
        if match
    }
    assert documented, "没有从 README 的环境变量表里解析到任何 DSZOON_* 键（表格格式变了？）"

    example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip().startswith("DSZOON_")
    }

    unknown = sorted((documented | declared) - VALID_ENV_KEYS)
    assert not unknown, (
        f"文档/模板里出现了 Settings 里不存在的配置项：{unknown}。"
        "pydantic-settings 会静默忽略未知环境变量，所以这类笔误不会报错、只会让文档说谎。"
    )
