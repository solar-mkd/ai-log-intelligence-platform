"""Apply LogLens database schema (idempotent).

Runs the SQL files under db/schema/ against the database identified by the
LOGLENS_DB_DSN environment variable. Safe to run repeatedly — every statement
uses IF NOT EXISTS, so re-running does not error or destroy data.

Run this once after starting the database (see docs/SETUP.md):

    python -m loglens.init_db

Or directly:

    python src/loglens/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from .storage.postgres import connect, get_dsn, StorageError

# Schema files are applied in this order. Add new layers here as they are built
# (e.g. "silver.sql", "gold.sql").
SCHEMA_FILES = [
    "bronze.sql",
    "silver.sql",
]


def _schema_dir() -> Path:
    """Locate the db/schema directory relative to the repository root.

    This file lives at src/loglens/init_db.py, so the repo root is three levels
    up, and the schema lives at <root>/db/schema.
    """
    root = Path(__file__).resolve().parents[2]
    return root / "db" / "schema"


def apply_schema(dsn: str | None = None) -> list[str]:
    """Apply each schema file in order. Returns the list of files applied."""
    schema_dir = _schema_dir()
    applied: list[str] = []

    with connect(dsn) as conn:
        for name in SCHEMA_FILES:
            path = schema_dir / name
            if not path.exists():
                raise StorageError(f"schema file not found: {path}")
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
            applied.append(name)
    return applied


def main() -> int:
    try:
        dsn = get_dsn()  # fail early with a clear message if unset
        print(f"Applying schema to database...")
        applied = apply_schema(dsn)
        for name in applied:
            print(f"  applied: {name}")
        print("Done. Schema is up to date.")
        return 0
    except StorageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
