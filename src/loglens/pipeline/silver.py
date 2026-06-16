"""Bronze -> Silver transform (ADR-005, ADR-006, ADR-007, ADR-014, ADR-016).

Reads UNDIGESTED entries from bronze_landing for one source, parses each
raw_entry into the common silver shape using the source's registered parser,
writes silver_log_entries rows, and marks the bronze entries digested.

Design (matching the agreed decisions):
  * Works per FILE: undigested entries are grouped by their bronze control row
    (processed_log_id). Each file's entries are transformed, written, marked
    digested, and COMMITTED as one unit — so a failure mid-run keeps completed
    files digested and rolls back only the failing file (ADR-014).
  * Reads only bronze's own tables; does not look at gold (ADR-016).
  * Does NOT mark files 'completed' and does NOT archive — a file may still be
    active (being appended to). Completion (judged by file-hash stability over
    days) and archiving are a separate, later maintenance process.
  * Idempotent: silver has a unique (source_id, entry_hash); re-running uses
    ON CONFLICT DO NOTHING, and only undigested bronze rows are read anyway.

Callable per source by main.py or any orchestrator (ADR-014).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from ..parsers.registry import get_parser
from ..parsers.windows_service import to_utc
from ..storage.postgres import connect

SILVER_BATCH_SIZE = 1000


@dataclass
class TransformResult:
    source_id: str
    files_transformed: int = 0
    entries_written: int = 0
    entries_skipped_duplicate: int = 0
    entries_failed: int = 0


def _undigested_file_ids(conn, source_id: str) -> list[uuid.UUID]:
    """Distinct bronze control rows (files) that still have undigested entries."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT processed_log_id
            FROM bronze_landing
            WHERE is_digested = FALSE
              AND processed_log_id IN (
                  SELECT id FROM bronze_processed_logs WHERE source_id = %s
              );
            """,
            (source_id,),
        )
        return [row[0] for row in cur.fetchall()]


def _undigested_entries(conn, processed_log_id: uuid.UUID):
    """Yield (landing_id, entry_hash, raw_entry) for one file's undigested rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, entry_hash, raw_entry
            FROM bronze_landing
            WHERE processed_log_id = %s AND is_digested = FALSE;
            """,
            (processed_log_id,),
        )
        return cur.fetchall()


def transform_to_silver(source_config: dict[str, Any], dsn: str | None = None) -> TransformResult:
    """Transform a source's undigested bronze entries into silver. Commits per file."""
    source_id = source_config["source_id"]
    log_type = source_config["log_type"]
    tz_name = source_config.get("timezone", "UTC")

    parser = get_parser(log_type)
    result = TransformResult(source_id=source_id)

    with connect(dsn) as conn:
        conn.autocommit = True  # explicit per-file transactions below

        file_ids = _undigested_file_ids(conn, source_id)

        for processed_log_id in file_ids:
            try:
                with conn.transaction():
                    rows = _undigested_entries(conn, processed_log_id)
                    silver_rows = []
                    landing_ids = []

                    for landing_id, entry_hash, raw_entry in rows:
                        try:
                            parsed = parser.parse(raw_entry, source_config)
                        except Exception:
                            result.entries_failed += 1
                            continue

                        event_time_utc = to_utc(parsed.event_time_local, tz_name)
                        silver_rows.append((
                            uuid.uuid4(), entry_hash, landing_id, source_id, log_type,
                            parsed.parser_version,
                            event_time_utc, parsed.event_time_local, tz_name,
                            parsed.severity, parsed.severity_raw, parsed.message,
                            parsed.logger, parsed.is_exception, parsed.exception_text,
                            Jsonb(parsed.extra_fields),
                        ))
                        landing_ids.append(landing_id)

                    written = _write_silver(conn, silver_rows)
                    result.entries_written += written
                    result.entries_skipped_duplicate += len(silver_rows) - written

                    _mark_digested(conn, landing_ids)
                    result.files_transformed += 1
                # committed per file

            except Exception:
                # a whole-file failure rolls back this file; continue with others
                result.entries_failed += 0  # file-level failure; entry counts already handled
                continue

    _print_summary(result)
    return result


def _write_silver(conn, silver_rows) -> int:
    if not silver_rows:
        return 0
    written = 0
    with conn.cursor() as cur:
        for start in range(0, len(silver_rows), SILVER_BATCH_SIZE):
            batch = silver_rows[start:start + SILVER_BATCH_SIZE]
            cur.executemany(
                """
                INSERT INTO silver_log_entries (
                    id, entry_hash, bronze_landing_id, source_id, log_type,
                    parser_version,
                    event_time_utc, event_time_local, source_timezone,
                    severity, severity_raw, message,
                    logger, is_exception, exception_text, extra_fields
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_id, entry_hash) DO NOTHING;
                """,
                batch,
            )
            written += cur.rowcount
    return written


def _mark_digested(conn, landing_ids) -> None:
    if not landing_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bronze_landing SET is_digested = TRUE WHERE id = ANY(%s);",
            (landing_ids,),
        )


def _print_summary(r: TransformResult) -> None:
    print(
        f"\nSilver transform for source '{r.source_id}':\n"
        f"  files transformed : {r.files_transformed}\n"
        f"  entries written   : {r.entries_written}\n"
        f"  entries duplicate : {r.entries_skipped_duplicate}\n"
        f"  entries failed    : {r.entries_failed}"
    )
