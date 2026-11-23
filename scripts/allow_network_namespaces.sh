#!/usr/bin/env bash
# Let an unprivileged user create the network namespace that agent commands and integration
# suites run in (`unshare -rn`), then prove it works. Used by CI and the live run, on
# GitHub's throwaway runners only.
#
# Ubuntu 24.04 (GitHub's ubuntu-latest) has AppArmor refuse unprivileged user namespaces by
# default, so `isolate_network = "auto"` quietly ran everything with network there. This
# lifts that restriction for the life of the runner. A host you keep decides this for
# itself; set `isolate_network = "always"` there to refuse to run without isolation.
set -euo pipefail
knob=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
if [ -e "$knob" ]; then
  echo "kernel.apparmor_restrict_unprivileged_userns was $(cat "$knob")"
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
unshare --user --map-root-user --net -- true
echo "ok: unshare --user --map-root-user --net works here"
