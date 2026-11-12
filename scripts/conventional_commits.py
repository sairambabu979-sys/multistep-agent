"""Check that commit subjects follow Conventional Commits.

The changelog is generated from commit subjects (``make changelog``), so a subject that
does not parse would silently drop out of it. This check keeps generation reliable:

    python scripts/conventional_commits.py --range origin/main..HEAD   # CI
    python scripts/conventional_commits.py --message-file .git/COMMIT_EDITMSG  # commit-msg hook
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

TYPES = ("feat", "fix", "perf", "refactor", "docs", "test", "build", "ci", "chore", "revert")
SUBJECT = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([a-z0-9-]+\))?!?: \S.*$")
EXEMPT = re.compile(r"^(?:Merge |Revert \"|fixup! |squash! )")


def invalid(subjects: Iterable[str]) -> list[str]:
    return [s for s in subjects if not (SUBJECT.match(s) or EXEMPT.match(s))]


def _subjects_in(revision_range: str) -> list[str]:
    log = subprocess.run(
        ["git", "log", "--format=%s", revision_range],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in log.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--range", help="git revision range, e.g. origin/main..HEAD")
    source.add_argument("--message-file", type=Path, help="commit message file (commit-msg hook)")
    args = parser.parse_args(argv)

    if args.message_file:
        subjects = args.message_file.read_text(encoding="utf-8").splitlines()[:1]
    else:
        subjects = _subjects_in(args.range)
    bad = invalid(subjects)
    for subject in bad:
        sys.stderr.write(f"not a conventional commit subject: {subject!r}\n")
    if bad:
        sys.stderr.write(f"expected '<type>[(scope)][!]: <description>' with type in {TYPES}\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
