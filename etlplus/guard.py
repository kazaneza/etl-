"""Read-only SQL guard: one SELECT or WITH ... SELECT statement, nothing else.

Every database read in etl+ goes through check_select(). The SQL is scanned with a small
tokenizer that knows string literals, quoted identifiers, dollar quotes and comments, so a
keyword inside a literal is fine while the same keyword in code is refused. The cleaned SQL
(comments removed, trailing semicolon dropped) is what callers must run.

The scan runs twice, once with standard quoting ('' escapes) and once treating a backslash
inside a literal as an escape (MySQL), and both must pass. That way a literal cannot be read
one way by the guard and another way by the server.

The dialect is a connection kind or a family (drivers maps a kind to its family; an unknown
dialect gets the strict reading of None). Per family: [x] is a quoted name only on SQL Server;
$$ dollar quotes only on the Postgres family; ClickHouse also escapes a backslash inside
backticks; Teradata may start with LOCKING ROW FOR ACCESS.

check_create_table() passes the one CREATE TABLE that ddl.py generates; check_mongo() passes a
MongoDB filter, projection or pipeline made of read operators only.
"""

import re


class GuardError(ValueError):
    pass


FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT",
    "DROP", "CREATE", "ALTER", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "DENY",
    "EXEC", "EXECUTE", "CALL",
    "COPY", "INTO", "LOCK",
    "SET", "DECLARE",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
    "VACUUM", "REINDEX", "ATTACH", "DETACH", "PRAGMA", "INSTALL",
    "SHUTDOWN", "KILL", "DBCC", "BACKUP", "RESTORE", "BULK",
    "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "OUTFILE", "DUMPFILE",
    "PREPARE", "DEALLOCATE", "LISTEN", "NOTIFY", "WAITFOR", "RECONFIGURE",
}

FORBIDDEN_FUNCTIONS = {
    "XP_CMDSHELL", "SP_EXECUTESQL", "SP_EXECUTE", "SP_CONFIGURE", "SP_OA_CREATE",
    "DBLINK", "DBLINK_EXEC", "LO_IMPORT", "LO_EXPORT", "LO_UNLINK",
    "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_TERMINATE_BACKEND",
    "PG_CANCEL_BACKEND", "PG_RELOAD_CONF", "PG_ROTATE_LOGFILE", "SETVAL", "NEXTVAL",
    "SET_CONFIG", "PG_ADVISORY_LOCK", "PG_ADVISORY_XACT_LOCK", "PG_SLEEP",
    "SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK",
    "DBMS_SQL", "DBMS_XMLGEN", "DBMS_LOCK", "DBMS_PIPE", "UTL_HTTP", "UTL_FILE",
    "SYS_EXEC", "SYS_EVAL", "READ_CSV", "READ_TEXT", "READ_BLOB", "WRITE_CSV",
    "PG_NOTIFY", "PG_SIGNAL_BACKEND", "PG_SWITCH_WAL", "PG_PROMOTE", "PG_STAT_FILE",
    "TXID_CURRENT", "PG_CURRENT_XACT_ID", "CURSOR_TO_XML", "CURSOR_TO_XMLSCHEMA",
    "GLOB", "GETENV", "SNIFF_CSV", "PARQUET_SCAN", "CSV_SCAN", "JSON_SCAN", "ICEBERG_SCAN",
    "DELTA_SCAN", "QUERY", "QUERY_TABLE", "XP_DIRTREE", "XP_FILEEXIST", "XP_REGREAD",
}

