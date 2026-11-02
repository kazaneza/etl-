"""Connections: open, test and browse every source and target kind.

The kinds, their fields and how each is reached live in etlplus.drivers (databases, MongoDB,
the local folder) and etlplus.remote (shared folders, SFTP, FTP, S3, Azure Blob, HTTP). This
module keeps the v1 public functions and delegates to them.

A connection whose role is "source" is always opened read-only, by the driver or the session.
Every SQL read goes through etlplus.guard. Passwords, keys and tokens come from the secrets
store or an environment variable, or from an unsaved test form, and are never written anywhere
by this module; a stored secret is only sent to the endpoint it was saved for.
"""

from __future__ import annotations

import datetime as dt
import decimal
import importlib
import importlib.metadata
import importlib.util
import os
import re
import time
import uuid
from pathlib import Path

from . import drivers, guard
from . import secrets as secret_store
from .drivers import DriverError

ConnectionError_ = DriverError

KINDS = set(drivers.REGISTRY)
NETWORK = {k for k, d in drivers.REGISTRY.items()
           if d.category in ("database", "document") and any(f.name in ("host", "uri", "account") for f in d.fields)}
FILE_DB = {"sqlite", "duckdb"}
REMOTE = set(drivers.kinds("remote"))
FILE_KINDS = REMOTE | {"folder"}

DRIVERS = {k: (d.module, d.pip) for k, d in drivers.REGISTRY.items()}
DRIVERS["excel"] = ("openpyxl", "openpyxl")
EXTRAS = {k: d.extra for k, d in drivers.REGISTRY.items() if d.extra}
EXTRAS["excel"] = "excel"
DEFAULT_PORTS = {k: d.default_port for k, d in drivers.REGISTRY.items() if d.default_port}

from .drivers.mssql import SQLSERVER_DRIVERS  # noqa: E402

TIMEOUT = 15


# ---------------------------------------------------------------- driver modules

def _import(module):
    try:
        return importlib.import_module(module)
    except ImportError:
        return None


def driver(kind):
    """The driver module for a kind. Looked up at call time by every connect path, so tests can
    replace it. A missing one gives a message naming pip install etlplus[extra]."""
    if kind == "excel":
        mod = _import("openpyxl")
        if mod is None:
            raise ConnectionError_("The Excel reader (openpyxl) is not installed. Install it with: "
                                   "pip install etlplus[excel].")
        return mod
    d = drivers.get(kind)
    mod = _import(d.module)
    if mod is None:
        raise ConnectionError_(drivers.missing_driver_message(kind))
    return mod


def _dist_version(pip_name: str) -> str:
    name = re.split(r"[\[<>=]", pip_name)[0]
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def driver_status() -> dict:
    """{kind: {installed, package, version, install}}. Imports nothing heavy (find_spec only)."""
    out = {}
    for kind, (module, package) in DRIVERS.items():
        if kind == "excel":
            ok = drivers.module_spec_exists("openpyxl")
            info = {"installed": ok, "package": package, "install": "pip install etlplus[excel]"}
        else:
            ok = drivers.installed(kind)
            info = {"installed": ok, "package": package, "install": drivers.install_hint(kind),
                    "status": drivers.get(kind).status}
        if ok and package != "built in":
            info["version"] = _dist_version(package)
        if kind == "sqlserver" and ok:
            mod = _import("pyodbc")
            try:
                info["odbc_drivers"] = [d for d in mod.drivers() if "SQL Server" in d]
            except Exception:
                info["odbc_drivers"] = []
            info["installed"] = bool(info["odbc_drivers"])
            if not info["installed"]:
                info["detail"] = "pyodbc is installed but no SQL Server ODBC driver was found."
        out[kind] = info
    return out


# ---------------------------------------------------------------- specs and secrets

