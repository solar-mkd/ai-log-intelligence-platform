# LogLens — AI Log Intelligence Platform

*An architecture-led platform for diagnosing errors and exceptions across heterogeneous systems, using a governed Medallion data pipeline and local Retrieval-Augmented Generation (RAG).*

---

## The problem

When something fails in a complex environment, the evidence is scattered. The same incident leaves traces in different systems — application logs, web-server logs, distributed-system logs — each in its own format, its own time zone, its own severity scheme. Answering a simple operational question is therefore hard:

> *"When did this error first appear, and what was happening in the other systems around that time?"*

Answering it normally means manually grepping several log stores, mentally normalizing timestamps, and eyeballing correlations. LogLens answers questions like this directly — by ingesting logs from any system into a common, governed model, and letting an LLM retrieve semantically and temporally relevant evidence across all of them.

## The idea

LogLens ingests heterogeneous logs into a **Medallion architecture** (bronze → silver → gold), extracts and segments exceptions, embeds each segment for semantic search, and serves cross-system diagnostic queries through a **local LLM + vector retrieval** — both as one-shot questions and as an interactive, memory-keeping chat. A BI layer sits on top for drill-down and slicing.

Two design choices set it apart from a typical RAG demo:

- **Structure-aware chunking.** Instead of cutting documents into fixed-size chunks with overlap (the common default), LogLens chunks on the *natural structure of an exception* — one embedding per exception signature, following the inner-exception chain, because that signature is the natural unit of *recurrence*. Similar errors from anywhere in the estate land near each other in vector space; shared-but-irrelevant stack frames don't create false matches. This produces far more precise retrieval for diagnostics.
- **Cross-system temporal correlation.** Retrieval is **hybrid**: filter by a time window, source, and severity, then rank by semantic similarity. Vector similarity alone cannot answer "what happened elsewhere around that time" — that's a temporal join across normalized, heterogeneous sources, designed in as a first-class concern.

## It works end-to-end

Ask a plain-English question; get an answer grounded in your actual log data — retrieved semantically, with real occurrence counts and time spans, and no hallucinated specifics:

```
$ python -m loglens.rag.ask_gold "what database errors are happening and how often?"

The database errors primarily involve connection timeouts from
System.Data.SqlClient.SqlException, across three timeout periods:
60000ms (142 occurrences), 30000ms (126), and 15000ms (121). There are
also deadlock errors from SqlException (several occurrences over the
period), and one Microsoft.EntityFrameworkCore.DbUpdateException
(753 occurrences).
```

The answer is generated *only* from segments the retrieval layer surfaced — the model is explicitly instructed to answer from that context and to decline when it lacks the information, so responses stay grounded in real data rather than invented.

For exploration, an **interactive chat** keeps conversation memory, so follow-up questions resolve against earlier answers (and each turn still re-retrieves fresh evidence from the logs):

```
$ python -m loglens.rag.chat_gold

you > what database errors are happening?
bot > The most frequent are SqlException connection timeouts and a
      large number of DbUpdateException save failures, plus some deadlocks.

you > when did the deadlocks first appear?
bot > The deadlock signatures first appear on 2026-05-01, recurring
      intermittently through 2026-05-08.

you > /context        (shows the segments retrieved for the last question)
you > exit
```

## Architecture at a glance

```
   Sources (Windows service logs first; Apache, HDFS, EVTX, … by design)
        │
        ▼
   ┌──────────┐   raw entries, hash-based idempotency, audit trail
   │  BRONZE  │   landing (+ archive by design), processed-logs control table,
   │          │   per-run observability (bronze_runs)
   └──────────┘
        │
        ▼
   ┌──────────┐   parsed & normalized entries
   │  SILVER  │   universal columns + JSON overflow
   │          │   UTC time (IANA zones), normalized + raw severity
   │          │   exceptions isolated; per-field PII policy (redact / HMAC)
   └──────────┘
        │
        ▼
   ┌──────────┐   exceptions → structure-aware signature segments
   │   GOLD   │   segments → embeddings (model-pinned, separate table)
   │          │   PostgreSQL + pgvector  →  hybrid retrieval
   └──────────┘
        │
        ▼
   RAG serving layer:  one-shot Q&A  +  interactive chat (memory)   [working]
        +              Power BI (drill-down, filtering, slicing)    [planned]
```

