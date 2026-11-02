"""etl+ REST API and console.

create_app(project) -> FastAPI in fixed mode (one project, the v1 form), or
create_app(home=...) in home mode, where the person chooses demo data or their own data and
the active project is read on every request. The console at /app/ calls only the routes below.
Secrets are write-only: a response never carries a password, only `password_set` and
`<field>_set`.
"""
from __future__ import annotations

import hmac
import os
import shutil
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

CONSOLE_DIR = Path(__file__).parent / "console"
SECRET_KEYS = {"password", "token", "secret", "pwd", "passwd", "api_key", "smtp_password"}
NOTIFY_SECRET_ID = "smtp"  # notify.py reads the SMTP password from here
LOG_TAIL = 200
LIST_CAP = 5000
PAGE_MAX = 500
CHOOSE = {"detail": "Choose demo data or your own data first.", "code": "choose"}
# routes that answer before the person has chosen (SPEC_V2 9.1)
OPEN_BEFORE_CHOICE = ("/api/start", "/api/kinds", "/api/gallery", "/api/formats", "/api/types")
FILE_FORMAT_CARDS = [
    ("csv", "CSV", "file-csv"), ("txt", "Delimited text", "file-text"), ("fixed", "Fixed width", "rows"),
    ("parquet", "Parquet", "file"), ("json", "JSON", "brackets-curly"), ("jsonl", "JSON lines", "brackets-curly"),
    ("excel", "Excel", "file-xls"),
]
FLOW_STATUS = {"done": "ok", "failed": "failed", "running": "running", "skipped": "skipped",
               "cancelled": "cancelled", "interrupted": "cancelled"}


def _mod(name):
    import importlib
    return importlib.import_module(f"etlplus.{name}")


def _is_secret(k) -> bool:
    k = str(k).lower()
    if k in SECRET_KEYS or k.endswith("_password") or k.endswith("_secret") or k.startswith("_"):
        return True
    try:
        return _mod("config").is_secret_key(k)
    except Exception:
        return False


def _strip_secrets(d):
    if isinstance(d, dict):
        return {k: _strip_secrets(v) for k, v in d.items() if not _is_secret(k)}
    if isinstance(d, list):
        return [_strip_secrets(v) for v in d]
    return d


def _fail(e: Exception, status=400):
    raise HTTPException(status_code=status, detail=str(e) or e.__class__.__name__)


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _page(response: Response, items: list, offset: int | None, limit: int | None) -> list:
    """Every list that can grow: ?offset=&limit= (max 500), X-Total-Count, the v1 array shape.
    No limit means the whole list, capped at 5000."""
    total = len(items)
    off = max(int(offset or 0), 0)
    lim = LIST_CAP if limit is None else max(1, min(int(limit), PAGE_MAX))
    response.headers["X-Total-Count"] = str(total)
    return items[off:off + lim]


# ------------------------------------------------------------------ helpers over the core


def _connection_out(project, cid, spec):
    out = {"id": cid, "kind": None, "role": "source", "host": None, "port": None, "database": None,
           "user": None, "path": None, "options": {}}
    conns = _mod("connections")
    if hasattr(conns, "public_spec"):
        try:
            out.update(conns.public_spec(project, cid, dict(spec or {})))
            out["id"] = cid
            out.setdefault("password_set", False)
            return _strip_secrets_keep_set(out)
        except Exception:
            pass
    secrets = _mod("secrets")
    try:
        has = secrets.has(project, cid) or {}
    except Exception:
        has = {}
    out.update(_strip_secrets(dict(spec or {})))
    out["id"] = cid
    out["password_set"] = bool(has.get("password"))
    for k, v in has.items():
        if k != "password" and not str(k).startswith("odbc:"):
            out[f"{k}_set"] = bool(v)
    return out


def _strip_secrets_keep_set(d: dict) -> dict:
    return {k: (_strip_secrets(v) if isinstance(v, dict) else v) for k, v in d.items()
            if str(k).endswith("_set") or not _is_secret(k)}


def _last_run(project, name):
    try:
        rows = _mod("store").runs(project, job=name, limit=1)
        return rows[0] if rows else None
    except Exception:
        return None


def _next_run(project, job):
    cron = (job or {}).get("schedule")
    if not cron:
        return None
    sched = None
    try:
        sched = _mod("scheduler")
    except Exception:
        pass
    if sched is not None and hasattr(sched, "next_run"):
        try:
            return _iso(sched.next_run(project, job.get("name")))
        except Exception:
            pass
    try:
        tz = (project.settings or {}).get("timezone") or "UTC"
        trig = _mod("scheduler").cron_trigger(cron, tz)
        now = datetime.now(timezone.utc)
        try:
            nxt = trig.get_next_fire_time(None, now)
        except TypeError:
            nxt = trig.next()
        return _iso(nxt)
    except Exception:
        return None


def _problems(project, job):
    try:
        runner = _mod("runner")
    except ImportError:
        return []
    try:
        probs = list(runner.validate_job(project, job))
    except Exception as e:
        return [str(e)]
    # mapping names against a destination design, when the job has both
    try:
        t = (job or {}).get("target") or {}
        m = (job or {}).get("mapping") or {}
        did = t.get("destination")
        if did and isinstance(m, dict) and isinstance(m.get("columns"), dict) and did in (project.destinations or {}):
            spec = project.destinations[did]
            if (spec.get("design") or "create") == "create" and spec.get("columns"):
                names = {str(c.get("name")).lower() for c in spec["columns"]}
                for d in m["columns"]:
                    if str(d).lower() not in names:
                        probs.append(f"Mapping '{d}': the destination has no such column.")
    except Exception:
        pass
    return probs


def _dir_size(p: Path) -> int:
    total = 0
    if p.exists():
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _log_tail(project, job, run_id, n=LOG_TAIL):
    try:
        f = Path(project.logs_dir()) / str(job) / f"{run_id}.log"
    except Exception:
        return []
    if not f.exists():
        return []
    try:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return lines[-n:]


def _staged(project, job_name):
    import json
    _mod("config").check_name(job_name, "job name")
    root = Path(project.stage_dir()).resolve()
    base = (root / job_name).resolve()
    if base.parent != root:
        raise ValueError(f"Not a valid job name: {job_name!r}")
    out = []
    if not base.exists():
        return out
    for d in sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                    key=lambda p: p.name, reverse=True):
        manifests = []
        for m in sorted(d.glob("*.json")):
            if m.name.endswith(".partial"):
                continue
            try:
                manifests.append(json.loads(m.read_text(encoding="utf-8")))
            except Exception:
                continue
        files = [{"name": f.name, "bytes": f.stat().st_size, "complete": not f.name.endswith(".partial")}
                 for f in sorted(d.iterdir()) if f.is_file()]
        out.append({"run_id": d.name, "manifests": manifests, "files": files,
                    "bytes": sum(f["bytes"] for f in files),
                    "complete": any(m.get("kind") in (None, "extract") for m in manifests)})
    return out


