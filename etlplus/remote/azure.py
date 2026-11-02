"""Azure Blob Storage through azure-storage-blob. A SAS token or an account key from secrets; an
account URL with a token in it is refused at save. Uploads go straight to the blob (a PUT is
atomic) and <blob>.done is written after."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from . import RemoteError, RemoteFile, iso_utc, options, paths


class AzureLocation:
    can_rename = False

    def __init__(self, project, spec, log=None):
        self.spec = spec
        self.log = log
        self.container = None
        url = str(spec.get("account_url") or "").strip()
        if not url:
            raise RemoteError("Set the account URL.")
        from etlplus import connections
        p = connections._url_problem("account URL", url, options(spec), need_https=True)
        if p:
            raise RemoteError(p)
        self.url = url
        self.name = str(spec.get("container") or "").strip()
        if not self.name:
            raise RemoteError("Set the container.")
        self.prefix = paths.check_rel(spec["prefix"], "prefix") if str(spec.get("prefix") or "").strip("/ ") else ""

    def _credential(self):
        secrets = dict(self.spec.get("_secrets") or {})
        auth = self.spec.get("auth") or ("account_key" if secrets.get("account_key") else "sas_token")
        if auth == "account_key" and secrets.get("account_key"):
            account = urlsplit(self.url).hostname.split(".")[0]
            return {"account_name": account, "account_key": secrets["account_key"]}
        if secrets.get("sas_token"):
            return secrets["sas_token"].lstrip("?")
        if options(self.spec).get("use_default_credentials"):
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError:
                raise RemoteError("Default credentials need azure-identity: pip install azure-identity.") from None
            return DefaultAzureCredential()
        raise RemoteError("Add keys or allow default credentials (options.use_default_credentials).")

    def connect(self):
        if self.container is not None:
            return
        from etlplus import connections
        blob = connections.driver("azure_blob")
        svc = blob.BlobServiceClient(account_url=self.url, credential=self._credential())
        self.container = svc.get_container_client(self.name)

    def _blob(self, rel):
        rel = paths.check_rel(rel)
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def iter_files(self, prefix, recursive, limit=5000):
        self.connect()
        base = self._blob(prefix) + "/" if prefix else (self.prefix + "/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        out = []
        for b in self.container.list_blobs(name_starts_with=base or None):
            name = b.name
            if name.endswith("/"):
                continue
            if not recursive and "/" in name[len(base):]:
                continue
            out.append(RemoteFile(name[strip:], int(getattr(b, "size", 0) or 0),
                                  iso_utc(getattr(b, "last_modified", None) or 0),
                                  str(getattr(b, "etag", "") or "").strip('"') or None))
            if len(out) >= limit:
                break
        return out

    def download(self, rel, sink):
        self.connect()
        stream = self.container.download_blob(self._blob(rel))
        for chunk in stream.chunks():
            sink.write(chunk)

    def upload_file(self, local, rel):
        self.connect()
        with open(local, "rb") as fh:
            self.container.upload_blob(self._blob(rel), fh, overwrite=True)

    def rename(self, src, dst, replace=False):
        self.connect()
        source = self.container.get_blob_client(self._blob(src))
        target = self.container.get_blob_client(self._blob(dst))
        target.start_copy_from_url(source.url)
        for _ in range(120):
            props = target.get_blob_properties()
            status = getattr(getattr(props, "copy", None), "status", "success")
            if status in (None, "success"):
                break
            if status in ("failed", "aborted"):
                raise RemoteError(f"Copying {src} to {dst} failed.")
            time.sleep(0.5)
        else:
            raise RemoteError(f"Copying {src} to {dst} did not finish in 60 s.")
        self.container.delete_blob(self._blob(src))

    def delete(self, rel):
        self.connect()
        self.container.delete_blob(self._blob(rel))

    def put_bytes(self, rel, data):
        self.connect()
        self.container.upload_blob(self._blob(rel), data, overwrite=True)

    def close(self):
        self.container = None
