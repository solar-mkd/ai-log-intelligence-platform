"""Ask questions about the logs — RAG over gold segments (ADR-013).

Completes the Retrieval-Augmented Generation loop:
  1. RETRIEVE relevant exception segments for the question (retrieve_gold).
  2. AUGMENT: format those segments into a grounded prompt.
  3. GENERATE: send the prompt to a local LLM (via Ollama) and return its answer.

The prompt instructs the model to answer ONLY from the retrieved context and to
say when the context is insufficient — so answers are grounded in real log data,
not hallucinated. The model is configurable (--model), defaulting to 'mistral',
so different locally-pulled models can be used or compared.

Talks to Ollama's local HTTP API (default http://localhost:11434) using the
standard library only (urllib) — no extra dependency.

CLI: python -m loglens.pipeline.ask_gold "why are inventory syncs failing?"
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .retrieve_gold import retrieve, RetrievedSegment

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "mistral"
DEFAULT_TOP_K = 8

SYSTEM_INSTRUCTION = (
    "You are a log analysis assistant. Answer the user's question using ONLY the "
    "log error context provided below. The context lists distinct exception "
    "signatures retrieved from the system's logs, with how many times each "
    "occurred and the time span over which they appeared. "
    "Base your answer strictly on this context. If the context does not contain "
    "enough information to answer, say so plainly rather than guessing. Do not "
    "invent errors, counts, or causes that are not in the context. Be concise "
    "and specific, and refer to the actual error types and counts when relevant."
)


def _format_context(results: list[RetrievedSegment], distinct: bool) -> str:
    """Render retrieved segments as readable context for the prompt."""
    if not results:
        return "(no relevant log errors were found)"
    lines = []
    for i, r in enumerate(results, 1):
        if distinct:
            span = ""
            if r.first_seen_utc and r.last_seen_utc:
                span = (f", seen from {r.first_seen_utc:%Y-%m-%d} "
                        f"to {r.last_seen_utc:%Y-%m-%d}")
            count = f"{r.occurrences} occurrence(s)" if r.occurrences else ""
            lines.append(f"{i}. {r.segment_text}  [{count}{span}]")
        else:
            when = f"{r.event_time_utc:%Y-%m-%d %H:%M}" if r.event_time_utc else ""
            where = "/".join(x for x in [r.source_id, r.severity] if x)
            lines.append(f"{i}. {r.segment_text}  [{when} {where}]")
    return "\n".join(lines)


def _build_prompt(question: str, context: str) -> str:
    return (
        f"{SYSTEM_INSTRUCTION}\n\n"
        f"--- LOG ERROR CONTEXT ---\n{context}\n--- END CONTEXT ---\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )


def _call_ollama(prompt: str, model: str, timeout: int = 120) -> str:
    """Send the prompt to Ollama's generate API and return the response text."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,            # get the whole answer in one response
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_URL}. Is it running, and is the "
            f"model '{model}' pulled? Original error: {exc}"
        ) from exc


def ask(
    question: str,
    *,
    model: str = DEFAULT_MODEL,
    top_k: int = DEFAULT_TOP_K,
    distinct: bool = True,
    source_id: str | None = None,
    severity: str | None = None,
    log_type: str | None = None,
    from_utc: datetime | None = None,
    to_utc: datetime | None = None,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Retrieve context for the question, ask the LLM, and return the answer
    plus the retrieved context (so the caller can show its sources)."""
    results = retrieve(
        question, top_k=top_k, distinct=distinct,
        source_id=source_id, severity=severity, log_type=log_type,
        from_utc=from_utc, to_utc=to_utc, dsn=dsn,
    )
    context = _format_context(results, distinct=distinct)
    prompt = _build_prompt(question, context)
    answer = _call_ollama(prompt, model=model)
    return {"answer": answer, "context": context, "results": results}


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Ask a natural-language question about the logs (RAG).",
    )
    p.add_argument("question", help="The question to answer from the logs.")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Ollama model to use. Default: {DEFAULT_MODEL}")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                   help=f"How many retrieved segments to feed the model. Default: {DEFAULT_TOP_K}")
    p.add_argument("--all-occurrences", action="store_true",
                   help="Feed individual occurrences instead of distinct signatures.")
    p.add_argument("--source-id", default=None)
    p.add_argument("--severity", default=None)
    p.add_argument("--log-type", default=None)
    p.add_argument("--from", dest="from_utc", default=None, help="ISO datetime lower bound.")
    p.add_argument("--to", dest="to_utc", default=None, help="ISO datetime upper bound.")
    p.add_argument("--show-context", action="store_true",
                   help="Also print the retrieved context that was sent to the model.")
    args = p.parse_args(argv)

    out = ask(
        args.question,
        model=args.model, top_k=args.top_k, distinct=not args.all_occurrences,
        source_id=args.source_id, severity=args.severity, log_type=args.log_type,
        from_utc=_parse_dt(args.from_utc), to_utc=_parse_dt(args.to_utc),
    )

    if args.show_context:
        print("\n--- Retrieved context ---")
        print(out["context"])
        print("--- End context ---")
    print(f"\n{out['answer']}\n")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))