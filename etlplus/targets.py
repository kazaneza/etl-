"""Loaders for every target kind: replace, append (window slice), upsert (merge on key)."""
from __future__ import annotations

import json
import os
import re
import time as _time
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

MODES = ("replace", "append", "upsert")
V1_DB_KINDS = ("sqlite", "duckdb", "postgres", "sqlserver", "mysql", "oracle")
FILE_FORMATS = ("parquet", "csv", "jsonl")
REMOTE_KINDS = ("share", "sftp", "ftp", "s3", "azure_blob", "http")
BATCH_ROWS = 10_000


def _db_kinds() -> tuple:
    """Every kind that can be a database target: from the driver registry when it is there."""
    try:
        from etlplus import drivers
        ks = [k for k, d in drivers.REGISTRY.items()
              if "target" in d.roles and d.category in ("database",)]
        return tuple(dict.fromkeys(list(V1_DB_KINDS) + ks))
    except Exception:
        return V1_DB_KINDS


DB_KINDS = _db_kinds()

DRIVERS = {
    "postgres": ("psycopg", "postgres"),
    "sqlserver": ("pyodbc", "sqlserver"),
    "mysql": ("pymysql", "mysql"),
    "oracle": ("oracledb", "oracle"),
}


class LoadError(RuntimeError):
    pass


# ---------------------------------------------------------------- dialects

def _split(name: str) -> list[str]:
    parts = [p.strip() for p in str(name).split(".") if p.strip()]
    out = []
    for p in parts:
        if len(p) >= 2 and ((p[0], p[-1]) in (('"', '"'), ("[", "]"), ("`", "`"))):
            p = p[1:-1]
        out.append(p)
    if not out:
        raise LoadError("The target table name is empty.")
    return out


class Dialect:
    name = "generic"
    ph_style = "?"
    transactional_ddl = True
    truncate = False
    open_q, close_q = '"', '"'

    def quote(self, ident: str) -> str:
        return self.open_q + str(ident).replace(self.close_q, self.close_q * 2) + self.close_q

    def table(self, name: str) -> str:
        return ".".join(self.quote(p) for p in _split(name))

    def ph(self, i: int) -> str:
        return self.ph_style

    def phs(self, n: int) -> str:
        return ", ".join(self.ph(i + 1) for i in range(n))

    # types
    def t_string(self): return "TEXT"
    def t_int(self, bits): return {8: "SMALLINT", 16: "SMALLINT", 32: "INTEGER"}.get(bits, "BIGINT")
    def t_float(self): return "DOUBLE PRECISION"
    def t_bool(self): return "BOOLEAN"
    def t_date(self): return "DATE"
    def t_time(self): return "TIME"
    def t_ts(self, tz): return "TIMESTAMP WITH TIME ZONE" if tz else "TIMESTAMP"
    def t_decimal(self, p, s): return f"NUMERIC({min(p, 38)},{s})"
    def t_binary(self): return "BLOB"

    def type_for(self, t: pa.DataType) -> str:
        if pa.types.is_boolean(t):
            return self.t_bool()
        if pa.types.is_integer(t):
            bits = t.bit_width + (8 if pa.types.is_unsigned_integer(t) else 0)
            return self.t_int(64 if bits > 32 else (32 if bits > 16 else bits))
        if pa.types.is_floating(t):
            return self.t_float()
        if pa.types.is_decimal(t):
            return self.t_decimal(t.precision, t.scale)
        if pa.types.is_timestamp(t):
            return self.t_ts(bool(t.tz))
        if pa.types.is_date(t):
            return self.t_date()
        if pa.types.is_time(t):
            return self.t_time()
        if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t):
            return self.t_binary()
        if pa.types.is_dictionary(t):
            return self.type_for(t.value_type)
        return self.t_string()

    def create_sql(self, table: str, schema: pa.Schema) -> str:
        cols = ",\n    ".join(f"{self.quote(f.name)} {self.type_for(f.type)}" for f in schema)
        return f"CREATE TABLE {self.table(table)} (\n    {cols}\n)"

    def clear_sql(self, table: str) -> str:
        return f"TRUNCATE TABLE {self.table(table)}" if self.truncate else f"DELETE FROM {self.table(table)}"

    def delete_slice_sql(self, table: str, column: str) -> str:
        c = self.quote(column)
        return f"DELETE FROM {self.table(table)} WHERE {c} >= {self.ph(1)} AND {c} < {self.ph(2)}"

    def delete_range_sql(self, table: str, column: str) -> str:
        c = self.quote(column)
        return f"DELETE FROM {self.table(table)} WHERE {c} >= {self.ph(1)} AND {c} <= {self.ph(2)}"

    def insert_sql(self, table: str, cols: list[str]) -> str:
        return (f"INSERT INTO {self.table(table)} ({', '.join(self.quote(c) for c in cols)}) "
                f"VALUES ({self.phs(len(cols))})")

    def stage_name(self) -> str:
        return "etlplus_stage"

    def create_stage_sql(self, table: str, cols: list[str]) -> str:
        sel = ", ".join(self.quote(c) for c in cols)
        return f"CREATE TEMPORARY TABLE {self.quote(self.stage_name())} AS SELECT {sel} FROM {self.table(table)} WHERE 1 = 0"

    def drop_stage_sql(self) -> str:
        return f"DROP TABLE IF EXISTS {self.quote(self.stage_name())}"

    def upsert_sql(self, table: str, cols: list[str], key: list[str]) -> list[str]:
        """Delete the target rows whose key is in the stage table, then insert the stage table."""
        t, s = self.table(table), self.quote(self.stage_name())
        on = " AND ".join(f"{t}.{self.quote(k)} = s.{self.quote(k)}" for k in key)
        names = ", ".join(self.quote(c) for c in cols)
        return [
            f"DELETE FROM {t} WHERE EXISTS (SELECT 1 FROM {s} s WHERE {on})",
            f"INSERT INTO {t} ({names}) SELECT {names} FROM {s}",
        ]

    def adapt(self, v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str)
        return v


