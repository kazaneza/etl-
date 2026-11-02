"""Run a job: extract -> stage, transform, load. Run records, logs, watermarks, cancel."""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from etlplus import store

STEPS = ("extract", "transform", "map", "load")
V1_FORMATS = ("csv", "txt", "fixed", "parquet", "json", "jsonl", "excel")
FILE_SOURCE_KINDS = ("folder", "share", "sftp", "ftp", "s3", "azure_blob", "http")
AFTER_ACTIONS = ("keep", "archive", "mark_done", "delete")


def _all_formats() -> tuple:
    """Every file format a job may name: sources.ALL_FORMATS (bank formats included) when B1
    has it, else the v1 list."""
    try:
        from etlplus import sources
        fm = getattr(sources, "ALL_FORMATS", None)
        if fm:
            return tuple(sorted(fm))
    except Exception:
        pass
    try:
        from etlplus import formats
        return V1_FORMATS + tuple(sorted(set(formats.FORMATS) - set(V1_FORMATS)))
    except Exception:
        return V1_FORMATS


def _db_source_kinds() -> tuple:
    try:
        from etlplus import drivers
        return tuple(k for k, d in drivers.REGISTRY.items() if d.category in ("database", "document")
                     and "source" in d.roles)
    except Exception:
        return ("postgres", "sqlserver", "mysql", "oracle", "sqlite", "duckdb")


FORMATS = _all_formats()          # an alias of sources.ALL_FORMATS (SPEC_V2 2.1)
DB_SOURCE_KINDS = _db_source_kinds()
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

_lock = threading.Lock()
_active: dict[str, dict] = {}       # run_id -> {"job", "project", "cancel": Event, "thread"}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- one run per job, across processes

class JobLock:
    """An exclusive OS lock on <project>/.locks/<job>.lock, held for the whole run.

    The in-process check (_active) cannot see a run started by another process (the CLI next to
    `etlplus serve`, a second scheduler). This lock can, and the OS drops it if the process dies.
    """

    def __init__(self, project, job: str):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job)).lstrip(".") or "_"
        self.path = Path(project.path) / ".locks" / f"{safe}.lock"
        self._f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            f.close()


def job_locked(project, job: str) -> bool:
    """True while some process (this one included) holds the job's run lock."""
    lk = JobLock(project, job)
    if lk.acquire():
        lk.release()
        return False
    return True


def reap(project) -> int:
    """Mark runs left 'running' by a process that stopped as failed. Returns how many."""
    keep = set(running(project))
    try:
        for r in store.running_runs(project):
            if r["run_id"] not in keep and job_locked(project, r["job"]):
                keep.add(r["run_id"])
        return store.mark_interrupted(project, keep)
    except Exception:
        return 0


# ---------------------------------------------------------------- validation

def _upsert_style(kind):
    """The registry's upsert style for a target kind; None means the kind cannot upsert."""
    try:
        from etlplus import drivers
        return drivers.get(kind).upsert
    except Exception:
        return None if kind == "clickhouse" else "delete_insert"


def _layout_problems(project, layout) -> list[str]:
    """format: layout needs options.layout: a layout name that exists, or a layout mapping."""
    if isinstance(layout, dict):
        try:
            from etlplus.formats import layouts
            probs = layouts.validate_layout(layout)
            return [f"The layout has problems: {' '.join(probs)}"] if probs else []
        except ImportError:
            return []
    if not layout:
        return ["format layout needs options.layout (a layout name)."]
    try:
        from etlplus.formats import layouts
        layouts.load(layouts.check_name(layout), project)
    except ImportError:
        return []
    except KeyError:
        return [f"There is no layout named '{layout}'."]
    except ValueError as e:
        return [str(e) if str(e).endswith(".") else str(e) + "."]
    return []


