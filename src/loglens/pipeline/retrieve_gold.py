"""Hybrid retrieval over gold exception segments (ADR-011, ADR-013).

Given a natural-language query and optional filters, returns the most relevant
exception segments by combining:
  * structured FILTERING on the carried metadata columns (time window, source,
    severity, log_type) — each filter is ignored when its argument is None; and
  * semantic RANKING by vector similarity (pgvector cosine distance, <=>), using
    an embedding of the query produced by the SAME model as the stored vectors.

Two modes:
  * distinct=True  (default): group by signature, returning each distinct
    signature once with its occurrence count, best (minimum) distance, and the
    earliest/latest time it occurred. Answers "what kinds of errors match, and
    how often / over what span".
  * distinct=False: return individual occurrences, each with its own time.
    Answers "every matching occurrence".

Filter-then-rank: filters narrow the candidate set, similarity orders it.

This is the retrieval half of RAG; the LLM step builds on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..storage.postgres import connect
from .embed_gold import EMBEDDING_MODEL, EMBEDDING_VERSION, _get_model, _to_pgvector


@dataclass
class RetrievedSegment:
    segment_text: str
    distance: float
    # distinct=True fields:
    occurrences: int | None = None
    first_seen_utc: datetime | None = None
    last_seen_utc: datetime | None = None
    # distinct=False fields:
    silver_entry_id: Any = None
    source_id: str | None = None
    severity: str | None = None
    logger: str | None = None
    event_time_utc: datetime | None = None


def _embed_query(query_text: str) -> str:
    """Embed the query with the same model as the stored vectors; return it in
    pgvector text form."""
    model = _get_model()
    vec = model.encode([query_text], normalize_embeddings=True, show_progress_bar=False)[0]
    return _to_pgvector(vec)


def retrieve(
    query_text: str,
    *,
    from_utc: datetime | None = None,
    to_utc: datetime | None = None,
    source_id: str | None = None,
    severity: str | None = None,
    log_type: str | None = None,
    top_k: int = 10,
    distinct: bool = True,
    dsn: str | None = None,
) -> list[RetrievedSegment]:
    """Run hybrid retrieval. Returns up to top_k RetrievedSegment, nearest first."""
    qvec = _embed_query(query_text)

    # Shared, NULL-aware filter block. A NULL argument disables that filter.
    # Each optional filter is ignored when its argument is NULL. The explicit
    # ::type casts are required so PostgreSQL can determine the parameter type
    # even when the value is NULL (otherwise: "could not determine data type").
    filters = """
        AND (%(source_id)s::text IS NULL OR g.source_id = %(source_id)s::text)
        AND (%(severity)s::text  IS NULL OR g.severity  = %(severity)s::text)
        AND (%(log_type)s::text  IS NULL OR g.log_type  = %(log_type)s::text)
        AND (%(from_utc)s::timestamptz IS NULL OR g.event_time_utc >= %(from_utc)s::timestamptz)
        AND (%(to_utc)s::timestamptz   IS NULL OR g.event_time_utc <= %(to_utc)s::timestamptz)
    """

    params: dict[str, Any] = {
        "qvec": qvec, "source_id": source_id, "severity": severity,
        "log_type": log_type, "from_utc": from_utc, "to_utc": to_utc,
        "top_k": top_k,
    }

    if distinct:
        # Group by signature; the group's distance is the MIN distance (its
        # closest occurrence represents the group). Also return count + span.
        sql = f"""
            SELECT g.segment_text,
                   MIN(e.embedding <=> %(qvec)s::vector) AS distance,
                   COUNT(*)                              AS occurrences,
                   MIN(g.event_time_utc)                 AS first_seen,
                   MAX(g.event_time_utc)                 AS last_seen
            FROM gold_embeddings e
            JOIN gold_exception_segments g ON g.id = e.segment_id
            WHERE e.embedding_model = %(model)s
              AND e.embedding_version = %(version)s
              {filters}
            GROUP BY g.segment_text
            ORDER BY distance ASC
            LIMIT %(top_k)s;
        """
        params["model"] = EMBEDDING_MODEL
        params["version"] = EMBEDDING_VERSION
        with connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [
                    RetrievedSegment(
                        segment_text=r[0], distance=float(r[1]),
                        occurrences=r[2], first_seen_utc=r[3], last_seen_utc=r[4],
                    )
                    for r in cur.fetchall()
                ]
    else:
        sql = f"""
            SELECT g.segment_text,
                   (e.embedding <=> %(qvec)s::vector) AS distance,
                   g.silver_entry_id, g.source_id, g.severity, g.logger,
                   g.event_time_utc
            FROM gold_embeddings e
            JOIN gold_exception_segments g ON g.id = e.segment_id
            WHERE e.embedding_model = %(model)s
              AND e.embedding_version = %(version)s
              {filters}
            ORDER BY distance ASC
            LIMIT %(top_k)s;
        """
        params["model"] = EMBEDDING_MODEL
        params["version"] = EMBEDDING_VERSION
        with connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [
                    RetrievedSegment(
                        segment_text=r[0], distance=float(r[1]),
                        silver_entry_id=r[2], source_id=r[3], severity=r[4],
                        logger=r[5], event_time_utc=r[6],
                    )
                    for r in cur.fetchall()
                ]


def _print_results(results: list[RetrievedSegment], distinct: bool) -> None:
    if not results:
        print("No matching segments.")
        return
    print(f"\nTop {len(results)} results:\n")
    for i, r in enumerate(results, 1):
        sim = 1.0 - r.distance  # cosine similarity from cosine distance
        if distinct:
            print(f"{i}. [{sim:.3f}] ({r.occurrences}x, "
                  f"{r.first_seen_utc:%Y-%m-%d} .. {r.last_seen_utc:%Y-%m-%d})")
            print(f"    {r.segment_text}")
        else:
            print(f"{i}. [{sim:.3f}] {r.event_time_utc:%Y-%m-%d %H:%M} "
                  f"{r.source_id}/{r.severity}")
            print(f"    {r.segment_text}")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Hybrid retrieval over gold exception segments.")
    p.add_argument("query", help="Natural-language query text.")
    p.add_argument("--source-id", default=None)
    p.add_argument("--severity", default=None)
    p.add_argument("--log-type", default=None)
    p.add_argument("--from", dest="from_utc", default=None, help="ISO datetime lower bound.")
    p.add_argument("--to", dest="to_utc", default=None, help="ISO datetime upper bound.")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--all-occurrences", action="store_true",
                   help="Return individual occurrences instead of distinct signatures.")
    args = p.parse_args(argv)

    results = retrieve(
        args.query,
        from_utc=_parse_dt(args.from_utc), to_utc=_parse_dt(args.to_utc),
        source_id=args.source_id, severity=args.severity, log_type=args.log_type,
        top_k=args.top_k, distinct=not args.all_occurrences,
    )
    _print_results(results, distinct=not args.all_occurrences)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
