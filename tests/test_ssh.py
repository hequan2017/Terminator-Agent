from conftest import add_host, needs_bash

pytestmark = needs_bash


async def test_key_auth_and_run(db, box, ssh, ssh_server):
    host = add_host(db, box, ssh_server)
    res = await ssh.run(host, "echo hello; echo oops >&2; exit 3")
    assert res.exit_code == 3
    assert res.stdout.strip() == "hello"
    assert "oops" in res.stderr
    assert not res.ok


async def test_streaming_output_and_stdin(db, box, ssh, ssh_server):
    host = add_host(db, box, ssh_server)
    chunks = []

    async def on_out(t):
        chunks.append(t)

    res = await ssh.run(host, "bash -s", stdin="echo from-stdin\n", on_output=on_out)
    assert res.ok
    assert "from-stdin" in "".join(chunks)


async def test_connection_reused(db, box, ssh, ssh_server):
    host = add_host(db, box, ssh_server)
    await ssh.run(host, "true")
    conn1 = ssh._conns[host["id"]]
    await ssh.run(host, "true")
    assert ssh._conns[host["id"]] is conn1


async def test_timeout(db, box, ssh, ssh_server):
    host = add_host(db, box, ssh_server)
    res = await ssh.run(host, "sleep 5", timeout=1)
    assert res.timed_out
    assert res.exit_code is None


async def test_wrong_key_rejected(db, box, ssh, ssh_server):
    import asyncssh
    other = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    hid = db.create_host({"name": "bad", "hostname": "127.0.0.1", "port": ssh_server.port, "username": "x",
                          "private_key_enc": box.encrypt(other)})
    res = await ssh.run(db.get_host(hid), "true")
    assert res.error and "认证失败" in res.error


async def test_unreachable_host(db, box, ssh):
    hid = db.create_host({"name": "down", "hostname": "127.0.0.1", "port": 1, "username": "x",
                          "password_enc": box.encrypt("pw")})
    res = await ssh.run(db.get_host(hid), "true")
    assert res.error and "连接失败" in res.error
