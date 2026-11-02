"""Readers: a job's source as a stream of Arrow record batches.

Databases are read with one guarded SELECT, streamed with server-side cursors where the
driver has them. An incremental window is a half-open range on the raw column,
"col >= from AND col < to", so the source can use its index. Files (csv, txt, fixed, parquet,
json, jsonl, excel, and the bank formats of etlplus.formats) are read from a folder, a shared
folder or a remote location, one file or a glob of many; a remote file is fetched into the stage
(incoming/) first and read there. MongoDB collections are read with a checked find or aggregate.
Every stream keeps the schema of its first chunk; later chunks are cast to it.
"""

import codecs
import csv
import datetime as dt
import decimal
import importlib
import importlib.util
import io
import json
import re
import shutil
import time
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from . import connections, drivers, guard

DEFAULT_CHUNK = 100_000
FORMATS = {"csv", "txt", "fixed", "parquet", "json", "jsonl", "excel"}


def _formats():
    """etlplus.formats (the bank formats) when it is there."""
    if importlib.util.find_spec("etlplus.formats") is None:
        return None
    return importlib.import_module("etlplus.formats")


def bank_formats() -> set:
    fm = _formats()
    return set(getattr(fm, "FORMATS", {}) or {}) if fm is not None else set()


# every format a job may name: the file formats here plus the bank formats
ALL_FORMATS = FORMATS | bank_formats()

PICKUP_DEFAULTS = {"skip_seen": True, "min_age_seconds": 60, "max_files": 500, "after": "keep",
                   "archive_to": None}


def _log(log, msg):
    if log:
        log(msg)


def _check(log):
    fn = getattr(log, "check", None)
    if fn:
        fn()


# schema handling

def _fallback_str(values):
    return pa.array([None if v is None else (v.hex() if isinstance(v, (bytes, bytearray)) else str(v))
                     for v in values], type=pa.string())


def _to_array(values, type_=None):
    if type_ is not None and not pa.types.is_null(type_):
        try:
            return pa.array(values, type=type_)
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError):
            pass
    try:
        arr = pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError, TypeError):
        arr = _fallback_str(values)
    if type_ is not None and not pa.types.is_null(type_) and arr.type != type_:
        arr = arr.cast(type_)
    return arr


def unique_names(names):
    seen, out = {}, []
    for i, n in enumerate(names):
        n = str(n).strip() if n is not None and str(n).strip() else f"col{i + 1}"
        base, k = n, 1
        while n.lower() in seen:
            k += 1
            n = f"{base}_{k}"
        seen[n.lower()] = True
        out.append(n)
    return out


def _widen(t: pa.DataType) -> pa.DataType:
    if pa.types.is_null(t):
        return pa.string()
    if pa.types.is_decimal128(t) and t.scale >= 0:
        return pa.decimal128(38, t.scale)
    return t


class Conform:
    """Locks the schema on the first batch and makes every later batch fit it."""

    def __init__(self, log=None):
        self.schema = None
        self.log = log
        self.warned = False

    def __call__(self, batch: pa.RecordBatch) -> pa.RecordBatch:
        if self.schema is None:
            fields = [pa.field(f.name, _widen(f.type)) for f in batch.schema]
            self.schema = pa.schema(fields)
        names = self.schema.names
        if batch.schema.names != names:
            extra = [n for n in batch.schema.names if n not in names]
            if extra and not self.warned:
                _log(self.log, f"warning: columns not in the first chunk are dropped: {', '.join(extra)}")
                self.warned = True
        cols = []
        for f in self.schema:
            idx = batch.schema.get_field_index(f.name)
            if idx < 0:
                cols.append(pa.nulls(batch.num_rows, f.type))
                continue
            col = batch.column(idx)
            if col.type != f.type:
                try:
                    col = col.cast(f.type)
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as e:
                    raise ValueError(
                        f"Column '{f.name}' changed type from {f.type} to {col.type} part way "
                        f"through the source ({e}). For files, set options.types: text.") from None
            cols.append(col)
        return pa.RecordBatch.from_arrays(cols, schema=self.schema)


def rows_to_batch(names, rows, schema=None, scales=None) -> pa.RecordBatch:
    columns = list(zip(*rows)) if rows else [[] for _ in names]
    arrays = []
    for i, name in enumerate(names):
        vals = list(columns[i])
        t = schema.field(name).type if schema is not None else None
        if t is None and scales and scales[i] is not None and any(isinstance(v, decimal.Decimal) for v in vals):
            t = pa.decimal128(38, min(max(int(scales[i]), 0), 37))
        if t is None and not vals:
            t = pa.string()
        arrays.append(_to_array(vals, t))
    return pa.RecordBatch.from_arrays(arrays, names=list(names))


