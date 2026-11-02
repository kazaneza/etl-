"""Command line: etlplus start | use | projects | init | demo | quickstart | connections | jobs |
destinations | detect | parse | run | runs | serve | scheduler."""
from __future__ import annotations

import argparse
import os
import sys
import time


from etlplus.startup import StartError  # noqa: E402  (no heavy imports)

CHOOSE_FIRST = "Choose demo or your own data first: etlplus use demo, or etlplus use real."


class NoProject(Exception):
    pass


def _project(args):
    """--project PATH (a project, or a home and its active project), else the home's active one."""
    from etlplus import projects, startup
    project_arg = getattr(args, "project", None)
    home_arg = None if project_arg else str(startup.home_path(getattr(args, "home", None)))
    p, _home = projects.find_project(project_arg, home_arg)
    if p is None:
        raise NoProject(CHOOSE_FIRST)
    return p


def _home(args):
    from etlplus import projects, startup
    path = getattr(args, "home", None) or getattr(args, "project", None)
    home = startup.check_writable(startup.home_path(path))
    return projects.Home.open(home)


def _print_table(rows: list[list], headers: list[str]):
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


def cmd_init(args):
    from etlplus.config import Project
    path = args.path or args.project or "project"
    p = Project.init(path)
    print(f"Project written to {p.path}")
    print("Next: add a connection in connections.yaml and a job in jobs/, or run: etlplus serve --project " + str(p.path))


def cmd_demo(args):
    from etlplus import demo
    root = demo.generate(args.path or "demo")
    print(f"Demo project written to {root}")
    print(f"Run every job:      etlplus --project {root} run --all")
    print(f"Start the console:  etlplus --project {root} serve")


def cmd_quickstart(args):
    from etlplus import demo
    res = demo.quickstart(args.path or "demo", host=args.host, port=args.port)
    if args.serve:
        from etlplus import api
        api.serve(str(args.path or "demo"), host=args.host, port=args.port)
    failed = [n for n, r in (res or {}).items() if (r or {}).get("status") != "done"]
    return 1 if failed else 0


def cmd_connections(args):
    p = _project(args)
    if args.action == "list":
        try:
            from etlplus import secrets
        except ImportError:
            secrets = None
        rows = []
        for cid, spec in sorted(p.connections.items()):
            where = spec.get("path") or ":".join(str(x) for x in (spec.get("host"), spec.get("port")) if x)
            if spec.get("database") and spec.get("kind") != "folder":
                where += f"/{spec['database']}"
            pw = ""
            if secrets is not None and spec.get("kind") not in ("folder", "sqlite", "duckdb"):
                pw = "yes" if (secrets.has(p, cid) or {}).get("password") else ("env" if spec.get("password_env") else "no")
            rows.append([cid, spec.get("kind"), spec.get("role", "source"), where, pw])
        _print_table(rows, ["id", "kind", "role", "where", "password"])
        return 0
    from etlplus import connections
    ids = [args.id] if args.id else sorted(p.connections)
    bad = 0
    for cid in ids:
        if cid not in p.connections:
            print(f"{cid}: there is no such connection")
            bad += 1
            continue
        res = connections.test(p, cid)
        print(f"{cid}: {'ok' if res.get('ok') else 'FAILED'}" + (f"  ({res['server']})" if res.get("server") else ""))
        for s in res.get("steps") or []:
            print(f"   {'ok ' if s.get('ok') else 'ERR'} {s.get('name')}: {s.get('detail') or ''} "
                  f"{'(' + str(s.get('ms')) + ' ms)' if s.get('ms') is not None else ''}".rstrip())
        bad += 0 if res.get("ok") else 1
    return 1 if bad else 0


