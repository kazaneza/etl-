"""Destinations: where a job's rows land, defined once and used by many jobs (SPEC_V2 7).

destinations.yaml holds no secrets. A database destination names a connection (role target or
both) and a table, with a column design that etl+ creates (design: create) or reads from the
database (design: existing). A file destination names a folder, shared folder or remote
location, a sub-folder, a file name pattern and a format.
"""
from __future__ import annotations

import builtins
import re
from pathlib import Path

from etlplus import ddl

DESIGNS = ("create", "existing")
FILE_FORMATS = ("parquet", "csv", "jsonl")
_TOKEN_RE = re.compile(r"\{(date|run_id|job|format)(?::([%A-Za-z_\-./ ]{1,40}))?\}")
_list = builtins.list


class DestinationError(ValueError):
    pass


class InUse(DestinationError):
    def __init__(self, did, jobs):
        self.jobs = _list(jobs)
        super().__init__(f"Destination '{did}' is used by {', '.join(self.jobs)}. Change those jobs first.")


class Busy(RuntimeError):
    pass


def _spec(project, did) -> dict:
    spec = (project.destinations or {}).get(did)
    if spec is None:
        raise KeyError(f"There is no destination named '{did}'.")
    return dict(spec)


def _conn(project, spec) -> dict:
    return dict((project.connections or {}).get(spec.get("connection")) or {})


def is_file(project, spec) -> bool:
    kind = _conn(project, spec).get("kind")
    if kind:
        try:
            return ddl.is_file_kind(kind)
        except Exception:
            return False
    return bool(spec.get("file") or spec.get("path")) and not spec.get("table")


def jobs_using(project, did) -> list[str]:
    return sorted(n for n, j in (project.jobs or {}).items()
                  if ((j or {}).get("target") or {}).get("destination") == did)


# ---------------------------------------------------------------- paths and names (section 4.6)

def check_rel_path(value, what="path") -> str:
    """A relative POSIX path: no absolute path, drive, UNC, backslash, '..', NUL, control
    character or empty part. Returns it unchanged."""
    s = str(value or "")
    if not s:
        raise DestinationError(f"The {what} is empty.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        raise DestinationError(f"The {what} holds a control character.")
    if "\\" in s:
        raise DestinationError(f"The {what} uses '\\'; use '/' between folders.")
    if s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        raise DestinationError(f"The {what} must be relative to the location, not absolute.")
    parts = s.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise DestinationError(f"The {what} has an empty, '.' or '..' part.")
    return s


def fill_tokens(template: str, *, job: str = "", run_id: str = "", fmt: str = "", today=None) -> str:
    """Fill {date}, {date:%Y%m%d}, {run_id}, {job} and {format}. Never str.format."""
    import datetime as dt
    today = today or dt.date.today()

    def rep(m):
        tok, f = m.group(1), m.group(2)
        if tok == "date":
            if f:
                if not re.fullmatch(r"(%[A-Za-z]|[-_./ ])+", f):
                    raise DestinationError("A date format holds only strftime letters, such as %Y%m%d.")
                return today.strftime(f)
            return today.isoformat()
        if f:
            raise DestinationError(f"{{{tok}}} takes no format.")
        return {"run_id": run_id, "job": job, "format": fmt}[tok]
    out = _TOKEN_RE.sub(rep, template)
    if "{" in out or "}" in out:
        raise DestinationError("The file name has an unknown {token}; use {date}, {run_id}, {job} or {format}.")
    return out


# ---------------------------------------------------------------- save, list, get

def problems(project, did, spec) -> list[str]:
    p = []
    try:
        from etlplus.config import check_name
        check_name(did, "destination id")
    except ValueError as e:
        p.append(str(e))
    if not isinstance(spec, dict):
        return p + ["A destination is a mapping of settings."]
    cid = spec.get("connection")
    conn = (project.connections or {}).get(cid) if cid else None
    if not cid:
        p.append("The destination needs a connection.")
    elif conn is None:
        p.append(f"The connection '{cid}' does not exist.")
    elif (conn.get("role") or "source") == "source":
        p.append(f"Connection '{cid}' is a source (read only); set its role to target or both.")
    kind = (conn or {}).get("kind")
    file_dest = bool(kind) and ddl.is_file_kind(kind)
    cols = spec.get("columns")
    keys = spec.get("keys") or []
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, _list) or not all(isinstance(k, str) and k for k in keys):
        p.append("keys must be a list of column names.")
        keys = []
    if file_dest:
        fmt = spec.get("format") or "parquet"
        if fmt not in FILE_FORMATS:
            p.append(f"A file destination writes {', '.join(FILE_FORMATS)}.")
        try:
            if spec.get("path"):
                check_rel_path(spec["path"], "path")
            name = spec.get("file") or ("{job}.{format}")
            filled = fill_tokens(str(name), job="job", run_id="20260101T000000Z-0000", fmt=fmt)
            check_rel_path(filled, "file name")
            if "/" in filled:
                p.append("The file name is a name, not a path; put folders in path.")
        except DestinationError as e:
            p.append(str(e))
        if spec.get("table"):
            p.append("A file destination has a path and a file name, not a table.")
        if cols is not None:
            if not isinstance(cols, _list):
                p.append("columns must be a list.")
            else:
                p += [_problem_text(x) for x in ddl.check("duckdb", cols, keys) if x["level"] == "error"]
    elif kind:
        if not spec.get("table"):
            p.append("A database destination needs a table.")
        else:
            try:
                ddl.quote_table(kind, spec["table"])
            except Exception as e:
                p.append(str(e))
        design = spec.get("design") or "create"
        if design not in DESIGNS:
            p.append("design is create (etl+ creates the table) or existing (the table is there already).")
        if design == "create":
            if not isinstance(cols, _list) or not cols:
                p.append("design create needs columns.")
            else:
                p += [_problem_text(x) for x in ddl.check(kind, cols, keys) if x["level"] == "error"]
        elif cols is not None and not isinstance(cols, _list):
            p.append("columns must be a list.")
    return p


