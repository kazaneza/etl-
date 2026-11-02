"""The demo project: a made-up bank that runs with no database server.

`generate(path)` writes a complete etl+ project: a SQLite "core" source, a landing
folder with one file per format (bank files included), a DuckDB warehouse, a DuckDB
reporting database fed through destinations and column mappings, and a Parquet folder
for an offerkit Data hub, plus connections and jobs that exercise every mode.
Everything is made up: people are ids, accounts start DEMO, BICs use country ZZ and the
currencies are USD, EUR and XTS (the ISO test code). The same seed and day always give
the same data.
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

DAYS = 120
SEGMENTS = ["mass", "mass", "mass", "affluent", "youth", "sme"]
DISTRICTS = ["North", "South", "East", "West", "Central", "Harbour", "Uptown"]
BRANCHES = [
    ("BR001", "Main Street", "Central"),
    ("BR002", "Market Square", "Central"),
    ("BR003", "Hillside", "North"),
    ("BR004", "Harbour Front", "Harbour"),
    ("BR005", "Riverside", "South"),
    ("BR006", "Airport Road", "East"),
    ("BR007", "Orchard Lane", "Uptown"),
    ("BR008", "Station Road", "West"),
]
PRODUCTS = {
    "CUR01": "current", "SAV01": "savings", "SAV02": "savings",
    "FD01": "fixed_deposit", "LN01": "loan", "WAL01": "wallet",
}
# code, direction, typical amount, share of activity
TXN_CODES = [
    ("SAL", "C", 3200, 0.04), ("TRF_IN", "C", 420, 0.10), ("WALLET_IN", "C", 110, 0.12),
    ("POS", "D", 85, 0.22), ("ATM", "D", 280, 0.14), ("WALLET_OUT", "D", 70, 0.16),
    ("BILL", "D", 175, 0.10), ("FEE", "D", 4, 0.08), ("REV", "C", 85, 0.04),
]
BILLERS = ["WATER", "POWER", "TV", "SCHOOL", "INSURANCE", "AIRTIME"]
EVENTS = ["login", "view_balance", "transfer", "pay_bill", "logout"]
CHANNELS = ["app", "app", "web", "ussd"]
FIXED_WIDTHS = [6, 24, 16, 10]
FIXED_NAMES = ["branch_code", "branch_name", "district", "opened_on"]

# The order the demo jobs run in, one after another (never in parallel: memory is short and
# several jobs load the same DuckDB file). quickstart and the chooser's setup run use it.
JOB_ORDER = [
    "core_customers", "core_accounts", "core_transactions",
    "file_customers", "file_payments", "file_branches", "file_events", "file_rates",
    "hub_daily_txn",
    "customers_to_reporting", "bank_mt940_lines", "bank_camt053_entries", "card_daily_ni_style",
    "payments_mt103",
]


def generate(path="demo", today: date | None = None, seed: int = 7, customers: int = 400) -> Path:
    """Write the demo project at `path` and return its folder."""
    root = Path(path).resolve()
    if root.exists() and any(root.iterdir()) and not (root / "etlplus.yaml").exists():
        raise FileExistsError(f"{root} is not empty and is not an etl+ project; pick another folder")
    today = today or date.today()
    rng = random.Random(seed)

    root.mkdir(parents=True, exist_ok=True)
    for sub in ("landing", "hub", "jobs"):
        (root / sub).mkdir(exist_ok=True)

    people = _customers(rng, customers, today)
    accounts = _accounts(rng, people, today)
    txns = _transactions(rng, accounts, today)
    _write_core(root / "core.db", people, accounts, txns)

    landing = root / "landing"
    _write_customers_csv(landing / "customers.csv", rng, people, today)
    _write_payments_txt(landing / "payments.txt", rng, people, today)
    _write_branches_fixed(landing / "branches.txt")
    _write_events_jsonl(landing / "events.jsonl", rng, people, today)
    has_excel = _write_rates_xlsx(landing / "rates.xlsx", rng, today)
    bank = _write_bank_samples(landing, today)

    reporting = root / "reporting.duckdb"
    if not reporting.exists():
        import duckdb
        duckdb.connect(str(reporting)).close()   # the demo destination database, created empty

    _write_yaml(root / "etlplus.yaml", _settings())
    _write_yaml(root / "connections.yaml", {"connections": _connections()})
    _write_yaml(root / "destinations.yaml", {"destinations": _destinations()})
    jobs_dir = root / "jobs"
    for old in jobs_dir.glob("*.yaml"):
        old.unlink()
    for job in _jobs(has_excel, bank):
        _write_yaml(jobs_dir / f"{job['name']}.yaml", job)
    (root / "README.txt").write_text(_readme_text(today), encoding="utf-8")
    return root


def quickstart(path="demo", host: str = "127.0.0.1", port: int = 8300, today: date | None = None,
               out=print) -> dict:
    """Write the demo, run every job once, print the console URL. Returns run records by job."""
    try:
        from etlplus import config, runner
    except ImportError as e:
        raise RuntimeError(f"quickstart needs etlplus.runner and etlplus.config, which could not be imported: {e}") from e

    root = generate(path, today=today)
    project = config.Project.load(root)
    out(f"Demo project written to {root}")
    results = {}
    names = [n for n in JOB_ORDER if n in project.jobs] + sorted(n for n in project.jobs if n not in JOB_ORDER)
    for name in names:
        rec = runner.run(project, name, trigger="quickstart")
        results[name] = rec
        rows = rec.get("rows") or {}
        loaded = rows.get("load", rows.get("loaded", ""))
        line = f"  {name:<24} {rec.get('status', '?'):<8} {loaded}"
        if rec.get("error"):
            line += f"  {rec['error']}"
        out(line)
    failed = [n for n, r in results.items() if r.get("status") != "done"]
    out(f"{len(results) - len(failed)} of {len(results)} jobs done" + (f", failed: {', '.join(failed)}" if failed else ""))
    out(f"Start the console:  etlplus serve --project {root}")
    out(f"Then open           http://{host}:{port}/app/")
    return results


# ---- data ------------------------------------------------------------------

def _ts(d: date, rng: random.Random) -> str:
    return datetime(d.year, d.month, d.day, rng.randint(6, 21), rng.randint(0, 59), rng.randint(0, 59)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _customers(rng, n, today):
    out = []
    for i in range(n):
        opened = today - timedelta(days=rng.randint(DAYS, 365 * 12))
        updated = today - timedelta(days=rng.randint(0, DAYS))
        out.append({
            "customer_number": f"C{100001 + i}",
            "segment": rng.choice(SEGMENTS),
            "branch_code": rng.choice(BRANCHES)[0],
            "birth_year": rng.randint(1950, 2006),
            "opened_on": opened.isoformat(),
            "updated_at": _ts(updated, rng),
        })
    return out


def _accounts(rng, people, today):
    out = []
    seq = 0
    for p in people:
        codes = ["CUR01"] + rng.sample([c for c in PRODUCTS if c != "CUR01"], rng.choice([0, 0, 1, 1, 2]))
        for code in codes:
            seq += 1
            opened = max(date.fromisoformat(p["opened_on"]), today - timedelta(days=rng.randint(30, 365 * 8)))
            closed = None
            if code != "CUR01" and rng.random() < 0.06:
                closed = (today - timedelta(days=rng.randint(1, DAYS))).isoformat()
            out.append({
                "account_number": f"A{200000001 + seq}",
                "customer_number": p["customer_number"],
                "product_code": code,
                "currency": "USD" if rng.random() < 0.05 else "XTS",
                "open_date": opened.isoformat(),
                "close_date": closed,
                "updated_at": _ts(today - timedelta(days=rng.randint(0, DAYS)), rng),
            })
    return out


def _transactions(rng, accounts, today):
    active = [a for a in accounts if a["product_code"] in ("CUR01", "WAL01", "SAV01")]
    weights = [c[3] for c in TXN_CODES]
    out = []
    seq = 0
    start = today - timedelta(days=DAYS)
    for a in active:
        rate = rng.choice([0.1, 0.25, 0.4, 0.7])
        pay_day = rng.randint(24, 28)
        for k in range(DAYS):
            d = start + timedelta(days=k)
            todays = []
            if d.day == pay_day and rng.random() < 0.5 and a["product_code"] == "CUR01":
                todays.append(TXN_CODES[0])
            n = sum(1 for _ in range(3) if rng.random() < rate / 3)
            todays += rng.choices(TXN_CODES, weights=weights, k=n)
            for code, direction, typical, _ in todays:
                seq += 1
                amount = round(typical * rng.uniform(0.3, 1.8), 2)
                out.append((f"T{seq:09d}", a["account_number"], a["customer_number"], d.isoformat(),
                            code, direction, amount if direction == "C" else -amount, _ts(d, rng)))
    return out


def _write_core(path: Path, people, accounts, txns):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript("""
            CREATE TABLE customers (
                customer_number TEXT PRIMARY KEY, segment TEXT, branch_code TEXT,
                birth_year INTEGER, opened_on TEXT, updated_at TEXT);
            CREATE TABLE products (product_code TEXT PRIMARY KEY, family TEXT);
            CREATE TABLE accounts (
                account_number TEXT PRIMARY KEY, customer_number TEXT, product_code TEXT,
                currency TEXT, open_date TEXT, close_date TEXT, updated_at TEXT);
            CREATE TABLE transactions (
                txn_id TEXT PRIMARY KEY, account_number TEXT, customer_number TEXT,
                booking_date TEXT, txn_code TEXT, dir TEXT, amount REAL, created_at TEXT);
            CREATE INDEX ix_txn_booking_date ON transactions(booking_date);
            CREATE INDEX ix_accounts_updated_at ON accounts(updated_at);
        """)
        con.executemany("INSERT INTO customers VALUES (:customer_number, :segment, :branch_code, :birth_year, "
                        ":opened_on, :updated_at)", people)
        con.executemany("INSERT INTO products VALUES (?, ?)", sorted(PRODUCTS.items()))
        con.executemany("INSERT INTO accounts VALUES (:account_number, :customer_number, :product_code, :currency, "
                        ":open_date, :close_date, :updated_at)", accounts)
        con.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", txns)
        con.commit()
    finally:
        con.close()


def _write_customers_csv(path, rng, people, today):
    notes = ["reviewed, no change", "address updated", "phone confirmed", "", "documents renewed"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["customer_number", "district", "phone_on_file", "email_on_file", "reviewed_on", "note"])
        for p in people:
            if rng.random() < 0.6:
                w.writerow([p["customer_number"], rng.choice(DISTRICTS), rng.choice("YYYN"), rng.choice("YNN"),
                            (today - timedelta(days=rng.randint(1, 60))).isoformat(), rng.choice(notes)])


def _write_payments_txt(path, rng, people, today):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("payment_id|customer_number|paid_on|biller|amount|currency\n")
        for i in range(1500):
            p = rng.choice(people)
            paid = today - timedelta(days=rng.randint(1, 30))
            f.write(f"P{i + 1:07d}|{p['customer_number']}|{paid.isoformat()}|{rng.choice(BILLERS)}|"
                    f"{rng.randint(2, 400) * 2}|XTS\n")


def _write_branches_fixed(path):
    lines = []
    for i, (code, name, district) in enumerate(BRANCHES):
        opened = date(1998 + 3 * i, 1 + i, 1).isoformat()
        values = [code, name, district, opened]
        lines.append("".join(v.ljust(w)[:w] for v, w in zip(values, FIXED_WIDTHS)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_events_jsonl(path, rng, people, today):
    rows = []
    for i in range(2000):
        p = rng.choice(people)
        d = today - timedelta(days=rng.randint(1, 14))
        rows.append({"event_id": f"E{i + 1:07d}", "customer_number": p["customer_number"],
                     "at": _ts(d, rng), "channel": rng.choice(CHANNELS), "event": rng.choice(EVENTS)})
    # channels resend: about 3% of events arrive twice, which the job's dedupe step removes
    rows += [dict(r) for r in rng.sample(rows, 60)]
    rng.shuffle(rows)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_rates_xlsx(path, rng, today) -> bool:
    try:
        from openpyxl import Workbook
    except ImportError:
        if path.exists():
            path.unlink()
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "rates"
    ws.append(["Daily reference rates (made up for the demo)"])
    ws.append(["rate_date", "currency", "mid_rate"])
    base = {"USD": 1.0, "EUR": 1.085, "XTS": 0.5}
    for k in range(30, 0, -1):
        d = today - timedelta(days=k)
        for ccy, b in base.items():
            ws.append([d.isoformat(), ccy, round(b * rng.uniform(0.98, 1.02), 4)])
    wb.properties.creator = "etl+ demo"
    wb.properties.created = datetime(today.year, today.month, today.day)
    wb.properties.modified = datetime(today.year, today.month, today.day)
    wb.save(path)
    return True


# ---- project files ------------------------------------------------------------

def _write_bank_samples(landing: Path, today: date) -> dict:
    """Made-up bank files from etlplus.formats.samples (landing/bank/*). Returns which bank jobs
    can run here: none when the formats package is not installed."""
    try:
        from etlplus import formats
        from etlplus.formats import samples
    except Exception:
        return {}
    try:
        samples.write(landing, today)
    except Exception:
        return {}
    # a bank job is written only when the source reader can read its format (sources.ALL_FORMATS)
    try:
        from etlplus import sources
        have = set(getattr(sources, "ALL_FORMATS", ()) or ()) & set(getattr(formats, "FORMATS", {}) or {})
    except Exception:
        have = set()
    bank = landing / "bank"
    return {
        "mt940": "mt940" in have and (bank / "statement_mt940.txt").exists(),
        "camt053": "camt053" in have and (bank / "statement_camt053.xml").exists(),
        "layout": "layout" in have and (bank / "card_daily_ni_style.txt").exists(),
        "mt103": "mt103" in have and (bank / "payments_mt103.txt").exists(),
    }


def _settings():
    return {
        "name": "etl+ demo",
        "kind": "demo",
        "stage": "stage",
        "logs": "logs",
        "keep_runs": 10,
        "timezone": "UTC",
        "notify": {"on": "failure", "webhook": None, "email": None},
    }


def _connections():
    return {
        "core": {"kind": "sqlite", "path": "./core.db", "role": "source",
                 "description": "Made-up core banking system"},
        "landing": {"kind": "folder", "path": "./landing", "role": "source",
                    "description": "Files dropped by other systems"},
        "warehouse": {"kind": "duckdb", "path": "./warehouse.duckdb", "role": "target",
                      "description": "Reporting warehouse"},
        "hub": {"kind": "folder", "path": "./hub", "role": "target",
                "description": "Parquet folder read by an offerkit Data hub (kind: parquet, path: ./hub)"},
        "reporting": {"kind": "duckdb", "path": "./reporting.duckdb", "role": "target",
                      "description": "Reporting database fed through destinations and column mappings"},
    }


def _destinations():
    loaded_at = {"name": "loaded_at", "type": "timestamp", "default": "CURRENT_TIMESTAMP"}
    return {
        "reporting_statement_lines": {
            "connection": "reporting", "table": "main.statement_lines", "design": "create",
            "columns": [
                {"name": "line_id", "type": "string(80)", "nullable": False},
                {"name": "account", "type": "string(34)", "personal": True},
                {"name": "value_date", "type": "date", "nullable": False},
                {"name": "entry_date", "type": "date"},
                {"name": "amount", "type": "decimal(18,3)", "nullable": False},
                {"name": "currency", "type": "string(3)"},
                {"name": "reference", "type": "string(40)"},
                {"name": "info", "type": "text", "personal": True},
                {"name": "source_system", "type": "string(10)", "default": "'MT940'"},
                dict(loaded_at),
            ],
            "keys": ["line_id"],
            "description": "Statement lines from every bank file",
        },
        "reporting_card_transactions": {
            "connection": "reporting", "table": "main.card_transactions", "design": "create",
            "columns": [
                {"name": "arn", "type": "string(23)", "nullable": False},
                {"name": "settlement_date", "type": "date", "nullable": False},
                {"name": "card_number", "type": "string(19)", "personal": True},
                {"name": "transaction_type", "type": "string(2)"},
                {"name": "merchant_name", "type": "string(40)"},
                {"name": "mcc", "type": "string(4)"},
                {"name": "amount", "type": "decimal(18,2)"},
                {"name": "currency", "type": "string(3)"},
                {"name": "response_code", "type": "string(2)"},
                dict(loaded_at),
            ],
            "keys": ["arn"],
            "description": "Card transactions from the processor's daily file",
        },
        "reporting_customers": {
            "connection": "reporting", "table": "main.customers", "design": "create",
            "columns": [
                {"name": "customer_id", "type": "string(12)", "nullable": False},
                {"name": "segment", "type": "string(20)"},
                {"name": "branch", "type": "string(10)"},
                {"name": "customer_type", "type": "string(20)"},
                {"name": "birth_date", "type": "date"},
                {"name": "opened_on", "type": "date"},
                {"name": "source_system", "type": "string(10)"},
                dict(loaded_at),
            ],
            "keys": ["customer_id"],
            "description": "Customers from the core, with a type and a birth date",
        },
        "reporting_payments": {
            "connection": "reporting", "table": "main.payments", "design": "create",
            "columns": [
                {"name": "transaction_reference", "type": "string(16)", "nullable": False},
                {"name": "value_date", "type": "date"},
                {"name": "currency", "type": "string(3)"},
                {"name": "amount", "type": "decimal(18,3)"},
                {"name": "ordering_customer", "type": "text", "personal": True},
                {"name": "beneficiary_account", "type": "string(34)", "personal": True},
                {"name": "beneficiary", "type": "text", "personal": True},
                {"name": "sender_bic", "type": "string(11)"},
                {"name": "receiver_bic", "type": "string(11)"},
                {"name": "uetr", "type": "string(36)"},
                {"name": "source_system", "type": "string(10)"},
                dict(loaded_at),
            ],
            "keys": ["transaction_reference"],
            "description": "Customer payments (MT103)",
        },
    }


def _jobs(has_excel: bool, bank: dict | None = None) -> list[dict]:
    bank = bank or {}
    jobs = [
        {
            "name": "core_customers",
            "description": "Every customer from the core, replaced each night",
            "source": {"connection": "core", "table": "customers",
                       "columns": ["customer_number", "segment", "branch_code", "birth_year", "opened_on", "updated_at"]},
            "transform": [{"cast": {"birth_year": "INTEGER", "opened_on": "DATE", "updated_at": "TIMESTAMP"}}],
            "target": {"connection": "warehouse", "table": "customers", "mode": "replace", "create": True},
            "schedule": "0 2 * * *",
            "notify": "failure",
        },
        {
            "name": "core_transactions",
            "description": "Last 7 days of transactions; the slice is deleted on the target first, so re-runs never duplicate",
            "source": {"connection": "core", "table": "transactions", "columns": "*",
                       "incremental": {"column": "booking_date", "lookback_days": 7},
                       "chunk_rows": 50000},
            "target": {"connection": "warehouse", "table": "transactions", "mode": "append",
                       "slice_column": "booking_date", "create": True},
            "schedule": "30 2 * * *",
            "notify": "failure",
        },
        {
            "name": "core_accounts",
            "description": "Accounts changed since the last run, merged on the account number",
            "source": {"connection": "core", "table": "accounts", "columns": "*",
                       "incremental": {"column": "updated_at", "watermark": True}},
            "target": {"connection": "warehouse", "table": "accounts", "mode": "upsert",
                       "key": ["account_number"], "create": True},
            "schedule": "0 3 * * *",
            "notify": "failure",
        },
        {
            "name": "file_customers",
            "description": "KYC review file, comma separated with a header",
            "source": {"connection": "landing", "files": "customers.csv", "format": "csv",
                       "options": {"delimiter": ",", "header": True, "encoding": "utf-8"}},
            "transform": [{"cast": {"reviewed_on": "DATE"}}],
            "target": {"connection": "warehouse", "table": "kyc_reviews", "mode": "replace", "create": True},
            "notify": "failure",
        },
        {
            "name": "file_payments",
            "description": "Bill payments from the billers' switch, pipe delimited",
            "source": {"connection": "landing", "files": "payments.txt", "format": "txt",
                       "options": {"delimiter": "|", "header": True}},
            "transform": [{"cast": {"paid_on": "DATE", "amount": "DECIMAL(18,2)"}}],
            "target": {"connection": "warehouse", "table": "bill_payments", "mode": "upsert",
                       "key": ["payment_id"], "create": True},
            "notify": "failure",
        },
        {
            "name": "file_branches",
            "description": "Branch list, fixed width with no header",
            "source": {"connection": "landing", "files": "branches.txt", "format": "fixed",
                       "options": {"widths": list(FIXED_WIDTHS), "names": list(FIXED_NAMES)}},
            "transform": [{"cast": {"opened_on": "DATE"}}],
            "target": {"connection": "warehouse", "table": "branches", "mode": "replace", "create": True},
            "notify": "failure",
        },
        {
            "name": "file_events",
            "description": "App events, one JSON object per line; resent events are removed",
            "source": {"connection": "landing", "files": "events.jsonl", "format": "jsonl"},
            "transform": [
                {"cast": {"at": "TIMESTAMP"}},
                {"dedupe": {"key": ["event_id"], "order_by": "at DESC"}},
            ],
            "target": {"connection": "warehouse", "table": "app_events", "mode": "replace", "create": True},
            "notify": "failure",
        },
        {
            "name": "customers_to_reporting",
            "description": "Customers into the reporting database: the columns are matched by name, one is renamed and one is a constant",
            "source": {"connection": "core", "table": "customers", "columns": "*"},
            "transform": [
                {"rename": {"branch_code": "branch"}},
                {"derive": {"customer_type": "'individual'", "birth_date": "make_date(CAST(birth_year AS INTEGER), 1, 1)"}},
                {"drop": ["updated_at", "birth_year"]},
            ],
            "target": {"destination": "reporting_customers", "mode": "replace"},
            "mapping": {
                "columns": {
                    "customer_id": {"from": "customer_number"},
                    "segment": {"from": "segment"},
                    "branch": {"from": "branch"},
                    "customer_type": {"from": "customer_type"},
                    "birth_date": {"from": "birth_date"},
                    "opened_on": {"from": "opened_on"},
                    "source_system": {"const": "CORE"},
                },
                "unmapped": "default",
                "on_cast_error": "error",
            },
            "notify": "failure",
        },
        {
            "name": "hub_daily_txn",
            "description": "Transactions aggregated to customer x day x code x direction for the offerkit Data hub",
            "source": {"connection": "core",
                       "query": "SELECT customer_number, booking_date, txn_code, dir, amount FROM transactions"},
            "transform": [
                {"cast": {"booking_date": "DATE", "amount": "DOUBLE"}},
                {"filter": "txn_code <> 'REV'"},
                {"sql": "SELECT customer_number, booking_date, txn_code, dir, count(*) AS n_txn, "
                        "round(sum(abs(amount)), 2) AS amount, max(abs(amount)) AS max_abs "
                        "FROM input GROUP BY customer_number, booking_date, txn_code, dir"},
                {"derive": {"loaded_on": "current_date"}},
            ],
            "target": {"connection": "hub", "table": "daily_txn", "format": "parquet", "mode": "replace"},
            "schedule": "0 4 * * *",
            "notify": "always",
        },
    ]
    jobs += _bank_jobs(bank)
    if has_excel:
        jobs.append({
            "name": "file_rates",
            "description": "Reference rates from a spreadsheet; the first row is a title",
            "source": {"connection": "landing", "files": "rates.xlsx", "format": "excel",
                       "options": {"sheet": "rates", "header": True, "skip_rows": 1}},
            "transform": [{"cast": {"rate_date": "DATE", "mid_rate": "DOUBLE"}}],
            "target": {"connection": "warehouse", "table": "fx_rates", "mode": "upsert",
                       "key": ["rate_date", "currency"], "create": True},
            "notify": "failure",
        })
    return jobs


def _bank_jobs(bank: dict) -> list[dict]:
    """Bank-file jobs, each with a mapping into a reporting destination. Two of them feed one
    destination; their line_id carries the source so the two feeds never collide on the key."""
    jobs = []
    if bank.get("mt940"):
        jobs.append({
            "name": "bank_mt940_lines",
            "description": "SWIFT MT940 statement lines into the reporting database",
            "source": {"connection": "landing", "files": "bank/statement_mt940.txt", "format": "mt940",
                       "options": {"checks": "error"}},
            "target": {"destination": "reporting_statement_lines", "mode": "upsert"},
            "mapping": {
                "columns": {
                    "line_id": {"expr": "'MT940-' || transaction_reference || '-' || CAST(record_line AS VARCHAR)"},
                    "account": {"from": "account"},
                    "value_date": {"from": "value_date"},
                    "entry_date": {"from": "entry_date"},
                    "amount": {"from": "signed_amount"},
                    "currency": {"from": "currency"},
                    "reference": {"from": "customer_reference"},
                    "info": {"from": "info"},
                    "source_system": {"const": "MT940"},
                },
                "unmapped": "default",
                "on_cast_error": "error",
            },
            "notify": "failure",
        })
    if bank.get("camt053"):
        jobs.append({
            "name": "bank_camt053_entries",
            "description": "ISO 20022 camt.053 entries into the same statement lines",
            "source": {"connection": "landing", "files": "bank/statement_camt053.xml", "format": "camt053",
                       "options": {"checks": "error"}},
            "target": {"destination": "reporting_statement_lines", "mode": "upsert"},
            "mapping": {
                "columns": {
                    "line_id": {"expr": "'CAMT053-' || file_name || '-' || CAST(record_line AS VARCHAR)"},
                    "account": {"from": "account"},
                    "value_date": {"from": "value_date"},
                    "entry_date": {"from": "booking_date"},
                    "amount": {"from": "signed_amount"},
                    "currency": {"from": "currency"},
                    "reference": {"from": "end_to_end_id"},
                    "info": {"from": "remittance_info"},
                    "source_system": {"const": "CAMT053"},
                },
                "unmapped": "default",
                "on_cast_error": "error",
            },
            "notify": "failure",
        })
    if bank.get("layout"):
        jobs.append({
            "name": "card_daily_ni_style",
            "description": "The card processor's daily file (a made-up NI-style layout), trailer count and sum checked",
            "source": {"connection": "landing", "files": "bank/card_daily_ni_style.txt", "format": "layout",
                       "options": {"layout": "card_processor_ni_style", "checks": "error"}},
            "target": {"destination": "reporting_card_transactions", "mode": "append",
                       "slice_column": "settlement_date"},
            "mapping": {
                "columns": {
                    "arn": {"from": "arn"},
                    "settlement_date": {"from": "settlement_date"},
                    "card_number": {"from": "card_number"},
                    "transaction_type": {"from": "transaction_type"},
                    "merchant_name": {"from": "merchant_name"},
                    "mcc": {"from": "mcc"},
                    "amount": {"from": "amount"},
                    "currency": {"from": "currency"},
                    "response_code": {"from": "response_code"},
                },
                "unmapped": "default",
                "on_cast_error": "error",
            },
            "notify": "failure",
        })
    if bank.get("mt103"):
        jobs.append({
            "name": "payments_mt103",
            "description": "SWIFT MT103 customer payments into the reporting database",
            "source": {"connection": "landing", "files": "bank/payments_mt103.txt", "format": "mt103",
                       "options": {"checks": "error"}},
            "target": {"destination": "reporting_payments", "mode": "upsert"},
            "mapping": {
                "columns": {
                    "transaction_reference": {"from": "transaction_reference"},
                    "value_date": {"from": "value_date"},
                    "currency": {"from": "currency"},
                    "amount": {"from": "amount"},
                    "ordering_customer": {"from": "ordering_customer"},
                    "beneficiary_account": {"from": "beneficiary_account"},
                    "beneficiary": {"from": "beneficiary"},
                    "sender_bic": {"from": "sender_bic"},
                    "receiver_bic": {"from": "receiver_bic"},
                    "uetr": {"from": "uetr"},
                    "source_system": {"const": "MT103"},
                },
                "unmapped": "default",
                "on_cast_error": "error",
            },
            "notify": "failure",
        })
    return jobs


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")


def _readme_text(today: date) -> str:
    return (
        "etl+ demo project\n"
        f"Generated for {today.isoformat()}. Everything here is made up: people are ids, accounts start DEMO,\n"
        "BICs use country ZZ, currencies are USD, EUR and XTS (the ISO test code).\n\n"
        "core.db             SQLite source: customers, products, accounts, transactions (120 days)\n"
        "landing/            files: customers.csv, payments.txt (pipe), branches.txt (fixed width),\n"
        "                    events.jsonl, rates.xlsx\n"
        "warehouse.duckdb    target, created by the first run\n"
        "reporting.duckdb    target fed through destinations (destinations.yaml) and column mappings\n"
        "landing/bank/       made-up bank files: MT940, MT103, camt.053, a card processor file, a payroll batch\n"
        "hub/                Parquet target for an offerkit Data hub\n"
        "jobs/               one YAML per job\n\n"
        "Run everything:  etlplus run --all --project .\n"
        "Console:         etlplus serve --project .   then http://127.0.0.1:8300/app/\n"
    )
