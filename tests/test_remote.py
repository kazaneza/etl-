"""Remote locations (SPEC_V2 section 4): share, HTTP, S3 (botocore Stubber), SFTP and Azure (stubs),
pickup rules, after-actions, uploads and every remote safety rule of section 4.6."""

import datetime as dt
import hashlib
import http.server
import io
import os
import threading
import time
import types
from pathlib import Path

import pyarrow as pa
import pytest

from etlplus import config, connections, remote, secrets, sources
from etlplus.remote import RemoteError, RemoteFile, paths

PLANTED = "planted-Secret-9f3e"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("ETLPLUS_SECRET_KEY", raising=False)
    monkeypatch.delenv("ETLPLUS_SHARE_ROOTS", raising=False)
    return config.Project.init(tmp_path / "proj")


@pytest.fixture
def share(tmp_path, project):
    root = tmp_path / "share"
    (root / "in" / "sub").mkdir(parents=True)
    (root / "in" / "a_1.csv").write_text("id,v\n1,x\n2,y\n", encoding="utf-8")
    (root / "in" / "a_2.csv").write_text("id,v\n3,z\n", encoding="utf-8")
    (root / "in" / "b.txt").write_text("nothing", encoding="utf-8")
    (root / "in" / "sub" / "a_3.csv").write_text("id,v\n4,w\n", encoding="utf-8")
    (root / "in" / ".hidden.csv").write_text("id\n9\n", encoding="utf-8")
    (root / "in" / "a_9.csv.partial").write_text("id\n9\n", encoding="utf-8")
    old = time.time() - 3600
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    project.save_connection("sh", {"kind": "share", "path": str(root), "role": "source"})
    return root


def _logs():
    lines = []

    def log(msg):
        lines.append(str(msg))
    return lines, log


# ---------------------------------------------------------------- share

def test_share_list_by_pattern(project, share):
    names = [f.path for f in remote.list_files(project, "sh", "in/a_*.csv")]
    assert names == ["in/a_1.csv", "in/a_2.csv"]
    names = [f.path for f in remote.list_files(project, "sh", "**/*.csv")]
    assert names == ["in/a_1.csv", "in/a_2.csv", "in/sub/a_3.csv"]
    names = [f.path for f in remote.list_files(project, "sh", ["in/b.txt", "in/a_2.csv"])]
    assert names == ["in/a_2.csv", "in/b.txt"]
    f = remote.list_files(project, "sh", "in/a_1.csv")[0]
    assert f.size == (share / "in" / "a_1.csv").stat().st_size and f.modified.endswith("Z")
    lst = remote.list_files(project, "sh", "**/*", limit=2)
    assert len(lst) == 2 and lst.truncated
    assert [f["path"] for f in connections.list_files(project, "sh", "in/*.txt")] == ["in/b.txt"]


def test_share_fetch_partial_then_rename(project, share, tmp_path, monkeypatch):
    from etlplus.remote import share as share_mod
    seen = {}
    orig = share_mod.ShareLocation.download

    def spy(self, rel, sink):
        orig(self, rel, sink)
        seen[rel] = sorted(p.name for p in (tmp_path / "incoming").iterdir())

    monkeypatch.setattr(share_mod.ShareLocation, "download", spy)
    files = remote.list_files(project, "sh", "in/**/a_*.csv")
    got = remote.fetch(project, "sh", files, tmp_path / "incoming")
    assert seen["in/a_1.csv"] == ["in__a_1.csv.partial"]
    assert sorted(p.name for p in (tmp_path / "incoming").iterdir()) == \
        ["in__a_1.csv", "in__a_2.csv", "in__sub__a_3.csv"]
    rec = got[0]
    data = (tmp_path / "incoming" / rec["local"]).read_bytes()
    assert rec["sha256"] == hashlib.sha256(data).hexdigest() and rec["size"] == len(data)
    assert rec["remote"] == "in/a_1.csv" and rec["modified"] == files[0].modified
    with pytest.raises(RemoteError, match="never overwritten"):
        remote.fetch(project, "sh", files[:1], tmp_path / "incoming")


def test_fetch_size_mismatch_leaves_nothing(project, share, tmp_path):
    f = remote.list_files(project, "sh", "in/a_1.csv")[0]
    f.size += 10
    with pytest.raises(RemoteError, match="bytes"):
        remote.fetch(project, "sh", [f], tmp_path / "inc")
    assert list((tmp_path / "inc").iterdir()) == []


