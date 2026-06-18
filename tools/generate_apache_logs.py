"""Synthetic Apache access-log generator (Combined Log Format).

Produces realistic-looking Apache access logs for the LogLens demo — a log type
structurally unlike the Windows service logs, to exercise the pluggable parser
and the cross-system temporal correlation.

Design for the demo:
  * Date range defaults to the same window as the sample Windows data
    (2026-04-30 .. 2026-05-10), so the two systems OVERLAP in time and the
    "what happened in other systems around that time?" query returns results
    from both. Override with --from-date / --to-date.
  * Most requests are normal 2xx/3xx. A handful of INCIDENT WINDOWS produce
    bursts of 5xx errors (the Apache analog of an exception). These bursts are
    biased toward the early part of the range, the same neighbourhood as the
    Windows incident bursts, so cross-system queries land on windows where BOTH
    systems show trouble (Option A — overlapping, not a shared schedule).
  * One file per day: access-YYYY-MM-DD.log.
  * Domain-neutral paths and hosts (no real data).

Usage:
    python tools/generate_apache_logs.py --output_dir data/raw/apache_1
    python tools/generate_apache_logs.py --output_dir data/raw/apache_2 \
        --from-date 2026-04-30 --to-date 2026-05-10 --seed 7
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import datetime, timedelta, date

# Domain-neutral request paths. A few are "hot" endpoints that participate in
# incidents (the backend they hit is what fails).
_NORMAL_PATHS = [
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/static/app.css"),
    ("GET", "/static/app.js"),
    ("GET", "/api/catalog"),
    ("GET", "/api/products"),
    ("POST", "/api/login"),
    ("GET", "/api/profile"),
    ("GET", "/docs"),
]
# Endpoints whose backend struggles during incidents (these go 5xx in bursts).
_INCIDENT_PATHS = [
    ("GET", "/api/orders"),
    ("POST", "/api/checkout"),
    ("GET", "/api/inventory"),
    ("POST", "/api/payment"),
]
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "curl/8.4.0",
    "python-requests/2.31.0",
    "Mozilla/5.0 (X11; Linux x86_64)",
]
_SERVER_ERRORS = [500, 502, 503, 504]
_CLIENT_ERRORS = [400, 401, 403, 404]


def _rand_ip(rng: random.Random) -> str:
    return f"{rng.randint(10, 203)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}"


def _fmt_time(dt: datetime) -> str:
    # Apache time format with +1000 (Brisbane) offset to match the demo zone.
    return dt.strftime("%d/%b/%Y:%H:%M:%S +1000")


def _line(rng: random.Random, dt: datetime, method: str, path: str, status: int) -> str:
    ip = _rand_ip(rng)
    size = 0 if status >= 400 else rng.randint(120, 8000)
    ua = rng.choice(_USER_AGENTS)
    user = "-"
    # occasionally an authenticated user (a PII-ish field for the demo)
    if path.startswith("/api/") and rng.random() < 0.3:
        user = rng.choice(["alice", "bob", "carol", "dave", "svc_jobs"])
    return (f'{ip} - {user} [{_fmt_time(dt)}] '
            f'"{method} {path} HTTP/1.1" {status} {size} '
            f'"-" "{ua}"')


def _daterange(d0: date, d1: date):
    cur = d0
    while cur <= d1:
        yield cur
        cur += timedelta(days=1)


def generate(output_dir: str, from_date: date, to_date: date,
             seed: int, base_per_day: int) -> None:
    rng = random.Random(seed)
    os.makedirs(output_dir, exist_ok=True)

    total_days = (to_date - from_date).days + 1
    # Pick 2-3 incident days, biased toward the first third of the range
    # (same neighbourhood as the Windows incident bursts).
    early_cutoff = from_date + timedelta(days=max(1, total_days // 3))
    early_days = [d for d in _daterange(from_date, early_cutoff)]
    incident_days = set(rng.sample(early_days, k=min(len(early_days), rng.randint(2, 3))))

    files_written = 0
    total_lines = 0
    for day in _daterange(from_date, to_date):
        lines: list[tuple[datetime, str]] = []
        n_normal = base_per_day + rng.randint(-20, 40)

        # normal traffic spread across the day
        for _ in range(n_normal):
            t = datetime(day.year, day.month, day.day,
                         rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
            method, path = rng.choice(_NORMAL_PATHS + _INCIDENT_PATHS)
            # normal status mix
            status = rng.choices([200, 200, 200, 301, 304, 404],
                                 weights=[60, 20, 10, 3, 4, 3])[0]
            lines.append((t, _line(rng, t, method, path, status)))

        # incident burst: a window of 5xx on the incident endpoints
        if day in incident_days:
            burst_hour = rng.randint(9, 17)
            burst_minutes = rng.randint(20, 60)
            burst_count = rng.randint(40, 90)
            for _ in range(burst_count):
                t = datetime(day.year, day.month, day.day, burst_hour,
                             rng.randint(0, burst_minutes - 1), rng.randint(0, 59))
                method, path = rng.choice(_INCIDENT_PATHS)
                status = rng.choice(_SERVER_ERRORS)
                lines.append((t, _line(rng, t, method, path, status)))

        # a few scattered client errors any day
        for _ in range(rng.randint(2, 8)):
            t = datetime(day.year, day.month, day.day,
                         rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
            method, path = rng.choice(_NORMAL_PATHS + _INCIDENT_PATHS)
            lines.append((t, _line(rng, t, method, path, rng.choice(_CLIENT_ERRORS))))

        lines.sort(key=lambda x: x[0])
        fname = os.path.join(output_dir, f"access-{day.isoformat()}.log")
        with open(fname, "w", encoding="utf-8") as f:
            for _, text in lines:
                f.write(text + "\n")
        files_written += 1
        total_lines += len(lines)

    print(f"Wrote {files_written} files ({total_lines} lines) to {output_dir}")
    print(f"Incident (5xx burst) days: "
          f"{sorted(d.isoformat() for d in incident_days)}")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Generate synthetic Apache access logs.")
    p.add_argument("--output_dir", required=True,
                   help="Directory to write access-YYYY-MM-DD.log files into.")
    p.add_argument("--from-date", default="2026-04-30", type=_parse_date,
                   help="Start date YYYY-MM-DD (default 2026-04-30, matching the Windows sample).")
    p.add_argument("--to-date", default="2026-05-10", type=_parse_date,
                   help="End date YYYY-MM-DD (default 2026-05-10).")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed (use different seeds for different sources).")
    p.add_argument("--per-day", type=int, default=120,
                   help="Approx normal requests per day (default 120).")
    args = p.parse_args(argv)

    generate(args.output_dir, args.from_date, args.to_date, args.seed, args.per_day)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
