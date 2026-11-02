"""SPEC_V2 9: every new route (happy path plus one error), secrets never in a response,
pagination headers, and the flow statuses."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from etlplus import api, config, projects, store

PLANT = "planted-Secret-9f3e"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ETLPLUS_TOKEN", "ETLPLUS_START", "ETLPLUS_HOME", "ETLPLUS_PROJECT", "ETLPLUS_ALLOWED_HOSTS"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def home(tmp_path):
    h = projects.Home.open(tmp_path / "home")
    p = h.choose("real", run_jobs=False)
    con = sqlite3.connect(Path(p.path) / "core.db")
    con.execute("CREATE TABLE customers (customer_number TEXT, segment TEXT, amount TEXT)")
    con.executemany("INSERT INTO customers VALUES (?, ?, ?)",
                    [(f"C{i:03d}", "mass" if i % 2 else "sme", f"{i}.5") for i in range(12)])
    con.commit()
    con.close()
    land = Path(p.path) / "land"
    (land / "bank").mkdir(parents=True)
    (land / "people.csv").write_text("id,score\n1,10\n2,20\n", encoding="utf-8")
    p.save_connection("core", {"kind": "sqlite", "path": "core.db", "role": "source"})
    p.save_connection("rep", {"kind": "duckdb", "path": "rep.duckdb", "role": "target"})
    p.save_connection("land", {"kind": "folder", "path": "land", "role": "both"})
    try:
        from etlplus.formats import samples
        import datetime as dt
        samples.write(land, dt.date(2026, 11, 1))
    except Exception:
        pass
    return h


@pytest.fixture
def client(home):
    return TestClient(api.create_app(home=home, start_scheduler=False), base_url="http://localhost")


DEST = {"connection": "rep", "table": "custs", "design": "create", "keys": ["cust_id"],
        "columns": [{"name": "cust_id", "type": "string(8)", "nullable": False},
                    {"name": "seg", "type": "string(10)"},
                    {"name": "src", "type": "string(10)", "default": "'CORE'"}]}
JOB = {"name": "m", "source": {"connection": "core", "table": "customers"},
       "target": {"destination": "custs", "mode": "replace"},
       "mapping": {"columns": {"cust_id": {"from": "customer_number"}, "seg": {"from": "segment"}}}}


def _wait(client, run_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{run_id}").json()
        if r.get("status") not in ("running", None):
            return r
        time.sleep(0.2)
    raise AssertionError("the run did not end")


# ---------------------------------------------------------------- start, setup, kinds, gallery, types, formats

def test_start_setup_overview(client):
    st = client.get("/api/start").json()
    assert st["state"] == "ready" and st["active"] == "real" and st["mode"] == "home"
    assert client.get("/api/setup").json()["steps"][0] == {"id": "connection", "done": True}
    ov = client.get("/api/overview").json()
    assert ov["project"]["kind"] == "real" and ov["counts"]["connections"] == 3


def test_kinds_gallery_types_formats(client):
    kinds = client.get("/api/kinds").json()
    assert any(k["kind"] == "postgres" for k in kinds)
    gal = client.get("/api/gallery").json()
    assert [g["group"] for g in gal] == ["Databases", "Files", "Bank files", "Remote locations"]
    files = next(g for g in gal if g["group"] == "Files")["cards"]
    assert {c["format"] for c in files} == {"csv", "txt", "fixed", "parquet", "json", "jsonl", "excel"}
    assert all(c["kind"] is None for c in files)
    types = client.get("/api/types", params={"kind": "postgres"}).json()
    assert {"generic": "string(40)", "native": "varchar(40)"} in types
    assert client.get("/api/types", params={"kind": "nosuch"}).status_code == 422
    assert isinstance(client.get("/api/formats").json(), list)


def test_formats_detect_and_layouts(client, home):
    pytest.importorskip("etlplus.formats")
    r = client.post("/api/formats/detect", json={"connection": "land", "file": "bank/statement_mt940.txt"})
    assert r.status_code == 200, r.text
    assert r.json()["detected"][0]["format"] == "mt940"
    assert client.post("/api/formats/detect", json={"connection": "land", "file": "../core.db"}).status_code == 422
    assert client.post("/api/formats/detect", json={"connection": "land", "file": "nope.txt"}).status_code == 404
    lays = client.get("/api/layouts")
    assert lays.status_code == 200 and int(lays.headers["X-Total-Count"]) >= 3
    name = "card_processor_ni_style"
    lay = client.get(f"/api/layouts/{name}").json()
    assert lay["name"] == name
    assert client.get("/api/layouts/BAD..name").status_code in (404, 422)
    cp = client.post(f"/api/layouts/{name}/copy", json={"to": "my_cards"})
    assert cp.status_code == 200, cp.text
    assert client.post(f"/api/layouts/{name}/copy", json={"to": "my_cards"}).status_code == 422
    t = client.post(f"/api/layouts/{name}/test", json={"connection": "land", "file": "bank/card_daily_ni_style.txt"})
    assert t.status_code == 200, t.text
    assert t.json()["rows"] and "checks" in t.json()
    unsaved = client.post("/api/layouts/x/test", json={"connection": "land", "file": "bank/card_daily_ni_style.txt",
                                                       "layout": lay})
    assert unsaved.status_code == 200
    bad = dict(lay, records=[])
    assert client.put("/api/layouts/my_cards", json=bad).status_code == 422
    assert client.put("/api/layouts/my_cards", json=dict(lay, name="my_cards", template=False)).status_code == 200
    assert client.delete("/api/layouts/my_cards").status_code == 200
    assert client.delete(f"/api/layouts/{name}").status_code in (404, 422)


# ---------------------------------------------------------------- connections v2

def test_connection_files_preview_and_source_columns(client):
    r = client.get("/api/connections/land/files", params={"pattern": "*.csv"})
    assert r.status_code == 200, r.text
    assert [f["path"] for f in r.json()] == ["people.csv"] and r.headers["X-Total-Count"] == "1"
    assert client.get("/api/connections/ghost/files").status_code == 404
    cols = client.post("/api/sources/columns", json={"connection": "core", "table": "customers"}).json()
    assert cols["rows_sampled"] == 12 and cols["columns"][0]["name"] == "customer_number"
    assert cols["columns"][0]["samples"][:1] == ["C000"]
    assert client.post("/api/sources/columns", json={}).status_code == 422


def test_secret_fields_never_reach_yaml_responses_or_logs(client, home):
    p = home.active()
    plants = {
        "s3": {"kind": "s3", "bucket": "b", "region": "us-east-1", "access_key_id": "AKIDDEMO",
               "secret_access_key": PLANT + "s3", "options": {"use_default_credentials": False}},
        "az": {"kind": "azure_blob", "account_url": "https://demo.blob.example", "container": "c",
               "sas_token": PLANT + "sas", "account_key": PLANT + "key"},
        "web": {"kind": "http", "url": "https://files.example/x.csv", "header_name": "Authorization",
                "header_value": PLANT + "hdr"},
        "sf": {"kind": "sftp", "host": "sftp.example", "user": "u", "private_key": PLANT + "pk",
               "passphrase": PLANT + "pp",
               "host_key_fingerprint": "SHA256:" + "A" * 43},
        "od": {"kind": "odbc", "connection_string": "DSN=demo;UID=u;PWD={" + PLANT + "odbc;x}}y};"
                                                     "KeyStoreSecret=" + PLANT + "ks;AuthToken=" + PLANT + "tok;"
                                                     "PRIV_KEY_FILE_PWD=" + PLANT + "pk;"},
        "mg": {"kind": "mongodb", "database": "x",
               "uri": "mongodb://u:" + PLANT + "pw@mongo.example/x?tlsCertificateKeyFilePassword=" + PLANT
                      + "kp&authMechanismProperties=AWS_SESSION_TOKEN:" + PLANT + "tok&replicaSet=rs0"},
    }
    bodies = []
    for cid, body in plants.items():
        r = client.put(f"/api/connections/{cid}", json=body)
        bodies.append(r.text)
        assert r.status_code == 200, (cid, r.text)
    for path in ("/api/connections", "/api/overview", "/api/settings", "/api/jobs", "/api/destinations",
                 "/api/gallery", "/api/kinds"):
        bodies.append(client.get(path).text)
    for cid in plants:
        bodies.append(client.get(f"/api/connections/{cid}/files").text)
        bodies.append(client.post(f"/api/connections/{cid}/test").text)
    yaml_text = (Path(p.path) / "connections.yaml").read_text(encoding="utf-8")
    logs = "".join(f.read_text(encoding="utf-8", errors="replace")
                   for f in Path(p.path).rglob("*.log")) if (Path(p.path) / "logs").exists() else ""
    for text in bodies + [yaml_text, logs]:
        assert PLANT not in text
    out = client.get("/api/connections/az").json()
    assert out.get("sas_token_set") is True and out.get("account_key_set") is True
    od = client.get("/api/connections/od").json()
    assert od.get("password_set") is True and PLANT not in json.dumps(od)


def test_connection_list_pagination(client):
    r = client.get("/api/connections", params={"offset": 1, "limit": 1})
    assert r.headers["X-Total-Count"] == "3" and [c["id"] for c in r.json()] == ["land"]
    assert len(client.get("/api/connections").json()) == 3


# ---------------------------------------------------------------- destinations

def test_destination_routes(client):
    r = client.put("/api/destinations/custs", json=DEST)
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is False and r.json()["jobs"] == []
    bad = client.put("/api/destinations/bad", json={"connection": "core", "table": "t", "columns": DEST["columns"]})
    assert bad.status_code == 422 and bad.json()["problems"]
    lst = client.get("/api/destinations", params={"limit": 10})
    assert lst.headers["X-Total-Count"] == "1" and lst.json()[0]["id"] == "custs"
    assert client.get("/api/destinations/ghost").status_code == 404
    ddl = client.get("/api/destinations/custs/ddl").json()
    assert ddl["sql"].startswith("CREATE TABLE") and ddl["dialect"] == "duckdb" and ddl["exists"] is False
    t = client.post("/api/destinations/custs/test").json()
    assert t["ok"] is True
    t2 = client.post("/api/destinations/custs/test", json=dict(DEST, keys=["nope"])).json()
    assert t2["ok"] is False
    c = client.post("/api/destinations/custs/create")
    assert c.status_code == 200 and c.json()["created"] is True
    assert client.post("/api/destinations/custs/create").status_code == 409
    pv = client.get("/api/destinations/custs/preview", params={"limit": 5}).json()
    assert pv["total"] == 0 and pv["columns"] == ["cust_id", "seg", "src"]
    assert client.get("/api/destinations/ghost/preview").status_code == 404
    design = client.post("/api/destinations/design-from-source",
                         json={"source": {"connection": "core", "table": "customers"}, "connection": "rep"})
    assert design.status_code == 200 and [c["name"] for c in design.json()] == ["customer_number", "segment", "amount"]
    assert client.post("/api/destinations/design-from-source", json={"source": {"connection": "ghost",
                                                                               "table": "x"}}).status_code == 400
    ren = client.put("/api/destinations/customers2", json=dict(DEST, rename_from="custs"))
    assert ren.status_code == 200 and client.get("/api/destinations/custs").status_code == 404
    client.put("/api/jobs/m", json=dict(JOB, target={"destination": "customers2", "mode": "replace"}))
    d = client.delete("/api/destinations/customers2")
    assert d.status_code == 409 and d.json()["jobs"] == ["m"]
    client.delete("/api/jobs/m")
    assert client.delete("/api/destinations/customers2").status_code == 200


# ---------------------------------------------------------------- jobs: validate, run with map, preview, suggest, flow

def test_job_mapping_run_preview_suggest_flow(client, home):
    client.put("/api/destinations/custs", json=DEST)
    bad = dict(JOB, mapping={"columns": {"cust_id": {"from": "x", "const": 1}}})
    r = client.put("/api/jobs/m", json=bad)
    assert r.status_code == 422 and any("exactly one of" in x for x in r.json()["problems"])
    ghost = dict(JOB, mapping={"columns": {"cust_id": {"from": "customer_number"}, "ghost": {"const": 1}}})
    assert client.put("/api/jobs/m", json=ghost).status_code == 422
    assert client.put("/api/jobs/m", json=dict(JOB, source={"connection": "land", "files": "x",
                                                           "pickup": {"after": "delete"},
                                                           "format": "csv"})).status_code == 422
    assert client.put("/api/jobs/m", json=JOB).status_code == 200

    flow = client.get("/api/jobs/m/flow").json()
    assert flow["run_id"] is None and {n["status"] for n in flow["nodes"]} == {"idle"}
    assert [n["id"] for n in flow["nodes"]] == ["source", "stage", "transform", "mapping", "destination"]
    assert client.get("/api/jobs/ghost/flow").status_code == 404

    pv = client.post("/api/jobs/m/mapping-preview", json=JOB).json()
    assert pv["based_on"] == "live-sample" and len(pv["rows"]) == 12 and pv["problems"] == []
    assert client.post("/api/jobs/m/mapping-preview",
                       json=dict(JOB, source={"connection": "ghost", "table": "x"})).status_code == 400

    sug = client.post("/api/mapping/suggest", json={"source_columns": ["Customer_Number", "seg"],
                                                    "destination_columns": [{"name": "customer_number"}, {"name": "segment"}]})
    assert sug.json()["columns"]["customer_number"]["how"] == "exact"
    assert client.post("/api/mapping/suggest", json={"source_columns": "x"}).status_code == 422

    assert client.post("/api/jobs/m/run", json={"steps": ["explode"]}).status_code == 422
    run_id = client.post("/api/jobs/m/run", json={}).json()["run_id"]
    run = _wait(client, run_id)
    assert run["status"] == "done", run.get("error")
    assert set(run["steps"]) == {"extract", "map", "load"} and run["rows"]["mapped"] == 12
    flow = client.get("/api/jobs/m/flow").json()
    st = {n["id"]: n["status"] for n in flow["nodes"]}
    assert st == {"source": "ok", "stage": "ok", "transform": "skipped", "mapping": "ok", "destination": "ok"}
    assert flow["edges"][0] == {"from": "source", "to": "stage", "rows": 12} and flow["live"] is False
    only_map = client.post("/api/jobs/m/run", json={"steps": ["map", "load"]}).json()["run_id"]
    assert _wait(client, only_map)["status"] == "done"
    pv2 = client.post("/api/jobs/m/mapping-preview", json=JOB).json()
    assert pv2["based_on"].startswith("staged:")
    files = client.get("/api/jobs/m/files")
    assert files.status_code == 200 and files.headers["X-Total-Count"] == "0"
    assert client.get("/api/jobs/ghost/files").status_code == 404


def test_flow_failed_at_map_and_running(client, home):
    client.put("/api/destinations/custs", json=DEST)
    client.put("/api/jobs/m", json=JOB)
    p = home.active()
    store.record(p, {"run_id": "20261101T000000Z-aaaa", "job": "m", "status": "failed", "trigger": "manual",
                     "started_at": "2026-11-01T00:00:00Z", "finished_at": "2026-11-01T00:00:01Z",
                     "steps": {"extract": {"status": "done"}, "map": {"status": "failed"}},
                     "rows": {"extracted": 12}, "error": "Mapping failed", "stage_run_id": None})
    st = {n["id"]: n["status"] for n in client.get("/api/jobs/m/flow").json()["nodes"]}
    assert st == {"source": "ok", "stage": "ok", "transform": "skipped", "mapping": "failed", "destination": "idle"}
    store.record(p, {"run_id": "20261101T000100Z-bbbb", "job": "m", "status": "running", "trigger": "manual",
                     "started_at": "2026-11-01T00:01:00Z", "finished_at": None,
                     "steps": {"extract": {"status": "done"}, "map": {"status": "running"}},
                     "rows": {"extracted": 12}, "error": None, "stage_run_id": None})
    flow = client.get("/api/jobs/m/flow").json()
    st = {n["id"]: n["status"] for n in flow["nodes"]}
    assert flow["live"] is True and st["mapping"] == "running" and st["destination"] == "idle"


def test_runs_and_alerts_pagination(client, home):
    p = home.active()
    for i in range(7):
        store.record(p, {"run_id": f"20261101T00000{i}Z-ab{i:02d}", "job": "j", "status": "failed",
                         "started_at": f"2026-11-01T00:00:0{i}Z", "finished_at": f"2026-11-01T00:00:0{i}Z",
                         "steps": {}, "rows": {}, "error": f"e{i}"})
    r = client.get("/api/runs", params={"offset": 2, "limit": 3})
    assert r.headers["X-Total-Count"] == "7" and len(r.json()) == 3
    assert r.json()[0]["run_id"] == "20261101T000004Z-ab04"
    assert len(client.get("/api/runs").json()) == 7
    a = client.get("/api/alerts", params={"offset": 5, "limit": 10})
    assert a.headers["X-Total-Count"] == "7" and len(a.json()) == 2
    j = client.get("/api/jobs", params={"limit": 1})
    assert j.headers["X-Total-Count"] == "0"
