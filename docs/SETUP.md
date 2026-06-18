# Setup

How to get LogLens running locally, from cloning the repository to asking
questions of your logs — the full pipeline (bronze → silver → gold →
embeddings) plus the RAG query layer.

> For *what* the system is and *why* it is built this way, see the
> [README](../README.md), [architecture](architecture.md), and
> [ADRs](adr/ADRs.md).

---

## Prerequisites

- **Docker** (Docker Desktop on Windows/macOS, Docker Engine on Linux) — runs
  the PostgreSQL + pgvector database. Verify with `docker run hello-world`.
- **Python 3.11+** — runs the application and the log generator. Verify with
  `python --version`.
- **[Ollama](https://ollama.com)** — runs the local LLM for the RAG layer
  (step 10 only). Download and install it from [ollama.com](https://ollama.com),
  then pull a model: `ollama pull mistral`. Ollama runs as its own local
  background service (not a Python package); see step 10 for starting it and
  confirming the model is available.

The database extension (pgvector) is enabled automatically; there is no manual
database installation.

---

## 1. Clone the repository

```bash
git clone https://github.com/solar-mkd/ai-log-intelligence-platform.git
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
| Stop (keep data) | `docker compose stop` (or `docker compose down`) |
| Reset (delete all data, fresh start) | `docker compose down -v` then `docker compose up -d` |
| View logs | `docker compose logs -f db` |

The data lives in a named Docker volume and persists across stop/start. Only
`docker compose down -v` (note the `-v`) deletes the volume and wipes the data —
use it when you deliberately want a clean database (e.g. after schema changes).
A plain `stop`/`down` keeps everything.

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
source take effect immediately. (Installing `sentence-transformers` also pulls
in PyTorch and friends, so this step downloads a few hundred MB the first time.)

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

## 4. Configure environment variables

Two environment variables are used. Neither is ever written into a committed
file — set them in your shell, or in a local `.env` (git-ignored) that your
editor loads.

**`LOGLENS_DB_DSN`** — the database connection string. For the default Docker
database:

```bash
# macOS/Linux
export LOGLENS_DB_DSN="postgresql://loglens:loglens@localhost:5432/loglens"

# Windows (PowerShell)
$env:LOGLENS_DB_DSN="postgresql://loglens:loglens@localhost:5432/loglens"
```

**`LOGLENS_PII_HMAC_KEY`** — a secret key used to pseudonymise PII fields that a
source configures for `hmac` (ADR-015). Required only if a source has an `hmac`
action in its `pii_policy`; the silver step **fails closed** (refuses to run)
rather than write unprotected PII if it is missing.

```bash
# macOS/Linux
export LOGLENS_PII_HMAC_KEY="a-long-random-secret-string"

# Windows (PowerShell)
$env:LOGLENS_PII_HMAC_KEY="a-long-random-secret-string"
```

> Keep the HMAC key **stable**. The same input only produces the same
> fingerprint under the same key — changing it changes every hash, breaking
> correlation across runs.

> **`.env` is read when the terminal is created.** If you add a variable to
> `.env`, open a **new** terminal so it is picked up.

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

You should see each schema file applied (bronze, silver, gold). This step is
**idempotent**, so it is safe to run again after pulling schema changes. To
verify: `docker exec -it loglens-db psql -U loglens -d loglens -c "\dt"`.

## 6. Configure your sources

Source definitions live in `config/config.yaml` (git-ignored). Copy the example
and edit it:

```bash
cp config/config.example.yaml config/config.yaml
```

Each entry under `sources:` defines one source — its log type, file location,
time zone, and (optionally) a per-field PII policy:

```yaml
sources:
  windows_service_1:
    log_type: windows_service
    location: data/raw/windows_service_1
    timezone: Australia/Brisbane
    pii_policy:              # optional; omit to leave fields as-is
      User: hmac             #   hmac   = keyed pseudonymisation
      Country: hmac          #   redact = irreversible removal
      Account Id: redact

  # A second, structurally different log type — Apache access logs — proving the
  # pluggable parser design (ADR-004). Single-line logs need two extra fields so
  # ingestion finds and splits them correctly:
  #   file_prefix   — the filename prefix to match (Apache files are access-*.log)
  #   header_pattern— a regex that marks the start of each entry; for single-line
  #                   logs, match the start of every line (here: a leading IP).
  apache_1:
    log_type: apache_access
    location: data/raw/apache_1
    timezone: Australia/Brisbane
    file_prefix: access
    header_pattern: "^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3} "
    pii_policy:              # client IP / remote user are PII for Apache
      client_ip: hmac
      remote_user: hmac
  apache_2:
    log_type: apache_access
    location: data/raw/apache_2
    timezone: Australia/Brisbane
    file_prefix: access
    header_pattern: "^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3} "
```

> **`file_prefix` and `header_pattern`** default to the Windows convention
> (prefix `log`, an `M/D/YYYY h:mm:ss AM/PM` header). Any log type whose files
> or entry boundaries differ — like Apache — sets them explicitly per source, as
> above. This is the only configuration a new single-line log type needs.

The same `log_type` may appear under several sources with different settings —
e.g. a production source with a `pii_policy` and a test source without one
(`apache_1` vs `apache_2` above). Confirm your sources load:

```bash
python -c "from loglens.config import list_sources; print(list_sources())"
```

## 7. Generate sample log data

Generate synthetic Windows service logs into each source's folder. The
`--incidents` and `--pii` flags seed correlated error bursts and synthetic PII
fields; both are optional (use `--pii` for a source whose PII handling you want
to exercise).

```bash
python tools/generate_windows_logs.py \
    --output_dir data/raw/windows_service_1 --incidents --pii
```

For Apache sources, generate access logs into each Apache source folder. The
generator defaults to the same date range as the Windows sample
(2026-04-30 .. 2026-05-10) so the two systems overlap in time and cross-system
queries have correlated evidence; use a different `--seed` per source:

```bash
python tools/generate_apache_logs.py --output_dir data/raw/apache_1 --seed 1
python tools/generate_apache_logs.py --output_dir data/raw/apache_2 --seed 2
```

See [tools/README.md](../tools/README.md) for all generator options. `data/raw`
is git-ignored (bulk data); `data/sample` holds the small committed sample.

---

## Running the pipeline

Every step is run **per source by id** (`--source-id`); all other settings come
from `config/config.yaml`. You can run the whole pipeline for every configured
source with one command, or run individual steps.

### 8. Run the full pipeline (all sources)

```bash
python -m loglens.run_pipeline
```

This runs, for every source in config, in order: **bronze landing → silver →
gold segmentation → embeddings**. It continues to the next source if one fails,
prints a per-source summary at the end, and records any failures in
`logs/pipeline_errors.log`.

<details>
<summary>Or run the steps individually</summary>

```bash
# bronze: land raw entries (idempotent; re-running skips unchanged files)
python -m loglens.pipeline.landing_bronze --source-id windows_service_1

# silver: parse + normalize + apply PII policy
python -m loglens.pipeline.silver --source-id windows_service_1

# gold: segment exceptions into signatures
python -m loglens.pipeline.gold --source-id windows_service_1

# embeddings: vectorise the segments (omit --source-id to embed all sources)
python -m loglens.pipeline.embed_gold --source-id windows_service_1
```
</details>

### 9. Inspect the result

> **Running the SQL examples.** These run against the `loglens` **database**.
> Open a SQL prompt with:
> ```bash
> docker exec -it loglens-db psql -U loglens -d loglens
> ```
> Note the naming: `loglens-db` is the *Docker container*, while the database,
> user, and password are all `loglens`. Or connect with a GUI client (e.g.
> DBeaver) using host `localhost`, port `5432`, database `loglens`, user
> `loglens`, password `loglens`. These are local-development defaults from
> `docker-compose.yml` — not secrets.

```sql
-- structured silver rows
SELECT severity, logger, is_exception, message, event_time_utc
FROM silver_log_entries LIMIT 20;

-- distinct exception signatures and how often each recurs
SELECT segment_text, count(*) AS occurrences
FROM gold_exception_segments
GROUP BY segment_text ORDER BY occurrences DESC LIMIT 20;

-- confirm a PII-protected source is obfuscated (hashes / [REDACTED])
SELECT extra_fields FROM silver_log_entries
WHERE source_id = 'windows_service_1' AND extra_fields ? 'User' LIMIT 5;

-- confirm BOTH log types are present in gold (the pluggability check)
SELECT source_id, log_type, count(*)
FROM gold_exception_segments
GROUP BY source_id, log_type ORDER BY source_id;
```

### 10. Ask questions (RAG)

This step needs Ollama running with a model pulled. Start it and confirm:

```bash
# 1. Make sure the Ollama service is running.
#    On Windows/macOS the desktop app starts it automatically (check the tray /
#    menu bar). To start it manually in a spare terminal:
ollama serve

# 2. Pull a model once (downloads a few GB the first time):
ollama pull mistral

# 3. Confirm the model is available (and the service is reachable):
ollama list      # 'mistral' should appear in the list
```

With Ollama running and a model pulled, ask in natural language. One-shot:

```bash
python -m loglens.rag.ask_gold "what database errors are happening and how often?"
```

Or an interactive chat that keeps conversation memory for follow-ups:

```bash
python -m loglens.rag.chat_gold
```

Useful flags: `--model <name>` (default `mistral`), `--top-k <n>`,
`--show-context` (ask_gold). The first call loads the model into memory and is
slow; later calls are faster.

> **First-call cold start.** On a CPU-only or slower machine the very first query
> can be slow enough to time out while the model loads. If that happens, simply
> run it again — the model stays warm in memory for a few minutes, so the second
> call returns quickly. You can also "warm" it first (`ollama run mistral`, type
> anything, then `/bye`), or use a smaller, faster model — e.g.
> `ollama pull llama3.2:3b` then `--model llama3.2:3b` — which is more than
> adequate for summarizing the retrieved segments.

> You can also query retrieval directly, without the LLM, to inspect what would
> be fed to it: `python -m loglens.rag.retrieve_gold "database deadlock"`.

## 11. Run the tests

```bash
pytest tests/ -v
```

The database-dependent tests run only when `LOGLENS_DB_DSN` is set; otherwise
they skip, so the suite stays green without a database. The PII tests need no
database.

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

**`LOGLENS_PII_HMAC_KEY is not set` (PIIPolicyError).** A source has an `hmac`
PII action but the key isn't set. Set it (step 4) in a fresh terminal. This
fail-closed behaviour is deliberate — it prevents writing unprotected PII.

**`source '<id>' not found in config`.** The `--source-id` doesn't match a key
under `sources:` in `config/config.yaml`. Check `list_sources()` (step 6).

**Transform/segment reports 0 entries.** The source's entries are already
processed at that layer (idempotency). To re-transform after a parser change,
reset: `UPDATE bronze_landing SET is_digested = false;` (and optionally
`TRUNCATE silver_log_entries;`). For a fully clean slate, `docker compose down -v`
and start from step 2.

**RAG can't reach Ollama.** Confirm the Ollama service is running and the model
is pulled (`ollama list` — start it with `ollama serve` or the desktop app). The
first query is slow while the model loads; if it times out, run it again (the
model stays warm), or use a smaller model (e.g. `--model llama3.2:3b`).

**A source runs `[OK]` but lands 0 entries.** Ingestion found no matching files,
or matched files but no entry boundaries. Check the source's `file_prefix`
matches the actual filenames (Apache files are `access-*.log`, so
`file_prefix: access`) and that `header_pattern` matches the start of an entry
for that log type (step 6).

**Cannot connect from Python.** Confirm the container is healthy
(`docker compose ps`) and that `LOGLENS_DB_DSN` matches the credentials in
`docker-compose.yml`.