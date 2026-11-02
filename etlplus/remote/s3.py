"""S3 and S3-compatible stores (MinIO) through boto3. Keys come from secrets only; the default
credential chain is used only with options.use_default_credentials. A PUT is atomic, so an upload
goes straight to its key and <key>.done is written after it."""

from __future__ import annotations

from . import CHUNK, RemoteError, RemoteFile, iso_utc, options, paths


class S3Location:
    can_rename = False

    def __init__(self, project, spec, log=None):
        self.spec = spec
        self.log = log
        self.client = None
        self.bucket = str(spec.get("bucket") or "").strip()
        if not self.bucket:
            raise RemoteError("Set the bucket.")
        self.prefix = paths.check_rel(spec["prefix"], "prefix") if str(spec.get("prefix") or "").strip("/ ") else ""
        url = spec.get("endpoint_url")
        if url:
            from etlplus import connections
            p = connections._url_problem("endpoint URL", url, options(spec), need_https=True)
            if p:
                raise RemoteError(p)

    def connect(self):
        if self.client is not None:
            return
        from etlplus import connections
        boto3 = connections.driver("s3")
        secret = (self.spec.get("_secrets") or {}).get("secret_access_key")
        key_id = self.spec.get("access_key_id")
        kw = {}
        if self.spec.get("endpoint_url"):
            kw["endpoint_url"] = self.spec["endpoint_url"]
        if self.spec.get("region"):
            kw["region_name"] = self.spec["region"]
        if key_id and secret:
            kw["aws_access_key_id"] = key_id
            kw["aws_secret_access_key"] = secret
        elif key_id or secret:
            raise RemoteError("Set both the access key id and the secret access key.")
        elif not options(self.spec).get("use_default_credentials"):
            raise RemoteError("Add keys or allow default credentials (options.use_default_credentials).")
        self.client = boto3.client("s3", **kw)

    def _key(self, rel):
        rel = paths.check_rel(rel)
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def iter_files(self, prefix, recursive, limit=5000):
        self.connect()
        base = self._key(prefix) + "/" if prefix else (self.prefix + "/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        out, token = [], None
        while len(out) < limit:
            kw = {"Bucket": self.bucket, "Prefix": base, "MaxKeys": min(1000, limit)}
            if not recursive:
                kw["Delimiter"] = "/"
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                out.append(RemoteFile(key[strip:], int(obj.get("Size") or 0), iso_utc(obj.get("LastModified")),
                                      str(obj.get("ETag") or "").strip('"') or None))
                if len(out) >= limit:
                    break
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return out

    def download(self, rel, sink):
        self.connect()
        body = self.client.get_object(Bucket=self.bucket, Key=self._key(rel))["Body"]
        try:
            while True:
                data = body.read(CHUNK)
                if not data:
                    break
                sink.write(data)
        finally:
            try:
                body.close()
            except Exception:
                pass

    def upload_file(self, local, rel):
        self.connect()
        with open(local, "rb") as fh:
            self.client.put_object(Bucket=self.bucket, Key=self._key(rel), Body=fh)

    def rename(self, src, dst, replace=False):
        self.connect()
        self.client.copy_object(Bucket=self.bucket, Key=self._key(dst),
                                CopySource={"Bucket": self.bucket, "Key": self._key(src)})
        self.client.delete_object(Bucket=self.bucket, Key=self._key(src))

    def delete(self, rel):
        self.connect()
        self.client.delete_object(Bucket=self.bucket, Key=self._key(rel))

    def put_bytes(self, rel, data):
        self.connect()
        self.client.put_object(Bucket=self.bucket, Key=self._key(rel), Body=data)

    def close(self):
        self.client = None