# What decides where a secret is sent. A stored or environment secret goes only to the endpoint it
# was saved for; a form that changes any of these must bring the secret itself.
ENDPOINT_KEYS = tuple(dict.fromkeys(("kind", "host", "port", "database", "user", "dsn", "options", "path",
                                     "driver") + drivers.ENDPOINT_FIELD_NAMES))
# etl+'s own keys are never a connection password, whatever a file says.
ENV_DENY = {"ETLPLUS_TOKEN", "ETLPLUS_SECRET_KEY"}
# A password_env set from the console (not by hand in connections.yaml) must start with this.
ENV_PREFIX = "ETLPLUS_CONN_"


def secret_fields(kind) -> list[str]:
    d = drivers.REGISTRY.get(kind)
    names = [f.name for f in d.fields if f.secret] if d else []
    return names or ["password"]


def _is_secret_name(k) -> bool:
    k = str(k).lower()
    if k in drivers.SECRET_FIELD_NAMES or k.startswith("odbc:") or k.startswith("mongo:"):
        return True
    from . import config
    return config.is_secret_key(k)


def _norm(v):
    if v in (None, "", {}, []):
        return None
    if isinstance(v, dict):
        return tuple(sorted((str(k), _norm(x)) for k, x in v.items()))
    return str(v).strip()


def same_endpoint(a: dict, b: dict) -> bool:
    """True when two specs point at the same server, database, user, bucket, URL or path."""
    a, b = dict(a or {}), dict(b or {})
    a_opts, b_opts = dict(a.get("options") or {}), dict(b.get("options") or {})
    a.setdefault("dsn", a_opts.get("dsn"))
    b.setdefault("dsn", b_opts.get("dsn"))
    return all(_norm(a.get(k)) == _norm(b.get(k)) for k in ENDPOINT_KEYS)


def env_password(name) -> str | None:
    name = str(name or "").strip()
    if not name or name.upper() in ENV_DENY:
        return None
    return os.environ.get(name)


def _split_embedded(kind, spec: dict) -> dict:
    """Move a password embedded in an ODBC connection string or a MongoDB URI out of spec.
    Returns the secrets found ({} when none)."""
    found = {}
    if kind == "odbc" and spec.get("connection_string"):
        from .drivers import odbc
        cs, parts = odbc.separate(spec["connection_string"])
        spec["connection_string"] = cs
        found.update(parts)
    if kind == "mongodb" and spec.get("uri"):
        from .drivers import mongo
        uri, opts = mongo.separate_options(spec["uri"])
        found.update(opts)
        uri, user, pw = mongo.separate_uri(uri)
        spec["uri"] = uri
        if user and not spec.get("user"):
            spec["user"] = user
        if pw:
            found["password"] = pw
    return found


def spec_for(project, cid: str, override: dict | None = None) -> dict:
    """Saved spec merged with unsaved form values; secrets resolved, never stored.

    The stored secrets and password_env are used only while the form leaves the endpoint as
    saved; a changed host, port, database, user, URL, bucket, path or kind needs the secret in
    the form. password_env always comes from the saved connection, never from the form.
    _password is the password; _secrets holds every secret field (password included).
    """
    saved = dict((getattr(project, "connections", None) or {}).get(cid) or {})
    base = dict(saved)
    override = dict(override or {})
    typed = {}
    kind = override.get("kind") or saved.get("kind")
    for k in list(override):
        if _is_secret_name(k) and str(k).lower() != "password_env":
            v = override.pop(k)
            if v not in (None, ""):
                typed[str(k)] = str(v)
    override.pop("password_env", None)
    for k, v in override.items():
        if k in ("id", "clear_password") or str(k).endswith("_set"):
            continue
        base[k] = v
    if override:
        typed.update({k: v for k, v in _split_embedded(kind, base).items() if k not in typed})
    stored = {}
    if saved and cid and same_endpoint(saved, base):
        try:
            stored = secret_store.get(project, cid)
        except Exception:
            stored = {}
        if not stored.get("password") and saved.get("password_env"):
            pw = env_password(saved["password_env"])
            if pw:
                stored["password"] = pw
    merged = dict(stored)
    merged.update(typed)
    base["_secrets"] = merged
    base["_password"] = merged.get("password") or None
    base["_id"] = cid
    return base


