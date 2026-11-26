"""Run the README's quickstart as a newcomer would (PLAN M10: it works from a clean machine).

Every ```bash block of the README's "Try it" section, up to the first one that needs a
model key, is run in order from the repository root, exactly as written, against a journal
of its own (``MSA_DB``), so a ``demo`` run already in ``.msa/runs.db`` neither breaks it nor
gains a twin. CI runs this on a fresh runner with a fresh checkout, uv and nothing else: no
cache, no ripgrep, no key.

    python3 scripts/quickstart_check.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTION = "## Try it"
NEEDS_A_KEY = "ANTHROPIC_API_KEY"  # the README says so before the first command that does
_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def commands(readme: str) -> list[str]:
    """The quickstart's commands that need no model key, in order."""
    if SECTION not in readme:
        return []
    section = readme.split(SECTION, 1)[1]
    keyless = section.split(NEEDS_A_KEY, 1)[0]
    return [block.strip() for block in _BLOCK.findall(keyless)]


def main() -> int:
    found = commands((ROOT / "README.md").read_text(encoding="utf-8"))
    if not found:
        sys.stderr.write(f"no commands found under {SECTION!r} in README.md\n")
        return 1
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "MSA_DB": str(Path(scratch) / "runs.db")}
        for command in found:
            sys.stdout.write(f"$ {command}\n")
            sys.stdout.flush()
            # The README's own words, run the way a reader would paste them into a shell.
            done = subprocess.run(command, shell=True, cwd=ROOT, env=env, check=False)
            if done.returncode != 0:
                sys.stderr.write(f"::error::the README quickstart failed at: {command}\n")
                return done.returncode
    sys.stdout.write(f"The quickstart's {len(found)} commands that need no key all succeeded.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
