"""Silver -> Gold segmentation (ADR-009, ADR-010, ADR-011, ADR-014, ADR-016).

Reads exception rows from the silver layer for one source, asks the source's
registered parser to split each exception into structure-aware SIGNATURE
segments, and writes them to gold_exception_segments.

This orchestrator is SYSTEM-AGNOSTIC: it contains no per-log-type logic. The
"how to segment this system's exceptions" knowledge lives in the per-type parser
(selected via the registry by log_type), which exposes segment_exception().
Adding a new log type means adding a parser with a segmenter — gold.py is
unchanged. (Same pattern as silver.py delegating parse() to the parser.)

Carried metadata (event_time_utc, severity, logger) is copied onto each segment
as filterable columns for later hybrid retrieval (ADR-011); only segment_text is
intended for embedding.

Commits per silver row (its segments are one unit). Idempotent: the unique
(silver_entry_id, segment_index) plus ON CONFLICT DO NOTHING make re-runs safe.
Embedding is a SEPARATE later step (ADR-012); this step only produces segments.

Callable per source by main.py or any orchestrator (ADR-014).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..parsers.registry import get_parser
from ..storage.postgres import connect


@dataclass
class SegmentResult:
    source_id: str
    exceptions_processed: int = 0
    segments_written: int = 0
    segments_skipped_duplicate: int = 0
    exceptions_failed: int = 0


def _exception_rows(conn, source_id: str):
    """Silver exception rows for this source that have no segments yet.

    'No segments yet' is judged from gold's own table (a LEFT JOIN anti-match),
    so re-running only processes new exceptions. Gold depends only on silver +
    its own state, never the reverse (ADR-016).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.exception_text, s.event_time_utc, s.severity, s.logger
            FROM silver_log_entries s
            WHERE s.source_id = %s
              AND s.is_exception = TRUE
              AND s.exception_text IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM gold_exception_segments g
                  WHERE g.silver_entry_id = s.id
              );
            """,
            (source_id,),
        )
        return cur.fetchall()


def transform_to_gold(source_config: dict[str, Any], dsn: str | None = None) -> SegmentResult:
    """Segment a source's silver exceptions into gold. Commits per exception."""
    source_id = source_config["source_id"]
    log_type = source_config["log_type"]

    parser = get_parser(log_type)
    if not hasattr(parser, "segment_exception"):
        raise AttributeError(
            f"parser for log_type '{log_type}' does not implement segment_exception()"
        )

    result = SegmentResult(source_id=source_id)

    with connect(dsn) as conn:
        conn.autocommit = True  # explicit per-exception transactions

        rows = _exception_rows(conn, source_id)

        for silver_id, exception_text, event_time_utc, severity, logger in rows:
            try:
                segments = parser.segment_exception(exception_text)
                with conn.transaction():
                    written = _write_segments(
                        conn, silver_id, source_id, log_type,
                        event_time_utc, severity, logger, segments,
                    )
                    result.segments_written += written
                    result.segments_skipped_duplicate += len(segments) - written
                    result.exceptions_processed += 1
            except Exception:
                result.exceptions_failed += 1
                continue

    _print_summary(result)
    return result


def _write_segments(conn, silver_id, source_id, log_type,
                    event_time_utc, severity, logger, segments) -> int:
    if not segments:
        return 0
    rows = [
        (uuid.uuid4(), silver_id, source_id, log_type,
         seg.segment_index, seg.segment_type, seg.segment_text,
         event_time_utc, severity, logger)
        for seg in segments
    ]
    written = 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO gold_exception_segments (
                id, silver_entry_id, source_id, log_type,
                segment_index, segment_type, segment_text,
                event_time_utc, severity, logger
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (silver_entry_id, segment_index) DO NOTHING;
            """,
            rows,
        )
        written = cur.rowcount
    return written


def _print_summary(r: SegmentResult) -> None:
    print(
        f"\nGold segmentation for source '{r.source_id}':\n"
        f"  exceptions processed : {r.exceptions_processed}\n"
        f"  segments written     : {r.segments_written}\n"
        f"  segments duplicate   : {r.segments_skipped_duplicate}\n"
        f"  exceptions failed    : {r.exceptions_failed}"
    )


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Segment a source's silver exceptions into the gold layer.",
    )
    p.add_argument("--source-id", required=True, help="Identifier for the source to segment.")
    p.add_argument("--log-type", default="windows_service", help="Parser/log type. Default: windows_service")
    args = p.parse_args(argv)

    transform_to_gold({"source_id": args.source_id, "log_type": args.log_type})
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
