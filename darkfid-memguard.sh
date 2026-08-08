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
# process uptime (low uptime => probably initial sync, not the disease).
#
# 2026-08-02 — CALIBRATED, and the answer to "promote this to a restart
# trigger?" is no. Fourteen days of recorded per-service anon (18,380 samples,
# three real wall episodes) say anon parks on a hard plateau at 3905 MiB:
# p95 3901, p99 3905, p99.5 3905. That is a ceiling, not a distribution, and
# there is no room to stand above it:
#
#     threshold    samples over    noise    warning before the wall
#     3850 MiB          1410       84.9%    21–359 min
#     3900 MiB          1087       86.1%    20–359 min
#     3950 MiB            24        0%      2–3 min
#
# Either it fires constantly during healthy operation or it fires three minutes
# before Trigger 1 would have anyway. So Trigger 1 stays the only restart path
# (2 for 2 in the recorded window, zero sled corruption).
#
# The watch line moves 2500 -> 3900 purely to stop the logging being noise: at
# 2500 it crossed ~45 min after every restart and then logged every 5 minutes
# forever, which is an excellent way to bury a signal you actually care about.
# At 3900 a line in this journal means the node is genuinely near the ceiling.
ANON_WATCH_MIB="${MEMGUARD_ANON_WATCH_MIB:-3900}"

# --- Trigger 3: MemoryHigh throttle-limbo (RESTARTS) ---
# 2026-08-08. The limbo the Trigger 2 note predicted finally got recorded, and
# it does not look like what we guessed. From 08-07 14:09 UTC the node sat for
# 22 h in this state, at 60 s resolution:
#
#     anon       pinned at 3825 MiB, unchanged to the MiB for 22 h
#     cache      0.0 MiB (cgroup memory.stat file == 0 bytes)
#     load1      2.2 -> 6.4 -> 7.4
#     peers      3 -> 1, monotonic
#     RPC        accepts TCP on 18345, never answers
#
# Why every existing trigger stayed silent: anon parks at 3825, which is BELOW
# the 3900 watch line calibrated on 08-02 from the old 3905 plateau, and far
# below WALL_MIB. cgroup memory.events said `high 29022305, max 0, oom 0` —
# the node never went near the wall Trigger 1 guards. It was being throttled to
# death in the band between MemoryHigh (3.5G) and MemoryMax (4.2G): five threads
# parked in __mem_cgroup_handle_over_high, three in uninterruptible D state, and
# 162.8M of 162.9M reclaim scans were *direct* (kswapd did 57904 of them). That
# is why RPC hangs and TLS handshakes to the seeds fail — the work happens
# inside the allocating threads, and they are asleep serving a throttle penalty.
#
# So the observable is not a memory number at all. anon has no usable band (the
# 08-02 analysis was right about that) but it was the wrong thing to measure.
# We restart on the node not doing its job — dead RPC — and require corroborating
# evidence that it is the throttle causing it, so an unrelated RPC blip does not
# cost a restart:
#
#     dead RPC for RPC_FAIL_RUNS consecutive runs   (functional: not serving)
#   AND (high-breach counter still climbing            (mechanism: in the band)
#        OR page cache fully collapsed)
#   AND uptime past MIN_UPTIME                        (excludes initial sync)
#
# Measured separation, healthy vs limbo: the high counter moves ~110k per 5 min
# run in limbo and does not move at all when healthy; cgroup file cache runs
# 150–1900 MiB healthy and 0 in limbo. There is no threshold to agonise over.
RPC_PORT="${MEMGUARD_RPC_PORT:-18345}"
RPC_TIMEOUT="${MEMGUARD_RPC_TIMEOUT:-5}"
RPC_FAIL_RUNS="${MEMGUARD_RPC_FAIL_RUNS:-3}"      # timer is every 5 min => 15 min
MIN_UPTIME_S="${MEMGUARD_MIN_UPTIME_S:-1800}"
HIGH_DELTA_MIN="${MEMGUARD_HIGH_DELTA_MIN:-1000}"
FILE_COLLAPSE_MIB="${MEMGUARD_FILE_COLLAPSE_MIB:-16}"
EVENTS="/sys/fs/cgroup/system.slice/${UNIT}/memory.events"
STATE="${MEMGUARD_STATE:-/run/darkfid-memguard.state}"

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

# Trigger 3: MemoryHigh throttle-limbo. See the header block for the recorded
# episode this is calibrated against.

