"""Run history, alerts and watermarks in <project>/runs.sqlite."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT,
    started_at TEXT,
    finished_at TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_job ON runs (job, started_at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    job TEXT,
    run_id TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
    job TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    job TEXT NOT NULL,
    connection TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified TEXT NOT NULL,
    sha256 TEXT,
    run_id TEXT,
    loaded_at TEXT,
    PRIMARY KEY (job, connection, path, size, modified)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(project) -> sqlite3.Connection:
    path = project.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _json_default(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (set, tuple)):
        return list(v)
    return str(v)


def record(project, run: dict) -> None:
    """Insert or update a run record. A run that ends failed raises one alert."""
    data = json.dumps(run, default=_json_default)
    with _lock:
        con = _connect(project)
        try:
            with con:
                prev = con.execute("SELECT status FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()
                con.execute(
                    "INSERT INTO runs (run_id, job, status, trigger, started_at, finished_at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run_id) DO UPDATE SET job = excluded.job, "
                    "status = excluded.status, trigger = excluded.trigger, started_at = excluded.started_at, "
                    "finished_at = excluded.finished_at, data = excluded.data",
                    (run["run_id"], run.get("job"), run.get("status", "running"), run.get("trigger"),
                     run.get("started_at"), run.get("finished_at"), data),
                )
                if run.get("status") == "failed" and (prev is None or prev["status"] != "failed"):
                    con.execute(
                        "INSERT INTO alerts (at, job, run_id, level, message) VALUES (?, ?, ?, ?, ?)",
                        (run.get("finished_at") or now(), run.get("job"), run["run_id"], "error",
                         run.get("error") or "The run failed."),
                    )
        finally:
            con.close()


def add_alert(project, message: str, job: str | None = None, run_id: str | None = None, level: str = "warning") -> None:
    with _lock:
        con = _connect(project)
        try:
            with con:
                con.execute(
                    "INSERT INTO alerts (at, job, run_id, level, message) VALUES (?, ?, ?, ?, ?)",
                    (now(), job, run_id, level, message),
                )
        finally:
            con.close()


def runs(project, job=None, limit=50, offset=0) -> list[dict]:
    con = _connect(project)
    try:
        if job:
            rows = con.execute("SELECT data FROM runs WHERE job = ? ORDER BY started_at DESC, rowid DESC "
                               "LIMIT ? OFFSET ?", (job, int(limit), max(int(offset or 0), 0))).fetchall()
        else:
            rows = con.execute("SELECT data FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
                               (int(limit), max(int(offset or 0), 0))).fetchall()
        return [json.loads(r["data"]) for r in rows]
    finally:
        con.close()


def count_runs(project, job=None) -> int:
    con = _connect(project)
    try:
        if job:
            return int(con.execute("SELECT count(*) FROM runs WHERE job = ?", (job,)).fetchone()[0])
        return int(con.execute("SELECT count(*) FROM runs").fetchone()[0])
    finally:
        con.close()


def count_alerts(project) -> int:
    con = _connect(project)
    try:
        return int(con.execute("SELECT count(*) FROM alerts").fetchone()[0])
    finally:
        con.close()


def get(project, run_id) -> dict | None:
    con = _connect(project)
    try:
        row = con.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["data"]) if row else None
    finally:
        con.close()


def last_runs(project) -> dict[str, dict]:
    """The most recent run of every job."""
    con = _connect(project)
    try:
        rows = con.execute(
            "SELECT r.data FROM runs r JOIN (SELECT job, MAX(started_at) AS s FROM runs GROUP BY job) m "
            "ON r.job = m.job AND r.started_at = m.s ORDER BY r.rowid"
        ).fetchall()
        out = {}
        for r in rows:
            d = json.loads(r["data"])
            out[d["job"]] = d
        return out
    finally:
        con.close()


def running_runs(project) -> list[dict]:
    """Every run stored as running: {run_id, job}."""
    con = _connect(project)
    try:
        rows = con.execute("SELECT run_id, job FROM runs WHERE status = 'running'").fetchall()
        return [{"run_id": r["run_id"], "job": r["job"]} for r in rows]
    finally:
        con.close()


def mark_interrupted(project, running_ids: set[str]) -> int:
    """Runs stored as running that no live process owns are marked failed."""
    con = _connect(project)
    n = 0
    try:
        rows = con.execute("SELECT run_id, data FROM runs WHERE status = 'running'").fetchall()
    finally:
        con.close()
    for r in rows:
        if r["run_id"] in running_ids:
            continue
        d = json.loads(r["data"])
        d["status"] = "failed"
        d["finished_at"] = d.get("finished_at") or now()
        d["error"] = "The run was interrupted (the process stopped before it finished)."
        record(project, d)
        n += 1
    return n


def alerts(project, limit=50, offset=0) -> list[dict]:
    con = _connect(project)
    try:
        rows = con.execute("SELECT id, at, job, run_id, level, message FROM alerts ORDER BY id DESC "
                           "LIMIT ? OFFSET ?", (int(limit), max(int(offset or 0), 0))).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------- the file ledger (SPEC_V2 4.4)

def _mod_text(modified) -> str:
    if isinstance(modified, datetime):
        return modified.isoformat()
    return str(modified or "")


def file_seen(project, job, cid, path, size, modified) -> bool:
    """True when this exact file (path, size and modified time) was loaded by the job before."""
    con = _connect(project)
    try:
        row = con.execute("SELECT 1 FROM files WHERE job = ? AND connection = ? AND path = ? AND size = ? "
                          "AND modified = ?", (job, cid, str(path), int(size or 0), _mod_text(modified))).fetchone()
        return row is not None
    finally:
        con.close()


def mark_files(project, job, run_id, cid, files: list[dict]) -> None:
    """Record files as loaded. Called by the runner after a successful load only."""
    if not files:
        return
    at = now()
    rows = []
    for f in files:
        path = f.get("remote") or f.get("path")
        if not path:
            continue
        rows.append((job, cid, str(path), int(f.get("size") or 0), _mod_text(f.get("modified")),
                     f.get("sha256"), run_id, at))
    with _lock:
        con = _connect(project)
        try:
            with con:
                con.executemany(
                    "INSERT OR REPLACE INTO files (job, connection, path, size, modified, sha256, run_id, loaded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        finally:
            con.close()


def files(project, job, limit=100, offset=0) -> tuple[list[dict], int]:
    """The ledger of one job, newest first, and the total count."""
    con = _connect(project)
    try:
        total = int(con.execute("SELECT count(*) FROM files WHERE job = ?", (job,)).fetchone()[0])
        rows = con.execute("SELECT job, connection, path, size, modified, sha256, run_id, loaded_at FROM files "
                           "WHERE job = ? ORDER BY loaded_at DESC, path LIMIT ? OFFSET ?",
                           (job, max(int(limit), 0), max(int(offset or 0), 0))).fetchall()
        return [dict(r) for r in rows], total
    finally:
        con.close()


def _encode(value):
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    return {"type": "plain", "value": value}


def _decode(d):
    t, v = d.get("type"), d.get("value")
    if v is None:
        return None
    if t == "datetime":
        return datetime.fromisoformat(v)
    if t == "date":
        return date.fromisoformat(v)
    if t == "decimal":
        return Decimal(v)
    return v


def watermark(project, job) -> object:
    con = _connect(project)
    try:
        row = con.execute("SELECT value FROM watermarks WHERE job = ?", (job,)).fetchone()
        return _decode(json.loads(row["value"])) if row and row["value"] else None
    finally:
        con.close()


def set_watermark(project, job, value) -> None:
    with _lock:
        con = _connect(project)
        try:
            with con:
                if value is None:
                    con.execute("DELETE FROM watermarks WHERE job = ?", (job,))
                else:
                    con.execute("INSERT OR REPLACE INTO watermarks (job, value, updated_at) VALUES (?, ?, ?)",
                                (job, json.dumps(_encode(value), default=_json_default), now()))
        finally:
            con.close()
