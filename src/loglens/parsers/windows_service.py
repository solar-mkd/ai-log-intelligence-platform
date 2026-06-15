"""Windows service log parser (the first vertical slice).

This is the first concrete implementation of the parser contract (ADR-004).
It self-registers under the ``windows_service`` log type. The structural
extraction logic here is a scaffold — the working extraction you already have
drops into ``parse`` and ``_segment_exception``.

Responsibilities specific to this parser:
- Promote the fields most often searched into the normalized core; everything
  else goes into ``extra_fields`` (ADR-005).
- Map Windows-native severity onto the common vocabulary (ADR-006).
- Reassembly of multi-line exceptions happens upstream (one entry arrives
  whole); segmentation into structure-aware chunks is a gold-layer concern
  (ADR-009, ADR-010) and lives in the gold step, not here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contract import ParsedEntry
from .registry import register


# Map Windows event levels onto the common severity vocabulary (ADR-006).
_SEVERITY_MAP = {
    "Critical": "FATAL",
    "Error": "ERROR",
    "Warning": "WARN",
    "Information": "INFO",
    "Verbose": "DEBUG",
}


@register
class WindowsServiceParser:
    """Parses Windows service log entries into the common ``ParsedEntry`` shape."""

    log_type = "windows_service"

    def parse(self, raw_entry: str, source_config: dict[str, Any]) -> ParsedEntry:
        source_id = source_config["source_id"]
        source_file = source_config.get("_current_file", "")
        tz_name = source_config["timezone"]            # IANA zone (ADR-007)
        tz = ZoneInfo(tz_name)

        # --- entry hash for idempotency (ADR-003) ---
        entry_hash = hashlib.sha256(raw_entry.encode("utf-8")).hexdigest()

        # --- TODO: real structural extraction (your existing logic) ---
        # The block below is a placeholder shape. Replace with the working
        # field extraction: pull the timestamp, level, message, and the
        # important key/value pairs; put the rest in extra_fields.
        local_naive = self._extract_timestamp(raw_entry)        # naive local dt
        event_time_local = local_naive.replace(tzinfo=tz)
        event_time_utc = event_time_local.astimezone(ZoneInfo("UTC"))

        severity_raw = self._extract_level(raw_entry)
        severity = _SEVERITY_MAP.get(severity_raw, "UNKNOWN")

        message = self._extract_message(raw_entry)
        is_exception, exception_text = self._extract_exception(raw_entry)

        extra_fields = self._extract_extra(raw_entry)           # everything else

        return ParsedEntry(
            entry_hash=entry_hash,
            source_id=source_id,
            log_type=self.log_type,
            source_file=source_file,
            event_time_utc=event_time_utc,
            event_time_local=event_time_local,
            source_timezone=tz_name,
            severity=severity,
            severity_raw=severity_raw,
            message=message,
            is_exception=is_exception,
            exception_text=exception_text,
            extra_fields=extra_fields,
        )

    # --- extraction helpers (stubs — drop your working logic in) ------------

    def _extract_timestamp(self, raw_entry: str) -> datetime:
        # TODO: parse the real timestamp from the entry.
        raise NotImplementedError("plug in timestamp extraction")

    def _extract_level(self, raw_entry: str) -> str:
        # TODO: return the Windows-native level string, e.g. "Error".
        raise NotImplementedError("plug in level extraction")

    def _extract_message(self, raw_entry: str) -> str:
        # TODO: return the primary human-readable message.
        raise NotImplementedError("plug in message extraction")

    def _extract_exception(self, raw_entry: str) -> tuple[bool, str | None]:
        # TODO: detect whether this entry is an exception and return the full
        # reassembled exception text. Segmentation happens later, in gold.
        return (False, None)

    def _extract_extra(self, raw_entry: str) -> dict[str, Any]:
        # TODO: return all parsed-but-not-promoted key/value pairs (ADR-005).
        return {}
