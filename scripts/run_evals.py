"""CLI for running LLM evaluation scenarios.

Usage:
    uv run python scripts/run_evals.py [--scenario chat|drafter|all]

Phase 3C scaffolding -- the actual eval scenarios will be filled in
incrementally. This script establishes the structure and output format.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime


def run_chat_evals() -> list[dict[str, str]]:
    """Placeholder chat evaluation scenarios."""
    return [
        {"name": "basic_greeting", "status": "skip", "reason": "Not implemented yet"},
        {"name": "provision_lookup", "status": "skip", "reason": "Not implemented yet"},
        {"name": "draft_context_qa", "status": "skip", "reason": "Not implemented yet"},
    ]


def run_drafter_evals() -> list[dict[str, str]]:
    """Placeholder drafter evaluation scenarios."""
    return [
        {"name": "vtk_structure_quality", "status": "skip", "reason": "Not implemented yet"},
        {"name": "clause_citations", "status": "skip", "reason": "Not implemented yet"},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LLM evaluation scenarios")
    parser.add_argument("--scenario", choices=["chat", "drafter", "all"], default="all")
    args = parser.parse_args()

    results: list[dict[str, str]] = []
    if args.scenario in ("chat", "all"):
        results.extend(run_chat_evals())
    if args.scenario in ("drafter", "all"):
        results.extend(run_drafter_evals())

    output = {
        "timestamp": datetime.now(UTC).isoformat(),
        "scenario": args.scenario,
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "failed": sum(1 for r in results if r["status"] == "fail"),
            "skipped": sum(1 for r in results if r["status"] == "skip"),
        },
    }
    json.dump(output, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