def cmd_jobs(args):
    from etlplus import runner, scheduler, store
    p = _project(args)
    last = store.last_runs(p)
    rows = []
    for name, job in sorted(p.jobs.items()):
        src = job.get("source") or {}
        t = job.get("target") or {}
        lr = last.get(name) or {}
        problems = runner.validate_job(p, job)
        rows.append([name, f"{src.get('connection')} -> {t.get('connection')}:{t.get('table')}", t.get("mode", "replace"),
                     job.get("schedule") or "", scheduler.next_run(p, name) or "",
                     f"{lr.get('status', '')} {lr.get('started_at', '')}".strip(),
                     f"{len(problems)} problem(s)" if problems else ""])
    _print_table(rows, ["job", "from -> to", "mode", "schedule", "next run", "last run", ""])
    return 0


def _print_run(rec):
    rows = rec.get("rows") or {}
    print(f"{rec['job']} {rec['run_id']}: {rec['status']}")
    for step, info in (rec.get("steps") or {}).items():
        extra = f", {info['rows']} rows" if info.get("rows") is not None else ""
        ms = f", {info['ms']} ms" if info.get("ms") is not None else ""
        print(f"   {step}: {info.get('status')}{extra}{ms}" + (f"  {info['detail']}" if info.get("detail") else ""))
    if rec.get("error"):
        print(f"   error: {rec['error']}")
    return rows


def cmd_run(args):
    from etlplus import runner
    p = _project(args)
    names = sorted(p.jobs) if args.all else [args.job]
    if not args.all and not args.job:
        print("Name a job, or use --all.")
        return 2
    steps = (args.step,) if args.step else runner.STEPS
    window = None
    if args.from_ or args.to:
        if not (args.from_ and args.to):
            print("Give both --from and --to (the window is [from, to)).")
            return 2
        window = (args.from_, args.to)
    worst = 0
    for name in names:
        if name not in p.jobs:
            print(f"There is no job named '{name}'.")
            worst = 1
            continue
        rec = runner.run(p, name, steps=steps, window=window, trigger="cli")
        _print_run(rec)
        if rec["status"] != "done":
            worst = 1
    return worst


def cmd_runs(args):
    from etlplus import store
    p = _project(args)
    if args.run_id:
        rec = store.get(p, args.run_id)
        if not rec:
            print(f"There is no run {args.run_id}.")
            return 1
        _print_run(rec)
        from etlplus import runner
        for line in runner.log_tail(p, rec["job"], rec["run_id"], 40):
            print("   | " + line)
        return 0
    rows = []
    for r in store.runs(p, job=args.job, limit=args.limit):
        n = r.get("rows") or {}
        rows.append([r.get("run_id"), r.get("job"), r.get("status"), r.get("trigger"), r.get("started_at"),
                     n.get("extracted", ""), n.get("loaded", ""), (r.get("error") or "")[:60]])
    _print_table(rows, ["run", "job", "status", "trigger", "started", "extracted", "loaded", "error"])
    return 0


def _serve_notes(args):
    if args.host not in ("127.0.0.1", "localhost") and not os.environ.get("ETLPLUS_TOKEN"):
        print("Note: the console is reachable from other machines and ETLPLUS_TOKEN is not set.")
    if not os.environ.get("ETLPLUS_TOKEN") and not os.environ.get("ETLPLUS_ALLOWED_HOSTS"):
        print("Only http://localhost and http://127.0.0.1 are answered. To open it by another name, set "
              "ETLPLUS_TOKEN, or list the names in ETLPLUS_ALLOWED_HOSTS.")


def _is_fixed_project(path) -> bool:
    from pathlib import Path
    from etlplus import projects
    p = Path(path)
    return (p / "etlplus.yaml").exists() and not (p / projects.PROJECTS_FILE).exists()


def _port_or_running(args) -> bool:
    """False when an etl+ already serves this port (the message is printed); raises StartError
    when another program holds it."""
    from etlplus import startup
    if startup.check_port(args.host, args.port) == "etlplus":
        print(f"etl+ is already running at {startup.local_url(args.host, args.port)}/app/")
        return False
    return True


def _print_notes(project):
    from etlplus import startup
    for line in startup.project_notes(project):
        print(line, flush=True)


