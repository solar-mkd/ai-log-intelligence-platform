"""Tests for the per-field PII policy (ADR-015). No database required."""

import os
import pytest

from loglens.pii import (
    apply_pii_policy, PIIPolicyError, HMAC_KEY_ENV_VAR, REDACTION_MARKER,
)

FIELDS = {"User": "svc_user_012", "Country": "MK", "Account Id": "ACC-1", "Other": "keep"}


def _set_key(monkeypatch):
    monkeypatch.setenv(HMAC_KEY_ENV_VAR, "test-secret-salt")


def test_no_policy_passthrough():
    assert apply_pii_policy(FIELDS, {}) == FIELDS


def test_redact(monkeypatch):
    _set_key(monkeypatch)
    out = apply_pii_policy(FIELDS, {"pii_policy": {"Account Id": "redact"}})
    assert out["Account Id"] == REDACTION_MARKER
    assert out["Other"] == "keep"


def test_hmac_deterministic(monkeypatch):
    _set_key(monkeypatch)
    cfg = {"pii_policy": {"User": "hmac"}}
    a = apply_pii_policy(FIELDS, cfg)
    b = apply_pii_policy(FIELDS, cfg)
    assert a["User"] == b["User"]          # same input -> same fingerprint
    assert a["User"] != "svc_user_012"     # not the original
    c = apply_pii_policy({**FIELDS, "User": "other"}, cfg)
    assert c["User"] != a["User"]          # different input -> different


def test_hmac_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv(HMAC_KEY_ENV_VAR, raising=False)
    with pytest.raises(PIIPolicyError):
        apply_pii_policy(FIELDS, {"pii_policy": {"User": "hmac"}})


def test_unknown_action_rejected(monkeypatch):
    _set_key(monkeypatch)
    with pytest.raises(PIIPolicyError):
        apply_pii_policy(FIELDS, {"pii_policy": {"User": "scramble"}})


def test_encrypt_not_implemented_fails_closed(monkeypatch):
    _set_key(monkeypatch)
    with pytest.raises(PIIPolicyError):
        apply_pii_policy(FIELDS, {"pii_policy": {"User": "encrypt"}})


def test_none_value_passes(monkeypatch):
    _set_key(monkeypatch)
    out = apply_pii_policy({"User": None}, {"pii_policy": {"User": "hmac"}})
    assert out["User"] is None