def prepare_save(spec: dict, saved: dict | None = None, project=None) -> dict:
    """Split a connection form into what connections.yaml may hold and what goes to secrets.

    Returns {"spec": clean, "secrets": {field: value}, "clear": bool}. clear is true when the
    endpoint changed (a saved key is never sent to a new server): the caller deletes every
    stored secret first, then sets the new ones (secrets.update(..., clear=True)).
    Raises DriverError with a plain message on a value that is refused (section 4.6, 3.2).
    """
    body = dict(spec or {})
    kind = body.get("kind")
    drivers.get(kind)
    new_secrets = {}
    for k in list(body):
        if str(k).lower() == "password_env":
            continue
        if _is_secret_name(k):
            v = body.pop(k)
            if v not in (None, ""):
                new_secrets[str(k)] = str(v)
    for k in ("id", "password_set", "clear_password", "_password", "_secrets", "_id"):
        body.pop(k, None)
    for k in [k for k in body if str(k).endswith("_set")]:
        body.pop(k, None)
    opts = body.get("options")
    if isinstance(opts, dict):
        from . import config
        body["options"] = config.strip_secrets(opts)
    for k, v in _split_embedded(kind, body).items():
        new_secrets.setdefault(k, v)
    problems = validate_spec(body, project)
    if problems:
        raise DriverError(" ".join(problems))
    clear = bool(saved) and not same_endpoint(saved, body)
    return {"spec": body, "secrets": new_secrets, "clear": clear}


def public_spec(project, cid: str, spec: dict | None = None) -> dict:
    """A connection as the API returns it: no secret value, and <field>_set for each secret field."""
    spec = dict(spec if spec is not None else (getattr(project, "connections", None) or {}).get(cid) or {})
    out = {k: v for k, v in spec.items() if not _is_secret_name(k) and not str(k).startswith("_")}
    if isinstance(out.get("options"), dict):
        from . import config
        out["options"] = config.strip_secrets(out["options"])
    try:
        has = secret_store.has(project, cid)
    except Exception:
        has = {}
    for name in secret_fields(spec.get("kind")):
        out[f"{name}_set"] = bool(has.get(name))
    if spec.get("kind") == "odbc" and any(k.startswith("odbc:") for k in has):
        out["password_set"] = True
    out["id"] = cid
    return out


_URL_SECRET = re.compile(r"(sig|token|key|secret|password|signature)", re.I)
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


def _url_problem(name, url, opts, need_https=False) -> str | None:
    from urllib.parse import urlsplit
    try:
        u = urlsplit(str(url))
    except ValueError:
        return f"The {name} is not a valid URL."
    if u.scheme not in ("http", "https") or not u.netloc:
        return f"The {name} must start with https://."
    if u.query and _URL_SECRET.search(u.query):
        return "Put the SAS token in its own field." if name == "account URL" else \
            "Put the token in its own field, not in the URL."
    if u.username or u.password:
        return f"Put the password in its own field, not in the {name}."
    if need_https and u.scheme != "https" and not opts.get("allow_http"):
        return f"The {name} must be https:// unless options.allow_http is true."
    return None


