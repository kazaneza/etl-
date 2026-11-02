"""SPEC_V2 8: column mapping (validate, suggest, the map step, load of mapped columns, ledger)."""
import sqlite3
from pathlib import Path

import duckdb
import pytest

from etlplus import config, destinations, mapping, runner, sources, stage, store, transform

DEST_COLS = [
    {"name": "cust_id", "type": "string(8)", "nullable": False},
    {"name": "seg", "type": "string(10)"},
    {"name": "amount", "type": "decimal(18,2)"},
    {"name": "src", "type": "string(10)", "default": "'CORE'"},
    {"name": "loaded_at", "type": "timestamp", "default": "CURRENT_TIMESTAMP"},
]


@pytest.fixture
def proj(tmp_path):
    p = config.Project.init(tmp_path / "p")
    con = sqlite3.connect(p.path / "core.db")
    con.execute("CREATE TABLE c (customer_number TEXT, segment TEXT, amount TEXT, day TEXT)")
    con.executemany("INSERT INTO c VALUES (?, ?, ?, ?)",
                    [(f"C{i:03d}", "mass" if i % 2 else "sme", f"{i}.25", f"2026-10-{1 + i % 5:02d}")
                     for i in range(10)])
    con.commit()
    con.close()
    p.save_connection("core", {"kind": "sqlite", "path": "core.db", "role": "source"})
    p.save_connection("rep", {"kind": "duckdb", "path": "rep.duckdb", "role": "target"})
    p.save_connection("land", {"kind": "folder", "path": "land", "role": "source"})
    (Path(p.path) / "land").mkdir()
    assert destinations.save(p, "custs", {"connection": "rep", "table": "custs", "design": "create",
                                          "columns": DEST_COLS, "keys": ["cust_id"]}) == []
    return config.Project.load(p.path)


def _job(**mapping_extra):
    m = {"columns": {"cust_id": {"from": "CUSTOMER_NUMBER"}, "seg": {"from": "segment"},
                     "amount": {"from": "amount"}},
         "unmapped": "default", "on_cast_error": "error"}
    m.update(mapping_extra)
    return {"name": "m", "source": {"connection": "core", "table": "c"},
            "target": {"destination": "custs", "mode": "upsert"}, "mapping": m}


def _save(proj, job):
    proj.save_job(job)
    return config.Project.load(proj.path)


