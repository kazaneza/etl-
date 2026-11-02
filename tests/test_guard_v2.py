"""The guard's v2 dialects, CREATE TABLE check and MongoDB check (SPEC_V2 sections 3.4, 3.2)."""

import pytest

from etlplus import guard
from etlplus.guard import GuardError, check_create_table, check_mongo, check_select


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT 'a\\\\b' FROM t", "mysql"),
    ("SELECT 'a\\\\b' FROM t", "mariadb"),
    ("SELECT [order], [from] FROM [dbo].[t]", "sqlserver"),
    ("SELECT [order] FROM [dbo].[t]", "azuresql"),
    ("SELECT $$ drop table x; $$ AS body", "postgres"),
    ("SELECT $tag$ delete $tag$ AS body", "redshift"),
    ("SELECT $$x$$", "cockroachdb"),
    ("SELECT arr[1] FROM t", "postgres"),
    ("SELECT `a\\`b` FROM t", "clickhouse"),
    ("LOCKING ROW FOR ACCESS SELECT a FROM t", "teradata"),
    ("locking row for access select a from t", "teradata"),
    ("SELECT TRIM(RDB$RELATION_NAME) FROM RDB$RELATIONS", "firebird"),
    ("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", "db2"),
    ("SELECT FIRST 5 * FROM t", "firebird"),
])
def test_dialect_allows(sql, dialect):
    assert check_select(sql, dialect)


@pytest.mark.parametrize("sql,dialect", [
    # a backslash ends a string differently on MySQL / ClickHouse: both readings must pass
    ("SELECT '\\'' ; DELETE FROM t; -- '", "mysql"),
    ("SELECT '\\'' ; DELETE FROM t; -- '", "mariadb"),
    ("SELECT '\\'' ; DELETE FROM t; -- '", "clickhouse"),
    ("SELECT '\\'' ; DELETE FROM t; -- '", "postgres"),
    # [x] is a name only on SQL Server; elsewhere it is code
    ("SELECT [delete] FROM t", "postgres"),
    ("SELECT [delete] FROM t", "mysql"),
    ("SELECT [xp_cmdshell]('dir')", "sqlserver"),
    # $$ is a string only on the postgres family
    ("SELECT $$ drop table x; $$ AS body", "mysql"),
    ("SELECT $$ drop table x $$ AS body", "sqlite"),
    ("SELECT $$ drop table x $$ AS body", "oracle"),
    # a backtick reads differently on ClickHouse: with no dialect both readings are checked
    ("SELECT `a\\` FROM t; DROP TABLE t; -- `", None),
    ("SELECT `a\\` FROM t; DROP TABLE t; -- `", "mysql"),
    # LOCKING is only the Teradata prefix, and only in front
    ("LOCKING ROW FOR ACCESS SELECT a FROM t", "postgres"),
    ("LOCKING ROW FOR ACCESS DELETE FROM t", "teradata"),
    ("LOCKING TABLE t FOR WRITE SELECT 1", "teradata"),
    # unknown dialect: the strict reading
    ("SELECT ARRAY[pg_sleep(1)]", "nosuchkind"),
])
def test_dialect_refuses(sql, dialect):
    with pytest.raises(GuardError):
        check_select(sql, dialect)


def test_family_mapping():
    assert guard.dialect_family("azuresql") == "sqlserver"
    assert guard.dialect_family("mariadb") == "mysql"
    assert guard.dialect_family("yugabytedb") == "postgres"
    assert guard.dialect_family("postgres") == "postgres"
    assert guard.dialect_family("nosuch") is None
    assert guard.dialect_family(None) is None


@pytest.mark.parametrize("sql,dialect", [
    ('CREATE TABLE "s"."t" ("a" integer NOT NULL, "b" varchar(40) DEFAULT \'x;y\', PRIMARY KEY ("a"))', "postgres"),
    ("CREATE TABLE IF NOT EXISTS t (a INTEGER)", "duckdb"),
    ("CREATE TABLE [dbo].[t] ([a] int)", "sqlserver"),
    ('CREATE TABLE "t" ("a" VARCHAR(10) CHARACTER SET UNICODE) UNIQUE PRIMARY INDEX ("a")', "teradata"),
    ("CREATE TABLE `t` (`a` Nullable(Int32), `d` DateTime64(6,'UTC')) ENGINE = MergeTree ORDER BY tuple()",
     "clickhouse"),
    ('CREATE TABLE "T" ("A" number(10), "B" timestamp(6) with time zone DEFAULT SYSTIMESTAMP);', "oracle"),
    ("CREATE TABLE t (a INTEGER, b TEXT DEFAULT 'DROP TABLE x') -- a comment", "sqlite"),
])
def test_create_table_allows(sql, dialect):
    out = check_create_table(sql, dialect)
    assert out.startswith("CREATE TABLE") and not out.endswith(";")


@pytest.mark.parametrize("sql,dialect", [
    ("CREATE TABLE x AS SELECT * FROM t", "postgres"),
    ("CREATE TABLE x (a int) AS SELECT 1", "mysql"),
    ("CREATE TABLE x (a int); DROP TABLE y", "postgres"),
    ("CREATE TABLE x (a int); CREATE TABLE y (b int)", "postgres"),
    ("DROP TABLE x", "postgres"),
    ("CREATE VIEW v AS SELECT 1", "postgres"),
    ("CREATE TABLE x (a int DEFAULT (SELECT 1))", "postgres"),
    ("CREATE TABLE x (a int DEFAULT pg_sleep(5))", "postgres"),
    ("CREATE TABLE x (a int) INSERT INTO y VALUES (1)", "postgres"),
    ("CREATE TABLE x LIKE y", "mysql"),
    ("CREATE TABLE x (a int SET DEFAULT 1)", "postgres"),
    ("CREATE TABLE x (a varchar(5) DEFAULT '\\'); DROP TABLE y; -- ')", "mysql"),
    ("", "postgres"),
    ("CREATE TABLE x (a int)\x00", "postgres"),
])
def test_create_table_refuses(sql, dialect):
    with pytest.raises(GuardError):
        check_create_table(sql, dialect)


@pytest.mark.parametrize("obj", [
    {"filter": {"$where": "this.a > 1"}},
    {"pipeline": [{"$out": "other"}]},
    {"pipeline": [{"$merge": {"into": "other"}}]},
    {"pipeline": [{"$group": {"_id": None, "x": {"$accumulator": {}}}}]},
    {"filter": {"$expr": {"$function": {"body": "return 1", "args": [], "lang": "js"}}}},
    {"pipeline": [{"$currentOp": {}}]},
    {"pipeline": [{"$lookup": {"from": "secrets"}}]},
    {"filter": {"a": {"$nosuch": 1}}},
])
def test_check_mongo_refuses(obj):
    with pytest.raises(GuardError):
        check_mongo(obj)


def test_check_mongo_allows_reads():
    check_mongo({"filter": {"a": {"$gte": 1, "$lt": 5}, "$or": [{"b": "x"}, {"c": {"$in": [1, 2]}}]},
                 "projection": {"a": 1, "_id": 0}})
    check_mongo({"pipeline": [{"$match": {"a": 1}}, {"$group": {"_id": "$b", "n": {"$sum": 1}}},
                              {"$sort": {"n": -1}}, {"$limit": 10}]})
    check_mongo({"filter": {"note": "$where is only text here"}})
    deep = {"a": 1}
    for _ in range(80):
        deep = {"$and": [deep]}
    with pytest.raises(GuardError):
        check_mongo(deep)
