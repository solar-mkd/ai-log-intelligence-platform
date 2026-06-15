# Tools

Developer utilities that support the LogLens platform but are not part of the
shipped application in [`src/loglens`](../src/loglens). Run them directly with
Python.

---

## `generate_windows_logs.py` — synthetic Windows service log generator

Generates realistic .NET / Windows service log files over a date range, for
testing and demos. All output is **synthetic and domain-neutral** — it contains
no real systems, customers, or data — so the files it writes are safe to commit
to [`data/sample`](../data/sample).

### Why the data is structured the way it is

This is not a random log spewer. Its shape is deliberate, because the platform's
core features can only be demonstrated against data that has the right structure:

- **Recurring exception families.** Errors are drawn from a fixed set of failure
  modes (SQL deadlock, connection timeout, HTTP 503/504, null reference, EF
  update conflict, operation cancelled, I/O error, invalid operation). Each
  family keeps a recognisable signature across occurrences while varying in
  surface detail. This recurrence-with-variation is what lets retrieval find
  *similar* errors and answer "when did this happen before" — purely random
  exceptions would have nothing to match (see ADR-010, ADR-011).
- **Authentic multi-line exceptions.** Output uses the real .NET format,
  including inner exceptions (`--->` and `--- End of inner exception stack
  trace ---`) and `at Namespace.Method() in File.cs:line N` frames. These lines
  are the natural boundaries the platform's structure-aware chunking splits on
  (ADR-009, ADR-010).
- **Incident bursts** (optional). Clusters of related errors across namespaces
  within a short window, so cross-system temporal correlation has something real
  to find (ADR-007, ADR-011).
- **Synthetic PII fields** (optional). Fake user / country / account fields so
  the silver-layer PII policy has something to act on (ADR-015).

Because the families are known up front, the generated data doubles as a small
evaluation set: you know the ground-truth family of each error, so you can check
whether embedding and retrieval actually group them correctly.

### Usage

```bash
python generate_windows_logs.py [options]
```

Run with no options to produce a default 3-week sample. Add `--help` to see all
options.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--from_date DATE` | `2026-05-01` | Start date. Accepts `YYYY-MM-DD`, `DD/MM/YYYY`, or `MM/DD/YYYY`. |
| `--to_date DATE` | `2026-05-21` | End date (inclusive). |
| `--from_events N` | `500` | Minimum entries generated **per day**. |
| `--to_events N` | `5000` | Maximum entries generated **per day**. |
| `--max_events N` | `1000` | Maximum entries **per file** before splitting (per-file cap). Also accepts the alias `--max_entries`. |
| `--output_dir DIR` | `logs` | Output directory. |
| `--incidents` | off | Inject correlated incident bursts (for correlation demos). |
| `--pii` | off | Emit synthetic PII fields (to exercise the PII policy). |

Note the two different axes: `--from_events` / `--to_events` control how many
entries a *day* has; `--max_events` controls how many entries a single *file*
holds before a new file is started.

### Output files

One or more `.log` files per day, named:

```
log-MM-DD-YYYY.log            # a day that fits in one file
log-MM-DD-YYYY-0.log          # first file when a day is split
log-MM-DD-YYYY-1.log          # second file, etc.
```

Each file's modified-time is set to its log date, so incremental
"modified since last run" ingestion can be tested. The `windows_service` parser
([`src/loglens/parsers/windows_service.py`](../src/loglens/parsers/windows_service.py))
expects this `log-MM-DD-YYYY[-N].log` naming.

### Examples

```bash
# Default 3-week sample into ./logs
python generate_windows_logs.py

# A specific fortnight
python generate_windows_logs.py --from_date 2026-05-01 --to_date 2026-05-14

# Variable daily volume, split into files of at most 500 entries each
python generate_windows_logs.py --from_events 200 --to_events 1000 --max_events 500

# Generate the committed sample set: correlation bursts + PII fields
python generate_windows_logs.py --from_events 100 --to_events 300 \
    --output_dir ../data/sample --incidents --pii

# Generate bulk data for local testing (data/raw is git-ignored)
python generate_windows_logs.py --from_date 2026-01-01 --to_date 2026-03-31 \
    --from_events 2000 --to_events 8000 --output_dir ../data/raw --incidents
```

### A note on committed samples

Keep anything written to `data/sample` small (a few MB) since it is committed.
Use `data/raw` (git-ignored) for large volumes. The generator only ever produces
synthetic values, so there is no PII or confidential data to worry about — which
is itself the point: the platform is built around governed handling of sensitive
log data, so its own test data is generated clean by design.
