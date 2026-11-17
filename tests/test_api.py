"""Web 层测试：登录、主机 CRUD（私钥加密）、对话 WebSocket 审批流程。"""
import asyncssh
import pytest
from fastapi.testclient import TestClient

from app.llm import LLMResponse
from app.main import Services, create_app
from conftest import FakeLLM, tool_call


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "secret_key", "test-secret")
    monkeypatch.setattr(config.settings, "admin_password", "pw-123456")
    llm = FakeLLM([])
    services = Services(db_path=str(tmp_path / "t.db"), llm=llm)
    with TestClient(create_app(services)) as c:
        c.services = services
        c.llm = llm
        yield c


def login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "pw-123456"})
    assert r.status_code == 200


def test_requires_login(client):
    assert client.get("/api/hosts").status_code == 401
    assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    login(client)
    assert client.get("/api/hosts").status_code == 200


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/api/login", json={"username": "admin", "password": "wrong"})
    r = client.post("/api/login", json={"username": "admin", "password": "pw-123456"})
    assert r.status_code == 429


def test_host_crud_encrypts_key(client):
    login(client)
    key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    r = client.post("/api/hosts", json={"name": "web-01", "hostname": "10.0.0.1", "username": "root",
                                        "private_key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_key"] and "private_key" not in body and "private_key_enc" not in body
    row = client.services.db.get_host(body["id"])
    assert "PRIVATE KEY" not in row["private_key_enc"]
    assert client.services.box.decrypt(row["private_key_enc"]).strip() == key.strip()

    # 留空私钥 = 不修改
    r = client.put(f"/api/hosts/{body['id']}", json={"name": "web-01", "hostname": "10.0.0.2", "username": "root"})
    assert r.status_code == 200
    assert client.services.db.get_host(body["id"])["private_key_enc"] == row["private_key_enc"]

    assert client.post("/api/hosts", json={"name": "bad", "hostname": "x", "username": "r",
                                           "private_key": "not a key"}).status_code == 400
    assert client.post("/api/hosts", json={"name": "nokey", "hostname": "x", "username": "r"}).status_code == 400
    assert client.delete(f"/api/hosts/{body['id']}").status_code == 200
    assert client.get("/api/hosts").json() == []


def test_app_deploy_validates_version(client):
    login(client)
    h = client.post("/api/hosts", json={"name": "web-01", "hostname": "127.0.0.1", "port": 1, "username": "r",
                                        "password": "x"}).json()
    a = client.post("/api/apps", json={"name": "api", "host_ids": [h["id"]], "deploy_script": "true"}).json()
    r = client.post(f"/api/apps/{a['id']}/deploy", json={"version": "v1; rm -rf /"})
    assert r.status_code == 400
    r = client.post(f"/api/apps/{a['id']}/rollback", json={"version": "v1"})
    assert r.status_code == 400 and "回滚脚本" in r.json()["detail"]


def test_chat_ws_approval_flow(client):
    login(client)
    client.services.db.create_host({"name": "web-01", "hostname": "127.0.0.1", "port": 1, "username": "r",
                                    "password_enc": client.services.box.encrypt("x")})
    client.llm.script.extend([
        LLMResponse("我来重启", [tool_call("run_command", {"hosts": ["web-01"], "command": "systemctl restart nginx",
                                                           "purpose": "重启"})]),
        LLMResponse("你拒绝了，那就不重启"),
    ])
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "chat", "text": "重启 nginx"})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] == "approval_request":
                assert ev["command"] == "systemctl restart nginx"
                ws.send_json({"type": "approval", "call_id": ev["call_id"], "approved": False, "note": "高峰期"})
            if ev["type"] == "turn_end":
                break
    types = [e["type"] for e in events]
    assert "conversation" in types and "approval_result" in types
    assert any(e["type"] == "tool_result" and e["status"] == "rejected" for e in events)
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[2]["_meta"]["note"] == "高峰期"


def test_ws_requires_login(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Terminator" in r.text
