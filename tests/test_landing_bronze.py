"""Tests for the bronze landing step.

Pure-logic tests (splitting, hashing) run anywhere. The end-to-end ingestion
test requires a running database and LOGLENS_DB_DSN, and skips otherwise.
"""

import os

import pytest

from loglens.pipeline.landing_bronze import (
    DEFAULT_WINDOWS_HEADER_PATTERN,
    hash_text,
    split_entries,
)
from loglens.storage.postgres import DSN_ENV_VAR

SAMPLE = """5/4/2026 04:42:39 AM Error: Some.Namespace.Task record processing details
   Exception Type               : System.NullReferenceException
   Message                      : Object reference not set to an instance of an object.
   Stack Trace                  :
System.NullReferenceException: Object reference not set to an instance of an object.
   at Some.Namespace.Service.Resolve(Int32 id) in Service.cs:line 515
5/4/2026 04:43:01 AM Information: Some.Namespace heartbeat
   Duration    : 00:00:00.5
   Status      : OK
"""


def test_split_finds_each_entry():
    entries = list(split_entries(SAMPLE, DEFAULT_WINDOWS_HEADER_PATTERN))
    assert len(entries) == 2


def test_split_captures_multiline_entry_whole():
    entries = list(split_entries(SAMPLE, DEFAULT_WINDOWS_HEADER_PATTERN))
    first_header, first_entry = entries[0]
    assert first_header.startswith("5/4/2026 04:42:39 AM Error")
    # the full multi-line body is part of the single entry
    assert "Stack Trace" in first_entry
    assert "Service.cs:line 515" in first_entry
    # and it stops at the next header
    assert "heartbeat" not in first_entry


def test_header_is_first_line():
    entries = list(split_entries(SAMPLE, DEFAULT_WINDOWS_HEADER_PATTERN))
    for header, full in entries:
        assert full.splitlines()[0].strip() == header.strip()


def test_hash_is_deterministic():
    assert hash_text("abc") == hash_text("abc")
    assert hash_text("abc") != hash_text("abd")


@pytest.mark.skipif(
    not os.environ.get(DSN_ENV_VAR),
    reason="LOGLENS_DB_DSN not set; skipping live ingestion test",
)
def test_ingest_to_bronze_lands_entries(tmp_path):
    """End-to-end: write a small log file, ingest it, confirm rows landed and
    that a second run is idempotent (no new rows)."""
    from loglens.pipeline.landing_bronze import ingest_to_bronze
    from loglens.storage.postgres import connect

    log = tmp_path / "log-05-04-2026.log"
    log.write_text(SAMPLE, encoding="utf-8")

    cfg = {
        "source_id": "test-src-pytest",
        "log_type": "windows_service",
        "location": str(tmp_path),
        "file_prefix": "log",
    }

    # clean any prior rows for this test source
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bronze_processed_logs WHERE source_id = %s;",
                (cfg["source_id"],),
            )

    first = ingest_to_bronze(cfg)
    assert first.entries_landed == 2
    assert first.files_processed == 1

    # second run: file unchanged -> skipped, nothing re-landed
    second = ingest_to_bronze(cfg)
    assert second.entries_landed == 0

    # cleanup
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bronze_processed_logs WHERE source_id = %s;",
                (cfg["source_id"],),
            )
