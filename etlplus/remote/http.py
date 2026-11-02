"""HTTP(S) downloads (urllib). The URL may hold {date} or {date:%Y%m%d}, filled by a regex
(never str.format). At most 5 redirects, never from https to http; the token header is dropped
on a redirect to another host. Loopback, link-local, cloud metadata and the etl+ server's own
port are refused unless options.allow_private. A download is streamed in 1 MB chunks up to
options.max_mb. A logged URL has its query string replaced by ?..."""

from __future__ import annotations

import datetime as dt
import email.utils
import ipaddress
import os
import re
import socket
import ssl
from urllib.parse import unquote, urlsplit

from . import CHUNK, RemoteError, RemoteFile, iso_utc, options, paths, redact_url

TIMEOUT = 30
MAX_REDIRECTS = 5
METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
            ipaddress.ip_address("100.100.100.200")}
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def own_port() -> int:
    try:
        return int(os.environ.get("ETLPLUS_PORT") or 8300)
    except ValueError:
        return 8300


def check_target(url: str, allow_private=False) -> None:
    """Refuse loopback, link-local, metadata and the server's own port (unless allowed)."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RemoteError("Only http:// and https:// URLs can be downloaded.")
    if allow_private:
        return
    port = u.port or (443 if u.scheme == "https" else 80)
    if port == own_port():
        raise RemoteError("That URL points at the etl+ server's own port.")
    try:
        infos = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise RemoteError(f"The host {u.hostname} cannot be found ({e.strerror}).") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip in METADATA or ip.is_multicast:
            raise RemoteError(f"{u.hostname} is a local or metadata address; set options.allow_private to "
                              "download from it.")


def redirect_handler(token_name="", allow_private=False, log=None):
    """At most 5 redirects, never https to http, each target checked, the token header dropped
    when the host changes."""
    import urllib.request

    class Redirects(urllib.request.HTTPRedirectHandler):
        max_redirections = MAX_REDIRECTS

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            old, new = urlsplit(req.full_url), urlsplit(newurl)
            if old.scheme == "https" and new.scheme != "https":
                raise RemoteError("A redirect from https to http is refused.")
            check_target(newurl, allow_private)
            r = super().redirect_request(req, fp, code, msg, headers, newurl)
            if r is not None and token_name and old.hostname != new.hostname:
                for k in list(r.headers):
                    if k.lower() == token_name:
                        del r.headers[k]
            if log:
                log(f"redirected to {redact_url(newurl)}")
            return r

    return Redirects()


def _file_name(url: str) -> str:
    name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = _SAFE_CHARS.sub("_", name).strip("._") or "download"
    return name[:200]


class HttpLocation:
    can_rename = False

    def __init__(self, project, spec, log=None):
        self.spec = spec
        self.log = log
        self.opts = options(spec)
        tz = (getattr(project, "settings", None) or {}).get("timezone")
        today = dt.date.today()
        if tz:
            try:
                from zoneinfo import ZoneInfo
                today = dt.datetime.now(ZoneInfo(str(tz))).date()
            except Exception:
                pass
        self.date = spec.get("_date") or today
        try:
            self.url = paths.fill_template(str(spec.get("url") or ""), date=self.date, allowed=("date",))
        except ValueError as e:
            raise RemoteError(str(e)) from None
        if not self.url:
            raise RemoteError("Set the URL.")
        self.allow_private = bool(self.opts.get("allow_private"))
        check_target(self.url, self.allow_private)

    def connect(self):
        pass

    def _context(self):
        verify = self.spec.get("verify_tls")
        if verify in (False, "false", "no", 0, "0"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context()

    def _headers(self) -> dict:
        h = {"User-Agent": "etlplus"}
        name = str(self.spec.get("header_name") or "").strip()
        value = (self.spec.get("_secrets") or {}).get("header_value")
        if name and value:
            if not re.match(r"^[A-Za-z0-9-]{1,64}$", name):
                raise RemoteError("The token header name holds characters a header cannot have.")
            h[name] = value
        return h

    def _open(self, method="GET"):
        import urllib.error
        import urllib.request

        token_name = str(self.spec.get("header_name") or "").strip().lower()
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self._context()),
                                             redirect_handler(token_name, self.allow_private, self.log))
        req = urllib.request.Request(self.url, headers=self._headers(), method=method)
        try:
            return opener.open(req, timeout=TIMEOUT)
        except RemoteError:
            raise
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                raise RemoteError(f"Too many redirects (more than {MAX_REDIRECTS}).") from None
            raise RemoteError(f"{redact_url(self.url)} answered {e.code} {e.reason}.") from None
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, RemoteError):
                raise reason from None
            raise RemoteError(f"{redact_url(self.url)} cannot be reached ({reason}).") from None

    def iter_files(self, prefix, recursive, limit=5000):
        size, modified, etag = 0, iso_utc(dt.datetime.combine(self.date, dt.time(0))), None
        try:
            with self._open("HEAD") as r:
                size = int(r.headers.get("Content-Length") or 0)
                lm = r.headers.get("Last-Modified")
                if lm:
                    modified = iso_utc(email.utils.parsedate_to_datetime(lm))
                etag = (r.headers.get("ETag") or "").strip('"') or None
        except RemoteError as e:
            if "refused" in str(e) or "local or metadata" in str(e) or "own port" in str(e):
                raise
        except Exception:
            pass
        return [RemoteFile(_file_name(self.url), size, modified, etag)]

    def download(self, rel, sink):
        if self.log:
            self.log(f"downloading {redact_url(self.url)}")
        with self._open("GET") as r:
            length = r.headers.get("Content-Length")
            cap = getattr(sink, "max", None)
            if length and cap is not None and int(length) > cap:
                raise RemoteError(f"The file is larger than the {cap // CHUNK} MB limit (options.max_mb).")
            while True:
                data = r.read(CHUNK)
                if not data:
                    break
                sink.write(data)

    def upload_file(self, local, rel):
        raise RemoteError("An HTTP location is only read.")

    def rename(self, src, dst, replace=False):
        raise RemoteError("An HTTP location is only read.")

    def delete(self, rel):
        raise RemoteError("An HTTP location is only read.")

    def put_bytes(self, rel, data):
        raise RemoteError("An HTTP location is only read.")

    def close(self):
        pass
