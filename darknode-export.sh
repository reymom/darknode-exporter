#!/usr/bin/env bash
# darknode-export.sh — assemble one offline-friendly snapshot of the Pi node
# and POST it to the site. Local reads only; OUTBOUND POST only.
#
# Sources (all localhost / local tools):
#   - xmrig HTTP API 127.0.0.1 (restricted, no token — read-only)
#   - systemctl show … MemoryCurrent; cgroup memory.high/max for the live limits
#   - vcgencmd measure_temp / get_throttled
#   - free -b (host memory)
#   - journalctl -u darkfid (optional WASM tail)
#   - darkfid height/tip via log scrape or optional RPC hook
#
# Hard rules:
#   - never dial into the Pi from the internet
#   - never emit wallet addresses
#   - never expose xmrig / darkfid control planes
set -euo pipefail

log() { printf '[darknode-export] %s\n' "$*" >&2; }

ENV_FILE="${ENV_FILE:-/etc/darknode-export.env}"
if [[ -r "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090,SC1091
  set -a
  # only KEY=VALUE lines; ignore comments/blank
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" 2>/dev/null || true)
  set +a
elif [[ -e "$ENV_FILE" ]]; then
  # exists but not readable (mode 600 owned by root, run as a normal user):
  # fine for DRY_RUN; the systemd unit reads it via EnvironmentFile.
  log "note: $ENV_FILE not readable as $(id -un) — env not loaded (ok for DRY_RUN)"
fi

: "${XMRIG_API:=http://127.0.0.1:18088}"
: "${DARKFID_UNIT:=darkfid.service}"
: "${XMRIG_UNIT:=xmrig.service}"
: "${DARKFID_RPC_PORT:=18345}"
: "${DARKFID_P2P_PORT:=18340}"
: "${WASM_TAIL_N:=12}"
: "${INGEST_URL:=}"
: "${DRY_RUN:=0}"
: "${CURL_TIMEOUT:=3}"
# Local history sink (the panel shows now; this accumulates the story).
# Empty HISTORY_DIR disables it.
: "${HISTORY_DIR:=/var/log/darknode}"
: "${HISTORY_MAX_DAYS:=120}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    log "missing dependency: $1"
    exit 1
  }
}

need curl
need jq
need awk

# ---------- helpers ----------

redact() {
  # strip terminal color codes, then wallet-ish tokens, from free-form log lines
  local esc=$'\033'
  sed -E \
    -e "s/${esc}\\[[0-9;]*[a-zA-Z]//g" \
    -e 's/dark1[a-z0-9]{20,}/dark1[redacted]/gi' \
    -e 's/\b[48][A-Za-z0-9]{90,}/[redacted-addr]/g' \
    -e 's/0x[a-fA-F0-9]{40,}/0x[redacted]/g'
}

mem_field() {
  # systemctl show UNIT -p Field  →  Field=value
  local unit="$1" field="$2"
  systemctl show "$unit" -p "$field" --value 2>/dev/null || echo ""
}

mem_stat_field() {
  # one line from the unit's cgroup memory.stat, e.g. anon (working set),
  # file (reclaimable page cache). Returns bytes, or 0 if unreadable.
  local unit="$1" field="$2"
  local f="/sys/fs/cgroup/system.slice/${unit}/memory.stat"
  [[ -r "$f" ]] && awk -v k="$field" '$1 == k { print $2; exit }' "$f" || echo 0
}

# systemd prints "infinity" / empty when unset; map those to 0 so the
# panel can still render a bar against a non-zero high.
as_bytes() {
  local v="$1"
  if [[ -z "$v" || "$v" == "infinity" || "$v" == "[not set]" ]]; then
    echo 0
  else
    echo "$v"
  fi
}

cg_limit() {
  # The limit the kernel is enforcing right now, read from the cgroup itself.
  # systemctl show reports the configured value, which is not the same thing
  # after a live `echo … > memory.high`: on 21-S the export said 4.5 G all night
  # while the cgroup had no soft limit at all. "max" = unlimited → 0, as above.
  local unit="$1" file="$2" field="$3" v=""
  local f="/sys/fs/cgroup/system.slice/${unit}/${file}"
  [[ -r "$f" ]] && v="$(<"$f")"
  if [[ -z "$v" ]]; then
    as_bytes "$(mem_field "$unit" "$field")"
  elif [[ "$v" == "max" ]]; then
    echo 0
  else
    echo "$v"
  fi
}

