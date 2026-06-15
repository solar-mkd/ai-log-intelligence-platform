# LogLens — AI Log Intelligence Platform

*An architecture-led reference platform for diagnosing errors and exceptions across heterogeneous systems, using a governed Medallion data pipeline and local Retrieval-Augmented Generation (RAG).*

---

## The problem

When something fails in a complex environment, the evidence is scattered. The same incident leaves traces in different systems — application logs, web-server logs, distributed-system logs — each in its own format, its own time zone, its own severity scheme. Answering a simple operational question is therefore hard:

> *"When did this error first appear, and what was happening in the other systems around that time?"*

Answering it normally means manually grepping several log stores, mentally normalizing timestamps, and eyeballing correlations. LogLens is built to answer questions like this directly — by ingesting logs from any system into a common, governed model, and letting an LLM retrieve semantically and temporally relevant evidence across all of them.

## The idea

LogLens ingests heterogeneous logs into a **Medallion architecture** (bronze → silver → gold), extracts and segments exceptions, embeds each segment for semantic search, and serves cross-system diagnostic queries through a **local LLM + vector retrieval**. A BI layer sits on top for drill-down and slicing.

Two design choices set it apart from a typical RAG demo:

- **Structure-aware chunking.** Instead of cutting documents into fixed-size chunks with overlap (the common default), LogLens chunks on the *natural structure of an exception* — one embedding per exception segment, because the segment is the natural unit of meaning. This produces far more precise retrieval for diagnostics.
- **Cross-system temporal correlation.** Retrieval is **hybrid**: filter by a time window and source, then rank by semantic similarity. Vector similarity alone cannot answer "what happened elsewhere around that time" — that's a temporal join across normalized, heterogeneous sources, designed in as a first-class concern.

## Architecture at a glance

```
   Sources (Windows service logs first; Apache, HDFS, EVTX, … by design)
        │
        ▼
   ┌──────────┐   raw entries, hash-based idempotency, audit trail
   │  BRONZE  │   landing → archive, processed-logs control table
   └──────────┘
        │
        ▼
   ┌──────────┐   parsed & normalized entries
   │  SILVER  │   universal columns + JSON overflow
   │          │   UTC time (IANA zones), normalized + raw severity
   │          │   per-field PII policy (redact / HMAC / encrypt)
   └──────────┘
        │
        ▼
   ┌──────────┐   reassembled exceptions → segments
   │   GOLD   │   structure-aware chunks → embeddings (model-pinned)
   │          │   PostgreSQL + pgvector  →  hybrid retrieval  →  local LLM
   └──────────┘
        │
        ▼
   RAG Q&A   +   Power BI (drill-down, filtering, slicing)
```

> Diagrams (C4 context/container and Medallion data flow) live in [`/docs`](docs/). The full reasoning behind every decision below is in the **[Architecture Decision Records](docs/adr/ADRs.md)**.

## Why it's built this way (design highlights)

This project leads with architecture; the code is the proof, not the point. The decisions that matter are captured as ADRs — a few of the notable ones:

- **Medallion layering with audit-first lineage** — raw fidelity, clean structure, and analytical products kept separate, each layer independently re-runnable. *(ADR-001, ADR-002)*
- **Hash-based idempotency** at entry and file level — safe re-runs, correct handling of rotated logs. *(ADR-003)*
- **Pluggable parser contract** — adding a new log type means dropping in one module that implements the contract, with no change to the dispatcher. This is what makes "flexible" a real property, not a hope. *(ADR-004)*
- **Normalize for querying, retain raw for audit** — dual-column pattern for time and severity. *(ADR-006, ADR-007)*
- **Embed segment text only; metadata as filterable columns** — so identical errors from different systems match semantically, instead of being pushed apart by their metadata. *(ADR-011)*
- **Embedding-model provenance (pinning)** — vectors are versioned by the model that produced them, enabling safe model migration and A/B evaluation. *(ADR-012)*
- **Stateless, orchestrator-agnostic steps with GUID keys** — runs single-node today, designed to shard across nodes tomorrow. *(ADR-014)*
- **Configurable per-field PII policy** — redact secrets, HMAC for privacy-preserving correlation, encrypt where reversibility is required. *(ADR-015)*

Read the full set: **[docs/adr/adrs.md](docs/adr/adrs.md)**.

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python |
| Store (relational + vector) | PostgreSQL + pgvector |
| Similarity search | pgvector (`<=>` cosine), HNSW index, hybrid metadata filtering |
| Embeddings | Local embedding model (pinned + versioned) |
| Generation | Local LLM (swappable, e.g. Mistral / Llama via Ollama) |
| BI | Power BI (downstream consumer of the gold layer) |
| Demo store | SQLite-compatible path for zero-setup local runs |

**Platform-agnostic by design.** Core logic is kept storage- and scheduler-agnostic behind clean boundaries, so the same Medallion model maps directly onto Delta Lake + Unity Catalog on Databricks, or equivalent services on AWS — PostgreSQL + pgvector is the reference implementation, not a lock-in.

## Status

This is an evolving reference build.

- **Built / in progress:** Windows service log ingestion → silver → exception segmentation → embeddings → RAG query (the first end-to-end vertical slice).
- **Designed for, not yet built:** additional log types (Apache, HDFS, EVTX), Power BI dashboards, distributed multi-node execution.

The system is deliberately built as a thin vertical slice first (one log type, all the way through), then generalized — so the extensibility points are real and exercised, not theoretical.

## Configuration & secrets

Source definitions (paths, log type, time zone, PII policy per field) live in config files. **No secrets are committed.** Real config is git-ignored; a `config.example.yaml` template with placeholders shows the expected shape. Connection strings, keys, and salts are supplied via environment / local config only.

## Design & authorship

Architecture, data-platform design, and all engineering decisions in this repository are **designed by Dragan Solarov**. The implementation is **AI-assisted** — code is generated with AI tooling against the architecture and decisions documented here. The intent of the project is to demonstrate data-platform and data-architecture thinking: the design, the trade-offs, and the reasoning recorded in the ADRs.

## License

Released under the [MIT License](LICENSE) — free to use, modify, and distribute.