def _save_settings(project, settings: dict):
    if hasattr(project, "save_settings"):
        project.save_settings(settings)
        return
    import yaml
    f = Path(project.path) / "etlplus.yaml"
    tmp = f.with_suffix(".yaml.partial")
    tmp.write_text(yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, f)
    project.settings = settings


def _pop_notify_password(notify: dict):
    """Take the SMTP password out of a notify block; returns (clean, password or None)."""
    notify = dict(notify or {})
    pw = notify.pop("smtp_password", None)
    email = notify.get("email")
    if isinstance(email, dict):
        email = dict(email)
        pw = email.pop("password", pw)
        pw = email.pop("smtp_password", pw)
        notify["email"] = email
    return notify, pw


def _pop_webhook_headers(notify: dict, current: dict):
    """Take the webhook header values out of a notify block (they are secrets).

    Returns (clean notify, {header: value or ""}, clear_all). A header sent with a value sets
    it; null removes it; "" keeps the stored one. A new webhook URL drops every stored header
    unless the same request sends them again (a saved token never goes to a new server)."""
    notify = dict(notify or {})
    hook = notify.get("webhook")
    changes, clear = {}, bool(notify.pop("clear_webhook_headers", False))
    old = (current or {}).get("webhook")
    old_url = old.get("url") if isinstance(old, dict) else old
    if isinstance(hook, dict):
        hook = dict(hook)
        hook.pop("headers_set", None)
        heads = hook.pop("headers", None)
        if isinstance(heads, dict):
            for k, v in heads.items():
                name = str(k).strip()
                if not name:
                    continue
                if v is None:
                    changes[name] = ""
                elif str(v) != "":
                    changes[name] = str(v)
        notify["webhook"] = hook if hook.get("url") else None
        new_url = hook.get("url")
    else:
        new_url = hook
    if (new_url or None) != (old_url or None):
        clear = True
    return notify, changes, clear


# ------------------------------------------------------------------ files of a connection


@contextmanager
def _local_file(project, cid: str, file: str):
    """A local path for one file of a folder, shared folder or remote location. A remote file is
    fetched into a temp folder under the stage and deleted after (SPEC_V2 9.2, 9.5)."""
    from etlplus.remote import paths
    spec = (project.connections or {}).get(cid)
    if spec is None:
        raise HTTPException(404, f"No connection named {cid}.")
    kind = spec.get("kind")
    try:
        rel = paths.check_rel(file, "file")
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if kind in ("folder", "share"):
        conns = _mod("connections")
        root = conns.file_path(project, spec) if kind == "folder" else paths.share_root(project, spec.get("path"))
        full = (Path(root) / rel).resolve()
        try:
            full.relative_to(Path(root).resolve())
        except ValueError:
            raise HTTPException(422, "The file must stay inside the connection's folder.") from None
        try:
            paths.check_local(project, full)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if project.protected(full) if hasattr(project, "protected") else False:
            raise HTTPException(422, "That file is an etl+ secret or state file.")
        if not full.is_file():
            raise HTTPException(404, f"No file {rel} in '{cid}'.")
        yield full
        return
    remote = _mod("remote")
    files = [f for f in remote.list_files(project, cid, rel, limit=10) if f.path == rel]
    if not files:
        raise HTTPException(404, f"No file {rel} in '{cid}'.")
    tmp = Path(project.stage_dir()) / ".tmp" / uuid.uuid4().hex[:10]
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        got = remote.fetch(project, cid, files[:1], tmp)
        yield tmp / got[0]["local"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ flow (SPEC_V2 9.5)


def _flow(project, name, job) -> dict:
    store = _mod("store")
    runs = store.runs(project, job=name, limit=1)
    rec = runs[0] if runs else None
    steps = (rec or {}).get("steps") or {}
    rows = (rec or {}).get("rows") or {}
    src = job.get("source") or {}
    t = job.get("target") or {}
    conns = project.connections or {}
    skind = (conns.get(src.get("connection")) or {}).get("kind")
    what = src.get("table") or src.get("files") or ("query" if src.get("query") else "")
    has_t = bool(job.get("transform"))
    m = job.get("mapping") or {}
    has_m = isinstance(m, dict) and bool(m.get("columns"))
    did = t.get("destination")
    if did:
        dspec = (project.destinations or {}).get(did) or {}
        tkind = (conns.get(dspec.get("connection")) or {}).get("kind")
        dlabel = did
    else:
        tkind = (conns.get(t.get("connection")) or {}).get("kind")
        dlabel = f"{t.get('connection')}:{t.get('table')}" if t.get("connection") else "Destination"

    def st(step, configured=True):
        if rec is None:
            return "idle"
        if not configured:
            return "skipped"
        s = (steps.get(step) or {}).get("status")
        if s is None and rec.get("status") == "running":
            return "idle"
        return FLOW_STATUS.get(s, "idle")

    stage_detail = "extract.parquet"
    if rec and rec.get("stage_run_id"):
        try:
            sm = _mod("stage").read_manifest(project, name, rec["stage_run_id"], "extract")
            if sm and sm.get("bytes") is not None:
                stage_detail = f"extract.parquet · {_size(sm['bytes'])}"
        except Exception:
            pass
    ext = steps.get("extract") or {}
    nodes = [
        {"id": "source", "label": src.get("connection") or "Source",
         "detail": " · ".join(x for x in (str(what), skind or "") if x), "status": st("extract"),
         "rows": rows.get("extracted"), "ms": ext.get("ms")},
        {"id": "stage", "label": "Stage", "detail": stage_detail, "status": st("extract"),
         "rows": rows.get("extracted")},
        {"id": "transform", "label": "Transform", "detail": f"{len(job.get('transform') or [])} steps",
         "status": st("transform", has_t), "rows": rows.get("transformed") if has_t else None,
         "ms": (steps.get("transform") or {}).get("ms")},
        {"id": "mapping", "label": "Mapping", "detail": f"{len(m.get('columns') or {}) if has_m else 0} columns",
         "status": st("map", has_m), "rows": rows.get("mapped") if has_m else None,
         "ms": (steps.get("map") or {}).get("ms")},
        {"id": "destination", "label": dlabel, "detail": " · ".join(x for x in (t.get("mode", "replace"), tkind or "") if x),
         "status": st("load"), "rows": rows.get("loaded"), "ms": (steps.get("load") or {}).get("ms")},
    ]
    flow_rows = [rows.get("extracted"), rows.get("extracted"),
                 rows.get("transformed") if has_t else rows.get("extracted"),
                 rows.get("mapped") if has_m else (rows.get("transformed") if has_t else rows.get("extracted"))]
    ids = [n["id"] for n in nodes]
    edges = [{"from": ids[i], "to": ids[i + 1], "rows": flow_rows[i]} for i in range(4)]
    return {"run_id": (rec or {}).get("run_id"), "live": (rec or {}).get("status") == "running",
            "status": (rec or {}).get("status") or "idle", "nodes": nodes, "edges": edges}


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} GB"