json_null_if_empty() {
  local v="${1:-}"
  if [[ -z "$v" ]]; then
    echo "null"
  else
    echo "$v"
  fi
}

# ---------- xmrig ----------

summary_json="{}"
backends_json="[]"
if summary_raw="$(curl -fsS --max-time "$CURL_TIMEOUT" "${XMRIG_API}/2/summary" 2>/dev/null)"; then
  summary_json="$summary_raw"
else
  log "xmrig summary unavailable at ${XMRIG_API}/2/summary"
fi
if backends_raw="$(curl -fsS --max-time "$CURL_TIMEOUT" "${XMRIG_API}/2/backends" 2>/dev/null)"; then
  backends_json="$backends_raw"
fi

# per-thread hashrate: take 10s window from each thread across CPU backends
threads_json="$(
  jq -c '
    [ .[]?
      | select(.type == "cpu" or .type == null)
      | .threads[]?
      | (if type == "object" then .hashrate[0] else .[0] end)
      | select(. != null)
    ]
  ' <<<"$backends_json" 2>/dev/null || echo '[]'
)"

# ---------- cgroup memory ----------

dark_cur="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemoryCurrent)")"
dark_high="$(cg_limit "$DARKFID_UNIT" memory.high MemoryHigh)"
dark_max="$(cg_limit "$DARKFID_UNIT" memory.max MemoryMax)"
dark_peak="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemoryPeak)")"
dark_swap="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemorySwapCurrent)")"
dark_anon="$(mem_stat_field "$DARKFID_UNIT" anon)"
dark_file="$(mem_stat_field "$DARKFID_UNIT" file)"

xmrig_cur="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemoryCurrent)")"
xmrig_high="$(cg_limit "$XMRIG_UNIT" memory.high MemoryHigh)"
xmrig_max="$(cg_limit "$XMRIG_UNIT" memory.max MemoryMax)"
xmrig_peak="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemoryPeak)")"
xmrig_swap="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemorySwapCurrent)")"
xmrig_anon="$(mem_stat_field "$XMRIG_UNIT" anon)"
xmrig_file="$(mem_stat_field "$XMRIG_UNIT" file)"

# host memory from /proc/meminfo (kB → bytes)
host_total=0
host_avail=0
host_used=0
swap_total=0
swap_free=0
if [[ -r /proc/meminfo ]]; then
  host_total=$(($(awk '/^MemTotal:/ {print $2}' /proc/meminfo) * 1024))
  host_avail=$(($(awk '/^MemAvailable:/ {print $2}' /proc/meminfo) * 1024))
  host_used=$((host_total - host_avail))
  swap_total=$(($(awk '/^SwapTotal:/ {print $2}' /proc/meminfo) * 1024))
  swap_free=$(($(awk '/^SwapFree:/ {print $2}' /proc/meminfo) * 1024))
fi
swap_used=$((swap_total - swap_free))
if ((swap_used < 0)); then swap_used=0; fi

# ---------- pi health ----------

temp_c=""
if command -v vcgencmd >/dev/null 2>&1; then
  # temp=54.9'C
  temp_c="$(vcgencmd measure_temp 2>/dev/null | sed -E "s/temp=([0-9.]+).*/\1/" || true)"
  throttled_raw="$(vcgencmd get_throttled 2>/dev/null | sed -E 's/throttled=//' || true)"
