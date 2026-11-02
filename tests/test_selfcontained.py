"""etl+ works for anyone who installs it: nothing is fetched from the internet at run time, the
wheel carries every console file, the home has a per-OS default, and the start-up checks speak
plainly (a home that cannot be written, a port in use, the secrets key, a missing driver)."""
from __future__ import annotations

import io
import re
import socket
import tokenize
import tomllib
from pathlib import Path

import pytest

from etlplus import __version__, cli, startup

ROOT = Path(__file__).resolve().parent.parent
_real_gai = socket.getaddrinfo
PKG = ROOT / "etlplus"
CONSOLE = PKG / "console"

URL = re.compile(r"(?:https?:)?//([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}|localhost|\d+\.\d+\.\d+\.\d+)(?::\d+)?")
# hosts that are never fetched: this machine, and the reserved example names used in help text
LOCAL = re.compile(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|.*\.example\.(com|org|net|local)|example\.(com|org|net)"
                   r"|.*\.local|.*\.invalid|.*\.test)$", re.I)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ETLPLUS_TOKEN", "ETLPLUS_START", "ETLPLUS_HOME", "ETLPLUS_PROJECT", "ETLPLUS_ALLOWED_HOSTS",
              "ETLPLUS_SECRET_KEY", "ETLPLUS_PORT", "ETLPLUS_HOST", "ETLPLUS_NO_BROWSER"):
        monkeypatch.delenv(k, raising=False)


# ------------------------------------------------------------------ no internet at run time
def _py_code_urls(path: Path) -> list[str]:
    """URLs in Python code and strings. Comments are documentation and are allowed; so are module,
    class and function docstrings."""
    out = []
    src = path.read_text(encoding="utf-8")
    prev = None
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING:
            docstring = prev in (None, tokenize.INDENT, tokenize.NEWLINE, tokenize.DEDENT) \
                and tok.string.lstrip("rbuRBU").startswith(('"""', "'''"))
            if not docstring:
                out += [f"{path.name}:{tok.start[0]}: {m.group(0)}" for m in URL.finditer(tok.string)
                        if not LOCAL.match(m.group(1))]
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev = tok.type
    return out


def _strip_web_comments(text: str, suffix: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if suffix == ".html":
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if suffix == ".js":
        text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def test_no_external_url_is_loaded_at_run_time():
    bad = []
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" not in p.parts:
            bad += _py_code_urls(p)
    for p in sorted(CONSOLE.rglob("*")):
        if p.suffix in (".js", ".html", ".css", ".json", ".svg"):
            text = _strip_web_comments(p.read_text(encoding="utf-8"), p.suffix)
            bad += [f"{p.relative_to(PKG)}: {m.group(0)}" for m in URL.finditer(text) if not LOCAL.match(m.group(1))]
            # @import or url() of anything but a file shipped beside it
            for m in re.finditer(r"@import\s+(?:url\()?['\"]?([^'\")\s;]+)", text):
                bad.append(f"{p.relative_to(PKG)}: @import {m.group(1)}")
    assert not bad, "network resources at run time:\n" + "\n".join(bad)


def test_the_check_catches_a_cdn_and_allows_a_comment(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('# docs: https://docs.python.org/3/\n'
                 'def f():\n    """See https://example.org/x."""\n'
                 '    return "https://cdn.jsdelivr.net/npm/x.js"\n', encoding="utf-8")
    found = _py_code_urls(f)
    assert len(found) == 1 and "cdn.jsdelivr.net" in found[0]
    assert URL.search('<link href="//fonts.googleapis.com/css2">').group(1) == "fonts.googleapis.com"


def test_console_references_only_shipped_files():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"#][^"]*)"', html)
    css = "".join(p.read_text(encoding="utf-8") for p in CONSOLE.rglob("*.css"))
    for p in CONSOLE.rglob("*.css"):
        for ref in re.findall(r"url\(\s*['\"]?([^'\")]+)", p.read_text(encoding="utf-8")):
            if not ref.startswith("data:"):
                assert (p.parent / ref).resolve().is_file(), f"{p.name}: {ref}"
    for ref in refs:
        assert not ref.startswith(("http:", "https:", "//")), ref
        assert (CONSOLE / ref).is_file(), f"index.html: {ref} is not shipped"
    assert css


