"""Per-field PII policy (ADR-015).

Applies a configurable, per-field, per-source policy to extracted log fields
before they are stored in silver. Two actions are implemented; a third
(reversible encryption) is reserved:

  * redact  — replace the value with a fixed marker. Irreversible. For secrets.
  * hmac    — keyed HMAC-SHA256 with a secret key. Deterministic (same input →
              same fingerprint, so grouping/joining on the value still works)
              but irreversible, and the secret key resists brute-forcing
              low-entropy values. For correlation fields (usernames, etc.).
              This is PSEUDONYMISATION, not anonymisation — under GDPR the
              hashed value is still personal data.
  * encrypt — RESERVED (AES, reversible). Not yet implemented; the dispatch
              point is marked so it can be added without restructuring.

Policy is FIELD-LEVEL: it acts on named, extracted fields. Detecting PII
embedded in free text (messages, stack traces) is a separate, harder problem
and is deliberately OUT OF SCOPE here (see ADR-015 notes); it would be
addressed per-source if a source that needs it is added.

Security posture: FAIL CLOSED. If a field is configured for `hmac` but the
secret key is not available, processing raises rather than writing unprotected
PII — a loud failure is safer than silently violating the policy (GDPR/PCI-DSS).

The HMAC key is read from the LOGLENS_PII_HMAC_KEY environment variable; it is
never stored in the repo or the database, and must be kept stable (a changed
key changes every fingerprint, breaking correlation across runs).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

HMAC_KEY_ENV_VAR = "LOGLENS_PII_HMAC_KEY"
REDACTION_MARKER = "[REDACTED]"

# Recognized policy actions.
ACTION_REDACT = "redact"
ACTION_HMAC = "hmac"
ACTION_ENCRYPT = "encrypt"   # reserved, not yet implemented
_KNOWN_ACTIONS = {ACTION_REDACT, ACTION_HMAC, ACTION_ENCRYPT}


class PIIPolicyError(Exception):
    """Raised when the PII policy cannot be applied safely (fail closed)."""


def _get_hmac_key() -> bytes:
    """Return the HMAC secret key from the environment, or raise (fail closed)."""
    key = os.environ.get(HMAC_KEY_ENV_VAR)
    if not key:
        raise PIIPolicyError(
            f"{HMAC_KEY_ENV_VAR} is not set, but a field is configured for "
            f"'hmac'. Refusing to proceed and write unprotected PII. Set the "
            f"key (e.g. in .env) and keep it stable across runs."
        )
    return key.encode("utf-8")


def _hmac_value(value: str, key: bytes) -> str:
    """Deterministic keyed HMAC-SHA256 fingerprint of a value (hex)."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def apply_pii_policy(fields: dict[str, Any], source_config: dict[str, Any]) -> dict[str, Any]:
    """Apply the source's per-field PII policy to a dict of extracted fields.

    source_config["pii_policy"] is an optional mapping of {field_name: action}.
    Fields not named in the policy pass through unchanged. Returns a new dict;
    the input is not mutated.

    Raises PIIPolicyError if a configured action is unknown, or if 'hmac' is
    configured but the secret key is unavailable (fail closed).
    """
    policy = (source_config or {}).get("pii_policy") or {}
    if not policy:
        return dict(fields)

    # Validate configured actions up front (clear error over silent mishandling).
    for fld, action in policy.items():
        if action not in _KNOWN_ACTIONS:
            raise PIIPolicyError(
                f"unknown PII action '{action}' for field '{fld}'. "
                f"Valid actions: {sorted(_KNOWN_ACTIONS)}."
            )

    # Load the HMAC key only if any field actually needs it (and fail closed).
    needs_hmac = any(a == ACTION_HMAC for a in policy.values())
    hmac_key = _get_hmac_key() if needs_hmac else None

    out: dict[str, Any] = {}
    for name, value in fields.items():
        action = policy.get(name)
        if action is None or value is None:
            out[name] = value
        elif action == ACTION_REDACT:
            out[name] = REDACTION_MARKER
        elif action == ACTION_HMAC:
            out[name] = _hmac_value(str(value), hmac_key)  # type: ignore[arg-type]
        elif action == ACTION_ENCRYPT:
            # Reserved: reversible AES encryption is not yet implemented. Fail
            # closed rather than store the value unprotected.
            raise PIIPolicyError(
                f"PII action 'encrypt' (field '{name}') is configured but not "
                f"yet implemented. Remove it or choose 'redact'/'hmac' for now."
            )
        else:  # defensive; validated above
            out[name] = value
    return out
