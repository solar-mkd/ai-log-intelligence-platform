"""Bronze landing step (ADR-001, ADR-002, ADR-003, ADR-014, ADR-016).

Ingests raw log entries from a single source into the bronze landing table with
MINIMAL transformation (ELT, not ETL): the file is split into individual
multi-line entries, each entry is hashed for idempotency, and the entries are
landed verbatim. No field parsing happens here — that is a silver-layer concern.

Each FILE is its own committed unit of work (short transactions): locks release
frequently so concurrent writers don't contend (ADR-014), and a downstream
bronze->silver mover — possibly a different node (ADR-016) — can start on
committed files immediately. A single bad file is marked 'failed' and skipped;
it does not abort the run.

Each RUN is recorded in bronze_runs for operational observability: a row is
written 'in_progress' at the start, updated after every file (so progress can be
monitored live), and finalized 'completed'/'failed' at the end. A crashed run
leaves an 'in_progress' row frozen at its last counts.

"Fresh source" is detected from bronze's own control table only (ADR-016).
Callable by main.py today, or by any orchestrator — the step is stateless and
reads its work from config + its own tables (ADR-014).
"""

from __future__ import annotations

import hashlib
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..storage.postgres import connect

DEFAULT_WINDOWS_HEADER_PATTERN = r"^\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} (AM|PM)"
INSERT_BATCH_SIZE = 1000


@dataclass
class IngestResult:
    """Summary of one ingestion run (in-memory mirror of the bronze_runs row)."""
    source_id: str
    run_id: uuid.UUID | None = None
    files_seen: int = 0
    files_processed: int = 0
    files_skipped_unchanged: int = 0
    files_failed: int = 0
    entries_landed: int = 0
    entries_skipped_duplicate: int = 0
    duration_seconds: float | None = None


# ── hashing helpers (ADR-003) ────────────────────────────────────────────────

def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── entry splitting ──────────────────────────────────────────────────────────

def split_entries(text: str, header_pattern: str) -> Iterator[tuple[str, str]]:
    """Split raw file text into (header_line, full_entry) pairs; multi-line
    entries (including exceptions) are captured whole."""
    header_re = re.compile(header_pattern)
    current_header: str | None = None
    current_lines: list[str] = []
    for raw_line in text.splitlines():
        if header_re.match(raw_line):
            if current_header is not None:
                yield current_header, "\n".join(current_lines)
            current_header = raw_line.strip()
            current_lines = [raw_line]
        elif current_header is not None:
            current_lines.append(raw_line)
    if current_header is not None:
        yield current_header, "\n".join(current_lines)


# ── file discovery ───────────────────────────────────────────────────────────

def discover_files(location: str, file_prefix: str, earliest_date: date | None) -> list[Path]:
    base = Path(location)
    matches: list[Path] = []
    for entry in base.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.lower().startswith(file_prefix.lower()):
            continue
        if earliest_date is not None:
            if datetime.fromtimestamp(entry.stat().st_mtime).date() < earliest_date:
                continue
        matches.append(entry)
    return sorted(matches, key=lambda p: p.stat().st_mtime)


# ── control-table helpers (bronze's own state only — ADR-016) ─────────────────

def _source_has_history(conn, source_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM bronze_processed_logs WHERE source_id = %s LIMIT 1;",
            (source_id,),
        )
        return cur.fetchone() is not None


def _get_or_create_control_row(conn, source_id, log_type, file_name, file_path, file_hash):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, file_hash FROM bronze_processed_logs
            WHERE source_id = %s AND file_name = %s AND file_path = %s;
            """,
            (source_id, file_name, file_path),
        )
        row = cur.fetchone()
        if row is not None:
            existing_id, existing_hash = row
            if existing_hash == file_hash:
                return existing_id, True
            cur.execute(
                """
                UPDATE bronze_processed_logs
                SET file_hash = %s, status = 'in_progress',
                    reprocess_count = reprocess_count + 1, updated_at_utc = now()
                WHERE id = %s;
                """,
                (file_hash, existing_id),
            )
            return existing_id, False
        new_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO bronze_processed_logs
                (id, source_id, log_type, file_name, file_path, file_hash, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'in_progress');
            """,
            (new_id, source_id, log_type, file_name, file_path, file_hash),
        )
        return new_id, False


def _mark_failed(conn, processed_log_id: uuid.UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bronze_processed_logs SET status='failed', updated_at_utc=now() WHERE id=%s;",
            (processed_log_id,),
        )


def _land_entries(conn, processed_log_id, entries) -> tuple[int, int]:
    rows = [
        (uuid.uuid4(), processed_log_id, hash_text(raw), raw, header)
        for header, raw in entries
    ]
    total = len(rows)
    landed = 0
    with conn.cursor() as cur:
        for start in range(0, total, INSERT_BATCH_SIZE):
            batch = rows[start:start + INSERT_BATCH_SIZE]
            cur.executemany(
                """
                INSERT INTO bronze_landing
                    (id, processed_log_id, entry_hash, raw_entry, header_excerpt)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (processed_log_id, entry_hash) DO NOTHING;
                """,
                batch,
            )
            landed += cur.rowcount
    return landed, total - landed


# ── run-tracking helpers (bronze_runs — observability) ───────────────────────

