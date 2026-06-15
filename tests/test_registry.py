"""Smoke test: the Windows service parser registers and dispatches (ADR-004)."""

from loglens.parsers import get_parser, known_log_types


def test_windows_service_registered():
    assert "windows_service" in known_log_types()


def test_get_parser_returns_matching_log_type():
    parser = get_parser("windows_service")
    assert parser.log_type == "windows_service"


def test_unknown_log_type_raises():
    try:
        get_parser("does_not_exist")
    except KeyError as e:
        assert "no parser registered" in str(e)
    else:
        raise AssertionError("expected KeyError for unknown log_type")
