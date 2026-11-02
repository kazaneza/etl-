"""CREATE TABLE for a destination design, per dialect (SPEC_V2 7.3).

The SQL is built here and nowhere else: every name goes through quote_ident, every type through
native_type, a default is parsed and re-rendered (never pasted), and the result passes
guard.check_create_table before anything runs it. No route accepts DDL text from a person.

The dialect facts (family, quoting, folding, name limits, the type map) come from
etlplus.drivers when it is installed; the tables below are the same facts from SPEC_V2 3.3 and
3.4, used when drivers is missing (for example in a builder's own tests).
"""
from __future__ import annotations

import re

GENERIC_TYPES = ("string", "text", "integer", "bigint", "decimal", "double", "boolean",
                 "date", "time", "timestamp", "timestamptz", "binary", "json")

# kind -> family (SPEC_V2 3.2 and 4.1)
FAMILY = {
    "postgres": "postgres", "redshift": "postgres", "greenplum": "postgres", "cockroachdb": "postgres",
    "timescaledb": "postgres", "yugabytedb": "postgres",
    "sqlserver": "sqlserver", "azuresql": "sqlserver",
    "mysql": "mysql", "mariadb": "mysql",
    "oracle": "oracle", "db2": "db2", "sqlite": "sqlite", "duckdb": "duckdb",
    "clickhouse": "clickhouse", "snowflake": "snowflake", "hana": "hana", "teradata": "teradata",
    "firebird": "firebird", "odbc": "odbc", "mongodb": "mongodb", "folder": "folder",
    "share": "remote", "sftp": "remote", "ftp": "remote", "s3": "remote", "azure_blob": "remote",
    "http": "remote",
}
FILE_KINDS = ("folder", "share", "sftp", "ftp", "s3", "azure_blob", "http")

# family -> (open quote, close quote, fold)
QUOTING = {
    "postgres": ('"', '"', "lower"), "sqlserver": ("[", "]", None), "mysql": ("`", "`", None),
    "oracle": ('"', '"', "upper"), "db2": ('"', '"', "upper"), "sqlite": ('"', '"', None),
    "duckdb": ('"', '"', None), "clickhouse": ("`", "`", None), "snowflake": ('"', '"', "upper"),
    "hana": ('"', '"', "upper"), "teradata": ('"', '"', None), "firebird": ('"', '"', "upper"),
    "odbc": ('"', '"', None),
}
NAME_LIMIT = {"clickhouse": 255, "duckdb": 255, "sqlserver": 128, "snowflake": 255, "hana": 127,
              "firebird": 63, "postgres": 63, "mysql": 64, "oracle": 128, "db2": 128, "teradata": 128}
BACKSLASH_FAMILIES = ("mysql", "clickhouse")

_GENERIC_RE = re.compile(r"^\s*([a-z]+)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$")
_NUMBER_RE = re.compile(r"^-?\d{1,30}(\.\d{1,30})?$")
_STRING_RE = re.compile(r"^'(?:[^']|'')*'$", re.S)


class DDLError(ValueError):
    pass


def _drivers():
    try:
        from etlplus import drivers
        return drivers
    except Exception:
        return None


def family(kind: str) -> str:
    d = _drivers()
    if d is not None:
        try:
            return d.get(kind).family
        except Exception:
            pass
    if kind not in FAMILY:
        raise DDLError(f"'{kind}' is not a known connection kind.")
    return FAMILY[kind]


def is_file_kind(kind: str) -> bool:
    d = _drivers()
    if d is not None:
        try:
            return d.get(kind).category in ("folder", "remote")
        except Exception:
            pass
    return kind in FILE_KINDS


def name_limit(kind: str) -> int:
    return NAME_LIMIT.get(family(kind), 128)


# ---------------------------------------------------------------- generic types

def parse_type(generic) -> tuple[str, int | None, int | None]:
    """'string(40)' -> ('string', 40, None); 'decimal(18,3)' -> ('decimal', 18, 3)."""
    m = _GENERIC_RE.match(str(generic or "").lower())
    if not m or m.group(1) not in GENERIC_TYPES:
        raise DDLError(f"'{generic}' is not a type. Use one of: {', '.join(GENERIC_TYPES)}.")
    base, a, b = m.group(1), m.group(2), m.group(3)
    a = int(a) if a is not None else None
    b = int(b) if b is not None else None
    if base == "string":
        if b is not None:
            raise DDLError("string takes one length, for example string(40).")
        if a is not None and a < 1:
            raise DDLError("A string length is 1 or more.")
    elif base == "decimal":
        if a is None:
            a, b = 18, 3
        b = 0 if b is None else b
        if a < 1 or b > a:
            raise DDLError(f"decimal({a},{b}): the scale must not be larger than the precision.")
    elif a is not None:
        raise DDLError(f"{base} takes no length.")
    return base, a, b