def _problem_text(x) -> str:
    return (f"Column '{x['column']}': " if x.get("column") else "") + x["message"]


def save(project, did, spec) -> list[str]:
    """Validate, then write destinations.yaml atomically. Returns problems ([] when saved)."""
    spec = dict(spec or {})
    for k in ("id", "exists", "jobs", "kind", "columns_count", "rename_from"):
        spec.pop(k, None)
    if spec.get("keys") and isinstance(spec["keys"], str):
        spec["keys"] = [spec["keys"]]
    probs = problems(project, did, spec)
    if probs:
        return probs
    clean = {}
    order = ("connection", "table", "design", "path", "file", "format", "columns", "keys", "description")
    for k in order:
        if k in spec and spec[k] not in (None, ""):
            clean[k] = spec[k]
    for k, v in spec.items():
        if k not in clean and v not in (None, ""):
            clean[k] = v
    if isinstance(clean.get("columns"), _list):
        clean["columns"] = [_clean_col(c) for c in clean["columns"]]
    project.save_destination(did, clean)
    return []


def _clean_col(c: dict) -> dict:
    out = {"name": c["name"], "type": str(c.get("type") or "text")}
    if c.get("nullable") is False:
        out["nullable"] = False
    if c.get("default") not in (None, ""):
        out["default"] = c["default"]
    if c.get("personal"):
        out["personal"] = True
    if c.get("description"):
        out["description"] = c["description"]
    return out


def rename(project, old, new, spec) -> list[str]:
    """Save under a new id and point every job that used the old id at it."""
    probs = save(project, new, spec)
    if probs:
        return probs
    if old != new and old in (project.destinations or {}):
        for name in jobs_using(project, old):
            job = dict(project.jobs[name])
            t = dict(job.get("target") or {})
            t["destination"] = new
            job["target"] = t
            project.save_job(job)
        project.delete_destination(old)
    return []


def delete(project, did) -> None:
    if did not in (project.destinations or {}):
        raise KeyError(f"There is no destination named '{did}'.")
    users = jobs_using(project, did)
    if users:
        raise InUse(did, users)
    project.delete_destination(did)


def list(project) -> list[dict]:  # noqa: A001 (the contract names it list)
    out = []
    for did in sorted(project.destinations or {}):
        out.append(summary(project, did))
    return out


