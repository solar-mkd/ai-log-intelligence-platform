-- ============================================================================
-- LogLens — Silver layer schema (ADR-005, ADR-006, ADR-007, ADR-015)
-- ============================================================================
-- One COMMON table for all log types. Type-specific differences are absorbed by
-- the extra_fields JSONB column, not by separate tables — this keeps
-- cross-system queries (the platform's core use case) simple: "all errors
-- across every system in a time window" is one query, and adding a new log type
-- means adding a PARSER, never changing this table (ADR-005).
--
-- Columns are promoted only when they are queried constantly / at scale for log
-- intelligence (time, severity, message, logger, exception). Everything
-- type-specific or incidental (counts, urls, user/country, queue names, …)
-- lives in extra_fields — preserved, still queryable via JSON when an
-- investigation needs it, but not cluttering the hot path.
--
-- "Normalize for querying, retain raw for audit" (ADR-006): severity and time
-- are stored both normalized and raw.
--
-- Idempotency: entry_hash carries over from bronze; a unique constraint per
-- (source_id, entry_hash) makes the bronze->silver merge safe to re-run.
--
-- Safe to run repeatedly (IF NOT EXISTS everywhere).
-- ============================================================================

CREATE TABLE IF NOT EXISTS silver_log_entries (
    -- ---- lineage / identity ----
    id                 UUID PRIMARY KEY,           -- app-generated (uuid4)
    entry_hash         TEXT        NOT NULL,        -- carried from bronze (dedup)
    bronze_landing_id  UUID,                        -- back-reference for traceability
    source_id          TEXT        NOT NULL,
    log_type           TEXT        NOT NULL,        -- windows_service, apache, …
    parser_version     TEXT        NOT NULL,        -- which parser produced this row
    ingested_at_utc    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ---- universal log core (queried constantly) ----
    event_time_utc     TIMESTAMPTZ,                 -- normalized UTC (correlation key)
    event_time_local   TIMESTAMP,                   -- original local time as recorded
    source_timezone    TEXT,                        -- IANA zone used for conversion
    severity           TEXT,                        -- normalized: ERROR/WARN/INFO/…
    severity_raw       TEXT,                        -- source's original level string
    message            TEXT,                        -- primary human-readable message

    -- ---- promoted intelligence fields ----
    logger             TEXT,                        -- emitting component/namespace
    is_exception       BOOLEAN     NOT NULL DEFAULT FALSE,
    exception_text     TEXT,                        -- full reassembled exception (gold segments this)

    -- ---- flexibility hedge: everything not promoted ----
    extra_fields       JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Idempotency: a given entry appears once per source (ADR-003/005).
    CONSTRAINT uq_silver_entry UNIQUE (source_id, entry_hash)
);

-- Time-window correlation is the headline query pattern → index event_time_utc.
CREATE INDEX IF NOT EXISTS ix_silver_event_time
    ON silver_log_entries (event_time_utc);

-- Cross-system filtering by source and severity.
CREATE INDEX IF NOT EXISTS ix_silver_source_severity
    ON silver_log_entries (source_id, severity);

-- Reporting/filtering by component (PowerBI: errors per logger).
CREATE INDEX IF NOT EXISTS ix_silver_logger
    ON silver_log_entries (logger);

-- Partial index on exceptions only (the Postgres-idiomatic equivalent of a
-- low-cardinality "bitmap" lookup): gold and error analysis query
-- WHERE is_exception = true, so index just that subset, ordered by time.
CREATE INDEX IF NOT EXISTS ix_silver_exceptions
    ON silver_log_entries (event_time_utc)
    WHERE is_exception = TRUE;

-- Optional: GIN index on the JSON overflow, enabling reasonably fast queries on
-- extra_fields when an incidental investigation needs them (e.g. by country).
-- Commented out by default — enable if/when JSON queries become frequent.
-- CREATE INDEX IF NOT EXISTS ix_silver_extra_fields
--     ON silver_log_entries USING GIN (extra_fields);