def _pickup_problems(src: dict, spec: dict) -> list[str]:
    p = []
    pk = src.get("pickup")
    if pk is None:
        return p
    if not isinstance(pk, dict):
        return ["source.pickup must be a block of settings."]
    if "skip_seen" in pk and not isinstance(pk["skip_seen"], bool):
        p.append("pickup.skip_seen is true or false.")
    for k in ("min_age_seconds", "max_files"):
        v = pk.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            p.append(f"pickup.{k} must be a whole number, 0 or more.")
    after = pk.get("after", "keep")
    if after not in AFTER_ACTIONS:
        p.append("pickup.after is keep, archive, mark_done or delete.")
    elif after != "keep":
        if not (spec.get("options") or {}).get("allow_after_actions"):
            p.append("This connection does not allow moving or deleting files; set allow_after_actions "
                     "on the connection.")
        if after == "archive":
            to = pk.get("archive_to")
            if not to:
                p.append("pickup.after archive needs archive_to (a folder in the location).")
            else:
                try:
                    from etlplus.destinations import check_rel_path
                    check_rel_path(str(to).rstrip("/"), "archive_to")
                except ValueError as e:
                    p.append(str(e))
    return p


def validate_job(project, job: dict) -> list[str]:
    p: list[str] = []
    if not isinstance(job, dict):
        return ["The job must be a mapping of settings."]
    conns = getattr(project, "connections", None) or {}
    name = job.get("name")
    if not name:
        p.append("The job needs a name.")
    elif not NAME_RE.match(str(name)):
        p.append("The job name can use letters, digits, '_', '-' and '.', and must start with a letter or digit.")

    src = job.get("source")
    if not isinstance(src, dict):
        p.append("The job needs a source.")
        src = {}
    else:
        cid = src.get("connection")
        spec = conns.get(cid) if cid else None
        if not cid:
            p.append("The source needs a connection.")
        elif spec is None:
            p.append(f"The source connection '{cid}' does not exist.")
        if spec is not None and spec.get("role") == "target":
            p.append(f"Connection '{cid}' is a target only; set its role to source or both to read from it.")
        kind = (spec or {}).get("kind")
        given = [k for k in ("table", "query", "files") if src.get(k)]
        if kind == "mongodb":
            if not src.get("table"):
                p.append("A MongoDB source needs table (the collection); query is an optional filter.")
            if src.get("files"):
                p.append(f"'{cid}' is MongoDB: give table (the collection), not files.")
        elif kind == "http" and not given:
            pass  # an http source reads its one URL; files defaults to "*"
        elif len(given) != 1:
            p.append("The source needs exactly one of table, query or files.")
        if src.get("files") and kind and kind not in FILE_SOURCE_KINDS:
            p.append(f"files can only be read from a folder or a remote location; '{cid}' is {kind}.")
        if (src.get("table") or src.get("query")) and kind in FILE_SOURCE_KINDS:
            p.append(f"'{cid}' is a folder or remote location: give files (a pattern such as in/*.csv), "
                     "not a table or query.")
        p += _pickup_problems(src, spec or {})
        if src.get("query") and kind == "mongodb":
            try:
                from etlplus.drivers import mongo
                mongo.parse_query(src["query"])
            except ImportError:
                pass
            except ValueError as e:
                p.append(f"The source query is refused: {e}")
        elif src.get("query"):
            try:
                from etlplus import guard
                guard.check_select(src["query"], kind)
            except ImportError:
                pass
            except ValueError as e:
                p.append(f"The source query is refused: {e}")
        if src.get("files") or (kind == "http" and not src.get("table") and not src.get("query")):
            fmt = src.get("format")
            formats = _all_formats()
            if not fmt:
                p.append("A file source needs a format: " + ", ".join(formats) + ".")
            elif fmt not in formats:
                p.append(f"'{fmt}' is not a file format. Use one of {', '.join(formats)}.")
            opts = src.get("options") or {}
            if fmt == "layout":
                p += _layout_problems(project, opts.get("layout"))
            if fmt == "fixed":
                widths, names = opts.get("widths"), opts.get("names")
                if not widths or not names:
                    p.append("A fixed width file needs options.widths and options.names.")
                elif len(widths) != len(names):
                    p.append(f"Fixed width: {len(widths)} widths but {len(names)} names; they must match.")
                elif not all(isinstance(w, int) and w > 0 for w in widths):
                    p.append("Fixed width: every width must be a whole number above zero.")
        cols = src.get("columns")
        if cols not in (None, "*") and not (isinstance(cols, list) and all(isinstance(c, str) and c for c in cols)):
            p.append("source.columns must be \"*\" or a list of column names.")
        inc = src.get("incremental")
        if inc:
            if not isinstance(inc, dict) or not inc.get("column"):
                p.append("incremental needs a column.")
            else:
                lb, wm = inc.get("lookback_days"), inc.get("watermark")
                if lb is None and not wm:
                    p.append("incremental needs lookback_days or watermark: true.")
                if lb is not None and (not isinstance(lb, int) or isinstance(lb, bool) or lb < 1):
                    p.append("lookback_days must be a whole number of days, 1 or more.")
        cr = src.get("chunk_rows")
        if cr is not None and (not isinstance(cr, int) or isinstance(cr, bool) or cr <= 0):
            p.append("chunk_rows must be a whole number above zero.")

    steps = job.get("transform")
    if steps:
        try:
            from etlplus import transform
            transform.validate(steps)
        except ValueError as e:
            p.append(f"Transform: {e}")

    t = job.get("target")
    if isinstance(t, dict) and t.get("destination"):
        did = t.get("destination")
        dests = getattr(project, "destinations", None) or {}
        if did not in dests:
            p.append(f"The destination '{did}' does not exist.")
            t = None
        else:
            extra = [k for k in ("connection", "table") if t.get(k)]
            if extra:
                p.append("A target with a destination takes mode, key and slice_column, not "
                         + " or ".join(extra) + ".")
            try:
                from etlplus import targets
                t = targets.resolve_job(project, job)["target"]
            except Exception as e:
                p.append(f"The destination '{did}' cannot be used: {e}")
                t = None
    if t is None:
        pass
    elif not isinstance(t, dict):
        p.append("The job needs a target.")
    else:
        cid = t.get("connection")
        spec = conns.get(cid) if cid else None
        if not cid:
            p.append("The target needs a connection.")
        elif spec is None:
            p.append(f"The target connection '{cid}' does not exist.")
        elif spec.get("role") == "source":
            p.append(f"Connection '{cid}' is a source (read only); set its role to target or both to load into it.")
        if not t.get("table"):
            p.append("The target needs a table (a file name for a folder).")
        mode = t.get("mode", "replace")
        if mode not in ("replace", "append", "upsert"):
            p.append(f"'{mode}' is not a load mode. Use replace, append or upsert.")
        key = t.get("key")
        if mode == "upsert" and not key:
            p.append("upsert needs key columns (target.key) to match rows on.")
        if key is not None and not (isinstance(key, str) or (isinstance(key, list) and all(isinstance(k, str) for k in key))):
            p.append("target.key must be a list of column names.")
        inc = (src or {}).get("incremental") or {}
        if mode == "append" and not (t.get("slice_column") or inc.get("column")):
            p.append("append needs a window: set source.incremental, or target.slice_column, so a re-run "
                     "replaces its slice instead of adding the rows twice.")
        tkind = (spec or {}).get("kind")
        if tkind in FILE_SOURCE_KINDS:
            from etlplus.targets import FILE_FORMATS
            fmt = t.get("format") or "parquet"
            if tkind == "folder" and fmt not in FILE_FORMATS:
                p.append("A folder target writes parquet or csv.")
            elif fmt not in FILE_FORMATS:
                p.append(f"A file target writes {', '.join(FILE_FORMATS)}.")
            if tkind not in ("folder", "share") and mode == "upsert":
                p.append("A remote location takes replace or append, not upsert.")
        elif mode == "upsert" and tkind and _upsert_style(tkind) is None:
            p.append(f"{tkind} cannot upsert; use replace or append.")
        bulk = t.get("bulk")
        if bulk:
            if tkind not in ("sqlserver",):
                p.append("bulk (BULK INSERT) only applies to a sqlserver target.")
            elif not isinstance(bulk, dict) or not bulk.get("local_dir") or not bulk.get("remote_dir"):
                p.append("bulk needs local_dir and remote_dir.")

    if job.get("mapping") is not None:
        from etlplus import mapping
        p += mapping.check_config(job.get("mapping"))

    sched = job.get("schedule")
    if sched:
        try:
            from etlplus.scheduler import cron_trigger
            cron_trigger(str(sched))
        except Exception as e:
            p.append(f"The schedule '{sched}' is not a valid cron line ({e}).")
    nt = job.get("notify")
    if nt is not None and nt not in ("never", "failure", "always"):
        p.append("notify must be never, failure or always.")
    return p


