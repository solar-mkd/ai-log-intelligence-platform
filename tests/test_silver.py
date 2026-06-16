"""Tests for the Windows parser and the bronze->silver transform.

Parser tests run anywhere. The transform end-to-end test requires a database
and LOGLENS_DB_DSN, and skips otherwise.
"""

import os
import pytest

from loglens.parsers.windows_service import WindowsServiceParser, to_utc
from loglens.storage.postgres import DSN_ENV_VAR

CFG = {"timezone": "Australia/Brisbane", "source_id": "demo", "log_type": "windows_service"}

ERROR_ENTRY = """5/4/2026 04:42:39 AM Error: Contoso.Platform.Inventory.ServiceLayer.Tasks.SyncTask record processing details
   Time Taken                   : 00:00:12.3
   Exception Type               : System.Data.SqlClient.SqlException
   Message                      : Transaction was deadlocked.
   Stack Trace                  :
System.Data.SqlClient.SqlException: Transaction was deadlocked.
   at Contoso.Platform.Core.DataAccess.Repository.SaveChangesAsync() in Repository.cs:line 142"""

INFO_ENTRY = """5/4/2026 04:43:01 AM Information: Contoso.Platform.Core.Infrastructure Health check completed
   Duration    : 00:00:00.5
   Status      : OK"""


def test_parses_header_fields():
    r = WindowsServiceParser().parse(ERROR_ENTRY, CFG)
    assert r.severity == "ERROR"
    assert r.severity_raw == "Error"
    assert r.logger.startswith("Contoso.Platform.Inventory")
    assert r.event_time_local is not None


def test_utc_conversion():
    r = WindowsServiceParser().parse(ERROR_ENTRY, CFG)
    utc = to_utc(r.event_time_local, CFG["timezone"])
    # Brisbane is UTC+10, so 04:42 local -> 18:42 previous day UTC
    assert utc.hour == 18


def test_detects_exception_and_captures_text():
    r = WindowsServiceParser().parse(ERROR_ENTRY, CFG)
    assert r.is_exception is True
    assert "SqlException" in r.exception_text
    assert "Repository.cs:line 142" in r.exception_text


def test_non_exception_fields_go_to_extra():
    r = WindowsServiceParser().parse(ERROR_ENTRY, CFG)
    assert "Time Taken" in r.extra_fields
    # exception-related keys must NOT be in extra_fields
    assert "Exception Type" not in r.extra_fields


def test_info_entry_not_exception():
    r = WindowsServiceParser().parse(INFO_ENTRY, CFG)
    assert r.is_exception is False
    assert r.severity == "INFO"
    assert r.extra_fields.get("Status") == "OK"


@pytest.mark.skipif(
    not os.environ.get(DSN_ENV_VAR),
    reason="LOGLENS_DB_DSN not set; skipping live transform test",
)
def test_transform_end_to_end():
    """Land two entries in bronze, transform to silver, confirm rows + digested."""
    import uuid
    from loglens.pipeline.landing_bronze import ingest_to_bronze
    from loglens.pipeline.silver import transform_to_silver
    from loglens.storage.postgres import connect
    import tempfile, pathlib

    src = f"silver-test-{uuid.uuid4().hex[:8]}"
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "log-05-04-2026.log").write_text(ERROR_ENTRY + "\n" + INFO_ENTRY + "\n", encoding="utf-8")

    cfg = {"source_id": src, "log_type": "windows_service", "location": str(tmp),
           "timezone": "Australia/Brisbane"}

    ingest_to_bronze(cfg)
    res = transform_to_silver(cfg)
    assert res.entries_written == 2

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM silver_log_entries WHERE source_id=%s;", (src,))
            assert cur.fetchone()[0] == 2
            cur.execute(
                "SELECT count(*) FROM silver_log_entries WHERE source_id=%s AND is_exception;", (src,))
            assert cur.fetchone()[0] == 1
            # all bronze entries for this source should now be digested
            cur.execute(
                """SELECT count(*) FROM bronze_landing bl
                   JOIN bronze_processed_logs bpl ON bl.processed_log_id=bpl.id
                   WHERE bpl.source_id=%s AND bl.is_digested=FALSE;""", (src,))
            assert cur.fetchone()[0] == 0
            # cleanup
            cur.execute("DELETE FROM silver_log_entries WHERE source_id=%s;", (src,))
            cur.execute("DELETE FROM bronze_processed_logs WHERE source_id=%s;", (src,))
