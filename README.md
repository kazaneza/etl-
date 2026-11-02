# etl+

**Extract, stage, transform, load, done.** etl+ moves data from any source to any
destination, and every step leaves a file behind, so any step can be re-run on its own.

```
extract  ->  stage  ->  transform  ->  map  ->  load  ->  done
 source      Parquet     DuckDB SQL     columns   destination
 (read only) + manifest  on the file    to table  (idempotent)
```

By Crested Crane Intelligence. Pure Python, runs on Windows, Linux, macOS and Docker, with a
console in the same colours and building blocks as offerkit.

## Why stage files

The original etl+ was a shell pipeline: Postgres to a CSV landing file, then BULK INSERT into
SQL Server. It worked because of a few habits, and etl+ keeps all of them:

- **The extract and the load are separate.** A slow or failed load never re-reads the source,
  and a source that kills long reads (a hot standby) is touched once, briefly.
- **A file is published only when complete.** It is written as `*.partial` and renamed after
  its manifest is written. A load never reads a `.partial`, so a killed extract cannot load half
  a table.
- **Every staged file has a manifest**: rows, columns and types, the window, the source,
  bytes and a SHA-256. You can see exactly what was loaded, and load it again.
- **Re-runs are safe.** Each load mode is idempotent (see below), so running a job twice
  gives the same target as running it once.

The staged format is Parquet: typed, compressed, and readable by DuckDB, pandas, Spark and
offerkit without a schema file.

## Install

Nothing is downloaded at run time: the console, its fonts and icons ship in the package, and the
demo data is generated on your machine.

**Docker.** From this folder, no `.env` needed:

```
docker compose up -d
```

Open http://localhost:8300. The home is the `etlplus-project` volume.

**pip.** Python 3.11 or newer:

```
pip install "etlplus[common]"
etlplus start
```

It serves on http://127.0.0.1:8300 and opens the browser. The home is `ETLPLUS_HOME` if set,
else `%LOCALAPPDATA%\etlplus` (Windows), `~/Library/Application Support/etlplus` (macOS) or
`~/.local/share/etlplus` (Linux). `--port`, `--home` and `--no-browser` change that.

**Offline bundle**, for a server without internet. On a machine with Docker and internet:

```
sh scripts/offline-bundle.sh            # Windows: powershell -File scripts\offline-bundle.ps1
```

Copy `dist/etlplus-<version>-offline` to the server and follow its `LOAD_AND_RUN.md`
(`docker load`, then `docker compose up -d`).

**First start.** etl+ asks one question: **Start with demo data** or **Connect my own data**
(in the console, or on the terminal for `etlplus start`; `--demo` / `--real` answer it). It also
creates the secrets key in the project folder (keep a copy) and names the pip extra for any
connection whose driver is missing, for example `pip install etlplus[postgres]`.

### More on Docker

The image ships the common drivers and the Microsoft ODBC Driver 18; Db2, Snowflake, SAP HANA,
Teradata and Firebird are added with `pip install etlplus[db2]` (and so on) in a derived image.
`ETLPLUS_START=demo` or `empty` makes the first choice without asking. The volume is kept until
`docker compose down -v`; to use a folder instead, replace the volume line with
`./project:/project`. The image runs as a non-root user and compose publishes the port on the
host's 127.0.0.1 only.

### From a source checkout

`pip install -e ".[all,dev]"` and `pytest`. `etlplus quickstart` writes a demo project in
`./demo`, runs every job once and prints the console address.

## Demo data or your own data

The first start asks one question: **Start with demo data** or **Connect my own data**. In
the console it is a full-page choice; on a terminal `etlplus start` asks it, and `etlplus
start --demo` / `--real` answer it. Both projects live side by side in one home folder:

    <home>/projects.yaml   which one is active
    <home>/demo/           made-up data and ready jobs
    <home>/real/           yours, empty until you add a connection, a destination and a job

`etlplus use demo|real` switches, `etlplus projects` lists both. Switching keeps both: each has
its own stage, run history and secrets. Docker reads `ETLPLUS_START=ask|demo|empty`
(`etlplus serve --start ...`). A 0.1.0 volume with a project at its root is served as it is.

