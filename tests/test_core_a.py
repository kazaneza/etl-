import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from etlplus import config, connections, guard, secrets, sources, stage
from etlplus.guard import GuardError, check_select


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("ETLPLUS_SECRET_KEY", raising=False)
    return config.Project.init(tmp_path / "proj")


# guard

ALLOWED = [
    "SELECT 1",
    "select * from t",
    "  SELECT a, b FROM s.t WHERE a > 1  ;  ",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT * FROM r",
    "SELECT 'delete from t; drop table x' AS note",
    "SELECT \"update\", [insert], `set` FROM t",
    "SELECT replace(name, 'a', 'b') FROM t",
    "SELECT * FROM t -- trailing comment with DROP TABLE x;",
    "/* leading comment: DELETE */ SELECT 1",
    "SELECT 'it''s; fine' FROM dual",
    "SELECT $$ drop table x; $$ AS body",
    "(SELECT 1) UNION (SELECT 2)",
    "SELECT updated_at, created_by, insert_date, deleted_flag FROM t",
    "SELECT @@VERSION",
    "SELECT * FROM t WHERE c >= %s AND c < %s",
    "SELECT * FROM t WHERE c >= :1 AND c < :2",
    "SELECT banner FROM v$version WHERE ROWNUM = 1",
]

REFUSED = [
    "",
    "   ;  ",
    "-- only a comment",
    "DELETE FROM t",
    "UPDATE t SET a = 1",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "TRUNCATE t",
    "ALTER TABLE t ADD c INT",
    "CREATE TABLE x (a INT)",
    "EXEC sp_who",
    "CALL proc()",
    "COPY t TO '/tmp/x'",
    "SELECT * INTO new_t FROM t",
    "SELECT 1; DROP TABLE t",
    "SELECT 1; SELECT 2",
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "WITH u AS (UPDATE t SET a = 1 RETURNING a) SELECT a FROM u",
    "SELECT 1 /* hidden */; DELETE FROM t",
    "SELECT 1 -- x\n; DROP TABLE t",
    "SELECT * FROM t FOR UPDATE",
    "LOCK TABLE t",
    "SET ROLE admin",
    "GRANT ALL ON t TO u",
    "SELECT pg_sleep(10)",
    "SELECT xp_cmdshell('dir')",
    "SELECT * FROM dblink_exec('x', 'drop table t')",
    "SELECT setval('s', 1)",
    "SELECT 'unclosed",
    "SELECT 1 /* unclosed",
    "EXPLAIN ANALYZE SELECT 1",
    "SELECT '\\'' ; DELETE FROM t; -- '",
    "MERGE INTO t USING s ON (1=1) WHEN MATCHED THEN DELETE",
    "BEGIN; SELECT 1; COMMIT",
    "SHOW TABLES",
    "VALUES (1)",
    "SELECT 1 # ; DROP TABLE t\n",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_guard_allows(sql):
    assert check_select(sql)


@pytest.mark.parametrize("sql", REFUSED)
def test_guard_refuses(sql):
    with pytest.raises(GuardError):
        check_select(sql)


def test_guard_cleans():
    assert check_select("/* a */ SELECT 1 -- b\n;") == "SELECT 1"
    assert check_select("SELECT '--not a comment' AS x") == "SELECT '--not a comment' AS x"


def test_guard_count():
    assert len(ALLOWED) + len(REFUSED) >= 30


# secrets

def test_secrets_round_trip(project):
    assert secrets.get(project, "core") == {}
    secrets.set(project, "core", {"password": "s3cret-value"})
    assert secrets.get(project, "core") == {"password": "s3cret-value"}
    assert secrets.has(project, "core") == {"password": True}
    secrets.set(project, "core", {"token": "t"})
    assert secrets.get(project, "core") == {"password": "s3cret-value", "token": "t"}
    secrets.set(project, "core", {"password": ""})
    assert secrets.get(project, "core") == {"token": "t"}
    secrets.delete(project, "core")
    assert secrets.get(project, "core") == {}


def test_secrets_never_plain(project):
    secrets.set(project, "core", {"password": "very-plain-text-password"})
    enc = project.path / "secrets.json.enc"
    assert enc.exists()
    assert not (project.path / "secrets.json").exists()
    for f in project.path.rglob("*"):
        if f.is_file() and f.name != ".secret.key":
            assert b"very-plain-text-password" not in f.read_bytes()
    assert (project.path / ".secret.key").exists()
    if os.name != "nt":
        assert (os.stat(project.path / ".secret.key").st_mode & 0o777) == 0o600


def test_secrets_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ETLPLUS_SECRET_KEY", "any passphrase works")
    p = config.Project.init(tmp_path / "p")
    secrets.set(p, "x", {"password": "pw"})
    assert not (p.path / ".secret.key").exists()
    assert secrets.get(p, "x") == {"password": "pw"}
    monkeypatch.setenv("ETLPLUS_SECRET_KEY", "another key")
    with pytest.raises(RuntimeError):
        secrets.get(p, "x")


# config

def test_project_init_load_save(project):
    assert (project.path / "etlplus.yaml").exists()
    project.save_connection("core", {"kind": "postgres", "host": "h", "user": "u", "password": "pw",
                                     "options": {"sslmode": "prefer", "client_secret": "x"}})
    text = (project.path / "connections.yaml").read_text(encoding="utf-8")
    assert "pw" not in text and "client_secret" not in text and "password" not in text
    p2 = config.Project.load(project.path)
    assert p2.connections["core"]["host"] == "h"
    assert p2.connections["core"]["role"] == "source"
    assert p2.resolve("landing") == project.path / "landing"
    assert p2.stage_dir() == project.path / "stage"
    assert p2.db_path() == project.path / "runs.sqlite"
    secrets.set(p2, "core", {"password": "pw"})
    p2.delete_connection("core")
    assert "core" not in config.Project.load(project.path).connections
    assert secrets.get(p2, "core") == {}


def test_project_bad_kind_and_name(project):
    with pytest.raises(ValueError):
        project.save_connection("x", {"kind": "nosuch"})
    with pytest.raises(ValueError):
        project.save_connection("../bad", {"kind": "folder", "path": "x"})


def test_project_load_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.Project.load(tmp_path / "nothing")


def test_save_job_round_trip(project, monkeypatch):
    import etlplus
    try:
        from etlplus import runner
        monkeypatch.setattr(runner, "validate_job", lambda p, j: [])
    except ImportError:
        pass
    job = {"name": "j1", "source": {"connection": "c", "table": "t"},
           "target": {"connection": "w", "table": "t", "mode": "replace"}}
    project.save_job(job)
    assert config.Project.load(project.path).jobs["j1"]["source"]["table"] == "t"
    project.delete_job("j1")
    assert "j1" not in config.Project.load(project.path).jobs


# connections

def _sqlite_db(project, rel="data/core.db"):
    p = project.path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE customers (id INTEGER, name TEXT, amount REAL, updated_at TEXT)")
    rows = [(i, f"c{i}", i * 1.5, f"2026-10-{(i % 28) + 1:02d} 10:00:00") for i in range(1, 101)]
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", rows)
    con.execute("CREATE VIEW v_customers AS SELECT id, name FROM customers")
    con.commit()
    con.close()
    project.save_connection("core", {"kind": "sqlite", "path": rel, "role": "source"})
    return p


def test_sqlite_connection(project):
    _sqlite_db(project)
    r = connections.test(project, "core")
    assert r["ok"], r
    assert r["server"].startswith("SQLite")
    assert [s["name"] for s in r["steps"]][:3] == ["settings", "driver", "connect"]
    tables = connections.list_tables(project, "core")
    names = {t["name"]: t["kind"] for t in tables}
    assert names == {"customers": "table", "v_customers": "view"}
    cols = connections.list_columns(project, "core", "customers")
    assert [c["name"] for c in cols] == ["id", "name", "amount", "updated_at"]
    pv = connections.preview(project, "core", "customers", limit=5)
    assert pv["columns"][0] == "id" and len(pv["rows"]) == 5
    pv = connections.preview(project, "core", "SELECT id FROM customers WHERE id < 3")
    assert pv["rows"] == [[1], [2]]
    with pytest.raises(GuardError):
        connections.preview(project, "core", "SELECT 1; DELETE FROM customers")


def test_sqlite_source_is_read_only(project):
    _sqlite_db(project)
    conn = connections.connect(project, "core")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM customers")
    conn.close()
    with pytest.raises(connections.ConnectionError_):
        connections.connect(project, "core", read_only=False)


def test_duckdb_connection(project):
    p = project.path / "wh.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE SCHEMA s; CREATE TABLE s.t AS SELECT range AS id, 'x' || range AS v FROM range(10)")
    con.close()
    project.save_connection("wh", {"kind": "duckdb", "path": "wh.duckdb", "role": "source"})
    r = connections.test(project, "wh")
    assert r["ok"], r
    assert any(t["name"] == "t" and t["schema"] == "s" for t in connections.list_tables(project, "wh"))
    assert [c["name"] for c in connections.list_columns(project, "wh", "s.t")] == ["id", "v"]
    conn = connections.connect(project, "wh")
    with pytest.raises(duckdb.Error):
        conn.execute("DELETE FROM s.t")
    conn.close()


