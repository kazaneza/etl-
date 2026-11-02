"""Remote locations: shared folders, SFTP, FTP/FTPS, S3, Azure Blob and HTTP downloads.

A remote file is always fetched into the stage first (incoming/<name>.partial, size checked,
sha256 computed, renamed), then read locally. A location is only read, unless a job asks for an
after-load action AND the connection allows it (options.allow_after_actions). Secrets come from
the secrets store only and are never logged; a logged URL has its query string replaced by ?...
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths

LIST_LIMIT = 5000
CHUNK = 1 << 20
AFTER_ACTIONS = ("keep", "archive", "mark_done", "delete")
NOT_ALLOWED = ("This connection does not allow moving or deleting files; set allow_after_actions on the "
               "connection.")


class RemoteError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    path: str            # path relative to the location root
    size: int
    modified: str        # ISO UTC
    etag: str | None = None

    def dict(self):
        return asdict(self)


class FileList(list):
    """A listing; truncated is true when it stopped at the limit."""
    truncated = False


def iso_utc(ts) -> str:
    if isinstance(ts, dt.datetime):
        d = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
    else:
        d = dt.datetime.fromtimestamp(float(ts or 0), tz=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log(log, msg):
    if log:
        log(msg)


def options(spec) -> dict:
    return dict(spec.get("options") or {})


def _spec(project, cid, spec=None, override=None):
    from etlplus import connections
    s = spec if spec is not None else connections.spec_for(project, cid, override)
    kind = s.get("kind")
    if kind not in ("share", "folder", "sftp", "ftp", "s3", "azure_blob", "http"):
        raise RemoteError(f"Connection '{cid}' ({kind}) is not a folder or remote location.")
    return s


def open_location(project, spec, log=None):
    kind = spec.get("kind")
    if kind in ("share", "folder"):
        from .share import ShareLocation
        return ShareLocation(project, spec, log)
    if kind == "sftp":
        from .sftp import SftpLocation
        return SftpLocation(project, spec, log)
    if kind == "ftp":
        from .ftp import FtpLocation
        return FtpLocation(project, spec, log)
    if kind == "s3":
        from .s3 import S3Location
        return S3Location(project, spec, log)
    if kind == "azure_blob":
        from .azure import AzureLocation
        return AzureLocation(project, spec, log)
    if kind == "http":
        from .http import HttpLocation
        return HttpLocation(project, spec, log)
    raise RemoteError(f"Unknown location kind '{kind}'.")


def _close(loc):
    try:
        loc.close()
    except Exception:
        pass


def _list(loc, patterns, limit, log=None) -> FileList:
    out, seen = FileList(), set()
    groups = {}
    for p in patterns:
        prefix, recursive = paths.static_prefix(p)
        groups.setdefault((prefix, recursive), []).append(p)
    for (prefix, recursive), pats in groups.items():
        regs = [paths.glob_regex(p) for p in pats]
        for f in loc.iter_files(prefix, recursive, limit=limit + 1):
            try:
                rel = paths.check_rel(f.path, "remote file name")
            except ValueError as e:
                _log(log, f"skipped an unsafe name from the server: {e}")
                continue
            if rel in seen or rel.endswith(".partial") or rel.endswith(".done") \
                    or rel.rsplit("/", 1)[-1].startswith("."):
                continue
            if not any(r.match(rel) for r in regs):
                continue
            f.path = rel
            seen.add(rel)
            out.append(f)
            if len(out) >= limit:
                out.truncated = True
                _log(log, f"the listing stopped at {limit} files")
                out.sort(key=lambda x: x.path)
                return out
    out.sort(key=lambda x: x.path)
    return out


def list_files(project, cid, pattern, *, limit=LIST_LIMIT, spec=None, log=None) -> FileList:
    """The files matching a glob (or a list of globs) relative to the location root."""
    spec = _spec(project, cid, spec)
    patterns = paths.check_pattern(pattern)
    limit = max(1, min(int(limit or LIST_LIMIT), LIST_LIMIT))
    loc = open_location(project, spec, log)
    try:
        return _list(loc, patterns, limit, log)
    finally:
        _close(loc)


class _Sink:
    """A file being written: counts, hashes and stops at the size cap."""

    def __init__(self, fh, max_bytes):
        self.fh = fh
        self.max = max_bytes
        self.n = 0
        self.h = hashlib.sha256()

    def write(self, data):
        if not data:
            return 0
        self.n += len(data)
        if self.max is not None and self.n > self.max:
            raise RemoteError(f"The file is larger than the {self.max // CHUNK} MB limit (options.max_mb).")
        self.h.update(data)
        self.fh.write(data)
        return len(data)


def max_bytes(spec) -> int:
    try:
        mb = float(options(spec).get("max_mb") or 2048)
    except (TypeError, ValueError):
        mb = 2048
    return int(mb * CHUNK)


def fetch(project, cid, files, dest_dir, log=None, spec=None) -> list[dict]:
    """Download each file to dest_dir/<name>.partial, check its size, hash it, rename it.
    Returns [{"remote", "local", "size", "modified", "sha256"}]."""
    spec = _spec(project, cid, spec)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    names = {}
    for f in files:
        name = paths.local_name(f.path, dest)
        if name.lower() in names:
            raise RemoteError(f"'{f.path}' and '{names[name.lower()]}' would both be saved as {name}; "
                              "narrow the file pattern.")
        names[name.lower()] = f.path
    cap = max_bytes(spec)
    out = []
    loc = open_location(project, spec, log)
    try:
        for f in files:
            name = paths.local_name(f.path, dest)
            final = dest / name
            if final.exists():
                raise RemoteError(f"{name} is already in the stage folder; it is never overwritten.")
            partial = dest / (name + ".partial")
            try:
                with open(partial, "wb") as fh:
                    sink = _Sink(fh, cap)
                    loc.download(f.path, sink)
                if f.size and f.size > 0 and sink.n != f.size:
                    raise RemoteError(f"{f.path}: got {sink.n} bytes, the server listed {f.size}.")
                os.replace(partial, final)
            except BaseException:
                try:
                    partial.unlink()
                except OSError:
                    pass
                raise
            _log(log, f"fetched {f.path} ({sink.n} bytes)")
            # size and modified are the listed values, as they are (a server that lists no size
            # gives 0): the ledger key file_seen compares the next listing against. bytes is what came.
            out.append({"remote": f.path, "path": f.path, "local": name, "size": int(f.size or 0),
                        "bytes": sink.n, "modified": f.modified, "sha256": sink.h.hexdigest(), "etag": f.etag})
    finally:
        _close(loc)
    return out


def check_after(spec, action, archive_to=None) -> str | None:
    """A problem with a pickup after-action, or None. Used by validate_job too."""
    action = action or "keep"
    if action not in AFTER_ACTIONS:
        return f"pickup.after is one of {', '.join(AFTER_ACTIONS)}."
    if action == "keep":
        return None
    if spec.get("kind") == "http":
        return "An HTTP download has nothing to move or delete; use after: keep."
    if not options(spec).get("allow_after_actions"):
        return NOT_ALLOWED
    if action == "archive":
        if not archive_to:
            return "Set pickup.archive_to, the folder that loaded files move to."
        try:
            paths.check_rel(archive_to, "archive_to")
        except ValueError as e:
            return str(e)
    return None


def finish(project, cid, fetched, action, archive_to=None, log=None, spec=None) -> None:
    """After a successful load: keep (nothing), archive, mark_done (<file>.done beside it) or delete."""
    action = action or "keep"
    if action == "keep":
        return
    spec = _spec(project, cid, spec)
    problem = check_after(spec, action, archive_to)
    if problem:
        raise RemoteError(problem)
    loc = open_location(project, spec, log)
    try:
        for rec in fetched:
            rel = paths.check_rel(rec["remote"], "remote file name")
            if action == "archive":
                dst = paths.check_rel(f"{paths.check_rel(archive_to, 'archive_to')}/{rel}", "archive path")
                loc.rename(rel, dst)
                _log(log, f"archived {rel} to {dst}")
            elif action == "mark_done":
                loc.put_bytes(rel + ".done", b"")
                _log(log, f"marked {rel} done")
            elif action == "delete":
                loc.delete(rel)
                _log(log, f"deleted {rel} at the source")
    finally:
        _close(loc)


def upload(project, cid, local, remote_path, log=None, spec=None) -> None:
    """Put a local file at remote_path: as <remote_path>.partial then a rename, or where the store
    cannot rename (S3, Azure) directly (a PUT is atomic) followed by <remote_path>.done."""
    spec = _spec(project, cid, spec)
    from etlplus import connections
    if connections.is_read_only(spec):
        raise RemoteError(f"Connection '{cid}' is a source; nothing is written to it.")
    rel = paths.check_rel(remote_path, "destination path")
    local = Path(local)
    if local.name.endswith(".partial"):
        raise RemoteError("A .partial file is never uploaded.")
    loc = open_location(project, spec, log)
    try:
        if getattr(loc, "can_rename", True):
            loc.upload_file(local, rel + ".partial")
            loc.rename(rel + ".partial", rel, replace=True)
        else:
            loc.upload_file(local, rel)
            loc.put_bytes(rel + ".done", b"")
        _log(log, f"uploaded {local.name} to {rel}")
    finally:
        _close(loc)


def _step(steps, name, fn):
    from etlplus import connections
    return connections._step(steps, name, fn)


def test(project, cid, override=None, spec=None) -> list[dict]:
    """Steps: driver, connect, list, and a write probe when the role allows writing."""
    from etlplus import connections, drivers
    steps = []
    try:
        spec = _spec(project, cid, spec, override)
    except Exception as e:
        return [{"name": "settings", "ok": False, "detail": str(e), "ms": 0}]
    kind = spec.get("kind")

    def drv():
        d = drivers.get(kind)
        if d.pip == "built in":
            return "built in"
        connections.driver(kind)
        return d.pip

    if not _step(steps, "driver", drv):
        return steps
    holder = {}
    if kind == "sftp" and not spec.get("host_key_fingerprint"):
        def scan():
            from .sftp import scan_fingerprint
            fp = scan_fingerprint(spec)
            return f"The server's host key is {fp}. Check it with the server's owner, then paste it."
        _step(steps, "scan", scan)
        steps.append({"name": "connect", "ok": False, "ms": 0,
                      "detail": "Paste the host key fingerprint to connect; an unknown key is never accepted."})
        return steps

    def conn():
        holder["loc"] = open_location(project, spec)
        holder["loc"].connect()
        return "connected"

    if not _step(steps, "connect", conn):
        if "loc" in holder:
            _close(holder["loc"])
        return steps
    loc = holder["loc"]
    try:
        def lst():
            files = _list(loc, ["**/*"] if kind != "http" else ["*"], 100)
            more = "+" if files.truncated else ""
            return f"{len(files)}{more} file(s)"
        _step(steps, "list", lst)
        if not connections.is_read_only(spec) and kind != "http":
            def probe():
                name = f".etlplus-probe-{os.getpid()}"
                loc.put_bytes(name, b"")
                loc.delete(name)
                return "can write"
            _step(steps, "write", probe)
    finally:
        _close(loc)
    return steps


def redact_url(url: str) -> str:
    s = str(url)
    i = s.find("?")
    return s if i < 0 else s[:i] + "?…"