class SQLite(Dialect):
    name = "sqlite"

    def table(self, name):
        parts = _split(name)
        if len(parts) > 1 and parts[0].lower() == "main":
            parts = parts[1:]
        return ".".join(self.quote(p) for p in parts)

    def t_int(self, bits): return "INTEGER"
    def t_float(self): return "REAL"
    def t_bool(self): return "INTEGER"
    def t_ts(self, tz): return "TEXT"
    def t_date(self): return "TEXT"
    def t_time(self): return "TEXT"
    def t_decimal(self, p, s): return "NUMERIC"

    def create_stage_sql(self, table, cols):
        sel = ", ".join(self.quote(c) for c in cols)
        return f"CREATE TEMP TABLE {self.quote(self.stage_name())} AS SELECT {sel} FROM {self.table(table)} WHERE 0"

    def adapt(self, v):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, datetime):
            return str(v)
        if isinstance(v, (date, time)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return str(v)
        return super().adapt(v)


class DuckDB(Dialect):
    name = "duckdb"

    def t_float(self): return "DOUBLE"
    def t_string(self): return "VARCHAR"
    def t_ts(self, tz): return "TIMESTAMPTZ" if tz else "TIMESTAMP"
    def t_decimal(self, p, s): return f"DECIMAL({min(p, 38)},{s})"


class Postgres(Dialect):
    name = "postgres"
    ph_style = "%s"
    truncate = True

    def t_ts(self, tz): return "TIMESTAMPTZ" if tz else "TIMESTAMP"
    def t_binary(self): return "BYTEA"

    def create_stage_sql(self, table, cols):
        sel = ", ".join(self.quote(c) for c in cols)
        return (f"CREATE TEMP TABLE {self.quote(self.stage_name())} ON COMMIT DROP AS "
                f"SELECT {sel} FROM {self.table(table)} WHERE false")

    def copy_sql(self, table: str, cols: list[str]) -> str:
        return f"COPY {table} ({', '.join(self.quote(c) for c in cols)}) FROM STDIN"

    def upsert_sql(self, table, cols, key):
        t, s = self.table(table), self.quote(self.stage_name())
        on = " AND ".join(f"t.{self.quote(k)} = s.{self.quote(k)}" for k in key)
        names = ", ".join(self.quote(c) for c in cols)
        return [
            f"DELETE FROM {t} t USING {s} s WHERE {on}",
            f"INSERT INTO {t} ({names}) SELECT {names} FROM {s}",
        ]


class SQLServer(Dialect):
    name = "sqlserver"
    open_q, close_q = "[", "]"
    truncate = True

    def t_string(self): return "NVARCHAR(MAX)"
    def t_int(self, bits): return {8: "SMALLINT", 16: "SMALLINT", 32: "INT"}.get(bits, "BIGINT")
    def t_float(self): return "FLOAT"
    def t_bool(self): return "BIT"
    def t_ts(self, tz): return "DATETIMEOFFSET" if tz else "DATETIME2"
    def t_decimal(self, p, s): return f"DECIMAL({min(p, 38)},{s})"
    def t_binary(self): return "VARBINARY(MAX)"

    def stage_name(self):
        return "#etlplus_stage"

    def create_stage_sql(self, table, cols):
        sel = ", ".join(self.quote(c) for c in cols)
        t = self.table(table)
        # the UNION ALL drops an IDENTITY property the target column may carry
        return (f"SELECT TOP 0 {sel} INTO {self.quote(self.stage_name())} FROM {t} "
                f"UNION ALL SELECT TOP 0 {sel} FROM {t}")

    def drop_stage_sql(self):
        return f"IF OBJECT_ID('tempdb..{self.stage_name()}') IS NOT NULL DROP TABLE {self.quote(self.stage_name())}"

    def upsert_sql(self, table, cols, key):
        t, s = self.table(table), self.quote(self.stage_name())
        on = " AND ".join(f"t.{self.quote(k)} = s.{self.quote(k)}" for k in key)
        rest = [c for c in cols if c not in key]
        names = ", ".join(self.quote(c) for c in cols)
        vals = ", ".join(f"s.{self.quote(c)}" for c in cols)
        sql = f"MERGE INTO {t} WITH (HOLDLOCK) AS t\nUSING {s} AS s\n    ON {on}\n"
        if rest:
            sets = ", ".join(f"t.{self.quote(c)} = s.{self.quote(c)}" for c in rest)
            sql += f"WHEN MATCHED THEN UPDATE SET {sets}\n"
        sql += f"WHEN NOT MATCHED BY TARGET THEN INSERT ({names}) VALUES ({vals});"
        return [sql]

    def bulk_insert_sql(self, into: str, remote_file: str, remote_err: str) -> str:
        def lit(s):
            return "'" + s.replace("'", "''") + "'"
        return (
            f"BULK INSERT {into}\nFROM {lit(remote_file)}\nWITH (\n"
            "    FORMAT = 'CSV',\n    FIELDQUOTE = '\"',\n    FIRSTROW = 2,\n"
            "    FIELDTERMINATOR = ',',\n    ROWTERMINATOR = '0x0a',\n    CODEPAGE = '65001',\n"
            "    KEEPNULLS,\n    TABLOCK,\n    BATCHSIZE = 50000,\n    MAXERRORS = 0,\n"
            f"    ERRORFILE = {lit(remote_err)}\n)"
        )


class MySQL(Dialect):
    name = "mysql"
    ph_style = "%s"
    transactional_ddl = False
    open_q, close_q = "`", "`"

    def t_float(self): return "DOUBLE"
    def t_ts(self, tz): return "DATETIME(6)"
    def t_decimal(self, p, s): return f"DECIMAL({min(p, 65)},{min(s, 30)})"
    def t_binary(self): return "LONGBLOB"
    def t_string(self): return "LONGTEXT"

    def upsert_sql(self, table, cols, key):
        t, s = self.table(table), self.quote(self.stage_name())
        on = " AND ".join(f"t.{self.quote(k)} = s.{self.quote(k)}" for k in key)
        names = ", ".join(self.quote(c) for c in cols)
        return [
            f"DELETE t FROM {t} AS t JOIN {s} AS s ON {on}",
            f"INSERT INTO {t} ({names}) SELECT {names} FROM {s}",
        ]


class Oracle(Dialect):
    name = "oracle"
    transactional_ddl = False

    def quote(self, ident):
        return '"' + str(ident).upper().replace('"', '""') + '"'

    def ph(self, i):
        return f":{i}"

    def t_string(self): return "VARCHAR2(4000 CHAR)"
    def t_int(self, bits): return {8: "NUMBER(5)", 16: "NUMBER(5)", 32: "NUMBER(10)"}.get(bits, "NUMBER(19)")
    def t_float(self): return "BINARY_DOUBLE"
    def t_bool(self): return "NUMBER(1)"
    def t_time(self): return "VARCHAR2(20)"
    def t_decimal(self, p, s): return f"NUMBER({min(p, 38)},{s})"

    def upsert_sql(self, table, cols, key):
        """One MERGE per row, run with executemany: no temporary table (DDL commits in Oracle)."""
        t = self.table(table)
        src = ", ".join(f"{self.ph(i + 1)} AS {self.quote(c)}" for i, c in enumerate(cols))
        on = " AND ".join(f"t.{self.quote(k)} = s.{self.quote(k)}" for k in key)
        rest = [c for c in cols if c not in key]
        names = ", ".join(self.quote(c) for c in cols)
        vals = ", ".join(f"s.{self.quote(c)}" for c in cols)
        sql = f"MERGE INTO {t} t USING (SELECT {src} FROM dual) s ON ({on})"
        if rest:
            sql += " WHEN MATCHED THEN UPDATE SET " + ", ".join(f"t.{self.quote(c)} = s.{self.quote(c)}" for c in rest)
        sql += f" WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({vals})"
        return [sql]

    def adapt(self, v):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, time):
            return v.isoformat()
        return super().adapt(v)


