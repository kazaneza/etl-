"""The REST API and the console mount, against a temporary project."""
import importlib
import json
import sqlite3
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

config = pytest.importorskip("etlplus.config")
api = pytest.importorskip("etlplus.api")

PASSWORD = "s3cret-Never-Shown"


def has_module(name):
    try:
        importlib.import_module(f"etlplus.{name}")
        return True
    except ImportError:
        return False


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("ETLPLUS_TOKEN", raising=False)
    monkeypatch.delenv("ETLPLUS_SECRET_KEY", raising=False)
    p = config.Project.init(tmp_path / "proj")
    db = p.path / "core.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customers (customer_number TEXT, segment TEXT, updated_at TEXT)")
    con.executemany("INSERT INTO customers VALUES (?, ?, ?)",
                    [(f"C{i:04d}", "retail" if i % 2 else None, "2026-10-30") for i in range(25)])
    con.commit()
    con.close()
    (p.path / "landing").mkdir()
    (p.path / "landing" / "people.csv").write_text("id,score\n1,10\n2,20\n3,30\n", encoding="utf-8")
    p.save_connection("core", {"kind": "sqlite", "path": "core.sqlite", "role": "source"})
    p.save_connection("landing", {"kind": "folder", "path": "landing", "role": "both"})
    p.save_connection("wh", {"kind": "duckdb", "path": "warehouse.duckdb", "role": "target"})
    return config.Project.load(p.path)


@pytest.fixture
def client(project):
    return TestClient(api.create_app(project, start_scheduler=False), base_url="http://localhost")


def no_secret(resp):
    assert PASSWORD not in resp.text
    return resp


# ------------------------------------------------------------------ console


