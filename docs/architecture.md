# Architecture

This document gives the high-level architecture of LogLens. It complements the [Architecture Decision Records](adr/adrs.md), which capture *why* each decision was made; this page shows *what* the system is and *how* data moves through it.

The diagrams below are written in [Mermaid](https://mermaid.js.org/) and render natively on GitHub.

---

## 1. Container view (C4 level 2)

A container-level view of LogLens: what the major pieces are and how they talk to each other. Log sources feed a pluggable ingestion and parsing layer, which writes into a single PostgreSQL + pgvector store organized as bronze, silver, and gold. A retrieval service combines time/source filtering with vector similarity, calls a local LLM, and answers user questions. Power BI reads the gold layer directly for drill-down and slicing.

```mermaid
flowchart TB
    sources["Log sources<br/><i>Windows service logs first;<br/>Apache, HDFS, EVTX by design</i>"]
    ingest["Ingestion + parsers<br/><i>Pluggable contract, one module per log type</i>"]

    subgraph store["PostgreSQL + pgvector — single store (GUID keys)"]
        direction TB
        bronze["Bronze<br/><i>Raw entries · landing/archive · control table</i>"]
        silver["Silver<br/><i>Parsed · UTC · PII policy · JSON overflow</i>"]
        gold["Gold<br/><i>Segments · embeddings · serving views</i>"]
        bronze --> silver --> gold
    end

    retrieval["Retrieval service<br/><i>Hybrid: time/source filter + similarity</i>"]
    llm["Local LLM<br/><i>Swappable (e.g. Mistral, Llama)</i>"]
    user["User<br/><i>Cross-system Q&amp;A</i>"]
    powerbi["Power BI<br/><i>Drill-down · filtering · slicing</i>"]

    sources --> ingest --> bronze
    gold --> retrieval
    retrieval --> llm
    retrieval --> user
    gold --> powerbi

    classDef src fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A;
    classDef proc fill:#EEEDFE,stroke:#534AB7,color:#26215C;
    classDef br fill:#E1F5EE,stroke:#0F6E56,color:#04342C;
    classDef ag fill:#FAEEDA,stroke:#854F0B,color:#412402;
    classDef ll fill:#EAF3DE,stroke:#3B6D11,color:#173404;

    class sources,user src;
    class ingest,retrieval proc;
    class bronze,silver br;
    class gold,powerbi ag;
    class llm ll;
```

**How to read it.** The store is one database, not three — the Medallion layers are logical, sharing PostgreSQL + pgvector ([ADR-013](adr/adrs.md)). The chat LLM hangs off the retrieval service as a swappable component, while the embedding model (used during ingestion into gold) is a pinned, versioned dependency ([ADR-012](adr/adrs.md)). Power BI is a consumer of gold, not part of the core pipeline.

---

## 2. Medallion data flow

The journey of a log entry through the layers, and where each transformation happens. Raw entries land in bronze with hashing for idempotency and a control table for traceability. They are parsed and normalized into silver. In gold, exceptions are reassembled and split into structure-aware segments, each segment is embedded, and hybrid retrieval serves cross-system questions.

```mermaid
flowchart TB
    raw["Raw log files"]

    subgraph bronze["BRONZE — raw fidelity · idempotency · audit"]
        direction TB
        b1["Landing → archive<br/>entry hash + file hash"]
        b2["Processed-logs control table"]
    end

    subgraph silver["SILVER — parsed · normalized · governed"]
        direction TB
        s1["Universal columns + JSON overflow<br/>UTC time (IANA) · raw + normalized severity"]
        s2["Per-field PII policy: redact / HMAC / encrypt<br/>is_exception flag · parser_version"]
    end

    subgraph gold["GOLD — analytical product · RAG-ready"]
        direction TB
        g1["Reassemble exceptions<br/>structure-aware segments"]
        g2["Embed per segment<br/>model-pinned vectors"]
        g3["Hybrid retrieval: filter by time/source,<br/>rank by similarity → top-K → local LLM"]
        g1 --> g2 --> g3
    end

    answer["Cross-system answers"]

    raw --> bronze
    bronze --> silver
    silver --> gold
    gold --> answer

    classDef neutral fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A;
    classDef br fill:#E1F5EE,stroke:#0F6E56,color:#04342C;
    classDef sv fill:#E6F1FB,stroke:#185FA5,color:#042C53;
    classDef gd fill:#FAEEDA,stroke:#854F0B,color:#412402;

    class raw,answer neutral;
    class b1,b2 br;
    class s1,s2 sv;
    class g1,g2,g3 gd;
```

**How to read it.** Each layer is independently re-runnable, and the boundaries between them are where idempotency and lineage are enforced. The single most important design point is in gold: exceptions are *reassembled* from multi-line entries, then split into segments that become the unit of embedding ([ADR-009](adr/adrs.md), [ADR-010](adr/adrs.md)). Retrieval is deliberately **hybrid** — time and source filtering combined with vector similarity — because the headline use case ("what happened in other systems around that time") is a temporal join, which pure vector similarity cannot answer on its own ([ADR-011](adr/adrs.md), [ADR-013](adr/adrs.md)).

---

## Where to go next

- The reasoning behind every decision above: **[Architecture Decision Records](adr/adrs.md)**
- Project overview and status: **[README](../README.md)**
