"""SAP HANA through hdbcli. A source session runs SET TRANSACTION READ ONLY."""

from __future__ import annotations

from ._common import module, port, run, truthy


def connect(spec, password, role, project=None):
    dbapi = module("hana")
    kw = {"address": spec.get("host") or "localhost", "port": port(spec, "hana"), "user": spec.get("user"),
          "password": password or "", "encrypt": truthy(spec.get("encrypt"), True)}
    if spec.get("database"):
        kw["databaseName"] = spec["database"]
    conn = dbapi.connect(**kw)
    try:
        conn.setautocommit(False)
    except Exception:
        pass
    if role == "source":
        run(conn, "SET TRANSACTION READ ONLY")
    return conn
