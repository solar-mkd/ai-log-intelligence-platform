"""
Synthetic Windows service (.NET) log generator.

Generates realistic .NET application/service logs for a date range, in the
multi-line format a Windows service typically writes. File modified-times are
set to match the log date so incremental "modified since last run" ingestion
can be exercised.

Design notes (why it is built this way):
- Logs are SYNTHETIC and domain-neutral. Nothing here maps to any real system.
- Exceptions are drawn from a fixed set of EXCEPTION FAMILIES. Each family has a
  stable signature (type, message template, characteristic stack frames) but
  varies in surface detail (ids, counts, line numbers, inner-exception depth).
  This recurrence-with-variation is deliberate: a RAG system can only find
  "similar errors" / "when did this happen before" if similar errors actually
  recur. Purely random exceptions would defeat the whole use case.
- Some runs inject INCIDENT BURSTS: a cluster of related errors across several
  namespaces within a short time window, so cross-system temporal correlation
  has something real to find.
- Optional synthetic PII fields (fake usernames / ips / account ids) can be
  emitted so a downstream per-field PII policy has something to act on.

Usage:
  python generate_windows_logs.py
  python generate_windows_logs.py --from_date 2026-05-01 --to_date 2026-05-21
  python generate_windows_logs.py --from_events 200 --to_events 2000 --max_events 500
  python generate_windows_logs.py --output_dir ../data/sample --incidents --pii
"""

import argparse
import os
import random
from datetime import datetime, timedelta

# ── Domain (fully synthetic, internally consistent) ───────────────────────────

NAMESPACES = [
    "Contoso.Platform.Ingestion.ServiceLayer.Tasks",
    "Contoso.Platform.Ingestion.ServiceLayer.Services",
    "Contoso.Platform.Scheduling.ServiceLayer.Tasks",
    "Contoso.Platform.Scheduling.ServiceLayer.Services",
    "Contoso.Platform.Inventory.ServiceLayer.Tasks",
    "Contoso.Platform.Reporting.ServiceLayer.Tasks",
    "Contoso.Platform.Core.Infrastructure",
    "Contoso.Platform.Core.DataAccess",
]

TASKS = [
    "SyncInventoryBalanceTask",
    "RefreshScheduleTask",
    "RecalculateAllocationTask",
    "GenerateDailyReportTask",
    "PurgeExpiredSessionsTask",
    "SyncWorkQueueTask",
    "UpdateRecordStatusTask",
    "ProcessBatchTask",
    "RefreshCacheTask",
    "ArchiveAuditLogTask",
    "ReconcileLedgerTask",
    "SyncExternalDataTask",
]

DATA_SOURCES = [
    "SOURCE-SYSTEM-A",
    "SOURCE-SYSTEM-B",
    "EXTERNAL-API-01",
    "WORK-QUEUE-PRIMARY",
    "INVENTORY-DB",
    "PAYMENT-GATEWAY",
    "REPORTING-DW",
]

SERVICE_ACTIONS = [
    "Initialising service configuration",
    "Health check completed",
    "Cache refreshed successfully",
    "Database connection pool resized",
    "Configuration reloaded",
    "Scheduled job registered",
    "Service started",
    "Graceful shutdown initiated",
    "Dependency resolved",
    "Circuit breaker state changed",
]

LOG_LEVELS = ["Information", "Warning", "Error", "Debug"]
LOG_LEVEL_WEIGHTS = [0.55, 0.15, 0.22, 0.08]  # error-rich so exceptions dominate

# Fake people/accounts for optional PII fields (clearly synthetic).
FAKE_USERS = [f"svc_user_{i:03}" for i in range(1, 40)]
FAKE_COUNTRIES = ["AU", "MK", "GB", "DE", "US", "NZ"]


# ── Exception families ────────────────────────────────────────────────────────
# Each family is a recurring failure mode. The signature stays recognisable
# across occurrences; only details vary. Families optionally carry an inner
# exception, producing the authentic .NET "--->" / "--- End of inner exception
# stack trace ---" structure that yields natural segments.

def _frame(method, file, line):
    return f"   at {method} in {file}:line {line}"


