"""Apache access-log parser (ADR-004, ADR-006, ADR-007, ADR-019).

Demonstrates the pluggable parser contract with a log type structurally unlike
Windows service logs: Apache access logs are SINGLE-LINE, structured request
records with no stack traces and no inner-exception chains. This parser shows
that a fundamentally different shape flows through the same pipeline by defining
its OWN notion of parsing and of an "exception" — with no change to the
dispatcher, silver, gold orchestrator, or RAG layer.

Format (Combined Log Format, the common Apache/nginx default):

    192.168.1.10 - - [03/May/2026:14:23:01 +1000] "GET /api/orders HTTP/1.1" 500 1234 "-" "curl/8.0"

Mapping to the common silver shape:
  * event time   <- the bracketed timestamp (parsed with its offset, then UTC).
  * severity     <- derived from the HTTP status code (ADR-006):
                      5xx -> ERROR, 4xx -> WARN, else INFO.
  * message      <- a compact "METHOD path -> status" summary.
  * logger       <- "apache.access" (no logger concept in the format).
  * is_exception <- True for 5xx (a server error is Apache's "exception").
  * exception_text <- for 5xx, the request line + status (so gold can segment).
  * extra_fields <- client IP, method, path, protocol, status, bytes, referer,
                     user agent (everything parsed, retained per ADR-005).

Segmentation (segment_exception, ADR-019): the recurring SIGNATURE of an Apache
server error is "HTTP <status> <METHOD> <path>" — the unit that recurs and is
worth embedding/correlating. There is no inner-exception chain, so a 5xx yields
exactly one segment. This is the Apache analog of the .NET exception signature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .registry import register
from ..pii import apply_pii_policy

PARSER_VERSION = "apache_access/1.0"

# Combined Log Format. Groups: ip, ident, user, time, request, status, bytes,
# referer, agent. ident/user/referer/agent are optional-ish in practice.
_LINE_RE = re.compile(
    r'(?P<ip>\S+) \S+ (?P<user>\S+) '
    r'\[(?P<time>[^\]]+)\] '
    r'"(?P<request>[^"]*)" '
    r'(?P<status>\d{3}) (?P<bytes>\S+)'
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?'
)

# Apache time format: 03/May/2026:14:23:01 +1000
_APACHE_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"


@dataclass
class ParsedEntry:
    event_time_local: datetime | None = None
    severity: str = "UNKNOWN"
    severity_raw: str | None = None
    message: str | None = None
    logger: str | None = None
    is_exception: bool = False
    exception_text: str | None = None
    parser_version: str = PARSER_VERSION
    extra_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExceptionSegment:
    segment_index: int
    segment_type: str
    segment_text: str


def _severity_from_status(status: int) -> str:
    """Map HTTP status to the common severity vocabulary (SEVERITY_LEVELS):
    5xx -> ERROR, 4xx -> WARN, else INFO. Note 'WARN' (not 'WARNING') to match
    the platform's shared severity set."""
    if status >= 500:
        return "ERROR"
    if status >= 400:
        return "WARN"
    return "INFO"


def _split_request(request: str) -> tuple[str, str, str]:
    """'GET /api/orders HTTP/1.1' -> (method, path, protocol). Tolerant of junk."""
    parts = request.split()
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 1:
        return parts[0], "", ""
    return "", request, ""


@register
class ApacheAccessParser:
    log_type = "apache_access"

    def parse(self, raw_entry: str, source_config: dict[str, Any]) -> ParsedEntry:
        result = ParsedEntry()
        line = raw_entry.strip()
        m = _LINE_RE.match(line)
        if not m:
            # Unparseable line: keep it, mark nothing as an exception. ELT
            # tolerance (ADR-001) — never drop data.
            result.message = line[:500]
            result.severity = "UNKNOWN"
            result.logger = "apache.access"
            result.extra_fields = _apply_pii_policy({"unparsed": line}, source_config)
            return result

        ip = m.group("ip")
        user = m.group("user")
        time_str = m.group("time")
        request = m.group("request")
        status = int(m.group("status"))
        size = m.group("bytes")
        referer = m.group("referer")
        agent = m.group("agent")

        method, path, protocol = _split_request(request)

        try:
            result.event_time_local = datetime.strptime(time_str, _APACHE_TIME_FMT)
        except ValueError:
            result.event_time_local = None

        result.severity = _severity_from_status(status)
        result.severity_raw = str(status)
        result.message = f"{method} {path} -> {status}".strip()
        result.logger = "apache.access"
        result.is_exception = status >= 500
        if result.is_exception:
            # Text gold will segment into the signature.
            result.exception_text = f"HTTP {status} {method} {path}".strip()

        fields: dict[str, Any] = {
            "client_ip": ip,
            "remote_user": user,
            "method": method,
            "path": path,
            "protocol": protocol,
            "status": status,
            "bytes": None if size in ("-", "") else size,
            "referer": referer,
            "user_agent": agent,
        }
        result.extra_fields = _apply_pii_policy(fields, source_config)
        return result

    def segment_exception(self, exception_text: str) -> "list[ExceptionSegment]":
        """An Apache server error has one signature: 'HTTP <status> <METHOD> <path>'.
        No inner-exception chain, so exactly one segment (index 0)."""
        if not exception_text:
            return []
        text = exception_text.strip()
        return [ExceptionSegment(0, "signature", text)]

    def to_utc(self, local_dt: datetime | None, tz_name: str) -> datetime | None:
        """Convert to UTC (ADR-007). Each parser owns its time handling. Apache
        timestamps already carry an offset, so if the datetime is tz-aware we use
        that offset directly; otherwise we attach the source's IANA zone."""
        if local_dt is None:
            return None
        if local_dt.tzinfo is not None:
            return local_dt.astimezone(ZoneInfo("UTC"))
        return local_dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC"))


def _apply_pii_policy(fields: dict[str, Any], source_config: dict[str, Any]) -> dict[str, Any]:
    """PII policy hook (ADR-015). Apache access logs can carry PII too — client
    IP and remote_user are the obvious candidates — so the same per-field policy
    applies here, configured per source."""
    return apply_pii_policy(fields, source_config)
