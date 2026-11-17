from conftest import add_host, needs_bash

from app.deploy import build_script, validate_version

pytestmark = needs_bash


def make_app(db, host_ids, deploy="echo deploying $APP $VERSION on $HOST prev=$PREV_VERSION", rollback="",
             health=""):
    return db.get_app(db.create_app({"name": "order-api", "host_ids": host_ids, "deploy_script": deploy,
                                     "rollback_script": rollback, "health_check": health}))


def test_validate_version_rejects_injection():
    assert validate_version(" v1.2.3 ") == "v1.2.3"
    for bad in ["", "v1; rm -rf /", "$(id)", "a b", "`x`"]:
        try:
            validate_version(bad)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝 {bad!r}")


def test_build_script_quotes_values():
    s = build_script("echo $VERSION", {"VERSION": "it's"})
    assert "set -euo pipefail" in s
    assert "export VERSION='it'\"'\"'s'" in s


async def test_rolling_deploy_success(db, box, deployer, ssh_server):
    h1 = add_host(db, box, ssh_server, "web-01")
    h2 = add_host(db, box, ssh_server, "web-02")
    app = make_app(db, [h1["id"], h2["id"]])
    dep_id = deployer.create(app, "v1.0", [h1, h2])
    result = await deployer.execute(dep_id)
    assert result["status"] == "success"
    dep = db.get_deployment(dep_id)
    assert dep["status"] == "success" and dep["finished_at"]
    assert "deploying order-api v1.0 on web-01 prev=" in dep["log"]
    assert "on web-02" in dep["log"]

    # 第二次发版能拿到上一个成功版本
    dep2 = deployer.create(app, "v1.1", [h1])
    await deployer.execute(dep2)
    assert "prev=v1.0" in db.get_deployment(dep2)["log"]


async def test_failure_stops_rollout(db, box, deployer, ssh_server):
    h1 = add_host(db, box, ssh_server, "web-01")
    h2 = add_host(db, box, ssh_server, "web-02")
    app = make_app(db, [h1["id"], h2["id"]], deploy='echo start; [ "$HOST" != web-01 ]; echo never')
    dep_id = deployer.create(app, "v2", [h1, h2])
    result = await deployer.execute(dep_id)
    assert result["status"] == "failed"
    log = db.get_deployment(dep_id)["log"]
    assert "never" not in log  # set -e 生效
    assert "后续主机未执行：web-02" in log
    assert result["hosts"] == [{"host": "web-01", "ok": False}]


async def test_health_check_failure_marks_failed(db, box, deployer, ssh_server, monkeypatch):
    import app.deploy as d
    monkeypatch.setattr(d.asyncio, "sleep", _no_sleep)
    h1 = add_host(db, box, ssh_server)
    app = make_app(db, [h1["id"]], health="exit 1")
    dep_id = deployer.create(app, "v3", [h1])
    result = await deployer.execute(dep_id)
    assert result["status"] == "failed"
    assert "健康检查" in db.get_deployment(dep_id)["log"]


async def test_rollback_requires_script(db, box, deployer, ssh_server):
    h1 = add_host(db, box, ssh_server)
    app = make_app(db, [h1["id"]])
    try:
        deployer.create(app, "v1", [h1], action="rollback")
    except ValueError as e:
        assert "回滚脚本" in str(e)
    else:
        raise AssertionError("应当报错")


async def test_rollback_runs_rollback_script(db, box, deployer, ssh_server):
    h1 = add_host(db, box, ssh_server)
    app = make_app(db, [h1["id"]], rollback="echo rolling back to $VERSION")
    dep_id = deployer.create(app, "v0.9", [h1], action="rollback")
    await deployer.execute(dep_id)
    assert "rolling back to v0.9" in db.get_deployment(dep_id)["log"]


async def test_resolve_hosts_rejects_foreign_host(db, box, deployer, ssh_server):
    h1 = add_host(db, box, ssh_server, "web-01")
    add_host(db, box, ssh_server, "db-01")
    app = make_app(db, [h1["id"]])
    try:
        deployer.resolve_hosts(app, ["db-01"])
    except ValueError as e:
        assert "不属于" in str(e)
    else:
        raise AssertionError("应当报错")


async def _no_sleep(_s):
    return None
