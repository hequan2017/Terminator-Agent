"""Agent 主循环测试：用脚本化 FakeLLM + 真实 SSH 服务端。"""
from conftest import FakeLLM, add_host, needs_bash, tool_call

from app.agent import Agent, to_llm_messages
from app.llm import LLMResponse

pytestmark = needs_bash


class Recorder:
    def __init__(self, approve=True, note=""):
        self.events = []
        self.approve = approve
        self.note = note
        self.approval_requests = []

    async def emit(self, ev):
        self.events.append(ev)

    async def approver(self, req):
        self.approval_requests.append(req)
        return self.approve, self.note

    def types(self):
        return [e["type"] for e in self.events]


def make_agent(db, ssh, deployer, script):
    llm = FakeLLM(script)
    return Agent(db, ssh, deployer, llm), llm


async def test_safe_command_runs_without_approval(db, box, ssh, deployer, ssh_server):
    add_host(db, box, ssh_server, "web-01")
    agent, llm = make_agent(db, ssh, deployer, [
        LLMResponse("先看看", [tool_call("run_command", {"hosts": ["web-01"], "command": "echo disk-ok",
                                                          "purpose": "检查"})]),
        LLMResponse("磁盘正常"),
    ])
    conv = db.create_conversation()
    rec = Recorder()
    await agent.run_turn(conv, "看下磁盘", rec.emit, rec.approver)

    assert rec.approval_requests == []
    outputs = "".join(e["text"] for e in rec.events if e["type"] == "tool_output")
    assert "disk-ok" in outputs
    # 工具结果被回填给 LLM
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert "disk-ok" in tool_msgs[0]["content"]
    assert all("_meta" not in m for m in llm.calls[1])
    # 对话持久化 + 标题
    roles = [m["role"] for m in db.get_messages(conv)]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert db.get_conversation(conv)["title"] == "看下磁盘"
    audit = db.list_audit()
    assert audit[0]["decision"] == "auto" and audit[0]["command"] == "echo disk-ok"


async def test_mutating_command_requires_approval_and_rejection_is_respected(db, box, ssh, deployer,
                                                                              ssh_server, tmp_path):
    add_host(db, box, ssh_server, "web-01")
    marker = tmp_path / "touched"
    cmd = f"touch '{marker.as_posix()}'"
    agent, llm = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("run_command", {"hosts": ["web-01"], "command": cmd, "purpose": "建文件"})]),
        LLMResponse("好的，不执行"),
    ])
    conv = db.create_conversation()
    rec = Recorder(approve=False, note="别动生产")
    await agent.run_turn(conv, "建个文件", rec.emit, rec.approver)

    assert len(rec.approval_requests) == 1
    assert rec.approval_requests[0]["risk"] == "approve"
    assert not marker.exists()
    tool_msg = [m for m in llm.calls[1] if m["role"] == "tool"][0]
    assert "拒绝" in tool_msg["content"] and "别动生产" in tool_msg["content"]
    assert db.list_audit()[0]["decision"] == "rejected"


async def test_approved_command_executes(db, box, ssh, deployer, ssh_server, tmp_path):
    add_host(db, box, ssh_server, "web-01")
    marker = tmp_path / "touched"
    agent, _ = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("run_command", {"hosts": ["web-01"], "command": f"touch '{marker.as_posix()}'",
                                                    "purpose": "建文件"})]),
        LLMResponse("完成"),
    ])
    rec = Recorder(approve=True)
    await agent.run_turn(db.create_conversation(), "建个文件", rec.emit, rec.approver)
    assert marker.exists()
    assert db.list_audit()[0]["decision"] == "approved"


