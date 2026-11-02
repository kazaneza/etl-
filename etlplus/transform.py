"""Transform steps compiled to DuckDB SQL over the staged Parquet (view "input")."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

STEP_KINDS = ("rename", "cast", "filter", "derive", "drop", "dedupe", "sql")

_TYPE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*( [A-Za-z][A-Za-z0-9_]*){0,3}"
    r"(\(\s*\d+\s*(,\s*\d+\s*)?\))?(\[\])?$"
)


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _lit(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def sandbox(*paths) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB that can read and write only the given files, nothing else on disk or
    the network. User SQL (filter, derive, order_by, sql steps) runs here, so it cannot reach the
    secrets key, other projects or a URL. The settings are locked so SQL cannot turn them back on."""
    con = duckdb.connect()
    try:
        allowed = ", ".join(_lit(str(p)) for p in paths)
        con.execute(f"SET allowed_paths = [{allowed}]")
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
    except BaseException:
        con.close()
        raise
    return con


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _guard_expr(expr: str, where: str) -> None:
    from etlplus import guard
    if ";" in expr:
        raise ValueError(f"{where}: the expression cannot contain ';'.")
    try:
        guard.check_select(f"SELECT {expr} AS x FROM input", "duckdb")
    except guard.GuardError as e:
        raise ValueError(f"{where}: {e}") from None


EXPR_MAX = 2000
_EXPR_WORDS = {"SELECT", "FROM", "JOIN", "WITH", "UNION", "TABLE", "PRAGMA", "SUMMARIZE", "DESCRIBE", "SHOW"}
_EXPR_FUNCS = {"CURRENT_SETTING", "GETVARIABLE", "DUCKDB_SETTINGS", "DUCKDB_EXTENSIONS", "DUCKDB_SECRETS",
               "GLOB", "GETENV"}


def _expr_call_refused(name: str) -> bool:
    from etlplus import guard
    n = name.upper()
    return (n in guard.FORBIDDEN_FUNCTIONS or n.startswith(guard.FORBIDDEN_PREFIXES) or n in _EXPR_FUNCS
            or n.startswith("READ_") or n.endswith("_SCAN") or n.startswith("PG_") or n.startswith("DUCKDB_"))


def _null_type(t: str) -> str:
    """A DuckDB type for a typed NULL; anything that is not a plain type name becomes VARCHAR."""
    t = str(t or "VARCHAR").strip()
    return t if _TYPE_RE.match(t) or re.match(r"^[A-Za-z][A-Za-z0-9_ ]*\(\d+(,\s*\d+)?\)$", t) else "VARCHAR"


def check_expr(expr, columns=None, where: str = "Expression") -> None:
    """The one check for a mapping expression (SPEC_V2 8.1). Stricter than v1's derive check:
    no subqueries, no table functions, no file or setting access. With columns
    ([{name, type}] or [name]) the expression is described in a fresh sandbox, so an unknown
    column or a type error comes back as the message. Raises ValueError."""
    from etlplus import guard
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError(f"{where}: give an expression.")
    if len(expr) > EXPR_MAX:
        raise ValueError(f"{where}: at most {EXPR_MAX} characters.")
    if ";" in expr or "\x00" in expr:
        raise ValueError(f"{where}: the expression cannot contain ';' or a NUL character.")
    try:
        guard.check_select(f"SELECT {expr} AS x FROM input", "duckdb")
    except guard.GuardError as e:
        raise ValueError(f"{where}: {e}") from None
    for backslash in (False, True):
        try:
            tokens = guard._scan(expr, backslash, brackets=False)
        except guard.GuardError as e:
            raise ValueError(f"{where}: {e}") from None
        items = []
        for kind, text in tokens:
            if kind == "code":
                for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*|\S", text):
                    items.append(m.group(0))
            elif kind == "ident":
                items.append("\x01" + text[1:-1].replace(text[-1] * 2, text[-1]))
            elif kind == "string":
                items.append("\x02")
            # a comment is no token at all: name/**/( is still a call of name
        for i, w in enumerate(items):
            if w[:1].isalpha() or w[:1] == "_":
                if w.upper() in _EXPR_WORDS:
                    raise ValueError(f"{where}: {w.upper()} is not allowed in an expression "
                                     "(no subqueries or tables).")
            if w == "(" and i > 0 and items[i - 1] not in ("\x02",):
                prev = items[i - 1]
                name = prev[1:] if prev.startswith("\x01") else prev
                if (name[:1].isalpha() or name[:1] == "_") and _expr_call_refused(name):
                    raise ValueError(f"{where}: {name}() is not allowed in an expression.")
    if columns is None:
        return
    cols = []
    for c in columns:
        if isinstance(c, dict):
            cols.append((str(c.get("name")), _null_type(c.get("duck") or c.get("type"))))
        else:
            cols.append((str(c), "VARCHAR"))
    nulls = ", ".join(f"CAST(NULL AS {t}) AS {q(n)}" for n, t in cols) or "1 AS __etlplus_none"
    con = sandbox()
    try:
        con.execute(f"DESCRIBE SELECT {expr} AS x FROM (SELECT {nulls}) input").fetchall()
    except duckdb.Error as e:
        msg = str(e).split("\n")[0]
        raise ValueError(f"{where}: {msg}") from None
    finally:
        con.close()


