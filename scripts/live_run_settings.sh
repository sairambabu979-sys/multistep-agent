#!/usr/bin/env bash
# The live run's settings, written to "$1": msa.toml, with two changes.
#
# - The sandbox runs ruff, mypy and pytest with this checkout's Python. The fixture has no
#   environment of its own; the integration tests do the same.
# - Network isolation is required, not attempted: both sections say "always", so a runner
#   that cannot take the network away fails the run instead of running it with network.
#
# Used by the run job of .github/workflows/live-run.yml. The release job resumes the run with
# the copy saved with its state, so it runs under exactly these settings.
set -euo pipefail
out=$1
sed -e "s|^launcher = .*|launcher = [\"$GITHUB_WORKSPACE/.venv/bin/python\", \"-m\"]|" \
  -e 's|^isolate_network = .*|isolate_network = "always"|' \
  msa.toml > "$out"
grep -n -e '^launcher' -e '^isolate_network' "$out"
test "$(grep -c '^isolate_network = "always"$' "$out")" -eq 2
uv run msa --config "$out" config check
