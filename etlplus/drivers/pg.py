"""PostgreSQL family: postgres, redshift, greenplum, cockroachdb, timescaledb, yugabytedb (psycopg 3)."""

from __future__ import annotations

import re

from ._common import TIMEOUT, driver_options, module, options, port, run, value


def connect(spec, password, role, project=None):
    kind = spec.get("kind") or "postgres"
    psycopg = module(kind)
    kw = {"host": spec.get("host") or "localhost", "port": port(spec, kind), "dbname": spec.get("database"),
          "user": spec.get("user"), "connect_timeout": TIMEOUT, "application_name": "etlplus"}
    if password:
        kw["password"] = password
    sslmode = value(spec, "sslmode")
    if sslmode:
        kw["sslmode"] = sslmode
    opts = driver_options(spec)
    pg_opts = []
    tz = (getattr(project, "settings", None) or {}).get("timezone")
    if tz and "options" not in opts and re.match(r"^[A-Za-z0-9_+/-]+$", str(tz)) and kind != "redshift":
        # a date bound compared with a timestamptz column means midnight in the project zone
        pg_opts.append(f"-c TimeZone={tz}")
    st = options(spec).get("statement_timeout")
    if st not in (None, "") and role == "source":
        if not re.match(r"^\d{1,9}$", str(st)):
            raise ValueError("options.statement_timeout is a number of milliseconds.")
        pg_opts.append(f"-c statement_timeout={int(st)}")
    if pg_opts and "options" not in opts:
        kw["options"] = " ".join(pg_opts)
    kw.update(opts)
    conn = psycopg.connect(**{k: v for k, v in kw.items() if v is not None})
    if role == "source":
        if kind == "redshift":
            # Redshift ignores the session flag: an explicit read-only transaction holds the reads
            conn.autocommit = True
            run(conn, "START TRANSACTION READ ONLY")
        elif kind == "cockroachdb":
            conn.autocommit = True
            run(conn, "SET default_transaction_read_only = on")
            conn.autocommit = False
        else:
            conn.read_only = True
    else:
        try:
            conn.autocommit = False
        except Exception:
            pass
    return conn