## The project folder

Everything etl+ knows is in one folder you can read, edit and keep in git (minus secrets and
data). The console edits the same files.

```
<project>/
  etlplus.yaml        name, stage and logs folders, keep_runs, timezone, notifications
  connections.yaml    connections, never a password
  jobs/<name>.yaml    one file per job
  secrets.json.enc    passwords, encrypted, managed by etl+ only
  .secret.key         the key for secrets.json.enc unless ETLPLUS_SECRET_KEY is set
  stage/<job>/<run_id>/   extract.parquet, transformed.parquet, manifest.json
  runs.sqlite         run history, alerts, watermarks
  logs/<job>/<run_id>.log
```

The project is `--project <folder>`, else `$ETLPLUS_HOME` or `$ETLPLUS_PROJECT`, else `./project` when it exists, else the per-user home above.
`etlplus init <folder>` writes an empty one.

## A job, line by line

```yaml
name: login_events
description: Login events, last day appended with the slice replaced
source:
  connection: core                 # an id from connections.yaml
  table: public.login_events       # or query: "SELECT ..."  or files: "in/*.csv"
  columns: "*"                     # or a list
  incremental:
    column: created_date           # indexed on the source
    lookback_days: 1               # window [today - 1, today), half-open
  chunk_rows: 200000               # rows per batch while extracting
transform:                         # optional, run in order with DuckDB on the staged file
  - cast: {created_date: TIMESTAMP}
  - filter: "user_id IS NOT NULL"
target:
  connection: reporting
  table: dbo.login_events
  mode: append                     # replace | append | upsert
  slice_column: created_date       # the window column on the target
  create: true                     # create the table from the staged schema if missing
schedule: "15 2 * * *"             # cron, in the project timezone
notify: failure                    # never | failure | always
```

What happens on a run:

1. **extract**: the source is read in chunks (`SELECT created_date ... WHERE created_date >= '2026-10-31' AND created_date < '2026-11-01'`)
   and written to `stage/login_events/<run_id>/extract.parquet.partial`, then the manifest,
   then the rename.
2. **transform**: the steps become one DuckDB query over the view `input`, written to
   `transformed.parquet` the same way. No steps, no file: the load reads the extract.
3. **load**: the slice `[2026-10-31, 2026-11-01)` is deleted on `dbo.login_events` and the
   staged rows inserted, in one transaction.
4. **done**: the run record (rows per step, durations, window, error) goes to `runs.sqlite`,
   old staged runs beyond `keep_runs` are removed.

Run one step alone: `etlplus run login_events --step load` loads the latest staged file again
without touching the source. The Stage page in the console does the same.

### Transform steps

| Step | Example | What it does |
| --- | --- | --- |
| `rename` | `{name_hash: name_key}` | rename columns |
| `cast` | `{amount: "DECIMAL(18,2)", day: DATE}` | change types (DuckDB types) |
| `filter` | `"segment IS NOT NULL"` | keep rows where the expression is true |
| `derive` | `{load_date: "current_date"}` | add or replace a column from an expression |
| `drop` | `[segment_old]` | remove columns |
| `dedupe` | `{key: [id], order_by: "updated_at DESC"}` | keep the first row per key |
| `sql` | `"SELECT ... FROM input GROUP BY ..."` | any single SELECT over `input` (guarded) |

The console's job editor shows the result of the steps on the latest staged file as you
type them.

## Sources and destinations: every kind

One driver registry (`etlplus/drivers`) knows every kind: PostgreSQL, Amazon Redshift,
Greenplum, CockroachDB, TimescaleDB, YugabyteDB, SQL Server, Azure SQL Database, MySQL, MariaDB,
Oracle, IBM Db2, SQLite, DuckDB, ClickHouse, Snowflake, SAP HANA, Teradata, Firebird, any ODBC
database (Informix, Sybase ASE, Temenos T24 through the database it runs on or its ODBC driver)
and MongoDB (source only). Each card in the gallery says how it is tested: **Tested** (against a
real server or in-process), **Simulated** (the read and load path through a fake driver) or
**Untested** (the SQL is generated and unit-tested only). `GET /api/kinds` lists them with their
fields and whether the driver is installed.