def _create_run(conn, run_id, source_id, location, run_host) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bronze_runs (id, source_id, location, run_host, status)
            VALUES (%s, %s, %s, %s, 'in_progress');
            """,
            (run_id, source_id, location, run_host),
        )


def _update_run(conn, run_id, result: "IngestResult") -> None:
    """Update the live counts on the run row (called after each file)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bronze_runs SET
                files_seen = %s, files_processed = %s,
                files_skipped_unchanged = %s, files_failed = %s,
                entries_landed = %s, entries_skipped_duplicate = %s
            WHERE id = %s;
            """,
            (result.files_seen, result.files_processed,
             result.files_skipped_unchanged, result.files_failed,
             result.entries_landed, result.entries_skipped_duplicate, run_id),
        )


def _finalize_run(conn, run_id, status, started_at) -> float:
    finished = datetime.now(timezone.utc)
    duration = (finished - started_at).total_seconds()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bronze_runs SET
                status = %s, finished_utc = %s, duration_seconds = %s
            WHERE id = %s;
            """,
            (status, finished, duration, run_id),
        )
    return duration


# ── public entry point ───────────────────────────────────────────────────────

def ingest_to_bronze(source_config: dict[str, Any], dsn: str | None = None) -> IngestResult:
    """Ingest one source's log files into bronze_landing. Commits per file, and
    records the run in bronze_runs with live per-file progress updates."""
    source_id = source_config["source_id"]
    log_type = source_config["log_type"]
    location = source_config["location"]
    file_prefix = source_config.get("file_prefix", "log")
    header_pattern = source_config.get("header_pattern", DEFAULT_WINDOWS_HEADER_PATTERN)
    earliest_date = _coerce_date(source_config.get("earliest_date"))

    run_id = uuid.uuid4()
    run_host = socket.gethostname()
    started_at = datetime.now(timezone.utc)
    result = IngestResult(source_id=source_id, run_id=run_id)

    with connect(dsn) as conn:
        conn.autocommit = True  # we control per-file transaction boundaries

        # Record the run as in_progress (committed immediately, so it's visible).
        with conn.transaction():
            _create_run(conn, run_id, source_id, location, run_host)

        run_status = "completed"
        try:
            fresh = not _source_has_history(conn, source_id)
            cutoff = earliest_date if fresh else None
            files = discover_files(location, file_prefix, cutoff)
            result.files_seen = len(files)

            for path in files:
                try:
                    file_hash = hash_file(path)
                    with conn.transaction():
                        control_id, unchanged = _get_or_create_control_row(
                            conn, source_id, log_type, path.name, str(path.parent), file_hash,
                        )
                        if unchanged:
                            result.files_skipped_unchanged += 1
                        else:
                            text = path.read_text(encoding="utf-8", errors="replace")
                            entries = list(split_entries(text, header_pattern))
                            landed, skipped = _land_entries(conn, control_id, entries)
                            result.entries_landed += landed
                            result.entries_skipped_duplicate += skipped
                            result.files_processed += 1
                    # file committed; now update the run row (its own commit) so
                    # progress is visible live.
                    with conn.transaction():
                        _update_run(conn, run_id, result)
                except Exception:
                    result.files_failed += 1
                    try:
                        with conn.transaction():
                            fh = ""
                            try:
                                fh = hash_file(path)
                            except Exception:
                                pass
                            cid, _ = _get_or_create_control_row(
                                conn, source_id, log_type, path.name, str(path.parent), fh,
                            )
                            _mark_failed(conn, cid)
                        with conn.transaction():
                            _update_run(conn, run_id, result)
                    except Exception:
                        pass
        except Exception:
            run_status = "failed"
            raise
        finally:
            with conn.transaction():
                duration = _finalize_run(conn, run_id, run_status, started_at)
            result.duration_seconds = duration

    _print_summary(result)
    return result


def _print_summary(r: IngestResult) -> None:
    duration = f"{r.duration_seconds:.2f}" if r.duration_seconds is not None else "n/a"
    print(
        f"\nIngestion run {r.run_id} for source '{r.source_id}':\n"
        f"  files seen        : {r.files_seen}\n"
        f"  files processed   : {r.files_processed}\n"
        f"  files unchanged   : {r.files_skipped_unchanged}\n"
        f"  files failed      : {r.files_failed}\n"
        f"  entries landed    : {r.entries_landed}\n"
        f"  entries duplicate : {r.entries_skipped_duplicate}\n"
        f"  duration (s)      : {duration}"
    )


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


# --- append this block to the END of src/loglens/pipeline/landing_bronze.py ---

def _build_source_config(args) -> dict:
    cfg = {
        "source_id": args.source_id,
        "log_type": args.log_type,
        "location": args.location,
    }
    if args.file_prefix:
        cfg["file_prefix"] = args.file_prefix
    if args.timezone:
        cfg["timezone"] = args.timezone
    if args.header_pattern:
        cfg["header_pattern"] = args.header_pattern
    if args.earliest_date:
        cfg["earliest_date"] = args.earliest_date
    return cfg


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Ingest a source's log files into the bronze landing table.",
    )
    p.add_argument("--source-id", required=True, help="Identifier for this source.")
    p.add_argument("--log-type", default="windows_service", help="Parser/log type. Default: windows_service")
    p.add_argument("--location", required=True, help="Directory containing the log files.")
    p.add_argument("--file-prefix", default=None, help="Filename prefix filter. Default: log")
    p.add_argument("--timezone", default=None, help="IANA timezone of the source (e.g. Australia/Brisbane).")
    p.add_argument("--header-pattern", default=None, help="Override the header regex for entry splitting.")
    p.add_argument("--earliest-date", default=None, help="YYYY-MM-DD cutoff applied only on a first import.")
    args = p.parse_args(argv)

    ingest_to_bronze(_build_source_config(args))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
