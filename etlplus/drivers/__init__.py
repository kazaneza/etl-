"""The driver registry: every database, document store, folder and remote location etl+ knows.

One Driver per connection kind says how to reach it (fields, pip package), how it is made
read-only as a source, how names are quoted and folded, how values are bound, how a preview is
limited and which native type each generic column type becomes. `connect` is the one path for
reads and writes: role "source" always gives a read-only session where the server can do it.

Driver modules (psycopg, pyodbc, ...) are imported only through `connections.driver(kind)`, at
call time, and never cached here. `installed` uses importlib.util.find_spec and imports nothing.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import re
from dataclasses import asdict, dataclass, field

STATUS = ("real", "simulated", "untested")
# real      = tested here against a real server (testdb profile, or in-process for sqlite/duckdb)
# simulated = the full read and load path tested through a fake DB-API module that records SQL
# untested  = the SQL is generated and unit-tested, but has never run against anything

GENERIC_TYPES = ("string", "text", "integer", "bigint", "decimal", "double", "boolean",
                 "date", "time", "timestamp", "timestamptz", "binary", "json")
# string takes a length (string(n)); decimal takes precision and scale (decimal(p,s))

DEFAULT_STRING_LENGTH = 255
DEFAULT_DECIMAL = (18, 3)


class DriverError(RuntimeError):
    """A plain message for the person: what is wrong and what to do."""


class UnknownKind(DriverError, ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"         # text | int | password | path | select | bool | textarea
    required: bool = False
    default: object = None
    choices: tuple = ()        # for select
    secret: bool = False       # stored only in etlplus.secrets
    endpoint: bool = False     # a change clears stored secrets (connections.ENDPOINT_KEYS)
    help: str = ""             # one short line


@dataclass(frozen=True)
class Driver:
    kind: str
    label: str
    family: str
    category: str              # database | document | folder | remote
    module: str
    pip: str
    extra: str | None
    fields: tuple
    default_port: int | None
    roles: frozenset
    status: str
    quote: tuple
    fold: str | None
    paramstyle: str            # format (%s) | qmark (?) | numeric (:1) | pyformat (%(p0)s)
    limit_style: str           # limit | top | fetch_first | first | sample | none
    type_map: dict = field(default_factory=dict, hash=False, compare=False)
    upsert: str | None = None
    bulk: str = ""
    read_only: str = ""
    group: str = "Databases"
    notes: str = ""


# ---------------------------------------------------------------- fields

HOST = Field("host", "Host", required=True, endpoint=True)
PORT = Field("port", "Port", type="int", endpoint=True)
DATABASE = Field("database", "Database", endpoint=True)
USER = Field("user", "User", endpoint=True)
PASSWORD = Field("password", "Password", type="password", secret=True)
SSLMODE = Field("sslmode", "SSL mode", type="select", default="prefer",
                choices=("disable", "allow", "prefer", "require", "verify-ca", "verify-full"))
PATH_DB = Field("path", "File", type="path", required=True, endpoint=True,
                help="A path inside the project, such as data/core.db.")


def _net(*extra, user_required=True, db_required=False):
    return (HOST, PORT, Field("database", "Database", required=db_required, endpoint=True),
            Field("user", "User", required=user_required, endpoint=True), PASSWORD) + tuple(extra)


# ---------------------------------------------------------------- type maps (section 3.3)
# a value is a template, or (template, max_n_or_p, overflow) for string and decimal:
# string: n above max -> overflow; decimal: p above max is cut to max (ddl warns).

_PG = {"string": "varchar({n})", "text": "text", "integer": "integer", "bigint": "bigint",
       "decimal": ("numeric({p},{s})", 1000), "double": "double precision", "boolean": "boolean",
       "date": "date", "time": "time", "timestamp": "timestamp", "timestamptz": "timestamptz",
       "binary": "bytea", "json": "jsonb"}
_REDSHIFT = dict(_PG, binary="varbyte", json="super")
_MSSQL = {"string": ("nvarchar({n})", 4000, "nvarchar(max)"), "text": "nvarchar(max)", "integer": "int",
          "bigint": "bigint", "decimal": ("decimal({p},{s})", 38), "double": "float", "boolean": "bit",
          "date": "date", "time": "time(6)", "timestamp": "datetime2(6)",
          "timestamptz": "datetimeoffset(6)", "binary": "varbinary(max)", "json": "nvarchar(max)"}
_MYSQL = {"string": ("varchar({n})", 16383, "longtext"), "text": "longtext", "integer": "int",
          "bigint": "bigint", "decimal": ("decimal({p},{s})", 65), "double": "double",
          "boolean": "tinyint(1)", "date": "date", "time": "time(6)", "timestamp": "datetime(6)",
          "timestamptz": "datetime(6)", "binary": "longblob", "json": "json"}
_ORACLE = {"string": ("varchar2({n} char)", 4000, "clob"), "text": "clob", "integer": "number(10)",
           "bigint": "number(19)", "decimal": ("number({p},{s})", 38), "double": "binary_double",
           "boolean": "number(1)", "date": "date", "time": "varchar2(15)", "timestamp": "timestamp(6)",
           "timestamptz": "timestamp(6) with time zone", "binary": "blob", "json": "clob"}
_DB2 = {"string": "varchar({n})", "text": "clob", "integer": "integer", "bigint": "bigint",
        "decimal": ("decimal({p},{s})", 31), "double": "double", "boolean": "boolean", "date": "date",
        "time": "time", "timestamp": "timestamp(6)", "timestamptz": "timestamp(6)", "binary": "blob",
        "json": "clob"}
_SQLITE = {"string": "TEXT", "text": "TEXT", "integer": "INTEGER", "bigint": "INTEGER", "decimal": "NUMERIC",
           "double": "REAL", "boolean": "INTEGER", "date": "TEXT", "time": "TEXT", "timestamp": "TEXT",
           "timestamptz": "TEXT", "binary": "BLOB", "json": "TEXT"}
_DUCKDB = {"string": "VARCHAR", "text": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
           "decimal": ("DECIMAL({p},{s})", 38), "double": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE",
           "time": "TIME", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ", "binary": "BLOB",
           "json": "JSON"}
_CLICKHOUSE = {"string": "String", "text": "String", "integer": "Int32", "bigint": "Int64",
               "decimal": ("Decimal({p},{s})", 76), "double": "Float64", "boolean": "Bool", "date": "Date32",
               "time": "String", "timestamp": "DateTime64(6)", "timestamptz": "DateTime64(6,'UTC')",
               "binary": "String", "json": "String"}
_SNOWFLAKE = {"string": "VARCHAR({n})", "text": "VARCHAR", "integer": "NUMBER(10,0)", "bigint": "NUMBER(19,0)",
              "decimal": ("NUMBER({p},{s})", 38), "double": "FLOAT", "boolean": "BOOLEAN", "date": "DATE",
              "time": "TIME", "timestamp": "TIMESTAMP_NTZ", "timestamptz": "TIMESTAMP_TZ", "binary": "BINARY",
              "json": "VARIANT"}
_HANA = {"string": ("NVARCHAR({n})", 5000, "NCLOB"), "text": "NCLOB", "integer": "INTEGER", "bigint": "BIGINT",
         "decimal": ("DECIMAL({p},{s})", 38), "double": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE",
         "time": "TIME", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMP", "binary": "BLOB", "json": "NCLOB"}
_TERADATA = {"string": "VARCHAR({n}) CHARACTER SET UNICODE", "text": "CLOB", "integer": "INTEGER",
             "bigint": "BIGINT", "decimal": ("DECIMAL({p},{s})", 38), "double": "FLOAT", "boolean": "BYTEINT",
             "date": "DATE", "time": "TIME(6)", "timestamp": "TIMESTAMP(6)",
             "timestamptz": "TIMESTAMP(6) WITH TIME ZONE", "binary": "BLOB", "json": "JSON"}
_FIREBIRD = {"string": ("VARCHAR({n})", 8191, "BLOB SUB_TYPE TEXT"), "text": "BLOB SUB_TYPE TEXT",
             "integer": "INTEGER", "bigint": "BIGINT", "decimal": ("DECIMAL({p},{s})", 18),
             "double": "DOUBLE PRECISION", "boolean": "BOOLEAN", "date": "DATE", "time": "TIME",
             "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMP WITH TIME ZONE", "binary": "BLOB",
             "json": "BLOB SUB_TYPE TEXT"}
_ODBC = {"string": "VARCHAR({n})", "text": "VARCHAR(4000)", "integer": "INTEGER", "bigint": "BIGINT",
         "decimal": ("DECIMAL({p},{s})", 38), "double": "DOUBLE PRECISION", "boolean": "SMALLINT", "date": "DATE",
         "time": "TIME", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMP", "binary": "VARBINARY(8000)",
         "json": "VARCHAR(4000)"}

# generic types a kind stores with a loss (ddl.py adds "/* was: */" and a warning)
LOSSY = {
    "mysql": {"timestamptz": "Stored as datetime(6) in UTC; the zone is not kept."},
    "oracle": {"time": "Oracle has no time type; stored as text (varchar2(15))."},
    "db2": {"timestamptz": "Stored as timestamp(6) in UTC; the zone is not kept."},
    "sqlite": {"date": "Stored as ISO text.", "timestamp": "Stored as ISO text.",
               "timestamptz": "Stored as ISO text.", "time": "Stored as text."},
    "clickhouse": {"time": "ClickHouse has no time type; stored as String."},
    "hana": {"timestamptz": "Stored as TIMESTAMP in UTC; the zone is not kept."},
    "teradata": {"boolean": "Stored as BYTEINT (0 or 1)."},
    "odbc": {"timestamptz": "Stored as TIMESTAMP; the zone is not kept.",
             "boolean": "Stored as SMALLINT (0 or 1)."},
}

# longest identifier per family (section 3.4)
NAME_LIMITS = {"clickhouse": 255, "duckdb": 255, "sqlserver": 128, "snowflake": 255, "hana": 127,
               "firebird": 63}
DEFAULT_NAME_LIMIT = 128

_Q_DQ, _Q_BR, _Q_BT = ('"', '"'), ("[", "]"), ("`", "`")

# ---------------------------------------------------------------- the registry


def _d(kind, label, family, category, module, pip, extra, fields, port, roles, status, quote, fold,
       paramstyle, limit_style, type_map, upsert, bulk, read_only, group="Databases", notes=""):
    return Driver(kind=kind, label=label, family=family, category=category, module=module, pip=pip,
                  extra=extra, fields=tuple(fields), default_port=port, roles=frozenset(roles),
                  status=status, quote=quote, fold=fold, paramstyle=paramstyle, limit_style=limit_style,
                  type_map=dict(type_map), upsert=upsert, bulk=bulk, read_only=read_only, group=group,
                  notes=notes)


ST = ("source", "target")
S = ("source",)

_PG_RO = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY (the session's read_only flag)"
_SSL = (SSLMODE,)

_DRIVERS = [
    # postgres family
    _d("postgres", "PostgreSQL", "postgres", "database", "psycopg", "psycopg[binary]", "postgres",
       _net(*_SSL), 5432, ST, "real", _Q_DQ, "lower", "format", "limit", _PG,
       "on_conflict", "copy", _PG_RO + " + statement_timeout option",
       notes="Upsert uses ON CONFLICT when a unique key matches, else delete and insert."),
    _d("redshift", "Amazon Redshift", "postgres", "database", "psycopg", "psycopg[binary]", "postgres",
       _net(*_SSL), 5439, ST, "untested", _Q_DQ, "lower", "format", "limit", _REDSHIFT,
       "merge", "insert_batches", "START TRANSACTION READ ONLY per read",
       notes="Loads use multi-row INSERT batches; COPY from S3 is not in this version."),
    _d("greenplum", "Greenplum", "postgres", "database", "psycopg", "psycopg[binary]", "postgres",
       _net(*_SSL), 5432, ST, "untested", _Q_DQ, "lower", "format", "limit", _PG,
       "delete_insert", "copy", _PG_RO),
    _d("cockroachdb", "CockroachDB", "postgres", "database", "psycopg", "psycopg[binary]", "postgres",
       _net(*_SSL), 26257, ST, "untested", _Q_DQ, "lower", "format", "limit", _PG,
       "on_conflict", "copy", "SET default_transaction_read_only = on"),
    _d("timescaledb", "TimescaleDB", "postgres", "database", "psycopg", "psycopg[binary]", "postgres",
       _net(*_SSL), 5432, ST, "untested", _Q_DQ, "lower", "format", "limit", _PG,
       "on_conflict", "copy", _PG_RO),
    _d("yugabytedb", "YugabyteDB", "postgres", "database", "psycopg", "psycopg[binary]", "postgres",
       _net(*_SSL), 5433, ST, "untested", _Q_DQ, "lower", "format", "limit", _PG,
       "on_conflict", "copy", _PG_RO),
    # sql server family
    _d("sqlserver", "SQL Server", "sqlserver", "database", "pyodbc", "pyodbc", "sqlserver",
       _net(Field("driver", "ODBC driver", endpoint=True,
                  help="Leave empty to use the newest installed SQL Server driver."),
            Field("encrypt", "Encrypt", type="select", choices=("yes", "no", "strict")),
            Field("trust_server_certificate", "Trust the server certificate", type="bool", default=False),
            user_required=False),
       1433, ST, "simulated", _Q_BR, None, "qmark", "top", _MSSQL,
       "delete_insert", "fast_executemany", "ApplicationIntent=ReadOnly + the SQL guard",
       notes="BULK INSERT is used when the job sets bulk. Upsert is delete and insert in one transaction."),
    _d("azuresql", "Azure SQL Database", "sqlserver", "database", "pyodbc", "pyodbc", "sqlserver",
       _net(Field("driver", "ODBC driver", endpoint=True,
                  help="Leave empty to use the newest installed SQL Server driver."),
            Field("authentication", "Sign-in", type="select", default="sql",
                  choices=("sql", "ActiveDirectoryPassword"))),
       1433, ST, "simulated", _Q_BR, None, "qmark", "top", _MSSQL,
       "delete_insert", "fast_executemany", "ApplicationIntent=ReadOnly + the SQL guard",
       notes="Encrypt=yes is always on."),
    # mysql family
    _d("mysql", "MySQL", "mysql", "database", "pymysql", "pymysql", "mysql",
       _net(Field("ssl", "TLS", type="select", default="none", choices=("none", "required", "verify"))),
       3306, ST, "simulated", _Q_BT, None, "format", "limit", _MYSQL,
       "on_duplicate", "insert_batches", "SET SESSION TRANSACTION READ ONLY"),
    _d("mariadb", "MariaDB", "mysql", "database", "pymysql", "pymysql", "mysql",
       _net(Field("ssl", "TLS", type="select", default="none", choices=("none", "required", "verify"))),
       3306, ST, "real", _Q_BT, None, "format", "limit", _MYSQL,
       "on_duplicate", "insert_batches", "SET SESSION TRANSACTION READ ONLY"),
    # oracle, db2
    _d("oracle", "Oracle", "oracle", "database", "oracledb", "oracledb", "oracle",
       (HOST, PORT, Field("connect_by", "Connect by", type="select", default="service_name",
                          choices=("service_name", "sid"), endpoint=True),
        Field("database", "Service name or SID", required=True, endpoint=True),
        Field("user", "User", required=True, endpoint=True), PASSWORD),
       1521, ST, "simulated", _Q_DQ, "upper", "numeric", "fetch_first", _ORACLE,
       "merge", "executemany", "SET TRANSACTION READ ONLY", notes="Thin mode: no Oracle client needed."),
    _d("db2", "IBM Db2", "db2", "database", "ibm_db_dbi", "ibm_db", "db2",
       _net(Field("security", "TLS", type="select", default="none", choices=("none", "SSL")), db_required=True),
       50000, ST, "untested", _Q_DQ, "upper", "qmark", "fetch_first", _DB2,
       "merge", "executemany", "SQL guard only: use a read-only user"),
    # embedded
    _d("sqlite", "SQLite", "sqlite", "database", "sqlite3", "built in", None, (PATH_DB,), None, ST, "real",
       _Q_DQ, None, "qmark", "limit", _SQLITE, "on_conflict", "executemany",
       "Opened with mode=ro", notes="Upsert uses ON CONFLICT when a unique index exists, else delete and insert."),
    _d("duckdb", "DuckDB", "duckdb", "database", "duckdb", "duckdb", None, (PATH_DB,), None, ST, "real",
       _Q_DQ, None, "qmark", "limit", _DUCKDB, "delete_insert", "arrow_insert",
       "read_only=True + enable_external_access=false"),
    # analytics and others
    _d("clickhouse", "ClickHouse", "clickhouse", "database", "clickhouse_connect", "clickhouse-connect",
       "clickhouse",
       _net(Field("secure", "HTTPS", type="bool", default=False, help="Port 8443 with HTTPS, 8123 without.")),
       8123, ST, "simulated", _Q_BT, None, "pyformat", "limit", _CLICKHOUSE,
       None, "insert_arrow", "Setting readonly=1 on every query",
       notes="No upsert. Replace builds a new table and swaps it with EXCHANGE TABLES."),
    _d("snowflake", "Snowflake", "snowflake", "database", "snowflake.connector", "snowflake-connector-python",
       "snowflake",
       (Field("account", "Account", required=True, endpoint=True, help="The account identifier, org-account."),
        Field("user", "User", required=True, endpoint=True), PASSWORD,
        Field("warehouse", "Warehouse"), Field("db_role", "Role", endpoint=True),
        Field("database", "Database", endpoint=True), Field("schema", "Schema")),
       None, ST, "untested", _Q_DQ, "upper", "format", "limit", _SNOWFLAKE,
       "merge", "put_copy", "SQL guard only: use a role with SELECT only"),
    _d("hana", "SAP HANA", "hana", "database", "hdbcli.dbapi", "hdbcli", "hana",
       _net(Field("encrypt", "Encrypt", type="bool", default=True)),
       30015, ST, "untested", _Q_DQ, "upper", "qmark", "limit", _HANA,
       "upsert_pk", "executemany", "SET TRANSACTION READ ONLY"),
    _d("teradata", "Teradata", "teradata", "database", "teradatasql", "teradatasql", "teradata",
       _net(Field("logmech", "Sign-in mechanism", type="select", default="TD2",
                  choices=("TD2", "LDAP", "KRB5", "TDNEGO"))),
       1025, ST, "untested", _Q_DQ, None, "qmark", "sample", _TERADATA,
       "merge", "executemany", "SQL guard + reads prefixed LOCKING ROW FOR ACCESS"),
    _d("firebird", "Firebird", "firebird", "database", "firebird.driver", "firebird-driver", "firebird",
       (HOST, PORT, Field("database", "Database path or alias", required=True, endpoint=True),
        Field("user", "User", required=True, endpoint=True), PASSWORD),
       3050, ST, "untested", _Q_DQ, "upper", "qmark", "first", _FIREBIRD,
       "update_or_insert", "executemany", "A read-only transaction (isc_tpb_read)"),
    _d("odbc", "Other (ODBC)", "odbc", "database", "pyodbc", "pyodbc", "odbc",
       (Field("dsn", "DSN", endpoint=True, help="A data source name set up in the ODBC manager."),
        Field("connection_string", "Connection string", type="textarea", endpoint=True,
              help="Instead of a DSN. A password in it is moved to the secrets store."),
        Field("user", "User", endpoint=True), PASSWORD,
        Field("dialect", "SQL dialect", type="select", default="ansi",
              choices=("ansi", "informix", "sybase", "other"))),
       None, ST, "untested", _Q_DQ, None, "qmark", "none", _ODBC,
       "delete_insert", "executemany", "pyodbc.connect(readonly=True) (SQL_ATTR_ACCESS_MODE)",
       notes=("Informix, Sybase ASE, Temenos T24 (through the database it runs on, or its ODBC driver), "
              "or anything else with an ODBC driver.")),
    # document
    _d("mongodb", "MongoDB", "mongodb", "document", "pymongo", "pymongo", "mongodb",
       (Field("uri", "Hosts or URI", required=True, endpoint=True,
              help="host1:27017,host2:27017 or a mongodb:// URI. A password in it is moved to secrets."),
        Field("database", "Database", required=True, endpoint=True),
        Field("user", "User", endpoint=True), PASSWORD,
        Field("auth_source", "Auth database", default="admin"),
        Field("tls", "TLS", type="bool", default=True)),
       27017, S, "simulated", _Q_DQ, None, "qmark", "none", {},
       None, "", "readPreference secondaryPreferred; only find and aggregate",
       notes="A table is a collection. A query is JSON: a filter and projection, or a pipeline."),
    # local folder
    _d("folder", "Folder on this machine", "folder", "folder", "pyarrow", "built in", None,
       (Field("path", "Folder", type="path", required=True, endpoint=True,
              help="A folder inside the project, such as landing."),), None, ST, "real", _Q_DQ, None,
       "qmark", "none", {}, None, "file_writer", "Files are opened read-only", group="Remote locations"),
    # remote locations (section 4.1)
    _d("share", "Shared folder", "remote", "remote", "os", "built in", None,
       (Field("path", "Folder path", required=True, endpoint=True,
              help=r"An absolute path or a UNC path such as \\server\share\in."),),
       None, ST, "real", _Q_DQ, None, "qmark", "none", {}, None, "file_writer",
       "Files are only read", group="Remote locations"),
    _d("sftp", "SFTP", "remote", "remote", "paramiko", "paramiko", "sftp",
       (Field("host", "Host", required=True, endpoint=True), Field("port", "Port", type="int", endpoint=True),
        Field("user", "User", required=True, endpoint=True),
        Field("password", "Password", type="password", secret=True),
        Field("private_key", "Private key", type="password", secret=True,
              help="Paste the key text. It is kept in memory only, never written to disk."),
        Field("passphrase", "Key passphrase", type="password", secret=True),
        Field("host_key_fingerprint", "Host key fingerprint", required=True, endpoint=True,
              help="SHA256:... as shown by Test. An unknown key is never accepted."),
        Field("path", "Folder", endpoint=True, help="The folder on the server, such as /in.")),
       22, ST, "simulated", _Q_DQ, None, "qmark", "none", {}, None, "upload", "Files are only read",
       group="Remote locations"),
    _d("ftp", "FTP / FTPS", "remote", "remote", "ftplib", "built in", None,
       (Field("host", "Host", required=True, endpoint=True), Field("port", "Port", type="int", endpoint=True),
        Field("user", "User", endpoint=True), Field("password", "Password", type="password", secret=True),
        Field("tls", "Explicit FTPS", type="bool", default=True),
        Field("passive", "Passive mode", type="bool", default=True),
        Field("path", "Folder", endpoint=True)),
       21, ST, "simulated", _Q_DQ, None, "qmark", "none", {}, None, "upload", "Files are only read",
       group="Remote locations", notes="Plain FTP needs options.allow_plain: the password is sent in clear."),
    _d("s3", "S3 or S3-compatible", "remote", "remote", "boto3", "boto3", "s3",
       (Field("endpoint_url", "Endpoint URL", endpoint=True, help="Empty for AWS, or a MinIO URL (https)."),
        Field("region", "Region", endpoint=True), Field("bucket", "Bucket", required=True, endpoint=True),
        Field("prefix", "Prefix"), Field("access_key_id", "Access key id", endpoint=True),
        Field("secret_access_key", "Secret access key", type="password", secret=True)),
       None, ST, "simulated", _Q_DQ, None, "qmark", "none", {}, None, "upload", "Files are only read",
       group="Remote locations"),
    _d("azure_blob", "Azure Blob Storage", "remote", "remote", "azure.storage.blob", "azure-storage-blob",
       "azure",
       (Field("account_url", "Account URL", required=True, endpoint=True,
              help="https://<account>.blob.core.windows.net, with no token in it."),
        Field("container", "Container", required=True, endpoint=True), Field("prefix", "Prefix"),
        Field("auth", "Sign-in", type="select", default="sas_token", choices=("sas_token", "account_key")),
        Field("sas_token", "SAS token", type="password", secret=True),
        Field("account_key", "Account key", type="password", secret=True)),
       None, ST, "simulated", _Q_DQ, None, "qmark", "none", {}, None, "upload", "Files are only read",
       group="Remote locations"),
    _d("http", "HTTP(S) download", "remote", "remote", "urllib.request", "built in", None,
       (Field("url", "URL", required=True, endpoint=True,
              help="May hold {date} or {date:%Y%m%d}."),
        Field("header_name", "Token header", help="Such as Authorization."),
        Field("header_value", "Token value", type="password", secret=True),
        Field("verify_tls", "Verify TLS", type="bool", default=True)),
       None, S, "real", _Q_DQ, None, "qmark", "none", {}, None, "", "Files are only read",
       group="Remote locations"),
]

REGISTRY: dict[str, Driver] = {d.kind: d for d in _DRIVERS}

SECRET_FIELD_NAMES = frozenset(f.name.lower() for d in _DRIVERS for f in d.fields if f.secret)
ENDPOINT_FIELD_NAMES = tuple(dict.fromkeys(f.name for d in _DRIVERS for f in d.fields if f.endpoint))

FAMILY_MODULES = {"postgres": "pg", "sqlserver": "mssql", "mysql": "mysql", "oracle": "oracle", "db2": "db2",
                  "sqlite": "sqlite", "duckdb": "duckdb", "clickhouse": "clickhouse",
                  "snowflake": "snowflake", "hana": "hana", "teradata": "teradata", "firebird": "firebird",
                  "odbc": "odbc", "mongodb": "mongo"}


# ---------------------------------------------------------------- lookups

def get(kind: str) -> Driver:
    d = REGISTRY.get(str(kind or ""))
    if d is None:
        raise UnknownKind(f"Unknown connection kind '{kind}'. Known kinds: {', '.join(sorted(REGISTRY))}.")
    return d


def family(kind: str) -> str | None:
    """The family of a kind, a family name as it is, or None when unknown."""
    if kind in REGISTRY:
        return REGISTRY[kind].family
    if kind in FAMILY_MODULES:
        return kind
    return None


def kinds(category: str | None = None) -> list[str]:
    return [d.kind for d in _DRIVERS if category is None or d.category == category]


def is_database(kind) -> bool:
    d = REGISTRY.get(kind)
    return bool(d and d.category in ("database", "document"))


def is_sql(kind) -> bool:
    d = REGISTRY.get(kind)
    return bool(d and d.category == "database")


def is_remote(kind) -> bool:
    d = REGISTRY.get(kind)
    return bool(d and d.category == "remote")


def module_spec_exists(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def installed(kind: str) -> bool:
    d = get(kind)
    if d.pip == "built in":
        return True
    return module_spec_exists(d.module)


def install_hint(kind: str) -> str | None:
    d = get(kind)
    if d.pip == "built in" or d.extra is None:
        return None
    return f"pip install etlplus[{d.extra}]"


def missing_driver_message(kind: str) -> str:
    d = get(kind)
    hint = install_hint(kind) or f"pip install {d.pip}"
    return f"The {d.label} driver ({d.pip}) is not installed. Install it with: {hint}."


def needs(kind: str) -> str:
    """One short line naming the required fields, for gallery cards."""
    d = get(kind)
    names = [f.label for f in d.fields if f.required or f.name in ("port", "database", "user", "password")]
    if not names:
        return ""
    return ", ".join([names[0]] + [n[0].lower() + n[1:] for n in names[1:]])


def field_dict(f: Field) -> dict:
    out = asdict(f)
    out["choices"] = list(f.choices)
    return out


_STATUS_CACHE: list | None = None


def status_table() -> list[dict]:
    """Rows for GET /api/kinds. Cached for the life of the process (find_spec is not free)."""
    global _STATUS_CACHE
    if _STATUS_CACHE is None:
        rows = []
        for d in _DRIVERS:
            rows.append({
                "kind": d.kind, "label": d.label, "family": d.family, "category": d.category, "group": d.group,
                "fields": [field_dict(f) for f in d.fields], "roles": sorted(d.roles), "status": d.status,
                "installed": installed(d.kind), "install": install_hint(d.kind), "package": d.pip,
                "default_port": d.default_port, "read_only": d.read_only, "upsert": d.upsert, "bulk": d.bulk,
                "needs": needs(d.kind), "notes": d.notes,
            })
        _STATUS_CACHE = rows
    return copy.deepcopy(_STATUS_CACHE)


def card(kind: str) -> dict:
    """A gallery card (section 6) for a connection kind."""
    d = get(kind)
    icon = {"database": "database", "document": "file-text", "folder": "folder",
            "remote": "cloud"}.get(d.category, "database")
    if d.kind == "share":
        icon = "folder-simple"
    elif d.kind == "http":
        icon = "globe"
    group = "Remote locations" if d.category in ("remote", "folder") else "Databases"
    return {"id": d.kind, "group": group, "label": d.label, "icon": icon, "needs": needs(kind),
            "installed": installed(kind), "install": install_hint(kind), "status": d.status, "kind": d.kind,
            "format": None, "roles": sorted(d.roles), "verify_note": None, "notes": d.notes}


# ---------------------------------------------------------------- connect

def _family_module(fam: str):
    name = FAMILY_MODULES.get(fam)
    if not name:
        raise DriverError(f"There is no database driver for the '{fam}' family.")
    return importlib.import_module(f"etlplus.drivers.{name}")


def connect(project, cid: str, *, role: str, override: dict | None = None, spec: dict | None = None):
    """The one connect path. role "source" gives a read-only session; "target" a writable one."""
    from etlplus import connections
    if role not in ("source", "target"):
        raise DriverError("role is source or target.")
    spec = spec if spec is not None else connections.spec_for(project, cid, override)
    kind = spec.get("kind")
    d = get(kind)
    if d.category not in ("database", "document"):
        raise DriverError(f"Connection '{cid}' ({kind}) is not a database.")
    if role not in d.roles:
        raise DriverError(f"{d.label} can only be a {' or '.join(sorted(d.roles))}.")
    if role == "target" and connections.is_read_only(spec):
        raise DriverError(f"Connection '{cid}' is a source; it is opened read-only.")
    mod = _family_module(d.family)
    return mod.connect(spec, spec.get("_password"), role, project=project)


# ---------------------------------------------------------------- names

_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _drv_or_none(kind):
    return REGISTRY.get(kind)


def name_limit(kind) -> int:
    return NAME_LIMITS.get(family(kind) or "", DEFAULT_NAME_LIMIT)


def check_name(kind, name: str) -> str:
    """Refuse a name that cannot be a safe identifier, with a plain message naming it."""
    if name is None or str(name) == "":
        raise DriverError("A name is empty.")
    name = str(name)
    if _CTRL.search(name):
        raise DriverError(f"The name {name!r} holds a control character.")
    lim = name_limit(kind)
    if len(name) > lim:
        raise DriverError(f"The name '{name[:40]}...' is longer than {lim} characters.")
    return name


def _unquote_part(part: str) -> tuple[str, bool]:
    """(inner name, was_quoted). A quoted part keeps its case."""
    p = part.strip()
    if len(p) >= 2 and ((p[0] == '"' and p[-1] == '"') or (p[0] == "`" and p[-1] == "`")
                        or (p[0] == "[" and p[-1] == "]")):
        inner = p[1:-1]
        if p[0] == "[":
            inner = inner.replace("]]", "]")
        else:
            inner = inner.replace(p[0] * 2, p[0])
        return inner, True
    return p, False


def _fold(kind, inner: str) -> str:
    d = _drv_or_none(kind)
    fold = d.fold if d else ({"postgres": "lower", "oracle": "upper"}.get(kind))
    if not _PLAIN.match(inner):
        return inner
    if fold == "lower" and inner == inner.upper():
        return inner.lower()
    if fold == "upper" and inner == inner.lower():
        return inner.upper()
    return inner


def _quote_chars(kind) -> tuple[str, str]:
    d = _drv_or_none(kind)
    if d:
        return d.quote
    return {"sqlserver": _Q_BR, "mysql": _Q_BT, "clickhouse": _Q_BT}.get(kind, _Q_DQ)


def quote_ident(kind, name) -> str:
    """Quote one identifier. Unquoted names fold the way the server folds them; a quoted name
    keeps its case and is re-escaped."""
    raw = str(name if name is not None else "").strip()
    inner, quoted = _unquote_part(raw)
    if not quoted:
        inner = _fold(kind, inner)
    check_name(kind, inner)
    open_, close = _quote_chars(kind)
    body = inner
    if family(kind) == "clickhouse":
        body = body.replace("\\", "\\\\")
    body = body.replace(close, close * 2)
    return open_ + body + close


def split_name(name: str) -> list[str]:
    """Split schema.table on dots outside quotes. At most 3 parts."""
    name = str(name or "")
    parts, cur, i, n = [], "", 0, len(name)
    while i < n:
        c = name[i]
        if c in '"`[':
            close = {'"': '"', "`": "`", "[": "]"}[c]
            j = i + 1
            while True:
                j = name.find(close, j)
                if j < 0:
                    raise DriverError(f"Unclosed quote in the name '{name}'.")
                if j + 1 < n and name[j + 1] == close:
                    j += 2
                    continue
                break
            cur += name[i:j + 1]
            i = j + 1
        elif c == ".":
            parts.append(cur.strip())
            cur = ""
            i += 1
        else:
            cur += c
            i += 1
    parts.append(cur.strip())
    if any(not p for p in parts):
        raise DriverError(f"The name '{name}' is not valid.")
    if len(parts) > 3:
        raise DriverError(f"The name '{name}' has more than 3 parts (database.schema.table).")
    return parts


def quote_table(kind, name) -> str:
    return ".".join(quote_ident(kind, p) for p in split_name(name))


def split_table(name: str) -> tuple[str | None, str]:
    parts = [_unquote_part(p)[0] for p in split_name(name)]
    if len(parts) == 1:
        return None, parts[0]
    return parts[-2], parts[-1]


def placeholder(kind, i: int) -> str:
    d = _drv_or_none(kind)
    style = d.paramstyle if d else {"postgres": "format", "mysql": "format", "oracle": "numeric"}.get(kind, "qmark")
    if style == "format":
        return "%s"
    if style == "numeric":
        return f":{int(i) + 1}"
    if style == "pyformat":
        return f"%(p{int(i)})s"
    return "?"


def bind_params(kind, params):
    """Params in the shape the driver takes: a dict for pyformat, else a list."""
    params = list(params or [])
    d = _drv_or_none(kind)
    if d and d.paramstyle == "pyformat":
        return {f"p{i}": v for i, v in enumerate(params)}
    return params


def escapes_percent(kind) -> bool:
    """True when a literal % in SQL with parameters must be written %% (format styles)."""
    d = _drv_or_none(kind)
    return bool(d and d.paramstyle in ("format", "pyformat"))


def limit_style(kind) -> str:
    d = _drv_or_none(kind)
    return d.limit_style if d else "limit"


def select_sql(kind, table, columns="*", where: str = "", limit: int | None = None) -> str:
    if columns in (None, "*", ["*"]) or not columns:
        cols = "*"
    else:
        if isinstance(columns, str):
            columns = [c.strip() for c in columns.split(",") if c.strip()]
        cols = ", ".join(quote_ident(kind, c) for c in columns)
    tbl = quote_table(kind, table)
    style = limit_style(kind) if limit is not None else None
    head = "SELECT "
    if style == "top":
        head += f"TOP {int(limit)} "
    elif style == "first":
        head += f"FIRST {int(limit)} "
    sql = f"{head}{cols} FROM {tbl}"
    if where:
        sql += f" WHERE {where}"
    if style == "limit":
        sql += f" LIMIT {int(limit)}"
    elif style == "fetch_first":
        sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
    elif style == "sample":
        sql += f" SAMPLE {int(limit)}"
    return sql


def limit_sql(kind, sql: str, n: int) -> str | None:
    """Wrap a query so it returns at most n rows; None when the kind has no way (use fetchmany)."""
    n = int(n)
    style = limit_style(kind)
    if style == "limit":
        return f"SELECT * FROM ({sql}) q LIMIT {n}"
    if style in ("top", "sample"):
        return f"SELECT TOP {n} * FROM ({sql}) q"
    if style == "fetch_first":
        return f"SELECT * FROM ({sql}) q FETCH FIRST {n} ROWS ONLY"
    if style == "first":
        return f"SELECT FIRST {n} * FROM ({sql}) q"
    return None


# ---------------------------------------------------------------- catalog SQL

VERSION_SQL = {
    "postgres": "SELECT version()",
    "sqlserver": "SELECT @@VERSION",
    "mysql": "SELECT VERSION()",
    "oracle": "SELECT banner FROM v$version WHERE ROWNUM = 1",
    "db2": "SELECT service_level FROM SYSIBMADM.ENV_INST_INFO",
    "sqlite": "SELECT 'SQLite ' || sqlite_version()",
    "duckdb": "SELECT 'DuckDB ' || version()",
    "clickhouse": "SELECT 'ClickHouse ' || version()",
    "snowflake": "SELECT 'Snowflake ' || CURRENT_VERSION()",
    "hana": "SELECT 'SAP HANA ' || VERSION FROM SYS.M_DATABASE",
    "teradata": "SELECT 'Teradata ' || InfoData FROM DBC.DBCInfoV WHERE InfoKey = 'VERSION'",
    "firebird": "SELECT 'Firebird ' || rdb$get_context('SYSTEM', 'ENGINE_VERSION') FROM rdb$database",
}

ORACLE_SYSTEM = ("SYS", "SYSTEM", "XDB", "MDSYS", "CTXSYS", "OUTLN", "DBSNMP", "APPQOSSYS",
                 "ORDSYS", "ORDDATA", "OLAPSYS", "WMSYS", "LBACSYS", "DVSYS", "GSMADMIN_INTERNAL",
                 "OJVMSYS", "AUDSYS", "DBSFWUSER", "REMOTE_SCHEDULER_AGENT", "GGSYS", "ORACLE_OCM")


def version_sql(kind) -> str | None:
    fam = family(kind)
    return VERSION_SQL.get(fam or "")


def _sql_family(kind) -> str:
    d = REGISTRY.get(kind)
    if d is None:
        raise DriverError(f"Unknown connection kind '{kind}'. Known kinds: {', '.join(sorted(REGISTRY))}.")
    if d.category != "database":
        raise DriverError(f"{d.label} has no SQL catalog; it lists {'collections' if d.family == 'mongodb' else 'files'}.")
    return d.family


def tables_sql(kind, schema) -> tuple[str, list]:
    """(sql, params) listing schema, name, kind, rows."""
    fam = _sql_family(kind)
    p = placeholder(kind, 0)
    prm = [schema] if schema else []
    if fam == "postgres":
        sql = ("SELECT t.table_schema, t.table_name, t.table_type, c.reltuples "
               "FROM information_schema.tables t "
               "LEFT JOIN pg_catalog.pg_namespace n ON n.nspname = t.table_schema "
               "LEFT JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = t.table_name "
               "WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')")
        if schema:
            return sql + f" AND t.table_schema = {p} ORDER BY 1, 2", prm
        return sql + " ORDER BY 1, 2", []
    if fam == "sqlserver":
        sql = ("SELECT s.name, o.name, o.type_desc, SUM(p.rows) "
               "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
               "LEFT JOIN sys.partitions p ON p.object_id = o.object_id AND p.index_id IN (0, 1) "
               "WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0")
        if schema:
            sql += f" AND s.name = {p}"
        return sql + " GROUP BY s.name, o.name, o.type_desc ORDER BY 1, 2", prm
    if fam == "mysql":
        sql = ("SELECT table_schema, table_name, table_type, table_rows FROM information_schema.tables "
               "WHERE table_schema = " + (p if schema else "DATABASE()") + " ORDER BY 1, 2")
        return sql, prm
    if fam == "oracle":
        owners = ", ".join(f"'{o}'" for o in ORACLE_SYSTEM)
        where = "owner = :1" if schema else f"owner NOT IN ({owners})"
        sql = (f"SELECT owner, table_name, 'TABLE', num_rows FROM all_tables WHERE {where} "
               f"UNION ALL SELECT owner, view_name, 'VIEW', NULL FROM all_views WHERE {where} "
               "ORDER BY 1, 2")
        return sql, [schema.upper(), schema.upper()] if schema else []
    if fam == "db2":
        sql = ("SELECT TRIM(TABSCHEMA), TABNAME, CASE TYPE WHEN 'V' THEN 'VIEW' ELSE 'TABLE' END, CARD "
               "FROM SYSCAT.TABLES WHERE TYPE IN ('T', 'V') AND TABSCHEMA NOT LIKE 'SYS%'")
        if schema:
            return sql + " AND TABSCHEMA = ? ORDER BY 1, 2", [schema.upper()]
        return sql + " ORDER BY 1, 2", []
    if fam == "sqlite":
        return ("SELECT 'main', name, type, NULL FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY 2"), []
    if fam == "duckdb":
        sql = ("SELECT t.table_schema, t.table_name, t.table_type, d.estimated_size "
               "FROM information_schema.tables t LEFT JOIN duckdb_tables() d "
               "ON d.schema_name = t.table_schema AND d.table_name = t.table_name "
               "AND d.database_name = t.table_catalog "
               "WHERE t.table_schema NOT IN ('information_schema', 'pg_catalog') "
               "AND t.table_catalog = current_database()")
        if schema:
            return sql + " AND t.table_schema = ? ORDER BY 1, 2", prm
        return sql + " ORDER BY 1, 2", []
    if fam == "clickhouse":
        sql = ("SELECT database, name, if(engine LIKE '%View', 'VIEW', 'TABLE'), total_rows FROM system.tables "
               "WHERE database = " + (p if schema else "currentDatabase()") + " ORDER BY 1, 2")
        return sql, prm
    if fam == "snowflake":
        sql = ("SELECT table_schema, table_name, table_type, row_count FROM information_schema.tables "
               "WHERE table_schema <> 'INFORMATION_SCHEMA'")
        if schema:
            return sql + f" AND table_schema = {p} ORDER BY 1, 2", [schema.upper()]
        return sql + " ORDER BY 1, 2", []
    if fam == "hana":
        sch = "?" if schema else "CURRENT_SCHEMA"
        sql = (f"SELECT SCHEMA_NAME, TABLE_NAME, 'TABLE', RECORD_COUNT FROM SYS.M_TABLES WHERE SCHEMA_NAME = {sch} "
               f"UNION ALL SELECT SCHEMA_NAME, VIEW_NAME, 'VIEW', NULL FROM SYS.VIEWS WHERE SCHEMA_NAME = {sch} "
               "ORDER BY 1, 2")
        return sql, [schema.upper(), schema.upper()] if schema else []
    if fam == "teradata":
        sql = ("SELECT DatabaseName, TableName, CASE TableKind WHEN 'V' THEN 'VIEW' ELSE 'TABLE' END, NULL "
               "FROM DBC.TablesV WHERE TableKind IN ('T', 'O', 'V') AND DatabaseName = "
               + ("?" if schema else "DATABASE") + " ORDER BY 1, 2")
        return sql, prm
    if fam == "firebird":
        return ("SELECT 'main', TRIM(RDB$RELATION_NAME), CASE WHEN RDB$VIEW_BLR IS NULL THEN 'TABLE' ELSE 'VIEW' END, "
                "NULL FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 ORDER BY 2"), []
    if fam == "odbc":
        sql = ("SELECT table_schema, table_name, table_type, NULL FROM information_schema.tables "
               "WHERE table_schema NOT IN ('information_schema', 'INFORMATION_SCHEMA')")
        if schema:
            return sql + " AND table_schema = ? ORDER BY 1, 2", prm
        return sql + " ORDER BY 1, 2", []
    raise DriverError(f"{kind} cannot list tables.")


def columns_sql(kind, table) -> tuple[str, list]:
    """(sql, params) listing name, type, nullable (1/0). An unknown kind gives a plain error."""
    fam = _sql_family(kind)
    schema, name = split_table(table)
    p0, p1 = placeholder(kind, 0), placeholder(kind, 1)
    both = [name, schema] if schema else [name]
    if fam == "sqlite":
        return "SELECT name, type, \"notnull\" = 0 FROM pragma_table_info(?) ORDER BY cid", [name]
    if fam == "oracle":
        sql = ("SELECT column_name, data_type || CASE WHEN data_type IN ('VARCHAR2', 'CHAR', 'NVARCHAR2') "
               "THEN '(' || char_length || ')' WHEN data_type = 'NUMBER' AND data_precision IS NOT NULL "
               "THEN '(' || data_precision || ',' || data_scale || ')' ELSE '' END, "
               "CASE nullable WHEN 'Y' THEN 1 ELSE 0 END FROM all_tab_columns "
               f"WHERE UPPER(table_name) = UPPER({p0}) AND owner = "
               + (f"UPPER({p1})" if schema else "USER") + " ORDER BY column_id")
        return sql, both
    if fam == "db2":
        sql = ("SELECT COLNAME, TYPENAME || CASE WHEN TYPENAME IN ('VARCHAR', 'CHARACTER') THEN "
               "'(' || VARCHAR(LENGTH) || ')' WHEN TYPENAME = 'DECIMAL' THEN '(' || VARCHAR(LENGTH) || ',' || "
               "VARCHAR(SCALE) || ')' ELSE '' END, CASE NULLS WHEN 'Y' THEN 1 ELSE 0 END FROM SYSCAT.COLUMNS "
               "WHERE UPPER(TABNAME) = UPPER(?) AND TABSCHEMA = " + ("UPPER(?)" if schema else "CURRENT SCHEMA")
               + " ORDER BY COLNO")
        return sql, both
    if fam == "clickhouse":
        sql = ("SELECT name, type, startsWith(type, 'Nullable') FROM system.columns "
               f"WHERE table = {p0} AND database = " + (p1 if schema else "currentDatabase()")
               + " ORDER BY position")
        return sql, both
    if fam == "hana":
        sql = ("SELECT COLUMN_NAME, DATA_TYPE_NAME || CASE WHEN DATA_TYPE_NAME IN ('VARCHAR', 'NVARCHAR') "
               "THEN '(' || LENGTH || ')' WHEN DATA_TYPE_NAME = 'DECIMAL' THEN '(' || LENGTH || ',' || SCALE || ')' "
               "ELSE '' END, CASE IS_NULLABLE WHEN 'TRUE' THEN 1 ELSE 0 END FROM SYS.TABLE_COLUMNS "
               "WHERE UPPER(TABLE_NAME) = UPPER(?) AND SCHEMA_NAME = " + ("UPPER(?)" if schema else "CURRENT_SCHEMA")
               + " ORDER BY POSITION")
        return sql, both
    if fam == "teradata":
        sql = ("SELECT TRIM(ColumnName), ColumnType, CASE Nullable WHEN 'Y' THEN 1 ELSE 0 END FROM DBC.ColumnsV "
               "WHERE TableName = ? AND DatabaseName = " + ("?" if schema else "DATABASE") + " ORDER BY ColumnId")
        return sql, both
    if fam == "firebird":
        sql = ("SELECT TRIM(rf.RDB$FIELD_NAME), CASE f.RDB$FIELD_TYPE WHEN 7 THEN 'SMALLINT' WHEN 8 THEN 'INTEGER' "
               "WHEN 16 THEN 'BIGINT' WHEN 10 THEN 'FLOAT' WHEN 27 THEN 'DOUBLE PRECISION' WHEN 12 THEN 'DATE' "
               "WHEN 13 THEN 'TIME' WHEN 35 THEN 'TIMESTAMP' WHEN 37 THEN 'VARCHAR' WHEN 14 THEN 'CHAR' "
               "WHEN 261 THEN 'BLOB' WHEN 23 THEN 'BOOLEAN' ELSE 'OTHER' END, "
               "CASE WHEN rf.RDB$NULL_FLAG = 1 THEN 0 ELSE 1 END "
               "FROM RDB$RELATION_FIELDS rf JOIN RDB$FIELDS f ON f.RDB$FIELD_NAME = rf.RDB$FIELD_SOURCE "
               "WHERE UPPER(TRIM(rf.RDB$RELATION_NAME)) = UPPER(?) ORDER BY rf.RDB$FIELD_POSITION")
        return sql, [name]
    default_schema = {"postgres": "current_schema()", "sqlserver": "SCHEMA_NAME()", "mysql": "DATABASE()",
                      "duckdb": "current_schema()", "snowflake": "CURRENT_SCHEMA()", "odbc": None}[fam]
    type_expr = "data_type"
    if fam in ("postgres", "snowflake"):
        type_expr = ("data_type || CASE WHEN character_maximum_length > 0 THEN '(' || "
                     "CAST(character_maximum_length AS VARCHAR(20)) || ')' ELSE '' END")
    elif fam == "sqlserver":
        type_expr = ("data_type + CASE WHEN character_maximum_length > 0 THEN '(' + "
                     "CAST(character_maximum_length AS VARCHAR(20)) + ')' "
                     "WHEN character_maximum_length = -1 THEN '(max)' ELSE '' END")
    elif fam == "mysql":
        type_expr = "column_type"
    where_schema = ""
    if schema:
        where_schema = f" AND table_schema = {p1}"
    elif default_schema:
        where_schema = f" AND table_schema = {default_schema}"
    sql = (f"SELECT column_name, {type_expr}, CASE WHEN is_nullable = 'YES' THEN 1 ELSE 0 END "
           "FROM information_schema.columns "
           f"WHERE LOWER(table_name) = LOWER({p0}){where_schema} ORDER BY ordinal_position")
    return sql, both


# ---------------------------------------------------------------- types

_GENERIC_RE = re.compile(r"^\s*([a-z]+)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$", re.I)


def parse_generic(generic: str) -> tuple[str, int | None, int | None]:
    """'decimal(18,2)' -> ('decimal', 18, 2); 'string(40)' -> ('string', 40, None)."""
    m = _GENERIC_RE.match(str(generic or ""))
    if not m or m.group(1).lower() not in GENERIC_TYPES:
        raise DriverError(f"'{generic}' is not a column type. Use one of: {', '.join(GENERIC_TYPES)}.")
    base = m.group(1).lower()
    a = int(m.group(2)) if m.group(2) else None
    b = int(m.group(3)) if m.group(3) else None
    if base == "string" and b is not None:
        raise DriverError(f"'{generic}': string takes one length, such as string(40).")
    if base not in ("string", "decimal") and a is not None:
        raise DriverError(f"'{generic}': {base} takes no length.")
    return base, a, b


def native_type_info(kind, generic: str) -> tuple[str, str | None]:
    """(native type, note or None). The note says when the type is cut or stored with a loss."""
    d = get(kind)
    if not d.type_map:
        raise DriverError(f"{d.label} has no column types.")
    base, a, b = parse_generic(generic)
    tpl = d.type_map[base]
    note = LOSSY.get(d.kind if d.kind in LOSSY else d.family, {}).get(base)
    if base == "string":
        n = a if a is not None else DEFAULT_STRING_LENGTH
        if n < 1:
            raise DriverError("A string length is at least 1.")
        if isinstance(tpl, tuple):
            template, mx, overflow = tpl
            if n > mx:
                return overflow, note
            return template.format(n=n), note
        return tpl.format(n=n) if "{n}" in tpl else tpl, note
    if base == "decimal":
        p = a if a is not None else DEFAULT_DECIMAL[0]
        s = b if b is not None else (0 if a is not None else DEFAULT_DECIMAL[1])
        if p < 1 or s > p:
            raise DriverError(f"decimal({p},{s}): the scale must be between 0 and the precision.")
        if isinstance(tpl, tuple):
            template, mx = tpl[0], tpl[1]
            if p > mx:
                # keep the whole digits (p-s); the scale gives way (decimal(50,40) -> (38,28))
                p0 = p
                p, s = mx, max(0, mx - (p - s))
                note = (f"Precision {p0} is above {mx}, the most {d.label} takes; "
                        f"stored as decimal({p},{s}).")
            return template.format(p=p, s=s), note
        return tpl.format(p=p, s=s) if "{p}" in tpl else tpl, note
    return tpl, note


def native_type(kind, generic: str) -> str:
    return native_type_info(kind, generic)[0]


def type_table(kind) -> list[dict]:
    """Rows for GET /api/types?kind=: the reference type map of section 3.3."""
    out = []
    for g in GENERIC_TYPES:
        sample = {"string": "string(n)", "decimal": "decimal(p,s)"}.get(g, g)
        try:
            native, note = native_type_info(kind, {"string": "string(40)", "decimal": "decimal(18,2)"}.get(g, g))
        except DriverError:
            continue
        out.append({"generic": sample, "native": native, "note": note})
    return out


_NATIVE_ARGS = re.compile(r"\(([^()]*)\)")


def generic_type(kind, native: str) -> str:
    """Best-effort reverse map of a native type, for column designs read from a database."""
    t = str(native or "").strip().lower()
    for wrap in ("nullable(", "lowcardinality("):
        while t.startswith(wrap) and t.endswith(")"):
            t = t[len(wrap):-1].strip()
    args = _NATIVE_ARGS.search(t)
    nums = []
    if args:
        for x in args.group(1).split(","):
            x = x.strip().split()[0] if x.strip() else ""
            if x.isdigit():
                nums.append(int(x))
    base = _NATIVE_ARGS.sub("", t).strip()
    fam = family(kind)
    if base in ("json", "jsonb", "variant", "super", "object"):
        return "json"
    if "sub_type text" in base or base in ("clob", "nclob", "dbclob", "longtext", "mediumtext", "text", "ntext",
                                           "tinytext", "string", "long varchar", "long", "nvarchar(max)"):
        return "text"
    if "char" in base or "string" in base or base in ("uuid", "enum", "set"):
        if "max" in t or not nums:
            return "text"
        return f"string({nums[0]})"
    if base in ("bit", "bool", "boolean") or (base == "tinyint" and nums == [1]):
        return "boolean"
    if fam == "oracle" and base == "number" and nums == [1]:
        return "boolean"
    if base == "int8":
        return "integer" if fam == "clickhouse" else "bigint"
    if base in ("bigint", "int64", "uint32", "bigserial", "serial8"):
        return "bigint"
    if base in ("int", "integer", "int4", "int32", "int16", "int2", "smallint", "tinyint", "mediumint", "byteint",
                "uint8", "uint16", "serial", "smallserial", "int unsigned", "integer unsigned", "short"):
        return "integer"
    if base in ("decimal", "numeric", "number", "dec", "money", "smallmoney", "fixed"):
        if base == "number" and len(nums) == 2 and nums[1] == 0:
            return "integer" if nums[0] <= 10 else ("bigint" if nums[0] <= 19 else f"decimal({nums[0]},0)")
        if base == "number" and len(nums) == 1:
            return "integer" if nums[0] <= 10 else ("bigint" if nums[0] <= 19 else f"decimal({nums[0]},0)")
        if len(nums) >= 2:
            return f"decimal({nums[0]},{nums[1]})"
        if len(nums) == 1:
            return f"decimal({nums[0]},0)"
        return "decimal"
    if base in ("double", "double precision", "float", "float4", "float8", "real", "binary_double",
                "binary_float", "float64", "float32", "decfloat", "smallfloat"):
        return "double"
    if "with time zone" in base or base in ("timestamptz", "datetimeoffset", "timestamp_tz", "timestamp_ltz") \
            or (base.startswith("datetime64") and "utc" in t) or base == "timestamp with local time zone":
        return "timestamptz"
    if base in ("timestamp", "datetime", "datetime2", "smalldatetime", "timestamp_ntz", "datetime64",
                "timestamp without time zone", "seconddate"):
        return "timestamp"
    if base in ("date", "date32"):
        return "date"
    if base in ("time", "time without time zone", "timetz", "time with time zone"):
        return "time"
    if base in ("bytea", "blob", "binary", "varbinary", "longblob", "mediumblob", "tinyblob", "varbyte", "raw",
                "long raw", "image", "bytes"):
        return "binary"
    return "text"


def arrow_to_generic(t) -> str:
    import pyarrow as pa
    if pa.types.is_null(t) or pa.types.is_string(t) or pa.types.is_large_string(t):
        return "text"
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_integer(t):
        if t.bit_width <= 16 or (t.bit_width == 32 and pa.types.is_signed_integer(t)):
            return "integer"
        if t.bit_width == 64 and pa.types.is_unsigned_integer(t):
            return "decimal(20,0)"
        return "bigint"
    if pa.types.is_decimal(t):
        return f"decimal({min(t.precision, 38)},{max(t.scale, 0)})"
    if pa.types.is_floating(t):
        return "double"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_time(t):
        return "time"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t):
        return "binary"
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_struct(t) or pa.types.is_map(t):
        return "json"
    return "text"
