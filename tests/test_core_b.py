"""core-b: transform, targets, runner, store, scheduler, notify, cli."""
import json
import sqlite3
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from etlplus import notify, runner, scheduler, store, targets, transform

config = pytest.importorskip("etlplus.config")
stage = pytest.importorskip("etlplus.stage")


# ---------------------------------------------------------------- fixtures

def _write_yaml(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "p"
    config.Project.init(root)
    src = root / "src.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE tx (id INTEGER, day TEXT, amount REAL, cust TEXT)")
    rows = []
    for d in range(1, 11):
        for i in range(5):
            rows.append((d * 100 + i, f"2026-10-{d:02d}", float(d * 10 + i), f"c{i}"))
    con.executemany("INSERT INTO tx VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    _write_yaml(root / "connections.yaml", {"connections": {
        "src": {"kind": "sqlite", "path": "./src.db", "role": "source"},
        "sq": {"kind": "sqlite", "path": "./wh.db", "role": "target"},
        "dk": {"kind": "duckdb", "path": "./wh.duckdb", "role": "target"},
        "fo": {"kind": "folder", "path": "./out", "role": "both"},
    }})
    return config.Project.load(root)


def _job(name="j", conn="sq", mode="replace", **target):
    t = {"connection": conn, "table": "t", "mode": mode, "create": True}
    t.update(target)
    return {"name": name, "source": {"connection": "src", "table": "tx",
                                     "incremental": {"column": "day", "lookback_days": 3}},
            "target": t}


def _stage(project, job_name, rows, window=None, run_id=None):
    run_id = run_id or stage.new_run_id()
    time.sleep(0.002)
    table = pa.Table.from_pylist(rows)
    return stage.write(project, job_name, run_id, table.to_batches(), window, "test", None)


def _rows(project, conn, table="t", order="id"):
    if conn == "sq":
        c = sqlite3.connect(project.resolve("wh.db"))
        try:
            return c.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        finally:
            c.close()
    if conn == "dk":
        c = duckdb.connect(str(project.resolve("wh.duckdb")))
        try:
            return c.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        finally:
            c.close()
    out = project.resolve("out")
    single = out / f"{table}.parquet"
    c = duckdb.connect()
    try:
        if single.exists():
            return c.execute(f"SELECT * FROM read_parquet('{single.as_posix()}') ORDER BY {order}").fetchall()
        return c.execute(f"SELECT * FROM read_parquet('{(out / table).as_posix()}/part-*.parquet', "
                         f"union_by_name=true) ORDER BY {order}").fetchall()
    finally:
        c.close()


def _day_rows(days, bump=0.0):
    return [{"id": d * 100 + i, "day": f"2026-10-{d:02d}", "amount": float(d * 10 + i) + bump, "cust": f"c{i}"}
            for d in days for i in range(3)]


ALL_KINDS = ["sq", "dk", "fo"]


# ---------------------------------------------------------------- transform

def test_validate_plain_messages():
    with pytest.raises(ValueError, match="Step 1: 'explode' is not a step kind"):
        transform.validate([{"explode": "x"}])
    with pytest.raises(ValueError, match="Step 1 \\(cast\\): 'INT; DROP' is not a type"):
        transform.validate([{"cast": {"a": "INT; DROP"}}])
    with pytest.raises(ValueError, match="Step 2 \\(sql\\)"):
        transform.validate([{"drop": ["a"]}, {"sql": "DELETE FROM input"}])
    with pytest.raises(ValueError, match="cannot contain ';'"):
        transform.validate([{"filter": "1=1; DROP TABLE x"}])
    with pytest.raises(ValueError, match="exactly one kind"):
        transform.validate([{"drop": ["a"], "filter": "x"}])
    transform.validate([{"rename": {"a": "b"}}, {"cast": {"b": "DECIMAL(18, 2)"}},
                        {"dedupe": {"key": ["b"], "order_by": "b DESC"}}])