def summary(project, did) -> dict:
    spec = _spec(project, did)
    conn = _conn(project, spec)
    try:
        ex = exists(project, did)
    except Exception:
        ex = None
    return {"id": did, "connection": spec.get("connection"), "kind": conn.get("kind"),
            "table": spec.get("table"), "path": spec.get("path"), "file": spec.get("file"),
            "format": spec.get("format") if is_file(project, spec) else None,
            "design": spec.get("design") or (None if is_file(project, spec) else "create"),
            "columns_count": len(spec.get("columns") or []), "keys": spec.get("keys") or [],
            "exists": ex, "jobs": jobs_using(project, did), "description": spec.get("description")}


def get(project, did) -> dict:
    spec = _spec(project, did)
    out = dict(spec)
    out["id"] = did
    out["kind"] = _conn(project, spec).get("kind")
    out["jobs"] = jobs_using(project, did)
    try:
        out["exists"] = exists(project, did)
    except Exception:
        out["exists"] = None
    try:
        out["columns"] = columns(project, did)
    except Exception:
        out["columns"] = spec.get("columns") or []
    return out


# ---------------------------------------------------------------- resolve to a v1 target

def resolve(project, did, job_name: str = "", run_id: str = "") -> dict:
    """The v1 target spec {connection, table, format?, key, columns, create} for a destination."""
    spec = _spec(project, did)
    keys = spec.get("keys") or []
    keys = [keys] if isinstance(keys, str) else _list(keys)
    out = {"connection": spec.get("connection"), "columns": spec.get("columns") or [], "key": keys,
           "destination": did}
    if is_file(project, spec):
        fmt = spec.get("format") or "parquet"
        name = fill_tokens(str(spec.get("file") or "{job}.{format}"), job=job_name or did,
                           run_id=run_id or "run", fmt=fmt)
        out["table"] = f"{spec['path'].rstrip('/')}/{name}" if spec.get("path") else name
        out["format"] = fmt
    else:
        out["table"] = spec.get("table")
        out["design"] = spec.get("design") or "create"
    return out


# ---------------------------------------------------------------- the database side

def _cursor_rows(cur, sql, params):
    if params is None:
        cur.execute(sql)
    else:
        cur.execute(sql, params)
    return cur.fetchall()


def _db_open(project, spec, read_only=False):
    """A connection for checks and previews. DuckDB and SQLite are opened read-only when asked,
    and a DuckDB file that a load is writing gives Busy."""
    from etlplus import targets
    cid = spec.get("connection")
    conn = _conn(project, spec)
    kind = conn.get("kind")
    if read_only and kind in ("duckdb", "sqlite"):
        path = targets.file_db_path(project, conn)
        if not path.exists():
            return None, kind
        if kind == "duckdb":
            import duckdb
            try:
                c = duckdb.connect(str(path), read_only=True)
            except duckdb.Error as e:
                low = str(e).lower()
                if any(w in low for w in ("lock", "conflicting", "used by another", "different configuration")):
                    raise Busy("A load is writing to it; try again in a moment.") from None
                raise
            c.execute("SET enable_external_access = false")
            return c, kind
        import sqlite3
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10), kind
    return targets.connect(project, cid, conn), kind


class _peek:
    """Hold a file database's load lock while reading it: Busy when a load holds it now, and a
    load that starts meanwhile waits for the read to end."""

    def __init__(self, project, spec):
        self.project, self.conn = project, _conn(project, spec)
        self.lk = None

    def __enter__(self):
        from etlplus import targets
        if self.conn.get("kind") not in ("duckdb", "sqlite"):
            return self
        lk = targets.file_lock(self.project, self.conn, wait_s=0)
        try:
            lk.__enter__()
        except Exception:
            raise Busy("A load is writing to it; try again in a moment.") from None
        self.lk = lk
        return self

    def __exit__(self, *exc):
        if self.lk is not None:
            self.lk.__exit__(*exc)
        return False


def _table_exists(project, spec) -> bool:
    with _peek(project, spec):
        return _table_exists_now(project, spec)