def _rows(proj, sql):
    con = duckdb.connect(str(Path(proj.path) / "rep.duckdb"), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- check_expr and literal

@pytest.mark.parametrize("expr", [
    "(SELECT 1)", "x IN (SELECT a FROM t)", "read_parquet('/etc/passwd')", "getenv('HOME')",
    "current_setting('allowed_paths')", "glob('*')", "a; DROP TABLE t", "'" + "a" * 2001 + "'",
    "duckdb_settings()", "sniff_csv('x')", "1 UNION SELECT 2", "parquet_scan('x')",
])
def test_check_expr_refuses(expr):
    with pytest.raises(ValueError):
        transform.check_expr(expr)


def test_check_expr_with_columns():
    cols = [{"name": "amount", "type": "DOUBLE"}, {"name": "name", "type": "VARCHAR"}]
    transform.check_expr("amount * 2 + length(name)", cols)
    transform.check_expr("'select' || name", cols)        # a keyword inside a string is fine
    with pytest.raises(ValueError, match="nope"):
        transform.check_expr("nope + 1", cols)


def test_literal():
    assert mapping.literal(None) == "NULL"
    assert mapping.literal(True) == "TRUE" and mapping.literal(7) == "7" and mapping.literal(1.5) == "1.5"
    assert mapping.literal("it's", "string(10)") == "CAST('it''s' AS VARCHAR)"
    back = mapping.literal("a\\b")
    assert duckdb.sql("SELECT " + back).fetchall()[0][0] == "a\\b"
    for bad in (float("nan"), float("inf"), [1], {"a": 1}, "x" * 4001):
        with pytest.raises(mapping.MappingError):
            mapping.literal(bad)


# ---------------------------------------------------------------- validate and suggest

def test_validate_errors():
    src = [{"name": "customer_number", "type": "VARCHAR"}, {"name": "amount", "type": "VARCHAR"}]
    job = {"mapping": {"columns": {"seg": {"from": "ghost"}, "amount": {"expr": "amount +* 2"},
                                   "nosuch": {"const": 1}}}}
    probs = mapping.validate(job, src, DEST_COLS)
    by = {(p["column"], p["level"]): p["message"] for p in probs}
    assert by[("cust_id", "error")] == "Required: map a column, a constant or an expression."
    assert "no source column 'ghost'" in by[("seg", "error")]
    assert ("amount", "error") in by
    assert by[("nosuch", "error")] == "The destination has no such column."
    strict = mapping.validate({"mapping": {"columns": {"cust_id": {"from": "customer_number"}},
                                           "unmapped": "error"}}, src, DEST_COLS)
    assert {p["column"] for p in strict if p["level"] == "error"} == {"seg", "amount", "src", "loaded_at"}
    shape = mapping.check_config({"columns": {"a": {"from": "x", "const": 1}}, "on_cast_error": "maybe"})
    assert any("exactly one of" in p for p in shape) and any("on_cast_error" in p for p in shape)


def test_validate_warnings_table():
    dest = [{"name": "n", "type": "integer"}, {"name": "d", "type": "date"}, {"name": "s", "type": "string(3)"},
            {"name": "k", "type": "string(5)", "nullable": False}, {"name": "t", "type": "text"},
            {"name": "ts", "type": "timestamp"}, {"name": "txt", "type": "decimal(10,2)"}]
    src = [{"name": "dbl", "type": "DOUBLE"}, {"name": "tsc", "type": "TIMESTAMP"},
           {"name": "long", "type": "VARCHAR", "samples": ["abcdef"]},
           {"name": "maybe", "type": "VARCHAR", "nulls": 2}, {"name": "i", "type": "INTEGER"},
           {"name": "dt", "type": "DATE"}, {"name": "text", "type": "VARCHAR"}]
    job = {"mapping": {"columns": {"n": {"from": "dbl"}, "d": {"from": "tsc"}, "s": {"from": "long"},
                                   "k": {"from": "maybe"}, "t": {"from": "i"}, "ts": {"from": "dt"},
                                   "txt": {"from": "text"}}}}
    probs = {p["column"]: p["message"] for p in mapping.validate(job, src, dest)}
    assert probs["n"] == "Decimals will be cut."
    assert probs["d"] == "The time part is dropped."
    assert probs["s"] == "Some sample values are longer than 3."
    assert probs["k"] == "Sample has empty values; this column is required."
    assert probs["txt"] == "Text into a number or date: rows that do not convert will fail."
    assert "t" not in probs and "ts" not in probs


def test_suggest_exact_alias_similar_no_double_use():
    out = mapping.suggest(["Account_No", "AMT", "ccy", "Value Date", "customer_refs", "amount"],
                          ["account", "amount", "currency", "value_date", "customer_ref", "amt2"])
    assert out["value_date"] == {"from": "Value Date", "confidence": 1.0, "how": "exact"}
    assert out["amount"]["from"] == "amount" and out["amount"]["how"] == "exact"
    assert out["account"]["how"] == "alias" and out["account"]["from"] == "Account_No"
    assert out["currency"] == {"from": "ccy", "confidence": 0.9, "how": "alias"}
    assert out["customer_ref"]["how"] == "similar" and out["customer_ref"]["from"] == "customer_refs"
    used = [v["from"] for v in out.values()]
    assert len(used) == len(set(used))


# ---------------------------------------------------------------- the map step and the load

def test_run_maps_through_partial_and_loads_only_mapped_columns(proj, monkeypatch):
    p = _save(proj, _job())
    seen = []
    real_replace = mapping.os.replace

    def spy(a, b):
        seen.append((Path(a).name, Path(b).name))
        return real_replace(a, b)
    monkeypatch.setattr(mapping.os, "replace", spy)
    rec = runner.run(p, "m")
    assert rec["status"] == "done", rec["error"]
    assert set(rec["steps"]) == {"extract", "map", "load"}
    assert rec["rows"]["mapped"] == 10 and rec["rows"]["loaded"] == 10
    assert ("mapped.parquet.partial", "mapped.parquet") in seen
    assert ("mapped.manifest.json.partial", "mapped.manifest.json") in seen
    run_dir = stage.run_dir(p, "m", rec["stage_run_id"])
    assert (run_dir / "mapped.parquet").exists() and not list(run_dir.glob("*.partial"))
    m = stage.read_manifest(p, "m", rec["stage_run_id"], "mapped")
    assert m["mapped"] is True and [c["name"] for c in m["columns"]] == ["cust_id", "seg", "amount"]
    rows = _rows(p, "SELECT cust_id, seg, amount, src, loaded_at IS NOT NULL FROM custs ORDER BY cust_id")
    assert rows[0][0] == "C000" and str(rows[1][2]) == "1.25" and rows[0][3] == "CORE" and rows[0][4] is True
    # latest_loadable prefers the mapped stage
    assert runner.latest_loadable(p, p.jobs["m"])["kind"] == "mapped"
    again = runner.run(p, "m", steps=("load",))
    assert again["status"] == "done" and again["rows"]["loaded"] == 10
    assert _rows(p, "SELECT count(*) FROM custs")[0][0] == 10


def test_constants_expressions_and_try_cast(proj):
    job = _job(on_cast_error="null")
    job["mapping"]["columns"]["amount"] = {"expr": "CASE WHEN customer_number = 'C003' THEN 'bad' ELSE amount END"}
    job["mapping"]["columns"]["src"] = {"const": "LIVE"}
    p = _save(proj, job)
    rec = runner.run(p, "m")
    assert rec["status"] == "done", rec["error"]
    rows = dict(_rows(p, "SELECT cust_id, amount FROM custs"))
    assert rows["C003"] is None and str(rows["C002"]) == "2.25"
    assert {r[0] for r in _rows(p, "SELECT DISTINCT src FROM custs")} == {"LIVE"}
    strict = _job()
    strict["mapping"]["columns"]["amount"] = {"expr": "CASE WHEN customer_number = 'C003' THEN 'bad' ELSE amount END"}
    p = _save(p, strict)
    rec = runner.run(p, "m")
    assert rec["status"] == "failed" and "Mapping failed" in rec["error"]
    assert rec["steps"]["map"]["status"] == "failed"


def test_job_without_mapping_keeps_v1_steps(proj):
    p = _save(proj, {"name": "plain", "source": {"connection": "core", "table": "c"},
                     "target": {"destination": "custs", "mode": "replace"},
                     "transform": [{"rename": {"customer_number": "cust_id", "segment": "seg"}},
                                   {"drop": ["day", "amount"]}]})
    rec = runner.run(p, "plain")
    assert rec["status"] == "done", rec["error"]
    assert set(rec["steps"]) == {"extract", "transform", "load"}


def test_preview_live_sample_and_staged(proj):
    job = _job()
    p = config.Project.load(proj.path)
    pv = mapping.preview(p, job)
    assert pv["based_on"] == "live-sample" and len(pv["rows"]) == 10
    assert [c["name"] for c in pv["destination_columns"]][:2] == ["cust_id", "seg"]
    assert pv["destination_columns"][0]["key"] is True
    src = {c["name"]: c for c in pv["source_columns"]}
    assert src["customer_number"]["samples"][:1] == ["C000"]
    assert not list((Path(p.path) / "stage").rglob("*.parquet"))   # nothing left behind
    p = _save(p, job)
    runner.run(p, "m", steps=("extract",))
    pv2 = mapping.preview(config.Project.load(p.path), job)
    assert pv2["based_on"].startswith("staged:")
    bad = _job()
    bad["mapping"]["columns"]["seg"] = {"from": "ghost"}
    pv3 = mapping.preview(config.Project.load(p.path), bad)
    assert any(x["level"] == "error" for x in pv3["problems"]) and pv3["rows"] == []


def test_validate_job_mapping_and_destination(proj):
    bad = _job()
    bad["mapping"]["columns"]["seg"] = {"from": "x", "expr": "1"}
    bad["target"] = {"destination": "nope"}
    probs = runner.validate_job(proj, bad)
    assert any("destination 'nope' does not exist" in p for p in probs)
    assert any("exactly one of" in p for p in probs)
    ok = runner.validate_job(proj, _job())
    assert ok == []


# ---------------------------------------------------------------- the file ledger

def _fake_remote_reader(monkeypatch, files):
    real = sources.read

    def read(project, job, window, log, fetch_dir=None, files_out=None):
        if files_out is not None:
            files_out.extend(files)
        yield from real(project, job, window, log)
    monkeypatch.setattr(sources, "read", read)


def test_ledger_marked_only_after_a_good_load(proj, monkeypatch):
    land = Path(proj.path) / "land"
    (land / "a.csv").write_text("customer_number,segment,amount\nC1,mass,1.5\nC2,sme,2.5\n", encoding="utf-8")
    files = [{"remote": "a.csv", "local": "a.csv", "size": 40, "modified": "2026-11-01T00:00:00Z", "sha256": "x"}]
    _fake_remote_reader(monkeypatch, files)
    job = _job()
    job["name"] = "f"
    job["source"] = {"connection": "land", "files": "a.csv", "format": "csv"}
    p = _save(proj, job)
    rec = runner.run(p, "f")
    assert rec["status"] == "done", rec["error"]
    rows, total = store.files(p, "f")
    assert total == 1 and rows[0]["path"] == "a.csv" and rows[0]["run_id"] == rec["run_id"]
    assert store.file_seen(p, "f", "land", "a.csv", 40, "2026-11-01T00:00:00Z")
    assert not store.file_seen(p, "f", "land", "a.csv", 41, "2026-11-01T00:00:00Z")
    ext = stage.read_manifest(p, "f", rec["stage_run_id"], "extract")
    assert ext["files"][0]["remote"] == "a.csv"


def test_failed_load_leaves_the_ledger_unchanged(proj, monkeypatch):
    land = Path(proj.path) / "land"
    (land / "b.csv").write_text("customer_number,segment,amount\nC1,mass,1.5\nC1,sme,2.5\n", encoding="utf-8")
    files = [{"remote": "b.csv", "local": "b.csv", "size": 40, "modified": "2026-11-01T00:00:00Z", "sha256": "y"}]
    _fake_remote_reader(monkeypatch, files)
    job = _job()
    job["name"] = "g"
    job["source"] = {"connection": "land", "files": "b.csv", "format": "csv"}
    p = _save(proj, job)
    rec = runner.run(p, "g")
    assert rec["status"] == "failed" and "more than once" in rec["error"]
    assert store.files(p, "g") == ([], 0)