def test_every_step_and_preview_writes_nothing(proj):
    rows = [{"id": 1, "at": "2026-10-01 10:00:00", "seg": "a", "old": 1},
            {"id": 1, "at": "2026-10-02 10:00:00", "seg": "a", "old": 1},
            {"id": 2, "at": "2026-10-01 09:00:00", "seg": None, "old": 2},
            {"id": 3, "at": "2026-10-03 08:00:00", "seg": "b", "old": 3}]
    m = _stage(proj, "tj", rows)
    job = {"name": "tj", "transform": [
        {"rename": {"seg": "segment"}},
        {"cast": {"at": "TIMESTAMP", "id": "VARCHAR"}},
        {"filter": "segment IS NOT NULL"},
        {"derive": {"load_tag": "'x'", "old": "old * 10"}},
        {"drop": ["load_tag"]},
        {"dedupe": {"key": ["id"], "order_by": "at DESC"}},
        {"sql": "WITH a AS (SELECT * FROM input) SELECT id, \"at\", segment, old FROM a ORDER BY id"},
    ]}
    run_dir = stage.path(proj, "tj", m["run_id"]).parent
    before = sorted(p.name for p in run_dir.iterdir())
    pv = transform.preview(proj, job)
    assert sorted(p.name for p in run_dir.iterdir()) == before
    assert [c["name"] for c in pv["columns"]] == ["id", "at", "segment", "old"]
    assert pv["rows"] == [["1", "2026-10-02T10:00:00", "a", 10], ["3", "2026-10-03T08:00:00", "b", 30]]
    assert pv["rows_in"] == 4 and pv["rows_out"] == 2

    tm = transform.apply(proj, job, m["run_id"], None)
    assert tm["kind"] == "transformed" and tm["rows"] == 2 and tm["window"] is None
    assert (run_dir / "transformed.parquet").exists()
    assert not list(run_dir.glob("*.partial"))
    assert stage.latest(proj, "tj", "transformed")["run_id"] == m["run_id"]


def test_step_error_names_the_step(proj):
    _stage(proj, "tj", [{"a": 1}])
    with pytest.raises(ValueError, match=r"Step 2 \(rename\): there is no column 'nope'"):
        transform.preview(proj, {"name": "tj", "transform": [{"filter": "a > 0"}, {"rename": {"nope": "x"}}]})
    with pytest.raises(ValueError, match=r"Step 1 \(filter\)"):
        transform.preview(proj, {"name": "tj", "transform": [{"filter": "missing_col > 0"}]})


def test_preview_without_stage(proj):
    with pytest.raises(ValueError, match="Nothing is staged"):
        transform.preview(proj, {"name": "none", "transform": []})


# ---------------------------------------------------------------- targets: every mode on sqlite, duckdb, folder

@pytest.mark.parametrize("conn", ALL_KINDS)
def test_replace_is_idempotent(proj, conn):
    job = _job(conn=conn)
    m = _stage(proj, "j", _day_rows(range(1, 5)))
    r1 = targets.load(proj, job, m, None)
    r2 = targets.load(proj, job, m, None)
    assert r1["rows_loaded"] == r2["rows_loaded"] == 12
    assert len(_rows(proj, conn)) == 12
    m2 = _stage(proj, "j", _day_rows([1]))
    targets.load(proj, job, m2, None)
    assert len(_rows(proj, conn)) == 3
    st = targets.status(proj, job)
    assert st["exists"] and st["rows"] == 3 and {c["name"] for c in st["columns"]} == {"id", "day", "amount", "cust"}


@pytest.mark.parametrize("conn", ALL_KINDS)
def test_append_deletes_the_window_slice(proj, conn):
    job = _job(conn=conn, mode="append")
    first = _stage(proj, "j", _day_rows([1, 2, 3]), window=(date(2026, 10, 1), date(2026, 10, 4)))
    targets.load(proj, job, first, None)
    assert len(_rows(proj, conn)) == 9
    second = _stage(proj, "j", _day_rows([3, 4], bump=0.5), window=(date(2026, 10, 3), date(2026, 10, 5)))
    for _ in range(2):
        res = targets.load(proj, job, second, None)
    assert res["deleted"] == 6
    got = _rows(proj, conn)
    assert len(got) == 12
    by_id = {r[0]: r for r in got}
    assert by_id[300][2] == 30.5 and by_id[100][2] == 10.0
    assert len(by_id) == 12


@pytest.mark.parametrize("conn", ALL_KINDS)
def test_upsert_merges_on_key(proj, conn):
    job = _job(conn=conn, mode="upsert", key=["id"])
    targets.load(proj, job, _stage(proj, "j", _day_rows([1, 2])), None)
    res = targets.load(proj, job, _stage(proj, "j", _day_rows([2, 3], bump=1.0)), None)
    assert res["deleted"] == 3
    got = {r[0]: r for r in _rows(proj, conn)}
    assert len(got) == 9
    assert got[100][2] == 10.0 and got[200][2] == 21.0 and got[300][2] == 31.0
    again = _rows(proj, conn)
    targets.load(proj, job, _stage(proj, "j", _day_rows([2, 3], bump=1.0)), None)
    assert _rows(proj, conn) == again


@pytest.mark.parametrize("conn", ALL_KINDS)
def test_upsert_refuses_duplicate_keys(proj, conn):
    job = _job(conn=conn, mode="upsert", key=["cust"])
    targets.load(proj, job, _stage(proj, "j", _day_rows([1])), None)
    with pytest.raises(targets.LoadError, match="more than once"):
        targets.load(proj, job, _stage(proj, "j", _day_rows([1, 2])), None)


def test_missing_table_without_create(proj):
    job = _job(conn="sq", create=False)
    with pytest.raises(targets.LoadError, match="does not exist"):
        targets.load(proj, job, _stage(proj, "j", _day_rows([1])), None)


def test_source_role_refused(proj):
    job = _job(conn="src")
    with pytest.raises(targets.LoadError, match="read only"):
        targets.load(proj, job, _stage(proj, "j", _day_rows([1])), None)


class _CancelAfter:
    def __init__(self, n):
        self.n = n

    def __call__(self, msg):
        pass

    def check(self):
        self.n -= 1
        if self.n < 0:
            raise runner.Cancelled("The run was cancelled.")


@pytest.mark.parametrize("conn", ["sq", "dk"])
def test_failed_load_leaves_target_unchanged(proj, conn):
    job = _job(conn=conn)
    targets.load(proj, job, _stage(proj, "j", _day_rows([1, 2])), None)
    before = _rows(proj, conn)
    bad = _day_rows([3, 4])
    bad[-1]["id"] = None
    if conn == "sq":
        c = sqlite3.connect(proj.resolve("wh.db"))
        c.execute("CREATE TABLE t2 (id INTEGER NOT NULL, day TEXT, amount REAL, cust TEXT)")
        c.execute("INSERT INTO t2 SELECT * FROM t")
        c.commit()
        c.close()
    else:
        c = duckdb.connect(str(proj.resolve("wh.duckdb")))
        c.execute("CREATE TABLE t2 (id BIGINT NOT NULL, day VARCHAR, amount DOUBLE, cust VARCHAR)")
        c.execute("INSERT INTO t2 SELECT * FROM t")
        c.close()
    job2 = _job(conn=conn, table="t2")
    with pytest.raises(Exception):
        targets.load(proj, job2, _stage(proj, "j", bad), None)
    assert _rows(proj, conn, "t2") == before
    # a cancel after the delete rolls back too
    with pytest.raises(runner.Cancelled):
        targets.load(proj, job, _stage(proj, "j", _day_rows([5])), _CancelAfter(0))
    assert _rows(proj, conn) == before


def test_extra_column_is_a_plain_error(proj):
    job = _job(conn="sq")
    targets.load(proj, job, _stage(proj, "j", _day_rows([1])), None)
    rows = _day_rows([2])
    for r in rows:
        r["extra"] = 1
    with pytest.raises(targets.LoadError, match="has no column 'extra'"):
        targets.load(proj, job, _stage(proj, "j", rows), None)
    assert len(_rows(proj, "sq")) == 3


def test_load_never_reads_partial(proj):
    m = _stage(proj, "j", _day_rows([1]))
    m = dict(m, file="extract.parquet.partial")
    with pytest.raises(targets.LoadError, match="partial"):
        targets.load(proj, _job(), m, None)


