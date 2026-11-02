"""Notifications: a webhook (POST JSON) and SMTP email.

Settings live in etlplus.yaml under `notify`:

    notify:
      on: failure                 # default for jobs that do not say: never | failure | always
      webhook: https://hooks.example.local/etl     # or {url: ..., headers: {...}}
      email:
        host: smtp.example.local
        port: 587
        security: starttls        # starttls | ssl | none
        user: etl@example.local   # optional
        from: etl@example.local
        to: [ops@example.local]

The SMTP password is kept in the secrets store under key "smtp". Webhook header values are
secrets too: they are kept under "notify:webhook" (one entry per header name), never in
etlplus.yaml, and the API returns only their names (headers_set).
"""
from __future__ import annotations

import json
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

LEVELS = ("never", "failure", "always")


def settings(project, override: dict | None = None) -> dict:
    s = dict((project.settings or {}).get("notify") or {})
    if override:
        s.update(override)
    return s


WEBHOOK_SECRET_ID = "notify:webhook"


def _webhook(cfg, project=None) -> tuple[str | None, dict]:
    """(url, headers). Headers come from the secrets store, plus any written by hand in the file."""
    url, headers = None, {}
    if isinstance(cfg, str):
        url = cfg or None
    elif isinstance(cfg, dict):
        url = cfg.get("url") or None
        headers = dict(cfg.get("headers") or {})
    if url and project is not None:
        try:
            from etlplus import secrets
            headers.update(secrets.get(project, WEBHOOK_SECRET_ID))
        except Exception:
            pass
    return url, headers


def send_webhook(url: str, payload: dict, headers: dict | None = None, timeout=10) -> str:
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "etlplus",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return f"HTTP {r.status}"


def send_email(project, cfg: dict, subject: str, text: str, password: str | None = None, timeout=20) -> str:
    host = cfg.get("host")
    to = cfg.get("to") or []
    if isinstance(to, str):
        to = [t.strip() for t in to.split(",") if t.strip()]
    if not host or not to:
        raise ValueError("Email needs a host and at least one recipient.")
    sender = cfg.get("from") or cfg.get("user") or "etlplus@localhost"
    security = (cfg.get("security") or "starttls").lower()
    port = int(cfg.get("port") or (465 if security == "ssl" else 587 if security == "starttls" else 25))
    if password is None:
        from etlplus import secrets
        password = (secrets.get(project, "smtp") or {}).get("password")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    ctx = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if security == "starttls":
            server.starttls(context=ctx)
        if cfg.get("user") and password:
            server.login(cfg["user"], password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return f"sent to {len(to)} recipient(s)"


def send(project, subject: str, text: str, payload: dict, override: dict | None = None,
         password: str | None = None) -> list[dict]:
    """Send on every configured channel. Never raises; returns one result per channel."""
    cfg = settings(project, override)
    out = []
    url, headers = _webhook(cfg.get("webhook"), project)
    if url:
        try:
            out.append({"channel": "webhook", "ok": True, "detail": send_webhook(url, payload, headers)})
        except Exception as e:
            out.append({"channel": "webhook", "ok": False, "detail": str(e)})
    email = cfg.get("email")
    if isinstance(email, dict) and email.get("host"):
        try:
            out.append({"channel": "email", "ok": True,
                        "detail": send_email(project, email, subject, text, password)})
        except Exception as e:
            out.append({"channel": "email", "ok": False, "detail": str(e)})
    return out


def run_message(project, run: dict) -> tuple[str, str, dict]:
    name = (project.settings or {}).get("name") or "etl+"
    status = run.get("status")
    job = run.get("job")
    subject = f"[{name}] {job}: {status}"
    lines = [f"Job {job} {status}.", f"Run {run.get('run_id')} started {run.get('started_at')}, "
             f"finished {run.get('finished_at')}.", ""]
    for step, info in (run.get("steps") or {}).items():
        lines.append(f"{step}: {info.get('status')}, {info.get('rows', '')} rows")
    if run.get("error"):
        lines += ["", "Error:", str(run["error"])]
    payload = {"project": name, "event": "run." + str(status), "job": job, "run_id": run.get("run_id"),
               "status": status, "trigger": run.get("trigger"), "window": run.get("window"),
               "rows": run.get("rows"), "error": run.get("error"),
               "started_at": run.get("started_at"), "finished_at": run.get("finished_at")}
    return subject, "\n".join(lines), payload


def should_notify(job: dict, run: dict, default: str = "failure") -> bool:
    level = str(job.get("notify") or default or "failure").lower()
    if level == "always":
        return run.get("status") in ("done", "failed", "cancelled")
    if level == "failure":
        return run.get("status") == "failed"
    return False


def notify_run(project, job: dict, run: dict) -> list[dict] | None:
    if not should_notify(job, run, settings(project).get("on") or "failure"):
        return None
    subject, text, payload = run_message(project, run)
    return send(project, subject, text, payload)


def test(project, override: dict | None = None, password: str | None = None) -> dict:
    """Send a test message on every configured channel."""
    cfg = settings(project, override)
    if not _webhook(cfg.get("webhook"))[0] and not (isinstance(cfg.get("email"), dict) and cfg["email"].get("host")):
        return {"ok": False, "channels": [], "detail": "No webhook or email is set up."}
    name = (project.settings or {}).get("name") or "etl+"
    results = send(project, f"[{name}] test message", "This is a test message from etl+.",
                   {"project": name, "event": "test", "message": "This is a test message from etl+."},
                   override=override, password=password)
    return {"ok": all(r["ok"] for r in results), "channels": results}
