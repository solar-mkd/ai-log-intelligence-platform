"""LogLens entry point.

Config-driven: reads sources from config, and for each enabled source dispatches
to the registered parser by ``log_type`` (no branching here — ADR-004), then runs
the bronze -> silver -> gold steps. Orchestrator-agnostic: this can be invoked by
cron, an Azure DevOps pipeline, or any scheduler (ADR-014).
"""

from __future__ import annotations

import sys

from .parsers import get_parser, known_log_types


def run(config: dict) -> None:
    """Process every enabled source described in ``config``."""
    for source in config.get("sources", []):
        if not source.get("enabled", False):
            continue
        log_type = source["log_type"]
        parser = get_parser(log_type)  # raises clearly if no parser registered
        # TODO: read new/modified files for this source, then for each raw entry:
        #   parsed = parser.parse(raw_entry, source)
        #   bronze.land(parsed) -> silver.write(parsed) -> gold.build(...)
        _ = parser  # placeholder until the steps are wired


def main(argv: list[str] | None = None) -> int:
    # TODO: load config (config/config.yaml), then call run(config).
    print("LogLens scaffold. Registered parsers:", ", ".join(known_log_types()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