def test_pickup_min_age_skip_seen_max_files(project, share, monkeypatch):
    now = time.time()
    os.utime(share / "in" / "a_2.csv", (now, now))
    listed = remote.list_files(project, "sh", "in/a_*.csv")
    lines, log = _logs()
    got = sources.apply_pickup(project, "j", "sh", listed, {"min_age_seconds": 60}, log)
    assert [f.path for f in got] == ["in/a_1.csv"] and any("younger" in x for x in lines)
    from etlplus import store
    monkeypatch.setattr(store, "file_seen", lambda p, job, cid, path, size, mod: path == "in/a_1.csv",
                        raising=False)
    got = sources.apply_pickup(project, "j", "sh", listed, {"min_age_seconds": 0})
    assert [f.path for f in got] == ["in/a_2.csv"]
    got = sources.apply_pickup(project, "j", "sh", listed, {"min_age_seconds": 0, "skip_seen": False,
                                                             "max_files": 1})
    assert len(got) == 1


def test_sources_read_share_into_incoming(project, share, tmp_path, monkeypatch):
    from etlplus import store
    monkeypatch.setattr(store, "file_seen", lambda *a: False, raising=False)
    job = {"name": "pick", "source": {"connection": "sh", "files": "in/a_*.csv", "format": "csv",
                                      "options": {"filename_column": "src"}}}
    inc = tmp_path / "run" / "incoming"
    files_out = []
    t = pa.Table.from_batches(list(sources.read(project, job, None, None, fetch_dir=inc, files_out=files_out)))
    assert sorted(t.column("id").to_pylist()) == [1, 2, 3]
    assert set(t.column("src").to_pylist()) == {"in/a_1.csv", "in/a_2.csv"}
    assert sorted(r["remote"] for r in files_out) == ["in/a_1.csv", "in/a_2.csv"]
    assert all(r["connection"] == "sh" and len(r["sha256"]) == 64 for r in files_out)
    assert sorted(p.name for p in inc.iterdir()) == ["in__a_1.csv", "in__a_2.csv"]
    monkeypatch.setattr(store, "file_seen", lambda *a: True, raising=False)
    with pytest.raises(FileNotFoundError, match="No new file"):
        list(sources.read(project, job, None, None, fetch_dir=tmp_path / "run2"))
    # a preview fetches into a temp folder under the stage and removes it
    assert connections.preview(project, "sh", "in/a_1.csv", "csv")["rows"] == [[1, "x"], [2, "y"]]
    assert not list((project.stage_dir() / ".tmp").glob("*"))


# ---------------------------------------------------------------- after-actions and uploads

def test_finish_keep_archive_mark_done(project, share, tmp_path):
    files = remote.list_files(project, "sh", "in/a_*.csv")
    fetched = remote.fetch(project, "sh", files, tmp_path / "inc")
    remote.finish(project, "sh", fetched, "keep")
    for action in ("archive", "mark_done", "delete"):
        with pytest.raises(RemoteError, match="allow_after_actions"):
            remote.finish(project, "sh", fetched, action, "done")
    assert (share / "in" / "a_1.csv").exists()
    project.save_connection("sh", {"kind": "share", "path": str(share), "role": "source",
                                   "options": {"allow_after_actions": True}})
    remote.finish(project, "sh", fetched[:1], "mark_done")
    assert (share / "in" / "a_1.csv.done").exists() and (share / "in" / "a_1.csv").exists()
    remote.finish(project, "sh", fetched[:1], "archive", "done")
    assert (share / "done" / "in" / "a_1.csv").exists() and not (share / "in" / "a_1.csv").exists()
    with pytest.raises(RemoteError):
        remote.finish(project, "sh", fetched[1:], "archive", "../outside")
    with pytest.raises(RemoteError):
        remote.finish(project, "sh", fetched[1:], "archive", None)
    assert (share / "in" / "a_2.csv").exists()


def test_delete_only_with_both_settings(project, share, tmp_path):
    fetched = remote.fetch(project, "sh", remote.list_files(project, "sh", "in/a_2.csv"), tmp_path / "inc")
    with pytest.raises(RemoteError):
        remote.finish(project, "sh", fetched, "delete")
    assert (share / "in" / "a_2.csv").exists()
    spec = connections.spec_for(project, "sh")
    assert remote.check_after(spec, "delete") == remote.NOT_ALLOWED
    project.save_connection("sh", {"kind": "share", "path": str(share), "options": {"allow_after_actions": True}})
    remote.finish(project, "sh", fetched, "delete")
    assert not (share / "in" / "a_2.csv").exists()