def _table_exists_now(project, spec) -> bool:
    con, kind = _db_open(project, spec, read_only=True)
    if con is None:
        return False
    try:
        cur = con if kind == "duckdb" else con.cursor()
        sql, params = ddl.exists_sql(kind, spec["table"])
        return bool(_cursor_rows(cur, sql, params))
    finally:
        try:
            con.close()
        except Exception:
            pass


def _latest_file(project, spec) -> Path | None:
    conn = _conn(project, spec)
    if conn.get("kind") not in ("folder", "share"):
        return None
    root = Path(project.resolve(conn.get("path") or "."))
    folder = root / spec["path"] if spec.get("path") else root
    if not folder.is_dir():
        return None
    fmt = spec.get("format") or "parquet"
    name = str(spec.get("file") or "{job}.{format}")
    pattern = _TOKEN_RE.sub(lambda m: fmt if m.group(1) == "format" else "*", name)
    files = [p for p in folder.glob(pattern) if p.is_file() and not p.name.endswith(".partial")]
    sub = [p for p in folder.glob(pattern.rsplit(".", 1)[0] + "/part-*." + fmt)] if "." in pattern else []
    files += [p for p in sub if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def exists(project, did) -> bool:
    spec = _spec(project, did)
    if is_file(project, spec):
        conn = _conn(project, spec)
        if conn.get("kind") not in ("folder", "share"):
            return False
        return _latest_file(project, spec) is not None
    return _table_exists(project, spec)


def _live_columns(project, spec) -> list[dict]:
    with _peek(project, spec):
        return _live_columns_now(project, spec)


def _live_columns_now(project, spec) -> list[dict]:
    con, kind = _db_open(project, spec, read_only=True)
    if con is None:
        return []
    try:
        cur = con if kind == "duckdb" else con.cursor()
        sql, params = ddl.exists_sql(kind, spec["table"])
        if not _cursor_rows(cur, sql, params):
            return []
        cols = []
        if kind == "duckdb":
            for r in con.execute(f"DESCRIBE SELECT * FROM {ddl.quote_table(kind, spec['table'])}").fetchall():
                cols.append({"name": r[0], "type": ddl.duck_to_generic(r[1]), "native": r[1],
                             "nullable": str(r[2]).upper() != "NO"})
            return cols
        if kind == "sqlite":
            parts = ddl.split_table(spec["table"])
            for r in cur.execute(f"PRAGMA table_info({ddl.quote_ident(kind, parts[-1])})").fetchall():
                cols.append({"name": r[1], "type": ddl.generic_type(kind, r[2] or "TEXT"), "native": r[2],
                             "nullable": not r[3] and not r[5], "default": r[4]})
            return cols
        from etlplus import connections
        csql, cparams = connections.columns_sql(kind, spec["table"])
        for r in _cursor_rows(cur, csql, cparams):
            cols.append({"name": r[0], "type": ddl.generic_type(kind, str(r[1] or "")), "native": str(r[1] or ""),
                         "nullable": bool(r[2])})
        return cols
    finally:
        try:
            con.close()
        except Exception:
            pass


def columns(project, did) -> list[dict]:
    """The design, or what the database has now (existing design; falls back to the cache)."""
    spec = _spec(project, did)
    if is_file(project, spec) or (spec.get("design") or "create") == "create":
        return [dict(c) for c in spec.get("columns") or []]
    try:
        live = _live_columns(project, spec)
    except Busy:
        live = []
    if live:
        cache = {str(c.get("name")).lower(): c for c in spec.get("columns") or []}
        for c in live:
            extra = cache.get(str(c["name"]).lower()) or {}
            if extra.get("personal"):
                c["personal"] = True
        return live
    return [dict(c) for c in spec.get("columns") or []]


def ddl_info(project, did, spec=None) -> dict:
    spec = dict(spec) if spec is not None else _spec(project, did)
    conn = _conn(project, spec)
    kind = conn.get("kind")
    if not kind:
        raise DestinationError(f"The connection '{spec.get('connection')}' does not exist.")
    if ddl.is_file_kind(kind):
        return {"sql": None, "dialect": kind, "exists": None, "checks": [
            {"level": "ok", "column": None, "message": "A file destination needs no table."}]}
    checks = ddl.check(kind, spec.get("columns") or [], spec.get("keys") or [])
    sql = None
    try:
        sql = ddl.create_table_sql(kind, spec["table"], spec.get("columns") or [], spec.get("keys") or [])
    except Exception as e:
        if not any(c["level"] == "error" for c in checks):
            checks.append({"level": "error", "column": None, "message": str(e)})
    try:
        ex = _table_exists(project, spec)
    except Exception:
        ex = None
    return {"sql": sql, "dialect": kind, "exists": ex, "checks": checks}


def create(project, did) -> dict:
    """Create the table when it is missing. {"created": bool, "sql"}."""
    spec = _spec(project, did)
    if is_file(project, spec):
        return {"created": False, "sql": None}
    conn = _conn(project, spec)
    kind = conn.get("kind")
    sql = ddl.create_table_sql(kind, spec["table"], spec.get("columns") or [], spec.get("keys") or [])
    from etlplus import targets
    with targets.file_lock(project, conn):
        con = targets.connect(project, spec["connection"], conn)
        try:
            cur = con if kind == "duckdb" else con.cursor()
            esql, eparams = ddl.exists_sql(kind, spec["table"])
            if _cursor_rows(cur, esql, eparams):
                return {"created": False, "sql": sql}
            parts = ddl.split_table(spec["table"])
            if kind == "duckdb" and len(parts) == 2 and parts[0].lower() != "main":
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ddl.quote_ident(kind, parts[0])}")
            cur.execute(sql)
            if kind not in ("duckdb",):
                try:
                    con.commit()
                except Exception:
                    pass
        finally:
            try:
                con.close()
            except Exception:
                pass
    return {"created": True, "sql": sql}


