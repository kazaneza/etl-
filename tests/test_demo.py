import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import duckdb
import pytest
import yaml

from etlplus import demo

TODAY = date(2026, 11, 1)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    return demo.generate(tmp_path_factory.mktemp("d") / "demo", today=TODAY)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump_sqlite(path: Path) -> dict:
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    finally:
        con.close()


def test_layout(project):
    for p in ["etlplus.yaml", "connections.yaml", "core.db", "landing/customers.csv", "landing/payments.txt",
              "landing/branches.txt", "landing/events.jsonl", "jobs"]:
        assert (project / p).exists(), p
    assert (project / "hub").is_dir()


def test_deterministic(project, tmp_path):
    again = demo.generate(tmp_path / "again", today=TODAY)
    for name in ["customers.csv", "payments.txt", "branches.txt", "events.jsonl"]:
        assert _digest(project / "landing" / name) == _digest(again / "landing" / name), name
    assert _dump_sqlite(project / "core.db") == _dump_sqlite(again / "core.db")
    for job in (project / "jobs").glob("*.yaml"):
        assert job.read_text() == (again / "jobs" / job.name).read_text()
    other = demo.generate(tmp_path / "other", today=TODAY, seed=8)
    assert _digest(project / "landing" / "payments.txt") != _digest(other / "landing" / "payments.txt")


def test_regenerate_in_place(project):
    before = _dump_sqlite(project / "core.db")
    demo.generate(project, today=TODAY)
    assert _dump_sqlite(project / "core.db") == before


