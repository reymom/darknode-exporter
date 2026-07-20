#!/usr/bin/env bash
# darkfid-memguard.sh — gracefully restart darkfid when its ANONYMOUS memory
# grows unbounded, before the cgroup OOM-kill can corrupt sled mid-write.
#
# Why anon and not memory.current:
#   memory.current = anonymous heap + reclaimable page cache. The sled cache is
#   healthy and large (it fills toward MemoryHigh by design), so a total-memory
#   threshold either fires on a healthy node or — because MemoryHigh throttles
#   the total below the ceiling — never fires at all, leaving the node degraded
#   for hours. That "limbo" is exactly what happened during the lilith seed
#   outage: anon climbed to ~3.6G while the total stayed pinned at MemoryHigh
#   and never reached the old 4100 MiB trigger.
#   Anonymous memory is the node's own heap and is NOT reclaimable, so it is the
#   honest signal for the sled disconnect-growth. Healthy anon sits ~1.6G; the
#   disconnect-growth pushes it well past 2.5-3G.
#
# The threshold is intentionally conservative. We record per-service anon in
# every snapshot, so calibrate LIMIT_MIB from the recorded time series (see the
# Aug test session) rather than guessing.
set -euo pipefail

UNIT="darkfid.service"
STAT="/sys/fs/cgroup/system.slice/${UNIT}/memory.stat"
LIMIT_MIB="${MEMGUARD_ANON_MIB:-2800}"   # healthy ~1.6G; disconnect-growth blows past this

[[ -r "$STAT" ]] || exit 0
anon="$(awk '/^anon /{print $2; exit}' "$STAT")"
[[ "$anon" =~ ^[0-9]+$ ]] || exit 0

mib=$(( anon / 1024 / 1024 ))
if (( mib > LIMIT_MIB )); then
  logger -t darkfid-memguard \
    "darkfid anon at ${mib}MiB > ${LIMIT_MIB}MiB (sled disconnect-growth) — graceful restart"
  systemctl restart "$UNIT"
fi
