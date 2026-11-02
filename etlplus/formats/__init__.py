"""Bank file formats: SWIFT MT940/950/103/202, ISO 20022 camt.053/054, pain.001, pacs.008,
BAI2 and layout-driven fixed-width multi-record files.

    read(fmt, path, options, project=None, chunk_rows=50_000) -> Iterator[pa.RecordBatch]
    schema(fmt, options, project=None) -> pa.Schema
    check(fmt, path, options, project=None) -> {"ok", "checks", "counts"}
    preview(fmt, path, options, project=None, limit=50) -> {"columns", "rows", "checks"}
    detect(path, head=None) -> [{"format", "confidence", "reason", "options"}]
    catalog() -> list[dict]

`options.checks` is `error` (the default: a failed check raises FormatError, so the extract
fails and nothing is published) or `warn` (failures are logged and handed back through
`checks_out`, the list that goes into the manifest). Every row carries `file_name` and
`record_line`. Everything here was tested on made-up sample files only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow as pa

from . import layouts
from ._common import (Checks, Column, FormatError, column_dicts, head_text, json_safe,
                      read_head, schema_of)

__all__ = ["Column", "FormatInfo", "FormatError", "FORMATS", "read", "schema", "check",
           "preview", "detect", "catalog", "layouts", "Option"]

BANK = "Bank files"


@dataclass(frozen=True)
class Option:
    """drivers.Field-like option spec for the console."""
    name: str
    label: str
    type: str = "text"            # text | int | select | bool
    required: bool = False
    default: object = None
    choices: tuple = ()
    secret: bool = False
    endpoint: bool = False
    help: str = ""


@dataclass(frozen=True)
class FormatInfo:
    id: str
    label: str
    group: str
    extensions: tuple
    options: tuple
    columns: tuple | None
    needs_layout: bool = False
    status: str = "simulated"
    verify_note: str | None = None


_CHECKS = Option("checks", "On a failed check", "select", False, "error", ("error", "warn"),
                 help="error stops the run; warn logs and goes on")
_ENC = Option("encoding", "Encoding", "text", False, "auto",
              help="auto tries UTF-8, then latin-1")
_LEVEL = Option("level", "One row per", "select", False, "entry", ("entry", "transaction"),
                help="entry (Ntry) or transaction (TxDtls)")
_IMPLIED = Option("implied_decimals", "Implied decimals", "int", False, 2,
                  help="Digits after the point in every amount")
_LAYOUT = Option("layout", "Layout", "select", True, None,
                 help="A built-in template or one of your project's layouts")

_NOTE = "Built from the public standard and tested on made-up files only. Check it on a file from your bank."


def _info():
    from .bai2 import BAI2_COLUMNS
    from .iso import CAMT_COLUMNS, PACS_COLUMNS, PAIN_COLUMNS
    from .swift import MT103_COLUMNS, MT202_COLUMNS, MT940_COLUMNS
    mt950 = tuple(c for c in MT940_COLUMNS)
    items = [
        FormatInfo("mt940", "SWIFT MT940 statement", BANK, (".txt", ".sta", ".940", ".mt940"),
                   (_CHECKS, _ENC), MT940_COLUMNS, verify_note=_NOTE),
        FormatInfo("mt950", "SWIFT MT950 statement", BANK, (".txt", ".950", ".mt950"),
                   (_CHECKS, _ENC), mt950, verify_note=_NOTE),
        FormatInfo("mt103", "SWIFT MT103 payment", BANK, (".txt", ".fin", ".103", ".mt103"),
                   (_CHECKS, _ENC), MT103_COLUMNS, verify_note=_NOTE),
        FormatInfo("mt202", "SWIFT MT202 bank transfer", BANK, (".txt", ".fin", ".202", ".mt202"),
                   (_CHECKS, _ENC), MT202_COLUMNS, verify_note=_NOTE),
        FormatInfo("camt053", "ISO 20022 camt.053 statement", BANK, (".xml",),
                   (_CHECKS, _ENC, _LEVEL), CAMT_COLUMNS, verify_note=_NOTE),
        FormatInfo("camt054", "ISO 20022 camt.054 notification", BANK, (".xml",),
                   (_CHECKS, _ENC, _LEVEL), CAMT_COLUMNS, verify_note=_NOTE),
        FormatInfo("pain001", "ISO 20022 pain.001 payment order", BANK, (".xml",),
                   (_CHECKS, _ENC), PAIN_COLUMNS, verify_note=_NOTE),
        FormatInfo("pacs008", "ISO 20022 pacs.008 credit transfer", BANK, (".xml",),
                   (_CHECKS, _ENC), PACS_COLUMNS, verify_note=_NOTE),
        FormatInfo("bai2", "BAI2 cash report", BANK, (".bai", ".bai2", ".txt"),
                   (_CHECKS, _ENC, _IMPLIED), BAI2_COLUMNS, verify_note=_NOTE),
        FormatInfo("layout", "Fixed width with a layout", BANK, (".txt", ".dat"),
                   (_LAYOUT, _CHECKS, _ENC), None, needs_layout=True,
                   verify_note="A layout is only as right as its positions. Check them against "
                               "your provider's file specification."),
    ]
    return {i.id: i for i in items}


FORMATS: dict[str, FormatInfo] = _info()


def _get(fmt) -> FormatInfo:
    try:
        return FORMATS[fmt]
    except KeyError:
        raise FormatError(f"Unknown bank format '{fmt}'. Use one of: "
                          f"{', '.join(FORMATS)}.") from None


def _columns(fmt, options, project=None, compiled=None):
    info = _get(fmt)
    if info.columns is not None:
        return info.columns
    return (compiled or layouts.compiled(options or {}, project)).columns()


def schema(fmt, options=None, *, project=None) -> pa.Schema:
    return schema_of(_columns(fmt, options or {}, project))


def _stream(fmt, path, options, checks, chunk_rows, project):
    from .bai2 import read_bai2
    from .iso import read_iso
    from .swift import read_mt940, read_payment
    if fmt in ("mt940", "mt950"):
        return read_mt940(path, options, checks, chunk_rows, fmt)
    if fmt in ("mt103", "mt202"):
        return read_payment(path, options, checks, chunk_rows, fmt)
    if fmt in ("camt053", "camt054", "pain001", "pacs008"):
        return read_iso(path, options, checks, chunk_rows, fmt)
    if fmt == "bai2":
        return read_bai2(path, options, checks, chunk_rows, fmt)
    return layouts.read_layout(path, options, checks, chunk_rows, project)


def read(fmt, path: Path, options: dict | None = None, *, project=None, chunk_rows=50_000,
         log=None, checks_out: list | None = None) -> Iterator[pa.RecordBatch]:
    """Stream a bank file as record batches.

    `log(msg)` receives every failed check under `checks: warn`; `checks_out` (a list) is
    filled with the check results when the stream ends, for the manifest.
    """
    _get(fmt)
    options = dict(options or {})
    path = Path(path)
    checks = Checks(options.get("checks", "error"), log=log, out=checks_out)
    yield from _stream(fmt, path, options, checks, chunk_rows, project)
    checks.finish()


def check(fmt, path, options=None, *, project=None) -> dict:
    """Parse the whole file (streaming, nothing kept) and report every check."""
    options = dict(options or {})
    checks = Checks("collect")
    fatal = None
    try:
        _get(fmt)
        for _ in _stream(fmt, Path(path), options, checks, 50_000, project):
            pass
    except FormatError as e:
        fatal = {"name": "read", "level": "error", "message": str(e)}
        if e.line is not None:
            fatal["line"] = e.line
    except (OSError, KeyError, ValueError, ArithmeticError) as e:
        # ArithmeticError: decimal.InvalidOperation on a value no check caught; a failed check, not a crash
        fatal = {"name": "read", "level": "error", "message": str(e) or e.__class__.__name__}
    res = checks.result()
    if fatal:
        res.append(fatal)
    ok = not any(c["level"] == "error" for c in res)
    return {"ok": ok, "checks": res, "counts": dict(checks.counts)}


def preview(fmt, path, options=None, *, project=None, limit=50) -> dict:
    """The first `limit` rows (JSON-safe), the columns, and the checks seen so far."""
    options = dict(options or {})
    limit = max(1, min(int(limit), 1000))
    checks = Checks("collect")
    compiled = None
    if _get(fmt).needs_layout:
        compiled = layouts.compiled(options, project)
    cols = _columns(fmt, options, project, compiled)
    rows = []
    fatal = None
    try:
        if compiled is not None:
            gen = layouts.read_layout(Path(path), options, checks, limit, project, compiled)
        else:
            gen = _stream(fmt, Path(path), options, checks, limit, project)
        try:
            for batch in gen:
                for r in batch.to_pylist():
                    rows.append([json_safe(r[c.name]) for c in cols])
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit:
                    break
        finally:
            gen.close()
    except FormatError as e:
        fatal = {"name": "read", "level": "error", "message": str(e)}
        if e.line is not None:
            fatal["line"] = e.line
    except ArithmeticError as e:
        fatal = {"name": "read", "level": "error", "message": str(e) or e.__class__.__name__}
    res = checks.result()
    if fatal:
        res.append(fatal)
    return {"columns": [{"name": c.name, "type": c.type, "personal": c.personal}
                        for c in cols], "rows": rows, "checks": res}


# --------------------------------------------------------------------------- detection

_XML_NS = re.compile(r"""xmlns(?::\w+)?\s*=\s*["']([^"']+)["']""")
_ROOTS = {"BkToCstmrStmt": "camt053", "BkToCstmrDbtCdtNtfctn": "camt054",
          "CstmrCdtTrfInitn": "pain001", "FIToFICstmrCdtTrf": "pacs008"}
_NS_IDS = {"camt.053": "camt053", "camt.054": "camt054", "pain.001": "pain001",
           "pacs.008": "pacs008"}


def _detect_xml(text: str) -> list[dict]:
    out = []
    for ns in _XML_NS.findall(text[:4096]):
        for key, fmt in _NS_IDS.items():
            if key in ns:
                out.append({"format": fmt, "confidence": 0.99,
                            "reason": f"The XML namespace is {ns}", "options": {}})
    if not out:
        for el, fmt in _ROOTS.items():
            if re.search(r"<(?:\w+:)?" + el + r"[\s>]", text):
                out.append({"format": fmt, "confidence": 0.9,
                            "reason": f"The XML holds {el}", "options": {}})
    return out


def _detect_swift(text: str) -> list[dict]:
    out = []
    m = re.search(r"\{2:[IO](\d{3})", text)
    if m and m.group(1) in ("940", "950", "103", "202"):
        mt = m.group(1)
        out.append({"format": f"mt{mt}", "confidence": 0.99,
                    "reason": f"SWIFT block 2 names an MT{mt}", "options": {}})
        return out

    def has(tag):
        return re.search(r"(?m)^:" + tag + ":", text) is not None
    if has("20") and has("25") and has("60[FM]") and has("62[FM]"):
        if has("86"):
            out.append({"format": "mt940", "confidence": 0.9,
                        "reason": "Bare SWIFT statement fields with :86:", "options": {}})
        else:
            out.append({"format": "mt950", "confidence": 0.7,
                        "reason": "Bare SWIFT statement fields without :86:", "options": {}})
    elif has("23B") and has("32A") and re.search(r"(?m)^:59[A-Z]?:", text):
        out.append({"format": "mt103", "confidence": 0.8,
                    "reason": "Bare SWIFT fields :23B:, :32A: and :59:", "options": {}})
    elif has("21") and has("32A") and has("58A") and not re.search(r"(?m)^:59", text):
        out.append({"format": "mt202", "confidence": 0.75,
                    "reason": "Bare SWIFT fields :21:, :32A: and :58A:", "options": {}})
    return out


def _detect_bai2(lines: list[str], complete: bool) -> list[dict]:
    if not lines or not lines[0].startswith("01,"):
        return []
    if complete and lines[-1].startswith("99,"):
        return [{"format": "bai2", "confidence": 0.95,
                 "reason": "The first record is 01 and the last is 99", "options": {}}]
    if not complete and any(x[:3] in ("02,", "03,", "16,") for x in lines[1:20]):
        return [{"format": "bai2", "confidence": 0.9,
                 "reason": "The first record is 01, followed by BAI2 records", "options": {}}]
    return []


def detect(path: Path, head: bytes | None = None, *, project=None) -> list[dict]:
    """Best guesses, best first. Reads at most 64 KB. CSV and JSON are not detected here."""
    if head is None:
        head = read_head(Path(path))
    head = head[:65536]
    complete = len(head) < 65536
    text = head_text(head)
    out: list[dict] = []
    stripped = text.lstrip()
    if stripped.startswith("<"):
        out.extend(_detect_xml(text))
    else:
        out.extend(_detect_swift(text))
        lines = text.splitlines()
        if not complete and lines:
            lines = lines[:-1]          # the cut line is not judged
        clean = [x.strip() for x in lines if x.strip()]
        out.extend(_detect_bai2(clean, complete))
        try:
            out.extend(layouts.detect([x.rstrip("\r\n") for x in lines], project))
        except Exception:  # a broken project layout never breaks detection
            pass
    out.sort(key=lambda d: -d["confidence"])
    return out


def catalog() -> list[dict]:
    out = []
    for i in FORMATS.values():
        out.append({
            "id": i.id, "label": i.label, "group": i.group, "extensions": [*i.extensions],
            "options": [{"name": o.name, "label": o.label, "type": o.type,
                         "required": o.required, "default": o.default,
                         "choices": [*o.choices], "help": o.help} for o in i.options],
            "columns": column_dicts(i.columns) if i.columns is not None else None,
            "needs_layout": i.needs_layout, "status": i.status, "verify_note": i.verify_note,
        })
    return out
