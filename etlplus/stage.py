"""The stage: every extract lands as a Parquet file with a manifest beside it.

Layout: <stage>/<job>/<run_id>/extract.parquet and manifest.json (a transform adds
transformed.parquet and transformed.manifest.json). A file is written as *.partial, the
manifest is written next, and only then is the file renamed, so a reader never sees half a
file: a stage is complete when its manifest and its named file both exist.
"""

import datetime as dt
import hashlib
import json
import os
import re
import secrets as _random
import shutil
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

FILES = {"extract": "extract.parquet", "transformed": "transformed.parquet", "mapped": "mapped.parquet"}
MANIFESTS = {"extract": "manifest.json", "transformed": "transformed.manifest.json",
             "mapped": "mapped.manifest.json"}
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4,}$")


def _kind(kind: str) -> str:
    k = (kind or "extract").lower()
    if k in ("transform", "transformed"):
        return "transformed"
    if k in ("map", "mapped", "mapping"):
        return "mapped"
    return k


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(log, msg):
    if log:
        log(msg)


def new_run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + _random.token_hex(2)


def job_dir(project, job_name) -> Path:
    if not job_name or "/" in job_name or "\\" in job_name or job_name.startswith("."):
        raise ValueError(f"Not a valid job name: {job_name!r}")
    return Path(project.stage_dir()) / job_name


def run_dir(project, job_name, run_id) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise ValueError(f"Not a valid run id: {run_id!r}")
    return job_dir(project, job_name) / run_id


def path(project, job_name, run_id, kind="extract") -> Path:
    k = _kind(kind)
    return run_dir(project, job_name, run_id) / FILES.get(k, f"{k}.parquet")


def manifest_path(project, job_name, run_id, kind="extract") -> Path:
    k = _kind(kind)
    return run_dir(project, job_name, run_id) / MANIFESTS.get(k, f"{k}.manifest.json")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(v):
    if v is None:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v)


def _writable_schema(schema: pa.Schema) -> pa.Schema:
    return pa.schema([pa.field(f.name, pa.string() if pa.types.is_null(f.type) else f.type)
                      for f in schema])


def write(project, job_name, run_id, batches, window, source_desc, log, kind="extract", extra=None) -> dict:
    """Write batches to <kind>.parquet.partial, then the manifest, then rename. Returns the manifest.

    extra: a dict (or a function returning one, called after the last batch) merged into the
    manifest, for example the fetched files of a remote source."""
    k = _kind(kind)
    final = path(project, job_name, run_id, k)
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + ".partial")
    mpath = manifest_path(project, job_name, run_id, k)
    for stale in (partial, final, mpath):
        if stale.exists():
            stale.unlink()
    started = _now()
    writer = None
    schema = None
    rows = 0
    next_log = 0
    try:
        for batch in batches:
            if isinstance(batch, pa.Table):
                parts = batch.to_batches()
            else:
                parts = [batch]
            for b in parts:
                if writer is None:
                    schema = _writable_schema(b.schema)
                    writer = pq.ParquetWriter(str(partial), schema, compression="zstd")
                if b.schema != schema:
                    try:
                        b = pa.RecordBatch.from_arrays(
                            [b.column(b.schema.get_field_index(f.name)).cast(f.type) for f in schema],
                            schema=schema)
                    except Exception as e:
                        raise ValueError(f"A chunk does not fit the staged schema: {e}") from None
                if b.num_rows:
                    writer.write_batch(b)
                rows += b.num_rows
                if rows >= next_log:
                    _log(log, f"stage: {rows:,} rows")
                    next_log = rows + 1_000_000
        if writer is None:
            schema = pa.schema([])
            pq.write_table(pa.table({}), str(partial), compression="zstd")
        else:
            writer.close()
            writer = None
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
        raise

    win = None
    if window is not None:
        win = [_iso(window[0]), _iso(window[1])]
    manifest = {
        "job": job_name,
        "run_id": run_id,
        "kind": k,
        "rows": rows,
        "columns": [{"name": f.name, "type": str(f.type)} for f in schema],
        "window": win,
        "source": str(source_desc or ""),
        "file": final.name,
        "bytes": partial.stat().st_size,
        "sha256": _sha256(partial),
        "started_at": started,
        "finished_at": _now(),
    }
    more = extra() if callable(extra) else extra
    if more:
        manifest.update({k: v for k, v in dict(more).items() if k not in manifest})
    tmp = mpath.with_name(mpath.name + ".partial")
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, mpath)
    os.replace(partial, final)
    _log(log, f"stage: {rows:,} rows in {final.name} ({manifest['bytes']:,} bytes)")
    return manifest


