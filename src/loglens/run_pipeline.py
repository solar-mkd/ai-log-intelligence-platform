"""Pipeline orchestrator (ADR-014).

Runs the full pipeline for every source defined in the config file, in order:

    landing_bronze  ->  silver  ->  gold  ->  embed_gold

Behaviour (the agreed decisions):
  * ALL sources from config are processed (no per-source/per-step CLI options —
    individual steps remain runnable via their own modules when needed).
  * CONTINUE across sources: a failure in one source does not stop the others.
  * STOP a source's remaining steps once one of its steps fails (no point
    segmenting if silver failed, etc.) — then move on to the next source.
  * Failures are collected, printed in the end-of-run SUMMARY, and appended to
    an error-log file (logs/pipeline_errors.log). Alerting/notification is
    intentionally out of scope — a separate watchdog service would consume this
    log (ADR-014: orchestrator stays simple; concerns kept separate).
  * The orchestrator calls the steps' core functions directly (dict-driven),
    looping over get_source_config for each configured source.

PII note: silver is fail-closed (it raises if a source has a pii_policy but the
HMAC key is unavailable). That surfaces here as a step failure for that source:
the source aborts loudly and is logged, and other sources continue — never a
silent skip of PII protection (ADR-015).
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import list_sources, get_source_config, ConfigError
from .pipeline.landing_bronze import ingest_to_bronze
from .pipeline.silver import transform_to_silver
from .pipeline.gold import transform_to_gold
from .pipeline.embed_gold import embed_segments

ERROR_LOG_PATH = "logs/pipeline_errors.log"

# Steps run in this fixed order. Each is (name, callable taking source_config).
PIPELINE_STEPS = [
    ("landing_bronze", ingest_to_bronze),
    ("silver", transform_to_silver),
    ("gold", transform_to_gold),
    ("embed_gold", embed_segments),
]


@dataclass
class SourceOutcome:
    source_id: str
    steps_completed: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed_step is None


def _log_error(source_id: str, step: str, exc: Exception) -> None:
    """Append a failure record to the error log (a watchdog could consume this)."""
    Path(ERROR_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ts}\tsource={source_id}\tstep={step}\terror={exc!r}\n")
        f.write(traceback.format_exc())
        f.write("\n")


def run_source(source_id: str, config_path: str | None = None) -> SourceOutcome:
    """Run all steps for one source, stopping that source on the first failure."""
    outcome = SourceOutcome(source_id=source_id)
    try:
        source_config = get_source_config(source_id, config_path)
    except ConfigError as exc:
        outcome.failed_step = "config"
        outcome.error = str(exc)
        _log_error(source_id, "config", exc)
        return outcome

    for step_name, step_fn in PIPELINE_STEPS:
        try:
            step_fn(source_config)
            outcome.steps_completed.append(step_name)
        except Exception as exc:  # noqa: BLE001 — orchestrator records & continues
            outcome.failed_step = step_name
            outcome.error = repr(exc)
            _log_error(source_id, step_name, exc)
            break  # stop this source's remaining steps; caller moves to next
    return outcome


def run_all(config_path: str | None = None) -> list[SourceOutcome]:
    """Run the full pipeline for every configured source; continue across them."""
    try:
        sources = list_sources(config_path)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return []

    if not sources:
        print("No sources defined in config.")
        return []

    print(f"Running pipeline for {len(sources)} source(s): {', '.join(sources)}\n")
    outcomes = [run_source(sid, config_path) for sid in sources]
    _print_summary(outcomes)
    return outcomes


def _print_summary(outcomes: list[SourceOutcome]) -> None:
    print("\n" + "=" * 60)
    print("PIPELINE RUN SUMMARY")
    print("=" * 60)
    ok_count = sum(1 for o in outcomes if o.ok)
    for o in outcomes:
        if o.ok:
            print(f"  [OK]   {o.source_id}: {' -> '.join(o.steps_completed)}")
        else:
            done = " -> ".join(o.steps_completed) or "(none)"
            print(f"  [FAIL] {o.source_id}: completed [{done}], "
                  f"failed at '{o.failed_step}'")
            print(f"         {o.error}")
    print("-" * 60)
    print(f"  {ok_count}/{len(outcomes)} source(s) completed all steps.")
    if ok_count != len(outcomes):
        print(f"  Failures recorded in {ERROR_LOG_PATH}")
    print("=" * 60)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Run the full pipeline (all steps) for all configured sources.",
    )
    p.add_argument("--config", default=None,
                   help="Path to config file (default: config/config.yaml).")
    args = p.parse_args(argv)

    outcomes = run_all(args.config)
    # Non-zero exit if any source failed (useful for schedulers/CI).
    return 0 if outcomes and all(o.ok for o in outcomes) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
