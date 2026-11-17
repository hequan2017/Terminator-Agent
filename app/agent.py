"""运维 Agent：大模型 + 工具调用循环 + 人工审批。

一轮对话的流程：
  用户消息 → LLM → (工具调用 → 风险判断 → [人工审批] → SSH 执行 → 结果回填 LLM)* → 最终回答

所有对前端的输出都通过 emit(event) 回调，审批通过 approver(request) 回调等待用户决定。
"""
import asyncio
import json
import time
from typing import Awaitable, Callable

from .config import settings
from .db import Database
from .deploy import DeployEngine
from .llm import LLMClient, LLMError, ToolCall
from .risk import Risk, classify
from .ssh import SSHManager

Emit = Callable[[dict], Awaitable[None]]
# 返回 (是否批准, 用户附言)
Approver = Callable[[dict], Awaitable[tuple[bool, str]]]

MAX_TOOL_OUTPUT_FOR_LLM = 12_000
MAX_HISTORY_CHARS = 150_000

SYSTEM_PROMPT = """你是 Terminator，一名资深 Linux 运维工程师助手，通过工具在用户的服务器上排查问题、执行运维操作和发版。

## 工作方式
1. 先理解问题，再动手。信息不足时优先用只读命令收集证据（日志、进程、资源、配置），不要凭空猜测。
2. 每一步都说明你要做什么、为什么。命令尽量精确、输出尽量精简（用 head/tail/grep/--no-pager/-n 等控制输出量）。
3. 根据证据给出结论：问题现象 → 根因 → 解决方案。需要改动系统时，给出具体命令并说明影响与回退办法。
4. 回答使用简体中文，结构清晰，善用 Markdown（代码块、列表、表格）。

## 安全规则（必须遵守）
- 只读命令会自动执行；会修改系统的命令（重启服务、删除文件、修改配置、安装软件、kill 进程等）会由系统弹窗让用户审批。
- 不要试图绕过审批：不要把危险操作拆分、编码、写进脚本或借助 $()、eval 等方式隐藏。
- 用户拒绝某个操作后，不要换一种写法重复执行同样的操作；应询问用户或提供替代方案。
- 禁止执行破坏性极强的命令（如 rm -rf /、mkfs、dd 写裸盘），系统也会直接拦截。
- 执行修改操作前，先确认目标（主机、路径、服务名）准确无误；尽量先备份（如 cp xx xx.bak.$(date +%F)）。
- 不要读取或输出私钥、密码等敏感信息的完整内容。
- 命令必须是非交互式的（例如用 top -b -n 1 而不是 top，用 --no-pager，避免 vim/less 等）。

## 发版
- 使用 deploy 工具发版，它会按应用预先配置的脚本在目标主机上滚动执行，并在每台主机后做健康检查。
- 发版前先用 list_apps 确认应用名和目标主机；用户没给版本号时要先问清楚，不要自己编造版本号。
- 发版失败时，查看日志分析原因，并询问用户是否需要回滚（rollback 工具）。

## 当前可管理的主机
{hosts}

## 当前已配置的应用
{apps}

当前时间：{now}
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在一台或多台主机上执行 shell 命令（bash，非交互），返回退出码和输出。"
                           "多台主机会并行执行。只读命令自动执行，修改类命令需要用户审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hosts": {"type": "array", "items": {"type": "string"},
                              "description": "主机名称列表（使用主机列表中的 name 字段）"},
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "purpose": {"type": "string", "description": "一句话说明执行目的，会展示给用户"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
                },
                "required": ["hosts", "command", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_hosts",
            "description": "列出所有可管理的主机及其标签、描述。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_apps",
            "description": "列出已配置发版的应用、目标主机、以及最近的发版记录。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy",
            "description": "按应用配置的发版脚本，将指定版本发布到目标主机（逐台滚动，失败即停止）。需要用户审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "应用名称"},
                    "version": {"type": "string", "description": "要发布的版本号 / tag / commit"},
                    "hosts": {"type": "array", "items": {"type": "string"},
                              "description": "可选，只发布到其中部分主机；不填则发布到应用的全部目标主机"},
                },
                "required": ["app", "version"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback",
            "description": "执行应用的回滚脚本，回到指定版本（不填则回到上一个成功发布的版本）。需要用户审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "应用名称"},
                    "version": {"type": "string", "description": "可选，回滚到的目标版本"},
                    "hosts": {"type": "array", "items": {"type": "string"}, "description": "可选，部分主机"},
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deployment_history",
            "description": "查询某个应用的发版历史，或查看某次发版的详细日志。",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "应用名称（查询历史时使用）"},
                    "deployment_id": {"type": "integer", "description": "发版记录 ID（查看详细日志时使用）"},
                },
            },
        },
    },
]


def truncate_middle(text: str, limit: int = MAX_TOOL_OUTPUT_FOR_LLM) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n...[中间省略 {len(text) - limit} 字符]...\n\n{text[-tail:]}"


def to_llm_messages(history: list[dict]) -> list[dict]:
    """去掉仅供前端展示的字段，并在超长时从最早的完整轮次开始丢弃。"""
    cleaned = [{k: v for k, v in m.items() if not k.startswith("_")} for m in history]
    total = sum(len(json.dumps(m, ensure_ascii=False)) for m in cleaned)
    while total > MAX_HISTORY_CHARS:
        # 找到第二个 user 消息，丢弃它之前的所有消息（保证 tool_call 配对完整）
        user_idx = [i for i, m in enumerate(cleaned) if m["role"] == "user"]
        if len(user_idx) < 2:
            break
        dropped = cleaned[:user_idx[1]]
        cleaned = cleaned[user_idx[1]:]
        total -= sum(len(json.dumps(m, ensure_ascii=False)) for m in dropped)
    return cleaned


class Agent:
    def __init__(self, db: Database, ssh: SSHManager, deployer: DeployEngine, llm: LLMClient):
        self.db = db
        self.ssh = ssh
        self.deployer = deployer
        self.llm = llm

    # ---------- 上下文 ----------
    def _system_prompt(self) -> str:
        hosts = self.db.list_hosts()
        host_lines = "\n".join(
            f"- {h['name']}: {h['username']}@{h['hostname']}:{h['port']}"
            + (f"  标签[{h['tags']}]" if h["tags"] else "")
            + (f"  {h['description']}" if h["description"] else "")
            for h in hosts
        ) or "（暂无，请提醒用户先在「服务器」页面添加主机）"
        id2name = {h["id"]: h["name"] for h in hosts}
        apps = self.db.list_apps()
        app_lines = "\n".join(
            f"- {a['name']}: 目标主机 {', '.join(id2name.get(i, '?') for i in a['host_ids']) or '无'}"
            + (f"；{a['description']}" if a["description"] else "")
            + ("；支持回滚" if a["rollback_script"].strip() else "")
            for a in apps
        ) or "（暂无）"
        return SYSTEM_PROMPT.format(hosts=host_lines, apps=app_lines, now=time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---------- 主循环 ----------
    async def run_turn(self, conv_id: int, user_text: str, emit: Emit, approver: Approver) -> None:
        user_msg = {"role": "user", "content": user_text}
        self.db.add_message(conv_id, user_msg)
        history = self.db.get_messages(conv_id)

        conv = self.db.get_conversation(conv_id)
        if conv and conv["title"] == "新对话":
            title = user_text.strip().splitlines()[0][:30] if user_text.strip() else "新对话"
            self.db.rename_conversation(conv_id, title)
            await emit({"type": "title", "conversation_id": conv_id, "title": title})

        try:
            for _step in range(settings.agent_max_steps):
                messages = [{"role": "system", "content": self._system_prompt()}] + to_llm_messages(history)
                response = None
                await emit({"type": "assistant_start"})
                async for ev in self.llm.stream(messages, TOOLS):
                    if ev["type"] == "delta":
                        await emit({"type": "assistant_delta", "text": ev["text"]})
                    elif ev["type"] == "final":
                        response = ev["response"]
                assert response is not None

                assistant_msg: dict = {"role": "assistant", "content": response.content or None}
                if response.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": tc.arguments or "{}"}}
                        for tc in response.tool_calls
                    ]
                self.db.add_message(conv_id, assistant_msg)
                history.append(assistant_msg)
                await emit({"type": "assistant_end", "content": response.content or ""})

                if not response.tool_calls:
                    return

                for tc in response.tool_calls:
                    content, meta = await self._run_tool(tc, emit, approver)
                    tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": content, "_meta": meta}
                    self.db.add_message(conv_id, tool_msg)
                    history.append(tool_msg)

            note = f"已达到单轮最多 {settings.agent_max_steps} 次工具调用的上限，先停在这里。需要我继续吗？"
            msg = {"role": "assistant", "content": note}
            self.db.add_message(conv_id, msg)
            await emit({"type": "assistant_start"})
            await emit({"type": "assistant_delta", "text": note})
            await emit({"type": "assistant_end", "content": note})
        except LLMError as e:
            await emit({"type": "error", "message": str(e)})
        except asyncio.CancelledError:
            # 补齐未完成的 tool_call，保证下次请求时消息结构合法
            self._repair_dangling_tool_calls(conv_id)
            raise

    def _repair_dangling_tool_calls(self, conv_id: int) -> None:
        history = self.db.get_messages(conv_id)
        answered = {m.get("tool_call_id") for m in history if m["role"] == "tool"}
        for m in history:
            for tc in m.get("tool_calls") or []:
                if tc["id"] not in answered:
                    self.db.add_message(conv_id, {
                        "role": "tool", "tool_call_id": tc["id"], "content": "用户中止了本次操作。",
                        "_meta": {"tool": tc["function"]["name"], "status": "cancelled"},
                    })

    # ---------- 工具分发 ----------
    async def _run_tool(self, tc: ToolCall, emit: Emit, approver: Approver) -> tuple[str, dict]:
        args = tc.parsed_args()
        base = {"call_id": tc.id, "tool": tc.name, "args": args}
        handlers = {
            "run_command": self._tool_run_command,
            "list_hosts": self._tool_list_hosts,
            "list_apps": self._tool_list_apps,
            "deploy": self._tool_deploy,
            "rollback": self._tool_rollback,
            "deployment_history": self._tool_history,
        }
        handler = handlers.get(tc.name)
        if handler is None:
            meta = {**base, "status": "error"}
            await emit({"type": "tool_result", **meta, "summary": f"未知工具 {tc.name}"})
            return f"错误：不存在名为 {tc.name} 的工具", meta
        try:
            return await handler(args, base, emit, approver)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            meta = {**base, "status": "error"}
            await emit({"type": "tool_result", **meta, "summary": str(e)})
            return f"工具执行出错：{e}", meta

    async def _ask(self, base: dict, emit: Emit, approver: Approver, **details) -> tuple[bool, str]:
        request = {**base, **details}
        await emit({"type": "approval_request", **request})
        approved, note = await approver(request)
        await emit({"type": "approval_result", "call_id": base["call_id"], "approved": approved, "note": note})
        return approved, note

    def _resolve_hosts(self, names: list[str]) -> list[dict]:
        if not names:
            raise ValueError("请至少指定一台主机")
        hosts, missing = [], []
        for n in names:
            h = self.db.get_host_by_name(n)
            (hosts.append(h) if h else missing.append(n))
        if missing:
            known = ", ".join(h["name"] for h in self.db.list_hosts()) or "无"
            raise ValueError(f"主机不存在：{', '.join(missing)}。可用主机：{known}")
        return hosts

    async def _tool_run_command(self, args, base, emit, approver):
        command = (args.get("command") or "").strip()
        names = args.get("hosts") or []
        if isinstance(names, str):
            names = [names]
        hosts = self._resolve_hosts(names)
        timeout = max(1, min(int(args.get("timeout") or settings.command_timeout), 600))
        risk = classify(command)
        host_names = [h["name"] for h in hosts]
        base = {**base, "hosts": host_names, "command": command, "purpose": args.get("purpose", ""),
                "risk": risk.level.value, "reason": risk.reason}
        await emit({"type": "tool_start", **base})

        if risk.level == Risk.FORBIDDEN:
            for h in host_names:
                self.db.add_audit("agent", "chat", h, command, risk.level.value, "blocked")
            meta = {**base, "status": "blocked"}
            await emit({"type": "tool_result", **meta, "summary": f"已拦截：{risk.reason}"})
            return f"该命令被安全策略拦截（{risk.reason}），禁止执行。请换用更安全、范围更小的方式。", meta

        decision = "auto"
        if risk.level == Risk.APPROVE:
            approved, note = await self._ask(base, emit, approver, kind="command")
            if not approved:
                for h in host_names:
                    self.db.add_audit("agent", "chat", h, command, risk.level.value, "rejected")
                meta = {**base, "status": "rejected", "note": note}
                await emit({"type": "tool_result", **meta, "summary": "用户拒绝执行"})
                reply = "用户拒绝执行该命令。" + (f"用户说明：{note}" if note else "请询问用户原因或提供替代方案，不要重复尝试。")
                return reply, meta
            decision = "approved"

        async def run_one(host):
            async def on_output(text):
                await emit({"type": "tool_output", "call_id": base["call_id"], "host": host["name"], "text": text})
            res = await self.ssh.run(host, command, timeout=timeout, on_output=on_output)
            self.db.add_audit("agent", "chat", host["name"], command, risk.level.value, decision,
                              res.exit_code, res.combined())
            return host, res

        results = await asyncio.gather(*(run_one(h) for h in hosts))
        per_host = []
        parts = []
        for host, res in results:
            status = "ok" if res.ok else ("timeout" if res.timed_out else ("error" if res.error else "failed"))
            per_host.append({"host": host["name"], "exit_code": res.exit_code, "status": status,
                             "duration": round(res.duration, 2), "output": truncate_middle(res.combined(), 50_000)})
            body = truncate_middle(res.combined() or "(无输出)", MAX_TOOL_OUTPUT_FOR_LLM // max(1, len(hosts)))
            parts.append(f"### {host['name']}  exit_code={res.exit_code}  耗时 {res.duration:.1f}s\n{body}")
        all_ok = all(r["status"] == "ok" for r in per_host)
        meta = {**base, "status": "ok" if all_ok else "partial", "results": per_host}
        await emit({"type": "tool_result", **meta})
        return "\n\n".join(parts), meta

    async def _tool_list_hosts(self, args, base, emit, approver):
        await emit({"type": "tool_start", **base})
        hosts = [{"name": h["name"], "address": f"{h['username']}@{h['hostname']}:{h['port']}",
                  "tags": h["tags"], "description": h["description"]} for h in self.db.list_hosts()]
        meta = {**base, "status": "ok"}
        await emit({"type": "tool_result", **meta, "summary": f"共 {len(hosts)} 台主机"})
        return json.dumps(hosts, ensure_ascii=False, indent=1), meta

    async def _tool_list_apps(self, args, base, emit, approver):
        await emit({"type": "tool_start", **base})
        id2name = {h["id"]: h["name"] for h in self.db.list_hosts()}
        apps = []
        for a in self.db.list_apps():
            recent = self.db.list_deployments(a["id"], limit=3)
            apps.append({
                "name": a["name"], "description": a["description"],
                "hosts": [id2name.get(i, f"#{i}(已删除)") for i in a["host_ids"]],
                "has_rollback": bool(a["rollback_script"].strip()),
                "has_health_check": bool(a["health_check"].strip()),
                "recent_deployments": [
                    {"id": d["id"], "action": d["action"], "version": d["version"], "status": d["status"],
                     "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["created_at"]))}
                    for d in recent
                ],
            })
        meta = {**base, "status": "ok"}
        await emit({"type": "tool_result", **meta, "summary": f"共 {len(apps)} 个应用"})
        return json.dumps(apps, ensure_ascii=False, indent=1), meta

    async def _tool_deploy(self, args, base, emit, approver, action: str = "deploy"):
        app_name = args.get("app") or ""
        app = self.db.get_app_by_name(app_name)
        if not app:
            raise ValueError(f"应用不存在：{app_name}。已有应用：{', '.join(a['name'] for a in self.db.list_apps()) or '无'}")
        names = args.get("hosts") or None
        if isinstance(names, str):
            names = [names]
        hosts = self.deployer.resolve_hosts(app, names)
        version = (args.get("version") or "").strip()
        if action == "rollback" and not version:
            latest = self.db.list_deployments(app["id"], limit=1)
            before = latest[0]["id"] if latest else None
            version = self.db.last_successful_version(app["id"], before_id=before) or ""
            if not version:
                raise ValueError("找不到可回滚的历史成功版本，请明确指定 version")
        label = "回滚" if action == "rollback" else "发版"
        base = {**base, "app": app["name"], "version": version, "hosts": [h["name"] for h in hosts],
                "action": action, "risk": "approve", "reason": f"{label}会修改线上服务",
                "purpose": f"{label} {app['name']} → {version}"}
        await emit({"type": "tool_start", **base})

        approved, note = await self._ask(base, emit, approver, kind=action)
        if not approved:
            meta = {**base, "status": "rejected", "note": note}
            await emit({"type": "tool_result", **meta, "summary": "用户拒绝"})
            return f"用户拒绝了本次{label}。" + (f"用户说明：{note}" if note else ""), meta

        dep_id = self.deployer.create(app, version, hosts, action=action, triggered_by="agent")
        await emit({"type": "deployment_started", "call_id": base["call_id"], "deployment_id": dep_id})

        async def on_event(ev):
            if ev["type"] == "log":
                await emit({"type": "tool_output", "call_id": base["call_id"], "host": "deploy", "text": ev["text"]})

        task = self.deployer.start(dep_id, on_event)
        summary = await task
        dep = self.db.get_deployment(dep_id)
        status = summary["status"]
        meta = {**base, "status": "ok" if status == "success" else "failed", "deployment_id": dep_id,
                "summary": f"{label}{'成功' if status == 'success' else '失败'}（#{dep_id}）"}
        await emit({"type": "tool_result", **meta})
        return (f"{label}记录 #{dep_id}，结果：{status}\n各主机：{json.dumps(summary['hosts'], ensure_ascii=False)}\n"
                f"日志：\n{truncate_middle(dep['log'])}"), meta

    async def _tool_rollback(self, args, base, emit, approver):
        return await self._tool_deploy(args, base, emit, approver, action="rollback")

    async def _tool_history(self, args, base, emit, approver):
        await emit({"type": "tool_start", **base})
        if args.get("deployment_id"):
            dep = self.db.get_deployment(int(args["deployment_id"]))
            if not dep:
                raise ValueError("发版记录不存在")
            meta = {**base, "status": "ok"}
            await emit({"type": "tool_result", **meta, "summary": f"发版 #{dep['id']} 日志"})
            return (f"#{dep['id']} {dep['app_name']} {dep['action']} {dep['version']} 状态={dep['status']}\n"
                    f"{truncate_middle(dep['log'])}"), meta
        app_id = None
        if args.get("app"):
            app = self.db.get_app_by_name(args["app"])
            if not app:
                raise ValueError(f"应用不存在：{args['app']}")
            app_id = app["id"]
        rows = self.db.list_deployments(app_id, limit=20)
        data = [{"id": d["id"], "app": d["app_name"], "action": d["action"], "version": d["version"],
                 "status": d["status"], "by": d["triggered_by"],
                 "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"]))} for d in rows]
        meta = {**base, "status": "ok"}
        await emit({"type": "tool_result", **meta, "summary": f"{len(data)} 条记录"})
        return json.dumps(data, ensure_ascii=False, indent=1), meta