def ensure(project, did, log=None) -> bool:
    """Before a load: create a design-create table that is missing. True when created."""
    spec = _spec(project, did)
    if is_file(project, spec) or (spec.get("design") or "create") != "create":
        return False
    res = create(project, did)
    if res["created"] and log:
        log(f"destination {did}: created\n{res['sql']}")
    return res["created"]


def test(project, did, spec=None) -> dict:
    """{"ok", "checks": [{"level","message"}]} for a saved or unsaved destination."""
    spec = dict(spec) if spec is not None else _spec(project, did)
    checks = []
    for p in problems(project, did, spec):
        checks.append({"level": "error", "message": p})
    if checks:
        return {"ok": False, "checks": checks}
    conn = _conn(project, spec)
    kind = conn.get("kind")
    if ddl.is_file_kind(kind):
        if kind in ("folder", "share"):
            root = Path(project.resolve(conn.get("path") or "."))
            checks.append({"level": "ok" if root.exists() else "warn",
                           "message": "The folder is there." if root.exists() else "The folder is created on the first load."})
        else:
            try:
                from etlplus import remote
                for s in remote.test(project, spec["connection"]) or []:
                    checks.append({"level": "ok" if s.get("ok") else "error",
                                   "message": f"{s.get('name')}: {s.get('detail') or ''}".strip()})
            except Exception as e:
                checks.append({"level": "error", "message": f"The location could not be checked: {e}"})
        return {"ok": not any(c["level"] == "error" for c in checks), "checks": checks}
    try:
        ex = _table_exists(project, spec) if kind in ("duckdb", "sqlite") else None
        if ex is None:
            con = __import__("etlplus.targets", fromlist=["connect"]).connect(project, spec["connection"], conn)
            try:
                cur = con.cursor()
                sql, params = ddl.exists_sql(kind, spec["table"])
                ex = bool(_cursor_rows(cur, sql, params))
            finally:
                con.close()
        checks.append({"level": "ok", "message": "Connected."})
    except Busy as e:
        return {"ok": False, "checks": [{"level": "warn", "message": str(e)}]}
    except Exception as e:
        return {"ok": False, "checks": [{"level": "error", "message": f"Could not connect: {e}"}]}
    design = spec.get("design") or "create"
    if ex:
        checks.append({"level": "ok", "message": f"Table {spec['table']} exists."})
        if design == "create" and spec.get("columns"):
            live = {c["name"].lower() for c in _live_columns(project, spec)}
            missing = [c["name"] for c in spec["columns"] if c["name"].lower() not in live]
            if missing:
                checks.append({"level": "error", "message": f"The table has no column {', '.join(missing)}."})
    elif design == "create":
        checks.append({"level": "warn", "message": f"Table {spec['table']} does not exist yet; Create table makes it."})
    else:
        checks.append({"level": "error", "message": f"Table {spec['table']} does not exist."})
    for c in ddl.check(kind, spec.get("columns") or [], spec.get("keys") or []) if design == "create" else []:
        if c["level"] == "warn":
            checks.append({"level": "warn", "message": _problem_text(c)})
    return {"ok": not any(c["level"] == "error" for c in checks), "checks": checks}


