"""Regression tests for the review findings of 1 Nov 2026. Each test failed before its fix."""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pytest
import yaml
from fastapi.testclient import TestClient

from etlplus import api, config, connections, guard, runner, scheduler, sources, stage, store, targets, transform
from etlplus import secrets as secret_store

PW = "S3cretPW!-never-sent"


def _yaml(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("ETLPLUS_TOKEN", raising=False)
    monkeypatch.delenv("ETLPLUS_SECRET_KEY", raising=False)
    monkeypatch.delenv("ETLPLUS_ALLOWED_HOSTS", raising=False)
    root = tmp_path / "p"
    config.Project.init(root)
    con = sqlite3.connect(root / "src.db")
    con.execute("CREATE TABLE c (id TEXT, seg TEXT)")
    con.executemany("INSERT INTO c VALUES (?, ?)", [(f"C{i}", "a") for i in range(5)])
    con.commit()
    con.close()
    (root / "landing").mkdir()
    _yaml(root / "connections.yaml", {"connections": {
        "src": {"kind": "sqlite", "path": "./src.db", "role": "source"},
        "sq": {"kind": "sqlite", "path": "./wh.db", "role": "target"},
        "dk": {"kind": "duckdb", "path": "./wh.duckdb", "role": "target"},
        "fo": {"kind": "folder", "path": "./out", "role": "both"},
        "landing": {"kind": "folder", "path": "./landing", "role": "both"},
    }})
    _yaml(root / "jobs" / "j.yaml", {"name": "j", "source": {"connection": "src", "table": "c"},
                                     "target": {"connection": "dk", "table": "c", "mode": "replace", "create": True}})
    return config.Project.load(root)


@pytest.fixture
def client(proj):
    return TestClient(api.create_app(proj, start_scheduler=False), base_url="http://localhost")


def _stage(project, rows, window=None, job="j", schema=None):
    rid = stage.new_run_id()
    time.sleep(0.002)
    t = pa.Table.from_pylist(rows, schema=schema)
    return stage.write(project, job, rid, t.to_batches(), window, "test", None)


# ---------------------------------------------------------------- 1. transform sandbox

def test_transform_cannot_read_the_secret_key(proj, client):
    secret_store.set(proj, "sq", {"password": PW})
    client.post("/api/jobs/j/run", json={"steps": ["extract"]})
    for _ in range(100):
        if stage.latest(proj, "j"):
            break
        time.sleep(0.05)
    key = (proj.path / ".secret.key").as_posix()
    for step in ({"sql": f"SELECT column0 FROM read_csv_auto('{key}', header=false)"},
                 {"derive": {"k": f"(SELECT max(content) FROM read_text('{key}'))"}},
                 {"filter": f"EXISTS (SELECT 1 FROM glob('{proj.path.as_posix()}/*'))"}):
        r = client.post("/api/jobs/j/transform-preview", json={"name": "j", "transform": [step]})
        assert r.status_code == 400, r.text
        assert (proj.path / ".secret.key").read_text().strip() not in r.text


def test_transform_sandbox_blocks_files_even_past_the_guard(tmp_path):
    src = tmp_path / "a.parquet"
    duckdb.sql(f"COPY (SELECT 1 AS a) TO '{src.as_posix()}' (FORMAT parquet)")
    (tmp_path / "secret.txt").write_text("hidden", encoding="utf-8")
    con = transform.sandbox(src)
    try:
        last, _ = transform.build(con, str(src), [{"derive": {"b": "a + 1"}}])
        assert con.execute(f"SELECT * FROM {last}").fetchall() == [(1, 2)]
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_text('{(tmp_path / 'secret.txt').as_posix()}')").fetchall()
        with pytest.raises(duckdb.Error):
            con.execute("SET enable_external_access = true")
    finally:
        con.close()


def test_duckdb_source_cannot_read_files(proj, tmp_path):
    db = proj.path / "srcd.duckdb"
    c = duckdb.connect(str(db))
    c.execute("CREATE TABLE t AS SELECT 1 AS a")
    c.close()
    (tmp_path / "x.txt").write_text("hidden", encoding="utf-8")
    proj.save_connection("dsrc", {"kind": "duckdb", "path": "srcd.duckdb", "role": "source"})
    conn = connections.connect(proj, "dsrc")
    try:
        assert conn.execute("SELECT * FROM t").fetchall() == [(1,)]
        with pytest.raises(duckdb.Error):
            conn.execute(f"SELECT * FROM read_text('{(tmp_path / 'x.txt').as_posix()}')").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- 2. passwords stay with their endpoint

def test_stored_password_not_sent_to_another_host(proj, monkeypatch):
    proj.save_connection("pg", {"kind": "postgres", "host": "real.internal", "port": 5432, "database": "d",
                                "user": "u", "role": "target"})
    secret_store.set(proj, "pg", {"password": PW})
    p = config.Project.load(proj.path)
    assert connections.spec_for(p, "pg")["_password"] == PW
    assert connections.spec_for(p, "pg", {"host": "real.internal", "port": "5432"})["_password"] == PW
    for change in ({"host": "127.0.0.1"}, {"port": 6543}, {"user": "other"}, {"database": "x"},
                   {"options": {"sslmode": "disable"}}, {"kind": "mysql"}):
        assert connections.spec_for(p, "pg", change)["_password"] is None, change
    assert connections.spec_for(p, "pg", {"host": "127.0.0.1", "password": "typed"})["_password"] == "typed"


def test_password_env_only_from_saved_file_and_never_etlplus_keys(proj, monkeypatch):
    monkeypatch.setenv("ETLPLUS_SECRET_KEY_X", "env-secret")
    monkeypatch.setenv("CORE_PW", "core-pw")
    monkeypatch.setenv("ETLPLUS_TOKEN", "tok")
    over = {"kind": "postgres", "host": "evil", "user": "u", "password_env": "ETLPLUS_SECRET_KEY_X"}
    assert connections.spec_for(proj, "nosuch", over)["_password"] is None
    proj.save_connection("a", {"kind": "postgres", "host": "h", "user": "u", "password_env": "CORE_PW"})
    proj.save_connection("b", {"kind": "postgres", "host": "h", "user": "u", "password_env": "ETLPLUS_TOKEN"})
    p = config.Project.load(proj.path)
    assert connections.spec_for(p, "a")["_password"] == "core-pw"
    assert connections.spec_for(p, "a", {"host": "evil"})["_password"] is None
    assert connections.spec_for(p, "b")["_password"] is None


def test_api_save_with_new_host_drops_the_stored_password(client):
    body = {"kind": "postgres", "host": "real.internal", "database": "d", "user": "u", "role": "target",
            "password": PW}
    assert client.put("/api/connections/wh2", json=body).json()["password_set"] is True
    body.pop("password")
    body["host"] = "127.0.0.1"
    assert client.put("/api/connections/wh2", json=body).json()["password_set"] is False
    r = client.put("/api/connections/wh3", json={"kind": "postgres", "host": "h", "user": "u",
                                                  "password_env": "ETLPLUS_SECRET_KEY"})
    assert r.status_code == 422
    r = client.put("/api/connections/wh3", json={"kind": "postgres", "host": "h", "user": "u",
                                                  "password_env": "ETLPLUS_CONN_WH3"})
    assert r.status_code == 200


# ---------------------------------------------------------------- 3, 4. stage folder stays in the project

def test_settings_refuse_a_stage_outside_the_project(client, proj, tmp_path):
    victim = tmp_path / "victim"
    (victim / "j" / "Important").mkdir(parents=True)
    r = client.put("/api/settings", json={"settings": {"stage": str(victim)}})
    assert r.status_code == 422
    assert client.put("/api/settings", json={"settings": {"stage": "../victim"}}).status_code == 422
    assert client.put("/api/settings", json={"settings": {"stage": "staging"}}).status_code == 200


def test_cleanup_removes_only_run_folders(proj):
    jd = stage.job_dir(proj, "j")
    (jd / "MyFolder").mkdir(parents=True)
    (jd / "MyFolder" / "f.txt").write_text("keep", encoding="utf-8")
    for _ in range(3):
        _stage(proj, [{"a": 1}])
    stage.cleanup(proj, "j", 1)
    names = sorted(d.name for d in jd.iterdir())
    assert "MyFolder" in names and len(names) == 2


@pytest.mark.parametrize("name", ["%2E%2E", "..%5C..", "..%5C..%5C..", "nosuch"])
def test_stage_listing_refuses_traversal(client, name):
    assert client.get(f"/api/stage/{name}").status_code == 404


# ---------------------------------------------------------------- 5. guard

@pytest.mark.parametrize("sql,dialect", [
    ('SELECT "pg_terminate_backend"(1)', "postgres"),
    ("SELECT ARRAY[pg_terminate_backend(pid)] FROM pg_stat_activity", "postgres"),
    ("SELECT ARRAY[pg_terminate_backend(pid)] FROM pg_stat_activity", None),
    ("SELECT DBMS_XMLGEN.GETXML('select 1 from dual') FROM dual", "oracle"),
    ("SELECT UTL_HTTP.REQUEST('http://evil/') FROM dual", "oracle"),
    ("SELECT [xp_cmdshell]('dir')", "sqlserver"),
    ("SELECT pg_notify('c', 'x')", "postgres"),
    ("SELECT lo_from_bytea(0, 'x')", "postgres"),
    ("SELECT pg_logical_emit_message(true, 'a', 'b')", "postgres"),
    ("SELECT query_to_xml('insert into t values (1)', true, true, '')", "postgres"),
    ("SELECT txid_current()", "postgres"),
    ("SELECT * FROM read_csv_auto('x')", "duckdb"),
    ("SELECT * FROM read_parquet('x')", "duckdb"),
    ("SELECT * FROM glob('C:/*')", "duckdb"),
    ("SELECT * FROM \"read_json_auto\"('x')", "duckdb"),
    ("SELECT getenv('HOME')", "duckdb"),
])
def test_guard_refuses_hidden_calls(sql, dialect):
    with pytest.raises(guard.GuardError):
        guard.check_select(sql, dialect)


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT [order], [from] FROM [dbo].[t]", "sqlserver"),
    ("SELECT arr[1], upper(name) FROM t", "postgres"),
    ("SELECT read_count FROM t", "postgres"),
    ("SELECT \"dbms_x\" FROM t", "oracle"),
])
def test_guard_still_allows_plain_reads(sql, dialect):
    assert guard.check_select(sql, dialect)