def test_upload_partial_then_rename(project, share, tmp_path, monkeypatch):
    project.save_connection("out", {"kind": "share", "path": str(share), "role": "target"})
    local = tmp_path / "x.parquet"
    local.write_bytes(b"PAR1data")
    from etlplus.remote import share as share_mod
    calls = []
    orig = share_mod.ShareLocation.rename

    def spy(self, src, dst, replace=False):
        calls.append((src, dst, (share / src).exists(), (share / dst).exists()))
        orig(self, src, dst, replace)

    monkeypatch.setattr(share_mod.ShareLocation, "rename", spy)
    remote.upload(project, "out", local, "export/x.parquet")
    assert calls == [("export/x.parquet.partial", "export/x.parquet", True, False)]
    assert (share / "export" / "x.parquet").read_bytes() == b"PAR1data"
    with pytest.raises(RemoteError, match="source"):
        remote.upload(project, "sh", local, "y.parquet")
    with pytest.raises(ValueError):
        remote.upload(project, "out", local, "../y.parquet")


# ---------------------------------------------------------------- names, paths, home

@pytest.mark.parametrize("bad", ["/etc/passwd", "C:/x", "c:x", "\\\\server\\share\\x", "a\\b", "../x", "a/../b",
                                 "a//b", "a\x00b", "a\nb", "", "  "])
def test_check_rel_refuses(bad):
    with pytest.raises(ValueError):
        paths.check_rel(bad)


def test_local_names_and_collisions(project, share, tmp_path):
    assert paths.local_name("a/b.txt", tmp_path) == "a__b.txt"
    (share / "in" / "x").mkdir()
    (share / "in" / "x" / "y.csv").write_text("1", encoding="utf-8")
    (share / "in" / "x__y.csv").write_text("2", encoding="utf-8")
    files = [RemoteFile("in/x/y.csv", 1, ""), RemoteFile("in/x__y.csv", 1, "")]
    with pytest.raises(RemoteError, match="both be saved"):
        remote.fetch(project, "sh", files, tmp_path / "c")
    assert not (tmp_path / "c" / "in__x__y.csv").exists()


def test_share_path_inside_home_refused(project, tmp_path, monkeypatch):
    for path in (str(project.path), str(project.path / "stage"), str(tmp_path)):
        with pytest.raises(ValueError):
            paths.share_root(project, path)
    with pytest.raises(ValueError, match="absolute"):
        paths.share_root(project, "relative/folder")
    project.save_connection("bad", {"kind": "share", "path": str(project.path)})
    r = connections.test(project, "bad")
    assert not r["ok"] and r["steps"][0]["name"] == "settings"
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("ETLPLUS_SHARE_ROOTS", str(tmp_path / "allowed"))
    with pytest.raises(ValueError, match="ETLPLUS_SHARE_ROOTS"):
        paths.share_root(project, str(other))
    (tmp_path / "allowed" / "in").mkdir(parents=True)
    assert paths.share_root(project, str(tmp_path / "allowed" / "in")).name == "in"


def test_home_layout_protects_the_other_project(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "projects.yaml").write_text("active: demo\nprojects:\n  demo: {path: demo}\n  real: {path: real}\n",
                                        encoding="utf-8")
    demo = config.Project.init(home / "demo")
    real = config.Project.init(home / "real")
    (real.path / "data").mkdir()
    with pytest.raises(ValueError, match="other etl\\+ project"):
        paths.check_local(demo, real.path / "data" / "x.csv")
    with pytest.raises(ValueError):
        paths.share_root(demo, str(home))
    # a legacy home: the project at the root, the other one in projects/<id>
    legacy = config.Project.init(tmp_path / "legacy")
    (legacy.path / "projects.yaml").write_text("active: real\nprojects:\n  real: {path: .}\n  demo: "
                                               "{path: projects/demo}\n", encoding="utf-8")
    other = config.Project.init(legacy.path / "projects" / "demo")
    with pytest.raises(ValueError):
        paths.check_local(legacy, other.path / "secrets.json.enc")
    with pytest.raises(ValueError):
        paths.check_local(legacy, other.path / "landing" / "a.csv")
    legacy.save_connection("all", {"kind": "folder", "path": "projects"})
    with pytest.raises(connections.ConnectionError_):
        connections.file_path(legacy, connections.spec_for(legacy, "all"))


