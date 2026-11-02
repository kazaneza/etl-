"""Shared pieces for the bank file parsers: errors, checks, bounded line reading, typed rows.

Nothing here holds a whole file. Lines are read with a size limit, rows are collected into
record batches of `chunk_rows`, and whole-file checks keep running counters only.
"""

from __future__ import annotations

import codecs
import datetime as dt
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pyarrow as pa

LOG = logging.getLogger("etlplus.formats")

MAX_LINE = 1_000_000          # a longer line is a FormatError
MAX_FAILURES_LISTED = 100     # per check name; the rest are counted


class FormatError(ValueError):
    """A bank file that cannot be read, or a check that failed under `checks: error`."""

    def __init__(self, message: str, line: int | None = None, record: str | None = None):
        super().__init__(message)
        self.line = line
        self.record = record


@dataclass(frozen=True)
class Column:
    name: str
    type: str                  # generic type (drivers.GENERIC_TYPES)
    personal: bool = False
    help: str = ""


def col(name, type_="string", personal=False, help=""):
    return Column(name, type_, personal, help)


# --------------------------------------------------------------------------- types

def arrow_type(generic: str) -> pa.DataType:
    """Generic type name (with optional precision) to an Arrow type."""
    g = generic.strip().lower()
    m = re.fullmatch(r"decimal(?:\((\d+)\s*,\s*(\d+)\))?", g)
    if m:
        p = int(m.group(1) or 18)
        s = int(m.group(2) or 3)
        return pa.decimal128(p, s)
    base = g.split("(")[0]
    return {
        "string": pa.string(), "text": pa.string(), "integer": pa.int64(), "bigint": pa.int64(),
        "double": pa.float64(), "boolean": pa.bool_(), "date": pa.date32(),
        "time": pa.time64("us"), "timestamp": pa.timestamp("us"),
        "timestamptz": pa.timestamp("us", tz="UTC"), "binary": pa.binary(), "json": pa.string(),
    }.get(base, pa.string())


def schema_of(columns) -> pa.Schema:
    return pa.schema([pa.field(c.name, arrow_type(c.type)) for c in columns])


def column_dicts(columns) -> list[dict]:
    return [{"name": c.name, "type": c.type, "personal": c.personal, "help": c.help}
            for c in columns]


class Batcher:
    """Collects row dicts and hands out record batches of `chunk_rows` rows."""

    def __init__(self, schema: pa.Schema, chunk_rows: int):
        self.schema = schema
        self.chunk_rows = max(1, int(chunk_rows))
        self.rows: list[dict] = []
        self.count = 0

    def add(self, row: dict):
        self.rows.append(row)
        self.count += 1
        if len(self.rows) >= self.chunk_rows:
            return self.flush()
        return None

    def flush(self) -> pa.RecordBatch | None:
        if not self.rows:
            return None
        names = self.schema.names
        arrays = []
        for f in self.schema:
            vals = [r.get(f.name) for r in self.rows]
            try:
                arrays.append(pa.array(vals, type=f.type))
            except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as e:
                raise FormatError(f"Column {f.name}: a value does not fit {f.type} ({e}).") from e
        self.rows = []
        return pa.RecordBatch.from_arrays(arrays, names=names)

    def empty(self) -> pa.RecordBatch:
        return pa.RecordBatch.from_pylist([], schema=self.schema)


# --------------------------------------------------------------------------- checks

class Checks:
    """Check results. mode: error (raise), warn (record as warnings), collect (record, never raise)."""

    def __init__(self, mode: str = "error", log=None, out: list | None = None):
        mode = (mode or "error").lower()
        if mode not in ("error", "warn", "collect"):
            raise FormatError(f"options.checks must be error or warn, not '{mode}'.")
        self.mode = mode
        self.log = log
        self.out = out
        self.passed: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        self.items: list[dict] = []
        self.counts: dict[str, int] = {}

    def ok(self, name: str, n: int = 1) -> None:
        self.passed[name] = self.passed.get(name, 0) + n

    def fail(self, name: str, message: str, line: int | None = None, record: str | None = None,
             level: str | None = None) -> None:
        """Record a failed check. level 'warn' keeps it a warning in every mode."""
        lvl = "warn" if (self.mode == "warn" or level == "warn") else "error"
        if self.mode == "error" and lvl == "error":
            raise FormatError(f"Check {name} failed: {message}", line=line, record=record)
        self.failed[name] = self.failed.get(name, 0) + 1
        if self.failed[name] <= MAX_FAILURES_LISTED:
            item = {"name": name, "level": lvl, "message": message}
            if line is not None:
                item["line"] = line
            self.items.append(item)
        text = f"{name}: {message}" + (f" (line {line})" if line else "")
        if self.log is not None:
            try:
                self.log(text)
            except Exception:  # a log callback never breaks the parse
                pass
        else:
            LOG.warning(text)

    def count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def result(self) -> list[dict]:
        out = []
        for name, n in self.passed.items():
            if name not in self.failed:
                out.append({"name": name, "level": "ok",
                            "message": f"{n} passed" if n != 1 else "passed"})
        out.extend(self.items)
        for name, n in self.failed.items():
            if n > MAX_FAILURES_LISTED:
                out.append({"name": name, "level": "warn" if self.mode == "warn" else "error",
                            "message": f"{n - MAX_FAILURES_LISTED} more failures not listed"})
        return out

    def finish(self) -> None:
        """Hand the results to the caller's list (the manifest's checks)."""
        if self.out is not None:
            self.out.clear()
            self.out.extend(self.result())

    @property
    def has_errors(self) -> bool:
        return any(i["level"] == "error" for i in self.items)


