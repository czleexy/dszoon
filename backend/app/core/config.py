"""应用配置。

所有配置项都可以用 ``DSZOON_`` 前缀的环境变量覆盖，例如::

    DSZOON_DATABASE_URL=postgresql+asyncpg://dszoon:***@db:5432/dszoon
    DSZOON_SECRET_KEY=<Fernet key>
    DSZOON_JWT_SECRET=<随机串>

生产部署**必须**显式设置 ``DSZOON_SECRET_KEY`` 与 ``DSZOON_JWT_SECRET``；
开发环境缺省时会在 ``data_dir`` 下自动生成并落盘（权限 0600），并在日志里提醒。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

SECRETS_FILE = ".secrets.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSZOON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "dszoon"
    debug: bool = False
    #: 前端开发服务器地址（CORS 白名单）
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # ---- 存储 ----
    #: 开发默认 SQLite；生产用 postgresql+asyncpg://user:pass@host:5432/db
    database_url: str = "sqlite+aiosqlite:///./var/dszoon.db"
    #: 配置正文（内容寻址）、会话日志的根目录
    data_dir: Path = Path("var")
    #: 启动时是否自动建表（create_all）。
    #:
    #: 开发/单机部署置 true 最省事；**生产用 Alembic 管理 schema 时必须置 false**，
    #: 否则"应用自己补表"会让迁移历史与真实 schema 悄悄分叉——某天 alembic 报
    #: "表已存在"或"列不存在"时，几乎查不出是哪一步跑偏的。
    auto_create_schema: bool = True

    # ---- 密钥 ----
    #: 设备凭据加密密钥（Fernet，base64 32 字节）。留空则自动生成到 data_dir
    secret_key: str = ""
    #: JWT 签名密钥。留空则自动生成
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 720

    # ---- 初始管理员（仅首次建库时生效）----
    admin_username: str = "admin"
    #: 留空则生成随机密码并在启动日志里打印一次
    admin_password: str = ""

    # ---- 采集默认参数（可被单台设备覆盖）----
    backup_concurrency: int = 20
    backup_retries: int = 2
    backup_precheck: bool = True
    connect_timeout: float = 15.0
    command_timeout: float = 180.0
    idle_timeout: float = 25.0
    #: 是否保存原始会话日志（排障用）
    keep_session_logs: bool = True

    # ---- P2：定时调度 ----
    #: 调度器轮询间隔（秒）。精度受它限制，但 30s 对"每天凌晨备份"完全够用，
    #: 而且换来的是"数据库即唯一事实来源、没有内存 job 同步问题"。
    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = 30.0
    #: 自动执行保留策略的间隔（小时），0 = 只允许手工清理。
    #:
    #: 为什么要有它：保留策略只靠人手点"立即清理"，在真实环境里等于**永远不跑**——
    #: 版本会一直堆到磁盘满，而那时已经晚了。默认每天跑一次（靠策略上的
    #: ``last_run_at`` 判断，重启进程也不会重复执行）。
    #:
    #: ⚠ 它挂在调度器的轮询循环里：``scheduler_enabled=false`` 时**不会**自动清理，
    #: 只保留界面上的"立即清理"按钮。
    retention_interval_hours: float = 24.0

    # ---- P3：合规基线 ----
    #: 采集成功后是否自动跑一遍合规基线（关掉就只能手工检查）
    compliance_enabled: bool = True
    #: 自定义规则文件路径列表（YAML）。留空 = 用内置基线 ``app/compliance/baselines``。
    #: **是替换而不是叠加**：运维想完全接管基线时不必先把内置规则关掉。
    compliance_rules: list[Path] = Field(default_factory=list)

    # ---- P4：配置下发（默认关闭）----
    #: 配置下发总开关。**默认 false**，而且是刻意的。
    #:
    #: P0~P3 只读设备，最坏情况是"没采到"；P4 往生产设备写配置，最坏情况是**把网络改坏**。
    #: 所以"装上就具备下发能力"是不可接受的默认值：开启它应当是一次人为的、写在变更
    #: 记录里的决定，而不是升级完就自动生效。开启前请读完 ``DEVELOPMENT_PLAN.md`` §8.5.1。
    deploy_enabled: bool = False
    #: 是否允许申请人审批自己的变更单。**默认 false** = 必须第二人审批。
    #:
    #: 只在"单人值守、确实没有第二个人"的环境里才考虑打开；打开就意味着
    #: "谁申请谁批准"，事后追责链断掉一半，所以开启时应当有书面说明。
    deploy_allow_self_approval: bool = False

    # ---- P4：正文存储后端 ----
    #: ``filesystem``（默认）| ``git``。
    #: git = 在内容寻址目录之外，把每个**新对象**提交进一个仓库，让"这份配置哪来的、
    #: 什么时候进来的"有独立于本系统数据库的证据（``git log``）。写错的值会直接报错，
    #: 不会静默退回文件系统。
    blob_backend: str = "filesystem"
    #: git 后端要求的仓库路径。**对象目录必须在这个仓库内**（否则没法 add/commit），
    #: 不满足时构造存储会直接报错——静默降级会让"历史是完整的"变成假话。
    git_repo: Path | None = None

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, str):
            return Path(os.path.expanduser(value))
        return value

    # ------------------------------------------------------------------
    # 派生路径
    # ------------------------------------------------------------------
    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    # ------------------------------------------------------------------
    # 密钥
    # ------------------------------------------------------------------
    def resolve_secrets(self) -> None:
        """确保 secret_key / jwt_secret 可用。

        开发环境自动生成并落盘（只落盘**本次生成的**那一项，不会把环境变量里的
        密钥写进文件）；缺失时的提醒见日志。

        注意：**密钥换了就解不开已存的设备凭据**，所以生成后必须备份。
        """
        if self.secret_key and self.jwt_secret:
            return

        stored = self._load_stored_secrets()
        generated: dict[str, str] = {}

        if not self.secret_key:
            self.secret_key = stored.get("secret_key", "")
            if not self.secret_key:
                self.secret_key = _new_fernet_key()
                generated["secret_key"] = self.secret_key
        if not self.jwt_secret:
            self.jwt_secret = stored.get("jwt_secret", "")
            if not self.jwt_secret:
                self.jwt_secret = secrets.token_urlsafe(48)
                generated["jwt_secret"] = self.jwt_secret

        if generated:
            self._merge_secrets(generated)
            logger.warning(
                "已自动生成密钥并保存到 %s —— 生产环境请改用环境变量 "
                "DSZOON_SECRET_KEY / DSZOON_JWT_SECRET；并注意备份该文件："
                "更换 secret_key 会导致已保存的设备密码无法解密。",
                self.data_dir / SECRETS_FILE,
            )

    def _load_stored_secrets(self) -> dict[str, str]:
        path = self.data_dir / SECRETS_FILE
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("密钥文件 %s 无法解析，将重新生成", path)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def _merge_secrets(self, updates: dict[str, str]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = self._load_stored_secrets()
        payload.update(updates)
        path = self.data_dir / SECRETS_FILE
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:  # Windows 上 chmod 作用有限，但至少不要让它在 Unix 上裸奔
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover
            pass

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        if self.keep_session_logs:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if self.is_sqlite:
            # sqlite+aiosqlite:///./var/dszoon.db -> 确保 ./var 存在
            url_path = self.database_url.split("///", 1)[-1]
            if url_path and url_path != ":memory:":
                Path(url_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _new_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def configure_logging(level: int | None = None) -> None:
    """极简日志配置：统一格式，避免 uvicorn 与业务日志两套风格。"""
    logging.basicConfig(
        level=level or logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.resolve_secrets()
    return settings