def validate_spec(spec: dict, project=None) -> list[str]:
    """Problems with values that are refused at save. [] when the spec may be saved."""
    out = []
    kind = spec.get("kind")
    opts = dict(spec.get("options") or {})
    try:
        if kind in ("sqlserver", "azuresql") and spec.get("driver"):
            from .drivers.mssql import check_driver_name
            check_driver_name(spec["driver"])
        if kind == "odbc" and spec.get("connection_string"):
            from .drivers import odbc
            odbc.separate(spec["connection_string"])
    except DriverError as e:
        out.append(str(e))
    if kind == "azure_blob" and spec.get("account_url"):
        p = _url_problem("account URL", spec["account_url"], opts, need_https=True)
        if p:
            out.append(p)
    if kind == "s3" and spec.get("endpoint_url"):
        p = _url_problem("endpoint URL", spec["endpoint_url"], opts, need_https=True)
        if p:
            out.append(p)
    if kind == "http" and spec.get("url"):
        p = _url_problem("URL", spec["url"], opts)
        if p:
            out.append(p)
        else:
            from .remote import paths
            try:
                paths.fill_template(str(spec["url"]), date=dt.date.today(), allowed=("date",))
            except ValueError as e:
                out.append(str(e))
    if kind == "sftp" and spec.get("host_key_fingerprint") and \
            not _FINGERPRINT.match(str(spec["host_key_fingerprint"]).strip()):
        out.append("The host key fingerprint looks like SHA256:<43 characters>, as Test shows it.")
    if kind == "ftp" and spec.get("tls") in (False, "false", "no", 0, "0") and not opts.get("allow_plain"):
        out.append("Plain FTP sends the password in clear; set options.allow_plain to use it.")
    if kind == "share" and spec.get("path") and project is not None:
        from .remote import paths
        try:
            paths.share_root(project, spec["path"])
        except ValueError as e:
            out.append(str(e))
    return out


def is_read_only(spec: dict) -> bool:
    return (spec.get("role") or "source") == "source"


def file_path(project, spec: dict) -> Path:
    """The path of a folder, SQLite or DuckDB connection. A relative path stays in the project.
    It may never be a secret or state file of any project, nor the other project's folder."""
    raw = spec.get("path") or spec.get("database")
    if not raw:
        raise ConnectionError_("Set a path for this connection.")
    raw = str(raw)
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.is_absolute():
        full = p
    else:
        full = (project.path / p).resolve()
        try:
            full.relative_to(project.path.resolve())
        except ValueError:
            raise ConnectionError_(
                f"The path '{raw}' leaves the project folder. Use a path inside the project, "
                "or an absolute path.") from None
    from .remote import paths
    try:
        paths.check_local(project, full)
    except ValueError as e:
        raise ConnectionError_(str(e)) from None
    return full


def sqlserver_driver(spec: dict, installed=None) -> str:
    from .drivers import mssql
    if installed is None:
        installed = list(driver(spec.get("kind") or "sqlserver").drivers())
    return mssql.detect_driver(spec, installed)


def _odbc_value(v) -> str:
    from .drivers.mssql import odbc_value
    return odbc_value(v)


def sqlserver_conn_str(spec: dict, password=None, read_only=True, installed=None) -> str:
    from .drivers import mssql
    if installed is None:
        installed = list(driver(spec.get("kind") or "sqlserver").drivers())
    return mssql.conn_str(spec, password, read_only, installed)


def sqlite_uri(path: Path, read_only=True) -> str:
    uri = Path(path).resolve().as_uri()
    return uri + ("?mode=ro" if read_only else "?mode=rwc")


def connect(project, cid: str, override: dict | None = None, read_only: bool | None = None, spec=None):
    """A DB-API connection (a MongoDB database handle for mongodb). read_only defaults to the
    connection's role."""
    spec = spec or spec_for(project, cid, override)
    kind = spec.get("kind")
    if kind not in KINDS or not drivers.is_database(kind):
        raise ConnectionError_(f"Connection '{cid}' ({kind}) is not a database.")
    ro = is_read_only(spec) if read_only is None else read_only
    if not ro and is_read_only(spec):
        raise ConnectionError_(f"Connection '{cid}' is a source; it is opened read-only.")
    return drivers.connect(project, cid, role="source" if ro else "target", spec=spec)


def close(conn):
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------- SQL helpers per dialect

def _split_name(name: str) -> list[str]:
    try:
        return drivers.split_name(name)
    except DriverError as e:
        raise ValueError(str(e)) from None


def quote_ident(kind: str, name: str) -> str:
    return drivers.quote_ident(kind, name)


