"""DuckDB. A source opens read_only with external access off and the configuration locked."""

from __future__ import annotations

from ._common import module


def connect(spec, password, role, project=None):
    from etlplus import connections
    duckdb = module("duckdb")
    p = connections.file_path(project, spec)
    if role == "source":
        if not p.exists():
            raise connections.ConnectionError_(f"The DuckDB file {p} does not exist.")
        conn = duckdb.connect(str(p), read_only=True)
        # a source query reads this database only: no other file, no URL, no way to undo it
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
        return conn
    p.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(p))