EXCEPTION_FAMILIES = [
    {
        "name": "sql_deadlock",
        "type": "System.Data.SqlClient.SqlException",
        "messages": [
            "Transaction (Process ID {pid}) was deadlocked on lock resources with another process and has been chosen as the deadlock victim. Rerun the transaction.",
            "Deadlock found when trying to acquire lock on object 'dbo.{table}'; the transaction was rolled back.",
        ],
        "inner": None,
        "frames": [
            "Contoso.Platform.Core.DataAccess.Repository`1.SaveChangesAsync(CancellationToken ct)",
            "Contoso.Platform.Core.DataAccess.UnitOfWork.CommitAsync()",
            "Contoso.Platform.Inventory.ServiceLayer.Tasks.SyncInventoryBalanceTask.ExecuteAsync()",
        ],
        "files": ["Repository.cs", "UnitOfWork.cs", "SyncInventoryBalanceTask.cs"],
    },
    {
        "name": "connection_timeout",
        "type": "System.Data.SqlClient.SqlException",
        "messages": [
            "Connection Timeout Expired. The timeout period elapsed during the post-login phase. Timeout={timeout}ms.",
            "A connection was successfully established with the server, but then an error occurred during the pre-login handshake.",
        ],
        "inner": {
            "type": "System.ComponentModel.Win32Exception",
            "message": "The wait operation timed out",
            "frames": [
                "System.Data.SqlClient.SqlInternalConnectionTds..ctor(DbConnectionPoolIdentity identity)",
            ],
            "files": ["SqlInternalConnectionTds.cs"],
        },
        "frames": [
            "Contoso.Platform.Core.DataAccess.SqlConnectionFactory.OpenAsync()",
            "Contoso.Platform.Core.Infrastructure.Retry.ExecuteAsync(Func`1 action, RetryPolicy policy)",
        ],
        "files": ["SqlConnectionFactory.cs", "Retry.cs"],
    },
    {
        "name": "http_unavailable",
        "type": "System.Net.Http.HttpRequestException",
        "messages": [
            "Response status code does not indicate success: 503 (Service Unavailable).",
            "Response status code does not indicate success: 504 (Gateway Timeout).",
        ],
        "inner": {
            "type": "System.Net.Sockets.SocketException",
            "message": "A connection attempt failed because the connected party did not properly respond after a period of time",
            "frames": [
                "System.Net.Http.ConnectHelper.ConnectAsync(String host, Int32 port)",
            ],
            "files": ["ConnectHelper.cs"],
        },
        "frames": [
            "Contoso.Platform.Ingestion.ServiceLayer.Services.ExternalDataClient.FetchAsync(String endpoint)",
            "Contoso.Platform.Ingestion.ServiceLayer.Tasks.SyncExternalDataTask.ExecuteAsync()",
        ],
        "files": ["ExternalDataClient.cs", "SyncExternalDataTask.cs"],
    },
    {
        "name": "null_reference",
        "type": "System.NullReferenceException",
        "messages": [
            "Object reference not set to an instance of an object.",
        ],
        "inner": None,
        "frames": [
            "Contoso.Platform.Scheduling.ServiceLayer.Services.AllocationService.Resolve(Int32 recordId)",
            "Contoso.Platform.Scheduling.ServiceLayer.Tasks.RecalculateAllocationTask.ProcessItem(Int32 id)",
            "Contoso.Platform.Scheduling.ServiceLayer.Tasks.RecalculateAllocationTask.ExecuteAsync()",
        ],
        "files": ["AllocationService.cs", "RecalculateAllocationTask.cs", "RecalculateAllocationTask.cs"],
    },
    {
        "name": "ef_update",
        "type": "Microsoft.EntityFrameworkCore.DbUpdateException",
        "messages": [
            "An error occurred while saving the entity changes. See the inner exception for details.",
        ],
        "inner": {
            "type": "System.Data.SqlClient.SqlException",
            "message": "Cannot insert duplicate key row in object 'dbo.{table}' with unique index 'IX_{table}_Key'.",
            "frames": [
                "System.Data.SqlClient.SqlCommand.ExecuteNonQueryAsync(CancellationToken ct)",
            ],
            "files": ["SqlCommand.cs"],
        },
        "frames": [
            "Microsoft.EntityFrameworkCore.Update.ReaderModificationCommandBatch.ExecuteAsync(IRelationalConnection connection)",
            "Contoso.Platform.Core.DataAccess.Repository`1.SaveChangesAsync(CancellationToken ct)",
            "Contoso.Platform.Reporting.ServiceLayer.Tasks.GenerateDailyReportTask.ExecuteAsync()",
        ],
        "files": ["ReaderModificationCommandBatch.cs", "Repository.cs", "GenerateDailyReportTask.cs"],
    },
    {
        "name": "operation_cancelled",
        "type": "System.OperationCanceledException",
        "messages": [
            "The operation was canceled.",
        ],
        "inner": None,
        "frames": [
            "Contoso.Platform.Core.Infrastructure.QueueWorker.RunTaskAsync(ITask task, CancellationToken ct)",
            "Contoso.Platform.Scheduling.ServiceLayer.Tasks.SyncWorkQueueTask.ExecuteAsync()",
        ],
        "files": ["QueueWorker.cs", "SyncWorkQueueTask.cs"],
    },
    {
        "name": "io_error",
        "type": "System.IO.IOException",
        "messages": [
            "The process cannot access the file '{path}' because it is being used by another process.",
            "There is not enough space on the disk.",
        ],
        "inner": None,
        "frames": [
            "Contoso.Platform.Reporting.ServiceLayer.Tasks.ArchiveAuditLogTask.WriteArchive(String path)",
            "Contoso.Platform.Reporting.ServiceLayer.Tasks.ArchiveAuditLogTask.ExecuteAsync()",
        ],
        "files": ["ArchiveAuditLogTask.cs", "ArchiveAuditLogTask.cs"],
    },
    {
        "name": "invalid_operation",
        "type": "System.InvalidOperationException",
        "messages": [
            "Sequence contains no elements.",
            "The connection pool has been exhausted; the maximum pool size was reached.",
        ],
        "inner": None,
        "frames": [
            "System.Linq.Enumerable.First[TSource](IEnumerable`1 source)",
            "Contoso.Platform.Inventory.ServiceLayer.Tasks.ReconcileLedgerTask.ExecuteAsync()",
        ],
        "files": ["Enumerable.cs", "ReconcileLedgerTask.cs"],
    },
]