def quote_table(kind: str, name: str) -> str:
    return drivers.quote_table(kind, name)


def unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] in '"`[' and name[-1] in '"`]':
        return name[1:-1]
    return name


def split_table(name: str) -> tuple[str | None, str]:
    return drivers.split_table(name)


def placeholder(kind: str, i: int) -> str:
    return drivers.placeholder(kind, i)


def limit_sql(kind: str, sql: str, n: int) -> str:
    out = drivers.limit_sql(kind, sql, n)
    return sql if out is None else out


def select_sql(kind: str, table: str, columns="*", where: str = "", limit: int | None = None) -> str:
    return drivers.select_sql(kind, table, columns, where, limit)


def read_sql(kind, sql: str) -> str:
    """The SQL actually sent for a guarded read (Teradata gets LOCKING ROW FOR ACCESS)."""
    if drivers.family(kind) == "teradata":
        from .drivers import teradata
        return teradata.read_sql(sql)
    return sql


def execute(conn, kind: str, sql: str, params=None):
    """Run one guarded SELECT and return the cursor."""
    sql = read_sql(kind, guard.check_select(sql, kind))
    if drivers.family(kind) == "duckdb":
        return conn.execute(sql, list(params or []))
    cur = conn.cursor()
    if params:
        cur.execute(sql, drivers.bind_params(kind, params))
    else:
        cur.execute(sql)
    return cur


def fetch(conn, kind, sql, params=None, limit=None):
    cur = execute(conn, kind, sql, params)
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchmany(limit) if limit else cur.fetchall()
    return cols, [list(r) for r in rows]


def jsonable(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, decimal.Decimal):
        return float(v) if v == v.to_integral_value() or abs(v.as_tuple().exponent) <= 15 else str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    return str(v)


# ---------------------------------------------------------------- metadata

VERSION_SQL = {k: drivers.version_sql(k) for k in drivers.kinds() if drivers.version_sql(k)}
ORACLE_SYSTEM = drivers.ORACLE_SYSTEM


def tables_sql(kind: str, schema: str | None):
    """(sql, params) listing schema, name, kind, rows."""
    return drivers.tables_sql(kind, schema)


def columns_sql(kind: str, table: str):
    return drivers.columns_sql(kind, table)


def _folder_files(root: Path, limit=5000):
    from .remote import share
    return [root / f.path for f in share.walk_local(root, limit=limit)]


FILE_EXTENSIONS = ((".csv", "csv"), (".tsv", "txt"), (".txt", "txt"), (".dat", "txt"),
                   (".parquet", "parquet"), (".pq", "parquet"), (".jsonl", "jsonl"),
                   (".ndjson", "jsonl"), (".json", "json"), (".xlsx", "excel"), (".xlsm", "excel"))


def _formats():
    if importlib.util.find_spec("etlplus.formats") is None:
        return None
    return importlib.import_module("etlplus.formats")


def guess_format(name: str, head: bytes | None = None, project=None) -> str | None:
    """The format of a file: from its first bytes when given (bank formats are detected), else
    from its extension."""
    if head is not None:
        fm = _formats()
        if fm is not None and hasattr(fm, "detect"):
            try:
                found = fm.detect(Path(name), head=head, project=project)
            except Exception:
                found = []
            if found and float(found[0].get("confidence") or 0) >= 0.7:
                return found[0]["format"]
    n = name.lower()
    for ext, fmt in FILE_EXTENSIONS:
        if n.endswith(ext) or n.endswith(ext + ".gz"):
            return fmt
    return None


def list_files(project, cid: str, pattern="**/*", limit: int = 5000) -> list[dict]:
    """[{path, size, modified, format}] for a folder, shared folder or remote location."""
    spec = spec_for(project, cid)
    kind = spec.get("kind")
    if kind not in FILE_KINDS:
        raise ConnectionError_(f"Connection '{cid}' ({kind}) holds no files.")
    from . import remote
    files = remote.list_files(project, cid, pattern or "**/*", limit=limit, spec=spec)
    return [{"path": f.path, "size": f.size, "modified": f.modified, "format": guess_format(f.path)}
            for f in files]