def test_folder_connection(project):
    (project.path / "landing").mkdir()
    (project.path / "landing" / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    project.save_connection("landing", {"kind": "folder", "path": "landing", "role": "both"})
    r = connections.test(project, "landing")
    assert r["ok"], r
    assert [s["name"] for s in r["steps"]] == ["settings", "path", "read", "write"]
    t = connections.list_tables(project, "landing")
    assert t[0]["name"] == "a.csv" and t[0]["format"] == "csv"
    assert [c["name"] for c in connections.list_columns(project, "landing", "a.csv")] == ["x", "y"]
    assert connections.preview(project, "landing", "a.csv")["rows"] == [[1, 2]]


def test_folder_outside_project_refused(project):
    project.save_connection("bad", {"kind": "folder", "path": "../elsewhere"})
    r = connections.test(project, "bad")
    assert not r["ok"] and "leaves the project" in r["steps"][0]["detail"]


def test_test_with_unsaved_override(project):
    _sqlite_db(project, "data/other.db")
    r = connections.test(project, "new_one", {"kind": "sqlite", "path": "data/other.db",
                                              "password": "temp-only"})
    assert r["ok"], r
    assert "new_one" not in config.Project.load(project.path).connections
    assert secrets.get(project, "new_one") == {}


def test_test_reports_missing_driver_or_host(project):
    r = connections.test(project, "pg", {"kind": "postgres", "user": "u"})
    assert not r["ok"] and r["steps"][0]["name"] == "settings"


def test_password_resolution(project, monkeypatch):
    project.save_connection("pg", {"kind": "postgres", "host": "h", "user": "u", "password_env": "PG_PW"})
    monkeypatch.setenv("PG_PW", "from-env")
    assert connections.spec_for(project, "pg")["_password"] == "from-env"
    secrets.set(project, "pg", {"password": "from-store"})
    assert connections.spec_for(project, "pg")["_password"] == "from-store"
    assert connections.spec_for(project, "pg", {"password": "typed"})["_password"] == "typed"


def test_postgres_read_only_mocked(project, monkeypatch):
    calls = {}

    class FakeConn:
        read_only = False

    class FakePsycopg:
        @staticmethod
        def connect(**kw):
            calls.update(kw)
            return FakeConn()

    monkeypatch.setattr(connections, "driver", lambda kind: FakePsycopg)
    project.save_connection("pg", {"kind": "postgres", "host": "h", "port": 5433, "database": "d",
                                   "user": "u", "options": {"sslmode": "prefer"}})
    secrets.set(project, "pg", {"password": "pw"})
    conn = connections.connect(project, "pg")
    assert conn.read_only is True
    assert calls["password"] == "pw" and calls["port"] == 5433 and calls["sslmode"] == "prefer"


def test_sqlserver_conn_str():
    s = connections.sqlserver_conn_str(
        {"host": "srv", "port": 1433, "database": "dw", "user": "etl"}, "p;w}d", True,
        installed=["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"])
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in s
    assert "SERVER=srv,1433" in s and "ApplicationIntent=ReadOnly" in s
    assert "PWD={p;w}}d}" in s
    assert "TrustServerCertificate=yes" in s
    s = connections.sqlserver_conn_str({"host": "srv"}, None, False, installed=["SQL Server"])
    assert "Trusted_Connection=yes" in s and "ApplicationIntent" not in s
    with pytest.raises(connections.ConnectionError_):
        connections.sqlserver_driver({}, installed=[])


def test_quoting():
    assert connections.quote_table("postgres", "public.customers") == '"public"."customers"'
    assert connections.quote_table("postgres", "PUBLIC.CUSTOMERS") == '"public"."customers"'
    assert connections.quote_table("postgres", 'public."MixedCase"') == '"public"."MixedCase"'
    assert connections.quote_table("sqlserver", "dbo.my table") == "[dbo].[my table]"
    assert connections.quote_table("mysql", "db.t") == "`db`.`t`"
    assert connections.quote_table("oracle", "core.customers") == '"CORE"."CUSTOMERS"'
    assert connections.quote_ident("postgres", 'a"b') == '"a""b"'


def test_driver_status():
    st = connections.driver_status()
    assert set(connections.KINDS) <= set(st)
    assert st["duckdb"]["installed"] and st["sqlite"]["installed"]
    assert st["postgres"]["package"].startswith("psycopg")


# window SQL

def test_window_sql_per_dialect():
    w = (dt.date(2026, 10, 1), dt.date(2026, 10, 4))
    where, params = sources.window_sql("postgres", "updated_at", w)
    assert where == '"updated_at" >= %s AND "updated_at" < %s'
    assert params == [dt.date(2026, 10, 1), dt.date(2026, 10, 4)]
    where, _ = sources.window_sql("sqlserver", "updated_at", w)
    assert where == "[updated_at] >= ? AND [updated_at] < ?"
    where, p = sources.window_sql("oracle", "updated_at", w)
    assert where == '"UPDATED_AT" >= :1 AND "UPDATED_AT" < :2'
    assert p[0] == dt.datetime(2026, 10, 1)
    where, p = sources.window_sql("sqlite", "updated_at", w)
    assert p == ["2026-10-01", "2026-10-04"]
    where, p = sources.window_sql("mysql", "updated_at", None, watermark=5)
    assert where == "`updated_at` > %s" and p == [5]


def test_source_sql_never_wraps_column():
    src = {"table": "public.tx", "columns": ["id", "amount"], "incremental": {"column": "value_date"}}
    sql, params = sources.source_sql("postgres", src, ("2026-10-01", "2026-10-02"))
    assert sql == ('SELECT "id", "amount" FROM "public"."tx" '
                   'WHERE "value_date" >= %s AND "value_date" < %s')
    assert "date(" not in sql.lower() and "cast(" not in sql.lower()
    src = {"query": "SELECT * FROM tx WHERE note LIKE 'a%'", "incremental": {"column": "d"}}
    sql, _ = sources.source_sql("postgres", src, ("2026-10-01", "2026-10-02"))
    assert sql == "SELECT * FROM (SELECT * FROM tx WHERE note LIKE 'a%%') q WHERE \"d\" >= %s AND \"d\" < %s"
    sql, _ = sources.source_sql("sqlserver", {"table": "dbo.t"}, None)
    assert sql == "SELECT * FROM [dbo].[t]"
    with pytest.raises(GuardError):
        sources.source_sql("postgres", {"query": "DELETE FROM t"}, None)


def test_window_for():
    job = {"source": {"incremental": {"column": "d", "lookback_days": 3}}}
    today = dt.date(2026, 11, 1)
    assert stage.window_for(job, today, None) == (dt.date(2026, 10, 29), today)
    assert stage.window_for({"source": {}}, today, None) is None
    assert stage.window_for({"source": {"incremental": {"column": "d", "watermark": True}}}, today, None) is None
    assert stage.window_for(job, today, ("2026-10-01", "2026-10-05")) == (dt.date(2026, 10, 1), dt.date(2026, 10, 5))
    with pytest.raises(ValueError):
        stage.window_for(job, today, ("2026-10-05", "2026-10-01"))


# sources: database

def test_read_sqlite_table_with_window(project):
    _sqlite_db(project)
    job = {"name": "j", "source": {"connection": "core", "table": "customers", "columns": ["id", "updated_at"],
                                   "incremental": {"column": "updated_at"}, "chunk_rows": 7}}
    batches = list(sources.read(project, job, (dt.date(2026, 10, 1), dt.date(2026, 10, 3)), None))
    t = pa.Table.from_batches(batches)
    assert t.column_names == ["id", "updated_at"]
    days = {v[:10] for v in t.column("updated_at").to_pylist()}
    assert days == {"2026-10-01", "2026-10-02"}
    assert all(b.num_rows <= 7 for b in batches)


def test_read_query_and_empty(project):
    _sqlite_db(project)
    job = {"name": "j", "source": {"connection": "core", "query": "SELECT id, name FROM customers WHERE id < 0"}}
    batches = list(sources.read(project, job, None, None))
    t = pa.Table.from_batches(batches)
    assert t.num_rows == 0 and t.column_names == ["id", "name"]


def test_read_duckdb_streams(project):
    p = project.path / "src.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE TABLE t AS SELECT range AS id, DATE '2026-10-01' + CAST(range % 5 AS INTEGER) AS d FROM range(1000)")
    con.close()
    project.save_connection("dk", {"kind": "duckdb", "path": "src.duckdb"})
    job = {"name": "j", "source": {"connection": "dk", "table": "t", "chunk_rows": 100,
                                   "incremental": {"column": "d"}}}
    t = pa.Table.from_batches(list(sources.read(project, job, ("2026-10-02", "2026-10-04"), None)))
    assert t.num_rows == 400