def test_folder_never_reads_secret_or_state_files(project):
    project.save_connection("root", {"kind": "folder", "path": "."})
    (project.path / "runs.sqlite").write_bytes(b"x")
    for name in ("runs.sqlite", "secrets.json.enc", "*.sqlite"):
        if name == "secrets.json.enc":
            secrets.set(project, "x", {"password": "p"})
        with pytest.raises(ValueError):
            list(sources.read_files(project, "root", name, "csv"))
    with pytest.raises(FileNotFoundError):
        list(sources.read_files(project, "root", ".secret.key", "csv"))
    project.save_connection("db", {"kind": "sqlite", "path": "runs.sqlite"})
    with pytest.raises(connections.ConnectionError_):
        connections.connect(project, "db")


def test_share_listing_does_not_follow_symlinks_out(project, share, tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("id\n1\n", encoding="utf-8")
    try:
        os.symlink(outside, share / "in" / "link.csv")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert "in/link.csv" not in [f.path for f in remote.list_files(project, "sh", "in/*.csv")]


# ---------------------------------------------------------------- templates

def test_template_filler():
    d = dt.date(2026, 10, 31)
    assert paths.fill_template("r_{date}.csv", date=d) == "r_2026-10-31.csv"
    assert paths.fill_template("r_{date:%Y%m%d}.csv", date=d) == "r_20261031.csv"
    assert paths.fill_template("{job}_{run_id}.{format}", job="j", run_id="r1", format="csv", safe_name=True) \
        == "j_r1.csv"
    for bad in ("{date.__class__}", "{date:%Y.__class__}", "{date!r}", "{0}", "{__import__}", "x{", "{date:{x}}",
                "{date:%Y/%m}"):
        with pytest.raises(ValueError):
            paths.fill_template(bad, date=d)
    with pytest.raises(ValueError):
        paths.fill_template("{job}", job="../x", safe_name=True)
    with pytest.raises(ValueError):
        paths.fill_template("a/b", safe_name=True)
    with pytest.raises(ValueError):
        paths.fill_template("{run_id}", allowed=("date",), run_id="x")


# ---------------------------------------------------------------- http

class _Handler(http.server.BaseHTTPRequestHandler):
    files = {}
    seen_headers = []

    def log_message(self, *a):
        pass

    def _send(self, body_only=False):
        _Handler.seen_headers.append(dict(self.headers))
        if self.path.startswith("/loop"):
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        body = _Handler.files.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", "Sat, 31 Oct 2026 10:00:00 GMT")
        self.end_headers()
        if not body_only:
            self.wfile.write(body)

    def do_GET(self):
        self._send()

    def do_HEAD(self):
        self._send(body_only=True)


@pytest.fixture
def web():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http_download_with_date_template(project, web, tmp_path):
    today = dt.date.today()
    body = b"id,v\n1,a\n2,b\n"
    _Handler.files = {f"/files/report_{today:%Y%m%d}.csv": body}
    project.save_connection("web", {"kind": "http", "url": web + "/files/report_{date:%Y%m%d}.csv?k=1",
                                    "header_name": "X-Token", "options": {"allow_private": True}})
    secrets.update(project, "web", {"header_value": PLANTED})
    lines, log = _logs()
    files = remote.list_files(project, "web", "*", log=log)
    assert [f.path for f in files] == [f"report_{today:%Y%m%d}.csv"] and files[0].size == len(body)
    got = remote.fetch(project, "web", files, tmp_path / "inc", log=log)
    assert (tmp_path / "inc" / got[0]["local"]).read_bytes() == body
    assert any(h.get("X-Token") == PLANTED for h in _Handler.seen_headers)
    assert not any(PLANTED in x for x in lines) and not any("k=1" in x for x in lines)
    job = {"name": "w", "source": {"connection": "web", "format": "csv", "pickup": {"min_age_seconds": 0}}}
    t = pa.Table.from_batches(list(sources.read(project, job, None, None, fetch_dir=tmp_path / "r")))
    assert t.column("id").to_pylist() == [1, 2]


def test_http_private_targets_refused(project, web):
    for url in (web + "/x.csv", "http://169.254.169.254/latest/meta-data", "http://[::1]/x",
                "http://0.0.0.0/x", "http://example.test:8300/x"):
        project.save_connection("w", {"kind": "http", "url": url})
        with pytest.raises(RemoteError):
            remote.list_files(project, "w", "*")


def test_http_redirect_rules_and_size_cap(project, web, tmp_path):
    from etlplus.remote import http as http_mod
    h = http_mod.redirect_handler("x-token")
    import urllib.request
    req = urllib.request.Request("https://a.example.test/x", headers={"X-token": "t"})
    with pytest.raises(RemoteError, match="https to http"):
        h.redirect_request(req, None, 302, "Found", {}, "http://a.example.test/y")
    with pytest.raises(RemoteError):
        h.redirect_request(req, None, 302, "Found", {}, "https://169.254.169.254/y")
    assert http_mod.redirect_handler().max_redirections == 5
    project.save_connection("loop", {"kind": "http", "url": web + "/loop", "options": {"allow_private": True}})
    with pytest.raises(RemoteError, match="redirect"):
        remote.fetch(project, "loop", [RemoteFile("loop", 0, "")], tmp_path / "a")
    _Handler.files = {"/big.bin": b"x" * 5000}
    project.save_connection("big", {"kind": "http", "url": web + "/big.bin",
                                    "options": {"allow_private": True, "max_mb": 0.001}})
    with pytest.raises(RemoteError, match="limit"):
        remote.fetch(project, "big", [RemoteFile("big.bin", 0, "")], tmp_path / "b")
    assert list((tmp_path / "b").iterdir()) == []


# ---------------------------------------------------------------- s3 (botocore Stubber)

def test_s3_through_stubber(project, monkeypatch, tmp_path):
    boto3 = pytest.importorskip("boto3")
    from botocore.response import StreamingBody
    from botocore.stub import ANY, Stubber
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="AK", aws_secret_access_key="SK")
    stub = Stubber(client)
    made = {}

    def fake_client(name, **kw):
        made.update(kw)
        return client

    monkeypatch.setattr(connections, "driver", lambda kind: types.SimpleNamespace(client=fake_client))
    project.save_connection("s3", {"kind": "s3", "bucket": "demo-bucket", "prefix": "in", "region": "us-east-1",
                                   "access_key_id": "AKID", "role": "both",
                                   "options": {"allow_after_actions": True}})
    secrets.update(project, "s3", {"secret_access_key": PLANTED})
    when = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
    stub.add_response("list_objects_v2", {"IsTruncated": False, "KeyCount": 3, "Contents": [
        {"Key": "in/stmt/a.csv", "Size": 8, "LastModified": when, "ETag": '"e1"'},
        {"Key": "in/stmt/../../evil.csv", "Size": 8, "LastModified": when},
        {"Key": "in/stmt/b.txt", "Size": 3, "LastModified": when}]},
        {"Bucket": "demo-bucket", "Prefix": "in/stmt/", "MaxKeys": ANY, "Delimiter": "/"})
    with stub:
        lines, log = _logs()
        files = remote.list_files(project, "s3", "stmt/*.csv", log=log)
        assert [f.path for f in files] == ["stmt/a.csv"] and files[0].etag == "e1"
        assert any("unsafe" in x for x in lines)
        assert made["aws_secret_access_key"] == PLANTED and made["aws_access_key_id"] == "AKID"
        body = b"id\n1\n2\n"
        stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(body), len(body)),
                                         "ContentLength": len(body)}, {"Bucket": "demo-bucket", "Key": "in/stmt/a.csv"})
        files[0].size = len(body)
        got = remote.fetch(project, "s3", files, tmp_path / "inc", log=log)
        assert (tmp_path / "inc" / "stmt__a.csv").read_bytes() == body and got[0]["sha256"]
        stub.add_response("copy_object", {}, {"Bucket": "demo-bucket", "Key": "in/done/stmt/a.csv",
                                              "CopySource": {"Bucket": "demo-bucket", "Key": "in/stmt/a.csv"}})
        stub.add_response("delete_object", {}, {"Bucket": "demo-bucket", "Key": "in/stmt/a.csv"})
        stub.add_response("put_object", {}, {"Bucket": "demo-bucket", "Key": "in/out/x.csv", "Body": ANY})
        stub.add_response("put_object", {}, {"Bucket": "demo-bucket", "Key": "in/out/x.csv.done", "Body": b""})
        remote.finish(project, "s3", got, "archive", "done", log=log)
        local = tmp_path / "x.csv"
        local.write_bytes(b"a\n")
        remote.upload(project, "s3", local, "out/x.csv", log=log)
    stub.assert_no_pending_responses()
    assert not any(PLANTED in x for x in lines)