# Any function whose name, or whose package or schema name, starts with one of these.
FORBIDDEN_PREFIXES = (
    "DBMS_", "UTL_", "READ_", "WRITE_", "LO_", "PG_LOGICAL_", "PG_REPLICATION_", "PG_CREATE_",
    "PG_DROP_", "PG_READ_", "PG_LS_", "PG_FILE", "PG_ADVISORY", "PG_TRY_ADVISORY", "QUERY_TO_XML",
    "TABLE_TO_XML", "SCHEMA_TO_XML", "DATABASE_TO_XML", "PARQUET_", "XP_", "SP_", "HTTP_",
    # dblink_* and postgres_fdw_* open their own sessions, which are not read-only
    "DBLINK", "POSTGRES_FDW",
)

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _scan(sql, backslash, brackets=True, dollar=True, ident_backslash=False):
    """Split SQL into (kind, text) tokens. kind: code, string, ident, comment.

    brackets: [name] is a quoted identifier (SQL Server). Elsewhere [ ] is code (arrays).
    dollar: $tag$ ... $tag$ is a string (Postgres). Elsewhere $ is code.
    ident_backslash: a backslash escapes the next character inside `x` (ClickHouse).
    """
    out = []
    i, n = 0, len(sql)
    code_start = 0

    def flush(end):
        if end > code_start:
            out.append(("code", sql[code_start:end]))

    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":
            flush(i)
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", sql[i:j]))
            i = code_start = j
        elif c == "#" and backslash:
            # MySQL line comment; scanned only in the MySQL reading
            flush(i)
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", sql[i:j]))
            i = code_start = j
        elif c == "/" and nxt == "*":
            flush(i)
            j = sql.find("*/", i + 2)
            if j < 0:
                raise GuardError("Unclosed /* comment.")
            out.append(("comment", sql[i:j + 2]))
            i = code_start = j + 2
        elif c in "'\"`" or (c == "[" and brackets):
            flush(i)
            close = {"'": "'", '"': '"', "`": "`", "[": "]"}[c]
            kind = "string" if c == "'" else "ident"
            j = i + 1
            while True:
                if j >= n:
                    raise GuardError("Unclosed quote in SQL.")
                ch = sql[j]
                if ch == "\\" and ((backslash and kind == "string") or (ident_backslash and close == "`")):
                    j += 2
                    continue
                if ch == close:
                    if j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            out.append((kind, sql[i:j + 1]))
            i = code_start = j + 1
        elif c == "$" and dollar:
            m = _DOLLAR.match(sql, i)
            if m and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j < 0:
                    raise GuardError("Unclosed dollar-quoted string.")
                flush(i)
                out.append(("string", sql[i:j + len(tag)]))
                i = code_start = j + len(tag)
            else:
                i += 1
        else:
            i += 1
    flush(n)
    return out


def _ident_name(text: str) -> str:
    """The name inside a quoted identifier, case-folded: "x", `x` or [x]."""
    inner = text[1:-1]
    close = text[-1]
    return inner.replace(close * 2, close).strip().upper()


_ITEM = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*|\S")


def _forbidden_call(name: str) -> bool:
    return name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_PREFIXES)


def _check_calls(tokens) -> None:
    """Every name directly before '(' is a call: check each dotted part, quoted or not."""
    items = []
    for kind, text in tokens:
        if kind == "code":
            for m in _ITEM.finditer(text):
                w = m.group(0)
                items.append(("name", w.upper()) if (w[0].isalpha() or w[0] == "_") else ("sym", w))
        elif kind == "ident":
            items.append(("name", _ident_name(text)))
        elif kind == "string":
            items.append(("str", ""))
    for i, (kind, text) in enumerate(items):
        if kind != "sym" or text != "(" or i == 0 or items[i - 1][0] != "name":
            continue
        parts = []
        j = i - 1
        while j >= 0 and items[j][0] == "name":
            parts.append(items[j][1])
            if j >= 2 and items[j - 1] == ("sym", ".") and items[j - 2][0] == "name":
                j -= 2
            else:
                break
        for part in parts:
            if _forbidden_call(part):
                raise GuardError(f"{part}() is not allowed in a read-only query.")


