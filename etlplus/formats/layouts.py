"""Layout-driven fixed-width files with several record types (header, detail, trailer, ...).

A layout is YAML (read with yaml.safe_load only). Built-in templates live in this package's
`layouts/` folder and are never written to; a project keeps its own copies in
`<project>/layouts/`, and a project copy wins over a built-in of the same name.

Module functions (section 5.7 of SPEC_V2):

    list(project) -> list[dict]          load(name, project) -> dict
    save(project, name, data)            delete(project, name)
    copy(project, builtin_name, new_name)
    validate_layout(data, warnings=None) -> list[str]
    read_layout(...)                     (used by formats.read)
    format_record(layout, record, values) -> str   (a writer, used by the samples and tests)
"""

from __future__ import annotations

import builtins
import codecs
import datetime as dt
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from ._common import (Batcher, Checks, Column, FormatError, iter_lines, mask_pan, schema_of)

NAME_RE = re.compile(r"^[a-z0-9_]{1,60}$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TYPES = ("string", "integer", "decimal", "date", "time", "timestamp", "boolean")
DEFAULT_FORMATS = {"date": "%Y%m%d", "time": "%H%M%S", "timestamp": "%Y%m%d%H%M%S"}
MAX_LINE_LENGTH = 10_000
MAX_LAYOUT_BYTES = 1_000_000
PACKAGE_DIR = Path(__file__).resolve().parent / "layouts"
TRUE_VALUES = ("Y", "YES", "1", "T", "TRUE")


class LayoutError(ValueError):
    """A layout that does not validate; `problems` lists every sentence."""

    def __init__(self, problems):
        self.problems = [*problems]
        super().__init__("The layout has problems: " + " ".join(self.problems))


# --------------------------------------------------------------------------- names and files

def check_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("A layout name is 1 to 60 characters: lower-case letters, digits and _.")
    return name


def _project_dir(project) -> Path | None:
    if project is None:
        return None
    p = getattr(project, "path", project)
    return Path(p).resolve() / "layouts"


def _read_yaml(path: Path) -> dict:
    if path.stat().st_size > MAX_LAYOUT_BYTES:
        raise ValueError(f"Layout file {path.name} is larger than 1 MB.")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Layout file {path.name} does not hold a mapping.")
    return data


def _builtin_path(name: str) -> Path:
    return PACKAGE_DIR / f"{check_name(name)}.yaml"


def _project_path(project, name: str) -> Path | None:
    d = _project_dir(project)
    return None if d is None else d / f"{check_name(name)}.yaml"


def list_layouts(project=None) -> list[dict]:
    seen: dict[str, dict] = {}
    sources = [("builtin", PACKAGE_DIR)]
    pd = _project_dir(project)
    if pd is not None:
        sources.append(("project", pd))
    for source, folder in sources:
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.yaml")):
            if not NAME_RE.match(f.stem):
                continue
            try:
                data = _read_yaml(f)
            except Exception as e:  # a broken project file is listed, not fatal
                seen[f.stem] = {"name": f.stem, "label": f.stem, "template": False,
                                "verify": None, "source": source, "error": str(e)}
                continue
            seen[f.stem] = {"name": f.stem, "label": str(data.get("label") or f.stem),
                            "template": bool(data.get("template")) and source == "builtin",
                            "verify": data.get("verify"), "source": source,
                            "line_length": data.get("line_length")}
    return [seen[k] for k in sorted(seen)]


def load(name, project=None) -> dict:
    check_name(name)
    pp = _project_path(project, name)
    if pp is not None and pp.is_file():
        data = _read_yaml(pp)
    else:
        bp = _builtin_path(name)
        if not bp.is_file():
            raise KeyError(f"No layout named '{name}'.")
        data = _read_yaml(bp)
    data.setdefault("name", name)
    return data


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def save(project, name, data) -> None:
    check_name(name)
    if project is None:
        raise ValueError("A layout is saved into a project, never into the package.")
    if not isinstance(data, dict):
        raise LayoutError(["A layout is a mapping of keys to values."])
    data = dict(data)
    data["name"] = name
    data.pop("source", None)
    problems = validate_layout(data)
    if problems:
        raise LayoutError(problems)
    folder = _project_dir(project)
    pkg_root = Path(__file__).resolve().parent.parent
    if _inside(folder, pkg_root):
        raise ValueError("A layout is never written into the etl+ package.")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.yaml"
    part = folder / f".{name}.yaml.partial"
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    with open(part, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, target)


def delete(project, name) -> None:
    check_name(name)
    pp = _project_path(project, name)
    if pp is None or not pp.is_file():
        if _builtin_path(name).is_file():
            raise ValueError(f"'{name}' is a built-in template; only project copies are deleted.")
        raise KeyError(f"No layout named '{name}' in this project.")
    pp.unlink()


def copy(project, builtin_name, new_name) -> None:
    data = load(builtin_name, project)
    check_name(new_name)
    pp = _project_path(project, new_name)
    if pp is not None and pp.exists():
        raise ValueError(f"A layout named '{new_name}' already exists in this project.")
    data = dict(data)
    data["template"] = False
    data["name"] = new_name
    if new_name != builtin_name and data.get("label"):
        data["label"] = f"{data['label']} (copy)"
    save(project, new_name, data)


# --------------------------------------------------------------------------- validation

def _span(f: dict):
    """(start, end) 1-based inclusive, or None when the keys are wrong."""
    try:
        start = int(f.get("start"))
        if "length" in f and f.get("length") is not None:
            end = start + int(f.get("length")) - 1
        else:
            end = int(f.get("end"))
    except (TypeError, ValueError):
        return None
    if start < 1 or end < start:
        return None
    return start, end


def _format_ok(fmt: str, type_: str) -> bool:
    if not isinstance(fmt, str) or not fmt or len(fmt) > 100:
        return False
    sample = dt.datetime(2026, 12, 31, 23, 59, 58)
    try:
        text = sample.strftime(fmt)
        back = dt.datetime.strptime(text, fmt)
    except (ValueError, TypeError):
        return False
    if type_ == "date":
        return back.date() == sample.date()
    if type_ == "time":
        return back.time() == sample.time() or (back.hour, back.minute) == (23, 59)
    return back.date() == sample.date()


def _codes(rec) -> list[str]:
    m = rec.get("match")
    if m is None:
        return []
    if isinstance(m, (builtins.list, tuple)):
        return [str(x) for x in m]
    return [str(m)]


def validate_layout(data, warnings: builtins.list | None = None) -> builtins.list[str]:
    """Every problem is a plain sentence naming the record and the field. Overlaps are
    warnings (appended to `warnings` when given), never problems."""
    p: builtins.list[str] = []
    w: builtins.list[str] = []
    if not isinstance(data, dict):
        return ["A layout is a mapping of keys to values."]
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
        p.append("The layout name is 1 to 60 characters: lower-case letters, digits and _.")
    enc = data.get("encoding", "latin-1")
    try:
        info = codecs.lookup(str(enc))
        if not getattr(info, "_is_text_encoding", True):
            p.append(f"Encoding '{enc}' is not a text encoding.")
    except LookupError:
        p.append(f"Encoding '{enc}' is unknown.")
    ll = data.get("line_length")
    if ll is not None:
        if not isinstance(ll, int) or isinstance(ll, bool) or not 1 <= ll <= MAX_LINE_LENGTH:
            p.append(f"line_length must be a whole number from 1 to {MAX_LINE_LENGTH}.")
            ll = None
    if data.get("trim", "right") not in ("right", "both", "none"):
        p.append("trim must be right, both or none.")
    records = data.get("records")
    if not isinstance(records, builtins.list) or not records:
        p.append("The layout needs at least one record in records.")
        if warnings is not None:
            warnings.extend(w)
        return p
    rt = data.get("record_type")
    rt_len = None
    if rt is None:
        if len(records) != 1:
            p.append("record_type (start and length) is needed when there is more than one record.")
    else:
        sp = _span(rt) if isinstance(rt, dict) else None
        if sp is None:
            p.append("record_type needs a start and a length of 1 or more.")
        else:
            rt_len = sp[1] - sp[0] + 1
            if ll is not None and sp[1] > ll:
                p.append("record_type lies beyond line_length.")
    types_seen = set()
    fields_by_rec: dict[str, set] = {}
    outputs = 0
    codes_seen: dict[str, str] = {}
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            p.append(f"Record {i + 1} is not a mapping.")
            continue
        rtype = rec.get("type")
        label = f"Record '{rtype}'" if rtype else f"Record {i + 1}"
        if not isinstance(rtype, str) or not FIELD_NAME_RE.match(rtype):
            p.append(f"{label}: type must be a word (header, detail, trailer or your own).")
            rtype = f"#{i + 1}"
        elif rtype in types_seen:
            p.append(f"{label}: the type is used by two records.")
        types_seen.add(rtype)
        if rec.get("output"):
            outputs += 1
        occ = rec.get("occurs", "many")
        if occ not in ("first", "last", "once", "many"):
            p.append(f"{label}: occurs must be first, last, once or many.")
        codes = _codes(rec)
        if rt is not None:
            if not codes:
                p.append(f"{label}: match needs a record code.")
            for c in codes:
                if rt_len is not None and len(c) != rt_len:
                    p.append(f"{label}: match code '{c}' is {len(c)} characters; record_type "
                             f"is {rt_len}.")
                if c in codes_seen and codes_seen[c] != rtype:
                    p.append(f"{label}: match code '{c}' is also used by record "
                             f"'{codes_seen[c]}'.")
                codes_seen[c] = rtype
        fields = rec.get("fields") or []
        if not isinstance(fields, builtins.list):
            p.append(f"{label}: fields must be a list.")
            fields = []
        names = set()
        spans = []
        for j, f in enumerate(fields):
            if not isinstance(f, dict):
                p.append(f"{label}, field {j + 1}: not a mapping.")
                continue
            fname = f.get("name")
            fl = f"{label}, field '{fname}'" if fname else f"{label}, field {j + 1}"
            if not isinstance(fname, str) or not FIELD_NAME_RE.match(fname):
                p.append(f"{fl}: the name must start with a letter and hold letters, digits, _.")
            elif fname in names:
                p.append(f"{fl}: the name is used twice in this record.")
            elif fname in ("file_name", "record_line"):
                p.append(f"{fl}: the name is kept for etl+ (file_name, record_line).")
            names.add(fname)
            sp = _span(f)
            if sp is None:
                p.append(f"{fl}: start must be 1 or more, with a length of 1 or more (or an end).")
            else:
                spans.append((sp, fname))
                if ll is not None and sp[1] > ll:
                    p.append(f"{fl}: ends at {sp[1]}, beyond line_length {ll}.")
                if sp[1] > MAX_LINE_LENGTH:
                    p.append(f"{fl}: ends beyond {MAX_LINE_LENGTH}.")
            t = f.get("type", "string")
            if t not in TYPES:
                p.append(f"{fl}: type must be one of {', '.join(TYPES)}.")
                continue
            if "format" in f and t not in DEFAULT_FORMATS:
                p.append(f"{fl}: format is only for date, time and timestamp.")
            if t in DEFAULT_FORMATS and "format" in f and not _format_ok(f.get("format"), t):
                p.append(f"{fl}: format '{f.get('format')}' is not a valid {t} format.")
            if "scale" in f:
                s = f.get("scale")
                if not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 9:
                    p.append(f"{fl}: scale must be a whole number from 0 to 9.")
                elif t != "decimal":
                    p.append(f"{fl}: scale is only for decimal fields.")
            if f.get("implied_decimal") and t != "decimal":
                p.append(f"{fl}: implied_decimal is only for decimal fields.")
            sign = f.get("sign")
            if sign is not None:
                if t not in ("integer", "decimal"):
                    p.append(f"{fl}: sign is only for integer and decimal fields.")
                elif not isinstance(sign, str) or not (
                        sign in ("leading", "trailing", "none") or sign.startswith("separate:")):
                    p.append(f"{fl}: sign must be leading, trailing, none or separate:<field>.")
            mask = f.get("mask")
            if mask is not None and mask not in ("pan", "none"):
                p.append(f"{fl}: mask must be pan or none.")
            if mask == "pan" and t != "string":
                p.append(f"{fl}: mask pan is only for string fields.")
            tv = f.get("true_values")
            if tv is not None and (t != "boolean" or not isinstance(tv, builtins.list)):
                p.append(f"{fl}: true_values is a list, for boolean fields only.")
        for f in fields:
            if isinstance(f, dict) and isinstance(f.get("sign"), str) \
                    and f["sign"].startswith("separate:"):
                other = f["sign"].split(":", 1)[1]
                if other not in names:
                    p.append(f"{label}, field '{f.get('name')}': sign field '{other}' does not "
                             f"exist in this record.")
        spans.sort()
        for (a, an), (b, bn) in zip(spans, spans[1:]):
            if b[0] <= a[1]:
                w.append(f"{label}: fields '{an}' and '{bn}' overlap at positions "
                         f"{b[0]}..{min(a[1], b[1])}.")
        fields_by_rec[rtype] = names
    if outputs != 1:
        p.append(f"Exactly one record has output: true; this layout has {outputs}.")

    def ref_ok(ref, what):
        if not isinstance(ref, str) or "." not in ref:
            p.append(f"{what} '{ref}' must be record.field.")
            return False
        r, f = ref.split(".", 1)
        if r not in fields_by_rec:
            p.append(f"{what} '{ref}': there is no record '{r}'.")
            return False
        if f not in fields_by_rec[r]:
            p.append(f"{what} '{ref}': record '{r}' has no field '{f}'.")
            return False
        return True

    carry = data.get("carry") or []
    if not isinstance(carry, builtins.list):
        p.append("carry must be a list of record.field.")
        carry = []
    for c in carry:
        ref_ok(c, "Carry")
    checks = data.get("checks") or []
    if not isinstance(checks, builtins.list):
        p.append("checks must be a list.")
        checks = []
    for k, c in enumerate(checks):
        if not isinstance(c, dict):
            p.append(f"Check {k + 1} is not a mapping.")
            continue
        kind = c.get("kind")
        if c.get("level", "error") not in ("error", "warn"):
            p.append(f"Check {k + 1} ({kind}): level must be error or warn.")
        if kind == "count":
            ref_ok(c.get("field"), f"Check {k + 1} (count) field")
            if c.get("of") not in fields_by_rec:
                p.append(f"Check {k + 1} (count): 'of' must name a record; '{c.get('of')}' "
                         f"does not exist.")
        elif kind == "sum":
            ref_ok(c.get("field"), f"Check {k + 1} (sum) field")
            ref_ok(c.get("of"), f"Check {k + 1} (sum) of")
        elif kind != "unknown_records":
            p.append(f"Check {k + 1}: kind must be count, sum or unknown_records.")
    if warnings is not None:
        warnings.extend(w)
    return p


def check_layout(data) -> dict:
    """{"problems": [...], "warnings": [...]} for the API and the editor."""
    w: builtins.list[str] = []
    return {"problems": validate_layout(data, w), "warnings": w}


# --------------------------------------------------------------------------- compiled form

class _Field:
    __slots__ = ("name", "a", "b", "type", "fmt", "scale", "implied", "sign", "sep", "true",
                 "mask", "personal", "nullable")

    def __init__(self, f: dict):
        start, end = _span(f)
        self.name = f["name"]
        self.a, self.b = start - 1, end
        self.type = f.get("type", "string")
        self.fmt = f.get("format") or DEFAULT_FORMATS.get(self.type)
        self.implied = bool(f.get("implied_decimal"))
        self.scale = int(f.get("scale", 2 if self.implied else 3))
        sign = f.get("sign")
        self.sep = None
        if isinstance(sign, str) and sign.startswith("separate:"):
            self.sep = sign.split(":", 1)[1]
            sign = "separate"
        self.sign = sign
        self.true = tuple(str(x).upper() for x in (f.get("true_values") or TRUE_VALUES))
        self.mask = f.get("mask")
        self.personal = bool(f.get("personal")) or self.mask == "pan"
        self.nullable = f.get("nullable", True) is not False

    @property
    def generic(self) -> str:
        if self.type == "decimal":
            return f"decimal(18,{self.scale})"
        return self.type


class Compiled:
    def __init__(self, data: dict):
        self.data = data
        self.name = data.get("name")
        self.encoding = str(data.get("encoding", "latin-1"))
        self.line_length = data.get("line_length")
        self.trim = data.get("trim", "right")
        self.blank_as_null = data.get("blank_as_null", True) is not False
        rt = data.get("record_type")
        if rt:
            s, e = _span(rt)
            self.rt = (s - 1, e)
        else:
            self.rt = None
        self.records = []
        self.by_code: dict[str, dict] = {}
        self.output = None
        for rec in data["records"]:
            r = {"type": rec["type"], "occurs": rec.get("occurs", "many"),
                 "output": bool(rec.get("output")),
                 "fields": [_Field(f) for f in rec.get("fields") or []]}
            r["by_name"] = {f.name: f for f in r["fields"]}
            self.records.append(r)
            for c in _codes(rec):
                self.by_code[c] = r
            if r["output"]:
                self.output = r
        self.rec_by_type = {r["type"]: r for r in self.records}
        self.carry = [tuple(c.split(".", 1)) for c in data.get("carry") or []]
        self.checks = data.get("checks") or []

    def columns(self) -> tuple[Column, ...]:
        cols = [Column("file_name", "string", False, "The file the row came from"),
                Column("record_line", "integer", False, "Line of the record in the file")]
        for f in self.output["fields"]:
            cols.append(Column(f.name, f.generic, f.personal))
        for r, f in self.carry:
            fld = self.rec_by_type[r]["by_name"][f]
            cols.append(Column(f"{r}_{f}", fld.generic, fld.personal, f"From the {r} record"))
        return tuple(cols)

    def code_of(self, line: str):
        if self.rt is None:
            return None
        return line[self.rt[0]:self.rt[1]]

    def record_for(self, line: str):
        if self.rt is None:
            return self.records[0]
        return self.by_code.get(self.code_of(line))


def _signed(text: str, fld: _Field, raw_values: dict) -> tuple[str, bool]:
    """(digits text, negative)."""
    t = text.strip()
    neg = False
    if fld.sign == "leading":
        if t[:1] in "+-" and t:
            neg = t[0] == "-"
            t = t[1:].strip()
    elif fld.sign == "trailing":
        if t and t[-1] in "+-":
            neg = t[-1] == "-"
            t = t[:-1].strip()
    elif fld.sign == "separate":
        s = (raw_values.get(fld.sep) or "").strip().upper()
        neg = s in ("-", "D", "DR")
    elif fld.sign is None:
        if t[:1] in "+-" and t:
            neg = t[0] == "-"
            t = t[1:].strip()
    return t, neg


def convert(fld: _Field, raw: str, raw_values: dict, trim: str, blank_as_null: bool):
    """A field's text to its value. Raises ValueError with a plain reason."""
    if fld.type == "string":
        v = raw.rstrip() if trim == "right" else raw.strip() if trim == "both" else raw
        if v.strip() == "" and blank_as_null:
            return None
        if fld.mask == "pan":
            return mask_pan(v)
        return v
    t = raw.strip()
    if not t:
        return None
    if fld.type in ("integer", "decimal"):
        digits, neg = _signed(t, fld, raw_values)
        if fld.type == "integer":
            if not (digits.isascii() and digits.isdigit()):
                raise ValueError(f"'{t}' is not a whole number")
            v = int(digits)
            return -v if neg else v
        if fld.implied:
            if not (digits.isascii() and digits.isdigit()):
                raise ValueError(f"'{t}' is not an amount in minor units")
            v = Decimal(digits).scaleb(-fld.scale)
        else:
            try:
                v = Decimal(digits.replace(",", "."))
            except InvalidOperation:
                raise ValueError(f"'{t}' is not a number") from None
            if not v.is_finite():
                raise ValueError(f"'{t}' is not a number")
        q = Decimal(1).scaleb(-fld.scale)
        r = v.quantize(q)
        if r != v:
            raise ValueError(f"'{t}' has more than {fld.scale} decimals")
        return -r if neg else r
    if fld.type == "boolean":
        return t.upper() in fld.true
    try:
        v = dt.datetime.strptime(t, fld.fmt)
    except ValueError:
        raise ValueError(f"'{t}' does not match the format {fld.fmt}") from None
    if fld.type == "date":
        return v.date()
    if fld.type == "time":
        return v.time()
    return v


def resolve(options: dict, project=None) -> dict:
    lay = options.get("layout")
    if isinstance(lay, dict):
        data = dict(lay)
    elif isinstance(lay, str):
        try:
            data = load(lay, project)
        except (ValueError, KeyError, OSError, yaml.YAMLError) as e:
            raise FormatError(str(e).strip("'\"")) from None
    else:
        raise FormatError("The layout format needs options.layout (a layout name).")
    problems = validate_layout(data)
    if problems:
        raise FormatError("The layout has problems: " + " ".join(problems))
    return data


def compiled(options: dict, project=None) -> Compiled:
    return Compiled(resolve(options, project))


def read_layout(path: Path, options: dict, checks: Checks, chunk_rows: int, project=None,
                layout: Compiled | None = None):
    L = layout or compiled(options, project)
    cols = L.columns()
    batcher = Batcher(schema_of(cols), chunk_rows)
    enc = options.get("encoding") or L.encoding
    if str(enc).lower() == "auto":
        enc = L.encoding
    lookup = codecs.lookup(str(enc))
    if not getattr(lookup, "_is_text_encoding", True):
        raise FormatError(f"Encoding '{enc}' is not a text encoding.")
    unknown_level = "error"
    for c in L.checks:
        if c.get("kind") == "unknown_records":
            unknown_level = c.get("level", "error")
    counts = {r["type"]: 0 for r in L.records}
    first_line_of = {}
    last_seen: dict[str, dict] = {}
    sums: dict[str, Decimal] = {}
    sum_refs = set()
    for c in L.checks:
        if c.get("kind") == "sum":
            sum_refs.add(tuple(c["of"].split(".", 1)))
    for r, f in sum_refs:
        sums[f"{r}.{f}"] = Decimal(0)
    produced = False
    line_no_first = None
    line_no_last = None
    type_first = None
    type_last = None
    bad_len = 0
    for n, line in iter_lines(path, lookup.name):
        if not line.strip():
            continue
        if line_no_first is None:
            line_no_first = n
        line_no_last = n
        if L.line_length is not None and len(line) != L.line_length:
            bad_len += 1
            checks.fail("line_length", f"Line {n} is {len(line)} characters; the layout says "
                        f"{L.line_length}.", line=n)
        rec = L.record_for(line)
        if rec is None:
            checks.fail("unknown_records", f"Line {n}: record code '{L.code_of(line)}' matches "
                        f"no record of the layout.", line=n, level=unknown_level)
            type_last = None
            continue
        rtype = rec["type"]
        counts[rtype] += 1
        first_line_of.setdefault(rtype, n)
        if type_first is None:
            type_first = rtype
        type_last = rtype
        raw_values = {f.name: line[f.a:f.b] for f in rec["fields"]}
        values = {}
        for f in rec["fields"]:
            try:
                v = convert(f, raw_values[f.name], raw_values, L.trim, L.blank_as_null)
            except ValueError as e:
                checks.fail("values", f"Line {n}, record '{rtype}', field '{f.name}' "
                            f"(positions {f.a + 1}..{f.b}): {e}.", line=n, record=rtype)
                v = None
            if v is None and not f.nullable:
                checks.fail("values", f"Line {n}, record '{rtype}', field '{f.name}': a value "
                            f"is required.", line=n, record=rtype)
            values[f.name] = v
        for (r, fname) in sum_refs:
            if r == rtype and values.get(fname) is not None:
                sums[f"{r}.{fname}"] += Decimal(values[fname])
        if rec["output"]:
            row = {"file_name": path.name, "record_line": n, **values}
            for r, fname in L.carry:
                row[f"{r}_{fname}"] = last_seen.get(r, {}).get(fname)
            b = batcher.add(row)
            if b is not None:
                produced = True
                yield b
        else:
            last_seen[rtype] = values
    if bad_len == 0 and L.line_length is not None:
        checks.ok("line_length")
    # occurs
    occ_ok = True
    for r in L.records:
        k, c = r["occurs"], counts[r["type"]]
        problem = None
        if k in ("first", "last", "once") and c != 1:
            problem = f"record '{r['type']}' must appear once; it appears {c} times"
        elif k == "first" and type_first != r["type"]:
            problem = f"record '{r['type']}' must be the first line"
        elif k == "last" and type_last != r["type"]:
            problem = f"record '{r['type']}' must be the last line"
        if problem:
            occ_ok = False
            checks.fail("occurs", problem[0].upper() + problem[1:] + ".",
                        line=first_line_of.get(r["type"]), record=r["type"])
    if occ_ok:
        checks.ok("occurs")
    # count and sum checks
    for c in L.checks:
        kind = c.get("kind")
        level = c.get("level", "error")
        if kind not in ("count", "sum"):
            continue
        r, fname = c["field"].split(".", 1)
        said = last_seen.get(r, {}).get(fname)
        if said is None:
            checks.fail(kind, f"{c['field']} is missing, so the {kind} cannot be checked.",
                        level=level, line=first_line_of.get(r))
            continue
        if kind == "count":
            actual = counts[c["of"]]
            if int(said) != actual:
                checks.fail("count", f"{c['field']} says {said}; the file holds {actual} "
                            f"'{c['of']}' records.", line=first_line_of.get(r), record=r,
                            level=level)
            else:
                checks.ok("count")
        else:
            actual = sums[c["of"]]
            if Decimal(said) != actual:
                checks.fail("sum", f"{c['field']} says {said}; {c['of']} adds up to {actual}.",
                            line=first_line_of.get(r), record=r, level=level)
            else:
                checks.ok("sum")
    b = batcher.flush()
    if b is not None:
        yield b
    elif not produced:
        yield batcher.empty()
    checks.count("rows", batcher.count)
    for t, c in counts.items():
        checks.count(f"records_{t}", c)


# --------------------------------------------------------------------------- writer

def format_value(fld: _Field, value) -> str:
    width = fld.b - fld.a
    if value is None:
        return " " * width
    if fld.type in ("integer", "decimal"):
        v = Decimal(value)
        neg = v < 0
        v = abs(v)
        if fld.type == "decimal" and fld.implied:
            digits = str(int(v.scaleb(fld.scale).to_integral_value()))
        elif fld.type == "decimal":
            digits = f"{v:.{fld.scale}f}"
        else:
            digits = str(int(v))
        if fld.sign == "leading":
            return ("-" if neg else "+") + digits.rjust(width - 1, "0")
        if fld.sign == "trailing":
            return digits.rjust(width - 1, "0") + ("-" if neg else "+")
        if fld.sign is None and neg:
            return "-" + digits.rjust(width - 1, "0")
        return digits.rjust(width, "0")
    if fld.type in ("date", "time", "timestamp"):
        return value.strftime(fld.fmt).ljust(width)[:width]
    if fld.type == "boolean":
        return (fld.true[0] if value else "N").ljust(width)[:width]
    return str(value).ljust(width)[:width]


def format_record(layout, record: str, values: dict, code: str | None = None) -> str:
    """One line for `record` (its type) from `values`; for samples and tests."""
    L = layout if isinstance(layout, Compiled) else Compiled(layout)
    rec = L.rec_by_type[record]
    length = L.line_length or max([f.b for f in rec["fields"]] + [L.rt[1] if L.rt else 0])
    buf = [" "] * length
    if L.rt is not None:
        codes = [c for c, r in L.by_code.items() if r is rec]
        c = code or codes[0]
        buf[L.rt[0]:L.rt[1]] = c
    for f in rec["fields"]:
        if f.name in values:
            text = format_value(f, values[f.name])
            if len(text) != f.b - f.a:
                raise ValueError(f"{record}.{f.name}: '{text}' does not fit {f.b - f.a} chars")
            buf[f.a:f.b] = text
    return "".join(buf)


# --------------------------------------------------------------------------- detection

def detect(lines: builtins.list[str], project=None) -> builtins.list[dict]:
    """Layouts whose record codes (and line length) match the lines; share of lines matched."""
    out = []
    lines = [x for x in lines if x.strip()]
    if not lines:
        return out
    for info in list_layouts(project):
        if info.get("error"):
            continue
        try:
            L = Compiled(load(info["name"], project))
            if validate_layout(L.data):
                continue
        except Exception:
            continue
        if L.rt is None and L.line_length is None:
            continue
        hits = 0
        for x in lines:
            if L.line_length is not None and len(x) != L.line_length:
                continue
            if L.record_for(x) is None:
                continue
            hits += 1
        share = hits / len(lines)
        if share < 0.5:
            continue
        if L.line_length is not None and L.rt is not None:
            conf = 0.95 * share
        elif L.line_length is not None:
            conf = 0.75 * share
        else:
            conf = 0.6 * share
        out.append({"format": "layout", "confidence": round(conf, 3),
                    "reason": f"{hits} of {len(lines)} lines match layout '{info['name']}'",
                    "options": {"layout": info["name"]}})
    return out


# the module-level names of the spec (section 5.7); `list` shadows the builtin on purpose
list = list_layouts  # noqa: A001
