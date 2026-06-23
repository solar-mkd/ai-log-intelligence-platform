# Production Readiness & Operational Concerns

> **Purpose & honesty of scope.** LogLens is a reference implementation built to prove an
> architecture, not a deployed production service. This document sets out the operational concerns a
> production deployment must address, distinguishes what the current design **already provides**, what
> is **partially in place**, and what is **deliberately deferred** — and gives the reasoning for each
> deferral. The intent is to demonstrate that these concerns are understood and designed around, not
> overlooked.

Status markers used below:

- **[DESIGNED-IN]** — already provided by the current architecture
- **[PARTIAL]** — foundations present, completion deferred
- **[DEFERRED]** — understood, not yet built (with rationale)

**Why deferral is a legitimate architectural position.** A reference implementation should prove its
core thesis before elaborating operational machinery. Building a full SLO / alerting / rollout stack
before the architecture is validated would be premature. The discipline is to *know* what production
requires, *design so it can be added without rework*, and defer the build deliberately — which the
layer isolation, stateless steps, and observability hooks already in the design enable.

---

## 1. Service Levels (SLA / SLO)

LogLens has two operationally distinct surfaces with different service-level profiles; treating them
as one would be a mistake.

### Ingest / processing pipeline (write path)

Service level is about **freshness** and **completeness**, not request latency.

| SLO | Target (illustrative) | Rationale |
|---|---|---|
| Data freshness | 95% of entries queryable within 15 min of arrival | Diagnostics need recent data; minutes, not seconds, is acceptable |
| Completeness | 99.9% of landed entries reach gold | Idempotency + per-file commits make loss rare |
| Pipeline success | 99% of source runs complete without intervention | Per-source isolation contains failures |

**[DESIGNED-IN]** Hash idempotency makes re-runs safe (a missed window is recoverable by re-running),
per-file commits bound data loss to at most one in-flight file, and per-source isolation contains
failures.

### Query / RAG surface (read path)

**Latency** matters; the honest constraint is the LLM.

| SLO | Target (illustrative) | Note |
|---|---|---|
| Retrieval latency | p95 < 500 ms | pgvector + HNSW makes retrieval fast; achievable now |
| End-to-end answer | p95 < 10 s (warm model) | Dominated by LLM generation, not retrieval |
| Availability (read) | 99.5% | Degrades gracefully: retrieval works even if the LLM is down (§2) |

> **Honest operational reality.** The dominant latency factor is LLM inference, not the data platform.
> A cold model load takes far longer than a warm call. Production would keep the model warm (a resident
> inference service) and right-size the model to the latency target. The swappable-model design makes
> this a configuration choice, not a rebuild.

*Targets are illustrative starting points for an SLO conversation, not commitments.*

---

## 2. Failure-Mode Handling

The pipeline's structure was chosen partly *for* its failure behaviour.

| Failure | Current behaviour | Production addition |
|---|---|---|
| Bad file (corrupt/unparseable) | Marked *failed*, skipped; run continues; per-file rollback | Alert on failed-file rate; dead-letter queue for replay |
| Crash mid-run | Committed files stay done; run row left *in_progress* (diagnosable); re-run is safe | Watchdog for stale runs; auto-retry/alert |
| Duplicate / re-delivered data | Entry-hash idempotency: re-ingestion is a no-op | — (already robust) |
| One source failing | Per-source isolation; others proceed; non-zero exit but run completes | Per-source health on a dashboard |
| Missing PII key | Fail-closed: refuses to run rather than write unprotected PII | Pre-flight config validation in CI / on deploy |
| LLM unavailable | Retrieval still works; only generation fails | Graceful degradation: return evidence with a notice |
| Database unavailable | Steps fail cleanly, re-runnable; no partial-state corruption | Connection retry/backoff; read replica for queries |
| Embedding model change | Vectors model-pinned in a separate table; old/new coexist | Background re-embedding job with cutover |

**[DESIGNED-IN]** The current-behaviour column is implemented, not aspirational. **[DEFERRED]** The
production-addition column is understood and deferred; the design's isolation makes each additive.

> **Design principle.** Most failures are *contained* rather than *prevented* — a bad file, a crash, a
> down dependency cannot corrupt state or lose committed data, and recovery is "re-run." Designing for
> containment and safe re-run, rather than trying to prevent every failure, is the mature stance for a
> data pipeline.

---

## 3. Observability Depth

### What exists

**[PARTIAL]** The pipeline records per-run observability in `bronze_runs` — status, files processed,
entries landed, duration, host — updated after every file, so progress is visible live and a crashed
run is diagnosable. Failures are logged per source.

### What production needs (the three pillars)

| Pillar | For LogLens specifically | Status |
|---|---|---|
| Metrics | Throughput, freshness lag, failed-file rate, retrieval p95, LLM latency, embedding backlog | **[DEFERRED]** emit to Prometheus/StatsD from existing run-tracking hooks |
| Logs (of LogLens itself) | Structured logs per step with run_id/source_id correlation | **[PARTIAL]** failures logged; needs structured, correlated, levelled logging |
| Traces | A query traced retrieval → prompt → LLM; an entry traced bronze → silver → gold | **[DEFERRED]** GUID keys already provide the correlation IDs |

### Data-quality observability (often forgotten)

A data platform must also observe **data correctness**: parse-failure rate per source (a spike means a
log format changed), unexpected severity distributions, sources that went silent, embedding coverage.
**[DEFERRED]** — but these are straightforward scheduled SQL checks given the schema.

