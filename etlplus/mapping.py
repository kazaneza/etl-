"""Column mapping: destination column <- a source column, a constant or an expression (SPEC_V2 8).

The map step compiles one SELECT and runs it in the transform sandbox (external access off,
configuration locked) over the staged Parquet, writing mapped.parquet.partial, then the
manifest, then the rename. Expressions pass transform.check_expr; constants are rendered by
literal(); every destination name goes through transform.q.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from etlplus import ddl, transform

HOW = ("from", "const", "expr")
UNMAPPED = ("default", "error")
ON_CAST_ERROR = ("error", "null")
SAMPLE_ROWS = 200
STR_MAX = 4000

# small synonym groups for auto-match: every name in a group matches every other
ALIASES = [
    {"acct", "account", "accountnumber", "accountno", "acctno", "acctnumber", "accountid"},
    {"amt", "amount", "value"},
    {"ccy", "currency", "cur", "currencycode"},
    {"dt", "date"},
    {"ref", "reference"},
    {"cust", "customer", "customerid", "customernumber", "custno", "customerno"},
    {"desc", "description", "narrative"},
    {"id", "identifier"},
    {"qty", "quantity"},
    {"txn", "transaction", "trx", "tran"},
    {"txnid", "transactionid", "transactionreference", "trxid"},
    {"valuedate", "valdate", "valuedt"},
    {"bookingdate", "bookdate", "postingdate"},
]

_previews: set = set()
_previews_lock = threading.Lock()


class MappingError(ValueError):
    pass


class PreviewBusy(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- literals

def literal(value, generic_type: str = "text") -> str:
    """A constant as SQL text: None -> NULL; bool -> TRUE/FALSE; int -> digits; float -> repr
    (refused when not finite); str -> quoted with ' doubled, then CAST to the type."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MappingError("A constant number must be finite (not NaN or infinity).")
        return repr(value)
    if isinstance(value, str):
        if len(value) > STR_MAX:
            raise MappingError(f"A constant text is at most {STR_MAX} characters.")
        if "\x00" in value:
            raise MappingError("A constant text cannot hold a NUL character.")
        duck = ddl.generic_to_duck(generic_type or "text")
        return f"CAST('{value.replace(chr(39), chr(39) * 2)}' AS {duck})"
    raise MappingError("A constant is one value: text, a number, true, false or null (not a list or a mapping).")


# ---------------------------------------------------------------- names and matching

