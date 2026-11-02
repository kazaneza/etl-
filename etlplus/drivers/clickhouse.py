"""ClickHouse through clickhouse-connect (HTTP). A source runs every query with readonly=1.

clickhouse-connect is not DB-API, so a small adapter gives the cursor/execute/fetchmany shape
the readers use. Parameters are bound by the client (%(p0)s), never pasted into SQL.
"""

from __future__ import annotations

from ._common import TIMEOUT, module, port, truthy


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = iter(())
        self.rowcount = -1

    def execute(self, sql, params=None):
        client = self.conn.client
        kw = {}
        if params:
            kw["parameters"] = params if isinstance(params, dict) else {f"p{i}": v for i, v in enumerate(params)}
        if self.conn.settings:
            kw["settings"] = dict(self.conn.settings)
        self.close()
        if hasattr(client, "query_row_block_stream"):
            # streamed in blocks: an extract never holds the whole result
            self._ctx = client.query_row_block_stream(sql, **kw)
            stream = self._ctx.__enter__()
            source = getattr(stream, "source", None)
            names = list(getattr(source, "column_names", []) or [])
            types = list(getattr(source, "column_types", []) or [])
            self._rows = (tuple(r) for block in stream for r in block)
        else:
            result = client.query(sql, **kw)
            names = list(getattr(result, "column_names", []) or [])
            types = list(getattr(result, "column_types", []) or [])
            self._rows = iter(result.result_rows)
        self.description = [(n, (types[i] if i < len(types) else None), None, None, None, None, None)
                            for i, n in enumerate(names)]
        return self

    def fetchmany(self, n=1000):
        out = []
        for row in self._rows:
            out.append(tuple(row))
            if len(out) >= n:
                break
        return out

    def fetchall(self):
        return [tuple(r) for r in self._rows]

    def fetchone(self):
        return next(self._rows, None)

    def close(self):
        self._rows = iter(())
        ctx, self._ctx = getattr(self, "_ctx", None), None
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class Connection:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    def cursor(self, *a, **k):
        return Cursor(self)

    def command(self, sql, params=None):
        kw = {"settings": dict(self.settings)} if self.settings else {}
        if params:
            kw["parameters"] = params
        return self.client.command(sql, **kw)

    def insert_arrow(self, table, arrow_table):
        return self.client.insert_arrow(table, arrow_table)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


def connect(spec, password, role, project=None):
    cc = module("clickhouse")
    secure = truthy(spec.get("secure"))
    p = spec.get("port") or (8443 if secure else 8123)
    settings = {"readonly": 1} if role == "source" else {}
    client = cc.get_client(host=spec.get("host") or "localhost", port=int(p), username=spec.get("user") or "default",
                           password=password or "", database=spec.get("database") or "default", secure=secure,
                           connect_timeout=TIMEOUT, settings=dict(settings))
    return Connection(client, settings)