def test_ddl_per_dialect(proj):
    m = _stage(proj, "j", [{"id": 1, "amount": 1.5, "ok": True, "at": datetime(2026, 10, 1, 1, 2, 3),
                            "d": date(2026, 10, 1), "name": "x"}])
    assert targets.ddl(proj, _job(conn="dk"), m).startswith('CREATE TABLE "t" (')
    schema = pq.read_schema(targets.staged_file(proj, m))
    ms = targets.dialect("sqlserver").create_sql("dbo.t", schema)
    assert "[dbo].[t]" in ms and "[id] BIGINT" in ms and "[ok] BIT" in ms and "DATETIME2" in ms and "NVARCHAR(MAX)" in ms
    pg = targets.dialect("postgres").create_sql("public.t", schema)
    assert '"amount" DOUBLE PRECISION' in pg and '"at" TIMESTAMP' in pg and '"ok" BOOLEAN' in pg
    my = targets.dialect("mysql").create_sql("db.t", schema)
    assert "`db`.`t`" in my and "DATETIME(6)" in my
    ora = targets.dialect("oracle").create_sql("scott.t", schema)
    assert '"SCOTT"."T"' in ora and '"ID" NUMBER(19)' in ora and "VARCHAR2(4000 CHAR)" in ora
    assert "folder target" in targets.ddl(proj, _job(conn="fo"), m)


# ---------------------------------------------------------------- generated SQL for server dialects

def test_generated_sql_postgres():
    d = targets.dialect("postgres")
    assert d.delete_slice_sql("public.tx", "day") == 'DELETE FROM "public"."tx" WHERE "day" >= %s AND "day" < %s'
    assert d.clear_sql("public.tx") == 'TRUNCATE TABLE "public"."tx"'
    assert d.copy_sql('"public"."tx"', ["a", "b"]) == 'COPY "public"."tx" ("a", "b") FROM STDIN'
    up = d.upsert_sql("public.tx", ["id", "v"], ["id"])
    assert up[0] == 'DELETE FROM "public"."tx" t USING "etlplus_stage" s WHERE t."id" = s."id"'
    assert up[1].startswith('INSERT INTO "public"."tx" ("id", "v") SELECT')
    assert "ON COMMIT DROP" in d.create_stage_sql("public.tx", ["id", "v"])


def test_generated_sql_sqlserver():
    d = targets.dialect("sqlserver")
    assert d.table("dbo.transaction") == "[dbo].[transaction]"
    assert d.quote("a]b") == "[a]]b]"
    assert d.delete_slice_sql("dbo.tx", "day") == "DELETE FROM [dbo].[tx] WHERE [day] >= ? AND [day] < ?"
    merge = d.upsert_sql("dbo.tx", ["id", "k2", "v"], ["id", "k2"])[0]
    assert merge.startswith("MERGE INTO [dbo].[tx] WITH (HOLDLOCK) AS t\nUSING [#etlplus_stage] AS s")
    assert "ON t.[id] = s.[id] AND t.[k2] = s.[k2]" in merge
    assert "WHEN MATCHED THEN UPDATE SET t.[v] = s.[v]" in merge
    assert merge.endswith("WHEN NOT MATCHED BY TARGET THEN INSERT ([id], [k2], [v]) VALUES (s.[id], s.[k2], s.[v]);")
    keys_only = d.upsert_sql("dbo.tx", ["id"], ["id"])[0]
    assert "WHEN MATCHED" not in keys_only.replace("WHEN NOT MATCHED", "")
    assert "SELECT TOP 0 [id] INTO [#etlplus_stage]" in d.create_stage_sql("dbo.tx", ["id"])
    bulk = d.bulk_insert_sql("[#etlplus_stage]", r"\\fs\share\dbo.tx.csv", r"\\fs\share\dbo.tx.err")
    for part in ("FORMAT = 'CSV'", "FIRSTROW = 2", "ROWTERMINATOR = '0x0a'", "CODEPAGE = '65001'",
                 "MAXERRORS = 0", r"ERRORFILE = '\\fs\share\dbo.tx.err'", "TABLOCK"):
        assert part in bulk


