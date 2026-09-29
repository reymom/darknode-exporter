#!/usr/bin/env bash
# dnet-record.sh — subscribe to darkfid's dnet P2P event stream and append
# every event to a local NDJSON file, one file per day.
#
# dnet is darkfid's built-in p2p instrumentation (the same stream its own
# `dnetev` TUI consumes): per-channel send/recv with ns timestamps, peer
# discovery states, slot lifecycle. Recorded from the node's own vantage
# point — this is the raw material for session-lifetime / churn analysis.
#
# Hard rules (same as the exporter):
#   - localhost only; nothing dials in, nothing dials out
#   - events carry peer addresses — the recording stays ON THE PI, it is
#     never POSTed anywhere. Aggregates may leave; raw events do not.
set -euo pipefail

log() { printf '[dnet-record] %s\n' "$*" >&2; }

: "${DARKFID_MGMT_RPC_PORT:=18346}"
: "${HISTORY_DIR:=/var/log/darknode}"
: "${HISTORY_MAX_DAYS:=120}"
# How often to poke the write side of the socket (see the heartbeat note below).
: "${DNET_HEARTBEAT_S:=30}"
# Silence longer than this means the stream is dead, not that the overlay is
# quiet: baseline traffic is ~2 events/s and `outbound_peer_discovery` keeps
# firing even with every peer gone (that is exactly what the July seed outage
# looked like on the wire). So a full stall here is always the instrument.
: "${DNET_STALL_S:=300}"

command -v jq >/dev/null 2>&1 || { log "missing dependency: jq"; exit 1; }
command -v nc >/dev/null 2>&1 || { log "missing dependency: nc"; exit 1; }

mkdir -p "$HISTORY_DIR"

# Housekeeping once per start: compress older files, expire ancient ones.
find "$HISTORY_DIR" -name 'dnet-*.jsonl' -mtime +0 -exec gzip -q {} \; 2>/dev/null || true
find "$HISTORY_DIR" -name 'dnet-*.jsonl.gz' -mtime +"$HISTORY_MAX_DAYS" -delete 2>/dev/null || true

rpc_line() { printf '{"jsonrpc":"2.0","id":%d,"method":"%s","params":%s}\n' "$1" "$2" "$3"; }

# Make sure the switch flips back off if we die — dnet instrumentation has a
# (small) cost inside darkfid and shouldn't stay on with nobody listening.
cleanup() {
  rpc_line 9 "dnet.switch" "[false]" | timeout 3 nc 127.0.0.1 "$DARKFID_MGMT_RPC_PORT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- Stall watchdog (2026-08-01) -------------------------------------------
# This recorder went silent for twelve days without systemd ever noticing.
# darkfid restarted on 07-20, sent a FIN, and the socket parked in CLOSE-WAIT —
# but `nc` only exits on remote EOF once its *stdin* is also done, and stdin was
# `tail -f /dev/null`, i.e. never. So nc sat there forever, the pipeline never
# ended, the unit stayed `active (running)`, and `Restart=always` had nothing to
# fire on. A service that is up and producing nothing is worse than a crashed
# one, so there are now two independent ways out.
#
# Belt: the heartbeat below turns the half-open socket into a write error.
# Braces: this watchdog watches the data actually landing on disk, which also
# covers a socket that stays ESTABLISHED while the stream goes quiet.
MAIN_PID=$$
watchdog() {
  local started newest mtime age
  started=$(date +%s)
  while sleep 60; do
    newest="$(ls -t "$HISTORY_DIR"/dnet-*.jsonl 2>/dev/null | head -1)"
    mtime=""
    [[ -n "$newest" ]] && mtime="$(stat -c %Y "$newest" 2>/dev/null || true)"
    if [[ "$mtime" =~ ^[0-9]+$ ]]; then
      age=$(( $(date +%s) - mtime ))
    else
      # No readable file yet (cold start, or the hourly rotation gzipped it out
      # from under us): measure from our own start so we get a grace period
      # instead of an instant restart loop.
      age=$(( $(date +%s) - started ))
    fi
    if (( age > DNET_STALL_S )); then
      log "no dnet events for ${age}s — stream is stale, bailing out for a restart"
      kill -TERM "$MAIN_PID" 2>/dev/null || true
      return
    fi
  done
}
watchdog &
WATCHDOG_PID=$!
# shellcheck disable=SC2064  # capture the pid now, not at trap time
trap "kill $WATCHDOG_PID 2>/dev/null || true; cleanup" EXIT

log "subscribing to dnet events on 127.0.0.1:${DARKFID_MGMT_RPC_PORT} → ${HISTORY_DIR}/dnet-YYYY-MM-DD.jsonl"

# Mark the start of a recording run in the history itself, so that a later gap
# analysis can tell "the recorder was restarted" from "the overlay was quiet".
printf '{"rx":%s000,"event":"recorder_start"}\n' "$(date +%s)" \
  >>"$HISTORY_DIR/dnet-$(date +%F).jsonl"

# Keep the write side open so nc holds the TCP session; the read side streams
# one JSON event per line. `stdbuf -oL` defends against block buffering.
{
  rpc_line 1 "dnet.switch" "[true]"
  rpc_line 2 "dnet.subscribe_events" "[]"
  # Heartbeat, NOT a keepalive: the point is to keep *writing*. A peer that has
  # gone away swallows the first write after its FIN and answers the next with
  # RST, so nc takes a SIGPIPE and the whole pipeline unwinds. `dnet.switch
  # [true]` is the safe poke — idempotent, already proven on this connection,
  # and its ack is dropped by the jq filter below like the other two.
  while sleep "$DNET_HEARTBEAT_S"; do
    rpc_line 3 "dnet.switch" "[true]"
  done
} | stdbuf -oL nc 127.0.0.1 "$DARKFID_MGMT_RPC_PORT" \
  | jq -c --unbuffered '
      # keep only subscription events (skip the two rpc acks), stamp local
      # receive time in ms, flatten to the event payload
      select(.method == "dnet.subscribe_events")
      | {rx: (now * 1000 | floor)} + (.params[0] // {})
    ' 2>/dev/null \
  | while IFS= read -r event; do
      printf '%s\n' "$event" >>"$HISTORY_DIR/dnet-$(date +%F).jsonl"
    done

# nc exited → darkfid went away (restart, crash). Let systemd restart us.
log "dnet stream closed"
exit 1