else
  throttled_raw=""
  # generic thermal zone fallback
  if [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
    milli="$(cat /sys/class/thermal/thermal_zone0/temp)"
    temp_c="$(awk -v m="$milli" 'BEGIN { printf "%.1f", m/1000 }')"
  fi
fi
throttled="${throttled_raw:-}"

# ---------- darkfid start time ----------
# When the unit last entered active state, in epoch ms. A change in this value
# is a restart, exactly — the digest used to infer one from a drop in anon,
# which misses any restart that does not free two gigabytes.
darkfid_started=""
if started_raw="$(systemctl show darkfid -p ActiveEnterTimestamp --value 2>/dev/null)" \
   && [[ -n "$started_raw" ]]; then
  if started_epoch="$(date -d "$started_raw" +%s 2>/dev/null)"; then
    darkfid_started="$(( started_epoch * 1000 ))"
  fi
fi

# ---------- darkfid height / tip / peers ----------
# Order of preference:
#   1. DARKFID_HEIGHT_CMD — user-provided one-liner override.
#   2. darkfid's own JSON-RPC on localhost (line-delimited JSON over raw TCP,
#      not HTTP — hence /dev/tcp, not curl).
#   3. Journal scrape — the original best-effort fallback.
# Never talk to a remote peer for these numbers.

height=""
tip=""
peers=""
# xmrig reports the live PoW job difficulty it's mining against — this is the
# network's own difficulty target, not something specific to xmrig.
difficulty="$(jq -r '.results.diff_current // empty' <<<"$summary_json" 2>/dev/null || true)"

rpc_call() {
  # rpc_call PORT METHOD → one JSON-RPC response line from darkfid, or empty.
  timeout "$CURL_TIMEOUT" bash -c '
    exec 3<>"/dev/tcp/127.0.0.1/$1" || exit 1
    printf "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"%s\",\"params\":[]}\n" "$2" >&3
    IFS= read -r line <&3
    printf "%s\n" "$line"
  ' _ "$1" "$2" 2>/dev/null || true
}

if [[ -n "${DARKFID_HEIGHT_CMD:-}" ]]; then
  # user-provided one-liner that prints "height tip peers difficulty"
  # shellcheck disable=SC2086
  read -r height tip peers difficulty < <(eval "$DARKFID_HEIGHT_CMD" 2>/dev/null || true)
fi

# height = the head of the best fork the node follows, unconfirmed proposals
# included: best_fork_next_block_height is where the next block would land, so
# the head is next - 1. It used to be last_confirmed_block, which trails the
# head by the confirmation depth (5 blocks on 22-S), and the panel read a
# healthy node as "syncing, lag 5". The same head seeds tip; the network's tip
# from the log raises it below while a sync is running.
if [[ -z "$height" || -z "$tip" ]]; then
  next="$(rpc_call "$DARKFID_RPC_PORT" blockchain.best_fork_next_block_height \
    | jq -r '.result // empty' 2>/dev/null || true)"
  if [[ "$next" =~ ^[0-9]+$ ]] && ((next > 0)); then
    [[ -z "$height" ]] && height=$((next - 1))
    [[ -z "$tip" ]] && tip=$((next - 1))
  fi
fi
if [[ -z "$height" ]]; then
  # the call failed or darkfid is older: the last confirmed block, a few behind
  height="$(rpc_call "$DARKFID_RPC_PORT" blockchain.last_confirmed_block \
    | jq -r '.result[0] // empty' 2>/dev/null || true)"
fi

if [[ -z "$height" || -z "$tip" ]]; then
  # journal scrape — best-effort, last ~30 min
  journal="$(
    journalctl -u "$DARKFID_UNIT" --since "30 min ago" -o cat --no-pager 2>/dev/null \
      | tail -n 800 || true
  )"
  # node block: "Last received block: N" (syncing) / "Appended proposal <hash> - N"
  # (following). Take the highest seen.
  if [[ -z "$height" ]]; then
    height="$(
      printf '%s\n' "$journal" \
        | grep -Eo '(Last received block: |Appended proposal [a-f0-9]+ - )[0-9]+' \
        | grep -Eo '[0-9]+$' \
        | sort -n | tail -n1 || true
    )"
  fi
  # network tip: "Most common tip: N - <hash>"
  if [[ -z "$tip" ]]; then
    tip="$(
      printf '%s\n' "$journal" \
        | grep -Eo 'Most common tip: [0-9]+' \
        | grep -Eo '[0-9]+' \
        | sort -n | tail -n1 || true
    )"
  fi
fi
# While syncing, the RPC only knows the node's own chain, so height and tip move
# together and the panel read 100% through a whole 7 h resync (21-S). The
# network's height is only in the log, as "Most common tip: N", printed at the
# start of each sync round — which from genesis is hours before the round ends,
# hence the wide window. Take it whenever it is ahead; once the node has caught
# up, its own tip passes it and this is a no-op.
net_tip="$(
  journalctl -u "$DARKFID_UNIT" --since "24 hours ago" -o cat --no-pager \
    -g 'Most common tip: [0-9]+' -n 1 2>/dev/null \
    | grep -Eo 'Most common tip: [0-9]+' | grep -Eo '[0-9]+' | tail -n1 || true
)"
if [[ "$net_tip" =~ ^[0-9]+$ ]] && { [[ -z "$tip" ]] || ((net_tip > tip)); }; then
  tip="$net_tip"
fi
# The proposal tip can transiently sit at/below the confirmed height around a
# reorg; and when following quietly there may be no tip signal at all. The node
# is at the tip in both cases — never let tip sit below height.
if [[ -n "$height" ]] && { [[ -z "$tip" ]] || ((tip < height)); }; then
  tip="$height"