def test_s3_needs_keys_and_https(project, monkeypatch):
    monkeypatch.setattr(connections, "driver", lambda kind: types.SimpleNamespace(client=lambda *a, **k: None))
    project.save_connection("s", {"kind": "s3", "bucket": "b"})
    with pytest.raises(RemoteError, match="default credentials"):
        remote.list_files(project, "s", "*")
    project.save_connection("s", {"kind": "s3", "bucket": "b", "endpoint_url": "http://minio.example.test:9000"})
    with pytest.raises(RemoteError, match="https"):
        remote.list_files(project, "s", "*")
    assert connections.validate_spec({"kind": "s3", "bucket": "b", "endpoint_url": "http://m.example.test",
                                      "options": {"allow_http": True}}) == []


# ---------------------------------------------------------------- sftp (paramiko stub)

class _Key:
    def __init__(self, data=b"server-key-bytes"):
        self.data = data

    def asbytes(self):
        return self.data


class _Attr:
    def __init__(self, name, mode, size=4, mtime=1_790_000_000):
        self.filename, self.st_mode, self.st_size, self.st_mtime = name, mode, size, mtime


class _FakeSFTP:
    def __init__(self, tree):
        self.tree = tree
        self.ops = []

    def listdir_attr(self, d):
        return self.tree.get(d.rstrip("/") or "/", [])

    def open(self, path, mode="rb"):
        self.ops.append(("open", path, mode))
        return io.BytesIO(b"data") if "r" in mode else _W(self.ops, path)

    def putfo(self, fh, path):
        self.ops.append(("putfo", path))

    def stat(self, p):
        return True

    def mkdir(self, p):
        pass

    def posix_rename(self, a, b):
        self.ops.append(("rename", a, b))

    def rename(self, a, b):
        self.ops.append(("rename", a, b))

    def remove(self, p):
        self.ops.append(("remove", p))

    def close(self):
        pass