def list_tables(project, cid: str, schema: str | None = None) -> list[dict]:
    spec = spec_for(project, cid)
    kind = spec.get("kind")
    if kind in FILE_KINDS:
        out = []
        for f in list_files(project, cid):
            out.append({"name": f["path"], "schema": None, "kind": "file", "rows": None, "bytes": f["size"],
                        "format": f["format"], "modified": f["modified"]})
        return out
    if kind == "mongodb":
        db = connect(project, cid, spec=spec)
        try:
            return [{"name": n, "schema": None, "kind": "collection", "rows": None} for n in db.collections()]
        finally:
            close(db)
    conn = connect(project, cid, spec=spec)
    try:
        if drivers.family(kind) == "odbc":
            rows = _odbc_tables(conn, schema)
        else:
            sql, params = tables_sql(kind, schema)
            _, rows = fetch(conn, kind, sql, params)
    finally:
        close(conn)
    out = []
    for sch, name, typ, n in rows:
        t = str(typ or "").upper()
        rows_n = None
        try:
            rows_n = int(n) if n is not None and float(n) >= 0 else None
        except (TypeError, ValueError):
            pass
        out.append({"name": name, "schema": sch, "kind": "view" if "VIEW" in t else "table", "rows": rows_n})
    return out


def _odbc_tables(conn, schema=None):
    """ODBC catalog functions: they work on servers without information_schema."""
    cur = conn.cursor()
    rows = []
    for r in cur.tables(schema=schema) if schema else cur.tables():
        typ = str(getattr(r, "table_type", r[3]) or "")
        if typ.upper() not in ("TABLE", "VIEW"):
            continue
        rows.append((getattr(r, "table_schem", r[1]), getattr(r, "table_name", r[2]), typ, None))
        if len(rows) >= 5000:
            break
    return rows


def list_columns(project, cid: str, table: str) -> list[dict]:
    spec = spec_for(project, cid)
    kind = spec.get("kind")
    if kind in FILE_KINDS:
        from . import sources
        schema = sources.file_schema(project, cid, table)
        return [{"name": f.name, "type": str(f.type), "nullable": f.nullable,
                 "generic": drivers.arrow_to_generic(f.type)} for f in schema]
    if kind == "mongodb":
        from . import sources
        schema = sources.mongo_schema(project, cid, table, spec=spec)
        return [{"name": f.name, "type": str(f.type), "nullable": True,
                 "generic": drivers.arrow_to_generic(f.type)} for f in schema]
    conn = connect(project, cid, spec=spec)
    try:
        if drivers.family(kind) == "odbc":
            schema_name, name = split_table(table)
            cur = conn.cursor()
            rows = [(c.column_name, c.type_name, c.nullable) for c in cur.columns(table=name, schema=schema_name)]
        else:
            sql, params = columns_sql(kind, table)
            _, rows = fetch(conn, kind, sql, params)
    finally:
        close(conn)
    return [{"name": r[0], "type": str(r[1] or ""), "nullable": bool(r[2]),
             "generic": drivers.generic_type(kind, str(r[1] or ""))} for r in rows]


