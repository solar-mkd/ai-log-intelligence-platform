"""PostgreSQL + pgvector adapter (ADR-013).

Relational data and vectors live in one store. Similarity search uses pgvector
distance operators (cosine ``<=>``) with an HNSW index; retrieval filters on
metadata columns before ranking by similarity (hybrid retrieval).
"""

# TODO: implement connection handling, DDL, upserts (idempotent merge), and
# the pgvector similarity query.