# ---------------------------------------------------------------- log

class RunLog:
    """Callable log: writes timestamped lines to the run's log file. check() raises when cancelled."""

    def __init__(self, path: Path, cancel: threading.Event | None = None):
        self.path = path
        self.cancel = cancel
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    def __call__(self, msg, *args):
        if args:
            try:
                msg = str(msg) % args
            except Exception:
                msg = " ".join([str(msg), *map(str, args)])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in str(msg).splitlines() or [""]:
            self._f.write(f"{ts}  {line}\n")
        self._f.flush()

    info = warning = write = __call__

    def error(self, msg, *args):
        self("ERROR " + str(msg), *args)

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("The run was cancelled.")

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def log_path(project, job: str, run_id: str) -> Path:
    return Path(project.logs_dir()) / job / f"{run_id}.log"


def log_tail(project, job: str, run_id: str, lines=200) -> list[str]:
    p = log_path(project, job, run_id)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]


# ---------------------------------------------------------------- helpers

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today(project) -> date:
    tz = (project.settings or {}).get("timezone")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    return date.today()


def _source_desc(job) -> str:
    s = job.get("source") or {}
    what = s.get("table") or s.get("files") or ("query: " + " ".join(str(s.get("query") or "").split())[:120])
    return f"{s.get('connection')}:{what}"