def preview(project, cid: str, table_or_files: str, fmt=None, options=None, limit=20) -> dict:
    limit = max(1, min(int(limit or 20), 1000))
    spec = spec_for(project, cid)
    kind = spec.get("kind")
    if kind in FILE_KINDS:
        from . import sources
        fm = _formats()
        if fm is not None and fmt and fmt in getattr(fm, "FORMATS", {}):
            return sources.preview_bank(project, cid, table_or_files, fmt, options, limit, spec=spec)
        table = sources.preview_files(project, cid, table_or_files, fmt, options, limit)
        cols = table.column_names
        data = [[jsonable(v) for v in row.values()] for row in table.to_pylist()]
        return {"columns": cols, "types": [str(t) for t in table.schema.types], "rows": data}
    if kind == "mongodb":
        from . import sources
        table = sources.preview_mongo(project, cid, table_or_files, options, limit, spec=spec)
        return {"columns": table.column_names, "types": [str(t) for t in table.schema.types],
                "rows": [[jsonable(v) for v in row.values()] for row in table.to_pylist()]}
    text = (table_or_files or "").strip()
    if re.match(r"(?is)^\(?\s*(select|with|locking)\b", text):
        checked = guard.check_select(text, kind)
        if drivers.family(kind) in ("sqlserver", "teradata"):
            sql = checked  # TOP around a query with ORDER BY fails there; fetchmany limits instead
        else:
            sql = limit_sql(kind, checked, limit)
    else:
        sql = select_sql(kind, text, "*", limit=limit if drivers.limit_style(kind) != "none" else None)
    conn = connect(project, cid, spec=spec)
    try:
        cols, rows = fetch(conn, kind, sql, None, limit)
    finally:
        close(conn)
    return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows]}


# ---------------------------------------------------------------- test

def _step(steps, name, fn):
    t0 = time.perf_counter()
    try:
        detail = fn()
        steps.append({"name": name, "ok": True, "detail": str(detail or "ok"),
                      "ms": round((time.perf_counter() - t0) * 1000)})
        return True
    except Exception as e:
        msg = str(e).strip() or e.__class__.__name__
        steps.append({"name": name, "ok": False, "detail": msg[:500],
                      "ms": round((time.perf_counter() - t0) * 1000)})
        return False


def _redact(msg: str, password) -> str:
    secrets = password if isinstance(password, (list, tuple, set)) else [password]
    for s in secrets:
        if s and len(str(s)) >= 3:
            msg = msg.replace(str(s), "***")
    return msg


def _settings_check(project, spec):
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError(f"Pick a kind: {', '.join(sorted(KINDS))}.")
    d = drivers.get(kind)
    opts = spec.get("options") or {}
    for f in d.fields:
        if not f.required or f.secret or f.name == "host_key_fingerprint":
            continue  # a missing fingerprint: remote.test scans and shows it
        if spec.get(f.name) in (None, ""):
            if kind == "oracle" and f.name == "database" and opts.get("dsn"):
                continue
            if kind == "mongodb" and f.name == "uri" and spec.get("host"):
                continue
            raise ValueError(f"Set the {f.label[0].lower() + f.label[1:]}.")
    if kind == "odbc" and not (spec.get("dsn") or spec.get("connection_string")):
        raise ValueError("Set a DSN or a connection string.")
    problems = validate_spec(spec, project)
    if problems:
        raise ValueError(" ".join(problems))
    if kind in FILE_DB or kind == "folder":
        return str(file_path(project, spec))
    role = spec.get("role") or "source"
    where = spec.get("host") or spec.get("account") or spec.get("dsn") or spec.get("uri") or "connection string"
    port = spec.get("port") or d.default_port
    return f"{kind} {where}{':' + str(port) if port and spec.get('host') else ''}, role {role}"