def test_root_redirects_and_console_served(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/app/"
    r = client.get("/app/")
    assert r.status_code == 200 and "etl+" in r.text and "app.js" in r.text
    assert client.get("/app/app.css").status_code == 200
    assert client.get("/app/assets/crane-128.png").status_code == 200
    assert client.get("/health").json()["ok"] is True


def test_console_has_no_em_dashes():
    from pathlib import Path
    folder = Path(api.__file__).parent / "console"
    for name in ("index.html", "app.js", "app.css"):
        text = (folder / name).read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, name


# ------------------------------------------------------------------ auth


def test_token_required_when_set(project, monkeypatch):
    monkeypatch.setenv("ETLPLUS_TOKEN", "tok-123")
    c = TestClient(api.create_app(project, start_scheduler=False), base_url="http://localhost")
    assert c.get("/api/connections").status_code == 401
    assert c.get("/api/connections", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/connections", headers={"Authorization": "Bearer tok-123"}).status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/app/").status_code == 200


# ------------------------------------------------------------------ connections


def test_password_is_write_only(client, project):
    body = {"kind": "postgres", "host": "db.local", "port": 5432, "database": "core", "user": "reader",
            "role": "source", "password": PASSWORD, "options": {"sslmode": "prefer"}}
    r = no_secret(client.put("/api/connections/pg", json=body))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["password_set"] is True and "password" not in out
    lst = no_secret(client.get("/api/connections")).json()
    pg = next(c for c in lst if c["id"] == "pg")
    assert pg["password_set"] is True and pg["host"] == "db.local"
    assert PASSWORD not in (project.path / "connections.yaml").read_text(encoding="utf-8")
    # saving again without a password keeps it, while the endpoint stays the same
    body.pop("password")
    body["description"] = "Core, read only"
    body["port"] = "5432"
    assert no_secret(client.put("/api/connections/pg", json=body)).json()["password_set"] is True
    # clearing removes it
    body["clear_password"] = True
    assert client.put("/api/connections/pg", json=body).json()["password_set"] is False


def test_connection_validation_and_delete(client):
    assert client.put("/api/connections/x", json={"kind": "nosuch"}).status_code == 422
    assert client.put("/api/connections/bad name!", json={"kind": "sqlite", "path": "a.db"}).status_code == 422
    assert client.delete("/api/connections/missing").status_code == 404
    client.put("/api/connections/tmp", json={"kind": "sqlite", "path": "tmp.sqlite"})
    assert client.delete("/api/connections/tmp").status_code == 200
    assert all(c["id"] != "tmp" for c in client.get("/api/connections").json())


def test_test_unsaved_form_and_saved(client):
    r = client.post("/api/connections/draft/test", json={"kind": "sqlite", "path": "core.sqlite", "role": "source"})
    assert r.status_code == 200
    assert r.json()["ok"] is True, r.json()
    r = client.post("/api/connections/core/test")
    assert r.json()["ok"] is True
    r = client.post("/api/connections/draft/test", json={"kind": "sqlite", "path": "nope/missing.sqlite"})
    assert r.json()["ok"] is False
    assert client.post("/api/connections/ghost/test").status_code == 404


def test_test_never_echoes_password(client):
    r = client.post("/api/connections/pg2/test", json={"kind": "postgres", "host": "127.0.0.1", "port": 1,
                                                       "database": "x", "user": "u", "password": PASSWORD})
    assert r.status_code == 200
    no_secret(r)


def test_tables_columns_preview(client):
    tables = client.get("/api/connections/core/tables").json()
    assert any(t["name"] == "customers" for t in tables)
    cols = client.get("/api/connections/core/columns", params={"table": "customers"}).json()
    assert [c["name"] for c in cols] == ["customer_number", "segment", "updated_at"]
    p = client.post("/api/connections/core/preview", json={"table": "customers", "limit": 5}).json()
    assert len(p["rows"]) == 5
    q = client.post("/api/connections/core/preview", json={"table": "SELECT customer_number FROM customers"}).json()
    assert len(q["columns"]) == 1
    bad = client.post("/api/connections/core/preview", json={"table": "DELETE FROM customers"})
    assert bad.status_code == 400
    assert client.post("/api/connections/core/preview", json={}).status_code == 422


def test_folder_preview(client):
    if not has_module("sources"):
        pytest.skip("sources not built yet")
    p = client.post("/api/connections/landing/preview", json={"files": "people.csv", "format": "csv"})
    assert p.status_code == 200, p.text
    assert len(p.json()["rows"]) == 3


# ------------------------------------------------------------------ jobs

JOB = {
    "name": "customers",
    "description": "Customers",
    "source": {"connection": "core", "table": "customers", "columns": "*"},
    "transform": [{"filter": "segment IS NOT NULL"}],
    "target": {"connection": "wh", "table": "customers", "mode": "replace", "create": True},
    "schedule": "0 2 * * *",
    "notify": "failure",
}


def test_job_save_get_delete(client, project):
    r = client.put("/api/jobs/customers", json=JOB)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "customers" and out["problems"] == [] and out["last_run"] is None
    assert out["next_run"]
    assert (project.path / "jobs" / "customers.yaml").exists()
    lst = client.get("/api/jobs").json()
    assert [j["name"] for j in lst] == ["customers"]
    assert client.get("/api/jobs/customers").json()["source"]["table"] == "customers"
    # a connection in use cannot be deleted
    assert client.delete("/api/connections/core").status_code == 409
    assert client.delete("/api/jobs/customers").status_code == 200
    assert client.get("/api/jobs/customers").status_code == 404


def test_job_rename(client, project):
    client.put("/api/jobs/customers", json=JOB)
    r = client.put("/api/jobs/customers", json={**JOB, "name": "customers_v2"})
    assert r.status_code == 200, r.text
    names = [j["name"] for j in client.get("/api/jobs").json()]
    assert names == ["customers_v2"]


def test_job_problems_are_422(client):
    if not has_module("runner"):
        pytest.skip("runner not built yet")
    bad = {**JOB, "source": {"connection": "nosuch", "table": "x"}}
    r = client.put("/api/jobs/customers", json=bad)
    assert r.status_code == 422
    assert r.json()["problems"]


def test_run_unknown_step_and_job(client):
    client.put("/api/jobs/customers", json=JOB)
    assert client.post("/api/jobs/customers/run", json={"steps": ["explode"]}).status_code == 422
    assert client.post("/api/jobs/ghost/run", json={}).status_code == 404


def test_ddl_needs_a_stage(client):
    client.put("/api/jobs/customers", json=JOB)
    assert client.get("/api/jobs/customers/ddl").status_code == 404


def test_full_run_in_background(client):
    for m in ("runner", "sources", "stage", "transform", "targets", "store"):
        if not has_module(m):
            pytest.skip(f"{m} not built yet")
    client.put("/api/jobs/customers", json=JOB)
    run_id = client.post("/api/jobs/customers/run", json={}).json()["run_id"]
    deadline = time.time() + 60
    run = None
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run.get("status") not in ("running", None):
            break
        time.sleep(0.3)
    assert run["status"] == "done", run
    assert isinstance(run["log"], list)
    assert client.get("/api/runs", params={"job": "customers"}).json()[0]["run_id"] == run_id
    staged = client.get("/api/stage/customers").json()
    assert staged and staged[0]["manifests"]
    assert "CREATE" in client.get("/api/jobs/customers/ddl").json()["ddl"].upper()
    prev = client.post("/api/jobs/customers/transform-preview", json=JOB).json()
    assert all(r[1] is not None for r in prev["rows"])
    ov = client.get("/api/overview").json()
    assert ov["counts"]["jobs"] == 1 and ov["staged_bytes"] > 0


# ------------------------------------------------------------------ runs, stage


def test_run_detail_with_log_tail(client, project):
    store = pytest.importorskip("etlplus.store")
    run = {"run_id": "20261101T020000Z-ab12", "job": "customers", "status": "done", "steps": {},
           "rows": {"extract": 3}, "started_at": "2026-11-01T02:00:00Z", "finished_at": "2026-11-01T02:00:05Z",
           "error": None, "trigger": "manual", "window": None}
    store.record(project, run)
    log = project.logs_dir() / "customers"
    log.mkdir(parents=True)
    (log / f"{run['run_id']}.log").write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    r = client.get(f"/api/runs/{run['run_id']}").json()
    assert r["status"] == "done" and len(r["log"]) == 200 and r["log"][-1] == "line 499"
    assert client.get("/api/runs").json()[0]["run_id"] == run["run_id"]
    assert client.get("/api/runs/nope").status_code == 404


def test_stage_listing(client, project):
    client.put("/api/jobs/customers", json=JOB)
    d = project.stage_dir() / "customers" / "20261101T020000Z-ab12"
    d.mkdir(parents=True)
    (d / "extract.parquet").write_bytes(b"x" * 100)
    (d / "transformed.parquet.partial").write_bytes(b"y" * 10)
    (d / "manifest.json").write_text(json.dumps({"job": "customers", "run_id": d.name, "kind": "extract", "rows": 3,
                                                 "columns": [{"name": "a", "type": "VARCHAR"}], "window": None}),
                                     encoding="utf-8")
    s = client.get("/api/stage/customers").json()
    assert s[0]["run_id"] == d.name and s[0]["manifests"][0]["rows"] == 3
    files = {f["name"]: f for f in s[0]["files"]}
    assert files["transformed.parquet.partial"]["complete"] is False
    assert s[0]["bytes"] >= 110
    assert client.get("/api/stage/ghost").status_code == 404


# ------------------------------------------------------------------ settings, overview


def test_settings_smtp_password_write_only(client, project):
    body = {"name": "Test project", "timezone": "Africa/Maputo", "keep_runs": 5,
            "notify": {"on": "failure", "webhook": None,
                       "email": {"host": "smtp.local", "port": 587, "to": "ops@example.local",
                                 "security": "starttls", "password": PASSWORD}}}
    r = no_secret(client.put("/api/settings", json=body))
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["name"] == "Test project" and s["keep_runs"] == 5
    assert s["notify"]["smtp_password_set"] is True
    assert s["notify"]["email"]["host"] == "smtp.local"
    assert PASSWORD not in (project.path / "etlplus.yaml").read_text(encoding="utf-8")
    assert no_secret(client.get("/api/settings")).json()["settings"]["notify"]["smtp_password_set"] is True
    assert client.put("/api/settings", json={"keep_runs": "many"}).status_code == 422


def test_notify_test_without_channels(client):
    if not has_module("notify"):
        pytest.skip("notify not built yet")
    r = client.post("/api/notify/test")
    assert r.status_code == 200 and r.json()["ok"] is False


def test_overview_and_alerts(client):
    ov = client.get("/api/overview").json()
    assert ov["counts"]["connections"] == 3
    assert "drivers" in ov and "jobs" in ov and isinstance(ov["alerts"], list)
    if has_module("store"):
        assert client.get("/api/alerts").status_code == 200


def test_no_password_in_any_listing(client):
    client.put("/api/connections/pg", json={"kind": "postgres", "host": "h", "password": PASSWORD})
    for path in ("/api/connections", "/api/overview", "/api/settings", "/api/jobs"):
        no_secret(client.get(path))