# Families likely to co-occur in an incident burst (a DB problem cascades).
INCIDENT_FAMILIES = ["sql_deadlock", "connection_timeout", "ef_update"]

TABLES = ["RecordLog", "InventoryItem", "ScheduleEntry", "LedgerLine", "AuditEvent"]
PATHS = [
    r"D:\Services\Contoso\archive\audit_{n}.dat",
    r"D:\Services\Contoso\temp\export_{n}.tmp",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def random_timespan(min_sec=0.1, max_sec=120):
    total = random.uniform(min_sec, max_sec)
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02}:{m:02}:{s:010.7f}"


def random_datetime_in_day(date: datetime):
    return date + timedelta(seconds=random.randint(0, 86399))


def fmt_dt(dt: datetime):
    """Format like a .NET service log timestamp: 5/4/2026 03:46:38 AM."""
    pattern = "%#m/%#d/%Y %I:%M:%S %p" if os.name == "nt" else "%-m/%-d/%Y %I:%M:%S %p"
    return dt.strftime(pattern)


def random_data_sources():
    k = random.randint(1, 3)
    return ",".join(random.sample(DATA_SOURCES, k))


def _fill(template: str) -> str:
    return template.format(
        pid=random.randint(50, 400),
        timeout=random.choice([15000, 30000, 60000]),
        table=random.choice(TABLES),
        path=random.choice(PATHS).format(n=random.randint(1, 999)),
    )


def render_exception(family: dict, depth_hint: int = 0) -> list[str]:
    """Render one exception (optionally with an inner exception) as .NET text.

    Produces the authentic structure:
        System.X: outer message ---> System.Y: inner message
           at <inner frames> in <file>:line N
           --- End of inner exception stack trace ---
           at <outer frames> in <file>:line N
    Each line is a natural segment boundary for structure-aware chunking.
    """
    outer_msg = _fill(random.choice(family["messages"]))
    header = f"{family['type']}: {outer_msg}"

    inner = family.get("inner")
    if inner:
        inner_msg = _fill(inner["message"])
        header += f" ---> {inner['type']}: {inner_msg}"

    lines = [f"   Exception Type               : {family['type']}"]
    lines.append(f"   Message                      : {outer_msg}")
    lines.append(f"   Stack Trace                  :")
    lines.append(f"{header}")

    if inner:
        for fr, fl in zip(inner["frames"], inner["files"]):
            lines.append(_frame(fr, fl, random.randint(20, 400)))
        lines.append("   --- End of inner exception stack trace ---")

    # outer frames (vary how many are shown, but keep the signature recognisable)
    pairs = list(zip(family["frames"], family["files"]))
    n = random.randint(max(2, len(pairs) - 1), len(pairs))
    for fr, fl in pairs[:n]:
        lines.append(_frame(fr, fl, random.randint(20, 600)))

    return lines


