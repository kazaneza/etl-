# etl+ examples

Each folder is a complete project folder: copy it, edit the hosts, set the passwords, run.

| Folder | What it shows |
| --- | --- |
| `postgres_to_sqlserver/` | The original etl+ shape: a Postgres standby to SQL Server, one table replaced every night and one appended with a lookback window, optional BULK INSERT through a shared folder |
| `fixed_width_bank_file/` | A fixed-width file from a bank partner, trimmed, typed and upserted into DuckDB |
| `offerkit_hub/` | Feeding an offerkit Data hub: core tables written as Parquet into the folder a `hub.yaml` reads |
| `bank_files/` | Bank files: an MT940 and a camt.053 statement, a card processor's daily file read with a layout, and a BAI2 cash report, all made up |

Passwords are never in these files. Set them once with the console (Connections, write-only
password field) or with `password_env:` and an environment variable.
