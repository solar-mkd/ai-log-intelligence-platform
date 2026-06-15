"""Parser registry (ADR-004).

Parsers self-register here, keyed by their ``log_type``. The pipeline asks the
registry for the parser matching a source's ``log_type`` and never contains a
branching ``if log_type == ...`` chain. Adding a new log type is therefore a
matter of writing a new parser module and registering it — no existing code
changes.

Usage in a parser module::

    from .registry import register
    from .contract import Parser

    @register
    class WindowsServiceParser:
        log_type = "windows_service"
        def parse(self, raw_entry, source_config): ...
"""

from __future__ import annotations

from typing import Type

from .contract import Parser


_REGISTRY: dict[str, Parser] = {}


def register(parser_cls: Type) -> Type:
    """Class decorator that registers a parser by its ``log_type``.

    Returns the class unchanged so it can be used as a decorator.
    """
    instance = parser_cls()
    log_type = getattr(instance, "log_type", None)
    if not log_type:
        raise ValueError(f"{parser_cls.__name__} must define a non-empty 'log_type'")
    if log_type in _REGISTRY:
        raise ValueError(
            f"duplicate parser for log_type {log_type!r}: "
            f"{type(_REGISTRY[log_type]).__name__} and {parser_cls.__name__}"
        )
    _REGISTRY[log_type] = instance
    return parser_cls


def get_parser(log_type: str) -> Parser:
    """Return the registered parser for ``log_type``.

    Raises a clear error listing known types if none matches — this is the
    error a user sees if a source config names a log type with no parser.
    """
    try:
        return _REGISTRY[log_type]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(
            f"no parser registered for log_type {log_type!r}. Known types: {known}"
        ) from None


def known_log_types() -> list[str]:
    """List all registered log types (useful for diagnostics and tests)."""
    return sorted(_REGISTRY)