def read_manifest(project, job_name, run_id, kind="extract") -> dict | None:
    """The manifest of a complete stage, or None while it is partial or missing."""
    k = _kind(kind)
    mp = manifest_path(project, job_name, run_id, k)
    if not mp.exists():
        return None
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _kind(m.get("kind") or "extract") != k:
        return None
    f = mp.parent / (m.get("file") or FILES.get(k, f"{k}.parquet"))
    if not f.exists() or f.name.endswith(".partial"):
        return None
    return m


def _run_dirs(project, job_name) -> list[Path]:
    jd = job_dir(project, job_name)
    if not jd.is_dir():
        return []
    return sorted((d for d in jd.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: d.name, reverse=True)


def runs(project, job_name) -> list[dict]:
    """Staged runs newest first: {run_id, extract, transformed, partial}."""
    out = []
    for d in _run_dirs(project, job_name):
        out.append({
            "run_id": d.name,
            "extract": read_manifest(project, job_name, d.name, "extract"),
            "transformed": read_manifest(project, job_name, d.name, "transformed"),
            "mapped": read_manifest(project, job_name, d.name, "mapped"),
            "partial": any(p.name.endswith(".partial") for p in d.iterdir()),
        })
    return out


def latest(project, job_name, kind="extract") -> dict | None:
    k = _kind(kind)
    for d in _run_dirs(project, job_name):
        m = read_manifest(project, job_name, d.name, k)
        if m:
            return m
    return None


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    if not s:
        return None
    if len(s) == 10:
        return dt.date.fromisoformat(s)
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def window_for(job: dict, today: dt.date, override: tuple | None) -> tuple | None:
    """The extract window, half open [from, to). An override (from, to) wins."""
    if override is not None:
        lo, hi = override if isinstance(override, (list, tuple)) else (override, None)
        lo, hi = _to_date(lo), _to_date(hi)
        if lo is None and hi is None:
            return None
        if lo is not None and hi is not None and lo >= hi:
            raise ValueError(f"The window is empty: from {lo} is not before to {hi}.")
        return (lo, hi)
    inc = (job.get("source") or {}).get("incremental") or {}
    if not inc.get("column") or inc.get("watermark"):
        return None
    if isinstance(today, dt.datetime):
        today = today.date()
    days = int(inc.get("lookback_days") or 1)
    if days < 1:
        raise ValueError("lookback_days must be 1 or more.")
    return (today - dt.timedelta(days=days), today)


def cleanup(project, job_name, keep: int) -> None:
    """Keep the newest `keep` staged runs of a job; remove the rest. A run still writing stays."""
    keep = max(int(keep or 0), 0)
    # only folders etl+ made (named like a run id) are ever removed
    dirs = [d for d in _run_dirs(project, job_name) if RUN_ID_RE.match(d.name)]
    complete = 0
    for d in dirs:
        partial = [p for p in d.iterdir() if p.name.endswith(".partial")]
        if partial and time.time() - max(p.stat().st_mtime for p in partial) < 3600:
            continue
        if complete < keep and read_manifest(project, job_name, d.name, "extract"):
            complete += 1
            continue
        shutil.rmtree(d, ignore_errors=True)


def size(project, job_name=None) -> int:
    root = Path(project.stage_dir())
    if job_name:
        root = root / job_name
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