def _string(fam, n, kind):
    if fam == "postgres":
        return f"varchar({n})" if n else "text"
    if fam == "sqlserver":
        return f"nvarchar({n})" if n and n <= 4000 else "nvarchar(max)"
    if fam == "mysql":
        return f"varchar({n})" if n and n <= 16383 else "longtext"
    if fam == "oracle":
        return f"varchar2({n} char)" if n and n <= 4000 else "clob"
    if fam == "db2":
        return f"varchar({n})" if n else "clob"
    if fam == "sqlite":
        return "TEXT"
    if fam == "duckdb":
        return "VARCHAR"
    if fam == "clickhouse":
        return "String"
    if fam == "snowflake":
        return f"VARCHAR({n})" if n else "VARCHAR"
    if fam == "hana":
        return f"NVARCHAR({n})" if n and n <= 5000 else "NCLOB"
    if fam == "teradata":
        return f"VARCHAR({n}) CHARACTER SET UNICODE" if n else "CLOB"
    if fam == "firebird":
        return f"VARCHAR({min(n, 8191)})" if n else "BLOB SUB_TYPE TEXT"
    return f"VARCHAR({n})" if n else "VARCHAR(4000)"


_PLAIN = {
    #            text            integer        bigint         double              boolean       date      time            timestamp          timestamptz                          binary            json
    "postgres": ("text", "integer", "bigint", "double precision", "boolean", "date", "time", "timestamp", "timestamptz", "bytea", "jsonb"),
    "sqlserver": ("nvarchar(max)", "int", "bigint", "float", "bit", "date", "time(6)", "datetime2(6)", "datetimeoffset(6)", "varbinary(max)", "nvarchar(max)"),
    "mysql": ("longtext", "int", "bigint", "double", "tinyint(1)", "date", "time(6)", "datetime(6)", "datetime(6)", "longblob", "json"),
    "oracle": ("clob", "number(10)", "number(19)", "binary_double", "number(1)", "date", "varchar2(15)", "timestamp(6)", "timestamp(6) with time zone", "blob", "clob"),
    "db2": ("clob", "integer", "bigint", "double", "boolean", "date", "time", "timestamp(6)", "timestamp(6)", "blob", "clob"),
    "sqlite": ("TEXT", "INTEGER", "INTEGER", "REAL", "INTEGER", "TEXT", "TEXT", "TEXT", "TEXT", "BLOB", "TEXT"),
    "duckdb": ("VARCHAR", "INTEGER", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ", "BLOB", "JSON"),
    "clickhouse": ("String", "Int32", "Int64", "Float64", "Bool", "Date32", "String", "DateTime64(6)", "DateTime64(6,'UTC')", "String", "String"),
    "snowflake": ("VARCHAR", "NUMBER(10,0)", "NUMBER(19,0)", "FLOAT", "BOOLEAN", "DATE", "TIME", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "BINARY", "VARIANT"),
    "hana": ("NCLOB", "INTEGER", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIMESTAMP", "BLOB", "NCLOB"),
    "teradata": ("CLOB", "INTEGER", "BIGINT", "FLOAT", "BYTEINT", "DATE", "TIME(6)", "TIMESTAMP(6)", "TIMESTAMP(6) WITH TIME ZONE", "BLOB", "JSON"),
    "firebird": ("BLOB SUB_TYPE TEXT", "INTEGER", "BIGINT", "DOUBLE PRECISION", "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "BLOB", "BLOB SUB_TYPE TEXT"),
    "odbc": ("VARCHAR(4000)", "INTEGER", "BIGINT", "DOUBLE PRECISION", "SMALLINT", "DATE", "TIME", "TIMESTAMP", "TIMESTAMP", "VARBINARY(8000)", "VARCHAR(4000)"),
}
_PLAIN_ORDER = ("text", "integer", "bigint", "double", "boolean", "date", "time", "timestamp", "timestamptz",
                "binary", "json")
_DEC_MAX = {"sqlserver": 38, "mysql": 65, "oracle": 38, "db2": 31, "duckdb": 38, "teradata": 38, "firebird": 18,
            "postgres": 1000, "clickhouse": 76, "snowflake": 38, "hana": 38, "odbc": 38}
# lossy combinations: a warning in check() and a /* was: */ comment in the DDL
LOSSY = {("oracle", "time"), ("mysql", "timestamptz"), ("db2", "timestamptz"), ("hana", "timestamptz"),
         ("sqlite", "date"), ("sqlite", "timestamp"), ("sqlite", "timestamptz"), ("sqlite", "decimal"),
         ("sqlite", "time"), ("sqlite", "boolean"), ("clickhouse", "time"), ("odbc", "timestamptz"),
         ("odbc", "boolean")}


def cap_decimal(p: int, s: int, cap: int) -> tuple[int, int]:
    """decimal(p,s) within a precision cap: the whole digits (p-s) are kept and the scale gives
    way, so decimal(50,40) under 38 is (38,28), never (38,38) where 1 already overflows."""
    if p <= cap:
        return p, s
    return cap, max(0, cap - (p - s))


def _local_native(kind: str, generic: str) -> str:
    fam = FAMILY.get(kind)
    if fam is None or fam not in _PLAIN:
        raise DDLError(f"'{kind}' has no table types.")
    base, a, b = parse_type(generic)
    if base == "string":
        return _string(fam, a, kind)
    if base == "decimal":
        p, s = cap_decimal(a, b, _DEC_MAX.get(fam, 38))
        return {"postgres": f"numeric({p},{s})", "sqlserver": f"decimal({p},{s})", "mysql": f"decimal({p},{s})",
                "oracle": f"number({p},{s})", "db2": f"decimal({p},{s})", "sqlite": "NUMERIC",
                "duckdb": f"DECIMAL({p},{s})", "clickhouse": f"Decimal({p},{s})",
                "snowflake": f"NUMBER({p},{s})"}.get(fam, f"DECIMAL({p},{s})")
    native = _PLAIN[fam][_PLAIN_ORDER.index(base)]
    if kind == "redshift" and base == "binary":
        return "varbyte"
    if kind == "redshift" and base == "json":
        return "super"
    return native


def native_type(kind: str, generic: str) -> str:
    d = _drivers()
    if d is not None and hasattr(d, "native_type"):
        try:
            return d.native_type(kind, generic)
        except KeyError:
            pass
    return _local_native(kind, generic)


def _check_name(kind: str, name: str, what="column") -> None:
    if not isinstance(name, str) or not name:
        raise DDLError(f"A {what} needs a name.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise DDLError(f"The {what} name {name!r} holds a control character.")
    lim = name_limit(kind)
    if len(name) > lim:
        raise DDLError(f"The {what} name '{name[:40]}...' is longer than {lim} characters, the limit for {kind}.")


def quote_ident(kind: str, name: str) -> str:
    _check_name(kind, name)
    d = _drivers()
    if d is not None and hasattr(d, "quote_ident"):
        try:
            return d.quote_ident(kind, name)
        except KeyError:
            pass
    fam = family(kind)
    o, c, fold = QUOTING.get(fam, ('"', '"', None))
    n = name
    if fold == "upper" and re.match(r"^[A-Za-z_][A-Za-z0-9_$#]*$", n):
        n = n.upper()
    elif fold == "lower" and re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", n):
        n = n.lower()
    if fam == "clickhouse":
        n = n.replace("\\", "\\\\")
    return o + n.replace(c, c + c) + c


def split_table(name: str) -> list[str]:
    """Split schema.table on dots outside quotes; at most 3 parts; quoted parts unquoted."""
    s = str(name or "").strip()
    parts, cur, i = [], "", 0
    while i < len(s):
        ch = s[i]
        if ch in '"`[':
            close = "]" if ch == "[" else ch
            j = i + 1
            buf = ""
            while j < len(s):
                if s[j] == close:
                    if j + 1 < len(s) and s[j + 1] == close:
                        buf += close
                        j += 2
                        continue
                    break
                buf += s[j]
                j += 1
            if j >= len(s):
                raise DDLError(f"The table name {name!r} has an unclosed quote.")
            cur += buf
            i = j + 1
            continue
        if ch == ".":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur)
    parts = [p.strip() if p.strip() == p else p for p in parts]
    if any(not p for p in parts):
        raise DDLError(f"The table name {name!r} has an empty part.")
    if len(parts) > 3:
        raise DDLError(f"The table name {name!r} has more than 3 parts (database.schema.table).")
    return parts


def quote_table(kind: str, name: str) -> str:
    d = _drivers()
    if d is not None and hasattr(d, "quote_table"):
        try:
            return d.quote_table(kind, name)
        except KeyError:
            pass
    parts = split_table(name)
    if family(kind) == "sqlite" and len(parts) > 1 and parts[0].lower() == "main":
        parts = parts[1:]
    for p in parts:
        _check_name(kind, p, "table")
    return ".".join(quote_ident(kind, p) for p in parts)


# ---------------------------------------------------------------- defaults

def default_literal(kind: str, default, generic: str = "text") -> str:
    """Parse a stored default and render it for the kind; anything else is refused."""
    fam = family(kind)
    if isinstance(default, bool):
        return _bool_lit(fam, default)
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        text = repr(default) if isinstance(default, float) else str(default)
        if not _NUMBER_RE.match(text):
            raise DDLError(f"The default {default!r} is not a plain number.")
        return text
    if not isinstance(default, str):
        raise DDLError(f"The default {default!r} is not a literal.")
    t = default.strip()
    if t.lower() in ("true", "false"):
        return _bool_lit(fam, t.lower() == "true")
    if t.upper() in ("CURRENT_TIMESTAMP", "NOW()", "CURRENT_TIMESTAMP()"):
        return {"clickhouse": "now()", "oracle": "SYSTIMESTAMP"}.get(fam, "CURRENT_TIMESTAMP")
    if _NUMBER_RE.match(t):
        return t
    if len(t) <= 4002 and _STRING_RE.match(t):
        inner = t[1:-1].replace("''", "'")
        if len(inner) > 4000:
            raise DDLError("A default text is at most 4000 characters.")
        out = inner.replace("'", "''")
        if fam in BACKSLASH_FAMILIES:
            out = out.replace("\\", "\\\\")
        return "'" + out + "'"
    raise DDLError(f"The default {default!r} is not allowed: use a number, one quoted text "
                   "('like this'), true, false or CURRENT_TIMESTAMP.")


def _bool_lit(fam, v: bool) -> str:
    if fam in ("sqlserver", "mysql", "oracle", "sqlite", "teradata", "odbc"):
        return "1" if v else "0"
    return "TRUE" if v else "FALSE"


# ---------------------------------------------------------------- the design

def check(kind: str, columns: list[dict], keys=None) -> list[dict]:
    """Problems with a column design for this kind: [{"level","column","message"}]."""
    out = []
    keys = [keys] if isinstance(keys, str) else list(keys or [])
    if not isinstance(columns, list) or not columns:
        return [{"level": "error", "column": None, "message": "Add at least one column."}]
    seen = set()
    fam = None
    try:
        fam = family(kind)
    except DDLError as e:
        return [{"level": "error", "column": None, "message": str(e)}]
    for c in columns:
        if not isinstance(c, dict):
            out.append({"level": "error", "column": None, "message": "Each column is a mapping with a name and a type."})
            continue
        name = c.get("name")
        try:
            _check_name(kind, name)
        except DDLError as e:
            out.append({"level": "error", "column": name, "message": str(e)})
            continue
        low = name.lower()
        if low in seen:
            out.append({"level": "error", "column": name, "message": "This name is used twice."})
        seen.add(low)
        try:
            base, a, b = parse_type(c.get("type") or "text")
        except DDLError as e:
            out.append({"level": "error", "column": name, "message": str(e)})
            continue
        if (fam, base) in LOSSY:
            out.append({"level": "warn", "column": name,
                        "message": f"{kind} has no exact {base} type; it is stored as {native_type(kind, c.get('type') or base)}."})
        if base == "decimal" and a and a > _DEC_MAX.get(fam, 38):
            out.append({"level": "warn", "column": name,
                        "message": "decimal({},{}) is more than {} allows; it becomes decimal({},{}).".format(
                            a, b, kind, *cap_decimal(a, b or 0, _DEC_MAX.get(fam, 38)))})
        if base == "string" and a and fam == "firebird" and a > 8191:
            out.append({"level": "warn", "column": name, "message": "Firebird VARCHAR holds at most 8191 characters."})
        if c.get("default") not in (None, ""):
            try:
                default_literal(kind, c.get("default"), base)
            except DDLError as e:
                out.append({"level": "error", "column": name, "message": str(e)})
    names = {str(c.get("name")).lower() for c in columns if isinstance(c, dict) and c.get("name")}
    for k in keys:
        if str(k).lower() not in names:
            out.append({"level": "error", "column": k, "message": f"The key '{k}' is not a column."})
    if fam == "oracle" and any(isinstance(c, dict) and len(str(c.get("name") or "")) > 30 for c in columns):
        out.append({"level": "warn", "column": None,
                    "message": "Oracle before 12.2 allows names of at most 30 characters."})
    if fam == "clickhouse" and not keys:
        out.append({"level": "warn", "column": None, "message": "No key: ClickHouse orders the table by nothing (ORDER BY tuple())."})
    return out


def create_table_sql(kind: str, table: str, columns: list[dict], keys=None) -> str:
    """One CREATE TABLE for the design. Raises DDLError on a design error."""
    problems = [p for p in check(kind, columns, keys) if p["level"] == "error"]
    if problems:
        p = problems[0]
        raise DDLError((f"Column '{p['column']}': " if p.get("column") else "") + p["message"])
    fam = family(kind)
    keys = [keys] if isinstance(keys, str) else list(keys or [])
    by_low = {str(c["name"]).lower(): c["name"] for c in columns}
    key_cols = [by_low[str(k).lower()] for k in keys]
    lines = []
    for c in columns:
        gen = str(c.get("type") or "text")
        base = parse_type(gen)[0]
        native = native_type(kind, gen)
        nullable = c.get("nullable", True) is not False and c["name"] not in key_cols
        if fam == "clickhouse" and nullable:
            native = f"Nullable({native})"
        part = f"{quote_ident(kind, c['name'])} {native}"
        if (fam, base) in LOSSY:
            part += f" /* was: {gen} */"
        if c.get("default") not in (None, ""):
            part += f" DEFAULT {default_literal(kind, c['default'], base)}"
        if not nullable and fam != "clickhouse":
            part += " NOT NULL"
        lines.append(part)
    tq = quote_table(kind, table)
    tail = ""
    if key_cols:
        kq = ", ".join(quote_ident(kind, k) for k in key_cols)
        if fam == "teradata":
            tail = f"\nUNIQUE PRIMARY INDEX ({kq})"
        elif fam != "clickhouse":
            lines.append(f"PRIMARY KEY ({kq})")
    if fam == "clickhouse":
        order = ", ".join(quote_ident(kind, k) for k in key_cols)
        tail = f"\nENGINE = MergeTree ORDER BY ({order})" if key_cols else "\nENGINE = MergeTree ORDER BY tuple()"
    ine = "IF NOT EXISTS " if fam in ("postgres", "mysql", "sqlite", "duckdb", "clickhouse", "snowflake") and \
        kind not in ("redshift",) else ""
    sql = f"CREATE TABLE {ine}{tq} (\n    " + ",\n    ".join(lines) + "\n)" + tail
    _guard(sql, kind)
    return sql


def _guard(sql: str, kind: str) -> None:
    from etlplus import guard
    fn = getattr(guard, "check_create_table", None)
    if fn is not None:
        try:
            fn(sql, kind)
        except guard.GuardError as e:
            raise DDLError(f"The generated DDL was refused by the guard: {e}") from None
        return
    # the guard has no check_create_table yet: a local check of the same rule
    _local_create_check(sql)


def _local_create_check(sql: str) -> None:
    from etlplus import guard
    tokens = guard._scan(sql, backslash=False, brackets=True)
    code = " ".join(t for k, t in tokens if k == "code")
    if ";" in code:
        raise DDLError("Only one statement is allowed.")
    words = [w.upper() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_$#]*", code)]
    if words[:2] != ["CREATE", "TABLE"]:
        raise DDLError("Only CREATE TABLE is allowed.")
    bad = [w for w in words[1:] if w in guard.FORBIDDEN and w != "CREATE"]
    if "SELECT" in words or bad or words.count("CREATE") != 1:
        raise DDLError("The CREATE TABLE holds something it should not.")
    guard._check_calls(tokens)


def stored_parts(kind: str, table: str) -> list[str]:
    """The name parts as the server stores them: a quoted part keeps its case, a plain one folds
    the way quote_ident folds it (Oracle 'orders' -> ORDERS, 'MyTable' stays MyTable)."""
    d = _drivers()
    if d is not None and hasattr(d, "split_name") and hasattr(d, "_unquote_part") and hasattr(d, "_fold"):
        out = []
        for part in d.split_name(table):
            inner, quoted = d._unquote_part(part)
            out.append(inner if quoted else d._fold(kind, inner))
        return out
    return split_table(table)


def exists_sql(kind: str, table: str) -> tuple[str, tuple]:
    """A read-only query that returns a row when the table exists. The name is compared exactly
    as CREATE TABLE stored it (stored_parts), so a mixed-case or quoted name is found."""
    fam = family(kind)
    parts = stored_parts(kind, table)
    name, schema = parts[-1], (parts[-2] if len(parts) > 1 else None)
    if fam == "sqlite":
        return "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
    if fam == "duckdb":
        if schema:
            return ("SELECT 1 FROM information_schema.tables WHERE table_name = ? AND table_schema = ?",
                    (name, schema))
        return ("SELECT 1 FROM information_schema.tables WHERE table_name = ? AND table_schema = current_schema()",
                (name,))
    if fam == "postgres":
        return ("SELECT 1 FROM information_schema.tables WHERE table_name = %s "
                "AND table_schema = COALESCE(%s, current_schema())",
                (name, schema))
    if fam == "mysql":
        return ("SELECT 1 FROM information_schema.tables WHERE table_schema = COALESCE(%s, DATABASE()) "
                "AND table_name = %s", (schema, name))
    if fam == "sqlserver":
        return "SELECT 1 WHERE OBJECT_ID(?, 'U') IS NOT NULL", (quote_table(kind, table),)
    if fam == "oracle":
        return ("SELECT 1 FROM all_tables WHERE owner = COALESCE(:1, USER) AND table_name = :2",
                (schema, name))
    if fam == "db2":
        return ("SELECT 1 FROM SYSCAT.TABLES WHERE TABSCHEMA = COALESCE(?, CURRENT SCHEMA) AND TABNAME = ?",
                (schema, name))
    if fam == "firebird":
        return "SELECT 1 FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?", (name,)
    if fam == "hana":
        return ("SELECT 1 FROM SYS.TABLES WHERE SCHEMA_NAME = COALESCE(?, CURRENT_SCHEMA) AND TABLE_NAME = ?",
                (schema, name))
    if fam == "clickhouse":
        return ("SELECT 1 FROM system.tables WHERE database = coalesce(%(s)s, currentDatabase()) AND name = %(n)s",
                {"s": schema, "n": name})
    if fam == "snowflake":
        return ("SELECT 1 FROM information_schema.tables WHERE table_name = %s "
                "AND table_schema = COALESCE(%s, CURRENT_SCHEMA())", (name, schema))
    if fam == "teradata":
        return ("SELECT 1 FROM DBC.TablesV WHERE TableName = ? AND DatabaseName = COALESCE(?, DATABASE)",
                (name, schema))
    return ("SELECT 1 FROM information_schema.tables WHERE table_name = ?", (name,))


def types_table(kind: str) -> list[dict]:
    """GET /api/types?kind= : [{generic, native}] for every generic type."""
    out = []
    for g in GENERIC_TYPES:
        sample = {"string": "string(40)", "decimal": "decimal(18,3)"}.get(g, g)
        try:
            out.append({"generic": sample, "native": native_type(kind, sample)})
        except Exception as e:
            out.append({"generic": sample, "native": None, "detail": str(e)})
    return out


# ---------------------------------------------------------------- arrow / duckdb <-> generic

def arrow_to_generic(t) -> str:
    d = _drivers()
    if d is not None and hasattr(d, "arrow_to_generic"):
        try:
            return d.arrow_to_generic(t)
        except Exception:
            pass
    import pyarrow as pa
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_integer(t):
        return "bigint" if t.bit_width > 32 or (pa.types.is_unsigned_integer(t) and t.bit_width >= 32) else "integer"
    if pa.types.is_floating(t):
        return "double"
    if pa.types.is_decimal(t):
        return f"decimal({min(t.precision, 38)},{t.scale})"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_time(t):
        return "time"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t):
        return "binary"
    if pa.types.is_dictionary(t):
        return arrow_to_generic(t.value_type)
    if pa.types.is_list(t) or pa.types.is_struct(t) or pa.types.is_map(t):
        return "json"
    return "text"


def duck_to_generic(t: str) -> str:
    """A DuckDB type name (DESCRIBE output) as a generic type."""
    u = str(t or "").upper().strip()
    m = re.match(r"^DECIMAL\((\d+),\s*(\d+)\)$", u)
    if m:
        return f"decimal({m.group(1)},{m.group(2)})"
    if u in ("BOOLEAN", "BOOL"):
        return "boolean"
    if u in ("TINYINT", "SMALLINT", "INTEGER", "INT", "UTINYINT", "USMALLINT"):
        return "integer"
    if u in ("BIGINT", "UINTEGER", "UBIGINT", "HUGEINT", "LONG"):
        return "bigint"
    if u in ("DOUBLE", "FLOAT", "REAL"):
        return "double"
    if u == "DATE":
        return "date"
    if u.startswith("TIME") and "STAMP" not in u:
        return "time"
    if u in ("TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"):
        return "timestamptz"
    if u.startswith("TIMESTAMP"):
        return "timestamp"
    if u in ("BLOB", "BYTEA", "BINARY", "VARBINARY"):
        return "binary"
    if u == "JSON" or u.endswith("[]") or u.startswith("STRUCT") or u.startswith("MAP"):
        return "json"
    return "text"


def generic_to_duck(generic: str) -> str:
    """The DuckDB type a mapping casts to for a generic destination type."""
    base, a, b = parse_type(generic)
    return {"string": "VARCHAR", "text": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
            "decimal": "DECIMAL({},{})".format(*cap_decimal(a or 18, b or 0, 38)), "double": "DOUBLE",
            "boolean": "BOOLEAN", "date": "DATE", "time": "TIME", "timestamp": "TIMESTAMP",
            "timestamptz": "TIMESTAMPTZ", "binary": "BLOB", "json": "VARCHAR"}[base]


def generic_type(kind: str, native: str) -> str:
    """Best-effort reverse map of a column type read from a database."""
    d = _drivers()
    if d is not None and hasattr(d, "generic_type"):
        try:
            return d.generic_type(kind, native)
        except Exception:
            pass
    u = str(native or "").upper()
    m = re.match(r"^(N?VARCHAR2?|VARCHAR|CHARACTER VARYING|NVARCHAR|CHAR|NCHAR)\s*\((\d+)", u)
    if m:
        return f"string({m.group(2)})"
    m = re.match(r"^(NUMERIC|DECIMAL|NUMBER)\s*\((\d+)\s*,\s*(\d+)\)", u)
    if m:
        if m.group(3) == "0" and int(m.group(2)) <= 10:
            return "integer"
        if m.group(3) == "0" and int(m.group(2)) <= 19:
            return "bigint"
        return f"decimal({m.group(2)},{m.group(3)})"
    if u in ("INT", "INTEGER", "INT4", "SMALLINT", "INT2", "TINYINT", "MEDIUMINT", "INT32"):
        return "integer"
    if u in ("BIGINT", "INT8", "INT64"):
        return "bigint"
    if u in ("REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION", "FLOAT8", "FLOAT64", "BINARY_DOUBLE"):
        return "double"
    if u in ("BOOLEAN", "BOOL", "BIT"):
        return "boolean"
    if u == "DATE":
        return "date"
    if u.startswith("TIMESTAMP") and ("TIME ZONE" in u or u.endswith("TZ")) or u.startswith("DATETIMEOFFSET"):
        return "timestamptz"
    if u.startswith("TIMESTAMP") or u.startswith("DATETIME"):
        return "timestamp"
    if u.startswith("TIME"):
        return "time"
    if u in ("JSON", "JSONB", "VARIANT", "SUPER"):
        return "json"
    if u in ("BLOB", "BYTEA", "LONGBLOB") or u.startswith("VARBINARY") or u.startswith("BINARY"):
        return "binary"
    return duck_to_generic(native)