# ---------------------------------------------------------------- 6. host and origin

def test_unknown_host_is_refused(proj):
    c = TestClient(api.create_app(proj, start_scheduler=False), base_url="http://attacker.example:8300")
    assert c.get("/api/connections").status_code == 421
    c = TestClient(api.create_app(proj, start_scheduler=False), base_url="http://127.0.0.1:8300")
    assert c.get("/api/connections").status_code == 200


def test_allowed_hosts_from_env(proj, monkeypatch):
    monkeypatch.setenv("ETLPLUS_ALLOWED_HOSTS", "etl.bank.local")
    c = TestClient(api.create_app(proj, start_scheduler=False), base_url="http://etl.bank.local")
    assert c.get("/api/connections").status_code == 200


def test_cross_site_writes_are_refused(client):
    r = client.post("/api/jobs/j/run", headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
                    content=b"")
    assert r.status_code == 403
    r = client.post("/api/jobs/j/run", headers={"Content-Type": "text/plain"}, content=b"")
    assert r.status_code == 415
    r = client.post("/api/connections/src/test",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, content=b"")
    assert r.status_code == 415
    r = client.post("/api/connections/src/test", headers={"Origin": "http://localhost"}, json={})
    assert r.status_code == 200


# ---------------------------------------------------------------- 7. cron weekdays