def test_refuses_foreign_folder(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(FileExistsError):
        demo.generate(tmp_path, today=TODAY)


def test_core_db(project):
    data = _dump_sqlite(project / "core.db")
    assert len(data["customers"]) == 400
    assert len(data["accounts"]) >= 400
    days = {r[3] for r in data["transactions"]}
    assert len(days) == demo.DAYS
    assert max(days) < TODAY.isoformat()
    con = sqlite3.connect(project / "core.db")
    cols = [r[1] for r in con.execute("PRAGMA table_info(customers)")]
    con.close()
    assert not any("name" in c for c in cols)


def test_files_readable(project):
    land = project / "landing"
    con = duckdb.connect()
    n_csv = con.execute("SELECT count(*), count(DISTINCT customer_number) FROM read_csv(?, header = true)",
                        [str(land / "customers.csv")]).fetchone()
    assert n_csv[0] > 100 and n_csv[0] == n_csv[1]
    note = con.execute("SELECT count(*) FROM read_csv(?, header = true) WHERE note LIKE '%,%'",
                       [str(land / "customers.csv")]).fetchone()[0]
    assert note > 0
    pay = con.execute("SELECT count(*), count(DISTINCT payment_id) FROM read_csv(?, delim = '|', header = true)",
                      [str(land / "payments.txt")]).fetchone()
    assert pay == (1500, 1500)

    lines = (land / "branches.txt").read_text().splitlines()
    assert len(lines) == len(demo.BRANCHES)
    assert all(len(line) == sum(demo.FIXED_WIDTHS) for line in lines)
    first = lines[0]
    assert first[:6].strip() == "BR001" and first[30:46].strip() == "Central"
    date.fromisoformat(first[46:56])

    events = [json.loads(line) for line in (land / "events.jsonl").read_text().splitlines()]
    assert len(events) == 2060
    assert len({e["event_id"] for e in events}) == 2000


def test_excel(project):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(project / "landing" / "rates.xlsx", read_only=True)
    rows = list(wb["rates"].iter_rows(values_only=True))
    assert rows[1] == ("rate_date", "currency", "mid_rate")
    assert len(rows) == 2 + 30 * 3


def _jobs(project):
    return {p.stem: yaml.safe_load(p.read_text()) for p in (project / "jobs").glob("*.yaml")}


def _target(project, job):
    """The job's target as {connection, mode, key, format}: a destination is read from
    destinations.yaml (SPEC_V2 2.2: demo jobs now use destinations)."""
    t = dict(job["target"])
    did = t.get("destination")
    if did:
        dests = yaml.safe_load((project / "destinations.yaml").read_text())["destinations"]
        d = dests[did]
        t.setdefault("connection", d["connection"])
        t.setdefault("key", d.get("keys"))
        t.setdefault("format", d.get("format"))
        t.setdefault("table", d.get("table") or d.get("file"))
    t.setdefault("mode", "replace")
    return t


def test_jobs_valid_yaml(project):
    conns = yaml.safe_load((project / "connections.yaml").read_text())["connections"]
    assert {c["kind"] for c in conns.values()} == {"sqlite", "folder", "duckdb"}
    for c in conns.values():
        assert "password" not in c
    jobs = _jobs(project)
    assert len(jobs) >= 9
    for stem, job in jobs.items():
        assert job["name"] == stem
        target = _target(project, job)
        assert job["source"]["connection"] in conns
        assert target["connection"] in conns
        assert conns[target["connection"]]["role"] in ("target", "both")
        assert target["mode"] in ("replace", "append", "upsert")
        if target["mode"] == "upsert":
            assert target["key"]
        assert sum(k in job["source"] for k in ("table", "query", "files")) == 1


def test_jobs_cover_everything(project):
    jobs = list(_jobs(project).values())
    modes = {_target(project, j)["mode"] for j in jobs}
    assert modes == {"replace", "append", "upsert"}
    formats = {j["source"].get("format") for j in jobs if "files" in j["source"]}
    assert {"csv", "txt", "fixed", "jsonl"} <= formats
    steps = {next(iter(s)) for j in jobs for s in j.get("transform", [])}
    assert steps == {"rename", "cast", "filter", "derive", "drop", "dedupe", "sql"}
    assert any(j["source"].get("incremental", {}).get("lookback_days") for j in jobs)
    assert any(j["source"].get("incremental", {}).get("watermark") for j in jobs)
    hub = [j for j in jobs if _target(project, j)["connection"] == "hub"]
    assert hub and all(_target(project, j)["format"] == "parquet" for j in hub)


def test_jobs_pass_runner_validation(project):
    config = pytest.importorskip("etlplus.config")
    runner = pytest.importorskip("etlplus.runner")
    p = config.Project.load(project)
    for name, job in p.jobs.items():
        assert runner.validate_job(p, job) == [], name


def test_transform_steps_validate(project):
    transform = pytest.importorskip("etlplus.transform")
    for job in _jobs(project).values():
        transform.validate(job.get("transform", []))


def test_quickstart_end_to_end(tmp_path):
    pytest.importorskip("etlplus.config")
    pytest.importorskip("etlplus.runner")
    pytest.importorskip("etlplus.sources")
    pytest.importorskip("etlplus.targets")
    lines = []
    results = demo.quickstart(tmp_path / "qs", today=TODAY, out=lines.append)
    assert results and all(r["status"] == "done" for r in results.values()), {
        n: r.get("error") for n, r in results.items() if r["status"] != "done"}
    assert any("http://127.0.0.1:8300/app/" in line for line in lines)
    assert (tmp_path / "qs" / "hub" / "daily_txn.parquet").exists() or any((tmp_path / "qs" / "hub").glob("daily_txn*"))
    con = duckdb.connect(str(tmp_path / "qs" / "warehouse.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 400
        assert con.execute("SELECT count(*) FROM app_events").fetchone()[0] == 2000
    finally:
        con.close()


def test_examples_are_valid_yaml():
    files = list((ROOT / "examples").rglob("*.yaml"))
    assert len(files) >= 10
    for f in files:
        data = yaml.safe_load(f.read_text())
        assert isinstance(data, dict), f
        if f.parent.name == "jobs":
            assert data["name"] == f.stem
            assert data["target"]["mode"] in ("replace", "append", "upsert")
        if f.name == "connections.yaml":
            for c in data["connections"].values():
                assert "password" not in c


def test_no_em_dashes():
    for f in [ROOT / "etlplus" / "demo.py", ROOT / "README.md", *(ROOT / "examples").rglob("*.*")]:
        if f.exists() and f.suffix in (".py", ".md", ".yaml", ".example", ".txt"):
            assert "—" not in f.read_text(encoding="utf-8"), f


# ---------------------------------------------------------------- v2 (SPEC_V2 12)

def test_demo_v2_destinations_and_kind(project):
    settings = yaml.safe_load((project / "etlplus.yaml").read_text())
    assert settings["kind"] == "demo"
    dests = yaml.safe_load((project / "destinations.yaml").read_text())["destinations"]
    assert set(dests) == {"reporting_statement_lines", "reporting_card_transactions", "reporting_customers",
                          "reporting_payments"}
    assert all(d["connection"] == "reporting" and d["design"] == "create" for d in dests.values())
    assert (project / "reporting.duckdb").exists()
    jobs = _jobs(project)
    assert "hub_customers" not in jobs
    hub = [n for n, j in jobs.items() if _target(project, j)["connection"] == "hub"]
    assert hub == ["hub_daily_txn"]
    assert jobs["customers_to_reporting"]["mapping"]["columns"]["source_system"] == {"const": "CORE"}
    assert set(demo.JOB_ORDER) >= set(jobs)


def test_demo_has_no_real_names(project):
    banned = ("BK", "Kigali", "RWF", "Rwanda")
    for f in project.rglob("*"):
        if f.is_file() and f.suffix in (".yaml", ".txt", ".csv", ".jsonl", ".xml"):
            text = f.read_text(encoding="utf-8", errors="replace")
            for word in banned:
                assert word not in text.replace("BKTOCSTMR", "").replace("BkToCstmr", ""), (f.name, word)


def test_demo_bank_jobs_run_and_two_feeds_land(tmp_path):
    from etlplus import config, runner
    root = demo.generate(tmp_path / "d2", today=TODAY)
    p = config.Project.load(root)
    if "bank_mt940_lines" not in p.jobs or "bank_camt053_entries" not in p.jobs:
        pytest.skip("the bank formats are not readable here")
    for name in ("bank_mt940_lines", "bank_camt053_entries", "card_daily_ni_style", "payments_mt103",
                 "customers_to_reporting"):
        if name in p.jobs:
            rec = runner.run(p, name)
            assert rec["status"] == "done", (name, rec["error"])
            assert "map" in rec["steps"]
    con = duckdb.connect(str(root / "reporting.duckdb"), read_only=True)
    try:
        by = dict(con.execute("SELECT source_system, count(*) FROM statement_lines GROUP BY 1").fetchall())
        assert by.get("MT940", 0) > 0 and by.get("CAMT053", 0) > 0
        assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 400
        assert con.execute("SELECT count(*) FROM statement_lines WHERE loaded_at IS NULL").fetchone()[0] == 0
    finally:
        con.close()
