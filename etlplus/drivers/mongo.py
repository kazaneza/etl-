"""MongoDB through pymongo, source only: readPreference secondaryPreferred, find and aggregate only.

A "table" is a collection. A query is JSON: {"filter": {...}, "projection": {...}} or
{"pipeline": [...]}, checked by guard.check_mongo. Documents come out flat: nested keys are
dotted, arrays are JSON text.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
from urllib.parse import unquote

from ._common import TIMEOUT, module, truthy

_URI_AUTH = re.compile(r"^(mongodb(?:\+srv)?://)([^@/]*)@(.*)$", re.I)


SECRET_PREFIX = "mongo:"


def is_secret_option(name: str) -> bool:
    """A URI option that holds a secret: tlsCertificateKeyFilePassword, authMechanismProperties
    (AWS session tokens), and any option whose name says password, token or secret."""
    k = str(name or "").strip().lower()
    return k == "authmechanismproperties" or any(w in k for w in ("password", "passwd", "token", "secret", "pwd"))


def separate_options(uri: str) -> tuple[str, dict]:
    """(uri without secret query options, {"mongo:<option>": raw value}). Options keep their
    order and their percent-encoding; build_uri puts them back."""
    uri = str(uri or "").strip()
    base, q, query = uri.partition("?")
    if not q:
        return uri, {}
    keep, found = [], {}
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        if is_secret_option(unquote(name)):
            found[SECRET_PREFIX + unquote(name)] = value
        else:
            keep.append(part)
    return (base + ("?" + "&".join(keep) if keep else "")), found


def separate_uri(uri: str) -> tuple[str, str | None, str | None]:
    """(uri without credentials and secret options, user, password). The stored URI never holds
    a password; secret options are taken out by separate_options first (connections does)."""
    uri, _ = separate_options(uri)
    m = _URI_AUTH.match(uri)
    if not m:
        return uri, None, None
    creds = m.group(2)
    user, _, pw = creds.partition(":")
    return m.group(1) + m.group(3), (unquote(user) or None), (unquote(pw) if pw else None)


def with_secret_options(uri: str, secrets: dict | None) -> str:
    """The URI with the secret options from the secrets store put back."""
    extra = [f"{k[len(SECRET_PREFIX):]}={v}" for k, v in (secrets or {}).items()
             if str(k).startswith(SECRET_PREFIX) and v]
    if not extra:
        return uri
    return uri + ("&" if "?" in uri else ("?" if "/" in uri.split("://", 1)[-1] else "/?")) + "&".join(extra)


class Database:
    """The handle connections and sources get: the client and the database."""

    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.db = client[name]

    def collections(self):
        return sorted(self.db.list_collection_names())

    def collection(self, name):
        return self.db[name]

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def rollback(self):
        pass


def client_args(spec, password) -> tuple[str, dict]:
    raw, user_in_uri, _ = separate_uri(spec.get("uri") or spec.get("host") or "localhost")
    host = raw if raw.lower().startswith("mongodb") else "mongodb://" + raw
    host = with_secret_options(host, spec.get("_secrets"))
    kw = {"readPreference": "secondaryPreferred", "serverSelectionTimeoutMS": TIMEOUT * 1000,
          "connectTimeoutMS": TIMEOUT * 1000, "appname": "etlplus", "tls": truthy(spec.get("tls"), True)}
    user = spec.get("user") or user_in_uri
    if user:
        kw["username"] = user
        kw["password"] = password or ""
        kw["authSource"] = spec.get("auth_source") or "admin"
    return host, kw


def connect(spec, password, role, project=None):
    from etlplus.drivers import DriverError
    if role != "source":
        raise DriverError("MongoDB is a source only.")
    pymongo = module("mongodb")
    if not spec.get("database"):
        raise DriverError("Set the database.")
    host, kw = client_args(spec, password)
    return Database(pymongo.MongoClient(host, **kw), spec["database"])


def _scalar(v):
    if v is None or isinstance(v, (bool, int, float, str, dt.datetime, dt.date, decimal.Decimal, bytes)):
        return v
    to_dec = getattr(v, "to_decimal", None)
    if callable(to_dec):
        return to_dec()
    return str(v)


def flatten(doc: dict, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    for k, v in (doc or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            flatten(v, key + ".", out)
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(v, default=str, ensure_ascii=False)
        else:
            out[key] = _scalar(v)
    return out


def parse_query(query) -> dict:
    from etlplus import guard
    if query in (None, ""):
        return {}
    obj = json.loads(query) if isinstance(query, str) else query
    if not isinstance(obj, dict):
        raise guard.GuardError("A MongoDB query is a JSON object: a filter and projection, or a pipeline.")
    unknown = set(obj) - {"filter", "projection", "pipeline", "sort", "limit"}
    if unknown:
        raise guard.GuardError(f"Unknown query keys: {', '.join(sorted(unknown))}.")
    if "pipeline" in obj and ("filter" in obj or "projection" in obj):
        raise guard.GuardError("Use a filter and projection, or a pipeline, not both.")
    guard.check_mongo(obj)
    return obj


def window_filter(column, window=None, watermark=None) -> dict:
    if window is not None:
        lo, hi = window
        cond = {}
        if lo is not None:
            cond["$gte"] = _bound(lo)
        if hi is not None:
            cond["$lt"] = _bound(hi)
        return {column: cond} if cond else {}
    if watermark is not None:
        return {column: {"$gt": _bound(watermark)}}
    return {}


def _bound(v):
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        try:
            if len(v.strip()) == 10:
                d = dt.date.fromisoformat(v.strip())
                return dt.datetime(d.year, d.month, d.day)
            return dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return v
    return v


def cursor(db: Database, collection: str, query=None, window_col=None, window=None, watermark=None,
           limit=None, batch_size=1000):
    """An iterator of raw documents for a collection and a checked query."""
    q = parse_query(query)
    coll = db.collection(collection)
    extra = window_filter(window_col, window, watermark) if window_col else {}
    if "pipeline" in q:
        pipeline = list(q["pipeline"])
        if extra:
            pipeline.insert(0, {"$match": extra})
        if limit:
            pipeline.append({"$limit": int(limit)})
        return coll.aggregate(pipeline, batchSize=batch_size, allowDiskUse=False)
    flt = dict(q.get("filter") or {})
    if extra:
        flt = {"$and": [flt, extra]} if flt else extra
    kw = {"batch_size": batch_size}
    if q.get("projection"):
        kw["projection"] = q["projection"]
    cur = coll.find(flt, **kw)
    if q.get("sort"):
        cur = cur.sort(list(q["sort"].items()) if isinstance(q["sort"], dict) else q["sort"])
    lim = limit or q.get("limit")
    if lim:
        cur = cur.limit(int(lim))
    return cur

