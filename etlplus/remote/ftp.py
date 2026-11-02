"""FTP and explicit FTPS (ftplib). tls (the default) means certificate and host name checked and
the data channel protected (prot_p). Plain FTP needs options.allow_plain."""

from __future__ import annotations

import datetime as dt
import io
import posixpath
import ssl

from . import CHUNK, RemoteError, RemoteFile, iso_utc, options, paths

TIMEOUT = 15


def _truthy(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class FtpLocation:
    can_rename = True

    def __init__(self, project, spec, log=None):
        self.spec = spec
        self.log = log
        self.ftp = None
        self.tls = _truthy(spec.get("tls"), True)
        if not self.tls and not options(spec).get("allow_plain"):
            raise RemoteError("Plain FTP sends the password in clear; set options.allow_plain to use it.")
        self.root = (str(spec.get("path") or "").strip() or ".").rstrip("/") or "/"

    def connect(self):
        if self.ftp is not None:
            return
        from etlplus import connections
        ftplib = connections.driver("ftp")
        if self.tls:
            ftp = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=TIMEOUT)
        else:
            ftp = ftplib.FTP(timeout=TIMEOUT)
        ftp.connect(self.spec.get("host"), int(self.spec.get("port") or 21))
        try:
            if self.tls:
                ftp.auth()
            secrets = dict(self.spec.get("_secrets") or {})
            ftp.login(self.spec.get("user") or "anonymous", secrets.get("password") or "")
            if self.tls:
                ftp.prot_p()
            ftp.set_pasv(_truthy(self.spec.get("passive"), True))
        except BaseException:
            try:
                ftp.close()
            except Exception:
                pass
            raise
        self.ftp = ftp

    def _abs(self, rel):
        rel = paths.check_rel(rel)
        return rel if self.root == "." else posixpath.join(self.root, rel)

    def _entries(self, d):
        try:
            for name, facts in self.ftp.mlsd(d, facts=["type", "size", "modify"]):
                yield name, facts
            return
        except Exception:
            pass
        for full in self.ftp.nlst(d):
            name = posixpath.basename(full.rstrip("/"))
            try:
                size = self.ftp.size(posixpath.join(d, name))
                yield name, {"type": "file", "size": str(size or 0)}
            except Exception:
                yield name, {"type": "dir"}

    def iter_files(self, prefix, recursive, limit=5000):
        self.connect()
        out = []
        start = self._abs(prefix) if prefix else self.root
        stack = [(start, prefix or "")]
        while stack and len(out) < limit:
            d, rel_d = stack.pop()
            for name, facts in sorted(self._entries(d), key=lambda x: x[0]):
                if name in (".", "..") or "/" in name:
                    continue
                rel = f"{rel_d}/{name}" if rel_d else name
                typ = (facts.get("type") or "").lower()
                if typ in ("dir", "cdir", "pdir"):
                    if recursive and typ == "dir" and not name.startswith("."):
                        stack.append((posixpath.join(d, name), rel))
                    continue
                if typ not in ("file", ""):
                    continue
                mod = facts.get("modify")
                when = 0
                if mod:
                    try:
                        when = dt.datetime.strptime(mod[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
                    except ValueError:
                        when = 0
                out.append(RemoteFile(rel, int(facts.get("size") or 0), iso_utc(when)))
                if len(out) >= limit:
                    break
        return out

    def download(self, rel, sink):
        self.connect()
        self.ftp.retrbinary(f"RETR {self._abs(rel)}", sink.write, blocksize=CHUNK)

    def upload_file(self, local, rel):
        self.connect()
        with open(local, "rb") as fh:
            self.ftp.storbinary(f"STOR {self._abs(rel)}", fh, blocksize=CHUNK)

    def rename(self, src, dst, replace=False):
        self.connect()
        d = self._abs(dst)
        if replace:
            try:
                self.ftp.delete(d)
            except Exception:
                pass
        self.ftp.rename(self._abs(src), d)

    def delete(self, rel):
        self.connect()
        self.ftp.delete(self._abs(rel))

    def put_bytes(self, rel, data):
        self.connect()
        self.ftp.storbinary(f"STOR {self._abs(rel)}", io.BytesIO(data))

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None
