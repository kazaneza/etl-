"""Any database with an ODBC driver: a DSN or a connection string. The session is opened with
pyodbc readonly=True (SQL_ATTR_ACCESS_MODE).

A connection string is split into key=value parts with ODBC brace quoting honoured
({a;b}}c} is the value a;b}c). The secret parts (PWD, Password, AccountKey, ...) are kept in the
secrets store under "odbc:<key>" and the stored string never holds them. A DRIVER= part must be
an installed driver's name: a path to a library is refused, because loading it runs its code.
"""

from __future__ import annotations

from ._common import TIMEOUT, module
from .mssql import check_driver_name, odbc_value

SECRET_PARTS = ("pwd", "password", "accountkey", "sharedaccesssignature", "token", "accesstoken",
                "clientsecret", "secretkey")
SECRET_PREFIX = "odbc:"


def split(cs: str) -> list[tuple[str, str]]:
    """[(key, value)] from a connection string. Values may be brace-quoted; '}}' inside braces is '}'."""
    from etlplus.drivers import DriverError
    s = str(cs or "")
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " ;\t\r\n":
            i += 1
        if i >= n:
            break
        j = s.find("=", i)
        if j < 0:
            raise DriverError("The connection string has a part without '='.")
        key = s[i:j].strip()
        if not key:
            raise DriverError("The connection string has a part with an empty name.")
        i = j + 1
        while i < n and s[i] == " ":
            i += 1
        if i < n and s[i] == "{":
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise DriverError("The connection string has an unclosed '{'.")
                if s[i] == "}":
                    if i + 1 < n and s[i + 1] == "}":
                        buf.append("}")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(s[i])
                i += 1
            val = "".join(buf)
            while i < n and s[i] == " ":
                i += 1
            if i < n and s[i] != ";":
                raise DriverError("The connection string has text after a '}' value.")
        else:
            j = s.find(";", i)
            j = n if j < 0 else j
            val = s[i:j].strip()
            i = j
        out.append((key, val))
        if i < n and s[i] == ";":
            i += 1
    return out


def join(parts) -> str:
    return ";".join(f"{k}={odbc_value(v) if str(k).upper() != 'DRIVER' else '{' + str(v) + '}'}"
                    for k, v in parts) + (";" if parts else "")


# parts whose name looks secret but holds a setting, not a secret
NOT_SECRET_PARTS = ("keystoreauthentication", "keystoreprincipalid", "keystorelocation", "keystore",
                    "columnencryptionkeystoreprovider", "primarykey", "keyfile", "privkeyfile")
_SECRET_WORDS = ("pwd", "password", "passwd", "passphrase", "secret", "token", "signature", "credential")


def is_secret_part(key: str) -> bool:
    """True for a part that holds a secret. The rules follow config.is_secret_key on the
    normalised name: any name containing pwd, password, secret, token, signature or credential,
    or ending in key or sas, except a short list of settings (KeyStoreAuthentication, ...)."""
    k = str(key).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if k in SECRET_PARTS:
        return True
    if k in NOT_SECRET_PARTS:
        return False
    if any(w in k for w in _SECRET_WORDS):
        return True
    return k.endswith("key") or k.endswith("sas")


def separate(cs: str) -> tuple[str, dict]:
    """(the string without secret parts, {"odbc:<Key>": value}). DRIVER= is checked by name."""
    keep, secret = [], {}
    for k, v in split(cs):
        if k.strip().upper() == "DRIVER":
            check_driver_name(v)
        if is_secret_part(k):
            secret[SECRET_PREFIX + k.strip()] = v
        else:
            keep.append((k.strip(), v))
    return (join(keep) if keep else ""), secret


def build(spec, password) -> str:
    from etlplus.drivers import DriverError
    secrets = dict(spec.get("_secrets") or {})
    parts = []
    if spec.get("connection_string"):
        for k, v in split(spec["connection_string"]):
            if k.strip().upper() == "DRIVER":
                check_driver_name(v)
            if is_secret_part(k):
                continue  # a stored string never holds one; a typed one would come through secrets
            parts.append((k.strip(), v))
    elif spec.get("dsn"):
        parts.append(("DSN", str(spec["dsn"])))
    else:
        raise DriverError("Set a DSN or a connection string.")
    keys = {k.upper() for k, _ in parts}
    if spec.get("user") and "UID" not in keys:
        parts.append(("UID", str(spec["user"])))
    for k, v in secrets.items():
        if k.startswith(SECRET_PREFIX) and v:
            parts.append((k[len(SECRET_PREFIX):], v))
    if password and not any(k.startswith(SECRET_PREFIX) and k[len(SECRET_PREFIX):].lower() in ("pwd", "password")
                            for k in secrets):
        parts.append(("PWD", password))
    return join(parts)


def connect(spec, password, role, project=None):
    pyodbc = module("odbc")
    cs = build(spec, password)
    if role == "source":
        return pyodbc.connect(cs, readonly=True, timeout=TIMEOUT, autocommit=False)
    return pyodbc.connect(cs, timeout=TIMEOUT, autocommit=False)
