"""SPEC_V2 7: destinations, the DDL per dialect, create, preview and test."""
import sqlite3
from pathlib import Path

import duckdb
import pytest

from etlplus import config, ddl, destinations


@pytest.fixture
def proj(tmp_path):
    p = config.Project.init(tmp_path / "p")
    con = sqlite3.connect(p.path / "core.db")
    con.execute("CREATE TABLE customers (customer_number TEXT, segment TEXT, birth_year INTEGER, balance REAL)")
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)",
                    [(f"C{i:05d}", "mass" if i % 2 else None, 1970 + i, i * 1.5) for i in range(30)])
    con.commit()
    con.close()
    p.save_connection("core", {"kind": "sqlite", "path": "core.db", "role": "source"})
    p.save_connection("rep", {"kind": "duckdb", "path": "rep.duckdb", "role": "target"})
    p.save_connection("out", {"kind": "folder", "path": "out", "role": "target"})
    return config.Project.load(p.path)


COLS = [
    {"name": "line_id", "type": "string(40)", "nullable": False},
    {"name": "amount", "type": "decimal(18,3)", "nullable": False},
    {"name": "value_date", "type": "date"},
    {"name": "source_system", "type": "string(10)", "default": "'MT940'"},
    {"name": "loaded_at", "type": "timestamp", "default": "CURRENT_TIMESTAMP"},
]


def _dest(proj, did="lines", **kw):
    spec = {"connection": "rep", "table": "main.lines", "design": "create", "columns": COLS, "keys": ["line_id"]}
    spec.update(kw)
    assert destinations.save(proj, did, spec) == []
    return config.Project.load(proj.path)


# ---------------------------------------------------------------- save, list, delete

def test_save_list_get_delete_and_in_use(proj):
    p = _dest(proj)
    assert "lines" in p.destinations
    text = (Path(p.path) / "destinations.yaml").read_text(encoding="utf-8")
    assert "line_id" in text and "password" not in text
    lst = destinations.list(p)
    assert lst[0]["id"] == "lines" and lst[0]["kind"] == "duckdb" and lst[0]["columns_count"] == 5
    assert lst[0]["exists"] is False and lst[0]["jobs"] == []
    got = destinations.get(p, "lines")
    assert got["keys"] == ["line_id"] and got["columns"][0]["name"] == "line_id"
    p.save_job({"name": "feed", "source": {"connection": "core", "table": "customers"},
                "target": {"destination": "lines", "mode": "replace"}})
    p = config.Project.load(p.path)
    assert destinations.jobs_using(p, "lines") == ["feed"]
    with pytest.raises(destinations.InUse) as e:
        destinations.delete(p, "lines")
    assert e.value.jobs == ["feed"]
    p.delete_job("feed")
    destinations.delete(config.Project.load(p.path), "lines")
    assert "lines" not in config.Project.load(p.path).destinations


def test_save_problems(proj):
    assert destinations.save(proj, "bad id!", {"connection": "rep", "table": "t", "columns": COLS})
    probs = destinations.save(proj, "x", {"connection": "core", "table": "t", "columns": COLS})
    assert any("source (read only)" in p for p in probs)
    probs = destinations.save(proj, "x", {"connection": "nope", "table": "t", "columns": COLS})
    assert any("does not exist" in p for p in probs)
    probs = destinations.save(proj, "x", {"connection": "rep", "table": "t", "design": "create", "columns": []})
    assert "Add at least one column, or copy them from a source table." in probs
    assert not any("design create needs columns" in p for p in probs)
    probs = destinations.save(proj, "x", {"connection": "rep", "table": "t", "columns": COLS, "keys": ["nope"]})
    assert any("not a column" in p for p in probs)
    probs = destinations.save(proj, "x", {"connection": "rep", "table": "t",
                                          "columns": [{"name": "a", "type": "text", "default": "1); DROP TABLE t; --"}]})
    assert any("not allowed" in p for p in probs)
    probs = destinations.save(proj, "f", {"connection": "out", "path": "../up", "file": "x.{format}"})
    assert any("'..'" in p for p in probs)
    probs = destinations.save(proj, "f", {"connection": "out", "path": "C:/abs", "file": "x.{format}"})
    assert any("absolute" in p for p in probs)
    probs = destinations.save(proj, "f", {"connection": "out", "path": "hub", "file": "{date.__class__}.parquet"})
    assert probs
    assert destinations.save(proj, "f", {"connection": "out", "path": "hub", "file": "c_{date:%Y%m%d}.{format}",
                                         "format": "jsonl"}) == []


