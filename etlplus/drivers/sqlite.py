"""SQLite (built in). A source opens with mode=ro; load_extension stays disabled."""

from __future__ import annotations

from ._common import TIMEOUT


def connect(spec, password, role, project=None):
    import sqlite3

    from etlplus import connections
    p = connections.file_path(project, spec)
    if role == "source":
        if not p.exists():
            raise connections.ConnectionError_(f"The SQLite file {p} does not exist.")
        return sqlite3.connect(connections.sqlite_uri(p, True), uri=True, timeout=TIMEOUT,
                               check_same_thread=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    # a target: autocommit mode, the loader opens its own transactions
    return sqlite3.connect(connections.sqlite_uri(p, False), uri=True, timeout=60, isolation_level=None,
                           check_same_thread=False)
