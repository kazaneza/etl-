"""Oracle through python-oracledb in thin mode (no Oracle client needed)."""

from __future__ import annotations

import re

from ._common import TIMEOUT, driver_options, module, options, port, run

_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def dsn(spec) -> str:
    from etlplus.drivers import DriverError
    explicit = options(spec).get("dsn")
    if explicit:
        return str(explicit)
    host = str(spec.get("host") or "localhost")
    db = str(spec.get("database") or "")
    for what, v in (("host", host), ("service name or SID", db)):
        if v and not _SAFE.match(v):
            raise DriverError(f"The {what} '{v}' holds characters a connect descriptor cannot take.")
    p = port(spec, "oracle")
    if (spec.get("connect_by") or "service_name") == "sid":
        return (f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={p}))"
                f"(CONNECT_DATA=(SID={db})))")
    return f"{host}:{p}/{db}"


def connect(spec, password, role, project=None):
    oracledb = module("oracle")
    opts = driver_options(spec)
    conn = oracledb.connect(user=spec.get("user"), password=password, dsn=dsn(spec),
                            tcp_connect_timeout=TIMEOUT, **opts)
    if role == "source":
        run(conn, "SET TRANSACTION READ ONLY")
    else:
        try:
            conn.autocommit = False
        except Exception:
            pass
    return conn