class Generic(Dialect):
    """Any other database kind: quoting, placeholders and types from the driver registry
    (etlplus.ddl), upsert by delete then insert in one transaction."""

    def __init__(self, kind: str):
        from etlplus import ddl
        self.kind = kind
        self.name = kind
        self.fam = ddl.family(kind)
        self.truncate = self.fam in ("clickhouse", "snowflake", "teradata", "db2", "hana")
        self.transactional_ddl = self.fam not in ("clickhouse", "teradata", "oracle", "mysql", "db2")

    def quote(self, ident):
        from etlplus import ddl
        return ddl.quote_ident(self.kind, ident)

    def table(self, name):
        from etlplus import ddl
        return ddl.quote_table(self.kind, name)

    def ph(self, i):
        try:
            from etlplus import drivers
            return drivers.placeholder(self.kind, i)
        except Exception:
            return {"postgres": "%s", "mysql": "%s", "snowflake": "%s", "oracle": f":{i}"}.get(self.fam, "?")

    def type_for(self, t):
        from etlplus import ddl
        return ddl.native_type(self.kind, ddl.arrow_to_generic(t))

    def create_stage_sql(self, table, cols):
        sel = ", ".join(self.quote(c) for c in cols)
        return f"CREATE TEMPORARY TABLE {self.quote(self.stage_name())} AS SELECT {sel} FROM {self.table(table)} WHERE 1 = 0"


DIALECTS = {"sqlite": SQLite, "duckdb": DuckDB, "postgres": Postgres, "sqlserver": SQLServer,
            "mysql": MySQL, "oracle": Oracle}
# kinds that behave as a v1 dialect
ALIASES = {"mariadb": "mysql", "azuresql": "sqlserver", "timescaledb": "postgres", "yugabytedb": "postgres",
           "greenplum": "postgres"}


def dialect(kind: str) -> Dialect:
    if kind in DIALECTS:
        return DIALECTS[kind]()
    if kind in ALIASES:
        d = DIALECTS[ALIASES[kind]]()
        return d
    if kind in DB_KINDS or kind in _db_kinds():
        try:
            return Generic(kind)
        except Exception:
            pass
    raise LoadError(f"'{kind}' is not a database target kind.")


# ---------------------------------------------------------------- helpers

def _log(log, msg):
    if log:
        log(msg)


def _check(log):
    fn = getattr(log, "check", None)
    if fn:
        fn()


def _conn_spec(project, job) -> tuple[str, dict]:
    t = job.get("target") or {}
    cid = t.get("connection")
    if not cid or cid not in project.connections:
        raise LoadError(f"The target connection '{cid}' does not exist.")
    spec = dict(project.connections[cid])
    if spec.get("role") == "source":
        raise LoadError(f"Connection '{cid}' is a source (read only). Set its role to target or both to load into it.")
    return cid, spec


def _password(project, cid, spec) -> str | None:
    if spec.get("password_env"):
        from etlplus.connections import env_password
        v = env_password(spec["password_env"])
        if v:
            return v
    try:
        from etlplus import secrets
        return (secrets.get(project, cid) or {}).get("password")
    except ImportError:
        return None


def _require(kind):
    mod, extra = DRIVERS[kind]
    try:
        return __import__(mod)
    except ImportError:
        raise LoadError(f"The {kind} driver is not installed. Install it with: pip install etlplus[{extra}] "
                        f"(package {mod}).") from None


def _file_db(project, spec) -> Path:
    p = spec.get("path") or spec.get("database")
    if not p:
        raise LoadError("The connection has no path to its database file.")
    return Path(project.resolve(p))


def file_db_path(project, spec) -> Path:
    return _file_db(project, spec)


class file_lock:
    """One writer at a time for a SQLite or DuckDB target file: <file>.etlplus.lock, the JobLock
    mechanism. A second load waits (and can be cancelled) instead of failing on the file lock."""

    def __init__(self, project, spec, log=None, wait_s: float = 3600):
        self.project, self.spec, self.log, self.wait_s = project, dict(spec or {}), log, wait_s
        self._lk = None

    def __enter__(self):
        if self.spec.get("kind") not in ("sqlite", "duckdb"):
            return self
        from etlplus.runner import JobLock
        path = _file_db(self.project, self.spec)
        lk = JobLock(self.project, "x")
        lk.path = path.with_name(path.name + ".etlplus.lock")
        start = _time.time()
        told = False
        while not lk.acquire():
            if self.wait_s <= 0:
                raise LoadError(f"Another load is writing to {path.name}.")
            if not told:
                _log(self.log, f"load: waiting for another load into {path.name}")
                told = True
            _check(self.log)
            if _time.time() - start > self.wait_s:
                raise LoadError(f"Another load kept {path.name} busy for too long.")
            _time.sleep(0.2)
        self._lk = lk
        return self

    def __exit__(self, *exc):
        if self._lk is not None:
            self._lk.release()
            self._lk = None
        return False


def _drivers():
    try:
        from etlplus import drivers
        return drivers if hasattr(drivers, "connect") else None
    except Exception:
        return None