def test_rows_schema_locked_on_first_chunk():
    c = sources.Conform()
    b1 = c(sources.rows_to_batch(["a", "b"], [(1, None), (2, None)]))
    assert b1.schema.field("b").type == pa.string()
    b2 = c(sources.rows_to_batch(["a", "b"], [(3, 5)], c.schema))
    assert b2.schema == b1.schema and b2.column(1).to_pylist() == ["5"]


def test_watermark_mode(project, monkeypatch):
    _sqlite_db(project)
    store = pytest.importorskip("etlplus.store")
    monkeypatch.setattr(store, "watermark", lambda p, j: 95)
    job = {"name": "j", "source": {"connection": "core", "table": "customers",
                                   "incremental": {"column": "id", "watermark": True}}}
    t = pa.Table.from_batches(list(sources.read(project, job, None, None)))
    assert sorted(t.column("id").to_pylist()) == [96, 97, 98, 99, 100]


# sources: files

@pytest.fixture
def landing(project):
    d = project.path / "landing"
    d.mkdir()
    project.save_connection("landing", {"kind": "folder", "path": "landing", "role": "source"})
    return d


def _read(project, files, fmt=None, options=None, **src):
    job = {"name": "f", "source": {"connection": "landing", "files": files, "format": fmt,
                                   "options": options or {}, **src}}
    return pa.Table.from_batches(list(sources.read(project, job, src.pop("window", None), None)))


