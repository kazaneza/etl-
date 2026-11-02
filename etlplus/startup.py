"""Start-up: where the home lives on each OS, and the checks run before serving, each with a plain
message. Nothing here reaches the internet; the port probe only talks to this machine."""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
import uuid
from pathlib import Path

APP_DIR = "etlplus"


def default_home(platform: str | None = None, env: dict | None = None, home: Path | None = None) -> Path:
    """The per-user home when neither --home nor $ETLPLUS_HOME / $ETLPLUS_PROJECT says otherwise.

    Windows: %LOCALAPPDATA%\\etlplus. macOS: ~/Library/Application Support/etlplus.
    Linux and others: $XDG_DATA_HOME/etlplus, else ~/.local/share/etlplus."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        return Path(base) / APP_DIR if base else home / "AppData" / "Local" / APP_DIR
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR
    xdg = env.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg and (xdg.startswith("/") or os.path.isabs(xdg)) else home / ".local" / "share") / APP_DIR


def home_path(arg=None) -> Path:
    """--home, then $ETLPLUS_HOME, then $ETLPLUS_PROJECT, then ./project when it holds an etl+ home
    or project (as before 0.3.0), then the per-user default."""
    p = arg or os.environ.get("ETLPLUS_HOME") or os.environ.get("ETLPLUS_PROJECT")
    if p:
        return Path(p)
    here = Path("project")
    if (here / "projects.yaml").exists() or (here / "etlplus.yaml").exists():
        return here
    return default_home()


class StartError(RuntimeError):
    """A start-up check failed; the message says what to do."""


def check_writable(path) -> Path:
    """The home must exist and be writable. Leaves nothing behind."""
    p = Path(path).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f".etlplus-write-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        reason = e.strerror or e.__class__.__name__
        raise StartError(f"etl+ cannot write to its home folder {p} ({reason}). Choose a folder you can "
                         f"write to: etlplus start --home <folder>, or set ETLPLUS_HOME.") from None
    return p.resolve()


def _bindable(host: str, port: int) -> bool:
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def local_url(host: str, port: int) -> str:
    h = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in h:
        h = f"[{h}]"
    return f"http://{h}:{port}"


def running_etlplus(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when an etl+ already answers /health on that port (this machine only)."""
    try:
        req = urllib.request.Request(local_url(host, port) + "/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read(4096) or b"{}")
        return bool(isinstance(data, dict) and data.get("ok") is True and "project" in data)
    except Exception:
        return False


def check_port(host: str, port: int) -> str:
    """'free', 'etlplus' (an etl+ already serves it) or raises StartError."""
    if not 0 < int(port) < 65536:
        raise StartError(f"The port must be between 1 and 65535, not {port}.")
    if _bindable(host, port):
        return "free"
    if running_etlplus(host, port):
        return "etlplus"
    raise StartError(f"Port {port} on {host} is already in use by another program. Stop it, or start etl+ "
                     f"on another port: etlplus start --port {port + 1}  (or set ETLPLUS_PORT).")


def ensure_secret_key(project) -> str | None:
    """Create the project's secrets key on its first start (unless $ETLPLUS_SECRET_KEY is set), and
    check that saved secrets can be read. Returns a line to print, or None."""
    from etlplus import secrets
    if project is None:
        return None
    root = Path(getattr(project, "path", project))
    kf = root / secrets.KEY_FILE
    if os.environ.get(secrets.ENV_KEY, "").strip():
        created = False
    else:
        created = not kf.exists()
    try:
        secrets._fernet(root)
        if (root / "secrets.json.enc").exists():
            secrets._load(root)
    except Exception as e:
        return f"Saved passwords cannot be read: {e}"
    if created and kf.exists():
        return f"Secrets key created: {kf}. Keep a copy; without it saved passwords cannot be read."
    return None


def missing_drivers(project) -> list[str]:
    """One line per connection whose driver is not installed, naming the pip extra."""
    if project is None:
        return []
    from etlplus import drivers
    out = []
    for cid, spec in sorted((getattr(project, "connections", None) or {}).items()):
        kind = (spec or {}).get("kind")
        try:
            if kind and not drivers.installed(kind):
                out.append(f"Connection {cid}: " + drivers.missing_driver_message(kind))
        except Exception:
            continue
    return out


def project_notes(project) -> list[str]:
    lines = []
    key = ensure_secret_key(project)
    if key:
        lines.append(key)
    lines.extend(missing_drivers(project))
    return lines