# ---------------------------------------------------------------- DDL per kind

def test_ddl_snapshots_per_kind():
    cols = [{"name": "id", "type": "bigint", "nullable": False}, {"name": "name", "type": "string(40)"},
            {"name": "amt", "type": "decimal(18,2)", "default": 0}, {"name": "ok", "type": "boolean", "default": True}]
    duck = ddl.create_table_sql("duckdb", "main.t", cols, ["id"])
    assert duck == ('CREATE TABLE IF NOT EXISTS "main"."t" (\n    "id" BIGINT NOT NULL,\n    "name" VARCHAR,\n'
                    '    "amt" DECIMAL(18,2) DEFAULT 0,\n    "ok" BOOLEAN DEFAULT TRUE,\n    PRIMARY KEY ("id")\n)')
    pg = ddl.create_table_sql("postgres", "public.t", cols, ["id"])
    assert '"name" varchar(40)' in pg and '"amt" numeric(18,2) DEFAULT 0' in pg and 'PRIMARY KEY ("id")' in pg
    ms = ddl.create_table_sql("sqlserver", "dbo.t", cols, ["id"])
    assert ms.startswith("CREATE TABLE [dbo].[t] (") and "[ok] bit DEFAULT 1" in ms and "[name] nvarchar(40)" in ms
    my = ddl.create_table_sql("mysql", "db.t", cols, ["id"])
    assert "`ok` tinyint(1) DEFAULT 1" in my and "IF NOT EXISTS `db`.`t`" in my
    ora = ddl.create_table_sql("oracle", "scott.t", cols, ["id"])
    assert ora.startswith('CREATE TABLE "SCOTT"."T" (') and '"NAME" varchar2(40 char)' in ora
    ch = ddl.create_table_sql("clickhouse", "db.t", cols, ["id"])
    assert ch.endswith("ENGINE = MergeTree ORDER BY (`id`)") and "`name` Nullable(String)" in ch
    assert ddl.create_table_sql("clickhouse", "t", cols[1:], []).endswith("ENGINE = MergeTree ORDER BY tuple()")
    td = ddl.create_table_sql("teradata", "db.t", cols, ["id"])
    assert td.endswith('UNIQUE PRIMARY INDEX ("id")') and "PRIMARY KEY" not in td
    lite = ddl.create_table_sql("sqlite", "t", cols, ["id"])
    assert 'PRIMARY KEY ("id")' in lite and '"amt" NUMERIC /* was: decimal(18,2) */ DEFAULT 0' in lite
    assert "now()" in ddl.create_table_sql("clickhouse", "t", [{"name": "at", "type": "timestamp",
                                                                "default": "current_timestamp"}], [])
    assert "SYSTIMESTAMP" in ddl.create_table_sql("oracle", "t", [{"name": "at", "type": "timestamp",
                                                                   "default": "CURRENT_TIMESTAMP"}], [])