def _names(value, where: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where}: give a list of column names.")
    return value


def _mapping(value, where: str, what: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{where}: give {what}.")
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError(f"{where}: every column name must be text.")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{where}: the value for column '{k}' is empty.")
    return value


def _check_step(i: int, step) -> tuple[str, object]:
    n = i + 1
    if not isinstance(step, dict) or len(step) != 1:
        raise ValueError(f"Step {n}: each step has exactly one kind, one of {', '.join(STEP_KINDS)}.")
    kind, value = next(iter(step.items()))
    where = f"Step {n} ({kind})"
    if kind not in STEP_KINDS:
        raise ValueError(f"Step {n}: '{kind}' is not a step kind. Use one of {', '.join(STEP_KINDS)}.")
    if kind == "rename":
        m = _mapping(value, where, "a mapping of old name to new name")
        if len(set(m.values())) != len(m):
            raise ValueError(f"{where}: two columns cannot get the same new name.")
    elif kind == "cast":
        m = _mapping(value, where, "a mapping of column to type, for example {amount: DECIMAL(18,2)}")
        for col, typ in m.items():
            if not _TYPE_RE.match(typ.strip()):
                raise ValueError(f"{where}: '{typ}' is not a type name (column '{col}').")
    elif kind == "filter":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}: give a condition, for example \"segment IS NOT NULL\".")
        _guard_expr(value, where)
    elif kind == "derive":
        m = _mapping(value, where, "a mapping of new column to expression")
        for col, expr in m.items():
            _guard_expr(expr, f"{where}, column '{col}'")
    elif kind == "drop":
        _names(value, where)
    elif kind == "dedupe":
        if isinstance(value, (list, str)):
            value = {"key": value}
        if not isinstance(value, dict):
            raise ValueError(f"{where}: give a key list and an optional order_by.")
        extra = set(value) - {"key", "order_by"}
        if extra:
            raise ValueError(f"{where}: unknown setting {', '.join(sorted(extra))}. Use key and order_by.")
        if value.get("key") is not None:
            _names(value["key"], where + " key")
        ob = value.get("order_by")
        if ob is not None:
            if not isinstance(ob, str) or not ob.strip():
                raise ValueError(f"{where}: order_by must be text, for example \"updated_at DESC\".")
            _guard_expr(f"row_number() OVER (ORDER BY {ob})", where + " order_by")
    elif kind == "sql":
        from etlplus import guard
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}: give one SELECT over the view input.")
        try:
            guard.check_select(value, "duckdb")
        except guard.GuardError as e:
            raise ValueError(f"{where}: {e}") from None
    return kind, value


def validate(steps: list[dict]) -> None:
    if steps is None:
        return
    if not isinstance(steps, list):
        raise ValueError("Transform must be a list of steps.")
    for i, step in enumerate(steps):
        _check_step(i, step)


_ORDER_PART = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(ASC|DESC)?\s*(NULLS\s+(FIRST|LAST))?\s*$", re.I)


def order_by(text: str) -> str:
    """Quote plain column names ("at DESC" -> "\"at\" DESC"); anything else is used as written."""
    parts = text.split(",")
    out = []
    for part in parts:
        m = _ORDER_PART.match(part)
        if not m:
            return text
        out.append(" ".join(x for x in (q(m.group(1)), (m.group(2) or "").upper(),
                                          (m.group(3) or "").upper()) if x))
    return ", ".join(out)


