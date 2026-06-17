-- ============================================================================
-- LogLens — Gold layer schema (ADR-009, ADR-010, ADR-011, ADR-012)
-- ============================================================================
-- Gold is the analytical / RAG-serving product. Two tables:
--
--   gold_exception_segments — exceptions split into structure-aware segments
--     (ADR-009, ADR-010). The segment is the unit of recurrence: the exception
--     SIGNATURE (type + message), one per exception in the inner-exception
--     chain. segment_text is what gets embedded; time/severity/logger are
--     carried as FILTERABLE COLUMNS (not embedded) so hybrid retrieval can
--     filter on them while ranking by vector similarity (ADR-011).
--
--   gold_embeddings — one row per (segment, embedding model). Kept separate so
--     a segment can be embedded by multiple models for migration / A-B
--     comparison, each vector tagged with the model that produced it (ADR-012).
--
-- System-agnostic: every log type's parser produces the same segment shape, so
-- one set of tables serves all systems (ADR-005 applied to gold).
--
-- Safe to run repeatedly (IF NOT EXISTS everywhere).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Exception segments: the recurring, embeddable units.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold_exception_segments (
    id               UUID PRIMARY KEY,            -- app-generated (uuid4)
    silver_entry_id  UUID        NOT NULL,        -- lineage: which silver exception
    source_id        TEXT        NOT NULL,
    log_type         TEXT        NOT NULL,

    segment_index    INTEGER     NOT NULL,        -- order within the exception (0 = outer)
    segment_type     TEXT        NOT NULL,        -- e.g. 'signature', 'inner_signature'
    segment_text     TEXT        NOT NULL,        -- the text that gets embedded

    -- Carried metadata for HYBRID retrieval (ADR-011) — filterable columns,
    -- deliberately NOT embedded into segment_text.
    event_time_utc   TIMESTAMPTZ,
    severity         TEXT,
    logger           TEXT,

    created_at_utc   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A given exception (silver row) yields its segments once; re-running the
    -- segmenter is idempotent on (silver_entry_id, segment_index).
    CONSTRAINT uq_segment UNIQUE (silver_entry_id, segment_index)
);

CREATE INDEX IF NOT EXISTS ix_seg_event_time
    ON gold_exception_segments (event_time_utc);
CREATE INDEX IF NOT EXISTS ix_seg_source_severity
    ON gold_exception_segments (source_id, severity);
CREATE INDEX IF NOT EXISTS ix_seg_silver
    ON gold_exception_segments (silver_entry_id);

-- ----------------------------------------------------------------------------
-- Embeddings: one row per (segment, embedding model). Model-pinned (ADR-012).
-- Dimension fixed at 384 for the current model; other dimensions, if tested
-- later, get their own table (vector(N) is fixed-width per column).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold_embeddings (
    id                UUID PRIMARY KEY,           -- app-generated (uuid4)
    segment_id        UUID        NOT NULL
                      REFERENCES gold_exception_segments (id) ON DELETE CASCADE,
    embedding_model   TEXT        NOT NULL,        -- which model produced this vector
    embedding_version TEXT        NOT NULL,        -- model version (pinning)
    embedding         vector(384) NOT NULL,        -- the vector itself
    created_at_utc    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A segment is embedded once per (model, version): re-running is idempotent.
    CONSTRAINT uq_embedding UNIQUE (segment_id, embedding_model, embedding_version)
);

-- HNSW index for approximate-nearest-neighbour search using cosine distance
-- (pgvector's <=> operator). Built now so similarity search is fast as the
-- table grows; for small demo volumes it is harmless overhead.
CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw
    ON gold_embeddings USING hnsw (embedding vector_cosine_ops);