class _W(io.BytesIO):
    def __init__(self, ops, path):
        super().__init__()
        ops.append(("write", path))


def _paramiko(tree, key=b"server-key-bytes"):
    state = {"auth": [], "closed": 0}
    sftp = _FakeSFTP(tree)

    class Transport:
        def __init__(self, addr):
            state["addr"] = addr

        def start_client(self, timeout=None):
            state["started"] = True

        def get_remote_server_key(self):
            return _Key(key)

        def auth_password(self, user, pw):
            state["auth"].append(("password", user, pw))

        def auth_publickey(self, user, pkey):
            state["auth"].append(("key", user, pkey))

        def close(self):
            state["closed"] += 1

    class RSAKey:
        @staticmethod
        def from_private_key(fh, password=None):
            text = fh.read()
            if "PRIVATE KEY" not in text:
                raise ValueError("bad")
            return ("rsa", password)

    mod = types.SimpleNamespace(Transport=Transport, RSAKey=RSAKey,
                                SFTPClient=types.SimpleNamespace(from_transport=lambda t: sftp))
    return mod, state, sftp


def _fp(data=b"server-key-bytes"):
    from etlplus.remote.sftp import fingerprint
    return fingerprint(data)


def test_sftp_list_fetch_and_traversal_names(project, monkeypatch, tmp_path):
    import stat as st
    tree = {"/in": [_Attr("a.csv", st.S_IFREG), _Attr("../evil.csv", st.S_IFREG), _Attr("x\\y.csv", st.S_IFREG),
                    _Attr("link.csv", st.S_IFLNK), _Attr("sub", st.S_IFDIR)],
            "/in/sub": [_Attr("b.csv", st.S_IFREG)]}
    mod, state, sftp = _paramiko(tree)
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    project.save_connection("sf", {"kind": "sftp", "host": "sftp.example.test", "user": "u", "path": "/in",
                                   "host_key_fingerprint": _fp()})
    secrets.update(project, "sf", {"password": PLANTED})
    lines, log = _logs()
    files = remote.list_files(project, "sf", "**/*.csv", log=log)
    assert [f.path for f in files] == ["a.csv", "sub/b.csv"]
    assert state["auth"] == [("password", "u", PLANTED)]
    got = remote.fetch(project, "sf", files[:1], tmp_path / "inc")
    assert (tmp_path / "inc" / "a.csv").read_bytes() == b"data" and got[0]["size"] == 4
    assert not any(PLANTED in x for x in lines)


def test_sftp_fingerprint_mismatch_before_auth(project, monkeypatch):
    mod, state, _ = _paramiko({}, key=b"another-key")
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    project.save_connection("sf", {"kind": "sftp", "host": "h.example.test", "user": "u",
                                   "host_key_fingerprint": _fp()})
    secrets.update(project, "sf", {"password": PLANTED})
    with pytest.raises(RemoteError, match="host key does not match"):
        remote.list_files(project, "sf", "*")
    assert state["auth"] == [] and state["closed"] >= 1