def _fires(cron, n=7, tz="Africa/Maputo"):
    t = scheduler.cron_trigger(cron, ZoneInfo(tz))
    now = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo(tz))  # a Sunday
    out = []
    for _ in range(n):
        now = t.get_next_fire_time(None, now)
        out.append(now.strftime("%a"))
        now += timedelta(minutes=1)
    return out


def test_cron_day_of_week_reads_like_crontab():
    assert _fires("0 2 * * 1-5") == ["Mon", "Tue", "Wed", "Thu", "Fri", "Mon", "Tue"]
    assert set(_fires("0 2 * * 0", 3)) == {"Sun"}
    assert set(_fires("0 2 * * 7", 3)) == {"Sun"}
    assert set(_fires("0 2 * * 1", 3)) == {"Mon"}
    assert set(_fires("0 2 * * 6,0", 4)) == {"Sat", "Sun"}
    assert set(_fires("0 2 * * mon-fri", 7)) == {"Mon", "Tue", "Wed", "Thu", "Fri"}


def test_api_next_run_uses_crontab_weekdays(proj, client):
    job = dict(proj.jobs["j"])
    job["schedule"] = "0 7 * * 1-5"
    assert client.put("/api/jobs/j", json=job).status_code == 200
    nxt = datetime.fromisoformat(client.get("/api/jobs/j").json()["next_run"])
    assert nxt.weekday() < 5


