"""SSH 连接与命令执行（基于 asyncssh）。

- 支持私钥（可带 passphrase）和密码两种认证；首选私钥
- 每台主机复用一条连接，断开后自动重连
- 输出流式回调，便于网页实时展示
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncssh

from .config import settings
from .crypto import SecretBox

MAX_OUTPUT = 200_000  # 单条命令最多保留的字符数

OutputCallback = Callable[[str], Awaitable[None]]


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    def combined(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append("[stderr]\n" + self.stderr)
        if self.error:
            parts.append(f"[error] {self.error}")
        if self.timed_out:
            parts.append("[timeout] 命令执行超时，已终止")
        return "\n".join(parts)


class SSHManager:
    def __init__(self, box: SecretBox):
        self._box = box
        self._conns: dict[int, asyncssh.SSHClientConnection] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _connect_kwargs(self, host: dict) -> dict:
        kwargs: dict = {
            "host": host["hostname"],
            "port": int(host.get("port") or 22),
            "username": host["username"],
            "connect_timeout": 15,
            "keepalive_interval": 30,
            # 只使用面板中录入的凭据，不读取运行机器上的 ~/.ssh/config
            "config": None,
        }
        if not settings.ssh_strict_host_key:
            kwargs["known_hosts"] = None
        key_text = self._box.decrypt(host.get("private_key_enc"))
        if key_text:
            passphrase = self._box.decrypt(host.get("passphrase_enc"))
            try:
                key = asyncssh.import_private_key(key_text, passphrase)
            except (asyncssh.KeyImportError, ValueError) as e:
                raise ValueError(f"私钥无法解析：{e}") from e
            kwargs["client_keys"] = [key]
            kwargs["agent_path"] = None
        else:
            kwargs["client_keys"] = None
        password = self._box.decrypt(host.get("password_enc"))
        if password:
            kwargs["password"] = password
        if not key_text and not password:
            raise ValueError("该主机没有配置私钥或密码")
        return kwargs

    async def _get_conn(self, host: dict) -> asyncssh.SSHClientConnection:
        hid = host["id"]
        lock = self._locks.setdefault(hid, asyncio.Lock())
        async with lock:
            conn = self._conns.get(hid)
            if conn is not None and not conn.is_closed():
                return conn
            conn = await asyncssh.connect(**self._connect_kwargs(host))
            self._conns[hid] = conn
            return conn

    def invalidate(self, host_id: int) -> None:
        conn = self._conns.pop(host_id, None)
        if conn is not None:
            conn.close()

    async def close_all(self) -> None:
        for hid in list(self._conns):
            self.invalidate(hid)

    async def run(self, host: dict, command: str, timeout: int | None = None,
                  on_output: OutputCallback | None = None, stdin: str | None = None) -> ExecResult:
        timeout = timeout or settings.command_timeout
        start = time.monotonic()
        for attempt in range(2):
            try:
                conn = await self._get_conn(host)
                return await self._run_on(conn, command, timeout, on_output, stdin, start)
            except asyncssh.PermissionDenied:
                # 注意：PermissionDenied 是 DisconnectError 的子类，必须先捕获
                self.invalidate(host["id"])
                return ExecResult(None, "", "", time.monotonic() - start,
                                  error="SSH 认证失败：私钥/密码被拒绝，请检查用户名和公钥是否已加入 authorized_keys")
            except (asyncssh.ConnectionLost, asyncssh.DisconnectError, BrokenPipeError, ConnectionResetError) as e:
                self.invalidate(host["id"])
                if attempt == 1:
                    return ExecResult(None, "", "", time.monotonic() - start, error=f"SSH 连接中断：{e}")
            except (OSError, asyncssh.Error, ValueError) as e:
                self.invalidate(host["id"])
                return ExecResult(None, "", "", time.monotonic() - start, error=f"SSH 连接失败：{e}")
        raise AssertionError("unreachable")

    async def _run_on(self, conn, command, timeout, on_output, stdin, start) -> ExecResult:
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        sizes = {"out": 0, "err": 0}

        proc = await conn.create_process(command, encoding="utf-8", errors="replace")
        if stdin is not None:
            proc.stdin.write(stdin)
        proc.stdin.write_eof()

        async def pump(stream, buf, key, prefix=""):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if sizes[key] < MAX_OUTPUT:
                    buf.append(chunk)
                    sizes[key] += len(chunk)
                if on_output:
                    await on_output(prefix + chunk if prefix else chunk)

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(pump(proc.stdout, stdout_buf, "out"), pump(proc.stderr, stderr_buf, "err")),
                timeout=timeout,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.terminate()
            except Exception:
                pass
            proc.close()

        def finalize(buf, size):
            text = "".join(buf)
            if size >= MAX_OUTPUT:
                text = text[:MAX_OUTPUT] + f"\n...[输出过长，已截断，共约 {size} 字符]"
            return text

        return ExecResult(
            exit_code=None if timed_out else proc.exit_status,
            stdout=finalize(stdout_buf, sizes["out"]),
            stderr=finalize(stderr_buf, sizes["err"]),
            duration=time.monotonic() - start,
            timed_out=timed_out,
        )

    async def test(self, host: dict) -> ExecResult:
        return await self.run(host, "echo ok && hostname && uname -sr && uptime", timeout=20)
