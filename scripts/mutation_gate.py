"""Fail when mutation testing of the core leaves a mutant alive (``make mutation``, nightly).

Reads the stats ``mutmut export-cicd-stats`` writes. The budget is zero surviving mutants:
the suite kills every one today, so a survivor means new behavior arrived without a test
that would notice it breaking. Timeouts count as killed (the mutant made the code hang,
and the test noticed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_STATS = Path("mutants/mutmut-cicd-stats.json")


def problems(stats: dict[str, int], *, max_survived: int) -> list[str]:
    found: list[str] = []
    if stats.get("total", 0) == 0:
        found.append("no mutants were tested: did `mutmut run` run?")
    if stats.get("survived", 0) > max_survived:
        found.append(f"{stats['survived']} mutant(s) survived (budget {max_survived})")
    if stats.get("no_tests", 0):
        found.append(f"{stats['no_tests']} mutant(s) in code no selected test reaches")
    found += [
        f"{stats[key]} {key.replace('_', ' ')} result(s): rerun before trusting"
        for key in ("suspicious", "segfault", "check_was_interrupted_by_user")
        if stats.get(key, 0)
    ]
    return found


def score(stats: dict[str, int]) -> float:
    tested = stats.get("total", 0) - stats.get("skipped", 0)
    detected = stats.get("killed", 0) + stats.get("timeout", 0)
    return 100.0 * detected / tested if tested > 0 else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    parser.add_argument("--max-survived", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        stats: dict[str, int] = json.loads(args.stats.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.stderr.write(f"{args.stats} not found: run `mutmut export-cicd-stats` first\n")
        return 1
    sys.stdout.write(
        f"mutation score {score(stats):.1f}%: {stats.get('killed', 0)} killed, "
        f"{stats.get('timeout', 0)} timed out, {stats.get('survived', 0)} survived "
        f"of {stats.get('total', 0)}\n"
    )
    found = problems(stats, max_survived=args.max_survived)
    for problem in found:
        sys.stderr.write(f"error: {problem}\n")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
