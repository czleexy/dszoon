"""设备凭据的加解密。

设计要点：

* 只用一个对称密钥（Fernet = AES128-CBC + HMAC），密钥来自 ``DSZOON_SECRET_KEY``
  或 ``data_dir/.secrets.json``；**密钥丢失 = 所有设备密码都要重录**。
* 密文里带时间戳（Fernet 自带），但不做自动轮换——轮换需要重写全部凭据，
  属于 P2/运维流程，不是启动时的动作。
* 明文只在"构造 DeviceTarget 的那一刻"存在，绝不进日志、绝不进 API 响应。
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["CredentialCipher", "CredentialCryptoError", "generate_key"]


class CredentialCryptoError(Exception):
    """凭据解密失败（通常是换了 secret_key，或密钥文件损坏）。"""


def generate_key() -> str:
    """生成一个可直接放入 ``DSZOON_SECRET_KEY`` 的 Fernet 密钥。"""
    return Fernet.generate_key().decode()


class CredentialCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise CredentialCryptoError("secret_key 为空，无法加解密设备凭据")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise CredentialCryptoError(
                "secret_key 不是合法的 Fernet 密钥（应为 base64 编码的 32 字节）。"
                f"可用 `python -c \"from app.core.crypto import generate_key; "
                f"print(generate_key())\"` 生成一个。原始错误：{exc}"
            ) from exc

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None or plaintext == "":
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise CredentialCryptoError(
                "凭据解密失败：secret_key 与写入时不一致（或密钥文件被替换/损坏）。"
                "请恢复原密钥，或重新录入该设备的密码。"
            ) from exc

    def rotate(self, token: str | None) -> str | None:
        """用（可能已轮换的）新密钥重新加密一条旧密文。

        密钥轮换时要对每条凭据调用：先用旧密钥解，再用新密钥加。
        本方法只负责"用当前密钥重新加密同一条明文"，旧密钥的解密由调用方负责。
        """
        return self.encrypt(self.decrypt(token))

    # ------------------------------------------------------------------
    # 结构化敏感配置（通知渠道的 SMTP 口令、机器人密钥等）
    # ------------------------------------------------------------------
    def encrypt_json(self, payload: dict[str, Any]) -> str | None:
        """把一组敏感配置序列化后整体加密。

        比"给每个密钥开一列"更省事，也比塞进非加密的 config JSON 更安全——
        查 config 时不会顺带把口令读出来。``sort_keys`` 让同一份内容产生相同的
        密文输入（Fernet 本身每次随机化，这里只是让调试时更好比对）。
        """
        if not payload:
            return None
        return self.encrypt(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def decrypt_json(self, token: str | None) -> dict[str, Any]:
        """解密结构化敏感配置。格式坏掉时返回空字典而不是抛异常。"""
        if not token:
            return {}
        raw = self.decrypt(token)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialCryptoError(
                "敏感配置不是合法 JSON（密文可能来自不兼容的旧版本）"
            ) from exc
        return data if isinstance(data, dict) else {}
