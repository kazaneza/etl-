"""Real-server tests for PostgreSQL and MariaDB (SPEC_V2 section 14). Integrator only:

    docker compose --profile testdb up -d testdb-pg testdb-mariadb
    ETLPLUS_TESTDB=1 pytest -m realdb
    docker compose --profile testdb down -v

The password comes from ETLPLUS_TESTDB_PASSWORD and is never printed. Every table these tests
make is made-up data in the throwaway etltest database.
"""

import datetime as dt
import decimal
import os
import uuid

import pytest

pytestmark = pytest.mark.realdb

from etlplus import config, connections, drivers, runner, secrets  # noqa: E402

SERVERS = {
    "postgres": {"kind": "postgres", "host": "127.0.0.1", "port": int(os.environ.get("ETLPLUS_TESTDB_PG_PORT", "55432")), "database": "etltest", "user": "etl"},
    "mariadb": {"kind": "mariadb", "host": "127.0.0.1", "port": int(os.environ.get("ETLPLUS_TESTDB_MARIADB_PORT", "53306")), "database": "etltest", "user": "etl"},
}

# generic types a server stores under another name; the round trip compares the rest
ROUND_TRIP_LOSS = {"mariadb": {"timestamptz": "timestamp", "json": "text"}}


def _password():
    pw = os.environ.get("ETLPLUS_TESTDB_PASSWORD")
    if not pw:
        pytest.skip("ETLPLUS_TESTDB_PASSWORD is not set")
    return pw


def _admin(kind):
    """A raw connection for test setup only (never through etl+)."""
    s, pw = SERVERS[kind], _password()
    if kind == "postgres":
        psycopg = pytest.importorskip("psycopg")
        return psycopg.connect(host=s["host"], port=s["port"], dbname=s["database"], user=s["user"], password=pw,
                               autocommit=True)
    pymysql = pytest.importorskip("pymysql")
    return pymysql.connect(host=s["host"], port=s["port"], database=s["database"], user=s["user"], password=pw,
                           autocommit=True)


def _exec(con, sql, params=None):
    cur = con.cursor()
    cur.execute(sql, params) if params is not None else cur.execute(sql)
    try:
        return cur.fetchall()
    except Exception:
        return None
    finally:
        cur.close()


@pytest.fixture(params=["postgres", "mariadb"])
def env(request, tmp_path, monkeypatch):
    kind = request.param
    monkeypatch.delenv("ETLPLUS_SECRET_KEY", raising=False)
    project = config.Project.init(tmp_path / "proj")
    pw = _password()
    tag = uuid.uuid4().hex[:8]
    project.save_connection("src", dict(SERVERS[kind], role="source"))
    project.save_connection("tgt", dict(SERVERS[kind], role="target"))
    secrets.set(project, "src", {"password": pw})
    secrets.set(project, "tgt", {"password": pw})
    admin = _admin(kind)
    src = f"src_{tag}"
    _exec(admin, f"CREATE TABLE {src} (id INTEGER PRIMARY KEY, day DATE NOT NULL, amount NUMERIC(18,2), "
                 "note VARCHAR(40))")
    ph = "%s"
    rows = [(d * 100 + i, dt.date(2026, 10, d), decimal.Decimal(f"{d}.{i:02d}"), f"Demo row {d}-{i}")
            for d in (1, 2, 3) for i in range(1, 4)]
    cur = admin.cursor()
    cur.executemany(f"INSERT INTO {src} VALUES ({ph}, {ph}, {ph}, {ph})", rows)
    cur.close()
    made = [src]
    yield {"kind": kind, "project": config.Project.load(project.path), "admin": admin, "src": src, "tag": tag,
           "made": made}
    for t in made:
        try:
            _exec(admin, f"DROP TABLE IF EXISTS {t}")
        except Exception:
            pass
    admin.close()


def _count(admin, table):
    return _exec(admin, f"SELECT COUNT(*) FROM {table}")[0][0]


def _create(e, name, columns, keys=()):
    from etlplus import ddl
    sql = ddl.create_table_sql(e["kind"], name, columns, list(keys))
    _exec(e["admin"], sql)
    e["made"].append(name)
    return name


def _job(e, name, target, **src):
    job = {"name": name, "source": {"connection": "src", "table": e["src"], **src}, "target": target}
    p = e["project"]
    p.save_job(job)
    return config.Project.load(p.path)


def _ok(rec):
    assert rec.get("status") in ("done", "ok", "success"), rec


COLS = [{"name": "id", "type": "integer", "nullable": False}, {"name": "day", "type": "date"},
        {"name": "amount", "type": "decimal(18,2)"}, {"name": "note", "type": "string(40)"}]