Install only what you use: `pip install etlplus[postgres]`, `[sqlserver]`, `[mysql]`, `[oracle]`,
`[db2]`, `[clickhouse]`, `[snowflake]`, `[hana]`, `[teradata]`, `[firebird]`, `[odbc]`, `[mongodb]`,
`[sftp]`, `[s3]`, `[azure]`, `[excel]`; `[common]` is what the Docker image ships, `[all]` is
everything.

A source is always read-only. Every query passes the SQL guard (one SELECT or WITH, nothing
else), and the session is made read-only where the server can do it: Postgres read-only
sessions, `SET SESSION TRANSACTION READ ONLY` on MySQL and MariaDB, `SET TRANSACTION READ ONLY` on
Oracle and HANA, `ApplicationIntent=ReadOnly` on SQL Server, `readonly=1` on ClickHouse, ODBC
read-only mode, SQLite `mode=ro`, DuckDB `read_only` with external access off. Where the server
cannot (Db2, Snowflake), the card says to use a read-only user or role. A MongoDB query is a
filter and projection, or a pipeline of read stages only.

| Kind | Name | Status | Source | Target | Install |
| --- | --- | --- | --- | --- | --- |
| `postgres` | PostgreSQL | Tested | yes | yes | pip install etlplus[postgres] |
| `redshift` | Amazon Redshift | Untested | yes | yes | pip install etlplus[postgres] |
| `greenplum` | Greenplum | Untested | yes | yes | pip install etlplus[postgres] |
| `cockroachdb` | CockroachDB | Untested | yes | yes | pip install etlplus[postgres] |
| `timescaledb` | TimescaleDB | Untested | yes | yes | pip install etlplus[postgres] |
| `yugabytedb` | YugabyteDB | Untested | yes | yes | pip install etlplus[postgres] |
| `sqlserver` | SQL Server | Simulated | yes | yes | pip install etlplus[sqlserver] |
| `azuresql` | Azure SQL Database | Simulated | yes | yes | pip install etlplus[sqlserver] |
| `mysql` | MySQL | Simulated | yes | yes | pip install etlplus[mysql] |
| `mariadb` | MariaDB | Tested | yes | yes | pip install etlplus[mysql] |
| `oracle` | Oracle | Simulated | yes | yes | pip install etlplus[oracle] |
| `db2` | IBM Db2 | Untested | yes | yes | pip install etlplus[db2] |
| `sqlite` | SQLite | Tested | yes | yes |  |
| `duckdb` | DuckDB | Tested | yes | yes |  |
| `clickhouse` | ClickHouse | Simulated | yes | yes | pip install etlplus[clickhouse] |
| `snowflake` | Snowflake | Untested | yes | yes | pip install etlplus[snowflake] |
| `hana` | SAP HANA | Untested | yes | yes | pip install etlplus[hana] |
| `teradata` | Teradata | Untested | yes | yes | pip install etlplus[teradata] |
| `firebird` | Firebird | Untested | yes | yes | pip install etlplus[firebird] |
| `odbc` | Other (ODBC) | Untested | yes | yes | pip install etlplus[odbc] |
| `mongodb` | MongoDB | Simulated | yes |  | pip install etlplus[mongodb] |
| `folder` | Folder on this machine | Tested | yes | yes |  |
| `share` | Shared folder | Tested | yes | yes |  |
| `sftp` | SFTP | Simulated | yes | yes | pip install etlplus[sftp] |
| `ftp` | FTP / FTPS | Simulated | yes | yes |  |
| `s3` | S3 or S3-compatible | Simulated | yes | yes | pip install etlplus[s3] |
| `azure_blob` | Azure Blob Storage | Simulated | yes | yes | pip install etlplus[azure] |
| `http` | HTTP(S) download | Tested | yes |  |  |