def connect(project, cid: str, spec: dict):
    """A DB-API connection to a target, with autocommit off where the driver allows it.

    A thin wrapper over drivers.connect(..., role="target") when the registry is installed;
    a missing driver is a LoadError naming pip install etlplus[extra]."""
    kind = spec.get("kind")
    drv = _drivers()
    if drv is not None and kind not in ("sqlite", "duckdb"):
        if kind in DRIVERS:
            _require(kind)  # the v1 message for a missing v1 driver
        full = dict(spec)
        if "_password" not in full:
            # the saved connection's secrets, used only while the endpoint is the saved one
            try:
                from etlplus import connections
                res = connections.spec_for(project, cid, {k: v for k, v in spec.items() if not str(k).startswith("_")})
                full.update({k: res.get(k) for k in ("_password", "_secrets", "_id")})
            except Exception:
                pass
        try:
            return drv.connect(project, cid, role="target", spec=full)
        except ImportError as e:
            raise LoadError(str(e) if "pip install" in str(e) else
                            f"The {kind} driver is not installed. Install it with: pip install etlplus[{kind}].") from None
        except Exception as e:
            if "pip install" in str(e):
                raise LoadError(str(e)) from None
            raise
    opts = dict(spec.get("options") or {})
    pw = _password(project, cid, spec)
    if kind == "sqlite":
        import sqlite3
        path = _file_db(project, spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(path), isolation_level=None, timeout=60)
    if kind == "duckdb":
        path = _file_db(project, spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(path))
    if kind == "postgres":
        psycopg = _require("postgres")
        return psycopg.connect(host=spec.get("host"), port=spec.get("port") or 5432, dbname=spec.get("database"),
                               user=spec.get("user"), password=pw, autocommit=False, **opts)
    if kind == "sqlserver":
        pyodbc = _require("sqlserver")
        driver = opts.pop("driver", "ODBC Driver 18 for SQL Server")
        server = spec.get("host") or ""
        if spec.get("port"):
            server += f",{spec['port']}"
        parts = {"DRIVER": "{" + driver + "}", "SERVER": server, "DATABASE": spec.get("database") or ""}
        if spec.get("user"):
            parts["UID"] = spec["user"]
            parts["PWD"] = "{" + (pw or "").replace("}", "}}") + "}"
        else:
            parts["Trusted_Connection"] = "yes"
        parts["TrustServerCertificate"] = "yes" if opts.pop("trust_server_certificate", True) else "no"
        for k, v in opts.items():
            parts[k] = v
        return pyodbc.connect(";".join(f"{k}={v}" for k, v in parts.items()), autocommit=False)
    if kind == "mysql":
        pymysql = _require("mysql")
        return pymysql.connect(host=spec.get("host"), port=int(spec.get("port") or 3306), user=spec.get("user"),
                               password=pw or "", database=spec.get("database"), autocommit=False, **opts)
    if kind == "oracle":
        oracledb = _require("oracle")
        dsn = opts.pop("dsn", None) or f"{spec.get('host')}:{spec.get('port') or 1521}/{spec.get('database')}"
        con = oracledb.connect(user=spec.get("user"), password=pw, dsn=dsn, **opts)
        con.autocommit = False
        return con
    raise LoadError(f"'{kind}' is not a database target kind.")


def staged_file(project, manifest: dict) -> Path:
    from etlplus import stage
    kind = manifest.get("kind", "extract")
    p = Path(stage.path(project, manifest["job"], manifest["run_id"], "extract"))
    run_dir = p.parent if p.suffix else p
    f = run_dir / manifest.get("file", f"{kind}.parquet")
    if f.name.endswith(".partial"):
        raise LoadError("A load never reads a .partial file.")
    if not f.exists():
        raise LoadError(f"The staged file {f.name} for run {manifest['run_id']} is missing.")
    return f


def _schema(project, manifest) -> pa.Schema:
    return pq.read_schema(staged_file(project, manifest))


def _batches(path: Path):
    pf = pq.ParquetFile(str(path))
    yield from pf.iter_batches(batch_size=BATCH_ROWS)


def _rows(batch: pa.RecordBatch, adapt):
    cols = [c.to_pylist() for c in batch.columns]
    return [tuple(adapt(v) for v in row) for row in zip(*cols)]


def _window(manifest):
    w = manifest.get("window")
    if not w or len(w) != 2 or w[0] is None or w[1] is None:
        return None
    return tuple(_parse_dt(x) for x in w)


def _parse_dt(v):
    if isinstance(v, (date, datetime)):
        return v
    s = str(v)
    if len(s) == 10:
        return date.fromisoformat(s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _slice_column(job) -> str | None:
    t = job.get("target") or {}
    inc = (job.get("source") or {}).get("incremental") or {}
    return t.get("slice_column") or inc.get("column")


def _keys(job) -> list[str]:
    k = (job.get("target") or {}).get("key") or []
    return [k] if isinstance(k, str) else list(k)


def _duplicate_keys(path: Path, key: list[str]) -> int:
    cols = ", ".join('"' + k.replace('"', '""') + '"' for k in key)
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT count(*) FROM (SELECT {cols} FROM read_parquet(?) GROUP BY {cols} HAVING count(*) > 1)",
            [str(path)]).fetchone()[0]
    finally:
        con.close()


def _null_keys(path: Path, key: list[str]) -> dict[str, int]:
    """Rows with NULL in each key column. NULL never equals NULL, so such a row cannot be upserted."""
    con = duckdb.connect()
    try:
        out = {}
        for k in key:
            c = '"' + k.replace('"', '""') + '"'
            n = con.execute(f"SELECT count(*) FROM read_parquet(?) WHERE {c} IS NULL", [str(path)]).fetchone()[0]
            if n:
                out[k] = int(n)
        return out
    finally:
        con.close()


def _check_upsert_keys(path: Path, key: list[str]) -> None:
    nulls = _null_keys(path, key)
    if nulls:
        what = ", ".join(f"{n} in '{k}'" for k, n in nulls.items())
        raise LoadError(f"The staged file has rows with an empty (NULL) key: {what}. An upsert matches rows "
                        "on the key, and an empty key never matches, so each re-run would add them again. "
                        "Filter them out or fill the key in the transform.")
    dups = _duplicate_keys(path, key)
    if dups:
        raise LoadError(f"The staged file has {dups} key value(s) that appear more than once. "
                        "Add a dedupe step on the key so each key is loaded once.")


def _staged_range(path: Path, column: str):
    con = duckdb.connect()
    try:
        c = '"' + column.replace('"', '""') + '"'
        return con.execute(f"SELECT min({c}), max({c}) FROM read_parquet(?)", [str(path)]).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------- database targets

