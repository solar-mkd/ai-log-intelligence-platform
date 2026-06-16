# Architecture Decision Records

**Project:** Cross-System Log Intelligence Platform
**Purpose:** Ingest heterogeneous system logs into a governed Medallion (bronze/silver/gold) data platform, extract and segment exceptions, embed them for semantic retrieval, and answer cross-system diagnostic questions using local RAG.

Each record below captures a decision, why it was made, what alternatives were weighed, and the consequences. ADRs are immutable once accepted; if a decision changes later, a new ADR supersedes the old one rather than editing it.

---

## ADR-001 — Medallion architecture (bronze / silver / gold)

**Status:** Accepted

**Context.** The system ingests raw, messy, heterogeneous logs and must serve both audit/traceability needs and analytical/RAG needs. These have conflicting requirements: raw fidelity vs. clean structure vs. query-ready analytical product.

**Decision.** Adopt a Medallion layering. **Bronze** holds raw log entries exactly as ingested (one entry per row) plus a landing/archive split. **Silver** holds cleaned, parsed, structured entries with normalized fields. **Gold** holds the analytical products — reassembled/segmented exceptions, embeddings, and serving views for RAG and BI.

**Alternatives considered.**
- *Single flat table.* Rejected: cannot separate raw-fidelity audit needs from clean query needs; schema churn would corrupt history.
- *Two layers (raw + curated).* Rejected: conflates cleaned entries with derived analytical products (segments/embeddings), which have different lifecycles.

**Consequences.** Clear separation of concerns and lineage; each layer independently re-runnable. Slightly more storage (data exists at multiple stages) — an acceptable trade for auditability.

---

## ADR-002 — Bronze landing/archive split with control table

**Status:** Accepted

**Context.** Ingestion must be idempotent, resumable, and auditable. We need to know at any time what has been processed and what is in flight.

**Decision.** Bronze has a **landing** table (in-flight entries) and an **archive** table (entries from fully completed files). A **processed-logs control table** records each processed file by a composite identity (file name + source system + shared path location). When a file's entries are fully digested into silver, they move from landing to archive.

**Alternatives considered.**
- *Single bronze table with a status flag.* Rejected: mixing in-flight and completed data complicates reprocessing and bloats the hot table.

**Consequences.** Strong traceability and clean idempotency boundaries. Movement between landing and archive adds an orchestration step.

---

## ADR-003 — Hash-based idempotency at entry and file level

**Status:** Accepted

**Context.** Re-ingesting the same file, or processing rotated logs that duplicate boundary lines, must not create duplicate rows. The pipeline must be safely re-runnable.

**Decision.** Compute a hash of each raw log entry (the dedup/merge key) and a hash of each whole file (the "fully processed / changed since last run" signal). Deduplication compares the entry hash **only within a matched processed-file identity** (file name + system + shared path), which correctly handles rotated-log boundary duplication.

**Alternatives considered.**
- *Auto-increment IDs + truncate-and-reload.* Rejected: not incremental, not idempotent, loses history.
- *Global entry-hash dedup ignoring file identity.* Rejected: would wrongly drop legitimate identical lines that recur across rotated files.

**Consequences.** Merge/upsert semantics guarantee no duplicates. Requires consistent, deterministic hashing of entries and files.

---

## ADR-004 — Pluggable parser contract and registry

**Status:** Accepted

**Context.** The platform starts with Windows service logs but must accommodate future log types (Apache, HDFS, EVTX, etc.) with radically different structures (text, multi-line stack traces, binary XML). "Flexible" must be a real architectural property, not aspiration.

**Decision.** Define a single parser **contract** every log-type parser obeys (a common function signature, e.g. `parse(raw_entry, source_config) -> ParsedEntry`). Each log type is implemented as its own module and **registered** against its `log_type`. The entry point dispatches by looking up the registered parser. Adding a new log type means dropping in a new module that implements the contract — with **no changes** to the dispatcher.

**Alternatives considered.**
- *Branching logic (`if log_type == ...`) in one script.* Rejected: every new type edits core code; brittle and not open/closed.

**Consequences.** True extensibility; each log type self-contained, including its own exception-segment boundary rules. Requires up-front discipline defining a stable contract.

---

## ADR-005 — Silver schema: universal columns + JSON overflow

**Status:** Accepted

**Context.** Different log types share a few universal fields (time, severity, message) but vary widely otherwise. We want fast, indexable queries on common fields without losing any source-specific data, and without schema churn as new log types arrive.

