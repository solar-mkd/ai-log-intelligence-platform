"""Silver step (ADR-005, ADR-006, ADR-007, ADR-015).

Takes parsed entries and writes the normalized silver rows: universal columns
plus extra_fields JSON, UTC + local time, normalized + raw severity. Applies
the per-field PII policy (redact / HMAC / encrypt) at this boundary.
"""

# TODO: implement silver normalization + PII policy application.