def test_ddl_name_limits_and_hostile_values():
    long_ora = "c" * 129
    with pytest.raises(Exception, match="longer than 128"):
        ddl.create_table_sql("oracle", "t", [{"name": long_ora, "type": "text"}], [])
    warn = ddl.check("oracle", [{"name": "c" * 31, "type": "text"}], [])
    assert any("30 characters" in w["message"] for w in warn)
    with pytest.raises(Exception, match="longer than 63"):
        ddl.create_table_sql("postgres", "t", [{"name": "x" * 64, "type": "text"}], [])
    with pytest.raises(Exception):
        ddl.create_table_sql("duckdb", "t", [{"name": "a\x00b", "type": "text"}], [])
    sql = ddl.create_table_sql("duckdb", "t", [{"name": 'a"b', "type": "text", "default": "'it''s'"}], [])
    assert '"a""b" VARCHAR DEFAULT \'it\'\'s\'' in sql
    my = ddl.create_table_sql("mysql", "t", [{"name": "a", "type": "text", "default": "'back\\slash'"}], [])
    assert "'back\\\\slash'" in my
    for bad in ("1); DROP TABLE t; --", "now() || 'x'", "'a' 'b'", "(SELECT 1)"):
        with pytest.raises(Exception):
            ddl.default_literal("duckdb", bad)
    lossy = ddl.check("mysql", [{"name": "at", "type": "timestamptz"}], [])
    assert lossy and lossy[0]["level"] == "warn"
    assert ddl.create_table_sql("mysql", "t", [{"name": "at", "type": "timestamptz"}], []).count("/* was: timestamptz */") == 1


def test_types_table():
    rows = ddl.types_table("sqlserver")
    assert {"generic": "string(40)", "native": "nvarchar(40)"} in rows
    assert len(rows) == len(ddl.GENERIC_TYPES)


# ---------------------------------------------------------------- create, test, preview on duckdb

def test_create_runs_only_when_missing_then_test_and_preview(proj):
    p = _dest(proj)
    t0 = destinations.test(p, "lines")
    assert t0["ok"] and any("does not exist yet" in c["message"] for c in t0["checks"])
    info = destinations.ddl_info(p, "lines")
    assert info["exists"] is False and info["sql"].startswith("CREATE TABLE")
    res = destinations.create(p, "lines")
    assert res["created"] is True
    assert destinations.create(p, "lines")["created"] is False
    assert destinations.exists(p, "lines") is True
    t1 = destinations.test(p, "lines")
    assert t1["ok"] and any("exists" in c["message"] for c in t1["checks"])
    con = duckdb.connect(str(Path(p.path) / "rep.duckdb"))
    con.execute("INSERT INTO lines (line_id, amount, value_date) VALUES ('a', 1.5, DATE '2026-10-01'), "
                "('b', 2.5, DATE '2026-10-02'), ('c', 3.5, NULL)")
    con.close()
    pv = destinations.preview(p, "lines", limit=2, offset=1)
    assert pv["total"] == 3 and len(pv["rows"]) == 2 and pv["columns"][0] == "line_id"
    row = destinations.preview(p, "lines", limit=1)["rows"][0]
    assert row[3] == "MT940" and row[4] is not None    # the database defaults filled them
    cols = destinations.columns(p, "lines")
    assert [c["name"] for c in cols][:2] == ["line_id", "amount"]


def test_preview_while_a_load_holds_the_file(proj):
    from etlplus import targets
    p = _dest(proj)
    destinations.create(p, "lines")
    with targets.file_lock(p, p.connections["rep"]):
        with pytest.raises(destinations.Busy, match="try again"):
            destinations.preview(p, "lines")
    holder = duckdb.connect(str(Path(p.path) / "rep.duckdb"))    # a writer in this process
    try:
        with pytest.raises(destinations.Busy):
            destinations.preview(p, "lines")
    finally:
        holder.close()
    assert destinations.preview(p, "lines")["total"] == 0


def test_existing_design_reads_live_columns(proj):
    con = duckdb.connect(str(proj.path / "rep.duckdb"))
    con.execute('CREATE TABLE ready (k INTEGER PRIMARY KEY, label VARCHAR, "at" TIMESTAMP)')
    con.close()
    assert destinations.save(proj, "ready", {"connection": "rep", "table": "ready", "design": "existing"}) == []
    p = config.Project.load(proj.path)
    cols = destinations.columns(p, "ready")
    assert [(c["name"], c["type"]) for c in cols] == [("k", "integer"), ("label", "text"), ("at", "timestamp")]
    assert destinations.test(p, "ready")["ok"]
    assert destinations.save(p, "gone", {"connection": "rep", "table": "nope", "design": "existing"}) == []
    p = config.Project.load(proj.path)
    assert destinations.test(p, "gone")["ok"] is False


