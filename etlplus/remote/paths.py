"""Path and name safety for folders, shared folders and remote locations (section 4.6).

Every files pattern, archive_to, destination path or file name, and every name a server returns,
is a relative POSIX path: no absolute path, no drive, no UNC prefix, no backslash, no '..', no
NUL or control character, no empty part. Local names under incoming/ stay inside it. A local
path never reaches a secret or state file of any project, nor the other project's folder. URL
and file-name templates know exactly {date}, {date:<strftime>}, {run_id}, {job}, {format}.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")

STATE_FILES = (".secret.key", "secrets.json.enc", "projects.yaml", "runs.sqlite")


def check_rel(path, what="path", allow_glob=False) -> str:
    """The path as a clean relative POSIX path, or ValueError with a plain message."""
    if path is None:
        raise ValueError(f"The {what} is empty.")
    s = str(path)
    if s.strip() == "":
        raise ValueError(f"The {what} is empty.")
    if _CTRL.search(s):
        raise ValueError(f"The {what} '{s[:60]!r}' holds a control character.")
    if "\\" in s:
        raise ValueError(f"The {what} '{s}' holds a backslash; use / between folders.")
    if s.startswith("/") or _DRIVE.match(s) or s.startswith("//"):
        raise ValueError(f"The {what} '{s}' must be relative to the location.")
    s = s.rstrip("/") if s.endswith("/") and len(s) > 1 else s
    parts = s.split("/")
    for p in parts:
        if p == "":
            raise ValueError(f"The {what} '{s}' has an empty part.")
        if p == "..":
            raise ValueError(f"The {what} '{s}' must stay inside the location ('..' is not allowed).")
    parts = [p for p in parts if p != "."]
    if not parts:
        raise ValueError(f"The {what} '{s}' names no file.")
    return "/".join(parts)


def check_pattern(pattern) -> list[str]:
    pats = pattern if isinstance(pattern, (list, tuple)) else [pattern]
    out = [check_rel(p, "file pattern", allow_glob=True) for p in pats if str(p or "").strip()]
    if not out:
        raise ValueError("Set a file pattern, such as *.csv.")
    return out


def glob_regex(pattern: str) -> re.Pattern:
    """A glob as a regex over relative POSIX paths: * and ? stay in one folder, ** crosses folders."""
    i, n, out = 0, len(pattern), ["^"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def static_prefix(pattern: str) -> tuple[str, bool]:
    """(folder to list, recursive). 'in/MT940_*.txt' -> ('in', False); 'a/**/x' -> ('a', True)."""
    parts = pattern.split("/")
    fixed = []
    for p in parts[:-1]:
        if any(c in p for c in "*?["):
            break
        fixed.append(p)
    rest = parts[len(fixed):]
    recursive = len(rest) > 1 or "**" in pattern
    return "/".join(fixed), recursive


def match(patterns, path: str) -> bool:
    return any(glob_regex(p).match(path) for p in patterns)


def local_name(rel: str, dest_dir: Path) -> str:
    """The name of a fetched file under incoming/: its relative path with / replaced by __."""
    rel = check_rel(rel, "remote file name")
    name = rel.replace("/", "__")
    dest = Path(dest_dir).resolve()
    target = (dest / name).resolve()
    if target.parent != dest:
        raise ValueError(f"The remote file name '{rel}' would leave the stage folder.")
    return name


# ---------------------------------------------------------------- templates

_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^{}]*))?\}")
_STRFTIME = re.compile(r"^(?:%[A-Za-z]|[-_.])*$")


def fill_template(template: str, *, date=None, run_id=None, job=None, format=None,
                  allowed=("date", "run_id", "job", "format"), safe_name=False) -> str:
    """Fill {date}, {date:%Y%m%d}, {run_id}, {job}, {format}. Never str.format: anything else in
    braces (such as {date.__class__}) is refused."""
    template = str(template or "")
    values = {"date": date or dt.date.today(), "run_id": run_id, "job": job, "format": format}

    def sub(m):
        name, fmt = m.group(1), m.group(2)
        if name not in allowed:
            raise ValueError(f"'{{{name}}}' is not a placeholder here. Use: "
                             + ", ".join("{" + a + "}" for a in allowed) + ".")
        v = values.get(name)
        if name == "date":
            d = v if isinstance(v, (dt.date, dt.datetime)) else dt.date.fromisoformat(str(v))
            if fmt is None or fmt == "":
                return d.isoformat()[:10]
            if not _STRFTIME.match(fmt):
                raise ValueError(f"'{{date:{fmt}}}': use strftime letters only, such as %Y%m%d.")
            return d.strftime(fmt)
        if fmt is not None:
            raise ValueError(f"'{{{name}:...}}' takes no format.")
        if v is None:
            raise ValueError(f"'{{{name}}}' has no value here.")
        return str(v)

    out = _TOKEN.sub(sub, template)
    if "{" in out or "}" in out:
        raise ValueError("The template has a brace that is not a known placeholder.")
    if safe_name:
        if "/" in out:
            raise ValueError(f"The file name '{out}' must not hold a '/'.")
        check_rel(out, "file name")
        if out in (".", "..") or out.endswith(".partial"):
            raise ValueError(f"The file name '{out}' is not allowed.")
    return out


# ---------------------------------------------------------------- the etl+ home

def _resolve(p) -> Path:
    try:
        return Path(p).resolve()
    except OSError:
        return Path(os.path.abspath(p))


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def project_home(project) -> Path:
    """The folder that holds projects.yaml (a home), or the project itself."""
    path = _resolve(getattr(project, "path", project))
    if (path / "projects.yaml").exists():
        return path
    if (path.parent / "projects.yaml").exists():
        return path.parent
    if path.parent.name == "projects" and (path.parent.parent / "projects.yaml").exists():
        return path.parent.parent
    env = os.environ.get("ETLPLUS_HOME")
    if env and _inside(path, _resolve(env)):
        return _resolve(env)
    return path


def _registered(home: Path) -> list[Path]:
    f = home / "projects.yaml"
    out = []
    if not f.exists():
        return out
    if True:
        try:
            import yaml
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for v in (data.get("projects") or {}).values():
                p = (v or {}).get("path") if isinstance(v, dict) else v
                if p:
                    out.append(_resolve(home / str(p)))
        except Exception:
            pass
    for name in ("demo", "real", "demo.new", "demo.old", "projects"):
        out.append(_resolve(home / name))
    return out


def other_project_dirs(project) -> list[Path]:
    """Folders a path of this project may never reach: the other project, and for a legacy home
    (a project at the home root) the projects/ folder inside it."""
    me = _resolve(getattr(project, "path", project))
    home = project_home(project)
    out = []
    for p in _registered(home):
        if p == me or _inside(me, p):
            continue
        out.append(p)
    return out


def check_local(project, path) -> Path:
    """A local path a connection may read or write: never a secret or state file of any project,
    nor inside the other project's folder."""
    p = _resolve(path)
    name = p.name.lower()
    if name in STATE_FILES or name.startswith("runs.sqlite") or name.startswith("secrets.json") \
            or name.startswith(".secret.key"):
        raise ValueError(f"'{p.name}' is an etl+ secret or state file; a connection cannot use it.")
    for other in other_project_dirs(project):
        if p == other or _inside(p, other):
            raise ValueError("That path is inside the other etl+ project; a connection cannot use it.")
    return p


