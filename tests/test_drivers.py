"""The driver registry (SPEC_V2 section 3): kinds, type map, quoting, limits, read-only sessions
through fake DB-API modules, ODBC safety and the save/secret split."""

import sys
import types

import pyarrow as pa
import pytest

from etlplus import config, connections, drivers, guard, secrets, sources
from etlplus.drivers import DriverError

DB_KINDS = ["postgres", "redshift", "greenplum", "cockroachdb", "timescaledb", "yugabytedb", "sqlserver",
            "azuresql", "mysql", "mariadb", "oracle", "db2", "sqlite", "duckdb", "clickhouse", "snowflake",
            "hana", "teradata", "firebird", "odbc"]
ALL_KINDS = DB_KINDS + ["mongodb", "folder", "share", "sftp", "ftp", "s3", "azure_blob", "http"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("ETLPLUS_SECRET_KEY", raising=False)
    return config.Project.init(tmp_path / "proj")


# ---------------------------------------------------------------- registry

def test_registry_has_every_kind():
    assert set(ALL_KINDS) == set(drivers.REGISTRY)
    for kind in ALL_KINDS:
        d = drivers.get(kind)
        assert d.kind == kind and d.label and d.fields, kind
        assert d.status in drivers.STATUS, kind
        assert d.roles and d.roles <= {"source", "target"}, kind
        assert d.category in ("database", "document", "folder", "remote"), kind
    assert drivers.get("mongodb").roles == frozenset({"source"})
    assert drivers.get("clickhouse").upsert is None
    assert "Temenos T24" in drivers.get("odbc").notes
    assert set(connections.KINDS) == set(ALL_KINDS)


def test_unknown_kind_is_a_plain_message():
    with pytest.raises(DriverError) as e:
        drivers.get("nosuch")
    assert "Known kinds" in str(e.value) and "postgres" in str(e.value)
    with pytest.raises(DriverError) as e:
        drivers.columns_sql("nosuch", "t")
    assert not isinstance(e.value, KeyError) and "nosuch" in str(e.value)
    with pytest.raises(DriverError):
        drivers.columns_sql("mongodb", "t")


def test_secret_and_endpoint_fields():
    for name in ("password", "secret_access_key", "sas_token", "account_key", "header_value", "private_key",
                 "passphrase"):
        assert name in drivers.SECRET_FIELD_NAMES
    for name in ("host", "port", "user", "url", "endpoint_url", "region", "bucket", "account_url", "container",
                 "path", "host_key_fingerprint"):
        assert name in connections.ENDPOINT_KEYS


def test_status_table_does_not_import(monkeypatch):
    drivers._STATUS_CACHE = None
    before = set(sys.modules)
    rows = drivers.status_table()
    assert {r["kind"] for r in rows} == set(ALL_KINDS)
    new = set(sys.modules) - before
    assert not any(m.split(".")[0] in ("snowflake", "hdbcli", "boto3", "azure", "pymongo", "clickhouse_connect")
                   for m in new)
    pg = next(r for r in rows if r["kind"] == "postgres")
    assert pg["install"] == "pip install etlplus[postgres]" and pg["needs"].startswith("Host")
    assert all(not f["secret"] or f["type"] == "password" for r in rows for f in r["fields"])
    rows[0]["kind"] = "changed"
    assert drivers.status_table()[0]["kind"] == "postgres"


def test_card_shape():
    c = drivers.card("postgres")
    for k in ("id", "group", "label", "icon", "needs", "installed", "install", "status", "kind", "format",
              "roles", "verify_note"):
        assert k in c
    assert c["group"] == "Databases" and drivers.card("sftp")["group"] == "Remote locations"


def test_missing_driver_names_the_extra(monkeypatch):
    monkeypatch.setattr(connections, "_import", lambda m: None)
    with pytest.raises(connections.ConnectionError_, match=r"pip install etlplus\[clickhouse\]"):
        connections.driver("clickhouse")
    with pytest.raises(connections.ConnectionError_, match=r"pip install etlplus\[postgres\]"):
        connections.driver("redshift")


# ---------------------------------------------------------------- type map (table 3.3)

T = {
    "postgres": ["varchar(40)", "text", "integer", "bigint", "numeric(18,2)", "double precision", "boolean",
                 "date", "time", "timestamp", "timestamptz", "bytea", "jsonb"],
    "redshift": ["varchar(40)", "text", "integer", "bigint", "numeric(18,2)", "double precision", "boolean",
                 "date", "time", "timestamp", "timestamptz", "varbyte", "super"],
    "sqlserver": ["nvarchar(40)", "nvarchar(max)", "int", "bigint", "decimal(18,2)", "float", "bit", "date",
                  "time(6)", "datetime2(6)", "datetimeoffset(6)", "varbinary(max)", "nvarchar(max)"],
    "mysql": ["varchar(40)", "longtext", "int", "bigint", "decimal(18,2)", "double", "tinyint(1)", "date",
              "time(6)", "datetime(6)", "datetime(6)", "longblob", "json"],
    "oracle": ["varchar2(40 char)", "clob", "number(10)", "number(19)", "number(18,2)", "binary_double",
               "number(1)", "date", "varchar2(15)", "timestamp(6)", "timestamp(6) with time zone", "blob", "clob"],
    "db2": ["varchar(40)", "clob", "integer", "bigint", "decimal(18,2)", "double", "boolean", "date", "time",
            "timestamp(6)", "timestamp(6)", "blob", "clob"],
    "sqlite": ["TEXT", "TEXT", "INTEGER", "INTEGER", "NUMERIC", "REAL", "INTEGER", "TEXT", "TEXT", "TEXT", "TEXT",
               "BLOB", "TEXT"],
    "duckdb": ["VARCHAR", "VARCHAR", "INTEGER", "BIGINT", "DECIMAL(18,2)", "DOUBLE", "BOOLEAN", "DATE", "TIME",
               "TIMESTAMP", "TIMESTAMPTZ", "BLOB", "JSON"],
    "clickhouse": ["String", "String", "Int32", "Int64", "Decimal(18,2)", "Float64", "Bool", "Date32", "String",
                   "DateTime64(6)", "DateTime64(6,'UTC')", "String", "String"],
    "snowflake": ["VARCHAR(40)", "VARCHAR", "NUMBER(10,0)", "NUMBER(19,0)", "NUMBER(18,2)", "FLOAT", "BOOLEAN",
                  "DATE", "TIME", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "BINARY", "VARIANT"],
    "hana": ["NVARCHAR(40)", "NCLOB", "INTEGER", "BIGINT", "DECIMAL(18,2)", "DOUBLE", "BOOLEAN", "DATE", "TIME",
             "TIMESTAMP", "TIMESTAMP", "BLOB", "NCLOB"],
    "teradata": ["VARCHAR(40) CHARACTER SET UNICODE", "CLOB", "INTEGER", "BIGINT", "DECIMAL(18,2)", "FLOAT",
                 "BYTEINT", "DATE", "TIME(6)", "TIMESTAMP(6)", "TIMESTAMP(6) WITH TIME ZONE", "BLOB", "JSON"],
    "firebird": ["VARCHAR(40)", "BLOB SUB_TYPE TEXT", "INTEGER", "BIGINT", "DECIMAL(18,2)", "DOUBLE PRECISION",
                 "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "BLOB", "BLOB SUB_TYPE TEXT"],
    "odbc": ["VARCHAR(40)", "VARCHAR(4000)", "INTEGER", "BIGINT", "DECIMAL(18,2)", "DOUBLE PRECISION", "SMALLINT",
             "DATE", "TIME", "TIMESTAMP", "TIMESTAMP", "VARBINARY(8000)", "VARCHAR(4000)"],
}
for _k, _fam in (("greenplum", "postgres"), ("cockroachdb", "postgres"), ("timescaledb", "postgres"),
                 ("yugabytedb", "postgres"), ("azuresql", "sqlserver"), ("mariadb", "mysql")):
    T[_k] = T[_fam]
GENERICS = ["string(40)", "text", "integer", "bigint", "decimal(18,2)", "double", "boolean", "date", "time",
            "timestamp", "timestamptz", "binary", "json"]


@pytest.mark.parametrize("kind,generic,native",
                         [(k, g, T[k][i]) for k in sorted(T) for i, g in enumerate(GENERICS)])
def test_native_type_matches_table(kind, generic, native):
    assert drivers.native_type(kind, generic) == native


@pytest.mark.parametrize("kind,generic,native", [
    ("sqlserver", "string(5000)", "nvarchar(max)"), ("mysql", "string(20000)", "longtext"),
    ("oracle", "string(4001)", "clob"), ("hana", "string(6000)", "NCLOB"),
    ("firebird", "string(9000)", "BLOB SUB_TYPE TEXT"), ("oracle", "decimal(40,2)", "number(38,0)"), ("oracle", "decimal(40,10)", "number(38,8)"),
    ("db2", "decimal(40,5)", "decimal(31,0)"), ("firebird", "decimal(20,2)", "DECIMAL(18,0)"),
    ("mysql", "decimal(70,2)", "decimal(65,0)"), ("postgres", "string", "varchar(255)"),
])
def test_native_type_limits(kind, generic, native):
    assert drivers.native_type(kind, generic) == native


def test_native_type_notes_and_errors():
    assert drivers.native_type_info("oracle", "decimal(40,2)")[1]
    assert drivers.native_type_info("mysql", "timestamptz")[1]
    assert drivers.native_type_info("mariadb", "timestamptz")[1]
    assert drivers.native_type_info("oracle", "time")[1]
    assert drivers.native_type_info("postgres", "integer")[1] is None
    for bad in ("varchar(10)", "string(1,2)", "integer(4)", "decimal(2,5)", ""):
        with pytest.raises(DriverError):
            drivers.native_type("postgres", bad)
    with pytest.raises(DriverError):
        drivers.native_type("mongodb", "text")
    assert [r["generic"] for r in drivers.type_table("postgres")][:2] == ["string(n)", "text"]


LOSSY_BACK = {("mysql", "timestamptz"), ("mariadb", "timestamptz"), ("db2", "timestamptz"), ("hana", "timestamptz"),
              ("odbc", "timestamptz"), ("oracle", "time"), ("oracle", "json"), ("oracle", "text"), ("db2", "json"),
              ("sqlserver", "json"), ("azuresql", "json"), ("teradata", "boolean"), ("odbc", "boolean"),
              ("odbc", "text"), ("odbc", "json"), ("odbc", "binary"), ("hana", "json"), ("firebird", "json"),
              ("clickhouse", "string"), ("clickhouse", "time"), ("clickhouse", "binary"), ("clickhouse", "json"), ("clickhouse", "text"),
              ("duckdb", "string"), ("sqlite", "string"), ("snowflake", "text"), ("teradata", "json")}


@pytest.mark.parametrize("kind", sorted(T))
def test_generic_type_round_trip(kind):
    for g in GENERICS:
        base = g.split("(")[0]
        back = drivers.generic_type(kind, drivers.native_type(kind, g))
        if (kind, base) in LOSSY_BACK or kind == "sqlite":
            continue
        assert back.split("(")[0] == base, (kind, g, back)


def test_generic_type_samples():
    assert drivers.generic_type("postgres", "character varying(40)") == "string(40)"
    assert drivers.generic_type("postgres", "timestamp with time zone") == "timestamptz"
    assert drivers.generic_type("mysql", "tinyint(1)") == "boolean"
    assert drivers.generic_type("oracle", "NUMBER(10,0)") == "integer"
    assert drivers.generic_type("clickhouse", "Nullable(Int64)") == "bigint"
    assert drivers.generic_type("clickhouse", "Int8") == "integer"
    assert drivers.generic_type("postgres", "int8") == "bigint"
    assert drivers.generic_type("sqlserver", "nvarchar(max)") == "text"
    assert drivers.generic_type("postgres", "numeric(12,3)") == "decimal(12,3)"


def test_arrow_to_generic():
    assert drivers.arrow_to_generic(pa.string()) == "text"
    assert drivers.arrow_to_generic(pa.int32()) == "integer"
    assert drivers.arrow_to_generic(pa.int64()) == "bigint"
    assert drivers.arrow_to_generic(pa.decimal128(18, 3)) == "decimal(18,3)"
    assert drivers.arrow_to_generic(pa.timestamp("us", tz="UTC")) == "timestamptz"
    assert drivers.arrow_to_generic(pa.timestamp("us")) == "timestamp"
    assert drivers.arrow_to_generic(pa.list_(pa.int64())) == "json"
    assert drivers.arrow_to_generic(pa.bool_()) == "boolean"


# ---------------------------------------------------------------- quoting and limits

def test_quote_ident_folding_and_escaping():
    assert drivers.quote_ident("postgres", "CUSTOMERS") == '"customers"'
    assert drivers.quote_ident("redshift", "Mixed") == '"Mixed"'
    assert drivers.quote_ident("oracle", "customers") == '"CUSTOMERS"'
    assert drivers.quote_ident("snowflake", "customers") == '"CUSTOMERS"'
    assert drivers.quote_ident("db2", "t") == '"T"'
    assert drivers.quote_ident("firebird", "t") == '"T"'
    assert drivers.quote_ident("hana", "t") == '"T"'
    assert drivers.quote_ident("oracle", '"lower"') == '"lower"'
    assert drivers.quote_ident("sqlserver", "a]b") == "[a]]b]"
    assert drivers.quote_ident("azuresql", "[a]]b]") == "[a]]b]"
    assert drivers.quote_ident("mysql", "a`b") == "`a``b`"
    assert drivers.quote_ident("mariadb", "a`b") == "`a``b`"
    assert drivers.quote_ident("clickhouse", "a\\b`c") == "`a\\\\b``c`"
    assert drivers.quote_ident("duckdb", 'a"b') == '"a""b"'
    assert drivers.quote_ident("teradata", "Mixed") == '"Mixed"'
    assert drivers.quote_table("oracle", "core.customers") == '"CORE"."CUSTOMERS"'
    assert drivers.quote_table("sqlserver", "db.dbo.my table") == "[db].[dbo].[my table]"
    assert drivers.quote_table("postgres", 'public."a.b"') == '"public"."a.b"'
    with pytest.raises(DriverError):
        drivers.quote_table("postgres", "a.b.c.d")
    with pytest.raises(DriverError):
        drivers.quote_table("postgres", "a..b")


def test_quote_ident_refusals():
    for bad in ("", "a\x00b", "a\nb", "a\x7fb"):
        with pytest.raises(DriverError):
            drivers.quote_ident("postgres", bad)
    assert drivers.quote_ident("duckdb", "x" * 255)
    with pytest.raises(DriverError):
        drivers.quote_ident("sqlserver", "x" * 129)
    with pytest.raises(DriverError):
        drivers.quote_ident("firebird", "x" * 64)
    with pytest.raises(DriverError):
        drivers.quote_ident("hana", "x" * 128)
    assert drivers.quote_ident("snowflake", "X" * 255)


HOSTILE = ['a"b', "a`b", "a]b", "a\\b", "a;DROP TABLE t", "a' OR '1'='1", "a]; DELETE FROM t; --",
           'a"; DROP TABLE t; --', "a`; DROP TABLE t; -- `", "a\\`; DROP TABLE t"]


@pytest.mark.parametrize("kind", DB_KINDS)
def test_hostile_names_stay_one_identifier(kind):
    for name in HOSTILE:
        ident = drivers.quote_ident(kind, name)
        guard.check_select(f"SELECT {ident} FROM t", kind)
        tbl = drivers.quote_table(kind, f's."{name.replace(chr(34), chr(34) * 2)}"')
        guard.check_select(f"SELECT 1 FROM {tbl}", kind)
    with pytest.raises(DriverError):
        drivers.quote_ident(kind, "a\x00")


def test_placeholders():
    assert drivers.placeholder("postgres", 0) == "%s"
    assert drivers.placeholder("mariadb", 1) == "%s"
    assert drivers.placeholder("oracle", 1) == ":2"
    assert drivers.placeholder("sqlserver", 0) == "?"
    assert drivers.placeholder("clickhouse", 2) == "%(p2)s"
    assert drivers.bind_params("clickhouse", [1, 2]) == {"p0": 1, "p1": 2}
    assert drivers.bind_params("postgres", (1,)) == [1]


def test_select_sql_limit_styles():
    assert drivers.select_sql("postgres", "t", limit=5) == 'SELECT * FROM "t" LIMIT 5'
    assert drivers.select_sql("sqlserver", "dbo.t", ["a"], limit=5) == "SELECT TOP 5 [a] FROM [dbo].[t]"
    assert drivers.select_sql("oracle", "t", limit=5) == 'SELECT * FROM "T" FETCH FIRST 5 ROWS ONLY'
    assert drivers.select_sql("db2", "t", limit=5) == 'SELECT * FROM "T" FETCH FIRST 5 ROWS ONLY'
    assert drivers.select_sql("firebird", "t", limit=5) == 'SELECT FIRST 5 * FROM "T"'
    assert drivers.select_sql("teradata", "t", limit=5) == 'SELECT * FROM "t" SAMPLE 5'
    assert drivers.select_sql("odbc", "t", limit=5) == 'SELECT * FROM "t"'
    assert drivers.select_sql("clickhouse", "t", where="x = 1", limit=2) == "SELECT * FROM `t` WHERE x = 1 LIMIT 2"
    assert drivers.limit_sql("odbc", "SELECT 1", 3) is None
    assert drivers.limit_sql("teradata", "SELECT 1", 3) == "SELECT TOP 3 * FROM (SELECT 1) q"
    assert connections.limit_sql("odbc", "SELECT 1", 3) == "SELECT 1"


@pytest.mark.parametrize("kind", DB_KINDS)
def test_catalog_sql_is_guarded(kind):
    for schema in (None, "s"):
        sql, params = drivers.tables_sql(kind, schema)
        guard.check_select(sql, kind)
    for t in ("t", "s.t"):
        sql, params = drivers.columns_sql(kind, t)
        guard.check_select(sql, kind)
        assert params[0] == "t"
    v = drivers.version_sql(kind)
    if v:
        guard.check_select(v, kind)


# ---------------------------------------------------------------- read-only sessions (fake DB-API)

class Cur:
    def __init__(self, log, rows=None, desc=None):
        self.log = log
        self.rows = list(rows or [])
        self.description = desc
        self.arraysize = 1

    def execute(self, sql, params=None):
        self.log.append(sql if params is None else (sql, params))
        return self

    def fetchmany(self, n=1):
        out, self.rows = self.rows[:n], self.rows[n:]
        return out

    def fetchall(self):
        out, self.rows = self.rows, []
        return out

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self, kw=None, rows=None, desc=None):
        self.kw = kw or {}
        self.log = []
        self.read_only = False
        self.autocommit = None
        self._rows = rows
        self._desc = desc

    def cursor(self, *a, **k):
        return Cur(self.log, self._rows, self._desc)

    def rollback(self):
        self.log.append("ROLLBACK")

    def close(self):
        pass


class FakeModule:
    """One fake DB-API module for every kind: records connect arguments."""

    def __init__(self, rows=None, desc=None):
        self.calls = []
        self.rows = rows
        self.desc = desc
        self.odbc = ["ODBC Driver 17 for SQL Server"]
        self.cursors = types.SimpleNamespace(SSCursor=object)

    def connect(self, *a, **kw):
        self.calls.append((a, kw))
        return Conn(kw, self.rows, self.desc)

    def drivers(self):
        return list(self.odbc)


@pytest.fixture
def fake(monkeypatch):
    mod = FakeModule()
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    return mod


def _save(project, cid, spec, password="pw-123"):
    project.save_connection(cid, spec)
    if password:
        secrets.set(project, cid, {"password": password})


@pytest.mark.parametrize("kind,expect", [
    ("mysql", ["SET SESSION TRANSACTION READ ONLY"]),
    ("mariadb", ["SET SESSION TRANSACTION READ ONLY"]),
    ("oracle", ["SET TRANSACTION READ ONLY"]),
    ("hana", ["SET TRANSACTION READ ONLY"]),
    ("redshift", ["START TRANSACTION READ ONLY"]),
    ("cockroachdb", ["SET default_transaction_read_only = on"]),
])
def test_source_connect_issues_read_only_statement(project, fake, kind, expect):
    _save(project, "src", {"kind": kind, "host": "db.example.test", "user": "u", "database": "d"})
    conn = connections.connect(project, "src")
    assert conn.log == expect
    _save(project, "tgt", {"kind": kind, "host": "db.example.test", "user": "u", "database": "d", "role": "both"})
    assert connections.connect(project, "tgt").log == []


@pytest.mark.parametrize("kind", ["postgres", "timescaledb", "greenplum", "yugabytedb"])
def test_postgres_family_read_only_attribute(project, fake, kind):
    _save(project, "pg", {"kind": kind, "host": "db.example.test", "user": "u", "database": "d", "sslmode": "require"})
    conn = connections.connect(project, "pg")
    assert conn.read_only is True and conn.log == []
    kw = fake.calls[-1][1]
    assert kw["password"] == "pw-123" and kw["sslmode"] == "require"


def test_sqlserver_application_intent_and_same_driver(project, fake):
    _save(project, "ms", {"kind": "sqlserver", "host": "db.example.test", "user": "u", "database": "d"})
    connections.connect(project, "ms")
    read_cs = fake.calls[-1][0][0]
    assert "ApplicationIntent=ReadOnly" in read_cs and "DRIVER={ODBC Driver 17 for SQL Server}" in read_cs
    _save(project, "mt", {"kind": "sqlserver", "host": "db.example.test", "user": "u", "database": "d",
                          "role": "target"})
    drivers.connect(project, "mt", role="target")
    write_cs = fake.calls[-1][0][0]
    assert "ApplicationIntent" not in write_cs and "DRIVER={ODBC Driver 17 for SQL Server}" in write_cs


def test_azuresql_encrypt_fixed(project, fake):
    _save(project, "az", {"kind": "azuresql", "host": "x.database.example.test", "user": "u", "database": "d",
                          "authentication": "ActiveDirectoryPassword", "options": {"Encrypt": "no"}})
    connections.connect(project, "az")
    cs = fake.calls[-1][0][0]
    assert "Encrypt=yes" in cs and "Encrypt=no" not in cs and "Authentication=ActiveDirectoryPassword" in cs


def test_sqlserver_trust_server_certificate_field():
    s = connections.sqlserver_conn_str({"host": "h", "trust_server_certificate": False}, None, True,
                                       installed=["ODBC Driver 18 for SQL Server"])
    assert "TrustServerCertificate=no" in s
    s = connections.sqlserver_conn_str({"host": "h", "encrypt": "strict"}, None, True,
                                       installed=["ODBC Driver 18 for SQL Server"])
    assert "Encrypt=strict" in s and "TrustServerCertificate" not in s


def test_odbc_readonly_attribute(project, fake):
    _save(project, "ob", {"kind": "odbc", "dsn": "Warehouse", "user": "u"})
    connections.connect(project, "ob")
    args, kw = fake.calls[-1]
    assert kw.get("readonly") is True and "DSN=Warehouse" in args[0] and "PWD=pw-123" in args[0]
    _save(project, "ow", {"kind": "odbc", "dsn": "Warehouse", "role": "target"})
    drivers.connect(project, "ow", role="target")
    assert "readonly" not in fake.calls[-1][1]


def test_clickhouse_readonly_setting(project, monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kw):
            seen.update(kw)
            self.queries = []

        def query(self, sql, **kw):
            self.queries.append((sql, kw))
            return types.SimpleNamespace(column_names=["a"], column_types=["Int32"], result_rows=[(1,), (2,)])

        def close(self):
            pass

    cc = types.SimpleNamespace(get_client=lambda **kw: Client(**kw))
    monkeypatch.setattr(connections, "driver", lambda kind: cc)
    _save(project, "ch", {"kind": "clickhouse", "host": "ch.example.test", "user": "u", "database": "d"})
    conn = connections.connect(project, "ch")
    assert seen["settings"] == {"readonly": 1} and seen["port"] == 8123 and seen["password"] == "pw-123"
    cols, rows = connections.fetch(conn, "clickhouse", "SELECT a FROM t WHERE b = %(p0)s", [5])
    assert cols == ["a"] and rows == [[1], [2]]
    sql, kw = conn.client.queries[-1]
    assert kw["settings"] == {"readonly": 1} and kw["parameters"] == {"p0": 5}


def test_mongodb_read_preference_and_source_only(project, monkeypatch):
    seen = {}

    class Client(dict):
        def __init__(self, host, **kw):
            seen["host"] = host
            seen.update(kw)

        def __getitem__(self, k):
            return types.SimpleNamespace(list_collection_names=lambda: ["b", "a"])

    monkeypatch.setattr(connections, "driver", lambda kind: types.SimpleNamespace(MongoClient=Client))
    _save(project, "mg", {"kind": "mongodb", "uri": "m1.example.test:27017", "database": "d", "user": "u"})
    db = connections.connect(project, "mg")
    assert seen["readPreference"] == "secondaryPreferred" and seen["password"] == "pw-123"
    assert db.collections() == ["a", "b"]
    with pytest.raises(DriverError):
        drivers.connect(project, "mg", role="target")


def test_target_role_refused_for_a_source(project, fake):
    _save(project, "s", {"kind": "mysql", "host": "h", "user": "u"})
    with pytest.raises(DriverError, match="read-only"):
        drivers.connect(project, "s", role="target")
    with pytest.raises(DriverError, match="not a database"):
        drivers.connect(project, "x", role="source", spec={"kind": "folder", "path": "x"})


# ---------------------------------------------------------------- simulated reads through sources

def test_mysql_read_path_through_fake(project, monkeypatch):
    mod = FakeModule(rows=[(1, "a"), (2, "b"), (3, "c")], desc=[("id",), ("name",)])
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    _save(project, "my", {"kind": "mariadb", "host": "h", "user": "u", "database": "d"})
    job = {"name": "j", "source": {"connection": "my", "table": "db.t", "chunk_rows": 2,
                                   "incremental": {"column": "d"}}}
    t = pa.Table.from_batches(list(sources.read(project, job, ("2026-10-01", "2026-10-02"), None)))
    assert t.column("id").to_pylist() == [1, 2, 3]


def test_teradata_reads_get_locking_prefix(project, monkeypatch):
    mod = FakeModule(rows=[(1,)], desc=[("a",)])
    conns = []

    def connect(*a, **kw):
        c = Conn(kw, [(1,)], [("a",)])
        conns.append(c)
        return c

    mod.connect = connect
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    _save(project, "td", {"kind": "teradata", "host": "h", "user": "u"})
    job = {"name": "j", "source": {"connection": "td", "query": "SELECT a FROM t"}}
    list(sources.read(project, job, None, None))
    assert conns[-1].log[0] == "LOCKING ROW FOR ACCESS SELECT a FROM t"
    assert guard.check_select("LOCKING ROW FOR ACCESS SELECT a FROM t", "teradata").startswith("LOCKING")


def test_mongo_read_flattens_and_windows(project, monkeypatch):
    calls = {}
    docs = [{"_id": 1, "a": {"b": 2}, "tags": [1, 2]}, {"_id": 2, "a": {"b": 3}, "tags": []}]

    class Coll:
        def find(self, flt, **kw):
            calls["filter"] = flt
            return iter(docs)

        def aggregate(self, pipeline, **kw):
            calls["pipeline"] = pipeline
            return iter(docs)

    class Client:
        def __init__(self, host, **kw):
            pass

        def __getitem__(self, k):
            return {"c": Coll()}

        def close(self):
            pass

    monkeypatch.setattr(connections, "driver", lambda kind: types.SimpleNamespace(MongoClient=Client))
    project.save_connection("mg", {"kind": "mongodb", "uri": "m.example.test", "database": "d"})
    job = {"name": "j", "source": {"connection": "mg", "table": "c", "query": '{"filter": {"x": {"$gt": 1}}}',
                                   "incremental": {"column": "at"}}}
    t = pa.Table.from_batches(list(sources.read(project, job, ("2026-10-01", "2026-10-02"), None)))
    assert t.column_names == ["_id", "a.b", "tags"]
    assert t.column("tags").to_pylist() == ["[1, 2]", "[]"]
    assert "$and" in calls["filter"] and "$gte" in str(calls["filter"])
    job["source"]["query"] = '{"pipeline": [{"$match": {"x": 1}}]}'
    list(sources.read(project, job, None, None))
    assert calls["pipeline"] == [{"$match": {"x": 1}}]
    job["source"]["query"] = '{"pipeline": [{"$out": "x"}]}'
    with pytest.raises(guard.GuardError):
        list(sources.read(project, job, None, None))


# ---------------------------------------------------------------- ODBC safety and the save split

@pytest.mark.parametrize("bad", ["/usr/lib/libevil.so", "C:\\evil\\x.dll", "evil.dylib", "..\\x", "libx.so.1"])
def test_odbc_driver_by_path_refused(bad):
    with pytest.raises(DriverError, match="by name"):
        drivers.mssql.check_driver_name(bad)
    with pytest.raises(DriverError, match="by name"):
        connections.prepare_save({"kind": "odbc", "connection_string": f"DRIVER={{{bad}}};SERVER=x"})
    with pytest.raises(DriverError, match="by name"):
        connections.prepare_save({"kind": "sqlserver", "host": "h", "driver": bad})
    assert connections.prepare_save({"kind": "odbc", "connection_string": "DSN=Mine;UID=u"})["spec"]


def test_connection_string_splitter():
    from etlplus.drivers import odbc
    cs = "Driver={Some Driver};Server=h;PWD={p;w=}}d};Uid=u;Token=abc"
    parts = odbc.split(cs)
    assert ("PWD", "p;w=}d") in parts and ("Server", "h") in parts and ("Token", "abc") in parts
    kept, secret = odbc.separate(cs)
    assert "p;w" not in kept and "abc" not in kept and "PWD" not in kept
    assert secret == {"odbc:PWD": "p;w=}d", "odbc:Token": "abc"}
    built = odbc.build({"connection_string": kept, "_secrets": secret}, None)
    assert dict(odbc.split(built))["PWD"] == "p;w=}d"
    for bad in ("a=b;c", "PWD={x", "=x"):
        with pytest.raises(DriverError):
            odbc.split(bad)


def test_prepare_save_moves_secrets(project):
    out = connections.prepare_save({"kind": "odbc", "connection_string": "DSN=x;Password={s;}}e}", "user": "u"})
    assert out["spec"]["connection_string"] == "DSN=x;" and out["secrets"] == {"odbc:Password": "s;}e"}
    out = connections.prepare_save({"kind": "mongodb", "uri": "mongodb://alice:p%40ss@m.example.test/db",
                                    "database": "db"})
    assert out["spec"]["uri"] == "mongodb://m.example.test/db" and out["spec"]["user"] == "alice"
    assert out["secrets"] == {"password": "p@ss"}
    out = connections.prepare_save({"kind": "s3", "bucket": "b", "access_key_id": "AK", "secret_access_key": "SK",
                                    "options": {"client_secret": "x"}})
    assert out["secrets"] == {"secret_access_key": "SK"} and "client_secret" not in out["spec"]["options"]
    with pytest.raises(DriverError, match="SAS token in its own field"):
        connections.prepare_save({"kind": "azure_blob", "container": "c",
                                  "account_url": "https://acct.blob.example.test/?sv=1&sig=abc"})
    saved = {"kind": "sftp", "host": "a.example.test", "user": "u", "host_key_fingerprint": "SHA256:" + "A" * 43}
    assert connections.prepare_save(dict(saved), saved)["clear"] is False
    assert connections.prepare_save(dict(saved, host="b.example.test"), saved)["clear"] is True


def test_public_spec_flags_without_values(project):
    project.save_connection("s3", {"kind": "s3", "bucket": "b", "access_key_id": "AK"})
    secrets.update(project, "s3", {"secret_access_key": "SK-planted-777"})
    out = connections.public_spec(project, "s3")
    assert out["secret_access_key_set"] is True and "SK-planted-777" not in str(out)
    secrets.update(project, "s3", {"x": "1"}, clear=True)
    assert secrets.get(project, "s3") == {"x": "1"}