def test_generated_sql_mysql_and_oracle():
    my = targets.dialect("mysql")
    assert my.delete_slice_sql("db.tx", "day") == "DELETE FROM `db`.`tx` WHERE `day` >= %s AND `day` < %s"
    assert my.clear_sql("db.tx") == "DELETE FROM `db`.`tx`"
    assert my.upsert_sql("db.tx", ["id", "v"], ["id"])[0] == \
        "DELETE t FROM `db`.`tx` AS t JOIN `etlplus_stage` AS s ON t.`id` = s.`id`"
    assert my.insert_sql("db.tx", ["id", "v"]) == "INSERT INTO `db`.`tx` (`id`, `v`) VALUES (%s, %s)"
    ora = targets.dialect("oracle")
    assert ora.delete_slice_sql("scott.tx", "day") == 'DELETE FROM "SCOTT"."TX" WHERE "DAY" >= :1 AND "DAY" < :2'
    assert ora.insert_sql("tx", ["id", "v"]) == 'INSERT INTO "TX" ("ID", "V") VALUES (:1, :2)'
    m = ora.upsert_sql("tx", ["id", "v"], ["id"])[0]
    assert m == ('MERGE INTO "TX" t USING (SELECT :1 AS "ID", :2 AS "V" FROM dual) s ON (t."ID" = s."ID") '
                 'WHEN MATCHED THEN UPDATE SET t."V" = s."V" '
                 'WHEN NOT MATCHED THEN INSERT ("ID", "V") VALUES (s."ID", s."V")')


class FakeCursor:
    def __init__(self, log, cols):
        self.log, self.cols = log, cols
        self.description = None
        self.rowcount = 0
        self._one = None
        self.fast_executemany = False
        self.many = []

    def execute(self, sql, *params):
        self.log.append(sql)
        self.description = None
        self._one = None
        if "OBJECT_ID(?, 'U')" in sql or "to_regclass" in sql:
            self._one = (1,) if "to_regclass" not in sql else (True,)
        elif "WHERE 1 = 0" in sql:
            self.description = [(c,) for c in self.cols]
        elif sql.startswith("DELETE"):
            self.rowcount = 7

    def executemany(self, sql, rows):
        self.log.append(sql)
        self.many.extend(rows)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []

    def copy(self, sql):
        self.log.append(sql)
        cur = self

        class _Copy:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write_row(self, row):
                cur.many.append(row)
        return _Copy()


class FakeConn:
    def __init__(self, cols):
        self.log = []
        self.cur = FakeCursor(self.log, cols)
        self.committed = self.rolled_back = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _server_project(proj, kind):
    proj.connections["srv"] = {"kind": kind, "host": "db.example.local", "database": "wh", "user": "u",
                               "role": "target"}
    return proj


def test_sqlserver_bulk_insert_mocked(proj, monkeypatch, tmp_path):
    _server_project(proj, "sqlserver")
    fake = FakeConn(["id", "day", "amount", "cust"])
    monkeypatch.setattr(targets, "connect", lambda *a, **k: fake)
    local = tmp_path / "landing"
    local.mkdir()
    (local / "dbo.tx.err").write_text("old")
    (local / "dbo.tx.err.Error.Txt").write_text("old")
    job = _job(conn="srv", mode="append", table="dbo.tx",
               bulk={"local_dir": str(local), "remote_dir": r"\\fs\share\etl"})
    m = _stage(proj, "j", _day_rows([1, 2]), window=(date(2026, 10, 1), date(2026, 10, 3)))
    res = targets.load(proj, job, m, None)
    assert res == {"rows_loaded": 6, "mode": "append", "target": "srv:dbo.tx", "deleted": 7, "created": False}
    assert fake.committed and not fake.rolled_back
    assert not (local / "dbo.tx.err").exists() and not (local / "dbo.tx.err.Error.Txt").exists()
    raw = (local / "dbo.tx.csv").read_bytes()
    assert b"\r\n" not in raw and raw.startswith(b"id,day,amount,cust\n") and raw.count(b"\n") == 7
    sql = "\n".join(fake.log)
    assert "DELETE FROM [dbo].[tx] WHERE [day] >= ? AND [day] < ?" in sql
    assert r"FROM '\\fs\share\etl\dbo.tx.csv'" in sql and "ROWTERMINATOR = '0x0a'" in sql
    assert "INSERT INTO [dbo].[tx] ([id], [day], [amount], [cust]) SELECT [id], [day], [amount], [cust] FROM [#etlplus_stage]" in sql


def test_sqlserver_upsert_mocked(proj, monkeypatch):
    _server_project(proj, "sqlserver")
    fake = FakeConn(["id", "day", "amount", "cust"])
    monkeypatch.setattr(targets, "connect", lambda *a, **k: fake)
    job = _job(conn="srv", mode="upsert", table="dbo.tx", key=["id"])
    targets.load(proj, job, _stage(proj, "j", _day_rows([1])), None)
    assert fake.cur.fast_executemany is True
    assert len(fake.cur.many) == 3
    assert any(s.startswith("MERGE INTO [dbo].[tx]") for s in fake.log)
    assert any("INSERT INTO [#etlplus_stage]" in s for s in fake.log)


