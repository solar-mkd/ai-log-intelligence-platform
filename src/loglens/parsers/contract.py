"""Parser contract (ADR-004).

Every log-type parser implements the same contract: given a raw log entry and
its source configuration, it returns a normalized ``ParsedEntry``. This is what
makes the platform extensible — adding a new log type means adding a module that
satisfies this contract and self-registers, with no change to the dispatcher,
the pipeline, or existing parsers.

Design notes:
- Timestamps are normalized to UTC using the source's IANA time zone (ADR-007);
  the original local time and the zone are retained for audit.
- Severity is normalized to a common set while the raw value is kept (ADR-006).
- Fields the parser does not promote to columns go into ``extra_fields`` so
  nothing is lost and the schema can evolve (ADR-005).
- The PII policy (ADR-015) is applied downstream at silver ingestion, not here;
  the parser's job is structural extraction only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


# A small, common severity vocabulary. Each parser maps its source's native
# levels onto these; the raw value is preserved separately.
SEVERITY_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN")


@dataclass
class ParsedEntry:
    """The normalized result of parsing one raw log entry.

    This is the single shape every parser must produce, regardless of log type.
    It maps directly onto the silver schema (ADR-005, ADR-006, ADR-007).
    """

    # --- identity / lineage (ADR-003) ---
    entry_hash: str                      # hash of the raw entry; dedup/merge key
    source_id: str                       # which configured source this came from
    log_type: str                        # selects the parser; recorded for lineage
    source_file: str                     # originating file name/path

    # --- normalized core (every log type provides these) ---
    event_time_utc: datetime             # normalized to UTC (ADR-007)
    event_time_local: datetime           # original local time as recorded
    source_timezone: str                 # IANA zone used for conversion
    severity: str                        # normalized; one of SEVERITY_LEVELS
    severity_raw: str                    # source's original severity string
    message: str                         # primary human-readable message

    # --- exception handling (ADR-009) ---
    is_exception: bool = False           # cheap flag so gold can pull exceptions
    exception_text: str | None = None    # full reassembled exception, if any

    # --- flexibility hedge (ADR-005) ---
    # Everything parsed but not promoted to a column. Nothing is discarded.
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(
                f"severity {self.severity!r} not in {SEVERITY_LEVELS}; "
                "map the source's native level onto the common set"
            )


class Parser(Protocol):
    """Structural contract every parser satisfies.

    Implemented as a ``Protocol`` so parsers don't need to inherit from a base
    class — they just need a matching ``log_type`` attribute and ``parse``
    method. This keeps each parser self-contained.
    """

    #: The log type this parser handles, e.g. "windows_service". Used by the
    #: registry to dispatch (ADR-004).
    log_type: str

    def parse(self, raw_entry: str, source_config: dict[str, Any]) -> ParsedEntry:
        """Parse one raw log entry into a normalized ``ParsedEntry``.

        Args:
            raw_entry: the raw text of a single log entry (already split from
                the file by the ingestion step; multi-line entries arrive whole).
            source_config: the source's config dict (timezone, location, etc.),
                used for UTC normalization and provenance.

        Returns:
            A populated ``ParsedEntry``.
        """
        ...
