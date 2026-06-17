"""Interactive, conversational RAG over the logs (ADR-013).

A stateful companion to ask_gold.py: it keeps conversation memory so follow-up
questions resolve against earlier turns ("when did the first one start?").

How it works each turn:
  * RE-RETRIEVE fresh segments for the new question (so the conversation can
    shift topic and still pull relevant log data), and
  * include the recent CONVERSATION HISTORY in the prompt, so the model can
    resolve references to earlier answers.

Reuses ask_gold's lower-level pieces (retrieval, context formatting, Ollama
call) — ask_gold.py is left completely untouched. The only thing different here
is a conversation-aware prompt plus the interactive loop and memory.

The model loads once at startup, so after the first (slower) turn, follow-ups
are fast — the practical advantage of an interactive session over repeated
one-shot commands.

Commands inside the session:
  exit / quit   end the session
  reset         clear conversation memory (start a fresh conversation)
  /context      show the segments retrieved for the most recent question

CLI: python -m loglens.pipeline.chat_gold
     python -m loglens.pipeline.chat_gold --model mistral --top-k 8
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Reuse ask_gold's building blocks; ask_gold.py itself is unchanged.
from .ask_gold import (
    DEFAULT_MODEL,
    DEFAULT_TOP_K,
    _call_ollama,
    _format_context,
)
from .retrieve_gold import retrieve

# How many recent (question, answer) exchanges to keep in the prompt. Older
# turns are dropped so the prompt does not grow unbounded (local models have
# limited context). Recent turns are what follow-ups usually reference.
MAX_HISTORY_TURNS = 5

CHAT_SYSTEM_INSTRUCTION = (
    "You are a log analysis assistant having a conversation with an engineer. "
    "Answer the user's CURRENT question using the log error context provided for "
    "it, and use the prior conversation only to understand follow-up references "
    "(e.g. 'the first one', 'that error'). Base factual claims strictly on the "
    "provided log context — do not invent errors, counts, or causes. If the "
    "context lacks the information, say so plainly. Be concise and specific."
)


def _build_chat_prompt(history: list[tuple[str, str]], question: str, context: str) -> str:
    """Build a conversation-aware prompt: system instruction + recent history +
    fresh context for the current question."""
    parts = [CHAT_SYSTEM_INSTRUCTION, ""]

    if history:
        parts.append("--- CONVERSATION SO FAR ---")
        for q, a in history:
            parts.append(f"User: {q}")
            parts.append(f"Assistant: {a}")
        parts.append("--- END CONVERSATION ---")
        parts.append("")

    parts.append("--- LOG ERROR CONTEXT FOR THE CURRENT QUESTION ---")
    parts.append(context)
    parts.append("--- END CONTEXT ---")
    parts.append("")
    parts.append(f"Current question: {question}")
    parts.append("")
    parts.append("Answer:")
    return "\n".join(parts)


def chat_loop(
    *,
    model: str = DEFAULT_MODEL,
    top_k: int = DEFAULT_TOP_K,
    distinct: bool = True,
    source_id: str | None = None,
    severity: str | None = None,
    log_type: str | None = None,
    dsn: str | None = None,
) -> None:
    """Run the interactive conversational loop until the user exits."""
    print("LogLens interactive chat. Ask about your logs.")
    print("Commands: 'exit'/'quit' to leave, 'reset' to clear memory, "
          "'/context' to show the last retrieval.\n")

    history: list[tuple[str, str]] = []   # recent (question, answer) pairs
    last_context: str = ""                # context from the most recent question

    while True:
        try:
            question = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return

        if not question:
            continue

        cmd = question.lower()
        if cmd in ("exit", "quit"):
            print("Bye.")
            return
        if cmd == "reset":
            history.clear()
            last_context = ""
            print("(conversation memory cleared)\n")
            continue
        if cmd == "/context":
            print("\n--- Last retrieved context ---")
            print(last_context or "(nothing retrieved yet)")
            print("--- end ---\n")
            continue

        # Fresh retrieval for THIS question (re-retrieve each turn).
        results = retrieve(
            question, top_k=top_k, distinct=distinct,
            source_id=source_id, severity=severity, log_type=log_type, dsn=dsn,
        )
        last_context = _format_context(results, distinct=distinct)

        prompt = _build_chat_prompt(history, question, last_context)

        try:
            answer = _call_ollama(prompt, model=model)
        except RuntimeError as exc:
            print(f"\n[error] {exc}\n")
            continue

        print(f"\nbot > {answer}\n")

        # Remember this turn, keeping only the most recent MAX_HISTORY_TURNS.
        history.append((question, answer))
        if len(history) > MAX_HISTORY_TURNS:
            history.pop(0)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Interactive conversational RAG over the logs (with memory).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Ollama model. Default: {DEFAULT_MODEL}")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                   help=f"Segments retrieved per question. Default: {DEFAULT_TOP_K}")
    p.add_argument("--all-occurrences", action="store_true",
                   help="Use individual occurrences instead of distinct signatures.")
    p.add_argument("--source-id", default=None)
    p.add_argument("--severity", default=None)
    p.add_argument("--log-type", default=None)
    args = p.parse_args(argv)

    chat_loop(
        model=args.model, top_k=args.top_k, distinct=not args.all_occurrences,
        source_id=args.source_id, severity=args.severity, log_type=args.log_type,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
