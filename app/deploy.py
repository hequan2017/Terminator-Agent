"""发版引擎。

每个应用配置：
- deploy_script   发版脚本，可使用变量 $VERSION / $APP / $HOST / $PREV_VERSION
- rollback_script 回滚脚本（可选），同样可用以上变量
- health_check    健康检查命令（可选），返回码 0 视为健康

执行策略：按主机顺序滚动发布，某台失败立即停止，后续主机不再发布。
脚本通过 stdin 交给远端 `bash -s` 执行，并开启 `set -euo pipefail`。
"""
import asyncio
import re
import shlex
import time
from typing import Awaitable, Callable

from .db import Database
from .ssh import SSHManager

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-+/:@]{0,127}$")

EventCallback = Callable[[dict], Awaitable[None]]


def validate_version(version: str) -> str:
    version = (version or "").strip()
    if not VERSION_RE.match(version):
        raise ValueError("版本号格式不合法：只允许字母、数字和 . _ - + / : @，长度不超过 128")
    return version


def build_script(script: str, env: dict[str, str]) -> str:
    exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
    return f"set -euo pipefail\n{exports}\n{script}\n"


class DeployEngine:
    def __init__(self, db: Database, ssh: SSHManager):
        self.db = db
        self.ssh = ssh
        self._running: dict[int, asyncio.Task] = {}
        self._subscribers: dict[int, list[asyncio.Queue]] = {}

    # ---------- 订阅（网页实时查看日志） ----------
    def subscribe(self, dep_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(dep_id, []).append(q)
        return q

    def unsubscribe(self, dep_id: int, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(dep_id, [])
        if q in subs:
            subs.remove(q)

    async def _emit(self, dep_id: int, event: dict, on_event: EventCallback | None) -> None:
        if event.get("type") == "log":
            self.db.update_deployment(dep_id, append_log=event["text"])
        for q in self._subscribers.get(dep_id, []):
            q.put_nowait(event)
        if on_event:
            await on_event(event)

    def is_running(self, dep_id: int) -> bool:
        task = self._running.get(dep_id)
        return task is not None and not task.done()

    # ---------- 发起 ----------
    def resolve_hosts(self, app: dict, host_names: list[str] | None) -> list[dict]:
        if host_names:
            hosts = []
            for name in host_names:
                h = self.db.get_host_by_name(name)
                if not h:
                    raise ValueError(f"主机不存在：{name}")
                if h["id"] not in app["host_ids"]:
                    raise ValueError(f"主机 {name} 不属于应用 {app['name']} 的目标主机")
                hosts.append(h)
            return hosts
        hosts = [self.db.get_host(hid) for hid in app["host_ids"]]
        hosts = [h for h in hosts if h]
        if not hosts:
            raise ValueError(f"应用 {app['name']} 没有配置目标主机")
        return hosts

    def create(self, app: dict, version: str, hosts: list[dict], action: str = "deploy",
               triggered_by: str = "user") -> int:
        if action == "rollback" and not app.get("rollback_script", "").strip():
            raise ValueError(f"应用 {app['name']} 未配置回滚脚本")
        version = validate_version(version)
        return self.db.create_deployment(app["id"], version, [h["id"] for h in hosts], action, triggered_by)

    def start(self, dep_id: int, on_event: EventCallback | None = None) -> asyncio.Task:
        task = asyncio.create_task(self.execute(dep_id, on_event))
        self._running[dep_id] = task
        task.add_done_callback(lambda _t: self._running.pop(dep_id, None))
        return task

    def cancel(self, dep_id: int) -> bool:
        task = self._running.get(dep_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    # ---------- 执行 ----------
    async def execute(self, dep_id: int, on_event: EventCallback | None = None) -> dict:
        dep = self.db.get_deployment(dep_id)
        app = self.db.get_app(dep["app_id"])
        action = dep["action"]
        script = app["rollback_script"] if action == "rollback" else app["deploy_script"]
        prev_version = self.db.last_successful_version(app["id"], before_id=dep_id) or ""
        hosts = [self.db.get_host(hid) for hid in dep["host_ids"]]

        label = "回滚" if action == "rollback" else "发版"
        self.db.update_deployment(dep_id, status="running")
        await self._emit(dep_id, {"type": "status", "status": "running"}, on_event)
        await self._emit(dep_id, {"type": "log", "text": f"=== 开始{label} {app['name']} 版本 {dep['version']}"
                                                          f"（上一成功版本：{prev_version or '无'}），"
                                                          f"共 {len(hosts)} 台主机 ===\n"}, on_event)
        results: list[dict] = []
        status = "success"
        try:
            for idx, host in enumerate(hosts, 1):
                if host is None:
                    status = "failed"
                    await self._emit(dep_id, {"type": "log", "text": "[错误] 目标主机已被删除\n"}, on_event)
                    break
                ok = await self._deploy_one(dep_id, app, dep["version"], prev_version, host, script, idx,
                                            len(hosts), on_event)
                results.append({"host": host["name"], "ok": ok})
                if not ok:
                    status = "failed"
                    remaining = [h["name"] for h in hosts[idx:] if h]
                    if remaining:
                        await self._emit(dep_id, {"type": "log", "text": f"[停止] {host['name']} 失败，"
                                                  f"后续主机未执行：{', '.join(remaining)}\n"}, on_event)
                    break
        except asyncio.CancelledError:
            status = "cancelled"
            await self._emit(dep_id, {"type": "log", "text": "\n[取消] 发版已被手动取消\n"}, on_event)
        except Exception as e:  # noqa: BLE001
            status = "failed"
            await self._emit(dep_id, {"type": "log", "text": f"\n[异常] {e}\n"}, on_event)

        self.db.update_deployment(dep_id, status=status, finished=True)
        summary_text = {"success": "成功", "failed": "失败", "cancelled": "已取消"}[status]
        await self._emit(dep_id, {"type": "log", "text": f"=== {label}{summary_text} ===\n"}, on_event)
        await self._emit(dep_id, {"type": "status", "status": status, "done": True}, on_event)
        return {"deployment_id": dep_id, "status": status, "hosts": results}

    async def _deploy_one(self, dep_id, app, version, prev_version, host, script, idx, total,
                          on_event) -> bool:
        from .config import settings

        async def log(text: str):
            await self._emit(dep_id, {"type": "log", "text": text}, on_event)

        await log(f"\n--- [{idx}/{total}] {host['name']} ({host['hostname']}) ---\n")
        env = {"VERSION": version, "APP": app["name"], "HOST": host["name"], "PREV_VERSION": prev_version}
        started = time.monotonic()
        result = await self.ssh.run(host, "bash -s", timeout=settings.deploy_timeout, on_output=log,
                                    stdin=build_script(script, env))
        self.db.add_audit("deploy", f"deployment#{dep_id}", host["name"], f"[{app['name']}] 发版脚本 {version}",
                          "approve", "approved", result.exit_code, result.combined())
        if result.error:
            await log(f"[错误] {result.error}\n")
            return False
        if result.timed_out:
            await log(f"[错误] 脚本执行超过 {settings.deploy_timeout}s，已终止\n")
            return False
        if result.exit_code != 0:
            await log(f"[失败] 脚本退出码 {result.exit_code}\n")
            return False
        await log(f"[完成] 脚本执行成功，用时 {time.monotonic() - started:.1f}s\n")

        check = (app.get("health_check") or "").strip()
        if check:
            for attempt in range(1, 6):
                hc = await self.ssh.run(host, "bash -s", timeout=30, stdin=build_script(check, env))
                if hc.ok:
                    await log(f"[健康检查] 通过（第 {attempt} 次）\n")
                    return True
                await log(f"[健康检查] 第 {attempt} 次未通过：{(hc.combined() or '').strip()[:300]}\n")
                await asyncio.sleep(3)
            await log("[健康检查] 多次重试仍未通过，判定发版失败\n")
            return False
        return True