def test(project, cid: str, override: dict | None = None) -> dict:
    """Try a connection step by step. override holds unsaved form values, secrets included."""
    steps = []
    server = None
    try:
        spec = spec_for(project, cid, override)
    except Exception as e:
        return {"ok": False, "steps": [{"name": "settings", "ok": False, "detail": str(e), "ms": 0}],
                "server": None}
    kind = spec.get("kind")
    secrets = list((spec.get("_secrets") or {}).values())

    if kind in REMOTE:
        if not _step(steps, "settings", lambda: _settings_check(project, spec)):
            return {"ok": False, "steps": steps, "server": None}
        from . import remote
        more = remote.test(project, cid, override, spec=spec)
        for s in more:
            s["detail"] = _redact(str(s.get("detail") or ""), secrets)
        steps.extend(more)
        return {"ok": all(s["ok"] for s in steps), "steps": steps, "server": drivers.get(kind).label}

    if not _step(steps, "settings", lambda: _settings_check(project, spec)):
        return {"ok": False, "steps": steps, "server": None}

    if kind == "folder":
        root = file_path(project, spec)

        def exists():
            if not root.exists():
                if is_read_only(spec):
                    raise FileNotFoundError(f"{root} does not exist.")
                return f"{root} does not exist yet; it is created on the first load."
            if not root.is_dir():
                raise NotADirectoryError(f"{root} is not a folder.")
            return str(root)

        ok = _step(steps, "path", exists)
        if ok and root.is_dir():
            def readable():
                files = _folder_files(root)
                return f"{len(files)} file(s)"
            ok = _step(steps, "read", readable)
            if ok and not is_read_only(spec):
                def writable():
                    probe = root / f".etlplus-probe-{os.getpid()}"
                    probe.write_bytes(b"")
                    probe.unlink()
                    return "can write"
                ok = _step(steps, "write", writable)
        return {"ok": all(s["ok"] for s in steps), "steps": steps, "server": "folder"}

    def drv():
        mod = driver(kind)
        if drivers.family(kind) == "sqlserver":
            return sqlserver_driver(spec, list(mod.drivers()))
        d = drivers.get(kind)
        version = getattr(mod, "__version__", "") if d.pip == "built in" else _dist_version(d.pip)
        return f"{d.module} {version}".strip()

    if not _step(steps, "driver", drv):
        return {"ok": False, "steps": steps, "server": None}

    holder = {}

    def do_connect():
        try:
            holder["conn"] = connect(project, cid, spec=spec)
        except Exception as e:
            raise RuntimeError(_redact(str(e), secrets)) from None
        return "read-only session" if is_read_only(spec) else "connected"

    if _step(steps, "connect", do_connect):
        conn = holder["conn"]
        try:
            if kind == "mongodb":
                def ping():
                    nonlocal server
                    info = conn.client.server_info()
                    server = f"MongoDB {info.get('version', '')}".strip()
                    return server
                _step(steps, "query", ping)
                _step(steps, "tables", lambda: f"{len(conn.collections())} collection(s) visible")
                return {"ok": all(s["ok"] for s in steps), "steps": steps, "server": server}

            def version():
                nonlocal server
                vsql = drivers.version_sql(kind)
                if vsql is None:
                    getinfo = getattr(conn, "getinfo", None)
                    server = str(getinfo(17)) if getinfo else drivers.get(kind).label  # SQL_DBMS_NAME
                    return server
                try:
                    _, rows = fetch(conn, kind, vsql)
                    server = str(rows[0][0]).splitlines()[0][:200] if rows else None
                except Exception:
                    if drivers.family(kind) != "oracle":
                        raise
                    # v$version needs a grant; fall back to the client's view of the server
                    fetch(conn, kind, "SELECT 1 FROM dual")
                    server = f"Oracle {getattr(conn, 'version', '')}".strip()
                return server
            _step(steps, "query", version)

            if kind in ("postgres", "timescaledb", "greenplum", "yugabytedb", "redshift", "cockroachdb") \
                    and is_read_only(spec):
                def ro():
                    _, rows = fetch(conn, kind, "SELECT current_setting('transaction_read_only')")
                    if rows[0][0] != "on":
                        raise RuntimeError("The session is not read-only.")
                    return "transaction_read_only = on"
                _step(steps, "read only", ro)

            def tables():
                if drivers.family(kind) == "odbc":
                    return f"{len(_odbc_tables(conn))} table(s) and view(s) visible"
                sql, params = tables_sql(kind, None)
                _, rows = fetch(conn, kind, sql, params)
                return f"{len(rows)} table(s) and view(s) visible"
            _step(steps, "tables", tables)
        finally:
            close(conn)
    return {"ok": all(s["ok"] for s in steps), "steps": steps, "server": server}