def _key(project, job) -> str:
    return f"{Path(project.path).resolve()}::{job}"


def _run_dir_manifest(project, job_name, run_id, kind):
    from etlplus import stage, transform
    p = Path(stage.path(project, job_name, run_id, "extract"))
    run_dir = p.parent if p.suffix else p
    return transform.read_manifest(run_dir, kind)


def latest_loadable(project, job: dict) -> dict:
    """The manifest a load-only run uses: the latest complete stage; mapped when the job maps,
    else transformed when the job transforms, else the extract (SPEC_V2 8.4)."""
    from etlplus import stage
    name = job["name"]
    m = stage.latest(project, name, "extract")
    if not m:
        raise ValueError(f"Nothing is staged for job '{name}' yet. Run the extract step first.")
    if _has_mapping(job):
        mp = _run_dir_manifest(project, name, m["run_id"], "mapped")
        if not mp:
            raise ValueError(f"The latest staged extract ({m['run_id']}) has not been mapped yet. "
                             "Run the map step first.")
        return mp
    if job.get("transform"):
        t = _run_dir_manifest(project, name, m["run_id"], "transformed")
        if not t:
            raise ValueError(f"The latest staged extract ({m['run_id']}) has not been transformed yet. "
                             "Run the transform step first.")
        return t
    return m


def _has_mapping(job) -> bool:
    m = (job or {}).get("mapping")
    return isinstance(m, dict) and bool(m.get("columns"))


def _read_kwargs(fetch_dir, files_out, checks_out=None) -> dict:
    """sources.read takes fetch_dir, files_out and checks_out in v2 (B1); an older reader does not."""
    try:
        import inspect
        from etlplus import sources
        params = inspect.signature(sources.read).parameters
    except (TypeError, ValueError, ImportError):
        return {}
    kw = {}
    if "fetch_dir" in params:
        kw["fetch_dir"] = fetch_dir
    if "files_out" in params:
        kw["files_out"] = files_out
    if "checks_out" in params and checks_out is not None:
        kw["checks_out"] = checks_out
    return kw


def _window_tuple(window):
    if not window:
        return None
    a, b = window
    conv = []
    for v in (a, b):
        if isinstance(v, str):
            v = date.fromisoformat(v) if len(v) == 10 else datetime.fromisoformat(v)
        conv.append(v)
    return tuple(conv)


def _max_value(path: Path, column: str):
    import duckdb
    con = duckdb.connect()
    try:
        c = '"' + column.replace('"', '""') + '"'
        cols = [r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()]
        if column not in cols:
            return None
        return con.execute(f"SELECT max({c}) FROM read_parquet(?)", [str(path)]).fetchone()[0]
    finally:
        con.close()