def test_read_csv(project, landing):
    (landing / "a.csv").write_text("id,name,amount\n1,x,1.5\n2,y,\n", encoding="utf-8")
    t = _read(project, "a.csv", "csv")
    assert t.column_names == ["id", "name", "amount"]
    assert t.column("amount").to_pylist() == [1.5, None]


def test_read_csv_encoding_and_skip(project, landing):
    (landing / "b.csv").write_bytes("junk line\nid;city\n1;Lyon\n2;Orléans\n".encode("latin-1"))
    t = _read(project, "b.csv", "csv", {"delimiter": ";", "encoding": "latin-1", "skip_rows": 1})
    assert t.column("city").to_pylist() == ["Lyon", "Orléans"]


def test_read_txt_any_delimiter(project, landing):
    (landing / "p.txt").write_text("a|b\n1|2\n3|4\n", encoding="utf-8")
    assert _read(project, "p.txt", "txt").column("b").to_pylist() == [2, 4]
    (landing / "m.txt").write_text("a~~b\n1~~2\n", encoding="utf-8")
    assert _read(project, "m.txt", "txt", {"delimiter": "~~"}).to_pylist() == [{"a": "1", "b": "2"}]
    (landing / "t.txt").write_text("a\tb\nx\ty\n", encoding="utf-8")
    assert _read(project, "t.txt", "txt", {"delimiter": "\\t", "types": "text"}).to_pylist() == [{"a": "x", "b": "y"}]