> Rendered diagrams (C4 container view and Medallion data flow) are in **[docs/architecture.md](docs/architecture.md)**. The reasoning behind every decision is in the **[Architecture Decision Records](docs/adr/ADRs.md)**.

## Getting started

**Prerequisites:** Docker, Python 3.11+, and (for the RAG layer) [Ollama](https://ollama.com) with a local model pulled (e.g. `ollama pull mistral`).

```bash
git clone <your-repo-url>
cd ai-log-intelligence-platform
docker compose up -d        # starts PostgreSQL + pgvector
```

That brings up the database with the vector extension enabled automatically. For the full walkthrough — Python environment, configuration, generating sample data, and running the whole pipeline for every configured source with one command (`python -m loglens.run_pipeline`), and querying it (`ask_gold` / `chat_gold`) — see **[docs/SETUP.md](docs/SETUP.md)**.

## Why it's built this way (design highlights)

This project leads with architecture; the code is the proof, not the point. The decisions that matter are captured as ADRs — a few of the notable ones:

- **Medallion layering with audit-first lineage** — raw fidelity, clean structure, and analytical products kept separate, each layer independently re-runnable. *(ADR-001, ADR-002)*
- **ELT, not ETL, at the boundary** — bronze lands raw entries with minimal transformation, so ingestion almost never fails; shaping happens downstream. *(ADR-001/002)*
- **Hash-based idempotency** at entry and file level — safe re-runs, correct handling of rotated logs. *(ADR-003)*
- **Pluggable parser contract** — adding a new log type means dropping in one module that implements the contract (parse *and* segment), with no change to the dispatcher or the gold orchestrator. This is what makes "flexible" a real property, not a hope. *(ADR-004)*
- **One common silver/gold model + JSON overflow** — type-specific fields don't fragment the schema, so cross-system queries stay simple. *(ADR-005)*
- **Normalize for querying, retain raw for audit** — dual-column pattern for time and severity. *(ADR-006, ADR-007)*
- **Structure-aware exception segmentation** — segment on the exception signature (the unit of recurrence), not fixed-size windows. *(ADR-009, ADR-010)*
- **Embed segment text only; metadata as filterable columns** — so identical errors from different systems match semantically, instead of being pushed apart by their metadata. *(ADR-011)*
- **Embedding-model provenance (pinning), in a separate table** — vectors are versioned by the model that produced them, enabling safe model migration and A/B evaluation. *(ADR-012)*
- **Stateless, orchestrator-agnostic steps with GUID keys** — runs single-node today, designed to shard across nodes tomorrow. *(ADR-014)*
- **Layer state isolation** — each layer reads only its own state, never a downstream layer's, so layers can be physically separated. *(ADR-016)*
- **Configurable per-field, per-source PII policy** — redact secrets, HMAC for privacy-preserving correlation (both implemented, fail-closed if the key is missing); AES reserved for the rare reversible case. *(ADR-015, ADR-018–020)*

Read the full set: **[docs/adr/ADRs.md](docs/adr/ADRs.md)**.

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Store (relational + vector) | PostgreSQL + pgvector (via Docker) |
| Similarity search | pgvector (`<=>` cosine), HNSW index, hybrid metadata filtering |
| Embeddings | Local `sentence-transformers` model (all-MiniLM-L6-v2, 384-dim; pinned + versioned) |
| Generation | Local LLM via Ollama (swappable; default Mistral) |
| BI | Power BI (downstream consumer of the gold layer — planned) |

**Platform-agnostic by design.** Core logic is kept storage- and scheduler-agnostic behind clean boundaries, so the same Medallion model maps directly onto Delta Lake + Unity Catalog on Databricks, or equivalent services on AWS — PostgreSQL + pgvector is the reference implementation, not a lock-in.

## Repository layout

```
.
├── config/              # configuration (config.example.yaml; real config git-ignored)
├── data/
│   ├── raw/             # bulk local data, per-source subfolders (git-ignored)
│   └── sample/          # small committed sample data
├── db/
│   ├── init/            # database init scripts (enables pgvector on first start)
│   └── schema/          # bronze / silver / gold schema (applied by init_db)
├── docs/
│   ├── SETUP.md         # how to run it, end-to-end
│   ├── architecture.md  # C4 + Medallion diagrams
│   └── adr/ADRs.md      # architecture decision records
├── src/loglens/
│   ├── parsers/         # pluggable parser contract + registry + per-type parsers
│   ├── pipeline/        # data-building steps: landing_bronze, silver,
│   │                    #   gold (segment), embed_gold
│   ├── rag/             # serving/query layer: retrieve_gold, ask_gold (one-shot),
│   │                    #   chat_gold (interactive, memory)
│   ├── storage/         # storage adapter boundary (PostgreSQL + pgvector)
│   └── init_db.py       # idempotent schema application
├── tools/               # developer utilities (synthetic log generator)
├── tests/
└── docker-compose.yml   # local PostgreSQL + pgvector
```

The split between **`pipeline/`** (steps that *build* the data, layer by layer) and **`rag/`** (the serving layer that *queries* the finished gold data) reflects a deliberate separation of processing from consumption.

## Status

A working end-to-end reference build: heterogeneous logs in, grounded natural-language answers out.

- **Working:** the full vertical slice — Windows service logs through bronze (landing, idempotency, per-run observability) → silver (parsing, UTC/severity normalization, exception isolation, JSON overflow, per-field PII redaction/HMAC) → gold (structure-aware exception segmentation → model-pinned embeddings → hybrid vector retrieval) → local-LLM RAG, available both as one-shot questions (`ask_gold`) and an interactive chat with conversation memory (`chat_gold`). Sources are config-driven, and the whole pipeline runs for every source with a single orchestrator command (`run_pipeline`). Reproducible from a clean clone (see SETUP.md), with a test suite and a synthetic log generator.
- **Next (documented, designed for):** the bronze archive/completion maintenance process; message templating for tighter exception clustering; AES action for reversible PII fields (the policy hook is in place).
- **Planned:** additional log types (Apache, HDFS, EVTX) via new parsers; Power BI dashboards; distributed multi-node execution.

The system was deliberately built as a thin vertical slice first (one log type, all the way through), then generalized — so the extensibility points are real and exercised, not theoretical.

## Test data

A synthetic log generator under [`tools/`](tools/) produces realistic Windows service (.NET) logs with recurring exception families, optional correlated incident bursts, and optional synthetic PII fields — so the platform's retrieval and correlation features have structured data to work against. All generated data is synthetic and safe to commit. See **[tools/README.md](tools/README.md)**.

## Configuration & secrets

Source definitions (paths, log type, time zone, header pattern, PII policy per field) live in config. **No secrets are committed.** Real config is git-ignored; a `config.example.yaml` template with placeholders shows the expected shape. The database connection string, keys, and salts are supplied via environment / local config only.

## Design & authorship

Architecture, data-platform design, and all engineering decisions in this repository are **designed by Dragan Solarov**. The implementation is **AI-assisted** — code is generated with AI tooling against the architecture and decisions documented here. The intent of the project is to demonstrate data-platform and data-architecture thinking: the design, the trade-offs, and the reasoning recorded in the ADRs.

## License

Released under the [MIT License](LICENSE) — free to use, modify, and distribute.