def _open_browser_when_ready(url: str, host: str, port: int, wait: float = 60.0):
    """Open the console in the browser once the server answers (a background thread)."""
    import threading
    import webbrowser
    from etlplus import startup

    def go():
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if startup.running_etlplus(host, port, timeout=1.0):
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return
            time.sleep(0.3)

    threading.Thread(target=go, name="etlplus-browser", daemon=True).start()


def cmd_serve(args):
    """Never prompts. --start (default $ETLPLUS_START, else ask): with no active project,
    demo/empty makes the same choice as POST /api/start, the demo jobs in the background."""
    from etlplus import api, projects
    if getattr(args, "project", None) and not getattr(args, "home", None) and _is_fixed_project(args.project):
        from etlplus.config import Project
        p = Project.load(args.project)
        if not _port_or_running(args):
            return 1
        print(f"etl+ console on http://{args.host}:{args.port}/app/  (project {p.path})")
        _serve_notes(args)
        _print_notes(p)
        api.serve(p.path, host=args.host, port=args.port)
        return 0
    home = _home(args)
    if not getattr(args, "_checked_port", False) and not _port_or_running(args):
        return 1
    start = (getattr(args, "start", None) or projects.start_env()).lower()
    if start not in projects.START_ENV:
        print(f"--start is ask, demo or empty, not {start}.")
        return 2
    chose = getattr(args, "_chose", False)
    if home.active_id() is None and start in ("demo", "empty"):
        chose = True
        home.choose("demo" if start == "demo" else "real", run_jobs=True, background=(start == "demo"),
                    on_ready=_print_notes)
    active = home.active_id()
    print(f"etl+ console on http://{args.host}:{args.port}/app/  (home {home.path}, "
          f"{'project ' + active if active else ('the demo is being set up' if home.busy() else 'the console asks: demo data or your own data')})")
    _serve_notes(args)
    if active and not chose:
        _print_notes(home.active())
    if getattr(args, "_open_browser", False):
        from etlplus import startup
        _open_browser_when_ready(f"{startup.local_url(args.host, args.port)}/app/", args.host, args.port)
    api.serve(host=args.host, port=args.port, home=home)
    return 0


def cmd_start(args):
    """Choose demo or real (asking on a terminal when neither flag is given), then serve on
    127.0.0.1:8300 and open the browser. The home is --home, $ETLPLUS_HOME, $ETLPLUS_PROJECT, else a
    per-user folder (see startup.default_home)."""
    from etlplus import projects, startup
    home = _home(args)
    browser = not args.no_browser and os.environ.get("ETLPLUS_NO_BROWSER", "").strip() not in ("1", "true", "yes")
    if startup.check_port(args.host, args.port) == "etlplus":
        url = f"{startup.local_url(args.host, args.port)}/app/"
        print(f"etl+ is already running at {url}")
        if browser:
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0
    print(f"Home: {home.path}")
    choice = "demo" if args.demo else ("real" if args.real else None)
    if choice is None and home.active_id() is None and sys.stdin.isatty():
        print("etl+ first start:")
        print("  1) Start with demo data")
        print("  2) Connect my own data")
        ans = input("Choose 1 or 2: ").strip()
        choice = {"1": "demo", "2": "real"}.get(ans)
        if choice is None:
            print("Nothing chosen; the console will ask.")
    if choice is not None:
        try:
            home.choose(choice, run_jobs=True, background=(choice == "demo"), on_ready=_print_notes)
        except projects.Busy as e:
            print(str(e))
            return 1
        print(f"Active project: {choice} ({projects.LABELS[choice]}).")
        args._chose = True
    args.start = "ask"
    args.home = str(home.path)
    args.project = None
    args._checked_port = True
    args._open_browser = browser
    return cmd_serve(args)


def cmd_use(args):
    from etlplus import projects
    home = _home(args)
    try:
        p = home.choose(args.choice, run_jobs=False)
    except projects.Busy as e:
        print(str(e))
        return 1
    print(f"Active project: {args.choice} ({projects.LABELS[args.choice]}) at {p.path}")
    if args.choice == "demo":
        print("Run the demo jobs with: etlplus run --all")
    return 0


