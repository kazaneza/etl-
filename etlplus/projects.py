"""The home: two projects side by side, demo and real, and which one is active (SPEC_V2 11).

    <home>/projects.yaml   active: demo|real|null ; projects: {demo: {path: demo}, real: {path: real}}
    <home>/demo/           a full project (etlplus.yaml has kind: demo)
    <home>/real/           a full project (kind: real)

projects.yaml is written atomically and read on every call, so the CLI and the server agree.
Switching changes `active` and nothing else: both projects keep their own stage, runs.sqlite
and secrets. A 0.1.0 project at the home's root (a legacy volume) is registered as it is.
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import yaml

from etlplus import config

PROJECTS_FILE = "projects.yaml"
CHOICES = ("demo", "real")
LABELS = {"demo": "Demo data", "real": "My data"}
START_ENV = ("ask", "demo", "empty")

_setup_lock = threading.Lock()
_setup: dict[str, dict] = {}        # home path -> {"done","total","running","thread","stage"}


class Busy(RuntimeError):
    pass


class FixedMode(RuntimeError):
    pass


def start_env() -> str:
    v = (os.environ.get("ETLPLUS_START") or "ask").strip().lower()
    return v if v in START_ENV else "ask"


def resolve_home(path=None) -> Path:
    """--home, then $ETLPLUS_HOME, then $ETLPLUS_PROJECT, then ./project."""
    p = path or os.environ.get("ETLPLUS_HOME") or os.environ.get("ETLPLUS_PROJECT") or "./project"
    return Path(p)


def _read(f: Path) -> dict:
    if not f.exists():
        return {}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_kind(root: Path) -> str:
    s = _read(root / config.SETTINGS_FILE)
    if s.get("kind") == "demo" or s.get("name") == "etl+ demo":
        return "demo"
    return "real"


def setup(project) -> dict:
    """GET /api/setup: which first steps a real project has done, from its contents."""
    if project is None:
        return {"steps": [{"id": s, "done": False} for s in ("connection", "destination", "job")], "done": False}
    steps = [
        {"id": "connection", "done": bool(project.connections)},
        {"id": "destination", "done": bool(getattr(project, "destinations", None))},
        {"id": "job", "done": bool(project.jobs)},
    ]
    return {"steps": steps, "done": all(s["done"] for s in steps)}


class Home:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def __repr__(self):
        return f"Home({str(self.path)!r})"

    # ------------------------------------------------------------ registry
    @property
    def file(self) -> Path:
        return self.path / PROJECTS_FILE

    @classmethod
    def open(cls, path) -> "Home":
        """Create projects.yaml when it is missing. A project at the root is registered as it is."""
        h = cls(path)
        h.path.mkdir(parents=True, exist_ok=True)
        if not h.file.exists():
            if (h.path / config.SETTINGS_FILE).exists():
                kind = _legacy_kind(h.path)
                other = "real" if kind == "demo" else "demo"
                data = {"active": kind, "projects": {kind: {"path": "."}, other: {"path": f"projects/{other}"}}}
            else:
                data = {"active": None, "projects": {"demo": {"path": "demo"}, "real": {"path": "real"}}}
            h._write(data)
        return h

    def data(self) -> dict:
        d = _read(self.file)
        projects = d.get("projects") if isinstance(d.get("projects"), dict) else {}
        for pid in CHOICES:
            projects.setdefault(pid, {"path": pid})
        active = d.get("active") if d.get("active") in CHOICES else None
        return {"active": active, "projects": projects}

    def _write(self, data: dict) -> None:
        config.write_yaml(self.file, data)

    def project_path(self, pid: str) -> Path:
        if pid not in CHOICES:
            raise ValueError("Choose demo or real.")
        info = self.data()["projects"].get(pid) or {}
        rel = str(info.get("path") or pid)
        p = (self.path / rel).resolve()
        try:
            p.relative_to(self.path)
        except ValueError:
            raise ValueError(f"The {pid} project path leaves the home.") from None
        return p

    def exists(self, pid: str) -> bool:
        return (self.project_path(pid) / config.SETTINGS_FILE).exists()

    def load(self, pid: str):
        return config.Project.load(self.project_path(pid))

    def active_id(self) -> str | None:
        a = self.data()["active"]
        if a and not self.exists(a):
            return None
        return a

    def active(self):
        a = self.active_id()
        return self.load(a) if a else None

    def _set_active(self, pid: str) -> None:
        d = self.data()
        d["active"] = pid
        self._write(d)

    # ------------------------------------------------------------ state
    def setup_run(self) -> dict | None:
        with _setup_lock:
            s = _setup.get(str(self.path))
            if not s:
                return None
            th = s.get("thread")
            if th is not None and not th.is_alive() and s["done"] >= s["total"]:
                return {"done": s["done"], "total": s["total"], "running": None, "finished": True}
            return {"done": s["done"], "total": s["total"], "running": s.get("running")}

    def busy(self) -> bool:
        with _setup_lock:
            s = _setup.get(str(self.path))
            th = (s or {}).get("thread")
            return th is not None and th.is_alive()

    def state(self) -> dict:
        active = self.active_id()
        out = {"state": "ready" if active else "choose", "mode": "home", "active": active,
               "start_env": start_env(), "projects": []}
        for pid in CHOICES:
            item = {"id": pid, "label": LABELS[pid], "exists": False, "name": None, "jobs": 0,
                    "connections": 0, "destinations": 0, "setup_done": False}
            try:
                if self.exists(pid):
                    p = self.load(pid)
                    item.update({"exists": True, "name": (p.settings or {}).get("name"), "jobs": len(p.jobs),
                                 "connections": len(p.connections), "destinations": len(p.destinations),
                                 "setup_done": setup(p)["done"]})
            except Exception:
                pass
            out["projects"].append(item)
        run = self.setup_run()
        if run is not None and not run.get("finished"):
            out["setup_run"] = run
        elif run is not None:
            out["setup_run"] = run
        return out

    # ------------------------------------------------------------ choose, reset
    def choose(self, choice: str, run_jobs: bool = True, background: bool = False, on_ready=None):
        """Create the project when missing, then make it active. Demo jobs run one after another
        in one background thread. background=True also generates the demo in that thread."""
        if choice not in CHOICES:
            raise ValueError("Choose demo or real.")
        if self.busy():
            raise Busy("The demo is still being set up.")
        path = self.project_path(choice)
        if self.exists(choice):
            self._set_active(choice)
            p = self.load(choice)
            if on_ready:
                on_ready(p)
            return p
        if choice == "real":
            path.mkdir(parents=True, exist_ok=True)
            p = config.Project.init(path, kind="real", name="My data")
            self._set_active("real")
            p = self.load("real")
            if on_ready:
                on_ready(p)
            return p
        from etlplus import demo
        if background:
            self._start_setup(generate=True, run_jobs=run_jobs, on_ready=on_ready)
            return None
        demo.generate(path)
        self._set_active("demo")
        p = self.load("demo")
        if on_ready:
            on_ready(p)
        if run_jobs:
            self._start_setup(generate=False, run_jobs=True)
        return p

    def _start_setup(self, generate: bool, run_jobs: bool, on_ready=None) -> None:
        from etlplus import demo
        key = str(self.path)
        total = len(demo.JOB_ORDER) if run_jobs else 0
        state = {"done": 0, "total": total, "running": "Creating demo" if generate else None, "thread": None}

        def work():
            try:
                if generate:
                    demo.generate(self.project_path("demo"))
                    self._set_active("demo")
                    p = self.load("demo")
                    if on_ready:
                        try:
                            on_ready(p)
                        except Exception:
                            pass
                if run_jobs:
                    self._run_demo_jobs(state)
            finally:
                with _setup_lock:
                    state["running"] = None
                    state["done"] = state["total"]

        th = threading.Thread(target=work, name="etlplus-demo-setup", daemon=True)
        state["thread"] = th
        with _setup_lock:
            _setup[key] = state
        th.start()

    def _run_demo_jobs(self, state) -> None:
        from etlplus import demo, runner
        p = self.load("demo")
        names = [n for n in demo.JOB_ORDER if n in p.jobs]
        with _setup_lock:
            state["total"] = len(names)
        for i, name in enumerate(names):
            with _setup_lock:
                state["running"] = name
            try:
                runner.run(p, name, trigger="setup")
            except Exception:
                pass
            with _setup_lock:
                state["done"] = i + 1

    def wait_setup(self, timeout=None) -> None:
        with _setup_lock:
            th = (_setup.get(str(self.path)) or {}).get("thread")
        if th is not None:
            th.join(timeout)

    def reset_demo(self, run_jobs: bool = False):
        """Regenerate the demo into demo.new/, then swap folders, so a failure leaves the old one."""
        from etlplus import demo, runner
        if self.busy():
            raise Busy("The demo is still being set up.")
        path = self.project_path("demo")
        if self.exists("demo"):
            live = runner.running(self.load("demo"))
            if live:
                raise Busy("A demo job is running; try again when it ends.")
        if path == self.path:
            # a legacy demo at the home's root: regenerate in place (nothing to swap)
            demo.generate(path)
            return self.load("demo")
        new = path.with_name(path.name + ".new")
        old = path.with_name(path.name + ".old")
        for leftover in (new, old):
            if leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)
        try:
            demo.generate(new)
        except BaseException:
            shutil.rmtree(new, ignore_errors=True)
            raise
        if path.exists():
            os.replace(path, old)
        try:
            os.replace(new, path)
        except BaseException:
            if old.exists() and not path.exists():
                os.replace(old, path)
            raise
        shutil.rmtree(old, ignore_errors=True)
        if run_jobs:
            self._start_setup(generate=False, run_jobs=True)
        return self.load("demo")


def find_project(project_arg=None, home_arg=None):
    """How the CLI finds its project (SPEC_V2 9.6). Returns (project or None, home or None)."""
    if project_arg:
        p = Path(project_arg)
        if (p / config.SETTINGS_FILE).exists() and not (p / PROJECTS_FILE).exists():
            return config.Project.load(p), None
        if (p / PROJECTS_FILE).exists():
            h = Home.open(p)
            return h.active(), h
        if (p / config.SETTINGS_FILE).exists():
            h = Home.open(p)
            return h.active(), h
        raise FileNotFoundError(f"No etl+ project or home at {p}.")
    h = Home.open(resolve_home(home_arg))
    return h.active(), h
