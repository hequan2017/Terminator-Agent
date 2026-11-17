"""FastAPI 入口：REST 接口 + WebSocket（对话 / 发版日志） + 静态前端。"""
import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncssh
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .auth import SESSION_COOKIE, SESSION_MAX_AGE, Auth, hash_password, verify_password
from .config import BASE_DIR, settings
from .crypto import SecretBox
from .db import Database
from .deploy import DeployEngine, validate_version
from .llm import OpenAICompatClient
from .ssh import SSHManager

log = logging.getLogger("terminator")
STATIC_DIR = BASE_DIR / "static"


# ---------------- 启动初始化 ----------------

def _ensure_secret_key() -> str:
    if settings.secret_key:
        return settings.secret_key
    key_file = Path(settings.db_path).parent / "secret.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_urlsafe(48)
    key_file.write_text(key)
    log.warning("未设置 SECRET_KEY，已自动生成并保存到 %s（请妥善备份，丢失后已保存的私钥无法解密）", key_file)
    return key


def _ensure_admin(db: Database) -> None:
    password = settings.admin_password
    existing = db.get_user(settings.admin_username)
    if password:
        if not existing or not verify_password(password, existing["password_hash"]):
            db.upsert_user(settings.admin_username, hash_password(password))
        return
    if not existing:
        password = secrets.token_urlsafe(12)
        db.upsert_user(settings.admin_username, hash_password(password))
        banner = f"初始管理员账号：{settings.admin_username} / {password}（可在 .env 中设置 ADMIN_PASSWORD）"
        print("\n" + "=" * len(banner) * 2 + f"\n{banner}\n" + "=" * len(banner) * 2 + "\n", flush=True)


class Services:
    def __init__(self, db_path: str | None = None, llm=None):
        secret = _ensure_secret_key()
        self.db = Database(db_path or settings.db_path)
        self.box = SecretBox(secret)
        self.auth = Auth(secret)
        self.ssh = SSHManager(self.box)
        self.deployer = DeployEngine(self.db, self.ssh)
        self.agent = Agent(self.db, self.ssh, self.deployer, llm or OpenAICompatClient())
        _ensure_admin(self.db)


