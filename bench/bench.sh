#!/usr/bin/env bash
# bench.sh — run one arm of a darkfid experiment and print what it cost.
#
#   ./bench.sh <label> [ENV=VALUE ...]
#
#   ./bench.sh glibc
#   ./bench.sh arena1     MALLOC_ARENA_MAX=1
#   ./bench.sh jemalloc   LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
#
# Every arm restores the SAME chain database first, so the only thing that
# differs between two runs is the environment you passed. Without that you are
# comparing two different chains and the numbers mean nothing.
#
# It watches until the node clears $TARGET_HEIGHT or dies, then appends a row to
# $LOG: label, start, end, seconds, peak anon MiB, anon at the end, outcome, and
# CPU seconds. CPU against wall is the cheap way to tell waiting from working:
# same CPU and more wall means it was blocked, not busy.
#
# Needs: a darkfid under systemd, a snapshot made by ./snapshot.sh, and
# ./sampler.sh running if you want the shape rather than just the peak.
set -u

: "${DARKFID_UNIT:=darkfid.service}"
: "${SNAPSHOT:=$HOME/darkfid-bench/snapshot}"
: "${DB:=$HOME/.local/share/darkfi/darkfid/testnet}"
: "${BLOCKS:=/var/log/darknode/blocks.tsv}"
: "${LOG:=$HOME/darkfid-bench/runs.tsv}"
: "${TARGET_HEIGHT:=65000}"
: "${TIMEOUT_MIN:=30}"

[[ "$DARKFID_UNIT" == *.* ]] || DARKFID_UNIT="${DARKFID_UNIT}.service"
CG="/sys/fs/cgroup/system.slice/${DARKFID_UNIT}"
DROPIN="/etc/systemd/system/${DARKFID_UNIT}.d/bench-arm.conf"

[[ $# -ge 1 ]] || { echo "usage: $0 <label> [ENV=VALUE ...]" >&2; exit 2; }
label="$1"; shift

[[ -d "$SNAPSHOT" ]] || { echo "no snapshot at $SNAPSHOT — run ./snapshot.sh save first" >&2; exit 1; }
mkdir -p "$(dirname "$LOG")"
[[ -s "$LOG" ]] || printf 'label\tstart\tend\tsecs\tpeak_anon_mib\tfinal_anon_mib\toutcome\tcpu_s\tenv\n' > "$LOG"

systemctl stop "$DARKFID_UNIT" 2>/dev/null; sleep 3
rm -rf "$DB"; cp -a "$SNAPSHOT" "$DB"

# The arm is one drop-in, written fresh every time, so nothing survives between
# runs by accident. That has bitten people: a stale LD_PRELOAD makes the control
# arm quietly be the treatment.
mkdir -p "$(dirname "$DROPIN")"
rm -f "$DROPIN"
if [[ $# -gt 0 ]]; then
  { echo "[Service]"; for e in "$@"; do echo "Environment=$e"; done; } > "$DROPIN"
fi
systemctl daemon-reload

n0=$(wc -l < "$BLOCKS" 2>/dev/null || echo 0)
t0=$(date +%s)
systemctl start "$DARKFID_UNIT"
echo "=== $label   env: ${*:-none}   started $(date -u +%H:%M:%SZ)"

peak=0; outcome="no outcome"; h=""
for _ in $(seq 1 $((TIMEOUT_MIN * 3))); do
  sleep 20
  # Only the rows THIS run appended. The block log is cumulative, and reading its
  # last line called every arm a pass within twenty seconds.
  h=$(tail -n +$((n0 + 1)) "$BLOCKS" 2>/dev/null | awk -F'\t' 'END{print $2}')
  a=$(awk '$1=="anon"{print int($2/1048576)}' "$CG/memory.stat" 2>/dev/null)
  [[ -n "${a:-}" && "$a" -gt "$peak" ]] && peak=$a
  if ! systemctl is-active -q "$DARKFID_UNIT"; then
    outcome="KILLED at height ${h:-?}"
    journalctl -k --no-pager 2>/dev/null | grep -i "Killed process" | tail -1 | grep -o 'anon-rss:[0-9]*kB' || true
    break
  fi
  if [[ -n "${h:-}" && "$h" -ge "$TARGET_HEIGHT" ]]; then
    outcome="PASSED at height $h"
    break
  fi
done

t1=$(date +%s); secs=$((t1 - t0))
final=$(awk '$1=="anon"{print int($2/1048576)}' "$CG/memory.stat" 2>/dev/null)
cpu=$(( $(systemctl show "$DARKFID_UNIT" -p CPUUsageNSec --value 2>/dev/null || echo 0) / 1000000000 ))

printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$label" "$t0" "$t1" "$secs" "$peak" "${final:-0}" "$outcome" "$cpu" "${*:-none}" >> "$LOG"
echo "$label: $outcome in ${secs}s · peak anon ${peak} MiB · holding ${final:-?} MiB at the end · cpu ${cpu}s"
