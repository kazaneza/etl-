"""Cron schedules per job (APScheduler BackgroundScheduler) in the project timezone."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_lock = threading.RLock()
_sched: BackgroundScheduler | None = None
_project = None
_signature = None


def timezone(project):
    tz = (project.settings or {}).get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:
        from zoneinfo import ZoneInfo
        return ZoneInfo("UTC")


_DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _day_number(token: str) -> int:
    t = token.strip().lower()
    if t[:3] in _DAY_NAMES and t.isalpha():
        return _DAY_NAMES.index(t[:3])
    if not t.isdigit() or int(t) > 7:
        raise ValueError(f"'{token}' is not a day of the week (0-7 or sun-sat).")
    return int(t) % 7


def crontab_dow(field: str) -> str:
    """A crontab day-of-week field (0 or 7 = Sunday, 1 = Monday) as APScheduler day names.

    APScheduler numbers Monday as 0, so a crontab '1-5' passed through as it is would run
    Tuesday to Saturday. Names mean the same in both.
    """
    field = str(field).strip()
    if field in ("*", "?"):
        return "*"
    days = set()
    for part in field.split(","):
        base, _, step = part.partition("/")
        step_n = int(step) if step else 1
        if step and (not step.isdigit() or step_n < 1):
            raise ValueError(f"'{part}' has a bad step.")
        if base in ("*", "?"):
            lo, hi = 0, 6
        elif "-" in base:
            a, b = base.split("-", 1)
            lo, hi = _day_number(a), _day_number(b)
            if b.strip() == "7":
                hi = 7
            if hi < lo:
                raise ValueError(f"'{part}': the range runs backwards.")
        else:
            lo = _day_number(base)
            hi = 6 if step else lo
        days.update(d % 7 for d in range(lo, hi + 1, step_n))
    return ",".join(_DAY_NAMES[d] for d in sorted(days))


def cron_trigger(cron: str, tz=None) -> CronTrigger:
    """A CronTrigger that reads a standard 5-field crontab line the way cron does."""
    parts = str(cron or "").split()
    if len(parts) != 5:
        raise ValueError(f"A cron line has 5 fields (minute hour day month weekday), got {len(parts)}.")
    minute, hour, day, month, dow = parts
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=crontab_dow(dow),
                       timezone=tz)


def trigger(project, cron: str) -> CronTrigger:
    return cron_trigger(cron, timezone(project))


def next_run(project, name: str) -> str | None:
    """Next fire time of a job as ISO text, whether or not the scheduler is running."""
    job = (project.jobs or {}).get(name) or {}
    cron = job.get("schedule")
    if not cron:
        return None
    try:
        t = trigger(project, cron).get_next_fire_time(None, datetime.now(timezone(project)))
    except Exception:
        return None
    return t.isoformat() if t else None


def next_runs(project) -> dict[str, str | None]:
    return {name: next_run(project, name) for name in (project.jobs or {})}


_DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def describe(cron: str) -> str:
    """A plain reading of the common cron shapes; the cron line itself otherwise."""
    parts = str(cron or "").split()
    if len(parts) != 5:
        return "No schedule" if not cron else f"cron {cron}"
    mi, hr, dom, mon, dow = parts
    if mi.isdigit() and hr.isdigit():
        at = f"at {int(hr):02d}:{int(mi):02d}"
        if dom == "*" and mon == "*" and dow == "*":
            return f"Every day {at}"
        if dom == "*" and mon == "*" and dow in ("1-5", "MON-FRI", "mon-fri"):
            return f"Every weekday {at}"
        if dom == "*" and mon == "*" and dow.isdigit() and int(dow) <= 7:
            return f"Every {_DOW[int(dow) % 7]} {at}"
        if dom.isdigit() and mon == "*" and dow == "*":
            return f"On day {int(dom)} of every month {at}"
    if mi.isdigit() and hr == "*" and dom == mon == dow == "*":
        return f"Every hour at minute {int(mi)}"
    if mi.startswith("*/") and hr == dom == mon == dow == "*":
        return f"Every {mi[2:]} minutes"
    if mi.isdigit() and hr.startswith("*/") and dom == mon == dow == "*":
        return f"Every {hr[2:]} hours at minute {int(mi)}"
    if parts == ["*"] * 5:
        return "Every minute"
    return f"cron {cron}"


def _fire(name):
    from etlplus import runner
    with _lock:
        project = _project
    if project is None:
        return
    try:
        runner.run(project, name, trigger="schedule")
    except RuntimeError:
        pass


def _files_signature(path: Path):
    files = [path / "etlplus.yaml", path / "connections.yaml", path / "destinations.yaml"]
    jobs = path / "jobs"
    if jobs.is_dir():
        files += sorted(jobs.glob("*.y*ml"))
    sig = []
    for f in files:
        try:
            st = f.stat()
            sig.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)


def _apply(project):
    """Replace every scheduled job with the project's current schedules."""
    global _project, _signature
    _project = project
    _signature = _files_signature(Path(project.path))
    for j in list(_sched.get_jobs()):
        if j.id.startswith("job:"):
            j.remove()
    for name, job in (project.jobs or {}).items():
        cron = job.get("schedule")
        if not cron:
            continue
        try:
            trg = trigger(project, cron)
        except Exception:
            continue
        _sched.add_job(_fire, trg, args=[name], id=f"job:{Path(project.path).resolve()}::{name}", name=name,
                       max_instances=1,
                       coalesce=True, misfire_grace_time=3600, replace_existing=True)


def _watch():
    """Reload when the project files change on disk (a job saved from the console or edited by hand)."""
    with _lock:
        project = _project
        if project is None:
            return
        if _files_signature(Path(project.path)) == _signature:
            return
        try:
            from etlplus.config import Project
            fresh = Project.load(project.path)
        except Exception:
            return
        _apply(fresh)


def start(project) -> BackgroundScheduler:
    """Start the scheduler once per process; a second call reloads it."""
    global _sched
    try:
        from etlplus import runner
        runner.reap(project)
    except Exception:
        pass
    with _lock:
        if _sched is None:
            _sched = BackgroundScheduler(timezone=timezone(project))
            _sched.add_job(_watch, "interval", seconds=30, id="etlplus:watch", coalesce=True, max_instances=1)
            _sched.start()
        _apply(project)
        return _sched


def reload(project=None) -> None:
    with _lock:
        if _sched is None:
            return
        if project is None and _project is not None:
            from etlplus.config import Project
            project = Project.load(_project.path)
        if project is not None:
            _apply(project)


def restart(project) -> BackgroundScheduler:
    """Stop the schedules of the project that was active and start them on this one. Only the
    active project's schedules run; runs in progress elsewhere finish where they are."""
    stop()
    return start(project)


def current_project():
    with _lock:
        return _project


def stop() -> None:
    global _sched, _project
    with _lock:
        if _sched is not None:
            _sched.shutdown(wait=False)
        _sched = None
        _project = None


def running() -> bool:
    return _sched is not None


def scheduled() -> list[dict]:
    with _lock:
        if _sched is None:
            return []
        return [{"job": j.name, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
                for j in _sched.get_jobs() if j.id.startswith("job:")]