def _step_sql(kind, value, prev: str, columns: list[str]) -> str:
    if kind == "rename":
        missing = [c for c in value if c not in columns]
        if missing:
            raise ValueError(f"there is no column {', '.join(repr(c) for c in missing)}.")
        parts = ", ".join(f"{q(a)} AS {q(b)}" for a, b in value.items())
        return f"SELECT * RENAME ({parts}) FROM {prev}"
    if kind == "cast":
        missing = [c for c in value if c not in columns]
        if missing:
            raise ValueError(f"there is no column {', '.join(repr(c) for c in missing)}.")
        parts = ", ".join(f"CAST({q(c)} AS {t.strip()}) AS {q(c)}" for c, t in value.items())
        return f"SELECT * REPLACE ({parts}) FROM {prev}"
    if kind == "filter":
        return f"SELECT * FROM {prev} WHERE ({value})"
    if kind == "derive":
        repl = [f"({e}) AS {q(c)}" for c, e in value.items() if c in columns]
        new = [f"({e}) AS {q(c)}" for c, e in value.items() if c not in columns]
        sel = "*" + (f" REPLACE ({', '.join(repl)})" if repl else "")
        if new:
            sel += ", " + ", ".join(new)
        return f"SELECT {sel} FROM {prev}"
    if kind == "drop":
        cols = _names(value, "drop")
        missing = [c for c in cols if c not in columns]
        if missing:
            raise ValueError(f"there is no column {', '.join(repr(c) for c in missing)}.")
        if len(set(cols)) >= len(columns):
            raise ValueError("this would drop every column.")
        return f"SELECT * EXCLUDE ({', '.join(q(c) for c in cols)}) FROM {prev}"
    if kind == "dedupe":
        if isinstance(value, (list, str)):
            value = {"key": value}
        key = value.get("key")
        if not key:
            return f"SELECT DISTINCT * FROM {prev}"
        key = _names(key, "dedupe")
        missing = [c for c in key if c not in columns]
        if missing:
            raise ValueError(f"there is no column {', '.join(repr(c) for c in missing)}.")
        order = f" ORDER BY {order_by(value['order_by'])}" if value.get("order_by") else ""
        return (f"SELECT * FROM {prev} QUALIFY row_number() OVER "
                f"(PARTITION BY {', '.join(q(c) for c in key)}{order}) = 1")
    if kind == "sql":
        from etlplus import guard
        body = guard.check_select(value, "duckdb").strip().rstrip(";").strip()
        head = f"input AS (SELECT * FROM {prev})"
        m = re.match(r"(?is)^with\s+(recursive\s+)?", body)
        if m:
            return f"WITH {m.group(1) or ''}{head}, {body[m.end():]}"
        return f"WITH {head} {body}"
    raise ValueError(f"'{kind}' is not a step kind.")


def build(con: duckdb.DuckDBPyConnection, source: str, steps: list[dict]) -> tuple[str, list[str]]:
    """Create view input over the source parquet and one view per step. Returns the last view and the SQL."""
    validate(steps)
    con.execute(f"CREATE OR REPLACE TEMP VIEW input AS SELECT * FROM read_parquet({_lit(source)})")
    prev, sqls = "input", []
    for i, step in enumerate(steps or []):
        kind, value = _check_step(i, step)
        columns = [r[0] for r in con.execute(f"DESCRIBE {prev}").fetchall()]
        try:
            sql = _step_sql(kind, value, prev, columns)
            view = f"s{i + 1}"
            con.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS {sql}")
            con.execute(f"DESCRIBE {view}").fetchall()
        except ValueError as e:
            raise ValueError(f"Step {i + 1} ({kind}): {e}") from None
        except duckdb.Error as e:
            msg = str(e).split("\n")[0]
            raise ValueError(f"Step {i + 1} ({kind}): {msg}") from None
        sqls.append(sql)
        prev = view
    return prev, sqls


def compile_sql(steps: list[dict]) -> str:
    """One readable SQL text for the steps (for display). Column checks happen at run time."""
    validate(steps)
    parts, prev = [], "input"
    for i, step in enumerate(steps or []):
        kind, value = _check_step(i, step)
        if kind == "derive":
            sql = "SELECT *, " + ", ".join(f"({e}) AS {q(c)}" for c, e in value.items()) + f" FROM {prev}"
        else:
            sql = _step_sql(kind, value, prev, _AnyColumns())
        parts.append(f"s{i + 1} AS ({sql})")
        prev = f"s{i + 1}"
    if not parts:
        return "SELECT * FROM input"
    return "WITH " + ",\n     ".join(parts) + f"\nSELECT * FROM {prev}"


