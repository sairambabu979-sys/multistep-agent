"""Who approved this workflow run's release, from GitHub's own record of it.

Used by the `live run` workflow's release job (.github/workflows/live-run.yml):

    gh api repos/OWNER/REPO/actions/runs/RUN_ID/approvals \\
        | python scripts/live_run_approver.py live-release

It prints the login of the person who approved the deployment to that environment, and
exits 1 when nobody did. Then nothing may be approved in a person's name. That happens when
the environment has no required reviewer: GitHub creates an environment the first time a
job names it, with no protection at all, and runs the job straight away.

Only a user counts. A bot, or an approval for another environment, is ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def approver(approvals: list[dict[str, Any]], environment: str) -> str | None:
    for approval in approvals:
        if approval.get("state") != "approved":
            continue
        if not any(e.get("name") == environment for e in approval.get("environments") or []):
            continue
        user = approval.get("user") or {}
        if user.get("type") == "User" and user.get("login"):
            return str(user["login"])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("environment")
    args = parser.parse_args(argv)
    found = approver(json.load(sys.stdin), args.environment)
    if found is None:
        sys.stderr.write(
            f"nobody approved the {args.environment!r} deployment of this run. Give that "
            "environment a required reviewer (Settings, Environments) and run it again.\n"
        )
        return 1
    sys.stdout.write(found + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