def test_read_fixed(project, landing):
    (landing / "f.txt").write_text("0001Alice     12\n0002Bob        7\n", encoding="utf-8")
    t = _read(project, "f.txt", "fixed", {"widths": [4, 10, 3], "names": ["id", "name", "n"]})
    assert t.to_pylist() == [{"id": "0001", "name": "Alice", "n": "12"}, {"id": "0002", "name": "Bob", "n": "7"}]


def test_read_parquet(project, landing):
    pq.write_table(pa.table({"a": [1, 2, 3]}), landing / "x.parquet")
    assert _read(project, "x.parquet").column("a").to_pylist() == [1, 2, 3]


def test_read_json_and_jsonl(project, landing):
    (landing / "r.json").write_text(json.dumps({"data": [{"a": 1}, {"a": 2, "b": "z"}]}), encoding="utf-8")
    t = _read(project, "r.json", "json")
    assert t.to_pylist() == [{"a": 1, "b": None}, {"a": 2, "b": "z"}]
    (landing / "r.jsonl").write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
    assert _read(project, "r.jsonl", "jsonl", chunk_rows=1).column("a").to_pylist() == [1, 2]


def test_read_excel(project, landing):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["id", "name", "when"])
    ws.append([1, "a", dt.datetime(2026, 10, 1)])
    ws.append([2, "b", dt.datetime(2026, 10, 2)])
    ws.append([None, None, None])
    wb.save(landing / "x.xlsx")
    t = _read(project, "x.xlsx", "excel", {"sheet": "Data"})
    assert t.column_names == ["id", "name", "when"]
    assert t.column("id").to_pylist() == [1, 2]


