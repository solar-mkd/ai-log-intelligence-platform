
---

## ADR-018 — Embedding model: all-MiniLM-L6-v2 (384-dim), pinned

**Status:** Accepted (resolves a previously deferred decision)

**Context.** ADR-012 established that the embedding model is a controlled, pinned dependency and deferred the specific choice. A concrete model must now be selected to produce the segment vectors. The embedded units are short, technical exception **signatures** (type + message), not long nuanced prose.

**Decision.** Use the local `sentence-transformers` model **`all-MiniLM-L6-v2`**, producing **384-dimensional** vectors, run on CPU. Each embedding row records the model name and version (ADR-012), so this choice is pinned and auditable.

**Alternatives considered.**
- *A higher-dimensional model (e.g. 768/1024-dim, such as a larger BGE or E5 model).* Rejected as the default: higher dimensions add storage and search cost and mainly help on long, semantically nuanced text. The embedded units here are short, distinctive, technical signatures whose discriminative signal is coarse — a 384-dim model captures it well, and the extra capacity would be largely unused.
- *A hosted/API embedding model.* Rejected: conflicts with the local, reproducible, no-external-dependency design.

**Consequences.** Fast, lightweight, fully local embeddings well-matched to the text. Because embeddings live in a separate, model-pinned table (ADR-012), this is not a one-way door: the *same* segments can later be embedded by a higher-dimensional model into a parallel table and compared empirically on real data, without disturbing the existing vectors. The 384-dim choice is a well-fitted starting point, not a permanent constraint.

---

## ADR-019 — The exception signature is the segment (recurrence-driven)

**Status:** Accepted (refines ADR-010)

**Context.** ADR-010 chose structure-aware chunking — one chunk per exception segment — but left open *what precisely constitutes a segment*. The goal of segmentation, for this platform, is retrieval of *similar* errors: a segment is useful only if it **recurs** across different exceptions in a semantically meaningful way, so vector search returns genuinely related failures rather than noise.

**Decision.** The segment is the exception **signature — type + message — one per exception in the inner-exception chain** (following the `.NET` `--->` / "End of inner exception" boundaries; analogous markers for other systems). The outer exception is segment 0; each inner exception is a further segment. **Stack frames are retained** in silver's full `exception_text` for display and investigation, but are **not emitted as separate embedded segments.**

**Alternatives considered.**
- *Embed the whole exception as one unit.* Rejected: too coarse; mixes the recurring signature with non-recurring detail, blurring similarity.
- *Embed each stack frame as its own segment.* Rejected: frames recur as *shared code paths* across unrelated errors, producing **false similarity** (errors ranked close merely because they touch the same method). This floods the vector store with low-signal segments and degrades retrieval — the opposite of the goal.

**Consequences.** Segments are the meaningful unit of recurrence: similar failures (e.g. the same deadlock with a different process ID) land close in vector space, while shared-but-irrelevant frames do not create spurious matches. Per-system signature/boundary detection lives in each parser (ADR-004). A known follow-on refinement is **message templating** (normalizing variable parameters such as IDs to placeholders) to tighten recurrence further; noted as future work.

---

## ADR-020 — Pattern detection by analytics; the LLM explains, it does not detect

**Status:** Accepted

**Context.** A natural extension is "find patterns where errors occur" (temporal spikes, recurring sequences, co-occurrence across systems). It is tempting to ask the LLM to discover these patterns directly over the data.

**Decision.** **Detection is performed by deterministic analytics, not the LLM.** Temporal/frequency patterns come from SQL aggregation over silver/gold (time bucketing, counts, co-occurrence); semantic grouping comes from clustering over the segment embeddings. The **LLM's role is to interpret and explain** detected patterns and to answer questions over a *bounded, retrieved* set of segments — never to scan the whole dataset for patterns.

**Alternatives considered.**
- *Ask the LLM to find patterns across the dataset.* Rejected: LLMs cannot reliably process large volumes and do not compute deterministically — they would confidently invent patterns (hallucinate). In a diagnostic context a confidently-wrong pattern is worse than none.

**Consequences.** Pattern *finding* is trustworthy and reproducible (statistics/clustering); the LLM adds value where it is strong — natural-language interpretation, summarization, and reasoning over retrieved evidence. This division also keeps the LLM swappable and the analytics independently testable. It defines a clear boundary for what the RAG layer answers (retrieval-grounded questions) versus what the BI/analytics layer answers (aggregate trends).