async def test_forbidden_command_blocked_even_without_asking(db, box, ssh, deployer, ssh_server):
    add_host(db, box, ssh_server, "web-01")
    agent, llm = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("run_command", {"hosts": ["web-01"], "command": "rm -rf /", "purpose": "x"})]),
        LLMResponse("被拦截了"),
    ])
    rec = Recorder(approve=True)
    await agent.run_turn(db.create_conversation(), "清空", rec.emit, rec.approver)
    assert rec.approval_requests == []
    assert "拦截" in [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    assert db.list_audit()[0]["decision"] == "blocked"


async def test_multi_host_parallel(db, box, ssh, deployer, ssh_server):
    add_host(db, box, ssh_server, "web-01")
    add_host(db, box, ssh_server, "web-02")
    agent, llm = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("run_command", {"hosts": ["web-01", "web-02"], "command": "echo up",
                                                    "purpose": "检查"})]),
        LLMResponse("都正常"),
    ])
    rec = Recorder()
    await agent.run_turn(db.create_conversation(), "检查", rec.emit, rec.approver)
    content = [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    assert "### web-01" in content and "### web-02" in content


async def test_unknown_host_reported_to_llm(db, box, ssh, deployer, ssh_server):
    add_host(db, box, ssh_server, "web-01")
    agent, llm = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("run_command", {"hosts": ["nope"], "command": "ls", "purpose": "x"})]),
        LLMResponse("没有这台机器"),
    ])
    rec = Recorder()
    await agent.run_turn(db.create_conversation(), "x", rec.emit, rec.approver)
    content = [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    assert "主机不存在：nope" in content and "web-01" in content


async def test_deploy_via_chat_requires_approval(db, box, ssh, deployer, ssh_server):
    h = add_host(db, box, ssh_server, "web-01")
    db.create_app({"name": "order-api", "host_ids": [h["id"]], "deploy_script": "echo shipped $VERSION"})
    agent, llm = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("deploy", {"app": "order-api", "version": "v1.2.3"})]),
        LLMResponse("发版完成"),
    ])
    rec = Recorder(approve=True)
    await agent.run_turn(db.create_conversation(), "发 v1.2.3", rec.emit, rec.approver)
    assert rec.approval_requests[0]["kind"] == "deploy"
    assert rec.approval_requests[0]["version"] == "v1.2.3"
    deps = db.list_deployments()
    assert deps[0]["status"] == "success" and deps[0]["triggered_by"] == "agent"
    content = [m for m in llm.calls[1] if m["role"] == "tool"][0]["content"]
    assert "shipped v1.2.3" in content and "success" in content


async def test_rollback_defaults_to_previous_success(db, box, ssh, deployer, ssh_server):
    h = add_host(db, box, ssh_server, "web-01")
    app_id = db.create_app({"name": "order-api", "host_ids": [h["id"]], "deploy_script": "true",
                            "rollback_script": "echo back to $VERSION"})
    app = db.get_app(app_id)
    for v in ("v1", "v2"):
        await deployer.execute(deployer.create(app, v, [h]))
    agent, _ = make_agent(db, ssh, deployer, [
        LLMResponse("", [tool_call("rollback", {"app": "order-api"})]),
        LLMResponse("已回滚"),
    ])
    rec = Recorder(approve=True)
    await agent.run_turn(db.create_conversation(), "回滚", rec.emit, rec.approver)
    assert rec.approval_requests[0]["version"] == "v1"
    assert "back to v1" in db.get_deployment(db.list_deployments()[0]["id"])["log"]


async def test_llm_error_surfaces(db, ssh, deployer):
    from app.llm import LLMError

    class Broken:
        async def stream(self, messages, tools):
            raise LLMError("接口挂了")
            yield  # noqa

    agent = Agent(db, ssh, deployer, Broken())
    rec = Recorder()
    await agent.run_turn(db.create_conversation(), "hi", rec.emit, rec.approver)
    assert any(e["type"] == "error" and "接口挂了" in e["message"] for e in rec.events)


def test_history_trimming_keeps_tool_pairs():
    big = "x" * 60_000
    history = []
    for i in range(4):
        history += [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "list_hosts", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": big, "_meta": {}},
            {"role": "assistant", "content": "ok"},
        ]
    trimmed = to_llm_messages(history)
    assert trimmed[0]["role"] == "user"
    assert trimmed[-1]["content"] == "ok"
    ids = {tc["id"] for m in trimmed for tc in m.get("tool_calls") or []}
    answered = {m["tool_call_id"] for m in trimmed if m["role"] == "tool"}
    assert ids == answered
    assert len(trimmed) < len(history)