def _exists(d: Dialect, cur, table: str) -> bool:
    parts = _split(table)
    name, schema = parts[-1], (parts[-2] if len(parts) > 1 else None)
    if d.name == "sqlite":
        cur.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,))
    elif d.name == "duckdb":
        if schema:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ? AND table_schema = ?",
                        [name, schema])
        else:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ? "
                        "AND table_schema = current_schema()", [name])
    elif d.name == "postgres":
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (d.table(table),))
        return bool(cur.fetchone()[0])
    elif d.name == "sqlserver":
        cur.execute("SELECT OBJECT_ID(?, 'U')", d.table(table))
        return cur.fetchone()[0] is not None
    elif d.name == "mysql":
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = COALESCE(%s, DATABASE()) "
                    "AND table_name = %s", (schema, name))
    elif d.name == "oracle":
        cur.execute("SELECT 1 FROM all_tables WHERE owner = COALESCE(:1, USER) AND table_name = :2",
                    [schema.upper() if schema else None, name.upper()])
    else:
        from etlplus import ddl
        sql, params = ddl.exists_sql(getattr(d, "kind", d.name), table)
        cur.execute(sql, params)
    return cur.fetchone() is not None


def _columns(d: Dialect, cur, table: str) -> list[str]:
    cur.execute(f"SELECT * FROM {d.table(table)} WHERE 1 = 0")
    names = [c[0] for c in cur.description]
    cur.fetchall()
    return names


def _match_columns(d: Dialect, staged: list[str], target: list[str], table: str, mapped: bool = False) -> list[str]:
    """Target names for the staged columns. A mapped stage already carries the destination's
    names, so only those columns are inserted and database defaults fill the rest."""
    by_lower = {t.lower(): t for t in target}
    if mapped:
        missing = [c for c in staged if c not in target and c.lower() not in by_lower]
        if missing:
            raise LoadError(f"Table {table} has no column {', '.join(repr(m) for m in missing)}, "
                            "which the mapping fills. Check the destination's columns.")
        return [c if c in target else by_lower[c.lower()] for c in staged]
    out, missing = [], []
    for c in staged:
        if c in target:
            out.append(c)
        elif c.lower() in by_lower:
            out.append(by_lower[c.lower()])
        else:
            missing.append(c)
    if missing:
        raise LoadError(f"Table {table} has no column {', '.join(repr(m) for m in missing)}. "
                        f"Add the column, or drop or rename it in the transform.")
    return out


def _window_params(d: Dialect, lo, hi):
    """Text for every dialect but Oracle, so a date or text slice column compares the same way."""
    if d.name == "oracle":
        return [lo, hi]
    return [str(lo), str(hi)]


def _exec(cur, sql, params=None):
    if params is None:
        cur.execute(sql)
    else:
        cur.execute(sql, params)


def _insert_rows(d: Dialect, con, cur, table_sql_name: str, cols: list[str], path: Path, log) -> int:
    """Insert every staged row into an already quoted table name. Returns rows inserted."""
    total = 0
    names = ", ".join(d.quote(c) for c in cols)
    if d.name == "postgres":
        with cur.copy(f"COPY {table_sql_name} ({names}) FROM STDIN") as cp:
            for batch in _batches(path):
                _check(log)
                for row in _rows(batch, d.adapt):
                    cp.write_row(row)
                total += batch.num_rows
                _log(log, f"  {total} rows sent")
        return total
    sql = f"INSERT INTO {table_sql_name} ({names}) VALUES ({d.phs(len(cols))})"
    if d.name == "sqlserver":
        cur.fast_executemany = True
    for batch in _batches(path):
        _check(log)
        rows = _rows(batch, d.adapt)
        if rows:
            cur.executemany(sql, rows)
        total += batch.num_rows
        _log(log, f"  {total} rows sent")
    return total


def _bulk_csv(path: Path, local: Path, staged_cols: list[str]):
    """Write the staged file as a UTF-8 CSV with LF line ends for BULK INSERT (partial then rename)."""
    local.parent.mkdir(parents=True, exist_ok=True)
    partial = local.with_name(local.name + ".partial")
    pf = pq.ParquetFile(str(path))
    try:
        with open(partial, "w", encoding="utf-8", newline="") as f:
            f.write(csv_line(staged_cols))
            for batch in pf.iter_batches(batch_size=BATCH_ROWS):
                for row in _rows(batch, _csv_value):
                    f.write(csv_line(row))
        os.replace(partial, local)
    finally:
        _unlink(partial)


def csv_line(values) -> str:
    """One CSV line for BULK INSERT (FORMAT = 'CSV', KEEPNULLS): NULL is an empty field and an
    empty string is a quoted "" field, so the two stay apart."""
    out = []
    for v in values:
        if v is None:
            out.append("")
            continue
        s = str(v)
        if s == "" or any(ch in s for ch in ',"\r\n'):
            s = '"' + s.replace('"', '""') + '"'
        out.append(s)
    return ",".join(out) + "\n"


def _csv_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v


def bulk_paths(job, table: str) -> tuple[Path, str, Path, str]:
    """(local csv, remote csv, local err, remote err) for a SQL Server BULK INSERT."""
    bulk = (job.get("target") or {}).get("bulk") or {}
    local_dir, remote_dir = bulk.get("local_dir"), bulk.get("remote_dir")
    if not local_dir or not remote_dir:
        raise LoadError("bulk needs both local_dir (where etl+ writes) and remote_dir (the same folder as SQL Server sees it).")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", ".".join(_split(table)))
    sep = "\\" if "\\" in remote_dir else "/"
    remote_dir = remote_dir.rstrip("\\/")
    return (Path(local_dir) / f"{base}.csv", f"{remote_dir}{sep}{base}.csv",
            Path(local_dir) / f"{base}.err", f"{remote_dir}{sep}{base}.err")