def test_demo_and_console_need_no_network(tmp_path, monkeypatch):
    """The demo is generated here, and the console is served from the package, with every
    outbound connection refused."""
    from fastapi.testclient import TestClient
    from etlplus import api, demo

    real_connect = socket.socket.connect

    def refuse(self, addr, *a, **k):
        # loopback stays open (the event loop wakes itself through a local socket pair)
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise OSError(f"network refused in this test: {addr}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, *a, **k: (_ for _ in ()).throw(OSError(f"network refused: {addr}")))
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: (_ for _ in ()).throw(OSError(f"no DNS in this test: {host}"))
                        if host not in ("127.0.0.1", "::1", "localhost", None) else _real_gai(host, *a, **k))
    root = demo.generate(tmp_path / "demo")
    assert (Path(root) / "etlplus.yaml").exists()
    c = TestClient(api.create_app(home=tmp_path / "home", start_scheduler=False), base_url="http://localhost")
    r = c.get("/app/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert c.get("/health").json()["ok"] is True


# ------------------------------------------------------------------ the wheel carries the console
def test_package_data_covers_every_console_file():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = meta["tool"]["setuptools"]["package-data"]["etlplus"]
    covered = {p for pat in patterns for p in PKG.glob(pat) if p.is_file()}
    shipped = {p for p in CONSOLE.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert shipped and shipped <= covered, sorted(str(p.relative_to(PKG)) for p in shipped - covered)
    kinds = {p.suffix for p in shipped}
    assert {".html", ".css", ".js", ".woff2", ".png"} <= kinds


def test_one_version_everywhere():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["version"] == __version__ == "0.3.0"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"image: crestedcrane/etlplus:{__version__}" in compose


def test_compose_needs_no_env_file():
    compose = re.sub(r"(?m)^\s*#.*$", "", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert ":?" not in compose  # a required variable would refuse a plain "up"
    for var in re.findall(r"\$\{([^}]+)\}", compose):
        assert ":-" in var, f"${{{var}}} has no default"
    assert 'profiles: ["testdb"]' in compose
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in docker and "/health" in docker
    assert (ROOT / "docker" / "entrypoint.sh").is_file()


def test_offline_bundle_scripts_exist():
    for name in ("offline-bundle.sh", "offline-bundle.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "docker save" in text and "docker build" in text
        assert "LOAD_AND_RUN.md" in text and "docker-compose.yml" in text


# ------------------------------------------------------------------ the home
@pytest.mark.parametrize("platform,env,expect", [
    ("win32", {"LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}, Path(r"C:\Users\ana\AppData\Local") / "etlplus"),
    ("win32", {}, Path("/h") / "AppData" / "Local" / "etlplus"),
    ("darwin", {}, Path("/h") / "Library" / "Application Support" / "etlplus"),
    ("linux", {}, Path("/h") / ".local" / "share" / "etlplus"),
    ("linux", {"XDG_DATA_HOME": "/data/xdg"}, Path("/data/xdg") / "etlplus"),
    ("linux", {"XDG_DATA_HOME": "relative/ignored"}, Path("/h") / ".local" / "share" / "etlplus"),
])
def test_default_home_per_os(platform, env, expect):
    assert startup.default_home(platform, env, Path("/h")) == expect


def test_home_path_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(startup, "default_home", lambda *a, **k: tmp_path / "peruser")
    assert startup.home_path() == tmp_path / "peruser"            # nothing here: the per-user home
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "projects.yaml").write_text("active: null\n", encoding="utf-8")
    assert startup.home_path() == Path("project")                  # a 0.2.0 ./project is kept
    monkeypatch.setenv("ETLPLUS_PROJECT", str(tmp_path / "p"))
    assert startup.home_path() == tmp_path / "p"
    monkeypatch.setenv("ETLPLUS_HOME", str(tmp_path / "h"))
    assert startup.home_path() == tmp_path / "h"
    assert startup.home_path(str(tmp_path / "arg")) == tmp_path / "arg"


def test_writable_home_leaves_nothing(tmp_path):
    p = startup.check_writable(tmp_path / "a" / "b")
    assert p.is_dir() and list(p.iterdir()) == []


def test_unwritable_home_says_what_to_do(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(startup.StartError) as e:
        startup.check_writable(blocker / "home")
    assert "cannot write" in str(e.value) and "--home" in str(e.value)


# ------------------------------------------------------------------ the port
def _listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


def test_port_free_and_taken(monkeypatch):
    s = _listener()
    port = s.getsockname()[1]
    try:
        monkeypatch.setattr(startup, "running_etlplus", lambda *a, **k: False)
        with pytest.raises(startup.StartError) as e:
            startup.check_port("127.0.0.1", port)
        assert "already in use" in str(e.value) and f"--port {port + 1}" in str(e.value)
        monkeypatch.setattr(startup, "running_etlplus", lambda *a, **k: True)
        assert startup.check_port("127.0.0.1", port) == "etlplus"
    finally:
        s.close()
    assert startup.check_port("127.0.0.1", port) == "free"
    with pytest.raises(startup.StartError):
        startup.check_port("127.0.0.1", 70000)


def test_start_on_a_taken_port_is_a_plain_message(tmp_path, monkeypatch, capsys):
    s = _listener()
    port = s.getsockname()[1]
    served = []
    monkeypatch.setattr(startup, "running_etlplus", lambda *a, **k: False)
    monkeypatch.setattr("etlplus.api.serve", lambda *a, **k: served.append(k))
    try:
        rc = cli.main(["start", "--real", "--no-browser", "--home", str(tmp_path / "home"), "--port", str(port)])
    finally:
        s.close()
    assert rc == 1 and not served
    assert "already in use" in capsys.readouterr().err


def test_start_makes_the_home_and_serves_on_localhost(tmp_path, monkeypatch, capsys):
    served, opened = [], []
    monkeypatch.setenv("ETLPLUS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("etlplus.api.serve", lambda *a, **k: served.append(k))
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
    monkeypatch.setattr(startup, "check_port", lambda host, port: "free")
    assert cli.main(["start", "--real", "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert served and served[0]["host"] == "127.0.0.1" and served[0]["port"] == 8300
    assert (tmp_path / "home" / "projects.yaml").exists()
    assert (tmp_path / "home" / "real" / ".secret.key").exists()
    assert "Secrets key created" in out and str(tmp_path / "home") in out
    assert opened == []


def test_start_when_etlplus_already_runs_opens_it(tmp_path, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(startup, "check_port", lambda host, port: "etlplus")
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
    monkeypatch.setattr("etlplus.api.serve", lambda *a, **k: pytest.fail("must not serve twice"))
    assert cli.main(["start", "--home", str(tmp_path / "home"), "--port", "8300"]) == 0
    assert opened == ["http://127.0.0.1:8300/app/"]
    assert "already running" in capsys.readouterr().out


def test_start_opens_the_browser_by_default(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(startup, "check_port", lambda host, port: "free")
    monkeypatch.setattr("etlplus.api.serve", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_open_browser_when_ready", lambda url, h, p, **k: calls.append(url))
    assert cli.main(["start", "--real", "--home", str(tmp_path / "home"), "--port", "8311"]) == 0
    assert calls == ["http://127.0.0.1:8311/app/"]
    monkeypatch.setenv("ETLPLUS_NO_BROWSER", "1")
    calls.clear()
    assert cli.main(["start", "--home", str(tmp_path / "home"), "--port", "8311"]) == 0
    assert calls == []


def test_version_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert __version__ in capsys.readouterr().out


# ------------------------------------------------------------------ secrets key and drivers
def test_secrets_key_created_once(tmp_path):
    msg = startup.ensure_secret_key(tmp_path / "p")
    assert msg and "Secrets key created" in msg
    key = (tmp_path / "p" / ".secret.key").read_bytes()
    assert startup.ensure_secret_key(tmp_path / "p") is None
    assert (tmp_path / "p" / ".secret.key").read_bytes() == key


def test_secrets_key_from_env_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ETLPLUS_SECRET_KEY", "a passphrase")
    assert startup.ensure_secret_key(tmp_path / "p") is None
    assert not (tmp_path / "p" / ".secret.key").exists()


def test_unreadable_secrets_are_named_without_values(tmp_path, monkeypatch):
    from etlplus import config, secrets
    p = config.Project.init(tmp_path / "p")
    secrets.set(p, "core", {"password": "do-not-print-me"})
    (p.path / ".secret.key").write_bytes(b"wrong" * 8)
    msg = startup.ensure_secret_key(p)
    assert msg and msg.startswith("Saved passwords cannot be read")
    assert "do-not-print-me" not in msg


def test_missing_driver_names_the_extra(monkeypatch):
    from etlplus import drivers

    class P:
        connections = {"core": {"kind": "postgres"}, "files": {"kind": "folder"}}
    monkeypatch.setattr(drivers, "installed", lambda kind: kind != "postgres")
    lines = startup.missing_drivers(P())
    assert len(lines) == 1
    assert lines[0].startswith("Connection core:") and "pip install etlplus[postgres]" in lines[0]
