"""Windows service (.NET) log parser (ADR-004, ADR-005, ADR-006, ADR-007).

Parses one raw multi-line Windows service log entry into the structured fields
the silver layer stores. The extraction approach is the proven one from the
original proof-of-concept:

  * the header line carries the timestamp, level, and the namespace/message;
  * indented "  key : value" lines form the body;
  * an "Exception"/"Stack Trace" section is reassembled as one block.

This parser self-registers under the "windows_service" log_type (ADR-004).
It maps into the common silver shape (ADR-005): universal fields become
columns, everything else (body key-values) goes to extra_fields, and the PII
policy is applied via a single hook (currently pass-through; ADR-015).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .registry import register

PARSER_VERSION = "windows_service/1.0"

# Header: "M/D/YYYY H:MM:SS AM  Level: <namespace/message...>"
# Capture timestamp, level, and the remainder (namespace + message text).
_HEADER_RE = re.compile(
    r"^(?P<ts>\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM))\s+"
    r"(?P<level>\w+):\s*(?P<rest>.*)$"
)

# Body key/value line: at least two leading spaces, "key : value".
_KV_RE = re.compile(r"^\s{2,}([^:]+?)\s*:\s*(.*)$")

# A leading namespace token in the header remainder, e.g.
# "Contoso.Platform.Inventory.ServiceLayer.Tasks.SyncInventoryBalanceTask ..."
_NAMESPACE_RE = re.compile(r"^([A-Za-z_][\w.]*\.[\w.]+)")

# Windows native level -> normalized severity (ADR-006).
_SEVERITY_MAP = {
    "Critical": "FATAL",
    "Error": "ERROR",
    "Warning": "WARN",
    "Information": "INFO",
    "Verbose": "DEBUG",
    "Debug": "DEBUG",
}

# Body keys that indicate the start of an exception block. Once seen, all
# following lines are appended to the exception text.
_EXCEPTION_KEYS = {"Exception", "Exception Type", "Stack Trace"}

# Known body key/value fields that may appear AFTER an exception block. When one
# of these is seen, it ends the exception capture and resumes normal key/value
# handling — so trailing fields (e.g. PII like User/Country/Account Id) are not
# swallowed into exception_text. Compared case-insensitively.
_TRAILING_BODY_KEYS = {
    "user", "country", "account id", "duration", "status", "time taken",
    "worker type", "queue name", "queue depth", "affected count",
    "unique data sources", "deleted count", "modified count", "new count",
    "detail", "warning",
}


@dataclass
class ParsedEntry:
    """Structured result of parsing one raw entry, mapped to the silver shape.

    Fields the parser could not determine are left as None; the silver columns
    are nullable so a partially-parseable entry still lands (ELT tolerance).
    """
    event_time_local: datetime | None = None
    severity: str = "UNKNOWN"
    severity_raw: str | None = None
    logger: str | None = None
    message: str | None = None
    is_exception: bool = False
    exception_text: str | None = None
    extra_fields: dict[str, Any] = field(default_factory=dict)
    parser_version: str = PARSER_VERSION


def _normalize_severity(level_raw: str) -> str:
    return _SEVERITY_MAP.get(level_raw, "UNKNOWN")


@register
class WindowsServiceParser:
    """Parses Windows service entries into the common ParsedEntry shape."""

    log_type = "windows_service"

    def parse(self, raw_entry: str, source_config: dict[str, Any]) -> ParsedEntry:
        lines = raw_entry.splitlines()
        if not lines:
            return ParsedEntry()

        result = ParsedEntry()

        # --- header line ---
        header = lines[0]
        m = _HEADER_RE.match(header.strip())
        if m:
            result.event_time_local = self._parse_timestamp(m.group("ts"))
            result.severity_raw = m.group("level")
            result.severity = _normalize_severity(m.group("level"))
            rest = m.group("rest").strip()
            # split the remainder into logger (namespace) + message text
            ns = _NAMESPACE_RE.match(rest)
            if ns:
                result.logger = ns.group(1)
                result.message = rest[ns.end():].strip() or None
            else:
                result.message = rest or None
        else:
            # Unparseable header: keep the line as the message, stay tolerant.
            result.message = header.strip() or None

        # --- body: key/values + exception block ---
        # Body key/values (before any exception block) are promoted to extra.
        # Once the exception block starts, the remaining lines are captured
        # VERBATIM (original text, only right-stripped) so the exception's
        # natural structure is preserved for gold's structure-aware chunking.
        extra: dict[str, Any] = {}
        exception_parts: list[str] = []
        in_exception = False
        last_key: str | None = None

        for line in lines[1:]:
            if in_exception:
                # A known body field (e.g. trailing PII) ends the exception
                # block and resumes normal key/value handling, so such fields
                # are not swallowed into exception_text.
                kv = _KV_RE.match(line)
                if kv and kv.group(1).strip().lower() in _TRAILING_BODY_KEYS:
                    in_exception = False
                    key = kv.group(1).strip()
                    value = kv.group(2).strip()
                    extra[key] = value
                    last_key = key
                    continue
                # otherwise this line is part of the exception, captured verbatim
                exception_parts.append(line.rstrip())
                continue

            kv = _KV_RE.match(line)
            if kv:
                key = kv.group(1).strip()
                value = kv.group(2).strip()
                if key in _EXCEPTION_KEYS:
                    in_exception = True
                    exception_parts.append(line.rstrip())  # verbatim from the start
                    last_key = key
                else:
                    extra[key] = value
                    last_key = key
            elif last_key is not None and last_key in extra:
                # continuation of a normal key/value
                extra[last_key] = f"{extra[last_key]}\n{line.strip()}"

        if exception_parts:
            result.is_exception = True
            result.exception_text = "\n".join(exception_parts).strip()

        # PII policy hook (ADR-015): currently pass-through. Later this will
        # redact / HMAC / encrypt configured fields before they enter silver.
        result.extra_fields = _apply_pii_policy(extra, source_config)

        return result

    def _parse_timestamp(self, ts: str) -> datetime | None:
        """Parse 'M/D/YYYY H:MM:SS AM' into a naive local datetime."""
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M:%S %p"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None


def _apply_pii_policy(fields: dict[str, Any], source_config: dict[str, Any]) -> dict[str, Any]:
    """PII policy hook (ADR-015).

    Currently a pass-through: returns fields unchanged. This is the single place
    the per-field policy (redact / HMAC / encrypt) will be applied later, reading
    the policy from source_config['pii_policy']. Building the seam now keeps the
    later change localized to this function.
    """
    return fields


def to_utc(local_dt: datetime | None, tz_name: str) -> datetime | None:
    """Convert a naive local datetime to UTC using an IANA zone (ADR-007)."""
    if local_dt is None:
        return None
    local_aware = local_dt.replace(tzinfo=ZoneInfo(tz_name))
    return local_aware.astimezone(ZoneInfo("UTC"))