# ---------------------------------------------------------------- 8, 17. one run per job across processes

_HOLD = """
import sys, time
from etlplus import config, runner
lk = runner.JobLock(config.Project.load(sys.argv[1]), "j")
assert lk.acquire()
print("held", flush=True)
time.sleep(30)
"""


def test_second_process_cannot_run_the_same_job(proj):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(proj.path)], stdout=subprocess.PIPE, env=env)
    try:
        assert holder.stdout.readline().strip() == b"held"
        assert runner.job_locked(proj, "j")
        with pytest.raises(RuntimeError, match="another process"):
            runner.run(proj, "j")
        with pytest.raises(RuntimeError, match="another process"):
            runner.start_background(proj, "j")
    finally:
        holder.kill()
        holder.wait()
    # the OS drops a dead process's lock, on Windows a moment after it exits
    for _ in range(100):
        if not runner.job_locked(proj, "j"):
            break
        time.sleep(0.05)
    assert not runner.job_locked(proj, "j")
    assert runner.run(proj, "j")["status"] == "done"
    assert not runner.job_locked(proj, "j")


def test_runs_left_running_by_a_dead_process_are_marked(proj, client):
    store.record(proj, {"run_id": "20261101T000000Z-dead", "job": "j", "status": "running",
                        "started_at": "2026-11-01T00:00:00Z", "steps": {}})
    runs = client.get("/api/runs").json()
    dead = next(r for r in runs if r["run_id"] == "20261101T000000Z-dead")
    assert dead["status"] == "failed" and "interrupted" in dead["error"]


def test_a_run_whose_lock_is_held_is_not_marked(proj):
    store.record(proj, {"run_id": "20261101T000000Z-live", "job": "j", "status": "running",
                        "started_at": "2026-11-01T00:00:00Z", "steps": {}})
    lk = runner.JobLock(proj, "j")
    assert lk.acquire()
    try:
        assert runner.reap(proj) == 0
        assert store.get(proj, "20261101T000000Z-live")["status"] == "running"
    finally:
        lk.release()
    assert runner.reap(proj) == 1


# ---------------------------------------------------------------- 9. upsert with a NULL key

@pytest.mark.parametrize("conn", ["sq", "dk", "fo"])
def test_upsert_refuses_null_keys(proj, conn):
    job = {"name": "j", "target": {"connection": conn, "table": "u", "mode": "upsert", "key": ["k"], "create": True}}
    m = _stage(proj, [{"k": "a", "v": 1}, {"k": None, "v": 2}])
    with pytest.raises(targets.LoadError, match="NULL"):
        targets.load(proj, job, m, None)
    m = _stage(proj, [{"k": "a", "v": 1}, {"k": "b", "v": 2}])
    for _ in range(3):
        targets.load(proj, job, m, None)
    assert targets.status(proj, job)["rows"] == 2


