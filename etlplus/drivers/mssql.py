"""SQL Server and Azure SQL Database through pyodbc. The same detected ODBC driver serves reads
and writes."""

from __future__ import annotations

import re

from ._common import TIMEOUT, module, options, truthy

SQLSERVER_DRIVERS = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13.1 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
]

_BAD_DRIVER = re.compile(r"[\\/]|\.(so|dll|dylib)(\b|$)", re.I)


def check_driver_name(name: str, installed=None) -> str:
    """An ODBC driver is named, never loaded by path (loading a library by path runs its code)."""
    from etlplus.drivers import DriverError
    name = str(name or "").strip().strip("{}").strip()
    if not name or _BAD_DRIVER.search(name) or any(c in name for c in ";{}=\x00"):
        raise DriverError("Pick an installed ODBC driver by name.")
    if installed is not None and name not in installed:
        raise DriverError(f"The ODBC driver '{name}' is not installed. Installed: {', '.join(installed) or 'none'}.")
    return name


def detect_driver(spec: dict, installed=None) -> str:
    from etlplus.drivers import DriverError
    if installed is None:
        installed = list(module(spec.get("kind") or "sqlserver").drivers())
    if spec.get("driver"):
        return check_driver_name(spec["driver"], installed if installed else None)
    for name in SQLSERVER_DRIVERS:
        if name in installed:
            return name
    raise DriverError("No SQL Server ODBC driver is installed. Install 'ODBC Driver 18 for SQL Server'.")


def odbc_value(v) -> str:
    v = str(v)
    if any(c in v for c in ";{}= ") or v != v.strip():
        return "{" + v.replace("}", "}}") + "}"
    return v


def conn_str(spec: dict, password=None, read_only=True, installed=None) -> str:
    drv = detect_driver(spec, installed)
    kind = spec.get("kind") or "sqlserver"
    host = spec.get("host") or "localhost"
    server = f"{host},{spec['port']}" if spec.get("port") else str(host)
    parts = [("DRIVER", "{" + drv + "}"), ("SERVER", odbc_value(server))]
    if spec.get("database"):
        parts.append(("DATABASE", odbc_value(spec["database"])))
    auth = spec.get("authentication") or "sql"
    if kind == "azuresql" and auth == "ActiveDirectoryPassword":
        parts.append(("Authentication", "ActiveDirectoryPassword"))
    if spec.get("user"):
        parts.append(("UID", odbc_value(spec["user"])))
        parts.append(("PWD", odbc_value(password or "")))
    else:
        parts.append(("Trusted_Connection", "yes"))
    if read_only:
        parts.append(("ApplicationIntent", "ReadOnly"))
    opts = {k: v for k, v in options(spec).items()
            if k not in ("allow_after_actions", "statement_timeout", "bulk", "driver")}
    lower = {k.lower() for k in opts}
    if kind == "azuresql":
        opts = {k: v for k, v in opts.items() if k.lower() != "encrypt"}
        parts.append(("Encrypt", "yes"))
        lower.add("encrypt")
    elif spec.get("encrypt") not in (None, "") and "encrypt" not in lower:
        parts.append(("Encrypt", odbc_value(spec["encrypt"])))
        lower.add("encrypt")
    tsc = spec.get("trust_server_certificate")
    if tsc not in (None, "") and "trustservercertificate" not in lower:
        parts.append(("TrustServerCertificate", "yes" if truthy(tsc) else "no"))
        lower.add("trustservercertificate")
    if "18" in drv and kind != "azuresql" and not ({"trustservercertificate", "encrypt"} & lower):
        # v1 behaviour for a connection saved without either setting
        parts.append(("TrustServerCertificate", "yes"))
    for k, v in opts.items():
        if str(k).lower() in ("driver", "pwd", "password", "uid", "server", "database"):
            continue
        if not re.match(r"^[A-Za-z][A-Za-z0-9_ ]{0,60}$", str(k)):
            continue
        if isinstance(v, bool):
            v = "yes" if v else "no"
        parts.append((str(k), odbc_value(v)))
    return ";".join(f"{k}={v}" for k, v in parts) + ";"


def connect(spec, password, role, project=None):
    pyodbc = module(spec.get("kind") or "sqlserver")
    ro = role == "source"
    try:
        installed = list(pyodbc.drivers())
    except Exception:
        installed = None
    conn = pyodbc.connect(conn_str(spec, password, ro, installed), timeout=TIMEOUT, autocommit=False)
    return conn
