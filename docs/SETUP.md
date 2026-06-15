# Setup

How to get LogLens running locally, from cloning the repository to a working
database. The pipeline steps will be added to this guide as they come online;
for now this covers the environment and the PostgreSQL + pgvector database.

> For *what* the system is and *why* it is built this way, see the
> [README](../README.md), [architecture](architecture.md), and
> [ADRs](adr/ADRs.md).

---

## Prerequisites

- **Docker** (Docker Desktop on Windows/macOS, Docker Engine on Linux) — runs
  the PostgreSQL + pgvector database. Verify with `docker run hello-world`.
- **Python 3.11+** — runs the application and the log generator. Verify with
  `python --version`.

That is all that is required. The database extension (pgvector) is enabled
automatically; there is no manual database installation.

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
`vector` extension automatically the first time the database is created, so no
further database setup is needed.

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
then `docker compose up -d` (see *Resetting* below).
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

```bash
# from the repository root
python -m venv .venv

# activate it:
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   Windows (cmd):         .venv\Scripts\activate.bat
#   macOS/Linux:           source .venv/bin/activate

pip install -r requirements.txt
```

## 4. Configure the application

Copy the example config and adjust as needed:

```bash
cp config/config.example.yaml config/config.yaml
```

`config/config.yaml` is git-ignored and is where local settings live. Secrets
and the database connection string are supplied via environment variables, not
written into the file. For the default Docker database above, set:

```bash
# macOS/Linux
export LOGLENS_DB_DSN="postgresql://loglens:loglens@localhost:5432/loglens"

# Windows (PowerShell)
$env:LOGLENS_DB_DSN="postgresql://loglens:loglens@localhost:5432/loglens"
```

(The `loglens`/`loglens` credentials are local-development defaults defined in
`docker-compose.yml`. They are not secrets and are only used by the local
container.)

## 5. Generate sample log data

The repository includes a small committed sample under `data/sample`. To
generate more (for example, a larger local set for testing):

```bash
python tools/generate_windows_logs.py --output_dir data/raw --incidents --pii
```

See [tools/README.md](../tools/README.md) for all generator options. Note that
`data/raw` is git-ignored (for bulk data); `data/sample` is committed.

---

## Running the pipeline

*Coming soon — these steps will be documented here as each layer is implemented:*

- Ingest logs into the bronze layer
- Normalize into the silver layer
- Build exception segments and embeddings in the gold layer
- Ask cross-system questions via retrieval

---

## Troubleshooting

**`docker compose up` fails / port 5432 already in use.** Another Postgres may
be running on your machine. Stop it, or change the host port in
`docker-compose.yml` (e.g. `"5433:5432"`) and update `LOGLENS_DB_DSN`
accordingly.

**pgvector extension missing.** The init script only runs on a fresh database.
Reset with `docker compose down -v` then `docker compose up -d`.

**Cannot connect from Python.** Confirm the container is healthy
(`docker compose ps`) and that `LOGLENS_DB_DSN` matches the host, port, user,
password, and database in `docker-compose.yml`.
