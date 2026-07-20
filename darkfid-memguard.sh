#!/usr/bin/env bash
# darkfid-memguard.sh — restart darkfid before the cgroup OOM-kill can corrupt
# sled mid-write, and (separately, log-only for now) watch for the sled
# disconnect-growth so we can calibrate a real trigger for it.
set -euo pipefail

UNIT="darkfid.service"
CUR="/sys/fs/cgroup/system.slice/${UNIT}/memory.current"
STAT="/sys/fs/cgroup/system.slice/${UNIT}/memory.stat"

# --- Trigger 1: OOM-wall safety net (original, analyzed behaviour — DO NOT weaken) ---
# Restart before total memory reaches the cgroup wall (MemoryMax 4.2G), which
# would SIGKILL sled mid-write. This fires on ANY high-memory phase, including a
# legitimate from-zero initial sync where cache + anon both spike. This is the
# guard the 2026-07-14 post analysed; it stays exactly as it was.
WALL_MIB="${MEMGUARD_WALL_MIB:-4100}"

# --- Trigger 2: disconnect-growth WATCH (LOG ONLY — does not restart) ---
# The lilith seed outage put the node in a "limbo": anon heap elevated, total
# throttled just above MemoryHigh (~3.85G), load high for hours, but never
# reaching WALL_MIB — so Trigger 1 never fired and the node self-degraded
# without ever restarting. Anon (non-reclaimable heap) is the honest signal for
# this, BUT:
#   1. we have no recorded anon curve for a real outage yet — the exporter was
#      broken during the only one we've seen, so those snapshots were empty; and
#   2. a fresh from-zero initial sync also runs anon high, so a naive anon
#      threshold would restart mid-sync (a restart loop).
# So for now we only LOG when anon crosses a watch line, together with the
# process uptime (low uptime => probably initial sync, not the disease). Once the
# fixed exporter captures a real outage we calibrate MEMGUARD_ANON_WATCH_MIB from
# the recorded per-service anon series and decide whether to promote this to an
# actual restart trigger (gated on uptime past the initial sync).
ANON_WATCH_MIB="${MEMGUARD_ANON_WATCH_MIB:-2500}"

[[ -r "$CUR" ]] || exit 0
cur_mib=$(( $(cat "$CUR") / 1024 / 1024 ))

anon_mib=0
if [[ -r "$STAT" ]]; then
  anon="$(awk '/^anon /{print $2; exit}' "$STAT")"
  [[ "$anon" =~ ^[0-9]+$ ]] && anon_mib=$(( anon / 1024 / 1024 ))
fi

up_s=0
pid="$(pgrep -x darkfid | head -1 || true)"
[[ -n "$pid" ]] && up_s="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0)"

# Trigger 1: hard OOM-wall net — the only path that restarts.
if (( cur_mib > WALL_MIB )); then
  logger -t darkfid-memguard \
    "darkfid total ${cur_mib}MiB > ${WALL_MIB}MiB (OOM-wall net) — graceful restart"
  systemctl restart "$UNIT"
  exit 0
fi

# Trigger 2: watch-only. Record the disconnect-growth signature for calibration.
# Journal it with: journalctl -t darkfid-memguard
if (( anon_mib > ANON_WATCH_MIB )); then
  logger -t darkfid-memguard \
    "WATCH: darkfid anon ${anon_mib}MiB > ${ANON_WATCH_MIB}MiB, total ${cur_mib}MiB, uptime ${up_s}s — NOT restarting (calibration mode)"
fi