def test_source_session_is_read_only(env):
    conn = drivers.connect(env["project"], "src", role="source")
    try:
        cur = conn.cursor()
        with pytest.raises(Exception):
            cur.execute(f"CREATE TABLE ro_probe_{env['tag']} (a INTEGER)")
    finally:
        connections.close(conn)
    assert not _exec(env["admin"], "SELECT 1 FROM information_schema.tables WHERE table_name = "
                                   f"'ro_probe_{env['tag']}'")


def test_replace_append_upsert_and_mapping(env):
    e, p = env, env["project"]
    # replace into a table designed through ddl
    rep = _create(e, f"dst_rep_{e['tag']}", COLS, ["id"])
    p = _job(e, "rep", {"connection": "tgt", "table": rep, "mode": "replace"})
    _ok(runner.run(p, "rep"))
    assert _count(e["admin"], rep) == 9

    # append with a 2-day window, twice: the slice is replaced, never doubled
    app = _create(e, f"dst_app_{e['tag']}", COLS)
    p = _job(e, "app", {"connection": "tgt", "table": app, "mode": "append"}, incremental={"column": "day", "lookback_days": 2})
    win = (dt.date(2026, 10, 2), dt.date(2026, 10, 4))
    for _ in range(2):
        _ok(runner.run(p, "app", window=win))
    assert _count(e["admin"], app) == 6
    assert _exec(e["admin"], f"SELECT COUNT(DISTINCT id) FROM {app}")[0][0] == 6

    # upsert on a key: native (unique key), then delete_insert once the key is dropped
    ups = _create(e, f"dst_ups_{e['tag']}", COLS, ["id"])
    p = _job(e, "ups", {"connection": "tgt", "table": ups, "mode": "upsert", "key": ["id"]})
    _ok(runner.run(p, "ups"))
    _exec(e["admin"], f"UPDATE {e['src']} SET amount = amount + 1000 WHERE day = '2026-10-02'")
    _ok(runner.run(p, "ups"))
    assert _count(e["admin"], ups) == 9
    assert _exec(e["admin"], f"SELECT MIN(amount) FROM {ups} WHERE day = '2026-10-02'")[0][0] > 1000
    if e["kind"] == "postgres":
        _exec(e["admin"], f"ALTER TABLE {ups} DROP CONSTRAINT {ups}_pkey")
    else:
        _exec(e["admin"], f"ALTER TABLE {ups} DROP PRIMARY KEY")
    _exec(e["admin"], f"UPDATE {e['src']} SET amount = amount + 1000 WHERE day = '2026-10-03'")
    _ok(runner.run(p, "ups"))
    assert _count(e["admin"], ups) == 9
    assert _exec(e["admin"], f"SELECT MIN(amount) FROM {ups} WHERE day = '2026-10-03'")[0][0] > 1000

    # a mapping job: a column, a constant and an expression
    mcols = [{"name": "line_id", "type": "string(20)", "nullable": False}, {"name": "amount", "type": "decimal(18,2)"},
             {"name": "source_system", "type": "string(10)"}]
    mt = _create(e, f"dst_map_{e['tag']}", mcols, ["line_id"])
    p.save_destination("mapped", {"connection": "tgt", "table": mt, "design": "existing", "columns": mcols,
                                  "keys": ["line_id"]})
    p = config.Project.load(p.path)
    job = {"name": "mapjob", "source": {"connection": "src", "table": e["src"]},
           "target": {"destination": "mapped", "mode": "replace"},
           "mapping": {"columns": {"line_id": {"expr": "CAST(id AS VARCHAR) || '-L'"},
                                   "amount": {"from": "amount"}, "source_system": {"const": "DEMO"}}}}
    p.save_job(job)
    p = config.Project.load(p.path)
    _ok(runner.run(p, "mapjob"))
    got = _exec(e["admin"], f"SELECT line_id, source_system FROM {mt} ORDER BY line_id")
    assert len(got) == 9 and got[0][0].endswith("-L") and {r[1] for r in got} == {"DEMO"}


def test_type_map_round_trip(env):
    e = env
    gens = ["string(40)", "text", "integer", "bigint", "decimal(18,2)", "double", "boolean", "date", "time",
            "timestamp", "timestamptz", "binary", "json"]
    cols = [{"name": f"c_{g.split('(')[0]}", "type": g} for g in gens]
    t = _create(e, f"types_{e['tag']}", cols)
    got = {c["name"].lower(): c for c in connections.list_columns(e["project"], "tgt", t)}
    loss = ROUND_TRIP_LOSS.get(e["kind"], {})
    for g in gens:
        base = g.split("(")[0]
        back = drivers.generic_type(e["kind"], got[f"c_{base}"]["type"]).split("(")[0]
        assert back == loss.get(base, base), (g, got[f"c_{base}"]["type"], back)
