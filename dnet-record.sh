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

log "subscribing to dnet events on 127.0.0.1:${DARKFID_MGMT_RPC_PORT} → ${HISTORY_DIR}/dnet-YYYY-MM-DD.jsonl"

# Keep the write side open so nc holds the TCP session; the read side streams
# one JSON event per line. `stdbuf -oL` defends against block buffering.
{
  rpc_line 1 "dnet.switch" "[true]"
  rpc_line 2 "dnet.subscribe_events" "[]"
  # hold stdin open forever
  tail -f /dev/null
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