fi

# peers = established TCP sessions on the P2P port (both directions). Count
# only — never IPs (DarkFi is an anonymity network; the panel stays blind).
#
# 2026-08-08: this used to end with `[[ "$peers" == "0" ]] && peers=""`, which
# meant a genuine zero was published as an ABSENT field — byte-identical to "the
# exporter could not measure". The panel reads absence as no-data, so the one
# event the peer instrument exists to catch was the one event it could not
# express, and /darknode/history has been asserting "never lost the network"
# from a series that is incapable of containing a zero. Zero is now reported as
# zero. Failure to measure is still absence, but it has to be a real failure:
# `ss | wc -l` returns 0 both when there are no sessions and when ss errors out,
# so the two are separated on ss's exit status rather than on its output.
if [[ -z "$peers" ]]; then
  if ss_out="$(ss -Htn state established \
        "( dport = :${DARKFID_P2P_PORT} or sport = :${DARKFID_P2P_PORT} )" 2>/dev/null)"; then
    peers="$(printf '%s' "$ss_out" | grep -c . || true)"
  else
    peers=""   # could not measure — distinct from measured zero
  fi
fi

# ---------- WASM tail ----------

wasm_tail_json='[]'
if wasm_lines="$(
  journalctl -u "$DARKFID_UNIT" --since "20 min ago" -n 4000 --no-pager -o cat 2>/dev/null \
    | grep -E '\[WASM\] Contract log:' \
    | sed -E 's/^[0-9]{2}:[0-9]{2}:[0-9]{2} \[INFO\] \[WASM\] Contract log: //' \
    | tail -n "$WASM_TAIL_N" \
    | redact \
    || true
)"; then
  if [[ -n "$wasm_lines" ]]; then
    wasm_tail_json="$(printf '%s\n' "$wasm_lines" | jq -R -s 'split("\n") | map(select(length>0))')"
  fi
fi

# ---------- assemble ----------

exported_at=$(($(date +%s) * 1000))