def test_postgres_copy_mocked(proj, monkeypatch):
    _server_project(proj, "postgres")
    fake = FakeConn(["id", "day", "amount", "cust"])
    monkeypatch.setattr(targets, "connect", lambda *a, **k: fake)
    job = _job(conn="srv", mode="replace", table="public.tx")
    res = targets.load(proj, job, _stage(proj, "j", _day_rows([1, 2])), None)
    assert res["rows_loaded"] == 6 and fake.committed
    assert 'TRUNCATE TABLE "public"."tx"' in fake.log
    assert 'COPY "public"."tx" ("id", "day", "amount", "cust") FROM STDIN' in fake.log
    assert len(fake.cur.many) == 6


def test_server_failure_rolls_back_mocked(proj, monkeypatch):
    _server_project(proj, "mysql")
    fake = FakeConn(["id", "day", "amount", "cust"])

    def boom(sql, rows):
        raise RuntimeError("row 2 rejected")
    fake.cur.executemany = boom
    monkeypatch.setattr(targets, "connect", lambda *a, **k: fake)
    with pytest.raises(RuntimeError):
        targets.load(proj, _job(conn="srv", table="db.tx"), _stage(proj, "j", _day_rows([1])), None)
    assert fake.rolled_back and not fake.committed
    assert "DELETE FROM `db`.`tx`" in fake.log


def test_missing_driver_message(proj, monkeypatch):
    import builtins
    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "oracledb":
            raise ImportError("no")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(targets.LoadError, match=r"pip install etlplus\[oracle\]"):
        targets.connect(proj, "x", {"kind": "oracle"})


# ---------------------------------------------------------------- runner

def _save_job(proj, job):
    _write_yaml(Path(proj.path) / "jobs" / f"{job['name']}.yaml", job)
    proj.reload()


def test_validate_job_sentences(proj):
    bad = {"name": "bad name!", "source": {"connection": "nope", "table": "x", "query": "SELECT 1"},
           "transform": [{"cast": {"a": "NOT A TYPE;"}}],
           "target": {"connection": "src", "mode": "upsert"}, "schedule": "every day", "notify": "sometimes"}
    probs = runner.validate_job(proj, bad)
    text = " | ".join(probs)
    for part in ("job name", "does not exist", "exactly one of table, query or files", "Transform: Step 1",
                 "is a source (read only)", "needs a table", "upsert needs key columns", "not a valid cron",
                 "notify must be"):
        assert part in text, part
    assert all(p.endswith(".") or p.endswith(")") for p in probs)
    assert runner.validate_job(proj, _job()) == []
    assert any("append needs a window" in p for p in runner.validate_job(
        proj, {"name": "a", "source": {"connection": "src", "table": "tx"},
               "target": {"connection": "sq", "table": "t", "mode": "append"}}))


def test_run_end_to_end_and_rerun(proj):
    job = _job(mode="append")
    job["transform"] = [{"derive": {"amount2": "amount * 2"}}]
    _save_job(proj, job)
    rec = runner.run(proj, "j", window=("2026-10-03", "2026-10-06"))
    assert rec["status"] == "done", rec["error"]
    assert rec["rows"]["extracted"] == 15 and rec["rows"]["loaded"] == 15
    assert rec["window"] == ["2026-10-03", "2026-10-06"]
    assert set(rec["steps"]) == {"extract", "transform", "load"}
    rec2 = runner.run(proj, "j", window=("2026-10-03", "2026-10-06"))
    assert rec2["status"] == "done" and rec2["rows"]["deleted"] == 15
    assert len(_rows(proj, "sq")) == 15
    assert store.get(proj, rec["run_id"])["status"] == "done"
    assert runner.log_path(proj, "j", rec["run_id"]).exists()
    assert any("done" in line for line in runner.log_tail(proj, "j", rec["run_id"]))
    assert [r["run_id"] for r in store.runs(proj, job="j")] == [rec2["run_id"], rec["run_id"]]
    st = runner.state(proj)
    assert st["running"] == [] and st["last"]["j"]["run_id"] == rec2["run_id"]


