"""PostgreSQL + pgvector storage adapter (ADR-013, ADR-014).

This is the storage boundary that keeps the rest of the platform
database-agnostic: the pipeline talks to this adapter, not to psycopg or to
SQL directly. PostgreSQL + pgvector is the reference implementation.

Connection details are read from the LOGLENS_DB_DSN environment variable so no
secrets live in code or config files (see config/config.example.yaml). For the
local Docker database the DSN is, for example:

    postgresql://loglens:loglens@localhost:5432/loglens

This first cut provides connection handling and a connectivity check. Schema
(the bronze tables) is added in a later step, once the connection is verified.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

# Name of the environment variable holding the connection string.
DSN_ENV_VAR = "LOGLENS_DB_DSN"


class StorageError(RuntimeError):
    """Raised for storage-layer problems (missing config, connection failure)."""


def get_dsn() -> str:
    """Return the database DSN from the environment, or raise a clear error.

    Keeping the DSN in the environment (not in code or committed config) is what
    keeps credentials out of the repository.
    """
    dsn = os.environ.get(DSN_ENV_VAR)
    if not dsn:
        raise StorageError(
            f"{DSN_ENV_VAR} is not set. Set it to your database connection "
            f"string, e.g. postgresql://loglens:loglens@localhost:5432/loglens"
        )
    return dsn


@contextmanager
def connect(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """Yield a database connection, closing it on exit.

    Usage:
        with connect() as conn:
            ...

    The connection commits on clean exit and rolls back if an exception
    propagates, so callers don't have to manage transactions by hand for simple
    operations.
    """
    dsn = dsn or get_dsn()
    try:
        conn = psycopg.connect(dsn)
    except psycopg.OperationalError as exc:
        raise StorageError(
            f"could not connect to the database using {DSN_ENV_VAR}. "
            f"Is the container running (docker compose ps)? Underlying error: {exc}"
        ) from exc

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_connection(dsn: str | None = None) -> dict[str, str]:
    """Verify connectivity and that pgvector is available.

    Returns a small dict of facts about the database (server version and the
    pgvector extension version). Raises StorageError if the database can't be
    reached. Useful as a first-run smoke test:

        python -c "from loglens.storage.postgres import check_connection; print(check_connection())"
    """
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()  # connectivity confirmed

            cur.execute("SHOW server_version;")
            server_version = cur.fetchone()[0]

            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
            )
            row = cur.fetchone()
            vector_version = row[0] if row else None

    if vector_version is None:
        raise StorageError(
            "connected, but the pgvector extension is not enabled. Recreate the "
            "database with: docker compose down -v && docker compose up -d"
        )

    return {
        "server_version": server_version,
        "pgvector_version": vector_version,
    }
