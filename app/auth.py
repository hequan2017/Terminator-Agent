"""登录认证：单管理员账号 + 签名 Cookie 会话。"""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "terminator_session"
SESSION_MAX_AGE = 7 * 24 * 3600


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    expected = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(expected, digest_hex)


class Auth:
    def __init__(self, secret_key: str):
        self._serializer = URLSafeTimedSerializer(secret_key, salt="terminator-session")
        self._failures: dict[str, list[float]] = defaultdict(list)

    def issue(self, username: str) -> str:
        return self._serializer.dumps({"u": username})

    def read(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None
        return data.get("u")

    # 简单的登录防爆破：同一 IP 10 分钟内失败 5 次则锁定
    def check_rate(self, ip: str) -> None:
        now = time.time()
        recent = [t for t in self._failures[ip] if now - t < 600]
        self._failures[ip] = recent
        if len(recent) >= 5:
            raise HTTPException(429, "登录失败次数过多，请 10 分钟后再试")

    def record_failure(self, ip: str) -> None:
        self._failures[ip].append(time.time())

    def require(self, request: Request) -> str:
        user = self.read(request.cookies.get(SESSION_COOKIE))
        if not user:
            raise HTTPException(401, "未登录")
        return user

    def ws_user(self, ws: WebSocket) -> str | None:
        return self.read(ws.cookies.get(SESSION_COOKIE))
