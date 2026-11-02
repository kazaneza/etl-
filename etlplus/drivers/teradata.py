"""Teradata through teradatasql. Reads are guarded, then prefixed LOCKING ROW FOR ACCESS."""

from __future__ import annotations

import json

from ._common import module, port

LOCKING = "LOCKING ROW FOR ACCESS "


def read_sql(sql: str) -> str:
    """The prefix is added after guard.check_select has passed the SQL."""
    return sql if sql.lstrip().upper().startswith("LOCKING ") else LOCKING + sql


def connect(spec, password, role, project=None):
    td = module("teradata")
    params = {"host": spec.get("host") or "localhost", "dbs_port": str(port(spec, "teradata")),
              "user": spec.get("user") or "", "password": password or "",
              "logmech": spec.get("logmech") or "TD2", "tmode": "ANSI"}
    if spec.get("database"):
        params["database"] = spec["database"]
    conn = td.connect(json.dumps(params))
    try:
        conn.autocommit = False
    except Exception:
        pass
    return conn