# ---------------------------------------------------------------- 10. folder append is atomic

def test_folder_append_failure_never_duplicates(proj, monkeypatch):
    job = {"name": "j", "source": {"incremental": {"column": "day", "lookback_days": 2}},
           "target": {"connection": "fo", "table": "tx", "mode": "append", "format": "parquet"}}
    m1 = _stage(proj, [{"id": 1, "day": "2026-10-29"}, {"id": 2, "day": "2026-10-30"}],
                (date(2026, 10, 29), date(2026, 10, 31)))
    targets.load(proj, job, m1, None)
    m2 = _stage(proj, [{"id": 2, "day": "2026-10-30"}, {"id": 3, "day": "2026-10-31"}],
                (date(2026, 10, 30), date(2026, 11, 1)))
    real = os.replace
    new_part = "part-2026-10-30_2026-11-01.parquet"

    def flaky(a, b):
        if Path(b).name == new_part:
            raise PermissionError("[WinError 32] in use")
        return real(a, b)
    monkeypatch.setattr(targets.os, "replace", flaky)
    with pytest.raises(PermissionError):
        targets.load(proj, job, m2, None)
    monkeypatch.setattr(targets.os, "replace", real)
    folder = (proj.path / "out" / "tx").as_posix()
    ids = [r[0] for r in duckdb.sql(f"SELECT id FROM read_parquet('{folder}/part-*.parquet') ORDER BY id").fetchall()]
    assert len(ids) == len(set(ids))
    assert not list((proj.path / "out" / "tx").glob("*.partial"))
    targets.load(proj, job, m2, None)
    ids = [r[0] for r in duckdb.sql(f"SELECT id FROM read_parquet('{folder}/part-*.parquet') ORDER BY id").fetchall()]
    assert ids == [1, 2, 3]


def test_folder_replace_cancel_leaves_no_partial(proj):
    job = {"name": "j", "target": {"connection": "fo", "table": "r", "mode": "replace", "format": "parquet"}}
    m = _stage(proj, [{"id": 1}])

    class Log:
        def __call__(self, msg):
            pass

        def check(self):
            raise runner.Cancelled("stop")
    with pytest.raises(runner.Cancelled):
        targets.load(proj, job, m, Log())
    assert not list((proj.path / "out").glob("*.partial"))


# ---------------------------------------------------------------- 11. CSV source keeps what the file says