def test_file_destination_resolve_and_preview(proj):
    assert destinations.save(proj, "hubc", {"connection": "out", "path": "hub", "file": "customers.{format}",
                                            "format": "parquet"}) == []
    p = config.Project.load(proj.path)
    t = destinations.resolve(p, "hubc", job_name="j")
    assert t["connection"] == "out" and t["table"] == "hub/customers.parquet" and t["format"] == "parquet"
    assert destinations.preview(p, "hubc")["rows"] == []
    out = Path(p.path) / "out" / "hub"
    out.mkdir(parents=True)
    duckdb.sql("SELECT 1 AS a, 'x' AS b").write_parquet(str(out / "customers.parquet"))
    pv = destinations.preview(p, "hubc")
    assert pv["columns"] == ["a", "b"] and pv["rows"] == [[1, "x"]]
    assert destinations.ddl_info(p, "hubc")["sql"] is None


def test_design_from_source_sqlite(proj):
    cols = destinations.design_from_source(proj, {"connection": "core", "table": "customers"}, "postgres")
    by = {c["name"]: c for c in cols}
    assert set(by) == {"customer_number", "segment", "birth_year", "balance"}
    assert by["customer_number"]["type"].startswith("string(")
    assert by["birth_year"]["type"] in ("integer", "bigint") and by["balance"]["type"] == "double"


def test_fill_tokens_never_uses_format():
    import datetime as dt
    d = dt.date(2026, 11, 1)
    assert destinations.fill_tokens("x_{date:%Y%m%d}_{job}.{format}", job="j", fmt="csv", today=d) == "x_20261101_j.csv"
    for bad in ("{date.__class__}", "{0}", "{job:%Y}", "{date:%Y{x}}"):
        with pytest.raises(destinations.DestinationError):
            destinations.fill_tokens(bad, job="j", fmt="csv", today=d)


def test_target_connect_goes_through_drivers_with_the_saved_secret(proj, monkeypatch):
    from etlplus import drivers, secrets, targets
    proj.save_connection("ch", {"kind": "clickhouse", "host": "ch.example", "user": "u", "role": "target"})
    secrets.set(proj, "ch", {"password": "pw-for-ch"})
    p = config.Project.load(proj.path)
    seen = {}

    def fake(project, cid, *, role, override=None, spec=None):
        seen.update(role=role, password=(spec or {}).get("_password"))
        return object()
    monkeypatch.setattr(drivers, "connect", fake)
    targets.connect(p, "ch", p.connections["ch"])
    assert seen == {"role": "target", "password": "pw-for-ch"}
    moved = dict(p.connections["ch"], host="other.example")
    targets.connect(p, "ch", moved)
    assert seen["password"] is None          # a saved secret never follows to another server


# ---------------------------------------------------------------- copy the design from a source

def _pk_source(proj):
    con = sqlite3.connect(Path(proj.path) / "core.db")
    con.execute("CREATE TABLE accounts (account_no VARCHAR(20) PRIMARY KEY, branch TEXT, opened_on DATE, "
                "balance DECIMAL(18,2), " + "x" * 70 + " INTEGER)")
    con.execute("INSERT INTO accounts VALUES ('A0001', 'kigali', '2026-01-02', 12.5, 3)")
    con.commit()
    con.close()