def _greater(a, b) -> bool:
    if b is None:
        return a is not None
    if a is None:
        return False
    try:
        return a > b
    except TypeError:
        return str(a) > str(b)


# ---------------------------------------------------------------- run

def run(project, name: str, steps=STEPS, window=None, trigger="manual", run_id=None) -> dict:
    from etlplus import stage
    steps = tuple(s for s in STEPS if s in (steps or STEPS))
    job = (project.jobs or {}).get(name)
    if run_id is None:
        run_id = stage.new_run_id()
    key = _key(project, name)
    cancel = None
    with _lock:
        for rid, a in _active.items():
            if a["key"] == key and rid != run_id:
                raise RuntimeError(f"Job '{name}' is already running (run {rid}).")
        if run_id in _active:
            cancel = _active[run_id]["cancel"]
        else:
            cancel = threading.Event()
            _active[run_id] = {"key": key, "job": name, "cancel": cancel, "thread": None}
        flock = _active[run_id].get("flock")
    if flock is None:
        flock = JobLock(project, name)
        if not flock.acquire():
            with _lock:
                _active.pop(run_id, None)
            raise RuntimeError(f"Job '{name}' is already running in another process.")
        with _lock:
            _active[run_id]["flock"] = flock

    rec = {"run_id": run_id, "job": name, "status": "running", "steps": {}, "rows": {},
           "started_at": _now(), "finished_at": None, "error": None, "trigger": trigger,
           "window": None, "stage_run_id": None}
    log = RunLog(log_path(project, name, run_id), cancel)
    try:
        if job is None:
            raise ValueError(f"There is no job named '{name}'.")
        job = dict(job)
        job.setdefault("name", name)
        store.record(project, rec)
        problems = validate_job(project, job)
        if problems:
            raise ValueError("The job has problems: " + " ".join(problems))
        log(f"run {run_id} of {name} ({trigger}), steps: {', '.join(steps)}")
        manifest = None

        if "extract" in steps:
            _step(rec, "extract", "running")
            store.record(project, rec)
            t0 = time.time()
            from etlplus import sources
            win = stage.window_for(job, _today(project), _window_tuple(window))
            rec["window"] = [str(w) for w in win] if win else None
            if win:
                log(f"window {win[0]} .. {win[1]}")
            counted = {"rows": 0}
            fetched: list = []
            checks: list = []
            kw = _read_kwargs(stage.run_dir(project, name, run_id) / "incoming", fetched, checks)

            def batches():
                for b in sources.read(project, job, win, log, **kw):
                    log.check()
                    counted["rows"] += b.num_rows
                    yield b
                log.check()

            manifest = stage.write(project, name, run_id, batches(), win, _source_desc(job), log,
                                   extra=lambda: {k: v for k, v in (("files", list(fetched)),
                                                                    ("checks", list(checks))) if v})
            rec["stage_run_id"] = run_id
            rec["rows"]["extracted"] = manifest.get("rows", counted["rows"])
            _step(rec, "extract", "done", rows=rec["rows"]["extracted"], ms=_ms(t0))
            store.record(project, rec)

        has_transform = bool(job.get("transform"))
        if "transform" in steps and has_transform:
            log.check()
            _step(rec, "transform", "running")
            store.record(project, rec)
            t0 = time.time()
            from etlplus import transform
            if manifest is None:
                manifest = stage.latest(project, name, "extract")
                if not manifest:
                    raise ValueError(f"Nothing is staged for job '{name}' yet. Run the extract step first.")
                rec["stage_run_id"] = manifest["run_id"]
                rec["window"] = rec["window"] or manifest.get("window")
            manifest = transform.apply(project, job, manifest["run_id"], log)
            rec["rows"]["transformed"] = manifest.get("rows")
            _step(rec, "transform", "done", rows=manifest.get("rows"), ms=_ms(t0))
            store.record(project, rec)
        elif "transform" in steps and steps == ("transform",):
            _step(rec, "transform", "skipped", detail="The job has no transform steps.")

        has_mapping = _has_mapping(job)
        if "map" in steps and has_mapping:
            log.check()
            _step(rec, "map", "running")
            store.record(project, rec)
            t0 = time.time()
            from etlplus import mapping
            if manifest is None or (has_transform and manifest.get("kind") != "transformed"):
                base = stage.latest(project, name, "extract")
                if not base:
                    raise ValueError(f"Nothing is staged for job '{name}' yet. Run the extract step first.")
                if has_transform:
                    manifest = _run_dir_manifest(project, name, base["run_id"], "transformed")
                    if not manifest:
                        raise ValueError(f"The latest staged extract ({base['run_id']}) has not been transformed "
                                         "yet. Run the transform step first.")
                else:
                    manifest = base
                rec["stage_run_id"] = base["run_id"]
                rec["window"] = rec["window"] or base.get("window")
            manifest = mapping.apply(project, job, manifest, log)
            rec["rows"]["mapped"] = manifest.get("rows")
            _step(rec, "map", "done", rows=manifest.get("rows"), ms=_ms(t0),
                  detail=f"{len(manifest.get('columns') or [])} columns")
            store.record(project, rec)

        if "load" in steps:
            log.check()
            _step(rec, "load", "running")
            store.record(project, rec)
            t0 = time.time()
            from etlplus import targets
            want = "mapped" if has_mapping else ("transformed" if has_transform else None)
            if manifest is None or (want and manifest.get("kind") != want):
                manifest = latest_loadable(project, job)
                rec["stage_run_id"] = manifest["run_id"]
                rec["window"] = rec["window"] or manifest.get("window")
                log(f"load from staged run {manifest['run_id']} ({manifest.get('kind', 'extract')})")
            did = (job.get("target") or {}).get("destination")
            if did:
                from etlplus import destinations
                destinations.ensure(project, did, log)
            result = targets.load(project, job, manifest, log)
            rec["rows"]["loaded"] = result.get("rows_loaded")
            rec["rows"]["deleted"] = result.get("deleted")
            _step(rec, "load", "done", rows=result.get("rows_loaded"), ms=_ms(t0),
                  detail=f"{result.get('mode')} into {result.get('target')}, {result.get('deleted', 0)} rows removed first")
            store.record(project, rec)
            _update_watermark(project, job, manifest, log)
            _after_load(project, job, rec, manifest, log)

        rec["status"] = "done"
        log("done")
    except Cancelled as e:
        rec["status"] = "cancelled"
        rec["error"] = str(e)
        _fail_running_step(rec, "cancelled")
        log("cancelled")
    except BaseException as e:
        rec["status"] = "failed"
        rec["error"] = str(e) or e.__class__.__name__
        _fail_running_step(rec, "failed", rec["error"])
        log.error(rec["error"])
        log(traceback.format_exc())
        if not isinstance(e, Exception):
            _finish(project, job, rec, log)
            raise
    _finish(project, job, rec, log)
    return rec