def share_root(project, raw) -> Path:
    """The root of a shared folder: absolute or UNC, never inside the etl+ home, and inside
    ETLPLUS_SHARE_ROOTS when that allow-list is set."""
    s = str(raw or "").strip()
    if not s:
        raise ValueError("Set the folder path.")
    if _CTRL.search(s):
        raise ValueError("The folder path holds a control character.")
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if not (p.is_absolute() or s.startswith("\\\\") or s.startswith("//")):
        raise ValueError("A shared folder is an absolute path or a UNC path. "
                         "For a folder inside the project, use the Folder kind.")
    root = _resolve(p)
    if project is not None:
        home = project_home(project)
        me = _resolve(getattr(project, "path", project))
        for protected in {home, me}:
            if root == protected or _inside(root, protected) or _inside(protected, root):
                raise ValueError("A shared folder cannot be the etl+ home or hold it; "
                                 "use the Folder kind for files inside the project.")
    allow = [a for a in os.environ.get("ETLPLUS_SHARE_ROOTS", "").split(os.pathsep) if a.strip()]
    if allow and not any(root == _resolve(a) or _inside(root, _resolve(a)) for a in allow):
        raise ValueError("That folder is outside ETLPLUS_SHARE_ROOTS, the folders this server may read.")
    return root