def test_read_glob_many_files_and_window(project, landing):
    (landing / "in").mkdir()
    (landing / "in" / "d1.csv").write_text("id,day\n1,2026-10-01\n2,2026-10-02\n", encoding="utf-8")
    (landing / "in" / "d2.csv").write_text("id,day\n3,2026-10-03\n4,2026-10-04\n", encoding="utf-8")
    (landing / "in" / "d3.csv.partial").write_text("id,day\n9,2026-10-03\n", encoding="utf-8")
    t = _read(project, "in/d*.csv", "csv", {"filename_column": "src"})
    assert t.column("id").to_pylist() == [1, 2, 3, 4]
    assert t.column("src").to_pylist()[-1] == "in/d2.csv"
    job = {"name": "f", "source": {"connection": "landing", "files": "in/*.csv", "format": "csv",
                                   "incremental": {"column": "day"}}}
    t = pa.Table.from_batches(list(sources.read(project, job, (dt.date(2026, 10, 2), dt.date(2026, 10, 4)), None)))
    assert t.column("id").to_pylist() == [2, 3]


def test_read_files_missing_and_escape(project, landing):
    with pytest.raises(FileNotFoundError):
        _read(project, "none_*.csv", "csv")
    with pytest.raises(ValueError):
        _read(project, "../etlplus.yaml", "csv")


# stage