def _check(sql, backslash, brackets=True, dollar=True, ident_backslash=False):
    tokens = _scan(sql, backslash, brackets, dollar, ident_backslash)
    parts = []
    for kind, text in tokens:
        parts.append(" " if kind == "comment" else text)
    cleaned = "".join(parts).strip()
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        raise GuardError("The SQL is empty.")

    tokens = _scan(cleaned, backslash, brackets, dollar, ident_backslash)
    code = []
    for kind, text in tokens:
        if kind == "code":
            code.append(text)
        else:
            code.append(" ")
    code_text = " ".join(code)
    if ";" in code_text:
        raise GuardError("Only one statement is allowed (found ';').")

    words = [w.upper() for w in _WORD.findall(code_text)]
    if not words:
        raise GuardError("Only SELECT or WITH ... SELECT is allowed.")
    first = words[0]
    if first not in ("SELECT", "WITH"):
        raise GuardError(f"Only SELECT or WITH ... SELECT is allowed, not {first}.")
    stripped = code_text.lstrip()
    if stripped and not (stripped[0] == "(" or stripped[: len(first)].upper() == first):
        raise GuardError("The statement must start with SELECT or WITH.")
    if "SELECT" not in words:
        raise GuardError("A WITH statement must end in a SELECT.")
    for w in words:
        if w in FORBIDDEN:
            raise GuardError(f"{w} is not allowed in a read-only query.")
    _check_calls(tokens)
    return cleaned


def dialect_family(dialect: str | None) -> str | None:
    """A kind or a family -> the family; None for no dialect or an unknown one."""
    if not dialect:
        return None
    from . import drivers
    return drivers.family(str(dialect))


_LOCKING = re.compile(r"^\s*LOCKING\s+ROW\s+FOR\s+ACCESS\s+", re.I)


def check_select(sql: str, dialect: str | None = None) -> str:
    """Return the cleaned SQL, or raise GuardError when it is not one read-only SELECT.

    dialect: the server kind or family. [x] is a quoted name only on sqlserver; on any other
    server it is code (an array) and is checked as code. With no dialect (or an unknown one)
    every reading must pass.
    """
    if not isinstance(sql, str):
        raise GuardError("The SQL must be text.")
    if "\x00" in sql:
        raise GuardError("The SQL contains a NUL character.")
    fam = dialect_family(dialect)
    prefix = ""
    if fam == "teradata":
        m = _LOCKING.match(sql)
        if m:
            prefix = "LOCKING ROW FOR ACCESS "
            sql = sql[m.end():]
    brackets = fam in (None, "sqlserver")
    dollar = fam in (None, "postgres")
    # a backslash inside `x` escapes on ClickHouse; with no dialect both readings must pass
    ident_readings = (True,) if fam == "clickhouse" else ((False, True) if fam is None else (False,))
    cleaned = None
    for ident_bs in ident_readings:
        for backslash in (False, True):
            c = _check(sql, backslash, brackets, dollar, ident_bs)
            if cleaned is None:
                cleaned = c
    if fam is None:
        # the server is not known: a call hidden in [ ] (an array elsewhere) is refused as well
        for backslash in (False, True):
            _check_calls(_scan(cleaned, backslash, brackets=False))
    return prefix + cleaned


def is_safe(sql: str, dialect: str | None = None) -> bool:
    try:
        check_select(sql, dialect)
        return True
    except GuardError:
        return False


# ---------------------------------------------------------------- CREATE TABLE (ddl.py)