# cgroup page cache: 0 in limbo, hundreds of MiB when healthy.
file_mib=0
if [[ -r "$STAT" ]]; then
  fbytes="$(awk '/^file /{print $2; exit}' "$STAT")"
  [[ "$fbytes" =~ ^[0-9]+$ ]] && file_mib=$(( fbytes / 1024 / 1024 ))
fi

# memory.events:high — a monotonic count of MemoryHigh breaches. We care about
# whether it is still moving, not its absolute value, so we diff against the
# previous run. Missing/unreadable is treated as "not climbing" (fail safe).
high_now=0
if [[ -r "$EVENTS" ]]; then
  h="$(awk '/^high /{print $2; exit}' "$EVENTS")"
  [[ "$h" =~ ^[0-9]+$ ]] && high_now="$h"
fi

prev_high=""
fail_streak=0
armed_pid=""
if [[ -r "$STATE" ]]; then
  read -r prev_high fail_streak armed_pid < "$STATE" 2>/dev/null || { prev_high=""; fail_streak=0; armed_pid=""; }
  [[ "$prev_high"   =~ ^[0-9]+$ ]] || prev_high=""
  [[ "$fail_streak" =~ ^[0-9]+$ ]] || fail_streak=0
  [[ "$armed_pid"   =~ ^[0-9]+$ ]] || armed_pid=""
fi

# First run after a boot or a restart has no baseline, and the counter is
# cumulative — diffing against 0 would report the whole history as this run's
# delta. Report 0 (unknown) instead; the next run has a real baseline. This only
# ever makes the trigger less eager, never more.
high_delta=0
if [[ -n "$prev_high" ]] && (( high_now > prev_high )); then
  high_delta=$(( high_now - prev_high ))
fi

# RPC liveness. Line-delimited JSON over raw TCP, not HTTP — same shape as the
# exporter's probe. A hung node accepts the connection and never answers, so a
# bare TCP connect is not enough: we must read a reply.
rpc_ok=0
if timeout "$RPC_TIMEOUT" bash -c '
      exec 3<>"/dev/tcp/127.0.0.1/$1" || exit 1
      printf "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"blockchain.last_confirmed_block\",\"params\":[]}\n" >&3
      IFS= read -r line <&3 || exit 1
      [[ -n "$line" ]]
    ' _ "$RPC_PORT" >/dev/null 2>&1; then
  rpc_ok=1
  fail_streak=0
  armed_pid="$pid"     # this process has served RPC at least once — now armable
else
  fail_streak=$(( fail_streak + 1 ))
fi

printf '%s %s %s\n' "$high_now" "$fail_streak" "$armed_pid" > "$STATE" 2>/dev/null || true

# Arming. A node that has never answered RPC since it started is booting or
# doing a from-zero initial sync, and a heavy sync runs anon high while the page
# cache gets squeezed — i.e. it can satisfy the corroboration test honestly, and
# restarting it would produce exactly the mid-sync restart loop the trigger-2
# note warned about. So the trigger only arms once RPC has been observed working
# for THIS pid. That makes trigger 3 strictly about regression from a serving
# state, which is the limbo signature: it served for two days, then stopped.
armed=0
[[ -n "$armed_pid" && -n "$pid" && "$armed_pid" == "$pid" ]] && armed=1

if (( rpc_ok == 0 )) \
   && (( armed == 1 )) \
   && (( fail_streak >= RPC_FAIL_RUNS )) \
   && (( up_s > MIN_UPTIME_S )) \
   && { (( high_delta >= HIGH_DELTA_MIN )) || (( file_mib <= FILE_COLLAPSE_MIB )); }; then
  logger -t darkfid-memguard \
    "THROTTLE-LIMBO: RPC dead ${fail_streak} runs, memory.events:high +${high_delta} since last run, cache ${file_mib}MiB, anon ${anon_mib}MiB, total ${cur_mib}MiB, uptime ${up_s}s — graceful restart"
  systemctl restart "$UNIT"
  rm -f "$STATE" 2>/dev/null || true
  exit 0
fi

# Log the near-miss so the journal shows the trigger reasoning, not just its
# firing. One line per run only while RPC is actually failing.
if (( rpc_ok == 0 )); then
  logger -t darkfid-memguard \
    "RPC probe failed (${fail_streak}/${RPC_FAIL_RUNS}), armed=${armed}, high +${high_delta}, cache ${file_mib}MiB, anon ${anon_mib}MiB, uptime ${up_s}s"
fi