# ------------------------------------------------------------------ app


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SIMPLE_TYPES = ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data")


def _host_name(value: str) -> str:
    """'Example.org:8300' -> 'example.org'; '[::1]:8300' -> '::1'."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v[1:]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def allowed_hosts(bind_host: str | None = None) -> set[str] | None:
    """Host names the API answers to; None means any. From $ETLPLUS_ALLOWED_HOSTS (comma list, or *),
    else localhost and the address the server is bound to."""
    env = os.environ.get("ETLPLUS_ALLOWED_HOSTS", "").strip()
    if env == "*":
        return None
    hosts = set(LOCAL_HOSTS)
    hosts.update(_host_name(h) for h in env.split(",") if h.strip())
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):
        hosts.add(_host_name(bind_host))
    return hosts


class _Choose(Exception):
    pass


def create_app(project=None, start_scheduler: bool = True, hosts: set[str] | None | str = "default",
               home=None) -> FastAPI:
    config = _mod("config")
    projects = _mod("projects")
    fixed = home is None
    H = None
    if fixed:
        if project is None or isinstance(project, (str, Path)):
            project = config.Project.load(project)
        root = Path(project.path)
    else:
        H = home if isinstance(home, projects.Home) else projects.Home.open(home)
        root = H.path
    token = os.environ.get("ETLPLUS_TOKEN") or None
    if hosts == "default":
        # with a token the browser cannot call the API without it, so any host name may be used
        hosts = None if token and not os.environ.get("ETLPLUS_ALLOWED_HOSTS") else allowed_hosts()
    sched_on = {"value": False}

    def active():
        if fixed:
            return config.Project.load(root)
        return H.active()

    def P():
        """The active project as it is on disk now, so the console and the CLI always agree."""
        try:
            p = active()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not read the project: {e}")
        if p is None:
            raise _Choose()
        return p

    def activate(p):
        """A project became active: reap its stale runs, create its secrets key on its first start
        (startup.project_notes) and move the schedules to it."""
        try:
            _mod("runner").reap(p)
        except Exception:
            pass
        try:
            for line in _mod("startup").project_notes(p):
                print(f"etl+: {line}")
        except Exception:
            pass
        if start_scheduler:
            try:
                _mod("scheduler").restart(p)
                sched_on["value"] = True
            except Exception as e:
                print(f"etl+: scheduler not started: {e}")

    @asynccontextmanager
    async def lifespan(app):
        p = None
        try:
            p = active()
        except Exception:
            p = None
        if p is not None:
            try:
                _mod("runner").reap(p)  # runs a stopped process left as running
            except Exception:
                pass
            if start_scheduler:
                try:
                    sched = _mod("scheduler")
                    if hasattr(sched, "start"):
                        sched.start(p)
                        sched_on["value"] = True
                except Exception as e:  # the API still serves without schedules
                    print(f"etl+: scheduler not started: {e}")
        yield
        if sched_on["value"]:
            try:
                _mod("scheduler").stop()
            except Exception:
                pass

    app = FastAPI(title="etl+", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.exception_handler(_Choose)
    async def _choose_handler(request, exc):
        return JSONResponse(CHOOSE, status_code=409)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        host = request.headers.get("host", "")
        if hosts is not None and _host_name(host) not in hosts:
            # a page served from another name (DNS rebinding) never reaches the API
            return JSONResponse({"detail": f"Host {host!r} is not allowed. Set ETLPLUS_ALLOWED_HOSTS "
                                           "to the names the console is opened with."}, status_code=421)
        path = request.url.path
        if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                from urllib.parse import urlsplit
                if urlsplit(origin).netloc.lower() != host.lower():
                    return JSONResponse({"detail": "A request from another site is refused."}, status_code=403)
            elif origin == "null":
                return JSONResponse({"detail": "A request from another site is refused."}, status_code=403)
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype in SIMPLE_TYPES:
                return JSONResponse({"detail": "Send JSON (Content-Type: application/json)."}, status_code=415)
        if token and path.startswith("/api/"):
            got = request.headers.get("authorization", "")
            ok = got.lower().startswith("bearer ") and hmac.compare_digest(got[7:].strip(), token)
            if not ok:
                return JSONResponse({"detail": "Sign in with the console token."}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        if not fixed and path.startswith("/api/") and not path.startswith(OPEN_BEFORE_CHOICE) \
                and path not in ("/api/docs", "/api/openapi.json"):
            try:
                chosen = H.active_id() is not None
            except Exception:
                chosen = False
            if not chosen:
                return JSONResponse(CHOOSE, status_code=409)
        return await call_next(request)

    # ---------------------------------------------------------------- console
    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/app/")

    @app.get("/app", include_in_schema=False)
    def app_noslash():
        return RedirectResponse("/app/")

    if CONSOLE_DIR.exists():
        app.mount("/app", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")

    @app.get("/health")
    def health():
        try:
            p = active()
        except Exception:
            p = None
        name = ((p.settings or {}).get("name") if p is not None else None) or root.name
        return {"ok": True, "project": name}

    # ---------------------------------------------------------------- start: demo or real (SPEC_V2 9.1)
    def _start_state():
        if fixed:
            p = active()
            kind = getattr(p, "kind", None) or "real"
            return {"state": "ready", "mode": "fixed", "active": kind, "start_env": projects.start_env(),
                    "projects": [{"id": kind, "label": projects.LABELS.get(kind, kind), "exists": True,
                                  "name": (p.settings or {}).get("name"), "jobs": len(p.jobs),
                                  "connections": len(p.connections),
                                  "destinations": len(getattr(p, "destinations", {}) or {}),
                                  "setup_done": projects.setup(p)["done"]}]}
        return H.state()

    @app.get("/api/start")
    def start_get():
        return _start_state()

    @app.post("/api/start")
    def start_post(body: dict = Body(...)):
        if fixed:
            raise HTTPException(409, "This server serves one project")
        choice = (body or {}).get("choice")
        if choice not in projects.CHOICES:
            raise HTTPException(422, "Choose demo or real.")
        run_jobs = bool((body or {}).get("run_jobs", True))
        try:
            H.choose(choice, run_jobs=run_jobs, on_ready=activate)
        except projects.Busy as e:
            raise HTTPException(409, str(e)) from None
        except Exception as e:
            _fail(e, 500)
        return _start_state()

    @app.post("/api/projects/demo/reset")
    def demo_reset(body: dict = Body(...)):
        if fixed:
            raise HTTPException(409, "This server serves one project")
        if (body or {}).get("confirm") != "demo":
            raise HTTPException(422, 'Send {"confirm": "demo"} to reset the demo.')
        try:
            p = H.reset_demo()
        except projects.Busy as e:
            raise HTTPException(409, str(e)) from None
        if H.active_id() == "demo":
            activate(p)
        return _start_state()

    @app.get("/api/setup")
    def setup_get():
        return projects.setup(P())

    # ---------------------------------------------------------------- overview
    @app.get("/api/overview")
    def overview():
        p = P()
        jobs = []
        failed = 0
        for name, job in sorted((p.jobs or {}).items()):
            last = _last_run(p, name)
            if last and last.get("status") == "failed":
                failed += 1
            jobs.append({"name": name, "description": job.get("description"), "last_run": last,
                         "next_run": _next_run(p, job), "schedule": job.get("schedule")})
        try:
            running = [r for r in (_mod("runner").state(p).get("running") or [])]
        except Exception:
            running = []
        try:
            alerts = _mod("store").alerts(p, limit=5)
        except Exception:
            alerts = []
        try:
            drivers = _mod("connections").driver_status()
        except Exception:
            drivers = {}
        kind = getattr(p, "kind", None)
        if kind is None and not fixed:
            kind = H.active_id()
        return {
            "project": {"id": kind or "real", "label": projects.LABELS.get(kind or "real"), "kind": kind or "real",
                        "name": (p.settings or {}).get("name") or root.name, "mode": "fixed" if fixed else "home"},
            "name": (p.settings or {}).get("name") or root.name,
            "setup": projects.setup(p),
            "path": str(p.path),
            "counts": {"connections": len(p.connections or {}), "jobs": len(p.jobs or {}),
                       "destinations": len(getattr(p, "destinations", {}) or {}),
                       "running": len(running), "failed": failed,
                       "scheduled": sum(1 for j in (p.jobs or {}).values() if j.get("schedule"))},
            "staged_bytes": _dir_size(Path(p.stage_dir())),
            "running": running,
            "jobs": jobs,
            "alerts": alerts,
            "drivers": drivers,
            "auth": bool(token),
        }

    # ---------------------------------------------------------------- kinds, gallery, types, formats
    @app.get("/api/kinds")
    def kinds():
        try:
            return _mod("drivers").status_table()
        except ImportError:
            return []

    def _file_cards():
        excel = True
        try:
            import importlib.util
            excel = importlib.util.find_spec("openpyxl") is not None
        except Exception:
            pass
        out = []
        for fid, label, icon in FILE_FORMAT_CARDS:
            out.append({"id": fid, "group": "Files", "label": label, "icon": icon,
                        "needs": "A folder, shared folder or remote location",
                        "installed": excel if fid == "excel" else True,
                        "install": "pip install etlplus[excel]" if fid == "excel" else None,
                        "status": "real", "kind": None, "format": fid, "roles": ["source"], "verify_note": None})
        return out

    def _bank_cards(p):
        out = []
        try:
            formats = _mod("formats")
        except ImportError:
            return out
        for c in formats.catalog():
            if c.get("group") != "Bank files":
                continue
            out.append({"id": c["id"], "group": "Bank files", "label": c["label"], "icon": "bank",
                        "needs": "A file in a folder or remote location" + (" and a layout" if c.get("needs_layout") else ""),
                        "installed": True, "install": None, "status": c.get("status") or "simulated",
                        "kind": None, "format": c["id"], "roles": ["source"], "verify_note": c.get("verify_note")})
        try:
            from etlplus.formats import layouts
            for lay in layouts.list_layouts(p):
                out.append({"id": f"layout:{lay['name']}", "group": "Bank files", "label": lay.get("label") or lay["name"],
                            "icon": "rows", "needs": "A fixed-width file in a folder or remote location",
                            "installed": True, "install": None, "status": "simulated", "kind": None,
                            "format": "layout", "layout": lay["name"], "roles": ["source"],
                            "verify_note": lay.get("verify")})
        except Exception:
            pass
        return out

    @app.get("/api/gallery")
    def gallery():
        try:
            p = active()
        except Exception:
            p = None
        groups = {"Databases": [], "Files": _file_cards(), "Bank files": _bank_cards(p), "Remote locations": []}
        try:
            drivers = _mod("drivers")
            for kind in drivers.kinds():
                d = drivers.get(kind)
                card = drivers.card(kind) if hasattr(drivers, "card") else {"id": kind, "label": d.label, "kind": kind}
                if d.category in ("database", "document"):
                    card["group"] = "Databases"
                    groups["Databases"].append(card)
                else:
                    card["group"] = "Remote locations"
                    groups["Remote locations"].append(card)
        except ImportError:
            pass
        return [{"group": g, "cards": cards} for g, cards in groups.items()]

    @app.get("/api/types")
    def types(kind: str = "duckdb"):
        ddl = _mod("ddl")
        try:
            ddl.family(kind)
        except Exception as e:
            raise HTTPException(422, str(e)) from None
        return ddl.types_table(kind)

    @app.get("/api/formats")
    def formats_list():
        try:
            return _mod("formats").catalog()
        except ImportError:
            return []

    @app.post("/api/formats/detect")
    def formats_detect(body: dict = Body(...)):
        p = P()
        cid, file = (body or {}).get("connection"), (body or {}).get("file")
        if not cid or not file:
            raise HTTPException(422, "Give a connection and a file.")
        with _local_file(p, cid, file) as path:
            formats = _mod("formats")
            with open(path, "rb") as f:
                head = f.read(65536)
            found = formats.detect(path, head, project=p)
            plain = None
            try:
                plain = _mod("connections").guess_format(Path(file).name)
            except Exception:
                pass
        return {"file": file, "detected": found, "guess": plain}

    # ---------------------------------------------------------------- layouts
    def _layouts():
        try:
            from etlplus.formats import layouts
            return layouts
        except ImportError:
            raise HTTPException(501, "Layouts need etlplus.formats.") from None

    @app.get("/api/layouts")
    def layouts_list(response: Response, offset: int | None = None, limit: int | None = None):
        return _page(response, _layouts().list_layouts(P()), offset, limit)

    @app.get("/api/layouts/{name}")
    def layout_get(name: str):
        lay = _layouts()
        try:
            return lay.load(lay.check_name(name), P())
        except (KeyError, FileNotFoundError) as e:
            raise HTTPException(404, str(e).strip("'\"")) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.put("/api/layouts/{name}")
    def layout_put(name: str, body: dict = Body(...)):
        lay = _layouts()
        p = P()
        try:
            lay.check_name(name)
            problems = lay.validate_layout(dict(body or {}))
            if problems:
                return JSONResponse({"detail": " ".join(problems), "problems": problems}, status_code=422)
            lay.save(p, name, dict(body or {}))
        except ValueError as e:
            probs = list(getattr(e, "problems", None) or [str(e)])
            return JSONResponse({"detail": " ".join(probs), "problems": probs}, status_code=422)
        return lay.load(name, p)

    @app.delete("/api/layouts/{name}")
    def layout_delete(name: str):
        lay = _layouts()
        p = P()
        try:
            lay.check_name(name)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        users = sorted(n for n, j in (p.jobs or {}).items()
                       if ((j.get("source") or {}).get("options") or {}).get("layout") == name)
        if users:
            raise HTTPException(409, f"Used by {', '.join(users)}. Change those jobs first.")
        try:
            lay.delete(p, name)
        except (KeyError, FileNotFoundError) as e:
            raise HTTPException(404, str(e).strip("'\"")) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"deleted": name}

    @app.post("/api/layouts/{name}/copy")
    def layout_copy(name: str, body: dict = Body(...)):
        lay = _layouts()
        p = P()
        to = (body or {}).get("to")
        try:
            if not isinstance(to, str):
                raise ValueError("Give the new layout's name in to.")
            lay.copy(p, lay.check_name(name), lay.check_name(to))
        except (KeyError, FileNotFoundError) as e:
            raise HTTPException(404, str(e).strip("'\"")) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return lay.load(to, p)

    @app.post("/api/layouts/{name}/test")
    def layout_test(name: str, body: dict = Body(...)):
        lay = _layouts()
        p = P()
        body = dict(body or {})
        layout = body.get("layout")
        if not isinstance(layout, dict):
            try:
                layout = lay.check_name(name)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        if not body.get("connection") or not body.get("file"):
            raise HTTPException(422, "Give a connection and a file.")
        formats = _mod("formats")
        with _local_file(p, body["connection"], body["file"]) as path:
            try:
                return formats.preview("layout", path, {"layout": layout, "checks": "warn"}, project=p,
                                       limit=int(body.get("limit") or 50))
            except Exception as e:
                raise HTTPException(422, str(e)) from None

    # ---------------------------------------------------------------- connections
    @app.get("/api/connections")
    def connections_list(response: Response, offset: int | None = None, limit: int | None = None):
        p = P()
        items = [_connection_out(p, cid, spec) for cid, spec in sorted((p.connections or {}).items())]
        return _page(response, items, offset, limit)

    @app.get("/api/connections/{cid}")
    def connection_get(cid: str):
        p = P()
        if cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        return _connection_out(p, cid, p.connections[cid])

    @app.put("/api/connections/{cid}")
    def connection_save(cid: str, body: dict = Body(...)):
        p = P()
        conns = _mod("connections")
        secrets = _mod("secrets")
        body = dict(body or {})
        clear_pw = bool(body.pop("clear_password", False))
        clears = {k[len("clear_"):] for k in list(body) if k.startswith("clear_") and body.pop(k)}
        body.pop("id", None)
        kinds_ok = config.known_kinds()
        if body.get("kind") not in kinds_ok:
            raise HTTPException(422, f"Pick a kind: {', '.join(sorted(kinds_ok))}.")
        saved = (p.connections or {}).get(cid)
        env = str(body.get("password_env") or "").strip()
        if env and env != str((saved or {}).get("password_env") or "").strip() \
                and not env.upper().startswith(conns.ENV_PREFIX):
            raise HTTPException(422, f"From the console, password_env must name a variable starting with "
                                     f"{conns.ENV_PREFIX}. Other names can be set in connections.yaml by hand.")
        try:
            config.check_name(cid, "connection id")
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if hasattr(conns, "prepare_save"):
            try:
                prep = conns.prepare_save(body, saved, p)
            except Exception as e:
                raise HTTPException(422, str(e)) from None
            spec, new_secrets, endpoint_changed = prep["spec"], dict(prep["secrets"]), prep["clear"]
        else:
            new_secrets = {k: str(v) for k, v in body.items() if _is_secret(k) and v not in (None, "")}
            spec = _strip_secrets(body)
            endpoint_changed = bool(saved) and not conns.same_endpoint(saved, spec)
        for k in [k for k in spec if str(k).endswith("_set")]:
            spec.pop(k, None)
        if endpoint_changed and env and env == str((saved or {}).get("password_env") or "").strip() \
                and "password" not in new_secrets:
            spec.pop("password_env", None)
        try:
            p.save_connection(cid, spec)
        except Exception as e:
            _fail(e, 422)
        if endpoint_changed:
            # a stored secret never follows a connection to another server, database or user
            secrets.delete(p, cid)
        if clear_pw:
            secrets.set(p, cid, {"password": ""})
        if clears:
            secrets.set(p, cid, {k: "" for k in clears})
        if new_secrets:
            secrets.set(p, cid, new_secrets)
        p = P()
        return _connection_out(p, cid, (p.connections or {}).get(cid, spec))

    @app.delete("/api/connections/{cid}")
    def connection_delete(cid: str):
        p = P()
        if cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        users = [n for n, j in (p.jobs or {}).items()
                 if (j.get("source") or {}).get("connection") == cid or (j.get("target") or {}).get("connection") == cid]
        users += [f"destination {d}" for d, s in (getattr(p, "destinations", {}) or {}).items()
                  if (s or {}).get("connection") == cid]
        if users:
            raise HTTPException(409, f"Used by {', '.join(sorted(users))}. Change those first.")
        p.delete_connection(cid)
        try:
            _mod("secrets").delete(p, cid)
        except Exception:
            pass
        return {"deleted": cid}

    @app.post("/api/connections/{cid}/test")
    def connection_test(cid: str, body: dict | None = Body(None)):
        p = P()
        override = None
        if body:
            override = {k: v for k, v in dict(body).items()
                        if k not in ("id", "password_set", "clear_password") and not str(k).endswith("_set")}
            for k in [k for k in override if _is_secret(k) and not override[k]]:
                override.pop(k, None)
        if override is None and cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        try:
            res = _mod("connections").test(p, cid, override=override)
        except Exception as e:
            res = {"ok": False, "steps": [{"name": "connect", "ok": False, "detail": str(e), "ms": 0}], "server": None}
        return _scrub(res, override)

    def _scrub(res, override):
        """A test result never repeats a secret the form carried."""
        secrets_in = [str(v) for k, v in (override or {}).items() if _is_secret(k) and v]
        if not secrets_in:
            return res
        import json
        text = json.dumps(res, default=str)
        for s in secrets_in:
            text = text.replace(json.dumps(s)[1:-1], "***")
        return json.loads(text)

    @app.get("/api/connections/{cid}/tables")
    def connection_tables(cid: str, response: Response, schema: str | None = None,
                          offset: int | None = None, limit: int | None = None):
        p = P()
        if cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        try:
            items = _mod("connections").list_tables(p, cid, schema=schema or None)
        except Exception as e:
            _fail(e)
        return _page(response, items, offset, limit)

    @app.get("/api/connections/{cid}/columns")
    def connection_columns(cid: str, table: str):
        p = P()
        if cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        try:
            return _mod("connections").list_columns(p, cid, table)
        except Exception as e:
            _fail(e)

    @app.get("/api/connections/{cid}/files")
    def connection_files(cid: str, response: Response, pattern: str | None = None,
                         offset: int | None = None, limit: int | None = None):
        p = P()
        if cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        conns = _mod("connections")
        try:
            items = conns.list_files(p, cid, pattern or "**/*")
        except Exception as e:
            _fail(e)
        items = [{"path": f["path"], "size": f.get("size"), "modified": f.get("modified"), "format": f.get("format")}
                 for f in items]
        return _page(response, items, offset, limit)

    @app.post("/api/connections/{cid}/preview")
    def connection_preview(cid: str, body: dict = Body(...)):
        p = P()
        if cid not in (p.connections or {}):
            raise HTTPException(404, f"No connection named {cid}.")
        what = body.get("table") or body.get("files")
        if not what:
            raise HTTPException(422, "Give a table or a file pattern.")
        opts = dict(body.get("options") or {})
        if body.get("layout") and "layout" not in opts:
            opts["layout"] = body["layout"]
        fmt = body.get("format") or ("layout" if body.get("layout") else None)
        try:
            limit = max(1, min(int(body.get("limit") or 20), 500))
            return _mod("connections").preview(p, cid, what, fmt=fmt, options=opts or None, limit=limit)
        except Exception as e:
            _fail(e)

    @app.post("/api/sources/columns")
    def source_columns(body: dict = Body(...)):
        p = P()
        src = {k: v for k, v in dict(body or {}).items() if k in ("connection", "table", "query", "files", "format",
                                                                   "options", "columns")}
        if not src.get("connection"):
            raise HTTPException(422, "Give a connection.")
        mapping = _mod("mapping")
        ddl = _mod("ddl")
        try:
            table = mapping.sample_source(p, src, mapping.SAMPLE_ROWS)
        except HTTPException:
            raise
        except Exception as e:
            _fail(e)
        personal = mapping._personal_source_columns(p, {"source": src})
        tr = _mod("transform")
        cols = []
        for f in table.schema:
            vals = table.column(f.name).to_pylist()
            cols.append({"name": f.name, "type": ddl.arrow_to_generic(f.type),
                         "samples": [tr.jsonable(v) for v in vals if v is not None][:3],
                         "nulls": sum(1 for v in vals if v is None), "personal": f.name in personal})
        return {"columns": cols, "rows_sampled": table.num_rows}

    # ---------------------------------------------------------------- destinations (SPEC_V2 9.4)
    def _dest_or_404(p, did):
        if did not in (getattr(p, "destinations", {}) or {}):
            raise HTTPException(404, f"No destination named {did}.")

    @app.get("/api/destinations")
    def destinations_list(response: Response, offset: int | None = None, limit: int | None = None):
        p = P()
        return _page(response, _mod("destinations").list(p), offset, limit)

    @app.get("/api/destinations/{did}")
    def destination_get(did: str):
        p = P()
        _dest_or_404(p, did)
        try:
            return _mod("destinations").get(p, did)
        except Exception as e:
            _fail(e)

    @app.put("/api/destinations/{did}")
    def destination_put(did: str, body: dict = Body(...)):
        p = P()
        dest = _mod("destinations")
        body = _strip_secrets(dict(body or {}))
        old = body.pop("rename_from", None)
        if old and old != did:
            if old not in (p.destinations or {}):
                raise HTTPException(404, f"No destination named {old}.")
            if did in (p.destinations or {}):
                return JSONResponse({"detail": f"A destination named {did} exists.",
                                     "problems": [f"A destination named {did} exists."]}, status_code=422)
            probs = dest.rename(p, old, did, body)
        else:
            probs = dest.save(p, did, body)
        if probs:
            return JSONResponse({"detail": " ".join(probs), "problems": probs}, status_code=422)
        return dest.get(P(), did)

    @app.delete("/api/destinations/{did}")
    def destination_delete(did: str):
        p = P()
        _dest_or_404(p, did)
        dest = _mod("destinations")
        try:
            dest.delete(p, did)
        except dest.InUse as e:
            return JSONResponse({"detail": str(e), "jobs": e.jobs}, status_code=409)
        return {"deleted": did}

    @app.post("/api/destinations/{did}/test")
    def destination_test(did: str, body: dict | None = Body(None)):
        p = P()
        dest = _mod("destinations")
        if body:
            return dest.test(p, did, _strip_secrets(dict(body)))
        _dest_or_404(p, did)
        return dest.test(p, did)

    @app.get("/api/destinations/{did}/ddl")
    def destination_ddl(did: str):
        p = P()
        _dest_or_404(p, did)
        try:
            return _mod("destinations").ddl_info(p, did)
        except Exception as e:
            _fail(e)

    @app.post("/api/destinations/{did}/create")
    def destination_create(did: str):
        p = P()
        _dest_or_404(p, did)
        dest = _mod("destinations")
        try:
            if dest.exists(p, did):
                raise HTTPException(409, f"The table of {did} exists already.")
            return dest.create(p, did)
        except HTTPException:
            raise
        except dest.Busy as e:
            raise HTTPException(409, str(e)) from None
        except Exception as e:
            _fail(e, 422)

    @app.get("/api/destinations/{did}/preview")
    def destination_preview(did: str, offset: int = 0, limit: int = 50):
        p = P()
        _dest_or_404(p, did)
        dest = _mod("destinations")
        try:
            return dest.preview(p, did, limit=limit, offset=offset)
        except dest.Busy as e:
            raise HTTPException(409, str(e)) from None
        except Exception as e:
            _fail(e)

    @app.post("/api/destinations/design-from-source")
    def destination_design(body: dict = Body(...)):
        p = P()
        src = dict((body or {}).get("source") or {})
        cid = (body or {}).get("connection")
        kind = ((p.connections or {}).get(cid) or {}).get("kind") or "duckdb"
        try:
            steps = (body or {}).get("transform") or None
            if steps is not None and not isinstance(steps, list):
                raise HTTPException(422, "transform is a list of steps.")
            if (body or {}).get("detail"):
                return _mod("destinations").design_detail(p, src, kind, steps)
            if steps:
                return _mod("destinations").design_from_source(p, src, kind, steps)
            return _mod("destinations").design_from_source(p, src, kind)
        except HTTPException:
            raise
        except Exception as e:
            _fail(e)

    # ---------------------------------------------------------------- jobs
    def _job_out(p, name, job):
        out = dict(job)
        out["name"] = name
        out["last_run"] = _last_run(p, name)
        out["next_run"] = _next_run(p, job)
        out["problems"] = _problems(p, job)
        out["timezone"] = (p.settings or {}).get("timezone") or "UTC"
        return out

    @app.get("/api/jobs")
    def jobs_list(response: Response, offset: int | None = None, limit: int | None = None):
        p = P()
        names = sorted((p.jobs or {}).items())
        response.headers["X-Total-Count"] = str(len(names))
        off = max(int(offset or 0), 0)
        lim = LIST_CAP if limit is None else max(1, min(int(limit), PAGE_MAX))
        return [_job_out(p, n, j) for n, j in names[off:off + lim]]

    @app.get("/api/jobs/{name}")
    def job_get(name: str):
        p = P()
        job = (p.jobs or {}).get(name)
        if job is None:
            raise HTTPException(404, f"No job named {name}.")
        return _job_out(p, name, job)

    @app.put("/api/jobs/{name}")
    def job_save(name: str, body: dict = Body(...)):
        p = P()
        job = {k: v for k, v in dict(body).items() if k not in ("last_run", "next_run", "problems", "timezone")}
        new_name = (job.get("name") or name).strip()
        job["name"] = new_name
        problems = _problems(p, job)
        if problems:
            return JSONResponse({"detail": " ".join(problems), "problems": problems}, status_code=422)
        try:
            p.save_job(job)
        except Exception as e:
            return JSONResponse({"detail": str(e), "problems": [str(e)]}, status_code=422)
        if new_name != name and name in (p.jobs or {}):
            p.delete_job(name)
        p = P()
        return _job_out(p, new_name, (p.jobs or {}).get(new_name, job))

    @app.delete("/api/jobs/{name}")
    def job_delete(name: str):
        p = P()
        if name not in (p.jobs or {}):
            raise HTTPException(404, f"No job named {name}.")
        p.delete_job(name)
        return {"deleted": name}

    @app.post("/api/jobs/{name}/run")
    def job_run(name: str, body: dict | None = Body(None)):
        p = P()
        if name not in (p.jobs or {}):
            raise HTTPException(404, f"No job named {name}.")
        body = body or {}
        all_steps = tuple(_mod("runner").STEPS)
        steps = tuple(body.get("steps") or all_steps)
        bad = [s for s in steps if s not in all_steps]
        if bad:
            raise HTTPException(422, f"Unknown step: {', '.join(map(str, bad))}.")
        window = (body["from"], body["to"]) if body.get("from") and body.get("to") else None
        try:
            run_id = _mod("runner").start_background(p, name, steps=steps, window=window, trigger="console")
        except Exception as e:
            _fail(e, 409)
        return {"run_id": run_id}

    @app.post("/api/jobs/{name}/transform-preview")
    def job_transform_preview(name: str, body: dict = Body(...)):
        p = P()
        job = dict(body)
        job.setdefault("name", name)
        if job["name"] not in (p.jobs or {}) and name in (p.jobs or {}):
            job["name"] = name  # the staged file belongs to the saved name
        tr = _mod("transform")
        try:
            tr.validate(job.get("transform") or [])
            return tr.preview(p, job, limit=50)
        except Exception as e:
            _fail(e)

    @app.post("/api/jobs/{name}/mapping-preview")
    def job_mapping_preview(name: str, body: dict = Body(...)):
        p = P()
        job = dict(body or {})
        job.setdefault("name", name)
        if job["name"] not in (p.jobs or {}) and name in (p.jobs or {}):
            job["name"] = name
        mapping = _mod("mapping")
        try:
            return mapping.preview(p, job, limit=int(job.pop("limit", 50) or 50))
        except mapping.PreviewBusy as e:
            raise HTTPException(429, str(e)) from None
        except HTTPException:
            raise
        except Exception as e:
            _fail(e)

    @app.post("/api/mapping/suggest")
    def mapping_suggest(body: dict = Body(...)):
        src = (body or {}).get("source_columns") or []
        dst = (body or {}).get("destination_columns") or []
        if not isinstance(src, list) or not isinstance(dst, list):
            raise HTTPException(422, "Give source_columns and destination_columns as lists.")
        return {"columns": _mod("mapping").suggest(src, dst)}

    @app.get("/api/jobs/{name}/flow")
    def job_flow(name: str):
        p = P()
        job = (p.jobs or {}).get(name)
        if job is None:
            raise HTTPException(404, f"No job named {name}.")
        return _flow(p, name, job)

    @app.get("/api/jobs/{name}/files")
    def job_files(name: str, response: Response, offset: int = 0, limit: int = 100):
        p = P()
        if name not in (p.jobs or {}):
            raise HTTPException(404, f"No job named {name}.")
        rows, total = _mod("store").files(p, name, limit=max(1, min(limit, PAGE_MAX)), offset=offset)
        response.headers["X-Total-Count"] = str(total)
        return rows

    @app.get("/api/jobs/{name}/ddl")
    def job_ddl(name: str):
        p = P()
        job = (p.jobs or {}).get(name)
        if job is None:
            raise HTTPException(404, f"No job named {name}.")
        stage = _mod("stage")
        manifest = stage.latest(p, name, "transform") or stage.latest(p, name, "extract")
        if not manifest:
            raise HTTPException(404, "Nothing staged yet. Run the extract first.")
        try:
            return {"ddl": _mod("targets").ddl(p, job, manifest)}
        except Exception as e:
            _fail(e)

    # ---------------------------------------------------------------- runs
    @app.get("/api/runs")
    def runs_list(response: Response, job: str | None = None, offset: int = 0, limit: int | None = None):
        p = P()
        _mod("runner").reap(p)
        store = _mod("store")
        lim = LIST_CAP if limit is None else max(1, min(limit, PAGE_MAX))
        response.headers["X-Total-Count"] = str(store.count_runs(p, job=job or None))
        return store.runs(p, job=job or None, limit=lim, offset=max(offset, 0))

    @app.get("/api/runs/{run_id}")
    def run_get(run_id: str):
        p = P()
        run = _mod("store").get(p, run_id)
        if run is None:
            try:
                run = next((r for r in _mod("runner").state(p).get("running") or [] if r.get("run_id") == run_id), None)
            except Exception:
                run = None
        if run is None:
            raise HTTPException(404, f"No run {run_id}.")
        out = dict(run)
        out["log"] = _log_tail(p, run.get("job"), run_id)
        return out

    @app.post("/api/runs/{run_id}/cancel")
    def run_cancel(run_id: str):
        p = P()
        return {"cancelled": bool(_mod("runner").cancel(p, run_id))}

    # ---------------------------------------------------------------- stage, alerts
    @app.get("/api/stage/{job}")
    def stage_list(job: str, response: Response, offset: int | None = None, limit: int | None = None):
        p = P()
        if job not in (p.jobs or {}):
            raise HTTPException(404, f"No job named {job}.")
        try:
            return _page(response, _staged(p, job), offset, limit)
        except ValueError as e:
            _fail(e, 404)

    @app.get("/api/alerts")
    def alerts(response: Response, offset: int = 0, limit: int = 50):
        p = P()
        store = _mod("store")
        response.headers["X-Total-Count"] = str(store.count_alerts(p))
        return store.alerts(p, limit=max(1, min(limit, PAGE_MAX)), offset=max(offset, 0))

    # ---------------------------------------------------------------- settings
    def _settings_out(p):
        s = _strip_secrets(dict(p.settings or {}))
        try:
            pw = bool(_mod("secrets").get(p, NOTIFY_SECRET_ID).get("password"))
        except Exception:
            pw = False
        s.setdefault("notify", {})
        s["notify"]["smtp_password_set"] = pw
        hook = s["notify"].get("webhook")
        if isinstance(hook, dict) or hook:
            hook = dict(hook) if isinstance(hook, dict) else {"url": hook}
            raw = ((p.settings or {}).get("notify") or {}).get("webhook")
            names = [str(k) for k in (raw.get("headers") or {})] if isinstance(raw, dict) else []
            try:
                names += list(_mod("secrets").get(p, _mod("notify").WEBHOOK_SECRET_ID))
            except Exception:
                pass
            hook.pop("headers", None)
            hook["headers_set"] = sorted(set(names), key=str.lower)
            s["notify"]["webhook"] = hook
        try:
            drivers = _mod("connections").driver_status()
        except Exception:
            drivers = {}
        return {"settings": s, "drivers": drivers, "path": str(p.path), "auth": bool(token),
                "mode": "fixed" if fixed else "home"}

    @app.get("/api/settings")
    def settings_get():
        return _settings_out(P())

    @app.put("/api/settings")
    def settings_put(body: dict = Body(...)):
        p = P()
        body = dict(body.get("settings", body))
        current = dict(p.settings or {})
        notify, pw = _pop_notify_password(body.pop("notify", current.get("notify") or {}))
        notify, hook_changes, hook_clear = _pop_webhook_headers(notify, current.get("notify") or {})
        notify.pop("smtp_password_set", None)
        clear = notify.pop("clear_smtp_password", False)
        for k in ("name", "keep_runs", "timezone", "stage"):
            if k in body:
                current[k] = body[k]
        if "stage" in body and body["stage"] != (p.settings or {}).get("stage"):
            raw = str(body["stage"] or "").strip()
            if not raw or not p.inside(p.resolve(raw)) or p.resolve(raw) == Path(p.path).resolve():
                raise HTTPException(422, "The stage folder must be a folder inside the project. "
                                         "A folder elsewhere can be set in etlplus.yaml by hand.")
        if "keep_runs" in current and current["keep_runs"] is not None:
            try:
                current["keep_runs"] = int(current["keep_runs"])
            except (TypeError, ValueError):
                raise HTTPException(422, "Keep runs must be a whole number.")
        current["notify"] = _strip_secrets(notify)
        if isinstance(current["notify"].get("webhook"), dict):
            current["notify"]["webhook"].pop("headers", None)
        hook_id = _mod("notify").WEBHOOK_SECRET_ID
        if hook_clear:
            _mod("secrets").delete(p, hook_id)
        if hook_changes:
            _mod("secrets").set(p, hook_id, hook_changes)
        _save_settings(p, current)
        if clear:
            _mod("secrets").set(p, NOTIFY_SECRET_ID, {"password": ""})
        elif pw:
            _mod("secrets").set(p, NOTIFY_SECRET_ID, {"password": str(pw)})
        return _settings_out(P())

    @app.post("/api/notify/test")
    def notify_test():
        p = P()
        n = _mod("notify")
        fn = getattr(n, "test", None) or getattr(n, "send_test", None)
        try:
            if fn is not None:
                res = fn(p)
            else:
                res = n.send(p, {"job": "test", "status": "test", "run_id": "test",
                                 "error": None, "message": "A test message from etl+."})
        except Exception as e:
            _fail(e)
        if isinstance(res, dict):
            return res
        return {"ok": True if res is None else bool(res)}

    app.state.home = H
    app.state.fixed = fixed
    return app


def serve(project=None, host="127.0.0.1", port=8300, home=None):
    import uvicorn
    if home is not None:
        app = create_app(home=home, hosts=_serve_hosts(host))
    else:
        app = create_app(project, hosts=_serve_hosts(host))
    uvicorn.run(app, host=host, port=port)


def _serve_hosts(bind_host):
    if os.environ.get("ETLPLUS_TOKEN") and not os.environ.get("ETLPLUS_ALLOWED_HOSTS"):
        return None
    return allowed_hosts(bind_host)
