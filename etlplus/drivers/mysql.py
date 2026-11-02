"""MySQL and MariaDB through PyMySQL."""

from __future__ import annotations

from ._common import TIMEOUT, driver_options, module, port, run, value


def connect(spec, password, role, project=None):
    kind = spec.get("kind") or "mysql"
    pymysql = module(kind)
    kw = {"host": spec.get("host") or "localhost", "port": port(spec, kind), "user": spec.get("user"),
          "password": password or "", "database": spec.get("database"), "connect_timeout": TIMEOUT,
          "charset": "utf8mb4", "autocommit": False}
    ssl = value(spec, "ssl", "none")
    if ssl == "required":
        kw["ssl"] = {"check_hostname": False}
        kw["ssl_verify_cert"] = False
    elif ssl == "verify":
        kw["ssl"] = {"check_hostname": True}
        kw["ssl_verify_cert"] = True
        kw["ssl_verify_identity"] = True
    opts = driver_options(spec)
    opts.pop("ssl", None)
    kw.update(opts)
    conn = pymysql.connect(**kw)
    if role == "source":
        # the session form: START TRANSACTION READ ONLY would end at the implicit commit before DDL
        run(conn, "SET SESSION TRANSACTION READ ONLY")
    return conn
