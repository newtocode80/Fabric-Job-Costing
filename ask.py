#!/usr/bin/env python3
"""Ask the job costing model a question from the command line.

    python ask.py "What is our total cost by cost type?"
    python ask.py --file evals/smoke.txt      # one question per line

Prints the answer, then every run_sql call the agent made with its SQL and rows.
Requires Anthropic API credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
`ant auth login` profile).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anthropic

from jobcosting.agent import Agent, Answer
from jobcosting.engine import DuckDBEngine

RULE = "=" * 78


def show(answer: Answer) -> None:
    print(RULE)
    print(f"Q: {answer.question}")
    print(RULE)
    print()
    print(answer.answer or "(no answer)")
    print()

    if not answer.tool_calls:
        print("-- no run_sql calls --")
    for i, call in enumerate(answer.tool_calls, 1):
        status = "ok" if call.ok else ("REJECTED" if call.rejected else "FAILED")
        print(f"--- run_sql call {i} of {len(answer.tool_calls)} [{status}] ---")
        # Show what actually ran. When a guardrail rewrote the query, show the
        # model's version too, so the panel never misrepresents either one.
        shown = call.executed_sql or call.sql
        print(shown.strip())
        if call.executed_sql and call.executed_sql.strip() != call.sql.strip():
            print(f"  (as written by the model: {call.sql.strip()})")
        print()
        if not call.ok:
            print(f"  {'rejected' if call.rejected else 'error'}: {call.error}")
        else:
            print(f"  {' | '.join(call.columns)}")
            for row in call.rows[:20]:
                print(f"  {' | '.join(str(v) for v in row)}")
            if len(call.rows) > 20:
                print(f"  ... {len(call.rows) - 20} more of {len(call.rows)} rows")
            if call.truncated:
                print("  (the model saw a truncated result)")
        print()

    if answer.error:
        print(f"ERROR: {answer.error}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="*", help="the question to ask")
    parser.add_argument("--file", type=Path, help="file of questions, one per line")
    args = parser.parse_args()

    questions = []
    if args.file:
        questions += [
            line.strip()
            for line in args.file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if args.question:
        questions.append(" ".join(args.question))
    if not questions:
        parser.error("give a question, or --file")

    try:
        client = anthropic.Anthropic()
        # The SDK resolves credentials lazily, so an unauthenticated client only
        # fails on the first request. Check now, with a message that says what to do.
        if not (client.api_key or client.auth_token):
            print(
                "No Anthropic API credentials found.\n"
                "Set one of:\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                "  export ANTHROPIC_AUTH_TOKEN=...\n"
                "  ant auth login          (stores a profile the SDK reads)\n"
                "In a Claude Code cloud session, add ANTHROPIC_API_KEY to the\n"
                "environment's variables and start a NEW session -- a running\n"
                "session copies the values once, at startup.\n"
                f"\nRequests would go to: {client.base_url}",
                file=sys.stderr,
            )
            return 2
        agent = Agent(engine=DuckDBEngine(), client=client)
    except Exception as exc:
        print(f"Could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    failed = False
    for question in questions:
        answer = agent.ask(question)
        show(answer)
        failed = failed or bool(answer.error)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
