"""The project folder: settings, connections and jobs as YAML files a person can edit."""

import os
import re
from pathlib import Path

import yaml

SETTINGS_FILE = "etlplus.yaml"
CONNECTIONS_FILE = "connections.yaml"
DESTINATIONS_FILE = "destinations.yaml"
JOBS_DIR = "jobs"
LAYOUTS_DIR = "layouts"
# files that hold state or secrets: no connection, pattern or path may ever read them
PROTECTED_NAMES = (".secret.key", "secrets.json.enc", "secrets.json", "projects.yaml", "runs.sqlite")

SECRET_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "access_key",
    "secret_key", "private_key", "client_secret", "smtp_password", "passphrase",
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

DEFAULT_SETTINGS = {
    "name": "etl+ project",
    "stage": "stage",
    "logs": "logs",
    "keep_runs": 10,
    "timezone": "UTC",
    "notify": {"on": "failure", "webhook": None, "email": None},
}


class JobProblems(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


_KEY_NOT_SECRET = {"access_key_id", "primary_key", "key", "keys"}
_EXTRA_SECRETS = {"secret_access_key", "sas_token", "account_key", "header_value", "passphrase"}


def _driver_secret_names() -> frozenset:
    try:
        from etlplus import drivers
        return frozenset(getattr(drivers, "SECRET_FIELD_NAMES", frozenset()) or frozenset())
    except Exception:
        return frozenset()


def is_secret_key(key: str) -> bool:
    """True for a setting that holds a password, token, key or other secret (SPEC_V2 2.1)."""
    k = str(key).lower()
    if k in SECRET_KEYS or k.endswith("_password") or k.endswith("_secret"):
        return True
    if k in _EXTRA_SECRETS or k in _driver_secret_names():
        return True
    if k.endswith("_token") or k.endswith("_sas"):
        return True
    # HTTP header names (webhooks): Authorization, X-Api-Key, X-Auth-Token, ...
    if k in ("authorization", "proxy-authorization", "cookie") or k.endswith("-key") or k.endswith("-token")             or k.endswith("-secret") or k.endswith("-password"):
        return True
    return k.endswith("_key") and k not in _KEY_NOT_SECRET


def strip_secrets(spec: dict) -> dict:
    """A copy of spec without secret values, at any depth (options included)."""
    out = {}
    for k, v in (spec or {}).items():
        if is_secret_key(k):
            continue
        out[k] = strip_secrets(v) if isinstance(v, dict) else v
    return out


def check_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(
            f"The {what} '{name}' is not valid: use letters, digits, '_', '-' or '.', "
            "starting with a letter or digit.")
    return name


def _read_yaml(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def known_kinds() -> set:
    """Every connection kind: the driver registry when it is there, plus the v1 list."""
    kinds = set()
    try:
        from etlplus import drivers
        kinds |= set(drivers.REGISTRY)
    except Exception:
        pass
    from .connections import KINDS
    kinds |= set(KINDS)
    return kinds


def _home_exclusions(path: Path) -> list[Path]:
    """When path is a project registered in a home (projects.yaml in it or up to two levels
    above), the home's other project folder and projects.yaml are not part of it."""
    path = Path(path).resolve()
    for home in (path, path.parent, path.parent.parent):
        f = home / "projects.yaml"
        if not f.exists():
            continue
        projects = (_read_yaml(f).get("projects") or {})
        paths = {pid: (home / str((info or {}).get("path") or pid)).resolve()
                 for pid, info in projects.items()}
        mine = [pid for pid, pp in paths.items() if pp == path]
        if not mine:
            continue
        return [f.resolve()] + [pp for pid, pp in paths.items() if pid not in mine and pp != path]
    return []


def write_yaml(p: Path, data: dict) -> None:
    """Atomic YAML write: <file>.partial, then rename."""
    _write_yaml(p, data)


def _write_yaml(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".partial")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, p)


class Project:
    def __init__(self, path, settings=None, connections=None, jobs=None, destinations=None):
        self.path = Path(path).resolve()
        self.settings = settings if settings is not None else dict(DEFAULT_SETTINGS)
        self.connections = connections if connections is not None else {}
        self.jobs = jobs if jobs is not None else {}
        self.destinations = destinations if destinations is not None else {}
        # folders and files inside this one that belong to someone else (the home's other
        # project, projects.yaml); filled from the home, empty for a lone project
        self.excluded: list[Path] = []

    def __repr__(self):
        return f"Project({str(self.path)!r})"

    @staticmethod
    def default_path() -> Path:
        return Path(os.environ.get("ETLPLUS_PROJECT") or "./project")

    @classmethod
    def load(cls, path=None) -> "Project":
        p = Path(path) if path else cls.default_path()
        if not (p / SETTINGS_FILE).exists():
            raise FileNotFoundError(
                f"No etl+ project at {p}: {SETTINGS_FILE} is missing. "
                "Run 'etlplus init' or 'etlplus demo' first.")
        proj = cls(p)
        proj.excluded = _home_exclusions(proj.path)
        proj.reload()
        return proj

    def reload(self) -> None:
        settings = dict(DEFAULT_SETTINGS)
        settings.update(_read_yaml(self.path / SETTINGS_FILE))
        self.settings = settings
        conns = _read_yaml(self.path / CONNECTIONS_FILE).get("connections") or {}
        self.connections = {str(k): dict(v or {}) for k, v in conns.items()}
        jobs = {}
        jdir = self.path / JOBS_DIR
        if jdir.is_dir():
            for f in sorted(jdir.glob("*.yaml")) + sorted(jdir.glob("*.yml")):
                job = _read_yaml(f)
                if not job:
                    continue
                name = str(job.get("name") or f.stem)
                job["name"] = name
                jobs[name] = job
        self.jobs = jobs
        dests = _read_yaml(self.path / DESTINATIONS_FILE).get("destinations") or {}
        self.destinations = {str(k): dict(v) for k, v in dests.items() if isinstance(v, dict)}

    @property
    def kind(self) -> str | None:
        """demo or real, from etlplus.yaml (None for a v1 project that never said)."""
        k = (self.settings or {}).get("kind")
        return k if k in ("demo", "real") else None

    @classmethod
    def init(cls, path, kind: str | None = None, name: str | None = None) -> "Project":
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        if not (p / SETTINGS_FILE).exists():
            settings = dict(DEFAULT_SETTINGS)
            settings["name"] = name or p.resolve().name or "etl+ project"
            if kind:
                settings["kind"] = kind
            _write_yaml(p / SETTINGS_FILE, settings)
        if not (p / CONNECTIONS_FILE).exists():
            _write_yaml(p / CONNECTIONS_FILE, {"connections": {}})
        (p / JOBS_DIR).mkdir(exist_ok=True)
        gi = p / ".gitignore"
        if not gi.exists():
            gi.write_text(
                ".secret.key\nsecrets.json.enc\nstage/\nlogs/\nruns.sqlite*\n.locks/\n", encoding="utf-8")
        return cls.load(p)

    # paths
    def resolve(self, p) -> Path:
        q = Path(os.path.expandvars(os.path.expanduser(str(p))))
        return q if q.is_absolute() else (self.path / q).resolve()

    def _in_excluded(self, rp: Path) -> bool:
        for ex in self.excluded or []:
            if rp == ex:
                return True
            try:
                rp.relative_to(ex)
                return True
            except ValueError:
                pass
        return False

    def inside(self, p) -> bool:
        """True when p is in this project's folder and not in the home's other project."""
        try:
            rp = Path(p).resolve()
            rp.relative_to(self.path)
        except (ValueError, OSError):
            return False
        return not self._in_excluded(rp)

    def protected(self, p) -> bool:
        """True for a secret or state file of any project (SPEC_V2 4.6), or anything in the
        home's other project. A read of such a path is refused."""
        try:
            rp = Path(p).resolve()
        except OSError:
            return True
        if rp.name in PROTECTED_NAMES or rp.name.startswith(".secret.key") or rp.name.startswith("runs.sqlite"):
            return True
        return self._in_excluded(rp)

    def stage_dir(self) -> Path:
        return self.resolve(self.settings.get("stage") or "stage")

    def logs_dir(self) -> Path:
        return self.resolve(self.settings.get("logs") or "logs")

    def db_path(self) -> Path:
        return self.resolve(self.settings.get("db") or "runs.sqlite")

    def jobs_dir(self) -> Path:
        return self.path / JOBS_DIR

    def job_file(self, name: str) -> Path:
        return self.jobs_dir() / f"{check_name(name, 'job name')}.yaml"

    def layouts_dir(self) -> Path:
        return self.path / LAYOUTS_DIR

    # settings
    def save_settings(self, settings: dict) -> None:
        merged = dict(self.settings)
        merged.update(settings or {})
        merged = strip_secrets(merged)
        _write_yaml(self.path / SETTINGS_FILE, merged)
        self.settings = merged

    # connections
    def _write_connections(self) -> None:
        _write_yaml(self.path / CONNECTIONS_FILE, {"connections": self.connections})

    def save_connection(self, cid: str, spec: dict) -> None:
        check_name(cid, "connection id")
        clean = strip_secrets(spec)
        clean.pop("id", None)
        clean.pop("password_set", None)
        kind = clean.get("kind")
        kinds = known_kinds()
        if kind not in kinds:
            raise ValueError(f"Unknown connection kind '{kind}'. Use one of: {', '.join(sorted(kinds))}.")
        role = clean.get("role") or "source"
        if role not in ("source", "target", "both"):
            raise ValueError("A connection's role is source, target or both.")
        clean["role"] = role
        self.connections[cid] = clean
        self._write_connections()

    def delete_connection(self, cid: str) -> None:
        if cid in self.connections:
            del self.connections[cid]
            self._write_connections()
        from . import secrets
        secrets.delete(self, cid)

    # destinations
    def _write_destinations(self) -> None:
        _write_yaml(self.path / DESTINATIONS_FILE, {"destinations": self.destinations})

    def save_destination(self, did: str, spec: dict) -> None:
        """Write one destination into destinations.yaml (atomic). Secrets never go there."""
        check_name(did, "destination id")
        clean = strip_secrets(dict(spec or {}))
        for k in ("id", "exists", "jobs", "kind", "columns_count", "rename_from"):
            clean.pop(k, None)
        self.destinations[did] = clean
        self._write_destinations()

    def delete_destination(self, did: str) -> None:
        if did in self.destinations:
            del self.destinations[did]
            self._write_destinations()

    # jobs
    def save_job(self, job: dict) -> None:
        job = dict(job or {})
        name = check_name(job.get("name"), "job name")
        try:
            from .runner import validate_job
        except ImportError:
            validate_job = None
        if validate_job is not None:
            problems = validate_job(self, job)
            if problems:
                raise JobProblems(problems)
        _write_yaml(self.job_file(name), job)
        self.jobs[name] = job

    def delete_job(self, name: str) -> None:
        f = self.job_file(name)
        if f.exists():
            f.unlink()
        alt = f.with_suffix(".yml")
        if alt.exists():
            alt.unlink()
        self.jobs.pop(name, None)