def test_csv_source_keeps_zeros_markers_and_empty_strings(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text('acct,name,note,n\n0012345,"",N/A,1\n0099,NULL,x,\n', encoding="utf-8")
    rows = [r for b in sources.read_file(f, "csv", {}, 1000) for r in b.to_pylist()]
    assert rows == [{"acct": "0012345", "name": "", "note": "N/A", "n": 1},
                    {"acct": "0099", "name": "NULL", "note": "x", "n": None}]
    rows = [r for b in sources.read_file(f, "csv", {"null_values": ["N/A", "NULL"]}, 1000) for r in b.to_pylist()]
    assert rows[0]["note"] is None and rows[1]["name"] is None and rows[0]["name"] == ""
    rows = [r for b in sources.read_file(f, "csv", {"types": "text"}, 1000) for r in b.to_pylist()]
    assert rows[0]["name"] == "" and rows[1]["name"] == "NULL"


# ---------------------------------------------------------------- 12. folder CSV upsert keeps "" apart from NULL

def test_folder_csv_upsert_keeps_empty_strings(proj):
    job = {"name": "j", "target": {"connection": "fo", "table": "e.csv", "mode": "upsert", "key": ["k"]}}
    targets.load(proj, job, _stage(proj, [{"k": "1", "s": ""}, {"k": "2", "s": None}, {"k": "007", "s": "z"}]), None)
    targets.load(proj, job, _stage(proj, [{"k": "3", "s": "y"}, {"k": "007", "s": "w"}]), None)
    text = (proj.path / "out" / "e.csv").read_text(encoding="utf-8")
    assert text.splitlines() == ["k,s", '1,""', "2,", "3,y", "007,w"]


# ---------------------------------------------------------------- 13. BULK INSERT file keeps "" apart from NULL

def test_bulk_csv_keeps_empty_strings_apart_from_null(proj, tmp_path):
    assert targets.csv_line(["a", "", None, 'q"x', "1,2"]) == 'a,"",,"q""x","1,2"\n'
    m = _stage(proj, [{"a": "x", "b": ""}, {"a": "y", "b": None}])
    local = tmp_path / "bulk" / "t.csv"
    targets._bulk_csv(targets.staged_file(proj, m), local, ["a", "b"])
    assert local.read_text(encoding="utf-8") == 'a,b\nx,""\ny,\n'
    assert "KEEPNULLS" in targets.SQLServer().bulk_insert_sql("[#s]", "r.csv", "r.err")


# ---------------------------------------------------------------- 14. window on a column with a time zone

def test_file_window_uses_project_midnight(proj):
    tzx = ZoneInfo("Africa/Maputo")
    at = [datetime(2026, 10, 29, 1, 30, tzinfo=tzx), datetime(2026, 10, 28, 23, 30, tzinfo=tzx)]
    b = pa.record_batch({"at": pa.array(at, pa.timestamp("us", tz="Africa/Maputo"))})
    win = (date(2026, 10, 29), date(2026, 10, 31))
    kept = sources.filter_window(b, "at", win, tz="Africa/Maputo").column(0).to_pylist()
    assert [v.astimezone(tzx).hour for v in kept] == [1]
    pa.parquet.write_table(pa.table({"at": b.column(0)}), proj.path / "landing" / "t.parquet")
    settings = dict(proj.settings, timezone="Africa/Maputo")
    proj.save_settings(settings)
    p = config.Project.load(proj.path)
    got = [r for x in sources.read_files(p, "landing", "t.parquet", "parquet", incremental={"column": "at"},
                                         window=win) for r in x.to_pylist()]
    assert len(got) == 1


# ---------------------------------------------------------------- 15. transform manifest never describes an old file

def test_transform_rerun_never_pairs_new_manifest_with_old_file(proj, monkeypatch):
    m = _stage(proj, [{"a": 1}, {"a": 2}])
    run_dir = stage.run_dir(proj, "j", m["run_id"])
    job = {"name": "j", "transform": [{"filter": "a > 0"}]}
    transform.apply(proj, job, m["run_id"], None)
    real = os.replace

    def fail_on_file(a, b):
        if Path(b).name == "transformed.parquet":
            raise OSError("disk gone")
        return real(a, b)
    monkeypatch.setattr(transform.os, "replace", fail_on_file)
    job2 = {"name": "j", "transform": [{"filter": "a > 1"}]}
    with pytest.raises(OSError):
        transform.apply(proj, job2, m["run_id"], None)
    got = transform.read_manifest(run_dir, "transformed")
    assert got is None or got["rows"] == duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{(run_dir / 'transformed.parquet').as_posix()}')").fetchone()[0]


def test_transform_failure_leaves_no_partial(proj, monkeypatch):
    m = _stage(proj, [{"a": 1}])
    run_dir = stage.run_dir(proj, "j", m["run_id"])
    monkeypatch.setattr(transform, "_sha256", lambda p: (_ for _ in ()).throw(OSError("read error")))
    with pytest.raises(OSError):
        transform.apply(proj, {"name": "j", "transform": [{"filter": "a > 0"}]}, m["run_id"], None)
    assert not list(run_dir.glob("*.partial"))


# ---------------------------------------------------------------- 18. job carries the project time zone

def test_job_out_carries_timezone_and_save_ignores_it(proj, client):
    out = client.get("/api/jobs/j").json()
    assert out["timezone"] == "UTC"
    assert client.put("/api/jobs/j", json=out).status_code == 200
    assert "timezone" not in yaml.safe_load((proj.path / "jobs" / "j.yaml").read_text(encoding="utf-8"))