def _after_load(project, job, rec, manifest, log):
    """After a successful load only: the file ledger, then the pickup's after action."""
    from etlplus import stage
    src = job.get("source") or {}
    cid = src.get("connection")
    ext = stage.read_manifest(project, job["name"], manifest["run_id"], "extract") or {}
    files = ext.get("files") or []
    if not files:
        return
    store.mark_files(project, job["name"], rec["run_id"], cid, files)
    log(f"ledger: {len(files)} file(s) marked as loaded")
    pk = src.get("pickup") or {}
    action = pk.get("after", "keep")
    if action == "keep":
        return
    try:
        from etlplus import remote
        remote.finish(project, cid, files, action, pk.get("archive_to"), log=log)
    except Exception as e:
        log.error(f"after-load action {action} failed: {e}")
        store.add_alert(project, f"After-load action {action} failed: {e}", job=job["name"], run_id=rec["run_id"])


def _finish(project, job, rec, log):
    rec["finished_at"] = _now()
    try:
        store.record(project, rec)
    except Exception as e:
        log.error(f"could not record the run: {e}")
    if job:
        try:
            from etlplus import notify
            res = notify.notify_run(project, job, rec)
            for r in res or []:
                log(f"notify {r['channel']}: {'ok' if r['ok'] else 'failed'}, {r['detail']}")
                if not r["ok"]:
                    store.add_alert(project, f"Notification by {r['channel']} failed: {r['detail']}",
                                    job=rec["job"], run_id=rec["run_id"])
        except Exception as e:
            log.error(f"notify failed: {e}")
        if rec["status"] == "done":
            try:
                from etlplus import stage
                keep = int((project.settings or {}).get("keep_runs") or 10)
                stage.cleanup(project, rec["job"], keep)
            except Exception as e:
                log.error(f"stage cleanup failed: {e}")
    log.close()
    with _lock:
        a = _active.pop(rec["run_id"], None)
    if a and a.get("flock"):
        a["flock"].release()


