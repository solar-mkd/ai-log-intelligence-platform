"""Source configuration loading (ADR-004, ADR-014, ADR-015).

Source definitions live in a YAML config file (default config/config.yaml,
git-ignored; config/config.example.yaml shows the shape). Each pipeline step
looks up its source's full settings by source_id, so the CLI only needs
--source-id rather than a growing list of per-setting flags.

Shape:

    sources:
      windows_service_1:
        log_type: windows_service
        location: data/raw/windows_service_1
        timezone: Australia/Brisbane
        header_pattern: "..."          # optional; parser default used if omitted
        earliest_date: "2026-01-01"    # optional; first-import cutoff
        pii_policy:                    # optional; per-field PII actions
          User: hmac
          Country: hmac
          Account Id: redact

The same log_type can appear under several sources with different settings —
e.g. a production source with a pii_policy and a test source without one.

The loaded per-source dict is exactly what the pipeline functions already
expect (ingest_to_bronze, transform_to_silver, ...): source_id is injected into
it so the dict is self-contained.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "config/config.yaml"
CONFIG_PATH_ENV_VAR = "LOGLENS_CONFIG"


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or a source is not found."""


def _config_path(path: str | None) -> Path:
    """Resolve the config path: explicit arg > env var > default."""
    chosen = path or os.environ.get(CONFIG_PATH_ENV_VAR) or DEFAULT_CONFIG_PATH
    return Path(chosen)


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load and return the whole config document."""
    cfg_path = _config_path(path)
    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found: {cfg_path}. Copy config/config.example.yaml "
            f"to {DEFAULT_CONFIG_PATH} and edit it, or set {CONFIG_PATH_ENV_VAR}."
        )
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path} must contain a mapping at the top level.")
    return data


def list_sources(path: str | None = None) -> list[str]:
    """Return the configured source ids."""
    data = load_config(path)
    sources = data.get("sources") or {}
    return sorted(sources.keys())


def get_source_config(source_id: str, path: str | None = None) -> dict[str, Any]:
    """Return one source's settings as a self-contained config dict.

    The returned dict includes 'source_id' (injected) plus everything defined
    for that source. Raises ConfigError with the available ids if not found.
    """
    data = load_config(path)
    sources = data.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ConfigError(
            "no 'sources' section found in config. Define sources under a "
            "top-level 'sources:' mapping (see config.example.yaml)."
        )
    if source_id not in sources:
        available = ", ".join(sorted(sources.keys())) or "(none)"
        raise ConfigError(
            f"source '{source_id}' not found in config. Available: {available}."
        )
    source_cfg = dict(sources[source_id] or {})
    source_cfg["source_id"] = source_id  # make the dict self-contained
    # log_type is required for parser dispatch.
    if "log_type" not in source_cfg:
        raise ConfigError(f"source '{source_id}' is missing required 'log_type'.")
    return source_cfg