def test_design_detail_from_a_table_carries_the_key_and_types(proj):
    _pk_source(proj)
    r = destinations.design_detail(proj, {"connection": "core", "table": "accounts"}, "postgres")
    by = {c["name"]: c for c in r["columns"]}
    assert r["keys"] == ["account_no"] and by["account_no"]["key"] is True and by["account_no"]["nullable"] is False
    assert by["account_no"]["type"] == "string(20)" and by["account_no"]["source_type"] == "VARCHAR(20)"
    assert by["balance"]["type"] == "decimal(18,2)" and by["balance"]["native"] == "numeric(18,2)"
    assert r["table"] == "accounts" and r["id"] == "accounts" and r["kind"] == "postgres" and r["name_limit"] == 63
    long = [c for c in r["checks"] if c["column"] == "x" * 70]
    assert long and long[0]["level"] == "error" and "longer than 63" in long[0]["message"]
    # the copied design saves and creates the table in another kind, key and all
    cols = [{k: c[k] for k in ("name", "type", "nullable")} for c in r["columns"] if len(c["name"]) < 60]
    sql = ddl.create_table_sql("postgres", "public.accounts", cols, r["keys"])
    assert '"account_no" varchar(20) NOT NULL' in sql and '"balance" numeric(18,2)' in sql
    assert 'PRIMARY KEY ("account_no")' in sql
    ms = ddl.create_table_sql("sqlserver", "dbo.accounts", cols, r["keys"])
    assert "[account_no] nvarchar(20) NOT NULL" in ms or "[account_no] varchar(20) NOT NULL" in ms
    assert "PRIMARY KEY ([account_no])" in ms
    assert destinations.save(proj, r["id"], {"connection": "rep", "table": r["table"], "design": "create",
                                             "columns": cols, "keys": r["keys"]}) == []
    p = config.Project.load(proj.path)
    assert destinations.create(p, "accounts")["created"] is True
    assert destinations.design_detail(p, {"connection": "core", "table": "accounts"}, "duckdb")["id"] == "accounts_2"


def test_design_detail_duckdb_composite_key_into_sqlserver(proj):
    con = duckdb.connect(str(Path(proj.path) / "wh.duckdb"))
    con.execute("CREATE TABLE lines (stmt_id INTEGER, line_no INTEGER, memo VARCHAR, PRIMARY KEY (stmt_id, line_no))")
    con.execute("INSERT INTO lines VALUES (1, 1, 'rent')")
    con.close()
    proj.save_connection("wh", {"kind": "duckdb", "path": "wh.duckdb", "role": "source"})
    p = config.Project.load(proj.path)
    r = destinations.design_detail(p, {"connection": "wh", "table": "lines"}, "sqlserver")
    assert r["keys"] == ["stmt_id", "line_no"]
    assert [c["native"] for c in r["columns"]][:2] == ["int", "int"]
    sql = ddl.create_table_sql("sqlserver", "dbo.lines", [{"name": c["name"], "type": c["type"]} for c in r["columns"]], r["keys"])
    assert "PRIMARY KEY ([stmt_id], [line_no])" in sql


def test_design_detail_from_files_keeps_personal_flags(proj):
    import shutil
    land = Path(proj.path) / "land"
    land.mkdir()
    shutil.copy(Path(__file__).parent / "fixtures" / "bank" / "mt940_bare_yearturn.txt", land / "Statement 2026-11.txt")
    proj.save_connection("land", {"kind": "folder", "path": "land", "role": "source"})
    p = config.Project.load(proj.path)
    r = destinations.design_detail(p, {"connection": "land", "files": "Statement 2026-11.txt", "format": "mt940"}, "duckdb")
    by = {c["name"]: c for c in r["columns"]}
    assert by["account"]["personal"] is True and not by["amount"]["personal"]
    assert r["keys"] == [] and r["table"] == "statement_2026_11"
    # into a file destination the checks run as duckdb and no native type is given
    f = destinations.design_detail(p, {"connection": "land", "files": "Statement 2026-11.txt", "format": "mt940"}, "folder")
    assert all(c["native"] is None for c in f["columns"])


def test_suggest_table_name_fits_the_kind():
    assert destinations.suggest_table_name("postgres", {"table": "main.Customer Accounts"}) == "customer_accounts"
    assert destinations.suggest_table_name("postgres", {"table": "2026_loans"}) == "t_2026_loans"
    assert len(destinations.suggest_table_name("postgres", {"table": "a" * 100})) == 63
    assert len(destinations.suggest_table_name("sqlserver", {"table": "a" * 200})) == 128
    assert destinations.suggest_table_name("duckdb", {"files": "in/*.csv"}) == "table"


def test_design_detail_after_a_transform_has_the_names_the_mapping_sees(proj):
    steps = [{"rename": {"segment": "customer_segment"}}, {"drop": ["balance"]}]
    r = destinations.design_detail(proj, {"connection": "core", "table": "customers"}, "postgres", steps)
    assert [c["name"] for c in r["columns"]] == ["customer_number", "customer_segment", "birth_year"]
