"""Hybrid retrieval (ADR-011, ADR-013).

Filter candidate segments by metadata (time window, source, severity), then
rank by vector similarity, take top-K, and hand them to the local LLM. Only the
segment text was embedded; metadata stays as filterable columns. The query is
embedded with the SAME pinned model used for the corpus (ADR-012).
"""

# TODO: implement embed-query -> filtered ANN search -> top-K -> LLM prompt.
