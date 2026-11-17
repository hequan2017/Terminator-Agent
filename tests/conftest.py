"""测试夹具：进程内 SSH 服务端（真实密钥认证，命令交给本机 bash 执行）+ 脚本化假 LLM。"""
import asyncio
import shutil

import asyncssh
import pytest

from app.crypto import SecretBox
from app.db import Database
from app.deploy import DeployEngine
from app.llm import LLMResponse, ToolCall
from app.ssh import SSHManager

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="需要本机 bash")


async def _handle_process(process: asyncssh.SSHServerProcess):
    """把客户端的命令交给本机 bash 执行，并把 stdin/stdout/stderr 接起来。"""
    proc = await asyncio.create_subprocess_exec(
        BASH, "-c", process.command or "true",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def feed():
        try:
            while True:
                data = await process.stdin.read(65536)
                if not data:
                    break
                proc.stdin.write(data.encode() if isinstance(data, str) else data)
                await proc.stdin.drain()
        finally:
            proc.stdin.close()

    async def pipe(src, dst):
        while True:
            chunk = await src.read(4096)
            if not chunk:
                break
            dst.write(chunk.decode("utf-8", "replace"))

    await asyncio.gather(feed(), pipe(proc.stdout, process.stdout), pipe(proc.stderr, process.stderr))
    code = await proc.wait()
    process.exit(code)


class SSHTestServer:
    def __init__(self):
        self.client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.server = None
        self.port = None

    async def start(self):
        authorized = asyncssh.import_authorized_keys(self.client_key.export_public_key().decode())
        self.server = await asyncssh.create_server(
            asyncssh.SSHServer, "127.0.0.1", 0,
            server_host_keys=[self.host_key], authorized_client_keys=authorized,
            process_factory=_handle_process, encoding="utf-8",
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        # 不等待 wait_closed：它会等所有客户端断开，而客户端由 ssh fixture 负责关闭
        self.server.close()

    def private_key_text(self) -> str:
        return self.client_key.export_private_key().decode()


@pytest.fixture
async def ssh_server():
    srv = SSHTestServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
def box():
    return SecretBox("test-secret")


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture
async def ssh(box):
    mgr = SSHManager(box)
    yield mgr
    await mgr.close_all()


@pytest.fixture
def deployer(db, ssh):
    return DeployEngine(db, ssh)


def add_host(db, box, srv: SSHTestServer, name="web-01") -> dict:
    hid = db.create_host({
        "name": name, "hostname": "127.0.0.1", "port": srv.port, "username": "tester",
        "private_key_enc": box.encrypt(srv.private_key_text()),
    })
    return db.get_host(hid)


class FakeLLM:
    """按预设剧本依次返回响应；记录每次收到的 messages 以便断言。"""

    def __init__(self, script: list[LLMResponse]):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def stream(self, messages, tools):
        self.calls.append(messages)
        resp = self.script.pop(0)
        if resp.content:
            for i in range(0, len(resp.content), 5):
                yield {"type": "delta", "text": resp.content[i:i + 5]}
        yield {"type": "final", "response": resp}


def tool_call(name: str, args: dict, call_id: str = "c1") -> ToolCall:
    import json
    return ToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))