def maybe_pii(lines: list[str], emit_pii: bool):
    if emit_pii and random.random() < 0.5:
        lines.append(f"   User                         : {random.choice(FAKE_USERS)}")
        lines.append(f"   Country                      : {random.choice(FAKE_COUNTRIES)}")
        lines.append(f"   Account Id                   : ACC-{random.randint(100000, 999999)}")


# ── Entry generators ──────────────────────────────────────────────────────────

def make_task_entry(dt, task, ns, emit_pii, force_family=None) -> str:
    level = "Error" if force_family else random.choices(LOG_LEVELS, LOG_LEVEL_WEIGHTS)[0]
    lines = [f"{fmt_dt(dt)} {level}: {ns}.{task} record processing details"]

    if level in ("Information", "Debug"):
        lines.append(f"   Time Taken                   : {random_timespan(1, 90)}")
        lines.append(f"   Unique Data Sources          : {random_data_sources()}")
        lines.append(f"   Deleted Count                : {random.randint(0, 100)}")
        lines.append(f"   Modified Count               : {random.randint(0, 500)}")
        lines.append(f"   New Count                    : {random.randint(0, 800)}")
    elif level == "Warning":
        lines.append(f"   Time Taken                   : {random_timespan(60, 120)}")
        lines.append(f"   Warning                      : Processing time exceeded threshold")
        lines.append(f"   Unique Data Sources          : {random_data_sources()}")
    else:  # Error
        family = force_family or random.choice(EXCEPTION_FAMILIES)
        lines.extend(render_exception(family))
        maybe_pii(lines, emit_pii)

    return "\n".join(lines)


def make_queue_entry(dt, task, ns, emit_pii, force_family=None) -> str:
    level = "Error" if force_family else random.choices(LOG_LEVELS, LOG_LEVEL_WEIGHTS)[0]
    lines = [f"{fmt_dt(dt)} {level}: Queue Worker Task"]
    lines.append(f"   Time Taken  : {random_timespan(5, 180)}")
    lines.append(f"   Worker Type : {ns}.{task}")
    lines.append(f"   Queue Name  : {task}")

    if level == "Warning":
        lines.append(f"   Warning     : Queue depth exceeded 1000 items")
        lines.append(f"   Queue Depth : {random.randint(1000, 5000)}")
    elif level == "Error":
        family = force_family or random.choice(EXCEPTION_FAMILIES)
        lines.extend(render_exception(family))
        maybe_pii(lines, emit_pii)

    return "\n".join(lines)


def make_service_entry(dt, ns, emit_pii, force_family=None) -> str:
    level = "Error" if force_family else random.choices(LOG_LEVELS, LOG_LEVEL_WEIGHTS)[0]
    action = random.choice(SERVICE_ACTIONS)
    lines = [f"{fmt_dt(dt)} {level}: {ns} {action}"]

    if level == "Information":
        lines.append(f"   Duration    : {random_timespan(0.01, 5)}")
        lines.append(f"   Status      : OK")
    elif level == "Warning":
        lines.append(f"   Duration    : {random_timespan(10, 60)}")
        lines.append(f"   Status      : Degraded")
        lines.append(f"   Detail      : Response time above acceptable threshold")
    elif level == "Error":
        family = force_family or random.choice(EXCEPTION_FAMILIES)
        lines.append(f"   Status      : Failed")
        lines.extend(render_exception(family))
        maybe_pii(lines, emit_pii)

    return "\n".join(lines)


def generate_entry(dt, emit_pii, force_family=None) -> str:
    ns = random.choice(NAMESPACES)
    task = random.choice(TASKS)
    kind = random.choices(["task", "queue", "service"], weights=[0.45, 0.35, 0.20])[0]
    if kind == "task":
        return make_task_entry(dt, task, ns, emit_pii, force_family)
    if kind == "queue":
        return make_queue_entry(dt, task, ns, emit_pii, force_family)
    return make_service_entry(dt, ns, emit_pii, force_family)