def test_sftp_never_auto_accepts(project, monkeypatch):
    src = Path(remote.__file__).with_name("sftp.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith(("#", '"""')))
    for word in ("set_missing_host_key_policy", "load_host_keys", "load_system_host_keys", "SSHClient("):
        assert word not in code
    # the stub has no AutoAddPolicy or SSHClient: the code works without them
    mod, state, _ = _paramiko({})
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    project.save_connection("sf", {"kind": "sftp", "host": "h.example.test", "user": "u"})
    r = connections.test(project, "sf")
    assert not r["ok"] and state["auth"] == []
    assert any(s["name"] == "scan" and _fp() in s["detail"] for s in r["steps"])
    assert connections.validate_spec({"kind": "sftp", "host_key_fingerprint": "md5:aa"})


def test_sftp_private_key_in_memory(project, monkeypatch, tmp_path):
    mod, state, _ = _paramiko({"/": []})
    monkeypatch.setattr(connections, "driver", lambda kind: mod)
    project.save_connection("sf", {"kind": "sftp", "host": "h.example.test", "user": "u", "path": "/",
                                   "host_key_fingerprint": _fp()})
    secrets.update(project, "sf", {"private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n", "passphrase": "pp"})
    before = set(tmp_path.rglob("*"))
    remote.list_files(project, "sf", "*")
    assert state["auth"] == [("key", "u", ("rsa", "pp"))]
    assert set(tmp_path.rglob("*")) - before <= set(project.path.rglob("*"))


# ---------------------------------------------------------------- azure (stub)

def test_azure_through_stub(project, monkeypatch, tmp_path):
    blobs = {"in/a.csv": b"id\n1\n", "in/x/../../evil": b"", "in/b.txt": b"t"}
    ops = []

    class Blob:
        def __init__(self, name):
            self.name, self.size = name, len(blobs.get(name, b""))
            self.last_modified = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
            self.etag = '"e"'
            self.url = "https://acct.blob.example.test/c/" + name

        def start_copy_from_url(self, url):
            ops.append(("copy", url, self.name))

        def get_blob_properties(self):
            return types.SimpleNamespace(copy=types.SimpleNamespace(status="success"))

    class Container:
        def list_blobs(self, name_starts_with=None):
            return [Blob(n) for n in blobs if n.startswith(name_starts_with or "")]

        def download_blob(self, name):
            return types.SimpleNamespace(chunks=lambda: iter([blobs[name]]))

        def upload_blob(self, name, data, overwrite=False):
            ops.append(("upload", name, overwrite))

        def delete_blob(self, name):
            ops.append(("delete", name))

        def get_blob_client(self, name):
            return Blob(name)

    seen = {}

    class Service:
        def __init__(self, account_url, credential):
            seen["url"], seen["cred"] = account_url, credential

        def get_container_client(self, name):
            return Container()

    monkeypatch.setattr(connections, "driver", lambda kind: types.SimpleNamespace(BlobServiceClient=Service))
    project.save_connection("az", {"kind": "azure_blob", "account_url": "https://acct.blob.example.test",
                                   "container": "c", "auth": "sas_token", "role": "both",
                                   "options": {"allow_after_actions": True}})
    secrets.update(project, "az", {"sas_token": "?sv=1&sig=" + PLANTED})
    files = remote.list_files(project, "az", "in/*.csv")
    assert [f.path for f in files] == ["in/a.csv"] and seen["cred"].startswith("sv=1")
    got = remote.fetch(project, "az", files, tmp_path / "inc")
    assert (tmp_path / "inc" / "in__a.csv").read_bytes() == b"id\n1\n"
    remote.finish(project, "az", got, "mark_done")
    local = tmp_path / "u.csv"
    local.write_bytes(b"x")
    remote.upload(project, "az", local, "out/u.csv")
    assert ("upload", "in/a.csv.done", True) in ops and ("upload", "out/u.csv", True) in ops \
        and ("upload", "out/u.csv.done", True) in ops
    remote.finish(project, "az", got, "archive", "done")
    assert ("delete", "in/a.csv") in ops and any(o[0] == "copy" and o[2] == "done/in/a.csv" for o in ops)
    project.save_connection("az2", {"kind": "azure_blob", "account_url": "https://acct.blob.example.test",
                                    "container": "c"})
    with pytest.raises(RemoteError, match="default credentials"):
        remote.list_files(project, "az2", "*")


# ---------------------------------------------------------------- secrets and endpoints

def test_secrets_never_in_spec_or_logs(project, monkeypatch, share):
    project.save_connection("s3", {"kind": "s3", "bucket": "b", "access_key_id": "AK"})
    secrets.update(project, "s3", {"secret_access_key": PLANTED})
    out = connections.public_spec(project, "s3")
    assert PLANTED not in repr(out) and out["secret_access_key_set"] is True

    def boom(name, **kw):
        raise RuntimeError("could not reach the endpoint with key " + kw.get("aws_secret_access_key", ""))

    monkeypatch.setattr(connections, "driver", lambda kind: types.SimpleNamespace(client=boom))
    r = connections.test(project, "s3")
    assert not r["ok"] and PLANTED not in repr(r)
    text = (project.path / "connections.yaml").read_text(encoding="utf-8")
    assert PLANTED not in text


def test_endpoint_change_clears_secrets(project):
    project.save_connection("s3", {"kind": "s3", "bucket": "b", "access_key_id": "AK", "region": "r1"})
    secrets.update(project, "s3", {"secret_access_key": PLANTED})
    assert connections.spec_for(project, "s3")["_secrets"]["secret_access_key"] == PLANTED
    for change in ({"bucket": "other"}, {"region": "r2"}, {"endpoint_url": "https://x.example.test"},
                   {"access_key_id": "AK2"}):
        assert connections.spec_for(project, "s3", change)["_secrets"] == {}, change
    project.save_connection("az", {"kind": "azure_blob", "account_url": "https://a.blob.example.test",
                                   "container": "c"})
    secrets.update(project, "az", {"sas_token": PLANTED})
    assert connections.spec_for(project, "az", {"container": "d"})["_secrets"] == {}
    assert connections.spec_for(project, "az", {"prefix": "p"})["_secrets"]["sas_token"] == PLANTED
    project.save_connection("w", {"kind": "http", "url": "https://a.example.test/x"})
    secrets.update(project, "w", {"header_value": PLANTED})
    assert connections.spec_for(project, "w", {"url": "https://b.example.test/x"})["_secrets"] == {}
    saved = project.connections["s3"]
    assert connections.prepare_save(dict(saved, bucket="z"), saved)["clear"] is True
    assert connections.prepare_save(dict(saved, prefix="p"), saved)["clear"] is False


def test_validate_spec_urls():
    assert connections.validate_spec({"kind": "azure_blob", "account_url": "https://a.blob.example.test/?sig=x"})
    assert connections.validate_spec({"kind": "http", "url": "https://a.example.test/x?api_key=1"})
    assert connections.validate_spec({"kind": "http", "url": "https://a.example.test/{date.__class__}"})
    assert connections.validate_spec({"kind": "http", "url": "ftp://a.example.test/x"})
    assert connections.validate_spec({"kind": "ftp", "host": "h", "tls": False})
    assert not connections.validate_spec({"kind": "ftp", "host": "h", "tls": False, "options": {"allow_plain": True}})
    assert not connections.validate_spec({"kind": "http", "url": "https://a.example.test/r_{date:%Y%m%d}.csv"})


def test_ftp_through_fake(project, monkeypatch, tmp_path):
    ops = []

    class FTP:
        def __init__(self, context=None, timeout=None):
            ops.append(("ctx", context is not None))

        def connect(self, host, port):
            ops.append(("connect", host, port))

        def auth(self):
            ops.append(("auth",))

        def login(self, user, pw):
            ops.append(("login", user, pw))

        def prot_p(self):
            ops.append(("prot_p",))

        def set_pasv(self, v):
            ops.append(("pasv", v))

        def mlsd(self, d, facts=None):
            return iter([("a.csv", {"type": "file", "size": "3", "modify": "20261030100000"}),
                         ("..", {"type": "pdir"}), ("x/../y", {"type": "file", "size": "1"})])

        def retrbinary(self, cmd, cb, blocksize=None):
            ops.append(("retr", cmd))
            cb(b"abc")

        def quit(self):
            pass

    fake = types.SimpleNamespace(FTP_TLS=FTP, FTP=FTP)
    monkeypatch.setattr(connections, "driver", lambda kind: fake)
    project.save_connection("f", {"kind": "ftp", "host": "ftp.example.test", "user": "u", "path": "/in"})
    secrets.update(project, "f", {"password": PLANTED})
    files = remote.list_files(project, "f", "*.csv")
    assert [f.path for f in files] == ["a.csv"]
    remote.fetch(project, "f", files, tmp_path / "inc")
    assert ("auth",) in ops and ("prot_p",) in ops and ("retr", "RETR /in/a.csv") in ops
    assert ops.index(("auth",)) < ops.index(("login", "u", PLANTED)) < ops.index(("prot_p",))
    project.save_connection("p", {"kind": "ftp", "host": "ftp.example.test", "tls": False})
    with pytest.raises(RemoteError, match="allow_plain"):
        remote.list_files(project, "p", "*")