> **The architect point.** The system was built with observability *hooks* (the run table, GUID
> correlation keys, per-source status) rather than observability *plumbing*. Design the seams telemetry
> attaches to; defer wiring to a specific stack (an environment decision) until deployment.

---

## 4. Rollout & Change Strategy

### Schema & pipeline-code changes

**[DESIGNED-IN]** Schema application is idempotent (`IF NOT EXISTS`), so rollout is safe and
repeatable; stateless steps and re-runnable layers let a fixed transform re-process a layer without
re-ingesting. **[DEFERRED]** Production adds up/down migrations, a staging environment, and CI gating
on the test suite.

### Parser changes / new log types

**[DESIGNED-IN]** A new log type is additive — one self-registering module, no change to existing
parsers or the pipeline (proven by adding Apache). Parser version is recorded per row, so changes are
traceable. **[DEFERRED]** Controlled backfill to re-process under a new parser version.

### Embedding-model changes (highest-risk change)

Changing the embedding model invalidates comparability between old and new vectors. **[DESIGNED-IN]**
Anticipated: embeddings are pinned to the producing model in a separate table, so a new model's
vectors coexist with the old. **[DEFERRED]** The enabled rollout: embed under the new model in
parallel, A/B-evaluate retrieval on the same queries, cut over the query path — rollback is pointing
back at the old model's vectors.

> **Why this reads as architect-level.** The riskiest production change (model migration) was made safe
> by an upfront design decision (model-pinned vectors), not patched later. Designing the rollback path
> before it is needed is the signal.

---

## 5. Cost Control

### Dominant cost drivers

| Driver | Behaviour | Lever |
|---|---|---|
| Storage | Three layers (raw + silver + gold) by design | Bronze archival/retention tiering (roadmap) |
| Embedding compute | One-off per segment; skipped if already embedded | Batch sizing; no re-embedding of unchanged segments |
| LLM inference | Per query; most variable; CPU/GPU-bound | Model right-sizing (swappable by config) |
| Database | HNSW index + vectors grow with corpus | Index tuning; partitioning/retention on gold |

### The structural cost advantage

**[DESIGNED-IN]** Because embeddings and the LLM run **locally**, there are **no per-token or
per-API-call charges** — cost is owned hardware, not metered cloud AI. For high or unpredictable query
volumes this is far cheaper and more predictable than hosted-LLM RAG, and it caps data egress and
third-party exposure at zero.

> **Cost as an architectural choice.** "Local-first" was chosen for privacy, but it doubles as cost
> control: it converts a variable, usage-metered AI bill into a fixed, capacity-planned hardware cost.
> One decision serves privacy, cost, and governance simultaneously.

**[DEFERRED]** Formal cost monitoring (storage-growth trending, per-query compute attribution,
retention automation) is roadmap, enabled by the same observability hooks in §3.

---

## 6. Security & Governance

This is where LogLens has the most *built*, not merely designed — handling logs means handling
sensitive data from the outset.

### What is implemented

- **[DESIGNED-IN] Per-field, per-source PII policy** — redaction and keyed-HMAC pseudonymisation,
  applied at silver ingestion, configurable per source (ADR-015).
- **[DESIGNED-IN] Fail-closed enforcement** — refuses to run if an HMAC field lacks its key, so it
  cannot silently write unprotected PII.
- **[DESIGNED-IN] Secrets never committed** — connection strings and keys come from environment/local
  config only; real config git-ignored, placeholder example checked in.
- **[DESIGNED-IN] Local-first data residency** — embeddings and LLM run on-machine; log data never
  leaves the environment.
- **[DESIGNED-IN] Full lineage** — every entry traceable bronze → silver → gold via stable keys; raw
  entry retained for audit.

### What production would add

| Concern | Production requirement | Status |
|---|---|---|
| Access control | AuthN/Z on the query surface; source-level authorization | **[DEFERRED]** |
| Encryption | At rest (DB/volume) and in transit (TLS); AES PII action for reversible fields | **[PARTIAL]** AES hook reserved; transport/at-rest are deploy config |
| Key management | HMAC key in a secrets manager with rotation (rotation changes fingerprints — a documented trade-off) | **[DEFERRED]** |
| Access audit | Who queried what, when — audit log of the read path | **[DEFERRED]** |
| Retention & erasure | Policy-driven retention; erasure strategy for pseudonymised fields | **[DEFERRED]** |

> **Governance posture.** The design treats PII and lineage as first-class, built-in concerns rather
> than add-ons — fail-closed by default, raw retained for audit, secrets externalized. The deferred
> items are deployment-context decisions layered onto a base designed to be governed, not retrofitted.

---

## Summary: Designed-In vs. Deferred

The recurring pattern is deliberate. The **architecture** already embeds the properties that make
production operability *possible*: idempotency and safe re-run, per-file and per-source isolation,
transactional containment, model-pinned vectors, observability hooks (run-tracking + GUID correlation
keys), fail-closed governance, and local-first residency. The **operational machinery** built on those
properties — alerting, dashboards, watchdogs, CI/CD gating, KMS, access control, retention automation —
is consciously deferred, because a reference implementation should validate its architecture before
elaborating its operations.

These concerns are **understood, reasoned about, and designed for** — the deferrals are choices with
rationale, not blind spots. Knowing what production demands, and designing so it can be added without
rework, is the architectural discipline; building it all upfront would be the opposite.
