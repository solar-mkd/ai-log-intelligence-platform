"""Tests for schema application.

Requires a running database and LOGLENS_DB_DSN; skips otherwise so the suite
stays green without a database. Applying the schema is idempotent, so running
this against a real database is safe and repeatable.
"""

import os

import pytest

from loglens.storage.postgres import DSN_ENV_VAR, connect

requires_db = pytest.mark.skipif(
    not os.environ.get(DSN_ENV_VAR),
    reason="LOGLENS_DB_DSN not set; skipping live database test",
)

EXPECTED_TABLES = {
    "bronze_processed_logs",
    "bronze_landing",
    "bronze_archive",
}


@requires_db
def test_apply_schema_creates_bronze_tables():
    from loglens.init_db import apply_schema

    apply_schema()  # idempotent

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s);
                """,
                (list(EXPECTED_TABLES),),
            )
            found = {row[0] for row in cur.fetchall()}

    missing = EXPECTED_TABLES - found
    assert not missing, f"missing bronze tables: {missing}"


@requires_db
def test_apply_schema_is_idempotent():
    from loglens.init_db import apply_schema

    # Running twice must not raise.
    apply_schema()
    apply_schema()
