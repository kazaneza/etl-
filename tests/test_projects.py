"""SPEC_V2 11: the person chooses demo data or their own data; both projects live in one home."""
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from etlplus import api, config, demo, projects, runner, scheduler, secrets, stage

TODAY = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ETLPLUS_TOKEN", "ETLPLUS_START", "ETLPLUS_HOME", "ETLPLUS_PROJECT", "ETLPLUS_ALLOWED_HOSTS"):
        monkeypatch.delenv(k, raising=False)


def _client(home, **kw):
    return TestClient(api.create_app(home=home, start_scheduler=kw.pop("start_scheduler", False), **kw),
                      base_url="http://localhost")


def _yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the home

def test_new_home_gives_choose_and_writes_only_projects_yaml(tmp_path):
    h = projects.Home.open(tmp_path / "home")
    assert [p.name for p in (tmp_path / "home").iterdir()] == ["projects.yaml"]
    assert _yaml(h.file)["active"] is None
    st = h.state()
    assert st["state"] == "choose" and st["mode"] == "home" and st["active"] is None
    assert {p["id"]: p["exists"] for p in st["projects"]} == {"demo": False, "real": False}
    assert h.active() is None


def test_choose_demo_and_real_and_switch_keeps_both(tmp_path):
    h = projects.Home.open(tmp_path / "home")
    p = h.choose("demo", run_jobs=False)
    assert p.kind == "demo" and h.active_id() == "demo"
    assert (tmp_path / "home" / "demo" / "etlplus.yaml").exists()
    assert "core_customers" in p.jobs and p.destinations
    r = h.choose("real", run_jobs=False)
    assert r.kind == "real" and r.jobs == {} and r.connections == {}
    assert h.active_id() == "real" and _yaml(h.file)["active"] == "real"
    # switching back keeps the demo as it was
    again = h.choose("demo", run_jobs=False)
    assert again.path == p.path and "core_customers" in again.jobs
    assert {x["id"]: x["exists"] for x in h.state()["projects"]} == {"demo": True, "real": True}
    with pytest.raises(ValueError):
        h.choose("other")


def test_legacy_home_registered_as_dot_and_other_project_goes_into_projects(tmp_path):
    root = tmp_path / "vol"
    demo.generate(root)
    (root / "projects.yaml").unlink(missing_ok=True)
    h = projects.Home.open(root)
    data = _yaml(h.file)
    assert data["active"] == "demo" and data["projects"]["demo"]["path"] == "."
    assert h.active().path == root.resolve()
    real = h.choose("real", run_jobs=False)
    assert real.path == (root / "projects" / "real").resolve()
    # nothing of the legacy project was moved or deleted
    assert (root / "core.db").exists() and (root / "jobs" / "core_customers.yaml").exists()
    # the legacy project does not contain the other project
    legacy = h.load("demo")
    assert not legacy.inside(root / "projects" / "real" / "etlplus.yaml")
    assert legacy.protected(root / "projects" / "real" / "secrets.json.enc")
    assert legacy.protected(root / ".secret.key")


def test_legacy_real_project_is_registered_as_real(tmp_path):
    root = tmp_path / "vol"
    config.Project.init(root)
    h = projects.Home.open(root)
    assert h.active_id() == "real" and _yaml(h.file)["projects"]["real"]["path"] == "."


def test_legacy_folder_connection_cannot_read_the_other_projects_secrets(tmp_path):
    root = tmp_path / "vol"
    config.Project.init(root)
    h = projects.Home.open(root)
    legacy = h.active()
    other = h.choose("demo", run_jobs=False)
    secrets.set(other, "x", {"password": "planted-Other-Secret"})
    assert (Path(other.path) / "secrets.json.enc").exists()
    legacy.save_connection("all", {"kind": "folder", "path": ".", "role": "source"})
    legacy = h.load("real")
    from etlplus import connections
    for target in ("projects/demo/secrets.json.enc", ".secret.key", "projects/demo/.secret.key"):
        with pytest.raises(Exception):
            res = connections.preview(legacy, "all", target, fmt="txt", limit=5)
            assert "planted" not in str(res)