def cmd_projects(args):
    home = _home(args)
    st = home.state()
    rows = []
    for item in st["projects"]:
        rows.append(["*" if item["id"] == st["active"] else "", item["id"], item["label"],
                     "yes" if item["exists"] else "no", item["jobs"], item["connections"], item["destinations"],
                     str(home.project_path(item["id"]))])
    _print_table(rows, ["", "id", "label", "exists", "jobs", "connections", "destinations", "path"])
    if st["active"] is None:
        print(CHOOSE_FIRST)
    return 0


def _maybe_project(args):
    try:
        return _project(args)
    except Exception:
        return None


def cmd_detect(args):
    from pathlib import Path
    from etlplus import formats
    path = Path(args.file)
    if not path.is_file():
        print(f"No file {path}.")
        return 1
    found = formats.detect(path, project=_maybe_project(args))
    if not found:
        print("No bank format detected (CSV, JSON and the like are told by their extension).")
        return 1
    _print_table([[d["format"], f"{d['confidence']:.2f}", d.get("reason") or ""] for d in found],
                 ["format", "confidence", "why"])
    return 0


def cmd_parse(args):
    """Check a bank file and write it as Parquet: <out>.partial, then the rename."""
    from pathlib import Path
    import pyarrow.parquet as pq
    from etlplus import formats
    path, out = Path(args.file), Path(args.out)
    if not path.is_file():
        print(f"No file {path}.")
        return 1
    project = _maybe_project(args)
    opts = {"checks": "error"}
    if args.layout:
        opts["layout"] = args.layout
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    writer = None
    rows = 0
    try:
        for batch in formats.read(args.format, path, opts, project=project):
            if writer is None:
                writer = pq.ParquetWriter(str(partial), batch.schema, compression="zstd")
            writer.write_batch(batch)
            rows += batch.num_rows
        if writer is None:
            pq.write_table(formats.schema(args.format, opts, project=project).empty_table(), str(partial))
        else:
            writer.close()
            writer = None
        os.replace(partial, out)
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if partial.exists():
            partial.unlink()
        raise
    print(f"{rows} rows written to {out}")
    return 0


def cmd_destinations(args):
    from etlplus import destinations
    p = _project(args)
    action = args.action or "list"
    if action == "list":
        rows = [[d["id"], d["connection"], d["kind"], d["table"] or d.get("path") or "", d["design"] or "",
                 "yes" if d["exists"] else "no", ", ".join(d["jobs"])] for d in destinations.list(p)]
        _print_table(rows, ["id", "connection", "kind", "table or path", "design", "exists", "jobs"])
        return 0
    if not args.id:
        print(f"Name a destination: etlplus destinations {action} ID")
        return 2
    if args.id not in p.destinations:
        print(f"There is no destination named '{args.id}'.")
        return 1
    if action == "ddl":
        info = destinations.ddl_info(p, args.id)
        print(info["sql"] or "-- a file destination needs no table")
        for c in info["checks"]:
            print(f"-- {c['level']}: " + (f"{c['column']}: " if c.get("column") else "") + c["message"])
        return 0
    if action == "create":
        res = destinations.create(p, args.id)
        print("created" if res["created"] else "the table exists already")
        return 0
    res = destinations.test(p, args.id)
    for c in res["checks"]:
        print(f"{c['level']:<5} {c['message']}")
    return 0 if res["ok"] else 1