def _update_watermark(project, job, manifest, log):
    inc = (job.get("source") or {}).get("incremental") or {}
    if not inc.get("watermark") or not inc.get("column"):
        return
    from etlplus import targets
    try:
        path = targets.staged_file(project, manifest)
    except Exception:
        return
    col = inc["column"]
    value = _max_value(path, col)
    if value is None:
        renamed = None
        for st in job.get("transform") or []:
            if "rename" in st and col in st["rename"]:
                renamed = st["rename"][col]
        if renamed:
            value = _max_value(path, renamed)
    old = store.watermark(project, job["name"])
    if value is not None and _greater(value, old):
        store.set_watermark(project, job["name"], value)
        log(f"watermark {col} = {value}")


def _ms(t0) -> int:
    return int((time.time() - t0) * 1000)


def _step(rec, name, status, rows=None, ms=None, detail=None):
    s = rec["steps"].setdefault(name, {})
    s["status"] = status
    if status == "running":
        s["started_at"] = _now()
    else:
        s["finished_at"] = _now()
    if rows is not None:
        s["rows"] = rows
    if ms is not None:
        s["ms"] = ms
    if detail:
        s["detail"] = detail


def _fail_running_step(rec, status, detail=None):
    for s in rec["steps"].values():
        if s.get("status") == "running":
            s["status"] = status
            s["finished_at"] = _now()
            if detail:
                s["detail"] = detail


# ---------------------------------------------------------------- background, cancel, state

def start_background(project, name, **kw) -> str:
    from etlplus import stage
    if name not in (project.jobs or {}):
        raise ValueError(f"There is no job named '{name}'.")
    key = _key(project, name)
    run_id = kw.pop("run_id", None) or stage.new_run_id()
    with _lock:
        for rid, a in _active.items():
            if a["key"] == key:
                raise RuntimeError(f"Job '{name}' is already running (run {rid}).")
        cancel = threading.Event()
        _active[run_id] = {"key": key, "job": name, "cancel": cancel, "thread": None}
    flock = JobLock(project, name)
    if not flock.acquire():
        with _lock:
            _active.pop(run_id, None)
        raise RuntimeError(f"Job '{name}' is already running in another process.")
    _active[run_id]["flock"] = flock

    def target():
        try:
            run(project, name, run_id=run_id, **kw)
        except Exception:
            with _lock:
                a = _active.pop(run_id, None)
            if a and a.get("flock"):
                a["flock"].release()

    th = threading.Thread(target=target, name=f"etlplus-{name}", daemon=True)
    _active[run_id]["thread"] = th
    th.start()
    return run_id


def cancel(project, run_id) -> bool:
    with _lock:
        a = _active.get(run_id)
        if not a:
            return False
        a["cancel"].set()
        return True


def running(project=None) -> list[str]:
    with _lock:
        if project is None:
            return list(_active)
        prefix = f"{Path(project.path).resolve()}::"
        return [rid for rid, a in _active.items() if a["key"].startswith(prefix)]


def wait(run_id, timeout=None) -> None:
    th = (_active.get(run_id) or {}).get("thread")
    if th:
        th.join(timeout)


def state(project) -> dict:
    reap(project)
    ids = set(running(project))
    runs_now = [r for r in (store.get(project, rid) for rid in ids) if r]
    last = store.last_runs(project)
    for job in (project.jobs or {}):
        last.setdefault(job, None)
    return {"running": runs_now, "last": last}