def normalise(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _alias_of(n: str) -> set:
    out = set()
    for g in ALIASES:
        if n in g:
            out |= g
    return out


def _names(cols) -> list[str]:
    return [c["name"] if isinstance(c, dict) else str(c) for c in cols or []]


def suggest(source_cols, dest_cols) -> dict:
    """{dest: {"from", "confidence", "how": exact|alias|similar}}. No source column twice."""
    src = _names(source_cols)
    dst = _names(dest_cols)
    used: set = set()
    out: dict = {}
    norm_src = {s: normalise(s) for s in src}
    for how, conf in (("exact", 1.0), ("alias", 0.9)):
        for d in dst:
            if d in out:
                continue
            nd = normalise(d)
            for s in src:
                if s in used:
                    continue
                ns = norm_src[s]
                if (how == "exact" and ns == nd and nd) or \
                        (how == "alias" and nd and ns and ns in _alias_of(nd)):
                    out[d] = {"from": s, "confidence": conf, "how": how}
                    used.add(s)
                    break
    for d in dst:
        if d in out:
            continue
        nd = normalise(d)
        best, score = None, 0.0
        for s in src:
            if s in used:
                continue
            r = difflib.SequenceMatcher(None, nd, norm_src[s]).ratio()
            if r > score:
                best, score = s, r
        if best is not None and score >= 0.85:
            out[d] = {"from": best, "confidence": round(score, 2), "how": "similar"}
            used.add(best)
    return out


# ---------------------------------------------------------------- the config

def config(job) -> dict | None:
    m = (job or {}).get("mapping")
    return m if isinstance(m, dict) and m.get("columns") else None


def check_config(mapping) -> list[str]:
    """Shape problems of a job's mapping block (no columns needed). Plain sentences."""
    p = []
    if mapping is None:
        return p
    if not isinstance(mapping, dict):
        return ["mapping must be a block with columns."]
    cols = mapping.get("columns")
    if not isinstance(cols, dict) or not cols:
        p.append("mapping.columns must map each destination column to from, const or expr.")
        cols = {}
    if mapping.get("unmapped", "default") not in UNMAPPED:
        p.append("mapping.unmapped is default or error.")
    if mapping.get("on_cast_error", "error") not in ON_CAST_ERROR:
        p.append("mapping.on_cast_error is error or null.")
    for dest, how in cols.items():
        if not isinstance(dest, str) or not dest:
            p.append("Every mapped destination column needs a name.")
            continue
        if not isinstance(how, dict) or sum(k in how for k in HOW) != 1:
            p.append(f"Mapping '{dest}': give exactly one of from, const or expr.")
            continue
        if "from" in how and (not isinstance(how["from"], str) or not how["from"]):
            p.append(f"Mapping '{dest}': from names a source column.")
        if "const" in how:
            try:
                literal(how["const"])
            except MappingError as e:
                p.append(f"Mapping '{dest}': {e}")
        if "expr" in how:
            try:
                transform.check_expr(how["expr"], None, where=f"Mapping '{dest}'")
            except ValueError as e:
                p.append(str(e) if str(e).endswith(".") else str(e) + ".")
    return p


def _src_lookup(source_columns) -> dict:
    out = {}
    for c in source_columns or []:
        name = c["name"] if isinstance(c, dict) else str(c)
        out.setdefault(name.lower(), c if isinstance(c, dict) else {"name": name, "type": "VARCHAR"})
    return out


def _dest_cols(dest_columns) -> list[dict]:
    out = []
    for c in dest_columns or []:
        if isinstance(c, dict):
            out.append(c)
        else:
            out.append({"name": str(c), "type": "text"})
    return out


def _src_type(c: dict) -> tuple:
    t = c.get("generic") or ""
    if t:
        return ddl.parse_type(t)
    return ddl.parse_type(ddl.duck_to_generic(c.get("type")))


def _src_generic(c: dict) -> str:
    return _src_type(c)[0]


def _narrowing(src: tuple, dest: tuple) -> list[str]:
    """Plain warnings for a number that fits the source type but may not fit the destination:
    a smaller decimal scale rounds without a word, fewer whole digits or bigint into integer
    fail at load."""
    (sb, sp, ss), (db, dp, ds) = src, dest
    out = []
    if sb == "decimal" and db == "decimal":
        if (ss or 0) > (ds or 0):
            out.append(f"The source has {ss} decimals and the destination {ds or 0}: values are rounded.")
        if (sp or 0) - (ss or 0) > (dp or 0) - (ds or 0):
            out.append(f"The destination holds {(dp or 0) - (ds or 0)} whole digits and the source "
                       f"{(sp or 0) - (ss or 0)}: larger values will not fit.")
    elif sb in ("integer", "bigint") and db == "decimal":
        whole = 10 if sb == "integer" else 19
        if (dp or 0) - (ds or 0) < whole:
            out.append(f"The destination holds {(dp or 0) - (ds or 0)} whole digits: larger values will not fit.")
    elif sb == "bigint" and db == "integer":
        out.append("bigint into integer: values above 2147483647 will not fit.")
    return out


def validate(job, source_columns, dest_columns) -> list[dict]:
    """Problems: [{"column","level":"error"|"warn","message"}] (SPEC_V2 8.2)."""
    out = []
    mapping = config(job) or {"columns": {}}
    for msg in check_config(mapping):
        out.append({"column": None, "level": "error", "message": msg})
    cols = mapping.get("columns") or {}
    unmapped_mode = mapping.get("unmapped", "default")
    src = _src_lookup(source_columns)
    dests = _dest_cols(dest_columns)
    dlow = {str(d["name"]).lower(): d for d in dests}
    keys = {str(k).lower() for k in (mapping.get("_keys") or [])}
    mapped_low = {str(k).lower() for k in cols}
    for dname in cols:
        if str(dname).lower() not in dlow:
            out.append({"column": dname, "level": "error", "message": "The destination has no such column."})
    for d in dests:
        name = d["name"]
        how = None
        for k, v in cols.items():
            if str(k).lower() == str(name).lower():
                how = v
        required = (d.get("nullable") is False or str(name).lower() in keys) and d.get("default") in (None, "")
        if how is None:
            if required:
                out.append({"column": name, "level": "error",
                            "message": "Required: map a column, a constant or an expression."})
            elif unmapped_mode == "error":
                out.append({"column": name, "level": "error", "message": "Not mapped."})
            continue
        if not isinstance(how, dict):
            continue
        try:
            dbase, dn, _ = ddl.parse_type(d.get("type") or "text")
        except ValueError as e:
            out.append({"column": name, "level": "error", "message": str(e)})
            continue
        if "from" in how:
            s = src.get(str(how.get("from") or "").lower())
            if s is None:
                out.append({"column": name, "level": "error",
                            "message": f"There is no source column '{how.get('from')}'."})
                continue
            try:
                stype = _src_type(s)
            except ValueError:
                stype = ("text", None, None)
            sbase = stype[0]
            for msg in _narrowing(stype, (dbase, dn, _)):
                out.append({"column": name, "level": "warn", "message": msg})
            if sbase in ("text", "string", "json") and dbase in ("integer", "bigint", "decimal", "double", "date",
                                                                 "timestamp", "timestamptz", "boolean", "time"):
                out.append({"column": name, "level": "warn",
                            "message": "Text into a number or date: rows that do not convert will fail."})
            elif sbase in ("decimal", "double") and dbase in ("integer", "bigint"):
                out.append({"column": name, "level": "warn", "message": "Decimals will be cut."})
            elif sbase in ("timestamp", "timestamptz") and dbase == "date":
                out.append({"column": name, "level": "warn", "message": "The time part is dropped."})
            if dbase == "string" and dn:
                ml = s.get("max_len")
                if ml is None and s.get("samples"):
                    ml = max((len(str(v)) for v in s["samples"] if v is not None), default=0)
                if ml and ml > dn:
                    out.append({"column": name, "level": "warn",
                                "message": f"Some sample values are longer than {dn}."})
            if (d.get("nullable") is False or str(name).lower() in keys) and s.get("nulls"):
                out.append({"column": name, "level": "warn",
                            "message": "Sample has empty values; this column is required."})
        elif "expr" in how:
            try:
                transform.check_expr(how["expr"], list(src.values()), where="Expression")
            except ValueError as e:
                out.append({"column": name, "level": "error", "message": str(e)})
        elif "const" in how:
            if how["const"] is None and (d.get("nullable") is False or str(name).lower() in keys):
                out.append({"column": name, "level": "error", "message": "A required column cannot be a null constant."})
    seen = set()
    uniq = []
    for p in out:
        k = (p["column"], p["level"], p["message"])
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    del mapped_low
    return uniq


def select_sql(job, source_columns, dest_columns, input_name: str = "input") -> str:
    """The mapping SELECT over the view input. Raises MappingError on an error problem."""
    mapping = config(job)
    if not mapping:
        raise MappingError("The job has no mapping.")
    errors = [p for p in validate(job, source_columns, dest_columns) if p["level"] == "error"]
    if errors:
        p = errors[0]
        raise MappingError((f"Mapping '{p['column']}': " if p.get("column") else "Mapping: ") + p["message"])
    cast = "TRY_CAST" if mapping.get("on_cast_error") == "null" else "CAST"
    src = _src_lookup(source_columns)
    cols = mapping["columns"]
    parts = []
    for d in _dest_cols(dest_columns):
        how = next((v for k, v in cols.items() if str(k).lower() == str(d["name"]).lower()), None)
        if how is None:
            continue
        gen = d.get("type") or "text"
        duck = ddl.generic_to_duck(gen)
        if "from" in how:
            exact = src[str(how["from"]).lower()]["name"]
            val = transform.q(exact)
        elif "const" in how:
            val = literal(how["const"], gen)
        else:
            val = f"({how['expr']})"
        parts.append(f"{cast}({val} AS {duck}) AS {transform.q(d['name'])}")
    if not parts:
        raise MappingError("Mapping: no column is mapped.")
    return "SELECT " + ",\n       ".join(parts) + f"\nFROM {input_name}"


# ---------------------------------------------------------------- columns of a job's destination

def _resolve_target(project, job) -> dict:
    t = dict((job or {}).get("target") or {})
    did = t.get("destination")
    if did:
        from etlplus import destinations
        base = destinations.resolve(project, did)
        base.update({k: v for k, v in t.items() if k != "destination" and v not in (None, [], "")})
        base["destination"] = did
        return base
    return t


def destination_columns(project, job) -> list[dict]:
    """[{name, type (generic), nullable, default, key, personal}] the mapping targets."""
    t = (job or {}).get("target") or {}
    did = t.get("destination")
    keys = []
    if did:
        from etlplus import destinations
        cols = destinations.columns(project, did)
        spec = (project.destinations or {}).get(did) or {}
        keys = t.get("key") or spec.get("keys") or []
    else:
        cols = []
        try:
            from etlplus import targets
            st = targets.status(project, job)
            cols = [{"name": c["name"], "type": ddl.duck_to_generic(c.get("type")) if c.get("type") else "text"}
                    for c in st.get("columns") or []]
        except Exception:
            cols = []
        if not cols:
            cols = [{"name": n, "type": "text"} for n in ((config(job) or {}).get("columns") or {})]
        keys = t.get("key") or []
    keys = [keys] if isinstance(keys, str) else list(keys)
    klow = {str(k).lower() for k in keys}
    out = []
    for c in cols:
        c = dict(c)
        c.setdefault("type", "text")
        c["key"] = str(c["name"]).lower() in klow
        if c["key"]:
            c["nullable"] = False
        c.setdefault("nullable", True)
        c.setdefault("personal", False)
        out.append(c)
    return out


def _with_keys(job, dest_cols) -> dict:
    j = dict(job)
    m = dict(config(j) or {})
    m["_keys"] = [c["name"] for c in dest_cols if c.get("key")]
    j["mapping"] = m
    return j


# ---------------------------------------------------------------- the map step

def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apply(project, job, manifest: dict, log=None) -> dict:
    """Map the staged file of `manifest` (transformed or extract) into mapped.parquet."""
    from etlplus import stage, targets
    name = job["name"]
    run_id = manifest["run_id"]
    src = targets.staged_file(project, manifest)
    run_dir = src.parent
    out = stage.path(project, name, run_id, "mapped")
    partial = out.with_name(out.name + ".partial")
    mp = stage.manifest_path(project, name, run_id, "mapped")
    for stale in (mp, out, partial):
        if stale.exists():
            stale.unlink()
    dest_cols = destination_columns(project, job)
    started = _now()
    con = transform.sandbox(src, partial)
    try:
        con.execute(f"CREATE OR REPLACE TEMP VIEW input AS SELECT * FROM read_parquet({transform._lit(src)})")
        source_cols = [{"name": r[0], "type": r[1]} for r in con.execute("DESCRIBE input").fetchall()]
        sql = select_sql(_with_keys(job, dest_cols), source_cols, dest_cols)
        if log:
            log(f"map: {sql}")
        _check(log)
        try:
            con.execute(f"COPY ({sql}) TO {transform._lit(partial)} (FORMAT parquet, COMPRESSION zstd)")
        except Exception as e:
            msg = str(e).split("\n")[0]
            raise MappingError(f"Mapping failed: {msg}") from None
        cols = [{"name": r[0], "type": r[1]}
                for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({transform._lit(partial)})").fetchall()]
        rows = con.execute(f"SELECT count(*) FROM read_parquet({transform._lit(partial)})").fetchone()[0]
    except BaseException:
        con.close()
        if partial.exists():
            partial.unlink()
        raise
    finally:
        con.close()
    tmp = mp.with_name(mp.name + ".partial")
    try:
        m = {
            "job": name, "run_id": run_id, "kind": "mapped", "mapped": True,
            "rows": int(rows), "rows_in": manifest.get("rows"), "columns": cols,
            "window": manifest.get("window"),
            "source": f"mapping of {src.name} ({len(cols)} column(s))",
            "file": out.name, "bytes": partial.stat().st_size, "sha256": _sha256(partial),
            "mapping": (config(job) or {}).get("columns"),
            "started_at": started, "finished_at": _now(),
        }
        tmp.write_text(json.dumps(m, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, mp)
        os.replace(partial, out)
    except BaseException:
        for f in (tmp, partial):
            if f.exists():
                f.unlink()
        if mp.exists() and not out.exists():
            mp.unlink()
        raise
    if log:
        log(f"map: {rows} rows written to {out.name}")
    del run_dir
    return m


def _check(log):
    fn = getattr(log, "check", None)
    if fn:
        fn()


# ---------------------------------------------------------------- preview (SPEC_V2 8.5)

def _personal_source_columns(project, job) -> set:
    src = (job or {}).get("source") or {}
    fmt = src.get("format")
    out = set()
    try:
        from etlplus import formats
        info = formats.FORMATS.get(fmt) if fmt else None
        if info is not None and info.columns:
            out |= {c.name for c in info.columns if getattr(c, "personal", False)}
        if fmt == "layout":
            from etlplus.formats import layouts
            lay = layouts.load((src.get("options") or {}).get("layout"), project)
            for rec in lay.get("records") or []:
                for f in rec.get("fields") or []:
                    if f.get("personal"):
                        out.add(f.get("name"))
    except Exception:
        pass
    return out


def _sample_live(project, job, folder: Path):
    """A live sample of at most SAMPLE_ROWS rows, run through the job's transform."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    src = dict((job or {}).get("source") or {})
    table = sample_source(project, src, SAMPLE_ROWS, folder)
    raw = folder / "sample.parquet"
    pq.write_table(table if table.num_columns else pa.table({"_empty": pa.array([], pa.string())}), str(raw))
    steps = job.get("transform") or []
    if not steps:
        return raw
    out = folder / "sample_t.parquet"
    con = transform.sandbox(raw, out)
    try:
        last, _ = transform.build(con, str(raw), steps)
        con.execute(f"COPY (SELECT * FROM {last}) TO {transform._lit(out)} (FORMAT parquet)")
    finally:
        con.close()
    return out


def sample_source(project, source: dict, limit: int = SAMPLE_ROWS, folder: Path | None = None):
    """At most `limit` rows of a source as an Arrow table, read through the normal readers."""
    import pyarrow as pa
    from etlplus import connections, sources
    cid = source.get("connection")
    spec = (project.connections or {}).get(cid)
    if not cid or spec is None:
        raise MappingError(f"The source connection '{cid}' does not exist.")
    kind = spec.get("kind")
    if source.get("files"):
        job = {"name": "_preview", "source": dict(source, chunk_rows=limit)}
        kw = {}
        try:
            import inspect
            params = inspect.signature(sources.read).parameters
            if "fetch_dir" in params and folder is not None:
                kw["fetch_dir"] = folder / "incoming"
                (folder / "incoming").mkdir(parents=True, exist_ok=True)
        except (TypeError, ValueError):
            pass
        gen = sources.read(project, job, None, None, **kw)
        batches, n = [], 0
        try:
            for b in gen:
                batches.append(b.slice(0, max(limit - n, 0)))
                n += batches[-1].num_rows
                if n >= limit:
                    break
        finally:
            close = getattr(gen, "close", None)
            if close:
                close()
        return pa.Table.from_batches(batches) if batches else pa.table({})
    what = source.get("table") or source.get("query")
    if not what:
        raise MappingError("The source needs a table, a query or files.")
    res = connections.preview(project, cid, what, limit=limit)
    names = res.get("columns") or []
    rows = res.get("rows") or []
    cols = {n: [r[i] for r in rows] for i, n in enumerate(names)}
    try:
        return pa.table(cols)
    except Exception:
        return pa.table({n: pa.array([None if v is None else str(v) for v in vals], pa.string())
                         for n, vals in cols.items()})
    del kind


def preview(project, job: dict, limit: int = 50) -> dict:
    """Mapped rows for a job that may be unsaved; writes nothing but a temp sample it deletes."""
    from etlplus import stage, targets
    job = dict(job or {})
    name = job.get("name") or "_preview"
    key = (str(Path(project.path).resolve()), name)
    with _previews_lock:
        if key in _previews:
            raise PreviewBusy("A preview is already running")
        _previews.add(key)
    tmpdir = None
    try:
        limit = max(1, min(int(limit or 50), 500))
        based_on = "live-sample"
        src_file = None
        if job.get("name") and job["name"] in (project.jobs or {}):
            m = stage.latest(project, name, "extract")
            if m is not None:
                if job.get("transform"):
                    t = stage.read_manifest(project, name, m["run_id"], "transformed")
                    if t is not None:
                        src_file, based_on = targets.staged_file(project, t), f"staged:{m['run_id']}"
                else:
                    src_file, based_on = targets.staged_file(project, m), f"staged:{m['run_id']}"
        if src_file is None:
            try:
                base = stage.job_dir(project, name)
            except ValueError:
                base = Path(project.stage_dir()) / "_preview"
            tmpdir = base / f".preview-{uuid.uuid4().hex[:8]}"
            tmpdir.mkdir(parents=True, exist_ok=True)
            src_file = _sample_live(project, job, tmpdir)
        dest_cols = destination_columns(project, job)
        personal = _personal_source_columns(project, job)
        mapped_from = {}
        for d, how in ((config(job) or {}).get("columns") or {}).items():
            if isinstance(how, dict) and "from" in how:
                mapped_from[str(how["from"]).lower()] = d
        dpersonal = {str(c["name"]).lower() for c in dest_cols if c.get("personal")}
        con = transform.sandbox(src_file)
        try:
            con.execute(f"CREATE OR REPLACE TEMP VIEW input AS SELECT * FROM "
                        f"(SELECT * FROM read_parquet({transform._lit(src_file)}) LIMIT {SAMPLE_ROWS})")
            described = con.execute("DESCRIBE input").fetchall()
            source_cols = []
            for r in described:
                cname, ctype = r[0], r[1]
                if cname == "_empty":
                    continue
                c = transform.q(cname)
                samples = [transform.jsonable(x[0]) for x in con.execute(
                    f"SELECT {c} FROM input WHERE {c} IS NOT NULL LIMIT 3").fetchall()]
                nulls, ml = con.execute(
                    f"SELECT count(*) FILTER (WHERE {c} IS NULL), max(length(CAST({c} AS VARCHAR))) FROM input").fetchone()
                dto = mapped_from.get(cname.lower())
                source_cols.append({"name": cname, "type": ctype, "samples": samples, "nulls": int(nulls or 0),
                                    "max_len": int(ml or 0),
                                    "personal": cname in personal or (dto is not None and dto.lower() in dpersonal)})
            jk = _with_keys(job, dest_cols)
            problems = validate(jk, source_cols, dest_cols) if config(job) else []
            rows, columns = [], []
            if config(job) and not [p for p in problems if p["level"] == "error"]:
                try:
                    sql = select_sql(jk, source_cols, dest_cols)
                    cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {limit}")
                    rows = [[transform.jsonable(v) for v in r] for r in cur.fetchall()]
                    columns_desc = [d[0] for d in cur.description]
                    columns = columns_desc
                except Exception as e:
                    problems.append({"column": None, "level": "error",
                                     "message": str(e).split("\n")[0]})
        finally:
            con.close()
        mcols = []
        for d, how in ((config(job) or {}).get("columns") or {}).items():
            dc = next((c for c in dest_cols if str(c["name"]).lower() == str(d).lower()), None)
            entry = {"name": d, "type": (dc or {}).get("type")}
            if isinstance(how, dict):
                entry.update({k: how[k] for k in HOW if k in how})
            mcols.append(entry)
        del columns
        return {
            "source_columns": [{k: c[k] for k in ("name", "type", "samples", "nulls", "personal")}
                               for c in source_cols],
            "destination_columns": [{"name": c["name"], "type": c.get("type"), "nullable": c.get("nullable", True),
                                     "key": bool(c.get("key")), "personal": bool(c.get("personal"))}
                                    for c in dest_cols],
            "columns": mcols, "rows": rows, "problems": problems, "based_on": based_on,
        }
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
        with _previews_lock:
            _previews.discard(key)