def build_incident(dt: datetime, emit_pii: bool) -> list[tuple[datetime, str]]:
    """A burst of related errors across namespaces within a short window.

    Gives cross-system temporal correlation something real to find: the same
    family (or its cascade) erupts at many timestamps inside a few minutes.
    """
    family = random.choice([f for f in EXCEPTION_FAMILIES if f["name"] in INCIDENT_FAMILIES])
    burst = []
    count = random.randint(8, 25)
    window = timedelta(minutes=random.randint(2, 8))
    for _ in range(count):
        offset = timedelta(seconds=random.uniform(0, window.total_seconds()))
        ts = dt + offset
        burst.append((ts, generate_entry(ts, emit_pii, force_family=family)))
    return burst


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_logs(start_date, end_date, min_events, max_events, max_per_file,
                  output_dir, emit_pii, with_incidents):
    os.makedirs(output_dir, exist_ok=True)
    current = start_date
    total_files = total_entries = 0

    while current <= end_date:
        day_entry_count = random.randint(min_events, max_events)
        date_str = current.strftime("%m-%d-%Y")

        pairs = [(ts, generate_entry(ts, emit_pii))
                 for ts in (random_datetime_in_day(current) for _ in range(day_entry_count))]

        # Occasionally inject one or more incident bursts into the day.
        if with_incidents and random.random() < 0.4:
            for _ in range(random.randint(1, 2)):
                burst_anchor = random_datetime_in_day(current)
                pairs.extend(build_incident(burst_anchor, emit_pii))

        pairs.sort(key=lambda p: p[0])
        entries = [text for _, text in pairs]

        chunks = [entries[i:i + max_per_file] for i in range(0, len(entries), max_per_file)]
        for idx, chunk in enumerate(chunks):
            filename = f"log-{date_str}.log" if len(chunks) == 1 else f"log-{date_str}-{idx}.log"
            filepath = os.path.join(output_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(chunk) + "\n")
            file_time = current.replace(hour=23, minute=59, second=59).timestamp()
            os.utime(filepath, (file_time, file_time))
            total_files += 1
            total_entries += len(chunk)
            print(f"  Created: {filename} ({len(chunk)} entries)")

        print(f"Day {current.strftime('%d %b %Y')}: {len(entries)} entries across {len(chunks)} file(s)")
        current += timedelta(days=1)

    print(f"\nDone! {total_files} files, {total_entries:,} total log entries in '{output_dir}/'")


def parse_date(value: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date '{value}'. Use YYYY-MM-DD, DD/MM/YYYY, or MM/DD/YYYY."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate synthetic Windows service (.NET) log files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from_date", type=parse_date, default=datetime(2026, 5, 1), metavar="DATE",
                        help="Start date. Default: 2026-05-01")
    parser.add_argument("--to_date", type=parse_date, default=datetime(2026, 5, 21), metavar="DATE",
                        help="End date inclusive. Default: 2026-05-21")
    parser.add_argument("--from_events", type=int, default=500, metavar="N",
                        help="Min entries per day. Default: 500")
    parser.add_argument("--to_events", type=int, default=5000, metavar="N",
                        help="Max entries per day. Default: 5000")
    parser.add_argument("--max_events", "--max_entries", type=int, default=1000, metavar="N",
                        dest="max_events",
                        help="Max entries PER FILE before splitting into log-MM-DD-YYYY-0.log, "
                             "-1.log, etc. (per-file cap; --from_events/--to_events are per-day). "
                             "Default: 1000")
    parser.add_argument("--output_dir", type=str, default="logs", metavar="DIR",
                        help="Output directory. Default: logs")
    parser.add_argument("--incidents", action="store_true",
                        help="Inject correlated incident bursts (for cross-system correlation demos).")
    parser.add_argument("--pii", action="store_true",
                        help="Emit synthetic PII fields (fake user/country/account) to exercise PII policy.")

    args = parser.parse_args()
    if args.from_date > args.to_date:
        parser.error("--from_date must be before or equal to --to_date")
    if args.from_events < 1:
        parser.error("--from_events must be at least 1")
    if args.to_events < args.from_events:
        parser.error("--to_events must be >= --from_events")
    if args.max_events < 1:
        parser.error("--max_events must be at least 1")

    print(f"Generating logs from {args.from_date:%d %b %Y} to {args.to_date:%d %b %Y}")
    print(f"Events per day : {args.from_events} - {args.to_events}")
    print(f"Max per file   : {args.max_events}")
    print(f"Incidents      : {'on' if args.incidents else 'off'}")
    print(f"PII fields     : {'on' if args.pii else 'off'}")
    print(f"Output dir     : {args.output_dir}\n")

    generate_logs(args.from_date, args.to_date, args.from_events, args.to_events,
                  args.max_events, args.output_dir, args.pii, args.incidents)
