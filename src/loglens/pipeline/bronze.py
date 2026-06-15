"""Bronze step (ADR-001, ADR-002, ADR-003).

Lands raw entries (one row each), records file/entry hashes for idempotency,
maintains the processed-logs control table, and moves completed files from
landing to archive. Stateless and re-runnable.
"""

# TODO: implement bronze landing/archive + control table.