PostgreSQL and MariaDB are **Tested** against real servers (`pytest -m realdb` with the
compose `testdb` profile: read-only session, replace, append with a window, upsert, a mapping
job and the type round trip). SQLite, DuckDB, folders, shared folders and HTTP are tested
in-process. **Simulated** kinds run the full read and load path through a fake driver that
records the SQL; ClickHouse, MongoDB, SFTP, FTP, S3 and Azure are covered by stubs only.
**Untested** kinds generate SQL that is unit-tested but has never run against a server:
check them on a test system first.

File options: `delimiter`, `header`, `encoding`, `skip_rows`, `sheet` (Excel), and for fixed
width `widths` and `names`. `files` is a pattern relative to the folder or remote connection
(`in/payments_*.txt`); every matching file is read into the same stage.

## Remote locations

Files can come from a **Shared folder** (an absolute or UNC path), **SFTP**, **FTP / FTPS**,
**S3** or S3-compatible storage, **Azure Blob Storage** or an **HTTP(S)** download (the URL may
hold `{date}` or `{date:%Y%m%d}`). Every remote file is fetched into the run's stage folder first
(`incoming/`, written as `.partial`, size checked, SHA-256 recorded, renamed) and read there.

    source:
      connection: sftp_in
      files: "statements/MT940_*.txt"
      format: mt940
      pickup: {skip_seen: true, min_age_seconds: 60, max_files: 500, after: keep}

`skip_seen` skips files already loaded (same path, size and time). `min_age_seconds` leaves
files that may still be being written. `after` (keep, archive with `archive_to`, mark_done,
delete) runs only after a successful load, and only when the connection says
`options: {allow_after_actions: true}`. Nothing is moved or deleted otherwise.

Safety: passwords, keys and tokens are kept in the encrypted secrets store only and are never
shown again; changing the host, bucket, URL or path of a connection drops its stored secrets. SFTP
checks the server's host key fingerprint (`SHA256:...`, shown by Test) before signing in and never
accepts an unknown key. FTP uses explicit TLS unless `options.allow_plain` is set. S3 and Azure
need keys unless `options.use_default_credentials` is set. HTTP refuses local and cloud-metadata
addresses unless `options.allow_private` is set, follows at most 5 redirects and never from https
to http, and stops at `options.max_mb` (2048 by default). A shared folder can never be the etl+
home, and `ETLPLUS_SHARE_ROOTS` limits which folders it may be.

## Bank files

A folder (or remote) connection can also read bank files. Set `format` to one of these:

| `format` | File | One row per |
| --- | --- | --- |
| `mt940`, `mt950` | SWIFT statement | `:61:` statement line |
| `mt103`, `mt202` | SWIFT payment, bank transfer | message |
| `camt053`, `camt054` | ISO 20022 statement, notification | entry (`Ntry`), or `TxDtls` with `level: transaction` |
| `pain001` | ISO 20022 payment order | `CdtTrfTxInf` |
| `pacs008` | ISO 20022 credit transfer | `CdtTrfTxInf` |
| `bai2` | BAI2 cash report | `16` detail record |
| `layout` | fixed width with several record types | the layout's output record |

```yaml
source:
  connection: inbox
  files: "statements/*.sta"
  format: mt940
  options: {checks: error}     # or warn
```

- **Checks run while the file streams.** Statements must balance (opening + lines =
  closing), trailer counts and sums must match, ISO `NbOfTxs`/`CtrlSum`/`TxsSummry` must add
  up. Under `checks: error` (the default) a failed check stops the run and nothing is
  published. Under `checks: warn` the run goes on and the failures go to the log and the
  manifest.
- **Every row carries** `file_name` and `record_line`, so any value can be traced back to
  its line. Amounts are `DECIMAL(18,3)`; a SWIFT `1234,56` is 1234.56.
- **Personal columns are flagged** (accounts, names, remittance text) and the console marks
  them. Card numbers in a layout field with `mask: pan` are masked when the file is read
  (first 6 and last 4 digits kept); only `mask: none` keeps a full number.
- **XML is read safely.** A file with a DOCTYPE or an entity declaration is refused by the
  parser itself, namespaces are ignored (every message version is accepted), and finished
  entries are dropped as they are read, so a large statement parses in flat memory.
- **Detection.** `etlplus detect FILE` names the format from the first 64 KB: SWIFT block
  2, the XML namespace or root element, BAI2 `01`/`99`, or a layout's record codes and line
  length.

