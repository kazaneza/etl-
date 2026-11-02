"""IBM Db2 through ibm_db_dbi. Read-only rests on the SQL guard: use a read-only user."""

from __future__ import annotations

from ._common import module, port
from .mssql import odbc_value


def conn_str(spec, password) -> str:
    parts = [("DATABASE", odbc_value(spec.get("database") or "")),
             ("HOSTNAME", odbc_value(spec.get("host") or "localhost")),
             ("PORT", str(port(spec, "db2"))), ("PROTOCOL", "TCPIP")]
    if spec.get("user"):
        parts.append(("UID", odbc_value(spec["user"])))
        parts.append(("PWD", odbc_value(password or "")))
    if (spec.get("security") or "none").upper() == "SSL":
        parts.append(("SECURITY", "SSL"))
    return ";".join(f"{k}={v}" for k, v in parts) + ";"


def connect(spec, password, role, project=None):
    dbi = module("db2")
    conn = dbi.connect(conn_str(spec, password), "", "")
    try:
        conn.set_autocommit(False)
    except Exception:
        pass
    return conn