def test_load_only_uses_latest_stage(proj):
    job = _job(mode="replace")
    _save_job(proj, job)
    first = runner.run(proj, "j", steps=("extract",), window=("2026-10-01", "2026-10-03"))
    assert first["status"] == "done" and "load" not in first["steps"]
    assert not Path(proj.resolve("wh.db")).exists() or not _table_exists(proj, "t")
    again = runner.run(proj, "j", steps=("load",))
    assert again["status"] == "done", again["error"]
    assert again["stage_run_id"] == first["run_id"] and again["run_id"] != first["run_id"]
    assert "extract" not in again["steps"]
    assert len(_rows(proj, "sq")) == 10


def test_load_only_needs_transform_when_job_transforms(proj):
    job = _job()
    job["transform"] = [{"filter": "amount > 0"}]
    _save_job(proj, job)
    runner.run(proj, "j", steps=("extract",), window=("2026-10-01", "2026-10-02"))
    rec = runner.run(proj, "j", steps=("load",))
    assert rec["status"] == "failed" and "has not been transformed" in rec["error"]
    assert runner.run(proj, "j", steps=("transform",))["status"] == "done"
    assert runner.run(proj, "j", steps=("load",))["status"] == "done"
    assert len(_rows(proj, "sq")) == 5


def _table_exists(proj, name):
    c = sqlite3.connect(proj.resolve("wh.db"))
    try:
        return c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None
    finally:
        c.close()


def test_watermark_moves_only_after_a_good_load(proj):
    job = {"name": "w", "source": {"connection": "src", "table": "tx",
                                   "incremental": {"column": "day", "watermark": True}},
           "target": {"connection": "sq", "table": "t", "mode": "append", "create": False}}
    _save_job(proj, job)
    rec = runner.run(proj, "w")
    assert rec["status"] == "failed" and "does not exist" in rec["error"]
    assert store.watermark(proj, "w") is None
    assert store.alerts(proj)[0]["run_id"] == rec["run_id"]
    job["target"]["create"] = True
    _save_job(proj, job)
    rec = runner.run(proj, "w")
    assert rec["status"] == "done", rec["error"]
    assert store.watermark(proj, "w") == "2026-10-10"
    rec = runner.run(proj, "w")
    assert rec["status"] == "done" and rec["rows"]["extracted"] == 0
    assert len(_rows(proj, "sq")) == 50


def test_cancel_and_one_run_per_job(proj, monkeypatch):
    from etlplus import sources
    job = _job()
    _save_job(proj, job)

    def slow(project, job, window, log):
        for i in range(200):
            time.sleep(0.02)
            yield pa.RecordBatch.from_pylist([{"id": i, "day": "2026-10-01", "amount": 1.0, "cust": "c"}])
    monkeypatch.setattr(sources, "read", slow)
    run_id = runner.start_background(proj, "j")
    with pytest.raises(RuntimeError, match="already running"):
        runner.start_background(proj, "j")
    time.sleep(0.2)
    assert run_id in runner.running(proj)
    assert runner.cancel(proj, run_id) is True
    runner.wait(run_id, 10)
    rec = store.get(proj, run_id)
    assert rec["status"] == "cancelled"
    assert rec["steps"]["extract"]["status"] == "cancelled"
    assert stage.latest(proj, "j") is None
    assert not Path(proj.resolve("wh.db")).exists() or not _table_exists(proj, "t")
    assert runner.cancel(proj, run_id) is False


def test_unknown_job_is_a_failed_record(proj):
    rec = runner.run(proj, "ghost")
    assert rec["status"] == "failed" and "no job named" in rec["error"]


# ---------------------------------------------------------------- store

def test_store_roundtrip(proj):
    store.record(proj, {"run_id": "r1", "job": "a", "status": "running", "started_at": "2026-11-01T01:00:00Z"})
    store.record(proj, {"run_id": "r1", "job": "a", "status": "failed", "started_at": "2026-11-01T01:00:00Z",
                        "error": "boom"})
    store.record(proj, {"run_id": "r1", "job": "a", "status": "failed", "started_at": "2026-11-01T01:00:00Z",
                        "error": "boom"})
    assert store.get(proj, "r1")["error"] == "boom"
    assert len(store.alerts(proj)) == 1
    ts = datetime(2026, 10, 31, 23, 59, 59)
    store.set_watermark(proj, "a", ts)
    assert store.watermark(proj, "a") == ts
    store.set_watermark(proj, "a", 42)
    assert store.watermark(proj, "a") == 42
    assert store.watermark(proj, "nope") is None
    store.record(proj, {"run_id": "r2", "job": "a", "status": "running", "started_at": "2026-11-01T02:00:00Z"})
    assert store.mark_interrupted(proj, set()) == 1
    assert store.get(proj, "r2")["status"] == "failed"


