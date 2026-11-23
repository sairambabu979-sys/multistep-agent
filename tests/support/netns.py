"""Whether this machine can take the network away from a command (``unshare -rn``).

Agent commands and integration suites run in a network namespace of their own where the
kernel lets an unprivileged user create one. Where it does not, the tests of that isolation
skip. With ``MSA_REQUIRE_NETNS=1`` (set in CI, after the workflow allows the namespaces) a
machine that cannot create one fails those tests instead, so network isolation is never
silently untested.
"""

from __future__ import annotations

import os

import pytest

REQUIRED = os.environ.get("MSA_REQUIRE_NETNS") == "1"


def isolation(available: bool) -> bool:
    """``available``, the probe's answer; with ``MSA_REQUIRE_NETNS=1``, False fails the test."""
    if not available and REQUIRED:
        pytest.fail(
            "MSA_REQUIRE_NETNS=1, but this machine cannot create an unprivileged network "
            "namespace (unshare --user --map-root-user --net)"
        )
    return available


def needs_isolation(available: bool) -> None:
    if not isolation(available):
        pytest.skip("this kernel does not let an unprivileged user create a network namespace")