def test_reset_demo_swaps_folders_and_a_failure_keeps_the_old_demo(tmp_path, monkeypatch):
    h = projects.Home.open(tmp_path / "home")
    p = h.choose("demo", run_jobs=False)
    marker = Path(p.path) / "jobs" / "mine.yaml"
    marker.write_text("name: mine\n", encoding="utf-8")
    real_generate = demo.generate

    def broken(path, *a, **k):
        real_generate(path, *a, **k)
        raise RuntimeError("disk full")
    monkeypatch.setattr(demo, "generate", broken)
    with pytest.raises(RuntimeError):
        h.reset_demo()
    assert marker.exists() and h.exists("demo")
    assert not (tmp_path / "home" / "demo.new").exists()
    monkeypatch.setattr(demo, "generate", real_generate)
    fresh = h.reset_demo()
    assert not marker.exists() and "core_customers" in fresh.jobs
    assert not (tmp_path / "home" / "demo.old").exists() and not (tmp_path / "home" / "demo.new").exists()


# ---------------------------------------------------------------- the API

def test_every_route_but_start_answers_409_choose_before_the_choice(tmp_path):
    c = _client(tmp_path / "home")
    assert c.get("/health").status_code == 200
    for path in ("/api/start", "/api/kinds", "/api/gallery", "/api/formats", "/api/types?kind=duckdb"):
        assert c.get(path).status_code == 200, path
    for method, path in (("get", "/api/overview"), ("get", "/api/connections"), ("get", "/api/jobs"),
                         ("get", "/api/destinations"), ("get", "/api/runs"), ("get", "/api/settings"),
                         ("get", "/api/setup"), ("get", "/api/alerts"), ("get", "/api/layouts"),
                         ("put", "/api/connections/x"), ("post", "/api/mapping/suggest")):
        r = getattr(c, method)(path, **({"json": {}} if method != "get" else {}))
        assert r.status_code == 409, (path, r.status_code)
        assert r.json() == {"detail": "Choose demo data or your own data first.", "code": "choose"}


def test_token_and_host_checks_run_before_the_choose_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ETLPLUS_TOKEN", "tok-1")
    c = _client(tmp_path / "home")
    assert c.get("/api/overview").status_code == 401
    assert c.post("/api/start", json={"choice": "real"}).status_code == 401
    bad = TestClient(api.create_app(home=tmp_path / "h2", start_scheduler=False, hosts={"localhost"}),
                     base_url="http://evil.example")
    assert bad.post("/api/start", json={"choice": "real"}).status_code == 421


def test_post_start_real_then_ready_and_setup(tmp_path):
    c = _client(tmp_path / "home")
    r = c.post("/api/start", json={"choice": "real"})
    assert r.status_code == 200 and r.json()["state"] == "ready" and r.json()["active"] == "real"
    s = c.get("/api/setup").json()
    assert [x["id"] for x in s["steps"]] == ["connection", "destination", "job"] and s["done"] is False
    ov = c.get("/api/overview").json()
    assert ov["project"]["kind"] == "real" and ov["setup"]["done"] is False
    assert c.post("/api/start", json={"choice": "nope"}).status_code == 422
    assert c.post("/api/projects/real/reset", json={"confirm": "real"}).status_code in (404, 405)


def test_scheduler_restarts_on_switch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "restart", lambda p: calls.append(Path(p.path).name))
    monkeypatch.setattr(scheduler, "start", lambda p: calls.append("start:" + Path(p.path).name))
    monkeypatch.setattr(scheduler, "stop", lambda: calls.append("stop"))
    c = _client(tmp_path / "home", start_scheduler=True)
    with c:
        assert c.post("/api/start", json={"choice": "real"}).status_code == 200
        assert c.post("/api/start", json={"choice": "demo", "run_jobs": False}).status_code == 200
    assert calls[:2] == ["real", "demo"]