### Layouts (fixed width, several record types)

A layout is YAML: where the record type code sits, then each record (header, detail,
trailer, ...) with its fields by position, which record gives the rows, which header fields
are copied onto every row, and the checks.

```yaml
record_type: {start: 1, length: 2}
records:
  - {type: header, match: "FH", occurs: first, fields: [{name: file_date, start: 20, length: 8, type: date, format: "%Y%m%d"}]}
  - type: detail
    match: "TD"
    output: true
    fields:
      - {name: card_number, start: 3, length: 19, type: string, mask: pan, personal: true}
      - {name: amount, start: 90, length: 13, type: decimal, scale: 2, implied_decimal: true, sign: trailing}
  - {type: trailer, match: "FT", occurs: last, fields: [{name: record_count, start: 3, length: 9, type: integer}]}
carry: [header.file_date]
checks:
  - {kind: count, field: trailer.record_count, of: detail}
```

Three templates ship with etl+: `card_processor_ni_style`, `payroll_batch_example` and
`statement_fixed_example`. Network International and other processors publish their own
layouts; this template shows the shape only. Check every position against your
processor's specification. Copy a template into your project (Layouts in the console, or
`layouts/<name>.yaml` next to `jobs/`) and edit the copy; a project layout of the same name
wins over the template.

Every bank format here is built from the public standard and tested on made-up files only.
Check each one on a real file from your bank before you rely on it.

## Destinations

A destination is where rows land, defined once in `destinations.yaml` and used by many jobs
(`target: {destination: reporting_customers, mode: upsert}`). A database destination names a
connection and a table; with `design: create` etl+ creates the table from your column design
(generic types such as `string(40)`, `decimal(18,3)`, `timestamp`, mapped to each database's
own types), with `design: existing` it reads the columns from the database. A file destination
names a folder or remote location, a sub-folder, a file name (`{date}`, `{run_id}`, `{job}`,
`{format}`) and parquet, csv or jsonl. `etlplus destinations list|ddl ID|create ID|test ID`.

## Column mapping

`mapping.columns` fills each destination column from a source column (`from`), a constant
(`const`) or a DuckDB expression (`expr`, no subqueries, tables or file access). Unmapped
columns take the database default; `unmapped: error` makes them an error; `on_cast_error: null`
turns values that do not convert into NULL. The map step writes `mapped.parquet` (a file left
behind like every other step) and the load inserts only the mapped columns.

    source -> stage -> transform -> map -> destination

Everything in the demo is made up: people are ids, accounts start DEMO, BICs use country ZZ,
currencies are USD, EUR and XTS.

## Modes and idempotency

| Mode | How | Re-run |
| --- | --- | --- |
| `replace` | load into a temporary table, then swap it in (or truncate and insert in one transaction where a swap is not possible) | same table, readers never see it half empty |
| `append` | delete the window slice on the target, insert the staged rows, one transaction | the slice is replaced, never duplicated |
| `upsert` | merge on `key` columns: update matches, insert the rest | same rows |

A failed load rolls back and leaves the target as it was; the staged file stays, so fix the
cause and run `--step load`.

## Incremental windows

- `lookback_days: N` reads `[today - N days, today)`, half-open on the raw column so the source
  index is used (never `DATE(column) = ...`). With `append`, the same slice is deleted on the
  target first, which is also what picks up late-arriving rows. Backfill any range with
  `etlplus run <job> --from 2026-08-01 --to 2026-08-05` or the Run button's dates.
- `watermark: true` reads rows with `column >` the highest value loaded so far (kept in
  `runs.sqlite`), for tables with a reliable `updated_at`. Use it with `upsert`.

## Secrets

Passwords are never in `connections.yaml`, never on a command line, never in a log and never
returned by the API (it answers `"password_set": true`). Two ways to give one:

- the console's connection form (write-only field), stored in `secrets.json.enc`, encrypted
  with `$ETLPLUS_SECRET_KEY` or a `.secret.key` file created in the project with owner-only
  permissions;
