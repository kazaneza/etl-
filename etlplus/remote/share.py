"""A shared folder (absolute or UNC path) and the project's own folder kind, read through the
same location interface. A listing never follows a symlink that leaves the root."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import CHUNK, RemoteError, RemoteFile, iso_utc, paths


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def walk_local(root: Path, prefix: str = "", recursive: bool = True, limit: int = 5000):
    """RemoteFiles under root/prefix, sorted, hidden and .partial files left out, no symlink out."""
    root = Path(root)
    try:
        real_root = root.resolve()
    except OSError:
        return []
    base = root / prefix if prefix else root
    if not base.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if not recursive:
            dirnames[:] = []
        for f in sorted(filenames):
            if f.startswith(".") or f.endswith(".partial"):
                continue
            p = Path(dirpath) / f
            try:
                if p.is_symlink() and not _inside(p.resolve(), real_root):
                    continue
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            out.append(RemoteFile(rel, st.st_size, iso_utc(st.st_mtime)))
            if len(out) >= limit:
                return out
    return out


class ShareLocation:
    can_rename = True

    def __init__(self, project, spec, log=None):
        self.project = project
        self.spec = spec
        self.log = log
        if spec.get("kind") == "folder":
            from etlplus import connections
            self.root = connections.file_path(project, spec)
        else:
            try:
                self.root = paths.share_root(project, spec.get("path"))
            except ValueError as e:
                raise RemoteError(str(e)) from None

    def connect(self):
        if not self.root.is_dir():
            raise RemoteError(f"{self.root} is not a folder this server can see.")

    def _path(self, rel) -> Path:
        rel = paths.check_rel(rel)
        p = self.root / rel
        real = p.resolve()
        if not _inside(real, self.root.resolve()):
            raise RemoteError(f"'{rel}' leaves the folder.")
        if self.spec.get("kind") == "folder":
            try:
                paths.check_local(self.project, real)
            except ValueError as e:
                raise RemoteError(str(e)) from None
        return p

    def iter_files(self, prefix, recursive, limit=5000):
        if prefix:
            paths.check_rel(prefix)
        return walk_local(self.root, prefix, recursive, limit)

    def download(self, rel, sink):
        with open(self._path(rel), "rb") as fh:
            while True:
                data = fh.read(CHUNK)
                if not data:
                    break
                sink.write(data)

    def upload_file(self, local, rel):
        dst = self._path(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "rb") as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, CHUNK)

    def rename(self, src, dst, replace=False):
        s, d = self._path(src), self._path(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists() and not replace:
            raise RemoteError(f"'{dst}' already exists; it is not overwritten.")
        os.replace(s, d)

    def delete(self, rel):
        self._path(rel).unlink()

    def put_bytes(self, rel, data):
        p = self._path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def close(self):
        pass
