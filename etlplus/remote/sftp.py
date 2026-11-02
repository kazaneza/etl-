"""SFTP through paramiko. The server's host key is compared with the stored SHA256 fingerprint
BEFORE any authentication; a mismatch closes the connection. No AutoAddPolicy, no known_hosts.
A private key is loaded from memory, never written to disk."""

from __future__ import annotations

import base64
import hashlib
import io
import posixpath
import stat

from . import CHUNK, RemoteError, RemoteFile, iso_utc, paths

TIMEOUT = 15


def fingerprint(key_bytes: bytes) -> str:
    """The OpenSSH form: SHA256:<base64 without padding>."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(key_bytes).digest()).decode("ascii").rstrip("=")


def _norm_fp(fp: str) -> str:
    return str(fp or "").strip().rstrip("=")


def _transport(spec):
    from etlplus import connections
    paramiko = connections.driver("sftp")
    host = spec.get("host")
    if not host:
        raise RemoteError("Set the host.")
    port = int(spec.get("port") or 22)
    t = paramiko.Transport((host, port))
    t.start_client(timeout=TIMEOUT)
    return paramiko, t


def scan_fingerprint(spec) -> str:
    """Only fetch and show the server's key; nothing is stored."""
    _, t = _transport(spec)
    try:
        return fingerprint(t.get_remote_server_key().asbytes())
    finally:
        t.close()


def _load_key(paramiko, text, passphrase):
    errors = []
    for cls_name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey"):
        cls = getattr(paramiko, cls_name, None)
        if cls is None:
            continue
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except Exception as e:  # wrong type, or wrong passphrase
            errors.append(e.__class__.__name__)
    raise RemoteError("The private key could not be read (wrong format or passphrase).")


class SftpLocation:
    can_rename = True

    def __init__(self, project, spec, log=None):
        self.spec = spec
        self.log = log
        self.t = None
        self.sftp = None
        root = str(spec.get("path") or ".").strip() or "."
        if "\x00" in root:
            raise RemoteError("The folder holds a NUL character.")
        self.root = root.rstrip("/") or "/"

    def connect(self):
        if self.sftp is not None:
            return
        expected = _norm_fp(self.spec.get("host_key_fingerprint"))
        if not expected:
            raise RemoteError("Set the host key fingerprint; an unknown key is never accepted.")
        paramiko, t = _transport(self.spec)
        try:
            got = fingerprint(t.get_remote_server_key().asbytes())
            if _norm_fp(got) != expected:
                raise RemoteError("The server's host key does not match.")
            secrets = dict(self.spec.get("_secrets") or {})
            user = self.spec.get("user") or ""
            if secrets.get("private_key"):
                pkey = _load_key(paramiko, secrets["private_key"], secrets.get("passphrase"))
                t.auth_publickey(user, pkey)
            else:
                t.auth_password(user, secrets.get("password") or "")
            self.sftp = paramiko.SFTPClient.from_transport(t)
            self.t = t
        except BaseException:
            t.close()
            raise

    def _abs(self, rel) -> str:
        rel = paths.check_rel(rel)
        return posixpath.join(self.root, rel)

    def iter_files(self, prefix, recursive, limit=5000):
        self.connect()
        out = []
        start = self._abs(prefix) if prefix else self.root
        stack = [(start, prefix or "")]
        while stack and len(out) < limit:
            d, rel_d = stack.pop()
            try:
                entries = sorted(self.sftp.listdir_attr(d), key=lambda a: a.filename)
            except IOError:
                continue
            for a in entries:
                name = a.filename
                rel = f"{rel_d}/{name}" if rel_d else name
                mode = a.st_mode or 0
                if stat.S_ISLNK(mode):
                    continue  # a listing never follows a symlink
                if stat.S_ISDIR(mode):
                    if recursive and not name.startswith(".") and name not in (".", ".."):
                        stack.append((posixpath.join(d, name), rel))
                    continue
                out.append(RemoteFile(rel, int(a.st_size or 0), iso_utc(a.st_mtime or 0)))
                if len(out) >= limit:
                    break
        return out

    def download(self, rel, sink):
        self.connect()
        with self.sftp.open(self._abs(rel), "rb") as fh:
            while True:
                data = fh.read(CHUNK)
                if not data:
                    break
                sink.write(data)

    def _mkdirs(self, path):
        parts = posixpath.dirname(path).split("/")
        cur = ""
        for p in parts:
            if not p:
                cur = "/" if not cur else cur
                continue
            cur = posixpath.join(cur, p) if cur else p
            try:
                self.sftp.stat(cur)
            except IOError:
                try:
                    self.sftp.mkdir(cur)
                except IOError:
                    pass

    def upload_file(self, local, rel):
        self.connect()
        dst = self._abs(rel)
        self._mkdirs(dst)
        with open(local, "rb") as fh:
            self.sftp.putfo(fh, dst)

    def rename(self, src, dst, replace=False):
        self.connect()
        s, d = self._abs(src), self._abs(dst)
        self._mkdirs(d)
        if replace and hasattr(self.sftp, "posix_rename"):
            self.sftp.posix_rename(s, d)
        else:
            self.sftp.rename(s, d)

    def delete(self, rel):
        self.connect()
        self.sftp.remove(self._abs(rel))

    def put_bytes(self, rel, data):
        self.connect()
        dst = self._abs(rel)
        self._mkdirs(dst)
        with self.sftp.open(dst, "wb") as fh:
            fh.write(data)

    def close(self):
        for x in (self.sftp, self.t):
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass
