# Setup

How to get LogLens running locally, from cloning the repository to running the
bronze → silver pipeline. The gold layer (segmentation, embeddings, retrieval)
will be added to this guide as it comes online.

> For *what* the system is and *why* it is built this way, see the
> [README](../README.md), [architecture](architecture.md), and
> [ADRs](adr/ADRs.md).

---

## Prerequisites

- **Docker** (Docker Desktop on Windows/macOS, Docker Engine on Linux) — runs
  the PostgreSQL + pgvector database. Verify with `docker run hello-world`.
- **Python 3.11+** — runs the application and the log generator. Verify with
  `python --version`.

The database extension (pgvector) is enabled automatically; there is no manual
database installation.

---

## 1. Clone the repository

```bash
git clone <your-repo-url>
cd ai-log-intelligence-platform
```

## 2. Start the database

```bash
docker compose up -d
```

The first run downloads the `pgvector/pgvector` image (one-time, ~150 MB) and
starts a Postgres container named `loglens-db`. An init script enables the
`vector` extension automatically the first time the database is created.

Check it is running and healthy:

```bash
docker compose ps
```

You should see `loglens-db` with status `Up ... (healthy)`.

<details>
<summary>Optional: confirm pgvector is enabled</summary>

```bash
docker exec -it loglens-db psql -U loglens -d loglens \
  -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

This should print a version number (e.g. `0.8.2`). If it returns no rows, the
init script did not run — recreate the database with `docker compose down -v`
then `docker compose up -d` (see *Managing the database* below).
</details>

### Managing the database

| Action | Command |
|---|---|
| Start (background) | `docker compose up -d` |
| Stop (keep data) | `docker compose down` |
| Reset (delete all data, fresh start) | `docker compose down -v` then `docker compose up -d` |
| View logs | `docker compose logs -f db` |

The data lives in a named Docker volume and persists across stop/start. Use the
reset command when you want a clean database (for example after schema changes).

## 3. Set up the Python environment

Create and activate a virtual environment, then install the dependencies **and**
the project itself:

```bash
# from the repository root
python -m venv .venv

# activate it:
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   Windows (cmd):         .venv\Scripts\activate.bat
#   macOS/Linux:           source .venv/bin/activate

pip install -r requirements.txt   # third-party dependencies
pip install -e .                  # install the loglens package (editable)
```

The `pip install -e .` step installs the project's own `loglens` package into
the environment in editable mode. This is required for `import loglens` to work
outside the test runner (the package lives under `src/`), and edits to the
source take effect immediately.

> **Activate the venv at the start of every session.** Each new terminal starts
> on the system Python; re-run the activate command above. When the venv is
> active your prompt shows `(.venv)`. In VS Code, select the `.venv` interpreter
> (Command Palette → *Python: Select Interpreter*) and integrated terminals
> activate it automatically.

> **Run package modules with `-m`.** Modules under `src/loglens/` use relative
> imports, so invoke them as `python -m loglens.<module>` (e.g.
> `python -m loglens.init_db`), **not** as a file path
> (`python src/loglens/init_db.py`), which fails with
> "attempted relative import with no known parent package".

Verify the environment:

```bash
python -c "import sys; print(sys.executable)"   # should point inside .venv
python -c "import psycopg; print(psycopg.__version__)"
```

## 4. Configure the database connection

The connection string is supplied via the `LOGLENS_DB_DSN` environment variable
(never written into a committed file). For the default Docker database:

```bash
# macOS/Linux
export LOGLENS_DB_DSN="postgresql://loglens:loglens@localhost:5432/loglens"

# Windows (PowerShell)
$env:LOGLENS_DB_DSN="postgresql://loglens:loglens@localhost:5432/loglens"
```

(The `loglens`/`loglens` credentials are local-development defaults defined in
`docker-compose.yml` — not secrets, used only by the local container. The
variable lasts only for the current terminal session.)

Confirm the application can reach the database and that pgvector is enabled:

```bash
python -c "from loglens.storage.postgres import check_connection; print(check_connection())"
```

This should print the server and pgvector versions, e.g.
`{'server_version': '17.x', 'pgvector_version': '0.8.2'}`.

## 5. Apply the database schema

With the database running and `LOGLENS_DB_DSN` set, create the tables:

```bash
python -m loglens.init_db
```

You should see each schema file applied, ending with `Done. Schema is up to
date.` This step is **idempotent** (it uses `IF NOT EXISTS` throughout), so it
is safe to run again after pulling schema changes. To verify:
`docker exec -it loglens-db psql -U loglens -d loglens -c "\dt"`.

## 6. Generate sample log data

Generate synthetic Windows service logs into a per-source folder. The
`--incidents` and `--pii` flags seed correlated error bursts and synthetic PII
fields (useful for later layers); both are optional.

```bash
python tools/generate_windows_logs.py \
    --output_dir data/raw/windows_service_1 --incidents --pii
```

See [tools/README.md](../tools/README.md) for all generator options. `data/raw`
is git-ignored (bulk data); `data/sample` holds the small committed sample.

---

## Running the pipeline

The pipeline currently covers two layers: **bronze** (raw landing) and
**silver** (parsed, normalized entries). Each step is run per source. While the
system is still under active development there is no single orchestrator yet;
each step is invoked directly (a `main.py` orchestrator and config-driven runs
will come once the layers stabilize).

### 7a. Ingest into the bronze layer

Reads the source's log files, splits them into individual entries, and lands
them verbatim into `bronze_landing` (idempotent — re-running skips unchanged
files). A run is recorded in `bronze_runs`.

```bash
python -m loglens.pipeline.landing_bronze \
    --source-id windows_service_1 \
    --location data/raw/windows_service_1 \
    --timezone Australia/Brisbane
```

A summary prints at the end (files processed, entries landed, duration). To
watch progress live, query `bronze_runs` in another session:

```sql
SELECT source_id, status, files_processed, entries_landed
FROM bronze_runs ORDER BY started_utc DESC LIMIT 1;
```

### 7b. Transform bronze → silver

Reads undigested `bronze_landing` entries for the source, parses each into the
common silver shape (UTC-normalized time, normalized + raw severity, logger,
message, exception text, and a JSON overflow for the rest), writes
`silver_log_entries`, and marks the bronze entries digested. Commits per file.

```bash
python -m loglens.pipeline.silver \
    --source-id windows_service_1 \
    --timezone Australia/Brisbane
```

### 7c. Inspect the result

```sql
-- structured silver rows
SELECT severity, logger, is_exception, message, event_time_utc
FROM silver_log_entries LIMIT 20;

-- error / exception counts
SELECT count(*) AS total,
       count(*) FILTER (WHERE is_exception) AS exceptions
FROM silver_log_entries;
```

> **Note on the `--timezone`.** Pass the IANA zone the source's logs were
> recorded in; it is used to normalize timestamps to UTC (ADR-007). Use the same
> value for ingest and transform of a given source.

> **Re-running the transform.** The transform only reads *undigested* bronze
> entries. To re-transform after a parser change during development, reset the
> flags first: `UPDATE bronze_landing SET is_digested = false;` (and optionally
> `TRUNCATE silver_log_entries;`). For a fully clean slate, reset the database
> with `docker compose down -v` and start from step 2.

## 8. Run the tests

```bash
pytest tests/ -v
```

The database-dependent tests run only when `LOGLENS_DB_DSN` is set; otherwise
they skip, so the suite stays green without a database.

---

## Troubleshooting

**`attempted relative import with no known parent package`.** You ran a module
by file path. Use the module form instead: `python -m loglens.init_db` (see
step 3).

**`docker compose up` fails / port 5432 already in use.** Another Postgres may
be running. Stop it, or change the host port in `docker-compose.yml` (e.g.
`"5433:5432"`) and update `LOGLENS_DB_DSN`.

**pgvector extension missing.** The init script only runs on a fresh database.
Reset with `docker compose down -v` then `docker compose up -d`.

**`ModuleNotFoundError: No module named 'loglens'`.** The package is not
installed in the active environment. Activate the venv and run `pip install -e .`
(step 3).

**`LOGLENS_DB_DSN is not set`.** Set the connection string in the current
terminal (step 4); it only lasts for that session.

**Transform reports 0 entries.** All bronze entries for the source are already
digested. Reset with `UPDATE bronze_landing SET is_digested = false;` to
re-transform (see step 7b note).

**Cannot connect from Python.** Confirm the container is healthy
(`docker compose ps`) and that `LOGLENS_DB_DSN` matches the credentials in
`docker-compose.yml`.