class _AnyColumns(list):
    def __contains__(self, item):
        return True

    def __len__(self):
        return 1 << 30


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_dir(project, job_name, run_id) -> Path:
    from etlplus import stage
    p = Path(stage.path(project, job_name, run_id, "extract"))
    return p if p.is_dir() or not p.suffix else p.parent


def manifest_path(run_dir: Path, kind: str) -> Path:
    """manifest.json belongs to the extract; the transform writes transformed.manifest.json."""
    return run_dir / ("manifest.json" if kind == "extract" else f"{kind}.manifest.json")


def read_manifest(run_dir: Path, kind: str) -> dict | None:
    p = manifest_path(run_dir, kind)
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    if kind == "extract" and m.get("kind", "extract") != "extract":
        return None
    f = run_dir / m.get("file", f"{kind}.parquet")
    return m if f.exists() else None


def _log(log, msg):
    if log:
        log(msg)


def _check(log):
    fn = getattr(log, "check", None)
    if fn:
        fn()


def apply(project, job, run_id, log) -> dict:
    name = job["name"]
    run_dir = _run_dir(project, name, run_id)
    src_manifest = read_manifest(run_dir, "extract")
    if src_manifest is None:
        raise ValueError(f"There is no complete staged extract for job '{name}' run {run_id}.")
    src = run_dir / src_manifest.get("file", "extract.parquet")
    out = run_dir / "transformed.parquet"
    partial = run_dir / "transformed.parquet.partial"
    steps = job.get("transform") or []
    started = _now()
    _log(log, f"transform: {len(steps)} step(s) over {src.name}")
    mp = manifest_path(run_dir, "transformed")
    # a re-run first removes the previous result, so the new manifest never describes the old file
    for stale in (mp, out, partial):
        if stale.exists():
            stale.unlink()
    con = sandbox(src, partial)
    try:
        last, sqls = build(con, str(src), steps)
        for i, s in enumerate(sqls):
            _log(log, f"  step {i + 1}: {s}")
        _check(log)
        con.execute(f"COPY (SELECT * FROM {last}) TO {_lit(partial)} (FORMAT parquet, COMPRESSION zstd)")
        cols = [{"name": r[0], "type": r[1]} for r in con.execute(f"DESCRIBE {last}").fetchall()]
        rows = con.execute(f"SELECT count(*) FROM read_parquet({_lit(partial)})").fetchone()[0]
    except BaseException:
        con.close()
        if partial.exists():
            partial.unlink()
        raise
    finally:
        con.close()
    tmp = mp.with_name(mp.name + ".partial")
    try:
        manifest = {
            "job": name,
            "run_id": run_id,
            "kind": "transformed",
            "rows": int(rows),
            "rows_in": src_manifest.get("rows"),
            "columns": cols,
            "window": src_manifest.get("window"),
            "source": f"transform of {src.name} ({len(steps)} step(s))",
            "file": out.name,
            "bytes": partial.stat().st_size,
            "sha256": _sha256(partial),
            "steps": steps,
            "started_at": started,
            "finished_at": _now(),
        }
        tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, mp)
        os.replace(partial, out)
    except BaseException:
        # nothing half done stays: no .partial, and no manifest without its file
        for f in (tmp, partial):
            if f.exists():
                f.unlink()
        if mp.exists() and not out.exists():
            mp.unlink()
        raise
    _log(log, f"transform: {rows} rows written to {out.name}")
    return manifest


def jsonable(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, (Decimal, timedelta)):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    return str(v)


def preview(project, job: dict, limit=50) -> dict:
    from etlplus import stage
    name = job.get("name")
    m = stage.latest(project, name, "extract") if name else None
    if not m:
        raise ValueError(f"Nothing is staged for job '{name}' yet. Run the extract step first.")
    run_dir = _run_dir(project, name, m["run_id"])
    src = run_dir / m.get("file", "extract.parquet")
    con = sandbox(src)
    try:
        last, sqls = build(con, str(src), job.get("transform") or [])
        cur = con.execute(f"SELECT * FROM {last} LIMIT {int(limit)}")
        rows = [[jsonable(v) for v in r] for r in cur.fetchall()]
        cols = [{"name": r[0], "type": r[1]} for r in con.execute(f"DESCRIBE {last}").fetchall()]
        rows_out = con.execute(f"SELECT count(*) FROM {last}").fetchone()[0]
    finally:
        con.close()
    return {"columns": cols, "rows": rows, "rows_in": m.get("rows"), "rows_out": int(rows_out),
            "run_id": m["run_id"], "sql": sqls}