def cmd_scheduler(args):
    from etlplus import scheduler
    p = _project(args)
    scheduler.start(p)
    jobs = scheduler.scheduled()
    if not jobs:
        print("No job has a schedule. The scheduler keeps watching the project for changes.")
    for j in jobs:
        print(f"{j['job']}: next run {j['next_run']}")
    print("Scheduler running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
    return 0


def _env_port() -> int:
    v = os.environ.get("ETLPLUS_PORT", "").strip()
    return int(v) if v.isdigit() else 8300


def build_parser() -> argparse.ArgumentParser:
    from etlplus import __version__
    ap = argparse.ArgumentParser(prog="etlplus", description="etl+: extract, stage, transform, load.")
    ap.add_argument("--version", action="version", version=f"etlplus {__version__}")
    ap.add_argument("--project", "-p", default=None,
                    help="project or home folder (default $ETLPLUS_HOME, $ETLPLUS_PROJECT, ./project if it "
                         "exists, else the per-user home)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # --project is accepted before or after the command: etlplus -p demo serve == etlplus serve -p demo
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", "-p", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    s = sub.add_parser("init", parents=[common], help="write an empty project")
    s.add_argument("path", nargs="?")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("demo", parents=[common], help="write the demo project (no database needed)")
    s.add_argument("path", nargs="?", default="demo")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("quickstart", parents=[common], help="demo, run every job, print the console URL")
    s.add_argument("path", nargs="?", default="demo")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8300)
    s.add_argument("--serve", action="store_true", help="start the console afterwards")
    s.set_defaults(fn=cmd_quickstart)

    s = sub.add_parser("start", parents=[common], help="choose demo or your own data, then serve")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--demo", action="store_true", help="start with demo data")
    g.add_argument("--real", action="store_true", help="connect my own data (an empty project)")
    s.add_argument("--home", default=None, help="the home folder (default $ETLPLUS_HOME, $ETLPLUS_PROJECT, ./project if it exists, "
                        "else the per-user home)")
    s.add_argument("--host", default=os.environ.get("ETLPLUS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=_env_port())
    s.add_argument("--no-browser", action="store_true", help="do not open the browser")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("use", parents=[common], help="switch the active project (creates it if missing)")
    s.add_argument("choice", choices=["demo", "real"])
    s.add_argument("--home", default=None)
    s.set_defaults(fn=cmd_use)

    s = sub.add_parser("projects", parents=[common], help="list both projects, mark the active one")
    s.add_argument("--home", default=None)
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("detect", parents=[common], help="tell the bank format of a file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_detect)

    s = sub.add_parser("parse", parents=[common], help="check a bank file and write it as Parquet")
    s.add_argument("file")
    s.add_argument("--format", required=True)
    s.add_argument("--layout")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_parse)

    s = sub.add_parser("destinations", parents=[common], help="list destinations, show DDL, create, test")
    s.add_argument("action", nargs="?", choices=["list", "ddl", "create", "test"], default="list")
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_destinations)

    s = sub.add_parser("connections", parents=[common], help="list or test connections")
    s.add_argument("action", choices=["list", "test"])
    s.add_argument("id", nargs="?")
    s.set_defaults(fn=cmd_connections)

    s = sub.add_parser("jobs", parents=[common], help="list jobs")
    s.add_argument("action", nargs="?", choices=["list"], default="list")
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("run", parents=[common], help="run a job (or one step of it)")
    s.add_argument("job", nargs="?")
    s.add_argument("--all", action="store_true", help="run every job")
    s.add_argument("--step", "--steps", dest="step", choices=["extract", "transform", "map", "load"])
    s.add_argument("--from", dest="from_", help="window start, e.g. 2026-10-01")
    s.add_argument("--to", help="window end (not included), e.g. 2026-11-01")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("runs", parents=[common], help="run history, or one run with its log")
    s.add_argument("run_id", nargs="?")
    s.add_argument("--job")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("serve", parents=[common], help="start the API and the console")
    s.add_argument("--home", default=None, help="the home folder (default $ETLPLUS_HOME, $ETLPLUS_PROJECT, ./project if it exists, "
                        "else the per-user home)")
    s.add_argument("--start", default=None, choices=["ask", "demo", "empty"],
                   help="with no active project: ask in the console (default), or choose demo or empty")
    s.add_argument("--host", default=os.environ.get("ETLPLUS_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=_env_port())
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("scheduler", parents=[common], help="run the schedules in the foreground")
    s.set_defaults(fn=cmd_scheduler)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130
    except NoProject as e:
        print(str(e), file=sys.stderr)
        return 2
    except StartError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"etlplus: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