# windows

def _as_value(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            if len(s) == 10:
                return dt.date.fromisoformat(s)
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s
    return v


def _param(kind, v):
    v = _as_value(v)
    kind = drivers.family(kind) or kind
    if kind == "sqlite":
        if isinstance(v, dt.datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(v, dt.date):
            return v.isoformat()
    if kind == "oracle" and isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return dt.datetime(v.year, v.month, v.day)
    return v


def window_sql(kind, column, window=None, watermark=None, start=0):
    """(where, params) for a half-open window on the raw column, or a watermark."""
    col = connections.quote_ident(kind, column)
    if window is not None:
        lo, hi = window
        conds, params = [], []
        if lo is not None:
            conds.append(f"{col} >= {connections.placeholder(kind, start + len(params))}")
            params.append(_param(kind, lo))
        if hi is not None:
            conds.append(f"{col} < {connections.placeholder(kind, start + len(params))}")
            params.append(_param(kind, hi))
        return " AND ".join(conds), params
    if watermark is not None:
        return f"{col} > {connections.placeholder(kind, start)}", [_param(kind, watermark)]
    return "", []


def _watermark(project, job):
    inc = (job.get("source") or {}).get("incremental") or {}
    if not inc.get("watermark"):
        return None
    try:
        from . import store
    except ImportError:
        return None
    return store.watermark(project, job.get("name"))


def source_sql(kind, src: dict, window=None, watermark=None):
    """(sql, params) for a table or query source, guarded."""
    columns = src.get("columns") or "*"
    if isinstance(columns, str) and columns.strip() != "*":
        columns = [c.strip() for c in columns.split(",") if c.strip()]
    inc = src.get("incremental") or {}
    where, params = ("", [])
    if inc.get("column"):
        where, params = window_sql(kind, inc["column"], window, watermark)
    if src.get("query"):
        inner = guard.check_select(src["query"], kind)
        cols = "*" if columns in ("*", None) or columns == ["*"] else ", ".join(
            connections.quote_ident(kind, c) for c in columns)
        base = inner if cols == "*" and not where else f"SELECT {cols} FROM ({inner}) q"
    elif src.get("table"):
        base = connections.select_sql(kind, src["table"], columns)
    else:
        raise ValueError("The source needs a table, a query or files.")
    if params and drivers.escapes_percent(kind):
        base = base.replace("%", "%%")
    sql = f"{base} WHERE {where}" if where else base
    return guard.check_select(sql, kind), params


def describe(project, job) -> str:
    src = job.get("source") or {}
    cid = src.get("connection")
    if src.get("files"):
        return f"{cid}:{src['files']}"
    if src.get("table"):
        return f"{cid}:{src['table']}"
    return f"{cid}:query"


# databases

SERVER_CURSOR_KINDS = ("postgres", "timescaledb", "greenplum", "yugabytedb")


def _cursor(conn, kind, name="etlplus_extract"):
    fam = drivers.family(kind)
    if kind in SERVER_CURSOR_KINDS:
        return conn.cursor(name=name)
    if fam == "mysql":
        cursors = getattr(connections.driver(kind), "cursors", None)
        ss = getattr(cursors, "SSCursor", None)
        return conn.cursor(ss) if ss is not None else conn.cursor()
    return conn.cursor()


def read_db(project, cid, sql, params, chunk_rows, log=None, spec=None):
    spec = spec or connections.spec_for(project, cid)
    kind = spec.get("kind")
    fam = drivers.family(kind)
    sql = connections.read_sql(kind, guard.check_select(sql, kind))
    conn = connections.connect(project, cid, spec=spec)
    try:
        conform = Conform(log)
        if fam == "duckdb":
            rel = conn.sql(sql, params=list(params or [])) if params else conn.sql(sql)
            if hasattr(rel, "to_arrow_reader"):
                reader = rel.to_arrow_reader(chunk_rows)
            else:
                reader = rel.fetch_arrow_reader(chunk_rows)
            any_rows = False
            for batch in reader:
                _check(log)
                any_rows = True
                yield conform(batch)
            if not any_rows:
                yield conform(pa.RecordBatch.from_pylist([], schema=reader.schema))
            return
        cur = _cursor(conn, kind)
        if kind in SERVER_CURSOR_KINDS:
            cur.itersize = chunk_rows
        if fam == "oracle":
            cur.arraysize = min(chunk_rows, 10_000)
            cur.prefetchrows = cur.arraysize + 1
        elif fam in ("db2", "hana", "teradata", "firebird", "odbc", "snowflake", "sqlserver"):
            try:
                cur.arraysize = min(chunk_rows, 10_000)
            except Exception:
                pass
        if params:
            cur.execute(sql, drivers.bind_params(kind, params))
        else:
            cur.execute(sql)
        desc = cur.description or []
        names = unique_names([d[0] for d in desc])
        scales = [d[5] if len(d) > 5 else None for d in desc]
        any_rows = False
        while True:
            _check(log)
            rows = cur.fetchmany(chunk_rows)
            if not rows:
                break
            any_rows = True
            batch = rows_to_batch(names, [tuple(r) for r in rows], conform.schema, scales)
            yield conform(batch)
        if not any_rows:
            yield conform(rows_to_batch(names, [], None))
        try:
            cur.close()
        except Exception:
            pass
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        connections.close(conn)


# files

def _norm_delim(d):
    if d is None:
        return None
    d = str(d)
    return {"\\t": "\t", "tab": "\t", "TAB": "\t", "\\|": "|", "pipe": "|", "space": " "}.get(d, d)


def match_files(root: Path, pattern, project=None) -> list[Path]:
    root = Path(root).resolve()
    patterns = pattern if isinstance(pattern, (list, tuple)) else [pattern]
    found = []
    for pat in patterns:
        pat = str(pat).replace("\\", "/").strip()
        if not pat:
            continue
        if Path(pat).is_absolute() or ".." in Path(pat).parts:
            raise ValueError(f"The file pattern '{pat}' must stay inside the folder {root}.")
        if any(c in pat for c in "*?["):
            matches = sorted(p for p in root.glob(pat) if p.is_file())
        else:
            p = root / pat
            matches = [p] if p.is_file() else []
        for m in matches:
            m = m.resolve()
            try:
                m.relative_to(root)
            except ValueError:
                continue  # a symlink that leaves the folder is not followed
            if m.name.endswith(".partial") or m.name.startswith("."):
                continue
            if project is not None:
                from .remote import paths
                paths.check_local(project, m)
            if m not in found:
                found.append(m)
    return found


def _sniff_delimiter(path: Path, encoding, skip_rows):
    with open(path, encoding=encoding, errors="replace", newline="") as f:
        for _ in range(skip_rows):
            f.readline()
        line = f.readline()
    counts = {d: line.count(d) for d in ("\t", "|", ";", ",")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _header_names(path, encoding, delimiter, skip_rows, quote):
    with open(path, encoding=encoding, errors="replace", newline="") as f:
        for _ in range(skip_rows):
            f.readline()
        line = f.readline()
    if line.startswith("﻿"):
        line = line[1:]
    row = next(csv.reader([line], delimiter=delimiter, quotechar=quote or '"'), [])
    return [c.strip() for c in row]


def _encoding(opts):
    enc = (opts.get("encoding") or "utf-8").strip()
    codecs.lookup(enc)
    return enc


def _read_delimited(path, opts, chunk_rows, state):
    enc = _encoding(opts)
    skip = int(opts.get("skip_rows") or 0)
    header = opts.get("header", True)
    header = header not in (False, "false", "no", 0)
    delim = _norm_delim(opts.get("delimiter"))
    if not delim:
        delim = "," if state["format"] == "csv" else _sniff_delimiter(path, enc, skip)
    quote = opts.get("quote", '"')
    names = opts.get("names")
    text = str(opts.get("types") or "infer").lower() == "text"

    if len(delim) > 1:
        yield from _read_multichar(path, enc, skip, header, delim, names, chunk_rows)
        return

    if header:
        cols = unique_names(_header_names(path, enc, delim, skip, quote))
        if names:
            cols = unique_names(names)
        read_opts = pacsv.ReadOptions(encoding=enc, skip_rows=skip + 1, column_names=cols,
                                      block_size=64 << 20)
    elif names:
        cols = unique_names(names)
        read_opts = pacsv.ReadOptions(encoding=enc, skip_rows=skip, column_names=cols, block_size=64 << 20)
    else:
        cols = None
        read_opts = pacsv.ReadOptions(encoding=enc, skip_rows=skip, autogenerate_column_names=True,
                                      block_size=64 << 20)
    parse_opts = pacsv.ParseOptions(delimiter=delim, quote_char=quote if quote else False,
                                    newlines_in_values=bool(opts.get("newlines_in_values", False)))
    # Only an unquoted empty field is NULL. Markers such as NULL or N/A are kept as written unless
    # the job lists them in options.null_values; a quoted "" stays an empty string.
    null_values = [""] + [str(v) for v in (opts.get("null_values") or [])]
    column_types = {}
    if text and cols:
        column_types = {c: pa.string() for c in cols}
    elif state.get("csv_types"):
        column_types = dict(state["csv_types"])
    elif not text:
        # numbers written with leading zeros (account numbers, codes) are kept as text
        for c in _leading_zero_columns(path, read_opts, parse_opts, null_values, cols):
            column_types[c] = pa.string()
    conv = pacsv.ConvertOptions(column_types=column_types, null_values=null_values,
                                strings_can_be_null=True, quoted_strings_can_be_null=False)
    stream = pa.input_stream(str(path), compression="detect")
    try:
        reader = pacsv.open_csv(stream, read_options=read_opts, parse_options=parse_opts,
                                convert_options=conv)
        first = True
        for batch in reader:
            if text and first and not cols:
                batch = batch.cast(pa.schema([pa.field(n, pa.string()) for n in batch.schema.names]))
            first = False
            if "csv_types" not in state:
                state["csv_types"] = {f.name: _widen(f.type) for f in batch.schema
                                      if not pa.types.is_null(f.type)}
            if text and batch.num_rows and not all(pa.types.is_string(t) for t in batch.schema.types):
                batch = batch.cast(pa.schema([pa.field(n, pa.string()) for n in batch.schema.names]))
            yield batch
    except pa.ArrowInvalid as e:
        if "conversion error" in str(e).lower():
            raise ValueError(f"{path.name}: {e}. Set options.types: text to read every column as text.") from None
        raise
    finally:
        stream.close()


_LEADING_ZERO = r"^\s*[+-]?0[0-9]"


def _leading_zero_columns(path, read_opts, parse_opts, null_values, cols) -> list[str]:
    """Columns whose values in the first block (the block types are inferred from) look like
    numbers with leading zeros, such as 0012345. Read as numbers they would lose the zeros."""
    if cols is None:
        n = len(_header_names(path, read_opts.encoding, parse_opts.delimiter, read_opts.skip_rows,
                              parse_opts.quote_char or None))
        names = [f"f{i}" for i in range(n)]
    else:
        names = list(cols)
    conv = pacsv.ConvertOptions(column_types={c: pa.string() for c in names}, null_values=null_values,
                                strings_can_be_null=True, quoted_strings_can_be_null=False)
    stream = pa.input_stream(str(path), compression="detect")
    try:
        reader = pacsv.open_csv(stream, read_options=read_opts, parse_options=parse_opts, convert_options=conv)
        try:
            batch = reader.read_next_batch()
        except StopIteration:
            return []
    except (pa.ArrowInvalid, OSError):
        return []
    finally:
        stream.close()
    out = []
    for name, col in zip(batch.schema.names, batch.columns):
        if not pa.types.is_string(col.type):
            continue
        hit = pc.any(pc.match_substring_regex(col, _LEADING_ZERO)).as_py()
        if hit:
            out.append(name)
    return out


def _read_multichar(path, enc, skip, header, delim, names, chunk_rows):
    with open(path, encoding=enc, errors="strict", newline="") as f:
        for _ in range(skip):
            f.readline()
        cols = None
        if header:
            first = f.readline().rstrip("\r\n").lstrip("﻿")
            cols = unique_names(first.split(delim))
        if names:
            cols = unique_names(names)
        rows = []
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split(delim)
            if cols is None:
                cols = [f"f{i}" for i in range(len(parts))]
            parts = (parts + [None] * len(cols))[:len(cols)]
            rows.append([None if p is None or p == "" else p for p in parts])
            if len(rows) >= chunk_rows:
                yield _string_batch(cols, rows)
                rows = []
        if rows or cols:
            yield _string_batch(cols or [], rows)


def _string_batch(cols, rows):
    data = list(zip(*rows)) if rows else [[] for _ in cols]
    return pa.RecordBatch.from_arrays([pa.array(list(c), type=pa.string()) for c in data], names=list(cols))


def _read_fixed(path, opts, chunk_rows):
    widths = [int(w) for w in (opts.get("widths") or [])]
    if not widths:
        raise ValueError("Fixed width files need options.widths, for example [10, 8, 30].")
    names = opts.get("names") or [f"col{i + 1}" for i in range(len(widths))]
    if len(names) != len(widths):
        raise ValueError(f"options.names has {len(names)} names for {len(widths)} widths.")
    names = unique_names(names)
    enc = _encoding(opts)
    skip = int(opts.get("skip_rows") or 0)
    if opts.get("header") in (True, "true", "yes", 1):
        skip += 1
    strip = opts.get("strip", True)
    offsets = []
    pos = 0
    for w in widths:
        offsets.append((pos, pos + w))
        pos += w
    rows = []
    with open(path, encoding=enc, newline="") as f:
        for i, line in enumerate(f):
            if i < skip:
                continue
            line = line.rstrip("\r\n")
            if i == 0:
                line = line.lstrip("﻿")
            if not line.strip():
                continue
            vals = []
            for a, b in offsets:
                v = line[a:b]
                if strip:
                    v = v.strip()
                vals.append(v if v != "" else None)
            rows.append(vals)
            if len(rows) >= chunk_rows:
                yield _string_batch(names, rows)
                rows = []
    yield _string_batch(names, rows)


def _records(data, opts):
    key = opts.get("records") or opts.get("path")
    if key:
        for part in str(key).split("."):
            data = data[part] if isinstance(data, dict) else data
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if len(lists) == 1 else [data]
    if not isinstance(data, list):
        raise ValueError("The JSON file does not hold a list of records.")
    return [r if isinstance(r, dict) else {"value": r} for r in data]


def _dicts_to_batch(records, schema=None):
    names = []
    seen = set()
    for r in records:
        for k in r:
            if k not in seen:
                seen.add(k)
                names.append(k)
    if schema is not None:
        for n in schema.names:
            if n not in seen:
                names.append(n)
                seen.add(n)
    arrays = []
    for n in names:
        vals = [r.get(n) for r in records]
        t = schema.field(n).type if schema is not None and n in schema.names else None
        arrays.append(_to_array(vals, t))
    return pa.RecordBatch.from_arrays(arrays, names=[str(n) for n in names])


def _read_json(path, opts, chunk_rows, lines):
    enc = _encoding(opts)
    if not lines:
        with open(path, encoding=enc) as f:
            records = _records(json.load(f), opts)
        for i in range(0, len(records), chunk_rows):
            yield _dicts_to_batch(records[i:i + chunk_rows])
        if not records:
            yield pa.RecordBatch.from_arrays([], names=[])
        return
    batch, schema = [], None
    with open(path, encoding=enc) as f:
        for n, line in enumerate(f, 1):
            line = line.strip().lstrip("﻿")
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name} line {n}: not valid JSON ({e.msg}).") from None
            batch.append(rec if isinstance(rec, dict) else {"value": rec})
            if len(batch) >= chunk_rows:
                b = _dicts_to_batch(batch, schema)
                schema = schema or b.schema
                yield b
                batch = []
    if batch or schema is None:
        yield _dicts_to_batch(batch, schema)


def _read_excel(path, opts, chunk_rows):
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("Excel files need openpyxl: pip install openpyxl (or etlplus[excel]).") from None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = opts.get("sheet")
        if sheet is None or sheet == "":
            ws = wb.worksheets[0]
        elif isinstance(sheet, int) or (isinstance(sheet, str) and sheet.isdigit() and sheet not in wb.sheetnames):
            ws = wb.worksheets[int(sheet)]
        else:
            if sheet not in wb.sheetnames:
                raise ValueError(f"No sheet '{sheet}' in {path.name}. Sheets: {', '.join(wb.sheetnames)}.")
            ws = wb[sheet]
        skip = int(opts.get("skip_rows") or 0)
        header = opts.get("header", True) not in (False, "false", "no", 0)
        it = ws.iter_rows(values_only=True)
        for _ in range(skip):
            next(it, None)
        names = None
        if header:
            first = next(it, None) or ()
            names = unique_names(list(first))
        if opts.get("names"):
            names = unique_names(opts["names"])
        rows, schema = [], None
        for row in it:
            if row is None or all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
                continue
            if names is None:
                names = [f"col{i + 1}" for i in range(len(row))]
            row = (list(row) + [None] * len(names))[:len(names)]
            rows.append(row)
            if len(rows) >= chunk_rows:
                b = rows_to_batch(names, rows, schema)
                schema = schema or b.schema
                yield b
                rows = []
        if rows or schema is None:
            yield rows_to_batch(names or [], rows, schema)
    finally:
        wb.close()


def read_file(path: Path, fmt: str, opts: dict, chunk_rows: int, state=None, project=None, log=None,
              checks_out: list | None = None):
    state = state if state is not None else {}
    state["format"] = fmt
    fm = _formats()
    if fm is not None and fmt in (getattr(fm, "FORMATS", None) or {}):
        yield from fm.read(fmt, Path(path), dict(opts or {}), project=project, chunk_rows=chunk_rows,
                           log=log, checks_out=checks_out)
        return
    if fmt in ("csv", "txt"):
        yield from _read_delimited(path, opts, chunk_rows, state)
    elif fmt == "fixed":
        yield from _read_fixed(path, opts, chunk_rows)
    elif fmt == "parquet":
        pf = pq.ParquetFile(path)
        n = 0
        for b in pf.iter_batches(batch_size=chunk_rows):
            n += 1
            yield b
        if n == 0:
            yield pa.RecordBatch.from_pylist([], schema=pf.schema_arrow)
    elif fmt == "json":
        yield from _read_json(path, opts, chunk_rows, lines=False)
    elif fmt == "jsonl":
        yield from _read_json(path, opts, chunk_rows, lines=True)
    elif fmt == "excel":
        yield from _read_excel(path, opts, chunk_rows)
    else:
        raise ValueError(f"Unknown file format '{fmt}'. Use one of: {', '.join(sorted(FORMATS | bank_formats()))}.")


def _rechunk(batch, n):
    if batch.num_rows <= n:
        yield batch
        return
    for i in range(0, batch.num_rows, n):
        yield batch.slice(i, n)


def _zone(tz):
    if tz is None or isinstance(tz, dt.tzinfo):
        return tz
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(tz))
    except Exception:
        return None


def _scalar_for(value, t, tz=None):
    """The bound as a scalar of the column's type. For a column with a time zone, a date or a
    naive time is read in the project time zone (tz): the window [29 Oct, 31 Oct) starts at
    midnight in the project time zone, not at midnight UTC."""
    v = _as_value(value)
    if pa.types.is_timestamp(t) and t.tz and isinstance(v, dt.date):
        zone = _zone(tz)
        if zone is not None:
            if not isinstance(v, dt.datetime):
                v = dt.datetime.combine(v, dt.time(0))
            if v.tzinfo is None:
                v = v.replace(tzinfo=zone)
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        if isinstance(v, dt.datetime):
            return pa.scalar(v.strftime("%Y-%m-%d %H:%M:%S"))
        return pa.scalar(v.isoformat() if isinstance(v, dt.date) else str(v))
    try:
        return pa.scalar(v).cast(t)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
        return pa.scalar(v)


def filter_window(batch, column, window=None, watermark=None, tz=None):
    idx = batch.schema.get_field_index(column)
    if idx < 0:
        raise ValueError(f"The incremental column '{column}' is not in the file.")
    col = batch.column(idx)
    mask = None
    if window is not None:
        lo, hi = window
        if lo is not None:
            mask = pc.greater_equal(col, _scalar_for(lo, col.type, tz))
        if hi is not None:
            m = pc.less(col, _scalar_for(hi, col.type, tz))
            mask = m if mask is None else pc.and_(mask, m)
    elif watermark is not None:
        mask = pc.greater(col, _scalar_for(watermark, col.type, tz))
    if mask is None:
        return batch
    return batch.filter(pc.fill_null(mask, False))


def _select(batch, columns):
    if columns in (None, "*") or columns == ["*"]:
        return batch
    if isinstance(columns, str):
        columns = [c.strip() for c in columns.split(",") if c.strip()]
    missing = [c for c in columns if c not in batch.schema.names]
    if missing:
        raise ValueError(f"Column(s) not found in the file: {', '.join(missing)}.")
    return batch.select(columns)


def _iso_age(modified: str, now: float) -> float | None:
    try:
        d = dt.datetime.fromisoformat(str(modified).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return now - d.timestamp()


def _seen(project, job, cid, f) -> bool:
    try:
        from . import store
    except ImportError:
        return False
    fn = getattr(store, "file_seen", None)
    if fn is None:
        return False
    return bool(fn(project, job, cid, f.path, f.size, f.modified))


def apply_pickup(project, job, cid, listed, pickup=None, log=None) -> list:
    """The listed remote files a run takes: not seen before (path+size+modified), old enough to
    be complete, at most max_files, oldest first."""
    if pickup is None:
        return list(listed)
    rules = dict(PICKUP_DEFAULTS)
    rules.update({k: v for k, v in (pickup or {}).items() if v is not None})
    skip_seen = rules.get("skip_seen") not in (False, "false", "no", 0)
    min_age = float(rules.get("min_age_seconds") or 0)
    max_files = max(1, int(rules.get("max_files") or PICKUP_DEFAULTS["max_files"]))
    now = time.time()
    out, young, seen = [], 0, 0
    for f in sorted(listed, key=lambda x: (str(x.modified or ""), x.path)):
        if min_age > 0:
            age = _iso_age(f.modified, now)
            if age is not None and age < min_age:
                young += 1
                continue
        if skip_seen and job and _seen(project, job, cid, f):
            seen += 1
            continue
        out.append(f)
        if len(out) >= max_files:
            break
    if seen:
        _log(log, f"pickup: {seen} file(s) already loaded, skipped")
    if young:
        _log(log, f"pickup: {young} file(s) younger than {int(min_age)} s, left for the next run")
    if len(out) >= max_files and len(listed) > max_files:
        _log(log, f"pickup: took {max_files} file(s) (max_files)")
    return out


def _temp_dir(project) -> Path:
    try:
        base = project.stage_dir()
    except Exception:
        base = Path(project.path) / "stage"
    d = base / ".tmp" / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_files(project, cid, spec, pattern, *, fetch_dir=None, files_out=None, pickup=None, job=None,
                log=None, max_files=None):
    """([(local Path, display name)], temp dir to delete or None) for a folder, shared folder or
    remote location. Remote files are fetched into fetch_dir (incoming/) first."""
    kind = spec.get("kind")
    if kind == "folder":
        root = connections.file_path(project, spec)
        found = match_files(root, pattern, project)
        if max_files:
            found = found[:max_files]
        return [(p, p.relative_to(root).as_posix()) for p in found], None
    if kind not in connections.REMOTE:
        raise ValueError(f"Connection '{cid}' is not a folder or remote location; files are read from those.")
    from . import remote
    listed = remote.list_files(project, cid, pattern or "*", spec=spec, log=log)
    chosen = apply_pickup(project, job, cid, listed, pickup, log)
    if max_files:
        chosen = chosen[:max_files]
    if not chosen:
        return [], None
    tmp = None
    if fetch_dir is None:
        tmp = fetch_dir = _temp_dir(project)
    try:
        recs = remote.fetch(project, cid, chosen, Path(fetch_dir), log=log, spec=spec)
    except BaseException:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    if files_out is not None:
        files_out.extend(dict(r, connection=cid) for r in recs)
    return [(Path(fetch_dir) / r["local"], r["remote"]) for r in recs], tmp


def _where(project, cid, spec):
    if spec.get("kind") == "folder":
        try:
            return str(connections.file_path(project, spec))
        except Exception:
            return cid
    return f"connection '{cid}'"


def read_files(project, cid, pattern, fmt=None, options=None, chunk_rows=DEFAULT_CHUNK, log=None,
               columns="*", incremental=None, window=None, watermark=None, spec=None, limit=None,
               fetch_dir=None, files_out=None, pickup=None, job=None, checks_out=None):
    spec = spec or connections.spec_for(project, cid)
    kind = spec.get("kind")
    if kind not in connections.FILE_KINDS:
        raise ValueError(f"Connection '{cid}' is not a folder or remote location; files are read from those.")
    max_files = 1 if (limit is not None and kind in connections.REMOTE) else None
    files, tmp = local_files(project, cid, spec, pattern, fetch_dir=fetch_dir, files_out=files_out,
                             pickup=pickup, job=job, log=log, max_files=max_files)
    try:
        if not files:
            what = "No new file" if pickup is not None else "No file"
            raise FileNotFoundError(f"{what} matches '{pattern}' in {_where(project, cid, spec)}.")
        opts = dict(options or {})
        fname_col = opts.get("filename_column")
        conform = Conform(log)
        state = {}
        col = (incremental or {}).get("column")
        tz = (getattr(project, "settings", None) or {}).get("timezone") or "UTC"
        emitted = 0
        for path, shown in files:
            f = fmt or connections.guess_format(shown)
            if not f:
                try:
                    with open(path, "rb") as fh:
                        head = fh.read(65536)
                    f = connections.guess_format(shown, head, project=project)
                except OSError:
                    f = None
            if not f:
                raise ValueError(f"Cannot tell the format of {shown}; set source.format.")
            _log(log, f"reading {shown} ({f})")
            for raw in read_file(path, f, opts, chunk_rows, state, project=project, log=log,
                                 checks_out=checks_out):
                for batch in _rechunk(raw, chunk_rows):
                    _check(log)
                    if fname_col:
                        batch = batch.append_column(fname_col, pa.array([shown] * batch.num_rows, pa.string()))
                    if col and (window is not None or watermark is not None):
                        batch = filter_window(batch, col, window, watermark, tz)
                    batch = _select(batch, columns)
                    batch = conform(batch)
                    if limit is not None:
                        batch = batch.slice(0, max(limit - emitted, 0))
                    emitted += batch.num_rows
                    yield batch
                    if limit is not None and emitted >= limit:
                        return
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def read_mongo(project, cid, src, window=None, watermark=None, chunk_rows=DEFAULT_CHUNK, log=None, spec=None,
               limit=None):
    """A MongoDB collection as Arrow batches: documents flattened with dotted keys, arrays as JSON."""
    from .drivers import mongo
    spec = spec or connections.spec_for(project, cid)
    collection = src.get("table") or src.get("collection")
    if not collection:
        raise ValueError("A MongoDB source needs a collection (table).")
    inc = src.get("incremental") or {}
    db = connections.connect(project, cid, spec=spec)
    try:
        cur = mongo.cursor(db, str(collection), src.get("query"), inc.get("column"), window, watermark,
                           limit=limit, batch_size=max(1, min(int(chunk_rows), 10_000)))
        _log(log, f"extract: {collection} (MongoDB)")
        conform = Conform(log)
        buf = []
        columns = src.get("columns") or "*"
        for doc in cur:
            buf.append(mongo.flatten(doc))
            if len(buf) >= chunk_rows:
                _check(log)
                yield conform(_select(_dicts_to_batch(buf, conform.schema), columns))
                buf = []
        if buf or conform.schema is None:
            batch = _dicts_to_batch(buf, conform.schema)
            yield conform(_select(batch, columns) if buf else batch)
    finally:
        connections.close(db)


def read(project, job: dict, window, log, fetch_dir: Path | None = None, files_out: list | None = None,
         checks_out: list | None = None):
    """Stream the job's source as Arrow record batches.

    For a shared folder or remote location the files are listed, the pickup rules applied, the
    files fetched into fetch_dir (the runner passes stage/<job>/<run_id>/incoming/) and their
    records appended to files_out (the runner marks them in the ledger after the load).
    For a bank format, checks_out (a list) is filled with the format's check results when the
    stream ends, for the manifest's checks.
    """
    src = dict(job.get("source") or {})
    cid = src.get("connection")
    if not cid:
        raise ValueError("The source needs a connection.")
    spec = connections.spec_for(project, cid)
    if not spec.get("kind"):
        raise ValueError(f"There is no connection named '{cid}'.")
    kind = spec["kind"]
    chunk = int(src.get("chunk_rows") or DEFAULT_CHUNK)
    inc = src.get("incremental") or {}
    watermark = _watermark(project, job) if (window is None and inc.get("watermark")) else None
    if kind == "mongodb":
        yield from read_mongo(project, cid, src, window, watermark, chunk, log, spec)
        return
    if src.get("files") or kind in connections.FILE_KINDS:
        files = src.get("files")
        if not files and kind == "http":
            files = "*"
        if not files:
            raise ValueError("A folder source needs a files pattern.")
        pickup = None
        if kind in connections.REMOTE:
            pickup = dict(PICKUP_DEFAULTS)
            pickup.update({k: v for k, v in (src.get("pickup") or {}).items() if v is not None})
        yield from read_files(project, cid, files, src.get("format"), src.get("options"), chunk, log,
                              src.get("columns") or "*", inc, window, watermark, spec,
                              fetch_dir=fetch_dir, files_out=files_out, pickup=pickup, job=job.get("name"),
                              checks_out=checks_out)
        return
    sql, params = source_sql(kind, src, window, watermark)
    _log(log, f"extract: {sql}" + (f" with {[str(p) for p in params]}" if params else ""))
    yield from read_db(project, cid, sql, params, chunk, log, spec)


def file_schema(project, cid, name, fmt=None, options=None) -> pa.Schema:
    for batch in read_files(project, cid, name, fmt, options, chunk_rows=1000, limit=1000):
        return batch.schema
    return pa.schema([])


def preview_files(project, cid, pattern, fmt=None, options=None, limit=20) -> pa.Table:
    batches = list(read_files(project, cid, pattern, fmt, options, chunk_rows=max(limit, 1000), limit=limit))
    if not batches:
        return pa.table({})
    return pa.Table.from_batches(batches)


def preview_bank(project, cid, pattern, fmt, options=None, limit=50, spec=None) -> dict:
    """{columns: [{name, type, personal}], rows, checks} for the first matching bank file."""
    fm = _formats()
    if fm is None or fmt not in (getattr(fm, "FORMATS", None) or {}):
        raise ValueError(f"'{fmt}' is not a bank file format.")
    spec = spec or connections.spec_for(project, cid)
    files, tmp = local_files(project, cid, spec, pattern, max_files=1)
    try:
        if not files:
            raise FileNotFoundError(f"No file matches '{pattern}' in {_where(project, cid, spec)}.")
        out = dict(fm.preview(fmt, files[0][0], dict(options or {}), project=project, limit=limit))
        out["rows"] = [[connections.jsonable(v) for v in (r.values() if isinstance(r, dict) else r)]
                       for r in out.get("rows") or []]
        out.setdefault("file", files[0][1])
        return out
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def preview_mongo(project, cid, collection, options=None, limit=20, spec=None) -> pa.Table:
    opts = dict(options or {})
    src = {"table": collection, "query": opts.get("query")}
    batches = [b for b in read_mongo(project, cid, src, chunk_rows=max(limit, 1), spec=spec, limit=limit)]
    batches = [b for b in batches if b.num_columns or b.num_rows]
    if not batches:
        return pa.table({})
    return pa.Table.from_batches(batches)


def mongo_schema(project, cid, collection, spec=None) -> pa.Schema:
    return preview_mongo(project, cid, collection, limit=200, spec=spec).schema