# --------------------------------------------------------------------------- text input

def _text_codec(name: str) -> str:
    try:
        info = codecs.lookup(name)
    except LookupError:
        raise FormatError(f"Unknown encoding '{name}'.") from None
    if not getattr(info, "_is_text_encoding", True):
        raise FormatError(f"Encoding '{name}' is not a text encoding.")
    return info.name


def pick_encoding(path: Path, requested: str | None) -> str:
    """auto: UTF-8 when the whole file decodes as UTF-8 (checked in 1 MB steps), else latin-1."""
    if requested and str(requested).lower() not in ("auto", ""):
        return _text_codec(str(requested))
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    dec.decode(b"", final=True)
                    break
                dec.decode(chunk)
    except UnicodeDecodeError:
        return "latin-1"
    return "utf-8-sig"


def iter_lines(path: Path, encoding: str):
    """(line number, text without the newline). A line over 1 MB is a FormatError."""
    with open(path, encoding=encoding, newline="") as f:
        n = 0
        while True:
            line = f.readline(MAX_LINE + 2)
            if not line:
                return
            n += 1
            if len(line) > MAX_LINE and not line.endswith(("\n", "\r")):
                raise FormatError(f"Line {n} is longer than 1 MB.", line=n)
            yield n, line.rstrip("\r\n")


def read_head(path: Path, size: int = 65536) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def head_text(head: bytes) -> str:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return head.decode("utf-16", errors="ignore")
    try:
        return head.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        if e.start > len(head) - 4:          # cut inside a character at the 64 KB edge
            return head[: e.start].decode("utf-8-sig", errors="ignore")
        return head.decode("latin-1")


# --------------------------------------------------------------------------- values

def parse_amount(text: str | None, *, comma: bool = True) -> Decimal | None:
    """'1234,56' or '1234.56' -> Decimal. None or blank -> None. Bad text raises ValueError."""
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    if comma:
        t = t.replace(",", ".")
    if t.endswith("."):
        t += "0"
    try:
        d = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not an amount") from None
    if not d.is_finite():
        raise ValueError(f"'{text}' is not an amount")
    return d


def fit(d: Decimal | None, scale: int = 3) -> Decimal | None:
    """Rounding never happens silently: more decimals than the scale is an error."""
    if d is None:
        return None
    q = Decimal(1).scaleb(-scale)
    r = d.quantize(q)
    if r != d:
        raise ValueError(f"{d} has more than {scale} decimals")
    return r


def yymmdd(text: str) -> dt.date:
    if not re.fullmatch(r"\d{6}", text or ""):
        raise ValueError(f"'{text}' is not a YYMMDD date")
    yy = int(text[:2])
    year = 2000 + yy if yy < 80 else 1900 + yy
    return dt.date(year, int(text[2:4]), int(text[4:6]))


def iso_date(text: str | None) -> dt.date | None:
    if not text:
        return None
    return dt.date.fromisoformat(text.strip()[:10])


def iso_timestamp(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    v = dt.datetime.fromisoformat(t)
    if v.tzinfo is None:
        v = v.replace(tzinfo=dt.timezone.utc)
    return v.astimezone(dt.timezone.utc)


def json_safe(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.time, dt.datetime)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


def mask_pan(value: str | None) -> str | None:
    """Keep the first 6 and last 4 digits; the rest become '*'. Already masked values stay."""
    if value is None:
        return None
    v = value.strip()
    if not v or "*" in v:
        return v or None
    digits = re.sub(r"\D", "", v)
    if len(digits) <= 4:
        return "*" * len(digits)
    if len(digits) < 11:
        return "*" * (len(digits) - 4) + digits[-4:]
    return digits[:6] + "*" * (len(digits) - 10) + digits[-4:]
