"""Firebird through firebird-driver. A source uses a read-only transaction (isc_tpb_read)."""

from __future__ import annotations

from ._common import module, port


def connect(spec, password, role, project=None):
    fdb = module("firebird")
    host = spec.get("host") or "localhost"
    database = f"{host}/{port(spec, 'firebird')}:{spec.get('database') or ''}"
    conn = fdb.connect(database, user=spec.get("user"), password=password or "", charset="UTF8")
    if role == "source":
        tpb = getattr(fdb, "tpb", None)
        mode = getattr(fdb, "TraAccessMode", None)
        iso = getattr(fdb, "Isolation", None)
        if tpb is not None and mode is not None and iso is not None:
            conn.main_transaction.default_tpb = tpb(iso.SNAPSHOT, access_mode=mode.READ)
        else:
            conn.read_only = True
    return conn
