"""对 SSH 私钥等敏感字段做对称加密后再落库。"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, secret_key: str):
        if not secret_key:
            raise ValueError("SECRET_KEY 未设置")
        digest = hashlib.sha256(("terminator-agent:" + secret_key).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: str | None) -> str | None:
        if not plaintext:
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("解密失败：SECRET_KEY 可能已变更") from e