**Decision.** One common silver table with **fixed, indexable columns** for universal/searchable fields, plus an **`extra_fields` JSON column** capturing everything parsed but not promoted. The tendency is for frequently searched fields to be promoted to real columns (so they can be indexed); everything else lives in JSON. Nothing parsed is ever discarded.

**Alternatives considered.**
- *Fully fixed schema per log type (separate tables).* Rejected: defeats cross-system querying, multiplies maintenance.
- *Everything in JSON.* Rejected: poor query performance, no enforced structure on the fields we rely on.

**Consequences.** Cross-system queries stay simple and fast; future log types self-describe via JSON. Querying overflow fields is slightly more costly — acceptable since they're rarely the search key.

---

## ADR-006 — Normalize for querying, retain raw for audit (dual-field pattern)

**Status:** Accepted

**Context.** Cross-system queries require common semantics (one severity scheme, one time basis), but audit requires the original values to be preserved exactly.

**Decision.** Apply a consistent dual-field pattern. **Severity:** store `severity` (normalized to a common set) and `severity_raw` (the source's original string). **Time:** store `event_time_utc` (normalized) and `event_time_local` (original as it appeared) plus `source_timezone`. Normalize for querying; retain raw for fidelity.

**Alternatives considered.**
- *Store only normalized values.* Rejected: loses source fidelity, breaks audit.
- *Store only raw values.* Rejected: cross-system queries become impossible.

**Consequences.** Both correlation and audit are satisfied. Modest extra columns; mapping tables needed for severity normalization.

---

## ADR-007 — Timestamps stored in UTC using IANA time zones

**Status:** Accepted

**Context.** Cross-system temporal correlation ("what happened in other systems around that time") requires a single time basis. Logs arrive in local times that span daylight-saving boundaries.

**Decision.** Normalize all timestamps to **UTC** in silver. Each source declares its time zone in config as an **IANA zone name** (e.g. `Australia/Brisbane`), not a fixed offset. The IANA zone resolves the correct offset per-timestamp, including across DST changes.

**Alternatives considered.**
- *Fixed UTC offset per source.* Rejected: breaks across DST transitions and for historical logs straddling them.
- *Keep local time only.* Rejected: cross-system correlation becomes unreliable.

**Consequences.** Correct, DST-safe temporal alignment across systems. Requires a reliable time-zone database at runtime.

---

## ADR-008 — Parser versioning on every row

**Status:** Accepted

**Context.** Parsers will improve over time. We must know which rows were produced by which parser version to selectively reprocess.

**Decision.** Stamp every silver row with `parser_version`. If a parser is improved, affected rows can be identified and reprocessed without touching the rest.

**Alternatives considered.**
- *No version tracking.* Rejected: a parser fix would force a full, undifferentiated reprocess or leave inconsistent data.

**Consequences.** Selective, safe reprocessing; clear provenance. One extra column.

---

## ADR-009 — Exception reassembly and segmentation as a dedicated gold step

**Status:** Accepted

**Context.** Exceptions span multiple log lines and must be reassembled, then split into meaningful segments for embedding. This is a derived analytical product, distinct from clean silver entries.

**Decision.** Silver stores the full reassembled exception in a column (plus JSON overflow) and an `is_exception` flag. A **separate gold script** extracts exceptions from silver and splits each into segments, writing one row per segment to a **gold exception-segment table**. A further script produces embeddings. Three scripts, three single responsibilities, each independently re-runnable.

**Alternatives considered.**
- *Do segmentation in the bronze→silver step.* Rejected: couples ingestion to analytical shaping; segments are a gold-layer product, not a silver one.

**Consequences.** Clean separation and re-runnability; each segment is independently addressable for embedding. Adds pipeline steps.

---

## ADR-010 — Structure-aware (semantic) chunking on exception segments

**Status:** Accepted

**Context.** Chunking strategy determines retrieval quality. The common default is fixed-size chunks with overlap, which cuts across meaning.

**Decision.** Use **structure-aware chunking**: one chunk per exception **segment**, aligned to the natural structure of the exception, because the segment is the natural unit of meaning. One exception therefore yields several chunks. Embeddings are produced per segment.

**Alternatives considered.**
- *Fixed-size chunking with overlap.* Rejected: arbitrary boundaries fragment meaning and degrade retrieval.
- *One embedding per whole exception.* Rejected: too coarse; loses the ability to match on the specific failing frame/segment.

**Consequences.** Higher-quality, more precise retrieval; matches the diagnostic use case. Requires reliable segment-boundary logic per log type (owned by each parser).

---

## ADR-011 — Embed segment text only; metadata stored as filterable columns

**Status:** Accepted

**Context.** Each embedded record needs time, source, and severity to support the headline temporal/cross-system queries. A tempting mistake is to fold that metadata into the text that gets embedded.

**Decision.** Embed **only the exception segment text**. Store `event_time_utc`, `source_id`, `severity` (and segment text, model reference, vector) as **separate structured columns** next to the vector — never inside the embedded string. Metadata is for filtering; the vector is for semantic meaning.

**Alternatives considered.**
- *Embed metadata + text together (JSON or key/value in the string).* Rejected: pollutes the vector so that identical errors from different systems produce different vectors — directly breaking cross-system similarity, which is the core use case.

**Consequences.** Cross-system semantic matching works correctly; metadata stays fast and indexable for filtering. Requires keeping vector and metadata aligned per segment.

---

## ADR-012 — Separate embeddings table with model provenance (pinning)

**Status:** Accepted

**Context.** Vectors from different embedding models are mathematically incompatible; comparing across models yields meaningless results. The embedding model is therefore a controlled dependency, unlike the freely swappable chat LLM. The project's identity is audit and lineage.

**Decision.** Store embeddings in a **dedicated table**, one row per (segment, embedding model). Each row records the chunk reference, the **embedding model name + version**, and the vector. This supports zero-downtime model migration, A/B evaluation of embedding models, and full re-embedding provenance — consistent with the system's audit-first design.

**Alternatives considered.**
- *Embedding as a column on the segment table (one-to-one).* Simpler and lower-latency, and correct for a single fixed model — but cannot hold multiple models simultaneously for migration/evaluation, and mutates live rows on model change. Rejected for this project; **would be preferable in a latency-critical production system with one fixed model.**

**Consequences.** Safe model evolution, empirical tuning, and provenance. Costs one join and an extra table — acceptable here, where audit consistency outweighs the join cost.

---

## ADR-013 — PostgreSQL + pgvector as the single store

**Status:** Accepted

**Context.** The system needs relational data (control tables, silver entries, segments) and vector data (embeddings) co-located, free/portable tooling, and mature vector search. SQL Server's native vector type is recent and version-dependent.

**Decision.** Use **PostgreSQL with the pgvector extension** as the single store for both relational and vector data. Similarity search uses pgvector distance operators (`<=>` cosine for text embeddings) with an **HNSW** index for approximate nearest-neighbour search at scale. Retrieval is **hybrid**: filter on metadata columns (`WHERE` on time window, source, severity), then rank survivors by vector similarity (`ORDER BY embedding <=> :query_vector LIMIT k`).

**Alternatives considered.**
- *SQL Server for relational + dedicated vector DB (Qdrant/Chroma).* Viable but adds a second system and a cross-store bridge to keep in sync.
- *SQL Server 2025 native vectors for everything.* Rejected: pins the solution to the newest version, hurting portability.

**Consequences.** Single backup/store, natural joins between segments and vectors, free and reproducible (anyone can clone and run). Note: filtered ANN search has known recall trade-offs (filter and approximate index interact) — acceptable at portfolio scale, worth monitoring at production scale. Introduces Postgres as a deliberate, requirements-driven technology selection.

---

## ADR-014 — Stateless, orchestrator-agnostic processing steps with GUID keys

**Status:** Accepted

**Context.** The reference build runs as separate Python scripts from a single entry point, but the design should not preclude future distribution across multiple nodes. The scheduler should be whatever the host provides (cron, Azure DevOps pipeline, etc.).

**Decision.** Build each step as a **stateless** unit that reads its work from the database/config and can run anywhere; sources are processed via a **config-driven loop**. Orchestration is **agnostic** — the same steps run under cron, an ADO pipeline, or any scheduler. Use **GUID surrogate keys** rather than integer IDs so records are globally unique and **shardable across nodes/databases without key collisions**, enabling future horizontal distribution.

**Alternatives considered.**
- *Stateful, single-node scripts with integer IDs.* Rejected: integer keys collide across nodes and block sharding; statefulness blocks distribution.

**Consequences.** Future-proof toward distributed/multi-node processing; portable across schedulers. GUIDs are wider than integers (minor storage/index cost) — an accepted trade for global uniqueness.

---

## ADR-015 — Configurable per-field PII policy at silver

**Status:** Accepted

**Context.** Logs often contain sensitive data (passwords, usernames, countries). Different fields need different handling: some must vanish, some must support correlation without revealing identity, a few must be recoverable. The platform targets regulated industries, so this must be a first-class, governed concern.

**Decision.** PII handling is a **configurable per-field, per-source policy** declared in config, with three actions:
- **Redact / discard** — for secrets (e.g. passwords). Irreversible.
- **Keyed hash (HMAC with a secret salt)** — for fields used for correlation but never read back (e.g. usernames, countries). Preserves grouping (same value → same fingerprint) and enables cross-system joins on the hashed value, while resisting dictionary/brute-force attacks. This is **pseudonymisation, not anonymisation** — under regulations like GDPR the data remains personal data.
- **AES symmetric encryption** — only for the rare fields whose original value must be recoverable later. Introduces key-management responsibility.

The mechanism is built once; behaviour is data-driven per field.

**Alternatives considered.**
- *Plain (unkeyed) hashing for correlation fields.* Rejected: low-entropy values (usernames, countries) are trivially brute-forced via precomputed dictionaries.
- *Public-key encryption for in-pipeline scrubbing.* Rejected: designed for encrypt-by-one / decrypt-by-another scenarios; overkill and inappropriate for single-party pipeline scrubbing.

**Consequences.** Privacy-preserving correlation (HMAC) ties directly back to the cross-system use case; flexible governance per field. Reference implementation provides redaction and HMAC; AES is supported through the same policy hook where reversibility is required. Key/salt management must be handled outside the data store.

---

## ADR-016 — Layer state isolation (each layer depends only on its own state)

**Status:** Accepted

**Context.** The Medallion layers (bronze, silver, gold) are built as stateless, orchestrator-agnostic steps (ADR-014) that may, in future, run on separate nodes — and potentially against separate databases — so that, for example, one node ingests Windows logs into bronze, another ingests Apache logs, and a third moves data from bronze to silver. For this to be possible, no layer's logic may depend on reading another layer's data.

A concrete case forces the decision. On the *first* ingestion of a source, "process every file not yet processed" would sweep in the entire history of a shared location (which may hold many years of logs), most of which is unwanted. A cutoff is needed to bound that initial import — but only on the first run. The question is how a layer detects "this is a fresh source" without coupling to a downstream layer.

**Decision.** Each layer's logic reads **only its own state**, never a downstream layer's data. Specifically, the bronze ingestion detects a fresh source by checking whether `bronze_processed_logs` (bronze's own control table) has any prior rows for that source. If it has none, the source is treated as a first import and the per-source cutoff (`earliest_date` in config) is applied to bound it; if it has history, ingestion proceeds normally and processes all missing files. Bronze never inspects silver (or gold) to make this — or any other — decision.

**Alternatives considered.**
- *Detect a fresh source by checking whether the silver layer is empty for that source.* Rejected: this couples bronze to silver. If silver lived in a separate database or on a separate node, bronze would need cross-layer access purely to make an ingestion decision — exactly the dependency this architecture aims to avoid.
- *A separate `initial_import` script distinct from ongoing ingestion.* Rejected: it duplicates discovery/landing logic across two code paths and still needs a rule for when to run; folding the cutoff into normal ingestion (gated on bronze's own state) achieves the same outcome with one path.

**Consequences.** Layers are decoupled to the point of being physically separable — they can run on different nodes and even against different databases, communicating only through their own persisted state. This enables independent scaling and distribution per layer and per log type (consistent with ADR-014). The cost is that each layer must carry enough of its own state to make its decisions locally: bronze's control table must record processing history per source so "fresh vs. ongoing" is answerable from bronze alone. This is a deliberate, acceptable trade — a small amount of per-layer bookkeeping in exchange for full layer independence.

---

## Decisions intentionally deferred

These do not block the initial build and will be decided during implementation:

- **Specific embedding model.** Pin whichever is chosen (per ADR-012); the choice itself is deferred.
- **Specific chat/generation LLM.** Freely swappable (e.g. Mistral, Llama); no architectural impact.
- **Exact Power BI visuals.** Power BI is a downstream consumer of gold, not part of the core system.
- **Single vs. multiple silver tables beyond the common table.** Currently one common table + JSON overflow; revisit only if a concrete need arises.
- **Throughput/scale targets.** Reference implementation assumes incremental batch, not real-time streaming.

## Out of scope (reference implementation)

- Full anonymisation guarantees beyond pseudonymisation.
- Production key-management infrastructure for AES fields (interface provided; implementation environment-specific).
- Distributed multi-node execution (the design enables it via ADR-014; the reference build runs single-node).