def create_app(services: Services | None = None) -> FastAPI:
    holder: dict[str, Services] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        holder["s"] = services or Services()
        yield
        await holder["s"].ssh.close_all()

    app = FastAPI(title="Terminator Agent", lifespan=lifespan)

    def svc() -> Services:
        return holder["s"]

    def current_user(request: Request) -> str:
        return svc().auth.require(request)

    # ---------------- 认证 ----------------
    class LoginBody(BaseModel):
        username: str
        password: str

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response):
        s = svc()
        ip = request.client.host if request.client else "?"
        s.auth.check_rate(ip)
        user = s.db.get_user(body.username)
        if not user or not verify_password(body.password, user["password_hash"]):
            s.auth.record_failure(ip)
            raise HTTPException(401, "用户名或密码错误")
        response.set_cookie(SESSION_COOKIE, s.auth.issue(body.username), max_age=SESSION_MAX_AGE,
                            httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return {"username": body.username}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        return {"username": user, "llm_configured": bool(settings.llm_api_key), "llm_model": settings.llm_model}

    # ---------------- 主机 ----------------
    class HostBody(BaseModel):
        name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
        hostname: str = Field(min_length=1, max_length=255)
        port: int = Field(22, ge=1, le=65535)
        username: str = Field(min_length=1, max_length=64)
        private_key: str | None = None
        passphrase: str | None = None
        password: str | None = None
        tags: str = ""
        description: str = ""

    def public_host(h: dict) -> dict:
        return {k: h[k] for k in ("id", "name", "hostname", "port", "username", "tags", "description")} | {
            "has_key": bool(h["private_key_enc"]), "has_password": bool(h["password_enc"]),
        }

    def _validate_key(key: str | None, passphrase: str | None) -> None:
        if not key:
            return
        try:
            asyncssh.import_private_key(key, passphrase or None)
        except (asyncssh.KeyImportError, ValueError) as e:
            raise HTTPException(400, f"私钥无法解析（格式错误或 passphrase 不正确）：{e}")

    @app.get("/api/hosts")
    def list_hosts(_u: str = Depends(current_user)):
        return [public_host(h) for h in svc().db.list_hosts()]

    @app.post("/api/hosts")
    def create_host(body: HostBody, _u: str = Depends(current_user)):
        s = svc()
        if s.db.get_host_by_name(body.name):
            raise HTTPException(400, f"主机名 {body.name} 已存在")
        key = (body.private_key or "").strip() or None
        if key:
            key += "\n"
        if not key and not body.password:
            raise HTTPException(400, "请提供 SSH 私钥或密码")
        _validate_key(key, body.passphrase)
        host_id = s.db.create_host({
            **body.model_dump(exclude={"private_key", "passphrase", "password"}),
            "auth_type": "key" if key else "password",
            "private_key_enc": s.box.encrypt(key),
            "passphrase_enc": s.box.encrypt(body.passphrase),
            "password_enc": s.box.encrypt(body.password),
        })
        return public_host(s.db.get_host(host_id))

    @app.put("/api/hosts/{host_id}")
    def update_host(host_id: int, body: HostBody, _u: str = Depends(current_user)):
        s = svc()
        h = s.db.get_host(host_id)
        if not h:
            raise HTTPException(404, "主机不存在")
        other = s.db.get_host_by_name(body.name)
        if other and other["id"] != host_id:
            raise HTTPException(400, f"主机名 {body.name} 已存在")
        data: dict[str, Any] = body.model_dump(exclude={"private_key", "passphrase", "password"})
        # 凭据字段：留空表示不修改
        key = (body.private_key or "").strip()
        if key:
            key += "\n"
            _validate_key(key, body.passphrase)
            data["private_key_enc"] = s.box.encrypt(key)
            data["passphrase_enc"] = s.box.encrypt(body.passphrase)
            data["auth_type"] = "key"
        if body.password:
            data["password_enc"] = s.box.encrypt(body.password)
        s.db.update_host(host_id, data)
        s.ssh.invalidate(host_id)
        return public_host(s.db.get_host(host_id))

    @app.delete("/api/hosts/{host_id}")
    def delete_host(host_id: int, _u: str = Depends(current_user)):
        s = svc()
        s.ssh.invalidate(host_id)
        s.db.delete_host(host_id)
        for a in s.db.list_apps():
            if host_id in a["host_ids"]:
                s.db.update_app(a["id"], {"host_ids": [i for i in a["host_ids"] if i != host_id]})
        return {"ok": True}

    @app.post("/api/hosts/{host_id}/test")
    async def test_host(host_id: int, _u: str = Depends(current_user)):
        s = svc()
        h = s.db.get_host(host_id)
        if not h:
            raise HTTPException(404, "主机不存在")
        s.ssh.invalidate(host_id)
        res = await s.ssh.test(h)
        return {"ok": res.ok, "output": res.combined(), "duration": round(res.duration, 2)}

    # ---------------- 应用 & 发版 ----------------
    class AppBody(BaseModel):
        name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
        description: str = ""
        host_ids: list[int] = []
        deploy_script: str = Field(min_length=1)
        rollback_script: str = ""
        health_check: str = ""

    def _check_hosts_exist(ids: list[int]) -> None:
        for i in ids:
            if not svc().db.get_host(i):
                raise HTTPException(400, f"主机 #{i} 不存在")

    @app.get("/api/apps")
    def list_apps(_u: str = Depends(current_user)):
        return svc().db.list_apps()

    @app.post("/api/apps")
    def create_app_(body: AppBody, _u: str = Depends(current_user)):
        s = svc()
        if s.db.get_app_by_name(body.name):
            raise HTTPException(400, f"应用 {body.name} 已存在")
        _check_hosts_exist(body.host_ids)
        return s.db.get_app(s.db.create_app(body.model_dump()))

    @app.put("/api/apps/{app_id}")
    def update_app(app_id: int, body: AppBody, _u: str = Depends(current_user)):
        s = svc()
        if not s.db.get_app(app_id):
            raise HTTPException(404, "应用不存在")
        other = s.db.get_app_by_name(body.name)
        if other and other["id"] != app_id:
            raise HTTPException(400, f"应用 {body.name} 已存在")
        _check_hosts_exist(body.host_ids)
        s.db.update_app(app_id, body.model_dump())
        return s.db.get_app(app_id)

    @app.delete("/api/apps/{app_id}")
    def delete_app(app_id: int, _u: str = Depends(current_user)):
        svc().db.delete_app(app_id)
        return {"ok": True}

    class DeployBody(BaseModel):
        version: str = ""
        hosts: list[str] | None = None

    def _start_deploy(app_id: int, body: DeployBody, action: str, user: str) -> dict:
        s = svc()
        app_ = s.db.get_app(app_id)
        if not app_:
            raise HTTPException(404, "应用不存在")
        version = body.version.strip()
        if action == "rollback" and not version:
            version = s.db.last_successful_version(app_id) or ""
        try:
            validate_version(version)
            hosts = s.deployer.resolve_hosts(app_, body.hosts)
            dep_id = s.deployer.create(app_, version, hosts, action=action, triggered_by=user)
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.deployer.start(dep_id)
        return s.db.get_deployment(dep_id)

    @app.post("/api/apps/{app_id}/deploy")
    async def deploy(app_id: int, body: DeployBody, user: str = Depends(current_user)):
        return _start_deploy(app_id, body, "deploy", user)

    @app.post("/api/apps/{app_id}/rollback")
    async def rollback(app_id: int, body: DeployBody, user: str = Depends(current_user)):
        return _start_deploy(app_id, body, "rollback", user)

    @app.get("/api/deployments")
    def list_deployments(app_id: int | None = None, _u: str = Depends(current_user)):
        return svc().db.list_deployments(app_id, limit=100)

    @app.get("/api/deployments/{dep_id}")
    def get_deployment(dep_id: int, _u: str = Depends(current_user)):
        dep = svc().db.get_deployment(dep_id)
        if not dep:
            raise HTTPException(404, "发版记录不存在")
        return dep

    @app.post("/api/deployments/{dep_id}/cancel")
    def cancel_deployment(dep_id: int, _u: str = Depends(current_user)):
        if not svc().deployer.cancel(dep_id):
            raise HTTPException(400, "该发版未在运行中")
        return {"ok": True}

    @app.websocket("/ws/deployments/{dep_id}")
    async def ws_deployment(ws: WebSocket, dep_id: int):
        s = svc()
        if not s.auth.ws_user(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        q = s.deployer.subscribe(dep_id)
        try:
            dep = s.db.get_deployment(dep_id)
            if not dep:
                await ws.send_json({"type": "error", "message": "发版记录不存在"})
                return
            await ws.send_json({"type": "snapshot", "deployment": dep})
            if dep["status"] in ("success", "failed", "cancelled"):
                return
            while True:
                ev = await q.get()
                await ws.send_json(ev)
                if ev.get("done"):
                    return
        except WebSocketDisconnect:
            pass
        finally:
            s.deployer.unsubscribe(dep_id, q)
            try:
                await ws.close()
            except RuntimeError:
                pass

    # ---------------- 对话 ----------------
    @app.get("/api/conversations")
    def list_conversations(_u: str = Depends(current_user)):
        return svc().db.list_conversations()

    @app.post("/api/conversations")
    def create_conversation(_u: str = Depends(current_user)):
        s = svc()
        return s.db.get_conversation(s.db.create_conversation())

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: int, _u: str = Depends(current_user)):
        svc().db.delete_conversation(conv_id)
        return {"ok": True}

    @app.get("/api/conversations/{conv_id}/messages")
    def get_messages(conv_id: int, _u: str = Depends(current_user)):
        s = svc()
        if not s.db.get_conversation(conv_id):
            raise HTTPException(404, "对话不存在")
        return s.db.get_messages(conv_id)

    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket):
        s = svc()
        user = s.auth.ws_user(ws)
        if not user:
            await ws.close(code=4401)
            return
        await ws.accept()
        pending: dict[str, asyncio.Future] = {}
        turn: asyncio.Task | None = None
        send_lock = asyncio.Lock()

        async def emit(event: dict) -> None:
            async with send_lock:
                await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))

        async def approver(request: dict) -> tuple[bool, str]:
            fut = asyncio.get_running_loop().create_future()
            pending[request["call_id"]] = fut
            try:
                return await fut
            finally:
                pending.pop(request["call_id"], None)

        async def run_turn(conv_id: int, text: str):
            try:
                await s.agent.run_turn(conv_id, text, emit, approver)
            except asyncio.CancelledError:
                await emit({"type": "stopped"})
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("agent turn failed")
                await emit({"type": "error", "message": f"内部错误：{e}"})
            finally:
                await emit({"type": "turn_end", "conversation_id": conv_id})

        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "chat":
                    if turn and not turn.done():
                        await emit({"type": "error", "message": "上一轮还在执行中，请稍候或点击停止"})
                        continue
                    conv_id = msg.get("conversation_id")
                    if not conv_id or not s.db.get_conversation(conv_id):
                        conv_id = s.db.create_conversation()
                        await emit({"type": "conversation", "conversation_id": conv_id})
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    turn = asyncio.create_task(run_turn(conv_id, text))
                elif kind == "approval":
                    fut = pending.get(msg.get("call_id"))
                    if fut and not fut.done():
                        fut.set_result((bool(msg.get("approved")), (msg.get("note") or "").strip()))
                elif kind == "stop":
                    if turn and not turn.done():
                        turn.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if turn and not turn.done():
                turn.cancel()

    # ---------------- 审计 ----------------
    @app.get("/api/audit")
    def audit(_u: str = Depends(current_user)):
        return svc().db.list_audit(300)

    # ---------------- 前端 ----------------
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
