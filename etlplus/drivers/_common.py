"""Small helpers shared by the family modules."""

from __future__ import annotations

TIMEOUT = 15

# keys in options{} that etl+ reads itself and never passes to a driver
ETL_OPTIONS = {"allow_after_actions", "allow_plain", "allow_http", "allow_private", "use_default_credentials",
               "max_mb", "statement_timeout", "dsn", "bulk"}


def module(kind):
    """The DB-API module for a kind, imported now (connections.driver is looked up at call time,
    so tests can replace it)."""
    from etlplus import connections
    return connections.driver(kind)


def options(spec) -> dict:
    return dict(spec.get("options") or {})


def driver_options(spec) -> dict:
    return {k: v for k, v in options(spec).items() if k not in ETL_OPTIONS}


def value(spec, key, default=None):
    """A field from the spec, or from options{} (v1 kept some there)."""
    v = spec.get(key)
    if v in (None, ""):
        v = options(spec).get(key)
    return default if v in (None, "") else v


def port(spec, kind):
    from etlplus import drivers
    p = spec.get("port") or drivers.get(kind).default_port
    return int(p) if p not in (None, "") else None


def truthy(v, default=False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def secrets(spec) -> dict:
    return dict(spec.get("_secrets") or {})


def run(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
    finally:
        try:
            cur.close()
        except Exception:
            pass