def _load_db(project, job, manifest, log, cid, spec) -> dict:
    kind = spec["kind"]
    d = dialect(kind)
    t = job["target"]
    table, mode = t["table"], t.get("mode", "replace")
    path = staged_file(project, manifest)
    schema = pq.read_schema(str(path))
    staged_cols = schema.names
    key = _keys(job)
    slice_col = _slice_column(job) if mode == "append" else None
    win = _window(manifest) if mode == "append" else None
    use_bulk = kind == "sqlserver" and bool(t.get("bulk"))

    if mode == "upsert":
        if not key:
            raise LoadError("upsert needs key columns (target.key).")
        missing = [k for k in key if k not in staged_cols]
        if missing:
            raise LoadError(f"The key column {', '.join(missing)} is not in the staged file.")
        _check_upsert_keys(path, key)

    rng = None
    if mode == "append" and slice_col and win is None and slice_col in staged_cols:
        rng = _staged_range(path, slice_col)
        if rng[0] is None:
            rng = None

    con = connect(project, cid, spec)
    created = False
    deleted = 0
    started = _time.time()
    try:
        cur = con if kind == "duckdb" else con.cursor()
        if kind in ("sqlite", "duckdb"):
            cur.execute("BEGIN TRANSACTION" if kind == "duckdb" else "BEGIN")
        exists = _exists(d, cur, table)
        if not exists:
            if not t.get("create"):
                raise LoadError(f"Table {table} does not exist on '{cid}'. Set create: true on the target, "
                                "or create it first (the DDL is on the job page).")
            sql = d.create_sql(table, schema)
            _log(log, f"load: creating {table}\n{sql}")
            cur.execute(sql)
            created = True
        target_cols = _columns(d, cur, table)
        cols = _match_columns(d, staged_cols, target_cols, table, mapped=bool(manifest.get("mapped")))
        tkey = [c for c, s in zip(cols, staged_cols) if s in key]
        tq = d.table(table)

        if mode == "replace":
            _log(log, f"load: {d.clear_sql(table)}")
            cur.execute(d.clear_sql(table))
            deleted = 0 if d.truncate else _affected(kind, cur)
        elif mode == "append":
            if slice_col:
                tcol = {c.lower(): c for c in target_cols}.get(slice_col.lower())
                if not tcol:
                    raise LoadError(f"{table} has no column '{slice_col}', so append cannot slice it. "
                                    "Set the job to replace, or add the column to the table.")
                if win:
                    sql, params = d.delete_slice_sql(table, tcol), _window_params(d, *win)
                    _log(log, f"load: delete the slice {win[0]} .. {win[1]} on {tcol}")
                elif rng:
                    sql, params = d.delete_range_sql(table, tcol), _window_params(d, *rng)
                    _log(log, f"load: delete the staged range {rng[0]} .. {rng[1]} on {tcol}")
                else:
                    sql = None
                if sql:
                    _exec(cur, sql, params)
                    deleted = _affected(kind, cur)
                    _log(log, f"load: deleted {deleted} existing rows in the slice")
            else:
                _log(log, "load: no slice column, rows are appended as they are")

        if kind == "duckdb":
            names = ", ".join(d.quote(c) for c in cols)
            src = ", ".join(d.quote(c) for c in staged_cols)
            if mode == "upsert":
                cur.execute(f"CREATE OR REPLACE TEMP TABLE etlplus_stage AS SELECT {src} FROM read_parquet(?)",
                            [str(path)])
                on = " AND ".join(f"{tq}.{d.quote(tc)} = s.{d.quote(sc)}"
                                  for sc, tc in zip(staged_cols, cols) if sc in key)
                cur.execute(f"DELETE FROM {tq} WHERE EXISTS (SELECT 1 FROM etlplus_stage s WHERE {on})")
                deleted = _affected(kind, cur)
                cur.execute(f"INSERT INTO {tq} ({names}) SELECT {src} FROM etlplus_stage")
                cur.execute("DROP TABLE etlplus_stage")
            else:
                _check(log)
                cur.execute(f"INSERT INTO {tq} ({names}) SELECT {src} FROM read_parquet(?)", [str(path)])
            rows = manifest.get("rows")
            if rows is None:
                rows = pq.ParquetFile(str(path)).metadata.num_rows
        elif use_bulk:
            local, remote, local_err, remote_err = bulk_paths(job, table)
            for stale in (local_err, local_err.with_name(local_err.name + ".Error.Txt")):
                if stale.exists():
                    stale.unlink()
            _bulk_csv(path, local, staged_cols)
            stage_t = d.quote(d.stage_name())
            cur.execute(d.drop_stage_sql())
            cur.execute(d.create_stage_sql(table, cols))
            _log(log, f"load: BULK INSERT from {remote}")
            _check(log)
            try:
                cur.execute(d.bulk_insert_sql(stage_t, remote, remote_err))
            except Exception as e:
                detail = ""
                errtxt = local_err.with_name(local_err.name + ".Error.Txt")
                if errtxt.exists():
                    detail = "\nRejected rows:\n" + "".join(errtxt.read_text(errors="replace").splitlines(True)[:20])
                raise LoadError(f"BULK INSERT failed: {e}. SQL Server must be able to read {remote} "
                                f"(its service account, not only your login), and the login needs bulkadmin.{detail}") from e
            rows = pq.ParquetFile(str(path)).metadata.num_rows
            _apply_stage(d, cur, table, cols, mode, tkey, log)
            cur.execute(d.drop_stage_sql())
        elif mode == "upsert":
            if kind == "oracle":
                sql = d.upsert_sql(table, cols, tkey)[0]
                rows = 0
                for batch in _batches(path):
                    _check(log)
                    cur.executemany(sql, _rows(batch, d.adapt))
                    rows += batch.num_rows
            else:
                cur.execute(d.drop_stage_sql())
                cur.execute(d.create_stage_sql(table, cols))
                rows = _insert_rows(d, con, cur, d.quote(d.stage_name()), cols, path, log)
                stmts = d.upsert_sql(table, cols, tkey)
                for s in stmts:
                    _log(log, f"load: {s}")
                    cur.execute(s)
                    if s.lstrip().upper().startswith("DELETE"):
                        deleted = _affected(kind, cur)
                if kind != "postgres":
                    cur.execute(d.drop_stage_sql())
        else:
            rows = _insert_rows(d, con, cur, tq, cols, path, log)

        _check(log)
        if kind in ("sqlite", "duckdb"):
            cur.execute("COMMIT")
        else:
            con.commit()
    except BaseException:
        try:
            if kind in ("sqlite", "duckdb"):
                cur.execute("ROLLBACK")
            else:
                con.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass
    _log(log, f"load: {rows} rows into {table} ({mode}) in {_time.time() - started:.1f}s")
    return {"rows_loaded": int(rows), "mode": mode, "target": f"{cid}:{table}", "deleted": int(deleted),
            "created": created}


def _apply_stage(d, cur, table, cols, mode, tkey, log) -> None:
    """Move rows from the stage table into the target (used after BULK INSERT)."""
    names = ", ".join(d.quote(c) for c in cols)
    s = d.quote(d.stage_name())
    if mode == "upsert":
        for sql in d.upsert_sql(table, cols, tkey):
            _log(log, f"load: {sql}")
            cur.execute(sql)
    else:
        cur.execute(f"INSERT INTO {d.table(table)} ({names}) SELECT {names} FROM {s}")