# ---------------------------------------------------------------- scheduler, notify

def test_scheduler_next_run_and_describe(proj):
    proj.settings["timezone"] = "Africa/Maputo"
    proj.jobs["j"] = dict(_job(), schedule="0 2 * * *")
    nxt = scheduler.next_run(proj, "j")
    assert nxt.endswith("T02:00:00+02:00")
    assert scheduler.next_run(proj, "missing") is None
    assert scheduler.describe("0 2 * * *") == "Every day at 02:00"
    assert scheduler.describe("30 6 * * 1-5") == "Every weekday at 06:30"
    assert scheduler.describe("*/15 * * * *") == "Every 15 minutes"
    s = scheduler.start(proj)
    try:
        assert [j["job"] for j in scheduler.scheduled()] == ["j"]
        proj.jobs.pop("j")
        scheduler.reload(proj)
        assert scheduler.scheduled() == []
    finally:
        scheduler.stop()


def test_notify_webhook_and_levels(proj):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        proj.settings["notify"] = {"webhook": f"http://127.0.0.1:{srv.server_port}/hook"}
        res = notify.test(proj)
        assert res["ok"] and res["channels"][0]["channel"] == "webhook"
        run = {"run_id": "r", "job": "j", "status": "failed", "error": "boom"}
        assert notify.notify_run(proj, {"notify": "never"}, run) is None
        assert notify.notify_run(proj, {"notify": "failure"}, run)[0]["ok"]
        assert notify.notify_run(proj, {"notify": "failure"}, dict(run, status="done")) is None
        assert notify.notify_run(proj, {"notify": "always"}, dict(run, status="done"))[0]["ok"]
        assert [g.get("event") for g in got] == ["test", "run.failed", "run.done"]
        assert got[1]["error"] == "boom"
    finally:
        srv.shutdown()
    proj.settings["notify"] = {}
    assert notify.test(proj)["ok"] is False


def test_notify_email_uses_smtp_secret(proj, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = (host, port)

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, pw):
            sent["login"] = (user, pw)

        def send_message(self, msg):
            sent["to"] = msg["To"]

        def quit(self):
            pass
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    secrets = pytest.importorskip("etlplus.secrets")
    secrets.set(proj, "smtp", {"password": "s3cret"})
    proj.settings["notify"] = {"email": {"host": "smtp.example.local", "port": 587, "user": "etl@example.local",
                                         "to": ["ops@example.local"]}}
    res = notify.test(proj)
    assert res["ok"], res
    assert sent == {"host": ("smtp.example.local", 587), "tls": True, "login": ("etl@example.local", "s3cret"),
                    "to": "ops@example.local"}


# ---------------------------------------------------------------- cli

def test_cli_run_jobs_runs(proj, capsys):
    from etlplus import cli
    _save_job(proj, _job(mode="append"))
    p = str(proj.path)
    assert cli.main(["-p", p, "run", "j", "--from", "2026-10-01", "--to", "2026-10-03"]) == 0
    assert cli.main(["-p", p, "run", "j", "--step", "load"]) == 0
    assert cli.main(["-p", p, "jobs"]) == 0
    assert cli.main(["-p", p, "runs"]) == 0
    assert cli.main(["-p", p, "connections", "list"]) == 0
    out = capsys.readouterr().out
    assert "j" in out and "done" in out and "src" in out
    assert len(_rows(proj, "sq")) == 10
    assert cli.main(["-p", p, "run", "ghost"]) == 1
    # --project after the command, and --steps as the README spells it
    assert cli.main(["run", "j", "--steps", "load", "--project", p]) == 0
    assert cli.build_parser().parse_args(["serve", "-p", p]).project == p
    assert len(_rows(proj, "sq")) == 10