payload="$(
  jq -cn \
    --argjson exportedAt "$exported_at" \
    --arg darkfidStarted "$darkfid_started" \
    --argjson summary "$summary_json" \
    --argjson threads "$threads_json" \
    --argjson darkCur "$dark_cur" \
    --argjson darkHigh "$dark_high" \
    --argjson darkMax "$dark_max" \
    --argjson darkPeak "$dark_peak" \
    --argjson darkSwap "$dark_swap" \
    --argjson darkAnon "$dark_anon" \
    --argjson darkFile "$dark_file" \
    --argjson xmrigCur "$xmrig_cur" \
    --argjson xmrigHigh "$xmrig_high" \
    --argjson xmrigMax "$xmrig_max" \
    --argjson xmrigPeak "$xmrig_peak" \
    --argjson xmrigSwap "$xmrig_swap" \
    --argjson xmrigAnon "$xmrig_anon" \
    --argjson xmrigFile "$xmrig_file" \
    --argjson hostTotal "$host_total" \
    --argjson hostUsed "$host_used" \
    --argjson hostAvail "$host_avail" \
    --argjson swapTotal "$swap_total" \
    --argjson swapUsed "$swap_used" \
    --arg tempC "$temp_c" \
    --arg throttled "$throttled" \
    --arg height "$height" \
    --arg tip "$tip" \
    --arg peers "$peers" \
    --arg difficulty "$difficulty" \
    --argjson wasmTail "$wasm_tail_json" '
    {
      exportedAt: $exportedAt,
      hashrate: (
        if ($summary.hashrate? != null) then {
          total: ($summary.hashrate.total // [null,null,null]),
          highest: ($summary.hashrate.highest // null)
        } else null end
      ),
      hugepages: ($summary.hugepages // null),
      algo: ($summary.algo // null),
      uptime: ($summary.uptime // null),
      resources: (
        if ($summary.resources? != null) then {
          load_average: $summary.resources.load_average,
          hardware_concurrency: $summary.resources.hardware_concurrency
        } else null end
      ),
      results: (
        if ($summary.results? != null) then {
          shares_good: $summary.results.shares_good,
          shares_total: $summary.results.shares_total
        } else null end
      ),
      threads: (if ($threads | length) > 0 then $threads else null end),
      memory: {
        darkfid: {
          current: $darkCur, high: $darkHigh, max: $darkMax,
          peak: $darkPeak, swap: $darkSwap, anon: $darkAnon, cache: $darkFile
        },
        xmrig: {
          current: $xmrigCur, high: $xmrigHigh, max: $xmrigMax,
          peak: $xmrigPeak, swap: $xmrigSwap, anon: $xmrigAnon, cache: $xmrigFile
        },
        host: {
          total: $hostTotal, used: $hostUsed, available: $hostAvail,
          swapTotal: $swapTotal, swapUsed: $swapUsed
        }
      },
      wasmTail: (if ($wasmTail | length) > 0 then $wasmTail else null end)
    }
    + (if $tempC != "" then {tempC: ($tempC|tonumber)} else {} end)
    + (if $throttled != "" then {throttled: $throttled} else {} end)
    + (if $height != "" then {height: ($height|tonumber)} else {} end)
    + (if $tip != "" then {tip: ($tip|tonumber)} else {} end)
    + (if $peers != "" then {peers: ($peers|tonumber)} else {} end)
    + (if $difficulty != "" then {difficulty: ($difficulty|tonumber)} else {} end)
    + (if $darkfidStarted != "" then {darkfidStartedAt: ($darkfidStarted|tonumber)} else {} end)
    | with_entries(select(.value != null))
  '
)"

# A jq object with a bare `empty` in any value position collapses to an empty
# stream, so a single missing optional field (e.g. wasmTail when the node is
# quiet) would yield an empty payload. All optional values now resolve to null
# and are stripped by with_entries above; this guard is the backstop so we
# never record a blank line to history or POST an empty body (HTTP 400).
if [[ -z "$payload" ]]; then
  log "payload assembly produced empty output — refusing to record or POST"
  exit 1
fi

if [[ "$DRY_RUN" == "1" ]]; then
  printf '%s\n' "$payload"
  exit 0
fi

# ---------- local history (before the POST — outages are data too) ----------
# One JSONL file per day. Compress yesterday's files opportunistically and
# expire beyond HISTORY_MAX_DAYS. ~1 sample/min ≈ ~0.5 MB/day uncompressed.
# Rotation here also covers the dnet-*.jsonl files written by dnet-record: that
# service is long-lived and only rotates at startup, so without this its daily
# files never get compressed. `-mtime +0` only touches files older than a day,
# never dnet-record's actively-written current file.
if [[ -n "$HISTORY_DIR" ]]; then
  if mkdir -p "$HISTORY_DIR" 2>/dev/null && [[ -w "$HISTORY_DIR" ]]; then
    printf '%s\n' "$payload" >>"$HISTORY_DIR/snapshots-$(date +%F).jsonl"
    # Housekeeping (gzip yesterday, expire ancient) at most once an hour, not on
    # every 60s run — a given file only ever gets gzipped once anyway, so there's
    # no point walking the dir each minute.
    rotate_marker="$HISTORY_DIR/.last-rotate"
    if [[ ! -e "$rotate_marker" ]] || [[ -n "$(find "$rotate_marker" -mmin +60 2>/dev/null)" ]]; then
      find "$HISTORY_DIR" \( -name 'snapshots-*.jsonl' -o -name 'dnet-*.jsonl' \) -mtime +0 -exec gzip -q {} \; 2>/dev/null || true
      find "$HISTORY_DIR" \( -name 'snapshots-*.jsonl.gz' -o -name 'dnet-*.jsonl.gz' \) -mtime +"$HISTORY_MAX_DAYS" -delete 2>/dev/null || true
      touch "$rotate_marker"
    fi
  else
    log "history dir $HISTORY_DIR not writable — skipping local history"
  fi
fi

if [[ -z "${INGEST_URL:-}" ]]; then
  log "INGEST_URL is not set (set it in $ENV_FILE)"
  exit 1
fi

if [[ -z "${NODE_INGEST_TOKEN:-}" ]]; then
  log "NODE_INGEST_TOKEN is not set (load it via $ENV_FILE)"
  exit 1
fi

http_code="$(
  curl -sS --max-time 15 \
    -o /tmp/darknode-export.out \
    -w '%{http_code}' \
    -X POST "$INGEST_URL" \
    -H "Authorization: Bearer ${NODE_INGEST_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "User-Agent: darknode-export/1.0" \
    --data "$payload"
)"

if [[ "$http_code" != "200" ]]; then
  log "POST failed HTTP $http_code: $(cat /tmp/darknode-export.out 2>/dev/null || true)"
  exit 1
fi

log "POST ok ($http_code) → $INGEST_URL"
cat /tmp/darknode-export.out >&2 || true
printf '\n' >&2