def _affected(kind, cur) -> int:
    if kind == "duckdb":
        r = cur.fetchone()
        return int(r[0]) if r else 0
    return max(cur.rowcount or 0, 0)


# ---------------------------------------------------------------- folder targets

def _folder_target(project, job, spec) -> tuple[Path, str, str]:
    t = job["target"]
    root = Path(project.resolve(spec.get("path") or "."))
    name = str(t.get("table") or "").strip()
    if not name:
        raise LoadError("The target needs a file name (target.table).")
    fmt = (t.get("format") or "").lower()
    stem = name
    for ext in FILE_FORMATS:
        if name.lower().endswith("." + ext):
            fmt = fmt or ext
            stem = name[: -len(ext) - 1]
    fmt = fmt or "parquet"
    if fmt not in FILE_FORMATS:
        raise LoadError(f"A folder target writes {', '.join(FILE_FORMATS)}.")
    base = (root / stem).resolve()
    if root.resolve() not in base.parents:
        raise LoadError("The target file name must stay inside the connection folder.")
    return base, fmt, stem


def _copy_opts(fmt):
    if fmt == "jsonl":
        return "(FORMAT json)"
    return "(FORMAT parquet, COMPRESSION zstd)" if fmt == "parquet" else "(FORMAT csv, HEADER true)"


def _reader(fmt, path_or_glob, text=False) -> str:
    """A DuckDB reader. text=True reads a CSV exactly as written: every column as text and a quoted
    empty field as an empty string, so rewriting the file changes nothing it did not mean to."""
    p = str(path_or_glob).replace("'", "''")
    if fmt == "parquet":
        return f"read_parquet('{p}', union_by_name = true)"
    if fmt == "jsonl":
        return f"read_json_auto('{p}', format = 'newline_delimited', union_by_name = true)"
    if text:
        return f"read_csv('{p}', header = true, union_by_name = true, all_varchar = true, allow_quoted_nulls = false)"
    return f"read_csv_auto('{p}', header = true, union_by_name = true)"


def _lit(p) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def _safe(s) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "", str(s).replace(" ", "T").replace(":", ""))


def _unlink(*paths) -> None:
    for p in paths:
        try:
            if p is not None and Path(p).exists():
                Path(p).unlink()
        except OSError:
            pass


def _load_folder(project, job, manifest, log, cid, spec) -> dict:
    """Write files. Every file is written as .partial first; nothing is published until every
    file of the load is written, and a .partial is never left behind."""
    t = job["target"]
    mode = t.get("mode", "replace")
    base, fmt, stem = _folder_target(project, job, spec)
    src = staged_file(project, manifest)
    rows = pq.ParquetFile(str(src)).metadata.num_rows
    deleted = 0
    partials = []
    con = duckdb.connect()
    try:
        if mode == "upsert":
            key = _keys(job)
            if not key:
                raise LoadError("upsert needs key columns (target.key).")
            _check_upsert_keys(src, key)
        single = base.with_name(base.name + f".{fmt}")
        if mode == "replace" or (mode == "upsert" and not single.exists()):
            dest = single
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_name(dest.name + ".partial")
            partials.append(partial)
            con.execute(f"COPY (SELECT * FROM read_parquet({_lit(src)})) TO {_lit(partial)} {_copy_opts(fmt)}")
            con.close()
            _check(log)
            os.replace(partial, dest)
            _log(log, f"load: wrote {rows} rows to {dest.name}")
            target = dest
        elif mode == "upsert":
            dest = single
            partial = dest.with_name(dest.name + ".partial")
            partials.append(partial)
            old = _reader(fmt, dest, text=True)
            new = f"read_parquet({_lit(src)})"
            if fmt == "csv":
                # the old file is read as text, so keys compare as text on both sides
                on = " AND ".join(f"CAST(n.{_q(k)} AS VARCHAR) = o.{_q(k)}" for k in key)
            else:
                on = " AND ".join(f"n.{_q(k)} = o.{_q(k)}" for k in key)
            deleted = con.execute(f"SELECT count(*) FROM {old} o WHERE EXISTS (SELECT 1 FROM {new} n WHERE {on})").fetchone()[0]
            con.execute(
                f"COPY (SELECT * FROM {old} o WHERE NOT EXISTS (SELECT 1 FROM {new} n WHERE {on}) "
                f"UNION ALL BY NAME SELECT * FROM {new}) TO {_lit(partial)} {_copy_opts(fmt)}")
            con.close()
            _check(log)
            os.replace(partial, dest)
            _log(log, f"load: upserted {rows} rows into {dest.name} ({deleted} replaced)")
            target = dest
        else:
            folder = base
            folder.mkdir(parents=True, exist_ok=True)
            win = manifest.get("window")
            part_id = f"{_safe(win[0])}_{_safe(win[1])}" if win and win[0] is not None else _safe(manifest["run_id"])
            dest = folder / f"part-{part_id}.{fmt}"
            if dest.exists():
                deleted += con.execute(f"SELECT count(*) FROM {_reader(fmt, dest)}").fetchone()[0]
            partial = dest.with_name(dest.name + ".partial")
            partials.append(partial)
            con.execute(f"COPY (SELECT * FROM read_parquet({_lit(src)})) TO {_lit(partial)} {_copy_opts(fmt)}")
            _check(log)
            # older parts that hold rows of this window, rewritten without them (not yet published)
            rewrites = []
            slice_col = _slice_column(job)
            w = _window(manifest)
            if slice_col and w:
                c = _q(slice_col)
                for other in sorted(folder.glob(f"part-*.{fmt}")):
                    if other == dest:
                        continue
                    r = _reader(fmt, other, text=True)
                    cols = [x[0] for x in con.execute(f"DESCRIBE SELECT * FROM {r}").fetchall()]
                    if slice_col not in cols:
                        continue
                    cond = f"{c} >= {_dt_lit(w[0])} AND {c} < {_dt_lit(w[1])}"
                    n = con.execute(f"SELECT count(*) FROM {r} WHERE {cond}").fetchone()[0]
                    if not n:
                        continue
                    tmp = other.with_name(other.name + ".partial")
                    partials.append(tmp)
                    con.execute(f"COPY (SELECT * FROM {r} WHERE NOT ({cond}) OR {c} IS NULL) "
                                f"TO {_lit(tmp)} {_copy_opts(fmt)}")
                    rewrites.append((tmp, other, n))
                    _check(log)
            con.close()
            # publish: the trimmed older parts first, the new part last. A failure part way leaves
            # rows of the window missing until the re-run, never the same rows twice.
            for tmp, other, n in rewrites:
                os.replace(tmp, other)
                deleted += n
                _log(log, f"load: removed {n} rows of the window from {other.name}")
            os.replace(partial, dest)
            _log(log, f"load: wrote {rows} rows to {folder.name}/{dest.name}")
            target = dest
    finally:
        try:
            con.close()
        except Exception:
            pass
        _unlink(*partials)
    return {"rows_loaded": int(rows), "mode": mode, "target": f"{cid}:{target.name}", "deleted": int(deleted),
            "created": False}


