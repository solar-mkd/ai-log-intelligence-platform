"""Tests for the Apache access-log parser (ADR-004, ADR-006). No database."""

from datetime import datetime

from loglens.parsers.apache_access import ApacheAccessParser
from loglens.parsers.contract import SEVERITY_LEVELS

P = ApacheAccessParser()

LINE_500 = ('192.168.1.10 - - [03/May/2026:14:23:01 +1000] '
            '"GET /api/orders HTTP/1.1" 500 1234 "-" "curl/8.0"')
LINE_200 = ('10.0.0.5 - alice [03/May/2026:14:23:02 +1000] '
            '"POST /api/checkout HTTP/1.1" 200 89 "-" "Mozilla/5.0"')
LINE_404 = ('10.0.0.6 - - [04/May/2026:09:15:00 +1000] '
            '"GET /missing HTTP/1.1" 404 0 "-" "bot/1.0"')


def test_parses_core_fields():
    p = P.parse(LINE_500, {})
    assert p.severity_raw == "500"
    assert p.severity == "ERROR"
    assert p.is_exception is True
    assert p.message == "GET /api/orders -> 500"
    assert p.logger == "apache.access"
    assert p.extra_fields["method"] == "GET"
    assert p.extra_fields["path"] == "/api/orders"
    assert p.extra_fields["status"] == 500


def test_severity_mapping_in_common_set():
    for line, expected in [(LINE_500, "ERROR"), (LINE_404, "WARN"), (LINE_200, "INFO")]:
        p = P.parse(line, {})
        assert p.severity == expected
        assert p.severity in SEVERITY_LEVELS   # conforms to the shared vocabulary


def test_only_5xx_is_exception():
    assert P.parse(LINE_500, {}).is_exception is True
    assert P.parse(LINE_404, {}).is_exception is False
    assert P.parse(LINE_200, {}).is_exception is False


def test_segment_is_http_signature():
    p = P.parse(LINE_500, {})
    segs = P.segment_exception(p.exception_text)
    assert len(segs) == 1
    assert segs[0].segment_index == 0
    assert segs[0].segment_text == "HTTP 500 GET /api/orders"


def test_to_utc_uses_embedded_offset():
    p = P.parse(LINE_500, {})
    utc = P.to_utc(p.event_time_local, "Australia/Brisbane")
    # 14:23:01 +1000 -> 04:23:01 UTC
    assert utc.hour == 4 and utc.minute == 23 and utc.second == 1
    assert utc.tzinfo is not None


def test_unparseable_line_is_tolerated():
    p = P.parse("this is not an apache line", {})
    assert p.severity in SEVERITY_LEVELS      # valid severity (UNKNOWN)
    assert p.is_exception is False
    assert "unparsed" in p.extra_fields


def test_pii_policy_applied_to_fields():
    # client_ip configured for hmac -> value changes (needs key via monkeypatch
    # in a real run; here we just confirm the hook is wired and redact works).
    cfg = {"pii_policy": {"client_ip": "redact"}}
    p = P.parse(LINE_500, cfg)
    assert p.extra_fields["client_ip"] == "[REDACTED]"
