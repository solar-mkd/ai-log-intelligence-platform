"""Embed gold exception segments (ADR-011, ADR-012, ADR-013, ADR-014, ADR-016).

Generates a vector embedding for each gold_exception_segments.segment_text and
stores it in gold_embeddings, tagged with the model that produced it.

Design:
  * Embeds ONLY segments that lack an embedding for the CURRENT model+version.
    This avoids wasted CPU on re-runs and lets embedding run independently of
    segmentation — even on a different machine / GPU (ADR-016). A different
    model later will embed everything again (its own rows), enabling A/B model
    comparison (ADR-012).
  * Batched: segment_text is embedded in batches (models are far faster batched),
    and each batch is written + committed as a unit.
  * Idempotent: skip-by-query is the primary mechanism; the unique
    (segment_id, embedding_model, embedding_version) plus ON CONFLICT DO NOTHING
    is a safety net against races / partial runs.
  * embedding-only step: depends on the segments table + its own table; does not
    re-segment or touch silver (ADR-016).

The embedding model (sentence-transformers all-MiniLM-L6-v2, 384-dim) is loaded
lazily on first use; the model file downloads once and is cached locally.

Callable per source by main.py or any orchestrator (ADR-014).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..storage.postgres import connect

# Model pinning (ADR-012). Stored on every embedding row so vectors are always
# attributable to the exact model that produced them.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_VERSION = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# How many segment texts to embed per model call / per DB write+commit.
EMBED_BATCH_SIZE = 128

# Module-level cache so the (heavy) model is loaded once per process.
_model = None


def _get_model():
    """Lazily load the embedding model (first call downloads + caches it)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


@dataclass
class EmbedResult:
    source_id: str | None = None
    segments_embedded: int = 0
    segments_skipped_existing: int = 0
    batches: int = 0


def _unembedded_segments(conn, source_id: str | None):
    """Segments with no embedding for the CURRENT model+version.

    If source_id is given, scope to that source; otherwise embed across all
    sources. "Unembedded" is per (segment, model, version) — a segment already
    embedded by THIS model is skipped, but a new model would re-embed it.
    """
    sql = """
        SELECT g.id, g.segment_text
        FROM gold_exception_segments g
        WHERE NOT EXISTS (
            SELECT 1 FROM gold_embeddings e
            WHERE e.segment_id = g.id
              AND e.embedding_model = %s
              AND e.embedding_version = %s
        )
    """
    params: list[Any] = [EMBEDDING_MODEL, EMBEDDING_VERSION]
    if source_id is not None:
        sql += " AND g.source_id = %s"
        params.append(source_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def embed_segments(source_config: dict[str, Any] | None = None,
                   dsn: str | None = None) -> EmbedResult:
    """Embed unembedded gold segments. Commits per batch.

    source_config may be None (embed all sources) or carry {"source_id": ...}
    to scope to one source.
    """
    source_id = source_config.get("source_id") if source_config else None
    result = EmbedResult(source_id=source_id)

    with connect(dsn) as conn:
        conn.autocommit = True  # explicit per-batch transactions

        pending = _unembedded_segments(conn, source_id)
        if not pending:
            _print_summary(result)
            return result

        model = _get_model()  # load once (downloads on first ever run)

        for start in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[start:start + EMBED_BATCH_SIZE]
            seg_ids = [row[0] for row in batch]
            texts = [row[1] for row in batch]

            # Encode the batch. normalize_embeddings=True makes vectors unit-
            # length so cosine distance (pgvector <=>) behaves well.
            vectors = model.encode(
                texts, normalize_embeddings=True, show_progress_bar=False
            )

            rows = [
                (uuid.uuid4(), seg_id, EMBEDDING_MODEL, EMBEDDING_VERSION,
                 _to_pgvector(vec))
                for seg_id, vec in zip(seg_ids, vectors)
            ]

            with conn.transaction():
                written = _write_embeddings(conn, rows)
            result.segments_embedded += written
            result.batches += 1

    _print_summary(result)
    return result


def _to_pgvector(vec) -> str:
    """Format a numeric vector as pgvector's text input: '[v1,v2,...]'."""
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def _write_embeddings(conn, rows) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO gold_embeddings
                (id, segment_id, embedding_model, embedding_version, embedding)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (segment_id, embedding_model, embedding_version)
            DO NOTHING;
            """,
            rows,
        )
        return cur.rowcount


def _print_summary(r: EmbedResult) -> None:
    scope = f"source '{r.source_id}'" if r.source_id else "all sources"
    print(
        f"\nEmbedding ({EMBEDDING_MODEL}) for {scope}:\n"
        f"  segments embedded : {r.segments_embedded}\n"
        f"  batches           : {r.batches}"
    )


def main(argv=None) -> int:
    import argparse
    from ..config import get_source_config, ConfigError

    p = argparse.ArgumentParser(
        description="Embed gold exception segments into gold_embeddings.",
    )
    p.add_argument("--source-id", default=None,
                   help="Limit to one source (validated against config). "
                        "Omit to embed all sources.")
    p.add_argument("--config", default=None,
                   help="Path to config file (default: config/config.yaml).")
    args = p.parse_args(argv)

    cfg = None
    if args.source_id is not None:
        # Validate the source id against config (clear error on a typo);
        # embedding itself only needs the id, but this keeps the CLI uniform
        # with the other steps and catches mistakes loudly.
        try:
            cfg = get_source_config(args.source_id, args.config)
        except ConfigError as exc:
            print(f"ERROR: {exc}")
            return 1

    embed_segments(cfg)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))