def test_stage_write_manifest(project):
    run_id = stage.new_run_id()
    batches = [pa.record_batch({"a": [1, 2], "b": ["x", None]}), pa.record_batch({"a": [3], "b": ["z"]})]
    m = stage.write(project, "job1", run_id, iter(batches), (dt.date(2026, 10, 1), dt.date(2026, 10, 2)),
                    "core:t", None)
    f = stage.path(project, "job1", run_id)
    assert f.exists() and not f.with_name(f.name + ".partial").exists()
    assert m["rows"] == 3 and m["file"] == "extract.parquet" and m["kind"] == "extract"
    assert m["columns"] == [{"name": "a", "type": "int64"}, {"name": "b", "type": "string"}]
    assert m["window"] == ["2026-10-01", "2026-10-02"]
    assert m["bytes"] == f.stat().st_size
    import hashlib
    assert m["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    on_disk = json.loads((f.parent / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == m
    assert pq.read_table(f).num_rows == 3
    assert stage.latest(project, "job1") == m
    for k in ("job", "run_id", "kind", "rows", "columns", "window", "source", "file", "bytes", "sha256",
              "started_at", "finished_at"):
        assert k in m


def test_stage_partial_never_visible(project):
    run_id = stage.new_run_id()
    seen = {}

    def batches():
        yield pa.record_batch({"a": [1]})
        d = stage.path(project, "job2", run_id).parent
        seen["files"] = sorted(p.name for p in d.iterdir())
        seen["latest"] = stage.latest(project, "job2")
        raise RuntimeError("source broke")

    with pytest.raises(RuntimeError):
        stage.write(project, "job2", run_id, batches(), None, "x", None)
    assert seen["files"] == ["extract.parquet.partial"]
    assert seen["latest"] is None
    d = stage.path(project, "job2", run_id).parent
    assert list(d.iterdir()) == []
    assert stage.latest(project, "job2") is None


def test_stage_empty_and_schema_cast(project):
    m = stage.write(project, "job3", stage.new_run_id(), iter([]), None, "x", None)
    assert m["rows"] == 0 and m["columns"] == []
    m = stage.write(project, "job3", stage.new_run_id(),
                    iter([pa.record_batch({"a": pa.array([None, None], pa.null())}),
                          pa.record_batch({"a": [5]})]), None, "x", None)
    assert m["columns"] == [{"name": "a", "type": "string"}] and m["rows"] == 3


def test_stage_latest_and_cleanup(project):
    ids = []
    for i in range(4):
        rid = f"2026110{i}T020000Z-ab1{i}"
        stage.write(project, "j", rid, iter([pa.record_batch({"a": [i]})]), None, "x", None)
        ids.append(rid)
    assert stage.latest(project, "j")["run_id"] == ids[-1]
    assert stage.latest(project, "j", "transformed") is None
    stage.cleanup(project, "j", keep=2)
    left = sorted(d.name for d in (project.stage_dir() / "j").iterdir())
    assert left == ids[-2:]
    assert len(stage.runs(project, "j")) == 2


def test_extract_end_to_end(project):
    _sqlite_db(project)
    job = {"name": "cust", "source": {"connection": "core", "table": "customers", "chunk_rows": 30}}
    rid = stage.new_run_id()
    m = stage.write(project, "cust", rid, sources.read(project, job, None, None), None,
                    sources.describe(project, job), None)
    assert m["rows"] == 100 and m["source"] == "core:customers"
    assert pq.read_table(stage.path(project, "cust", rid)).num_rows == 100


def test_new_run_id_format():
    rid = stage.new_run_id()
    assert stage.RUN_ID_RE.match(rid)


class _Cur:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, *a):
        self.log.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.log = []

    def cursor(self, *a):
        return _Cur(self.log)


def test_mysql_and_oracle_read_only_mocked(project, monkeypatch):
    seen = {}

    class FakeDriver:
        @staticmethod
        def connect(**kw):
            seen.update(kw)
            return _Conn()

        @staticmethod
        def drivers():
            return ["ODBC Driver 17 for SQL Server"]

    monkeypatch.setattr(connections, "driver", lambda kind: FakeDriver)
    project.save_connection("my", {"kind": "mysql", "host": "h", "user": "u", "database": "d"})
    assert connections.connect(project, "my").log == ["SET SESSION TRANSACTION READ ONLY"]
    project.save_connection("ora", {"kind": "oracle", "host": "h", "user": "u", "database": "svc"})
    c = connections.connect(project, "ora")
    assert c.log == ["SET TRANSACTION READ ONLY"] and seen["dsn"] == "h:1521/svc"
    project.save_connection("ora_t", {"kind": "oracle", "host": "h", "user": "u", "database": "svc", "role": "both"})
    assert connections.connect(project, "ora_t").log == []


def test_sqlserver_connect_mocked(project, monkeypatch):
    seen = {}

    class FakeOdbc:
        @staticmethod
        def connect(s, **kw):
            seen["s"] = s
            return _Conn()

        @staticmethod
        def drivers():
            return ["ODBC Driver 17 for SQL Server"]

    monkeypatch.setattr(connections, "driver", lambda kind: FakeOdbc)
    project.save_connection("ms", {"kind": "sqlserver", "host": "h", "user": "u", "database": "d"})
    secrets.set(project, "ms", {"password": "pw"})
    connections.connect(project, "ms")
    assert "ApplicationIntent=ReadOnly" in seen["s"] and "ODBC Driver 17" in seen["s"] and "PWD=pw" in seen["s"]


def test_tables_and_columns_sql_are_guarded():
    for kind in ("postgres", "sqlserver", "mysql", "oracle", "sqlite", "duckdb"):
        for schema in (None, "s"):
            check_select(connections.tables_sql(kind, schema)[0])
        for t in ("t", "s.t"):
            check_select(connections.columns_sql(kind, t)[0])
        check_select(connections.VERSION_SQL[kind])