def check_create_table(sql: str, dialect: str | None = None) -> str:
    """Pass exactly one CREATE TABLE statement, with no AS SELECT and nothing after it.

    Every guard.FORBIDDEN word except CREATE (and SET in CHARACTER SET) and every forbidden
    function is refused, so a crafted default or name that slipped through is still stopped.
    Returns the cleaned statement (comments removed, no trailing semicolon).
    """
    if not isinstance(sql, str):
        raise GuardError("The SQL must be text.")
    if "\x00" in sql:
        raise GuardError("The SQL contains a NUL character.")
    fam = dialect_family(dialect)
    brackets = fam in (None, "sqlserver")
    dollar = fam == "postgres"
    ident_bs = fam in (None, "clickhouse")
    out = None
    for backslash in (False, True):
        tokens = _scan(sql, backslash, brackets, dollar, ident_bs)
        cleaned = "".join(" " if k == "comment" else t for k, t in tokens).strip()
        while cleaned.endswith(";"):
            cleaned = cleaned[:-1].rstrip()
        if not cleaned:
            raise GuardError("The SQL is empty.")
        tokens = _scan(cleaned, backslash, brackets, dollar, ident_bs)
        code = " ".join(t if k == "code" else " " for k, t in tokens)
        if ";" in code:
            raise GuardError("Only one statement is allowed (found ';').")
        words = [w.upper() for w in _WORD.findall(code)]
        if words[:2] != ["CREATE", "TABLE"] or not code.lstrip().upper().startswith("CREATE"):
            raise GuardError("Only one CREATE TABLE statement is allowed.")
        for i, w in enumerate(words):
            if i == 0:
                continue
            if w == "SET" and words[i - 1] == "CHARACTER":
                continue
            if w in FORBIDDEN or w in ("SELECT", "LIKE", "CLONE"):
                raise GuardError(f"{w} is not allowed in CREATE TABLE.")
        _check_calls(tokens)
        if out is None:
            out = cleaned
    return out


# ---------------------------------------------------------------- MongoDB

MONGO_REFUSED = {"$where", "$function", "$accumulator", "$out", "$merge"}
MONGO_ALLOWED = {
    # query and logical operators
    "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$and", "$or", "$nor", "$not", "$exists",
    "$type", "$regex", "$options", "$all", "$elemMatch", "$size", "$mod", "$expr",
    # read-only stages
    "$match", "$project", "$group", "$sort", "$limit", "$skip", "$unwind", "$addFields", "$set", "$unset",
    "$count", "$facet", "$bucket", "$bucketAuto", "$sortByCount", "$replaceRoot", "$replaceWith", "$sample",
    "$redact",
    # expression operators and their named arguments
    "$sum", "$avg", "$min", "$max", "$first", "$last", "$push", "$addToSet", "$concat", "$substr",
    "$substrCP", "$toUpper", "$toLower", "$dateToString", "$dateFromString", "$year", "$month",
    "$dayOfMonth", "$hour", "$minute", "$second", "$cond", "$ifNull", "$switch", "$add", "$subtract",
    "$multiply", "$divide", "$abs", "$round", "$floor", "$ceil", "$arrayElemAt", "$toString", "$toInt",
    "$toLong", "$toDouble", "$toDecimal", "$toDate", "$toBool", "$literal", "$split", "$trim", "$ltrim",
    "$rtrim", "$strLenCP", "$map", "$filter", "$reduce", "$objectToArray", "$arrayToObject", "$mergeObjects",
    "$isArray", "$concatArrays", "$dateTrunc", "$dateAdd", "$dateDiff", "$stdDevPop", "$stdDevSamp",
    "$getField", "$let", "$convert",
    # extended JSON values
    "$date", "$numberDecimal", "$numberLong", "$numberInt", "$oid",
}


def check_mongo(obj, _depth=0) -> None:
    """Refuse any $key that is not a read operator or stage: $where, $function, $accumulator,
    $out and $merge always, and anything not on the allow-list."""
    if _depth > 64:
        raise GuardError("The MongoDB query is nested too deeply.")
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k)
            if key.startswith("$"):
                if key in MONGO_REFUSED:
                    raise GuardError(f"{key} is not allowed in a read-only query.")
                if key not in MONGO_ALLOWED:
                    raise GuardError(f"{key} is not an allowed MongoDB operator here.")
            check_mongo(v, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            check_mongo(v, _depth + 1)