def preview(project, did, limit=50, offset=0) -> dict:
    """Rows now in the destination: a guarded SELECT, or the latest file. {columns, rows, total}."""
    from etlplus import guard, transform
    spec = _spec(project, did)
    limit = max(1, min(int(limit or 50), 500))
    offset = max(int(offset or 0), 0)
    if is_file(project, spec):
        f = _latest_file(project, spec)
        if f is None:
            return {"columns": [], "rows": [], "total": 0}
        fmt = spec.get("format") or "parquet"
        reader = {"parquet": "read_parquet", "csv": "read_csv_auto", "jsonl": "read_json_auto"}[fmt]
        con = transform.sandbox(f)
        try:
            src = f"{reader}({transform._lit(f)})"
            total = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
            cur = con.execute(f"SELECT * FROM {src} LIMIT {limit} OFFSET {offset}")
            cols = [d[0] for d in cur.description]
            rows = [[transform.jsonable(v) for v in r] for r in cur.fetchall()]
        finally:
            con.close()
        return {"columns": cols, "rows": rows, "total": int(total), "file": f.name}
    with _peek(project, spec):
        return _db_preview(project, spec, limit, offset)


def _db_preview(project, spec, limit, offset) -> dict:
    from etlplus import guard, transform
    con, kind = _db_open(project, spec, read_only=True)
    if con is None:
        return {"columns": [], "rows": [], "total": 0}
    try:
        cur = con if kind == "duckdb" else con.cursor()
        sql, params = ddl.exists_sql(kind, spec["table"])
        if not _cursor_rows(cur, sql, params):
            return {"columns": [], "rows": [], "total": 0}
        tq = ddl.quote_table(kind, spec["table"])
        count_sql = guard.check_select(f"SELECT COUNT(*) FROM {tq}", kind)
        total = _cursor_rows(cur, count_sql, None)[0][0]
        fam = ddl.family(kind)
        if fam == "sqlserver":
            q = f"SELECT * FROM {tq} ORDER BY (SELECT NULL) OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY"
        elif fam in ("oracle", "db2"):
            q = f"SELECT * FROM {tq} OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY"
        else:
            q = f"SELECT * FROM {tq} LIMIT {limit} OFFSET {offset}"
        q = guard.check_select(q, kind)
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        rows = [[transform.jsonable(v) for v in r] for r in cur.fetchall()]
        return {"columns": cols, "rows": rows, "total": int(total)}
    finally:
        try:
            con.close()
        except Exception:
            pass


# ---------------------------------------------------------------- design from a source

def design_from_source(project, source: dict, target_kind: str = "duckdb") -> list[dict]:
    """Proposed columns with generic types from a sample of the source."""
    from etlplus import mapping
    table = mapping.sample_source(project, dict(source or {}), mapping.SAMPLE_ROWS)
    personal = mapping._personal_source_columns(project, {"source": source})
    out = []
    for f in table.schema:
        gen = ddl.arrow_to_generic(f.type)
        if gen == "text":
            vals = [v for v in table.column(f.name).to_pylist() if v is not None]
            ml = max((len(str(v)) for v in vals), default=0)
            if ml:
                n = 10
                while n < ml * 2 and n < 4000:
                    n *= 2
                gen = f"string({min(n, 4000)})"
        out.append({"name": f.name, "type": gen, "nullable": True, "personal": f.name in personal})
    return out