def test_fixed_mode_refuses_post_start(tmp_path):
    p = config.Project.init(tmp_path / "p")
    c = TestClient(api.create_app(p, start_scheduler=False), base_url="http://localhost")
    st = c.get("/api/start").json()
    assert st["state"] == "ready" and st["mode"] == "fixed"
    r = c.post("/api/start", json={"choice": "demo"})
    assert r.status_code == 409 and r.json()["detail"] == "This server serves one project"
    assert c.post("/api/projects/demo/reset", json={"confirm": "demo"}).status_code == 409


def test_post_start_twice_while_the_demo_builds_gives_409(tmp_path, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def slow_jobs(self, state):
        state["total"] = 3
        state["running"] = "core_customers"
        started.set()
        gate.wait(20)
    monkeypatch.setattr(projects.Home, "_run_demo_jobs", slow_jobs)
    c = _client(tmp_path / "home")
    r = c.post("/api/start", json={"choice": "demo"})
    assert r.status_code == 200
    assert started.wait(10)
    st = c.get("/api/start").json()
    assert st["setup_run"]["total"] == 3 and st["setup_run"]["running"] == "core_customers"
    again = c.post("/api/start", json={"choice": "real"})
    assert again.status_code == 409 and again.json()["detail"] == "The demo is still being set up."
    assert c.post("/api/projects/demo/reset", json={"confirm": "demo"}).status_code == 409
    gate.set()
    projects.Home(tmp_path / "home").wait_setup(10)
    assert c.post("/api/start", json={"choice": "real"}).status_code == 200


def test_demo_reset_route(tmp_path):
    c = _client(tmp_path / "home")
    c.post("/api/start", json={"choice": "demo", "run_jobs": False})
    assert c.post("/api/projects/demo/reset", json={}).status_code == 422
    r = c.post("/api/projects/demo/reset", json={"confirm": "demo"})
    assert r.status_code == 200 and r.json()["active"] == "demo"


# ---------------------------------------------------------------- isolation

def test_same_job_name_in_both_projects_runs_without_a_lock_clash(tmp_path, monkeypatch):
    h = projects.Home.open(tmp_path / "home")
    a = h.choose("real", run_jobs=False)
    b_path = h.project_path("demo")
    b = config.Project.init(b_path, kind="demo")
    for p in (a, b):
        db = Path(p.path) / "src.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (id INTEGER)")
        con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
        con.commit()
        con.close()
        p.save_connection("src", {"kind": "sqlite", "path": "src.db", "role": "source"})
        p.save_connection("out", {"kind": "folder", "path": "out", "role": "target"})
        p.save_job({"name": "same", "source": {"connection": "src", "table": "t"},
                    "target": {"connection": "out", "table": "t", "mode": "replace"}})
    a, b = config.Project.load(a.path), config.Project.load(b.path)
    from etlplus import sources
    real_read = sources.read
    both_in = threading.Barrier(2, timeout=10)

    def slow(project, job, window, log, *args, **kw):
        both_in.wait()
        yield from real_read(project, job, window, log, *args, **kw)
    monkeypatch.setattr(sources, "read", slow)
    out = {}
    ths = [threading.Thread(target=lambda p=p, k=k: out.__setitem__(k, runner.run(p, "same")))
           for k, p in (("a", a), ("b", b))]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    assert out["a"]["status"] == "done", out["a"]["error"]
    assert out["b"]["status"] == "done", out["b"]["error"]
    assert out["a"]["run_id"] != out["b"]["run_id"] or Path(a.path) != Path(b.path)


def test_find_project_for_the_cli(tmp_path, capsys):
    from etlplus import cli
    home = tmp_path / "home"
    assert cli.main(["projects", "--home", str(home)]) == 0
    assert "Choose demo or your own data first" in capsys.readouterr().out
    assert cli.main(["--project", str(home), "jobs"]) == 2
    assert cli.main(["use", "real", "--home", str(home)]) == 0
    assert cli.main(["--project", str(home), "jobs"]) == 0
    assert cli.main(["projects", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "* " in out or "*  real" in out