def _dt_lit(v) -> str:
    """A text literal: DuckDB casts it to a DATE or TIMESTAMP column, and compares it as text otherwise."""
    return _lit(str(v))


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


# ---------------------------------------------------------------- public

def resolve_job(project, job: dict, run_id: str = "") -> dict:
    """The job with a destination target turned into the v1 target form (SPEC_V2 7.2)."""
    t = dict(job.get("target") or {})
    did = t.get("destination")
    if not did:
        return job
    from etlplus import destinations
    try:
        base = destinations.resolve(project, did, job_name=job.get("name") or "", run_id=run_id)
    except KeyError as e:
        raise LoadError(str(e).strip("'\"")) from None
    for k, v in t.items():
        if k in ("destination",) or v in (None, "", []):
            continue
        base[k] = v
    out = dict(job)
    out["target"] = base
    return out


def _load_remote(project, job, manifest, log, cid, spec) -> dict:
    """A remote location target: write the file locally (.partial, rename) in the run's stage
    folder, then remote.upload it (.partial then rename on the server, or PUT then .done)."""
    t = job["target"]
    if t.get("mode", "replace") == "upsert":
        raise LoadError("A remote location takes replace or append, not upsert.")
    src = staged_file(project, manifest)
    out_dir = src.parent / "outgoing"
    local_spec = {"kind": "folder", "path": str(out_dir), "role": "target"}
    res = _load_folder(project, job, manifest, log, cid, local_spec)
    name = res["target"].split(":", 1)[1]
    produced = [p for p in out_dir.rglob("*") if p.is_file() and p.name == name and not p.name.endswith(".partial")]
    if not produced:
        raise LoadError("The file to upload was not written.")
    local = produced[0]
    rel = local.relative_to(out_dir).as_posix()
    from etlplus import remote
    remote.upload(project, cid, local, rel, log=log)
    _log(log, f"load: uploaded {rel} to '{cid}'")
    res["target"] = f"{cid}:{rel}"
    return res


def load(project, job: dict, manifest: dict, log) -> dict:
    job = resolve_job(project, job, manifest.get("run_id") or "")
    t = job.get("target") or {}
    mode = t.get("mode", "replace")
    if mode not in MODES:
        raise LoadError(f"'{mode}' is not a load mode. Use replace, append or upsert.")
    if not t.get("table"):
        raise LoadError("The target needs a table (or a file name for a folder).")
    cid, spec = _conn_spec(project, job)
    kind = spec.get("kind")
    if kind in ("folder", "share"):
        return _load_folder(project, job, manifest, log, cid, spec)
    if kind in REMOTE_KINDS:
        return _load_remote(project, job, manifest, log, cid, spec)
    if kind not in DB_KINDS and kind not in _db_kinds() and kind not in ALIASES:
        raise LoadError(f"'{kind}' is not a target kind.")
    with file_lock(project, spec, log):
        return _load_db(project, job, manifest, log, cid, spec)


def ddl(project, job: dict, manifest: dict) -> str:
    job = resolve_job(project, job)
    t = job.get("target") or {}
    cid = t.get("connection")
    spec = project.connections.get(cid) or {}
    kind = spec.get("kind")
    schema = _schema(project, manifest)
    if kind == "folder":
        cols = ",\n    ".join(f"{_q(f.name)} {DuckDB().type_for(f.type)}" for f in schema)
        return f"-- folder target: files are written as they are; their columns:\n-- (\n--    " + \
            cols.replace("\n", "\n--") + "\n-- )"
    return dialect(kind).create_sql(t.get("table") or "new_table", schema) + ";"


def status(project, job: dict) -> dict:
    job = resolve_job(project, job)
    t = job.get("target") or {}
    cid = t.get("connection")
    spec = project.connections.get(cid)
    if not spec:
        raise LoadError(f"The target connection '{cid}' does not exist.")
    kind = spec.get("kind")
    if kind in REMOTE_KINDS:
        return {"exists": None, "rows": None, "columns": []}
    if kind in ("folder", "share"):
        base, fmt, _ = _folder_target(project, job, spec)
        single = base.with_name(base.name + f".{fmt}")
        paths = [p for p in (single,) if p.exists()]
        if base.is_dir() and list(base.glob(f"part-*.{fmt}")):
            paths.append(base / f"part-*.{fmt}")
        if not paths:
            return {"exists": False, "rows": None, "columns": []}
        con = duckdb.connect()
        try:
            r = _reader(fmt, paths[0]) if len(paths) == 1 else \
                f"(SELECT * FROM {_reader(fmt, paths[0])} UNION ALL BY NAME SELECT * FROM {_reader(fmt, paths[1])})"
            n = con.execute(f"SELECT count(*) FROM {r}").fetchone()[0]
            cols = [{"name": x[0], "type": x[1]} for x in con.execute(f"DESCRIBE SELECT * FROM {r}").fetchall()]
        finally:
            con.close()
        return {"exists": True, "rows": int(n), "columns": cols}
    d = dialect(kind)
    table = t["table"]
    con = connect(project, cid, spec)
    try:
        cur = con.cursor()
        if not _exists(d, cur, table):
            return {"exists": False, "rows": None, "columns": []}
        cur.execute(f"SELECT COUNT(*) FROM {d.table(table)}")
        n = cur.fetchone()[0]
        cur.execute(f"SELECT * FROM {d.table(table)} WHERE 1 = 0")
        cols = [{"name": c[0], "type": _type_name(c[1])} for c in cur.description]
        cur.fetchall()
        return {"exists": True, "rows": int(n), "columns": cols}
    finally:
        con.close()


def _type_name(code) -> str | None:
    if code is None:
        return None
    if isinstance(code, type):
        return code.__name__
    return str(code)
