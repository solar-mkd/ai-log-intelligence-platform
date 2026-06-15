"""Parser package.

Importing this package registers every bundled parser via its module-level
``@register`` decorator. Add a new parser by creating a module here and
importing it below; no other code changes (ADR-004).
"""

from . import windows_service  # noqa: F401  (import triggers registration)

from .contract import ParsedEntry, Parser, SEVERITY_LEVELS
from .registry import get_parser, known_log_types, register

__all__ = [
    "ParsedEntry",
    "Parser",
    "SEVERITY_LEVELS",
    "get_parser",
    "known_log_types",
    "register",
]