- `password_env: CORE_PASSWORD` on the connection, read from the environment at run time.

Keep `.secret.key` and `secrets.json.enc` out of git (the generated `.gitignore` does).

## Scheduling

A job with `schedule:` (standard five-field cron, in the project timezone) runs while
`etlplus serve` is up. One run per job at a time; a run still going when the next is due is
skipped and noted. `notify:` sends a webhook and/or an e-mail on failure or always, set once in
Settings. Without the server, use the system's scheduler: `etlplus run <job>` exits non-zero on
failure.

## The console

Open http://127.0.0.1:8300. On the first start it asks one thing: **Start with demo data** or
**Connect my own data**. Both are kept; switch in Settings, Project.

- **Sources**: your saved connections, and **Add source**, a gallery of databases, files, bank
  files and remote locations. Each card says Tested, Simulated or Untested and whether its
  driver is installed (with the `pip install` line to copy). Passwords and keys are write-only:
  leave the field empty to keep the saved one.
- **Destinations**: tables or folders to load into. Design the columns (or start from a source),
  see the SQL, create the table, preview the rows.
- **Jobs**: a five-step builder (Source, Transform, Destination, Mapping, Schedule). Mapping
  matches columns by name, suggests close names, takes constants and expressions, and shows a
  live preview with the problems above it. A job opens on its flow diagram: each step lights
  up as it runs, with row counts on the arrows; click a step for its detail.
- **Layouts**: fixed-width files with several record types, with a ruler that shows where each
  field sits.

Light and dark follow the system; the moon button switches. Motion stops when the system asks
for reduced motion.

## Feeding an offerkit Data hub

A `folder` target with `format: parquet` writes `<path>/<table>.parquet`, published only when
complete. Point an offerkit `hub.yaml` source at that folder (`kind: parquet`) and map the
tables. `examples/offerkit_hub/` has connections, jobs that aggregate transactions to customer
x day x code x direction before anything leaves the source, and the matching `hub.yaml`
section. The demo's `hub/` folder is the same thing on made-up data.

## Examples

- `examples/postgres_to_sqlserver/`: the original shape, a nightly `replace` and an `append`
  with a lookback, optional BULK INSERT through a shared folder
- `examples/fixed_width_bank_file/`: a partner's fixed-width file, typed and upserted
- `examples/offerkit_hub/`: the Data hub feed
- `examples/bank_files/`: MT940, camt.053, a card processor file with a layout, and BAI2,
  all made up

## Security notes

- **Sources are read-only.** Every database read goes through one guard: a single `SELECT` or
  `WITH ... SELECT`, comments stripped, no `;` chaining, no DML or DDL keywords. Give etl+ a
  read-only database user as well; the guard is a second line, not the first. File sources are
  opened read-only.
- **No login unless `ETLPLUS_TOKEN` is set.** With it, every `/api` call needs
  `Authorization: Bearer <token>`; the console asks once per browser session. Without it,
  anyone who can reach the port can run jobs.
- **Host and origin checks.** Without a token the API answers only to `localhost` and
  `127.0.0.1` (add names with `ETLPLUS_ALLOWED_HOSTS=a.example,b.example`, or `*`), so a web
  page cannot reach it by DNS rebinding. A `POST`, `PUT` or `DELETE` from another site, or one
  sent as a form or plain text, is refused: send JSON.
- **User SQL runs in a sandbox.** Transform steps and DuckDB source queries cannot read or write
  any file but the staged one, nor reach a URL. File-reading functions (`read_*`, `glob`, ...)
  are refused by the guard as well.
- **A stored password stays with its server.** Testing or saving a connection with another
  host, port, database or user needs the password again; `password_env` set from the console
  must start with `ETLPLUS_CONN_` (any name works when written in `connections.yaml` by hand).
- **Bind to localhost.** The server listens on 127.0.0.1 by default and compose publishes on
  127.0.0.1 only. Reach a remote server with `ssh -L 8300:127.0.0.1:8300 <host>`, or put a
  proxy with TLS and a login in front.
- Staged files hold real data: keep `stage/` on storage with the same protection as the
  source, and `keep_runs` low.

## License

Not chosen yet.
