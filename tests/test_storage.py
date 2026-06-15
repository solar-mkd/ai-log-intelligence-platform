"""Tests for the storage adapter.

The connectivity tests require a running database and LOGLENS_DB_DSN set; they
skip automatically if it isn't, so the suite stays green in environments without
a database (e.g. CI that doesn't spin one up).
"""

import os

import pytest

from loglens.storage.postgres import (
    DSN_ENV_VAR,
    StorageError,
    check_connection,
    get_dsn,
)


def test_get_dsn_raises_when_unset(monkeypatch):
    monkeypatch.delenv(DSN_ENV_VAR, raising=False)
    with pytest.raises(StorageError):
        get_dsn()


def test_get_dsn_returns_value_when_set(monkeypatch):
    monkeypatch.setenv(DSN_ENV_VAR, "postgresql://u:p@localhost:5432/db")
    assert get_dsn() == "postgresql://u:p@localhost:5432/db"


@pytest.mark.skipif(
    not os.environ.get(DSN_ENV_VAR),
    reason="LOGLENS_DB_DSN not set; skipping live database check",
)
def test_check_connection_live():
    facts = check_connection()
    assert facts["pgvector_version"], "pgvector should be enabled"
    assert facts["server_version"]
