"""Storage adapters (ADR-013, ADR-014).

The storage boundary keeps core logic platform-agnostic: the pipeline talks to
an adapter interface, not to a specific database. PostgreSQL + pgvector is the
reference implementation; the same boundary allows a SQLite demo or a future
port to other platforms.
"""
