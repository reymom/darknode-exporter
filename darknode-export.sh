#!/usr/bin/env bash
# darknode-export.sh — assemble one offline-friendly snapshot of the Pi node
# and POST it to the site. Local reads only; OUTBOUND POST only.
#
# Sources (all localhost / local tools):
#   - xmrig HTTP API 127.0.0.1 (restricted, no token — read-only)
#   - systemctl show … MemoryCurrent/High/Max
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
: "${WASM_TAIL_N:=12}"
: "${INGEST_URL:=}"
: "${DRY_RUN:=0}"
: "${CURL_TIMEOUT:=3}"

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
dark_high="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemoryHigh)")"
dark_max="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemoryMax)")"
dark_peak="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemoryPeak)")"
dark_swap="$(as_bytes "$(mem_field "$DARKFID_UNIT" MemorySwapCurrent)")"

xmrig_cur="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemoryCurrent)")"
xmrig_high="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemoryHigh)")"
xmrig_max="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemoryMax)")"
xmrig_peak="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemoryPeak)")"
xmrig_swap="$(as_bytes "$(mem_field "$XMRIG_UNIT" MemorySwapCurrent)")"

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

# ---------- darkfid height / tip / peers ----------
# Prefer an optional local helper if present; else scrape journal lines that
# DarkFi already logs ("Last known block", "Most common tip"). Never talk to
# a remote peer for this number.

height=""
tip=""
peers=""
difficulty=""

if [[ -n "${DARKFID_HEIGHT_CMD:-}" ]]; then
  # user-provided one-liner that prints "height tip peers difficulty"
  # shellcheck disable=SC2086
  read -r height tip peers difficulty < <(eval "$DARKFID_HEIGHT_CMD" 2>/dev/null || true)
fi

if [[ -z "$height" || -z "$tip" ]]; then
  # journal scrape — best-effort, last ~5 min
  journal="$(
    journalctl -u "$DARKFID_UNIT" --since "5 min ago" -o cat --no-pager 2>/dev/null \
      | tail -n 400 || true
  )"
  # Last known block: 12345 or height=12345
  height="$(
    printf '%s\n' "$journal" \
      | grep -Eo '(Last known block|height)[=: ]+[0-9]+' \
      | tail -n1 \
      | grep -Eo '[0-9]+$' || true
  )"
  tip="$(
    printf '%s\n' "$journal" \
      | grep -Eo '(Most common tip|network tip|tip)[=: ]+[0-9]+' \
      | tail -n1 \
      | grep -Eo '[0-9]+$' || true
  )"
  peers="$(
    printf '%s\n' "$journal" \
      | grep -Eio 'peers?[=: ]+[0-9]+' \
      | tail -n1 \
      | grep -Eo '[0-9]+$' || true
  )"
fi

# ---------- WASM tail ----------

wasm_tail_json='[]'
if wasm_lines="$(
  journalctl -u "$DARKFID_UNIT" --since "20 min ago" -n 4000 --no-pager -o cat 2>/dev/null \
    | grep -E '\[WASM\] Contract log:' \
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
  jq -n \
    --argjson exportedAt "$exported_at" \
    --argjson summary "$summary_json" \
    --argjson threads "$threads_json" \
    --argjson darkCur "$dark_cur" \
    --argjson darkHigh "$dark_high" \
    --argjson darkMax "$dark_max" \
    --argjson darkPeak "$dark_peak" \
    --argjson darkSwap "$dark_swap" \
    --argjson xmrigCur "$xmrig_cur" \
    --argjson xmrigHigh "$xmrig_high" \
    --argjson xmrigMax "$xmrig_max" \
    --argjson xmrigPeak "$xmrig_peak" \
    --argjson xmrigSwap "$xmrig_swap" \
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
        } else empty end
      ),
      hugepages: ($summary.hugepages // empty),
      algo: ($summary.algo // empty),
      uptime: ($summary.uptime // empty),
      resources: (
        if ($summary.resources? != null) then {
          load_average: $summary.resources.load_average,
          hardware_concurrency: $summary.resources.hardware_concurrency
        } else empty end
      ),
      results: (
        if ($summary.results? != null) then {
          shares_good: $summary.results.shares_good,
          shares_total: $summary.results.shares_total
        } else empty end
      ),
      threads: (if ($threads | length) > 0 then $threads else empty end),
      memory: {
        darkfid: {
          current: $darkCur, high: $darkHigh, max: $darkMax,
          peak: $darkPeak, swap: $darkSwap
        },
        xmrig: {
          current: $xmrigCur, high: $xmrigHigh, max: $xmrigMax,
          peak: $xmrigPeak, swap: $xmrigSwap
        },
        host: {
          total: $hostTotal, used: $hostUsed, available: $hostAvail,
          swapTotal: $swapTotal, swapUsed: $swapUsed
        }
      },
      wasmTail: (if ($wasmTail | length) > 0 then $wasmTail else empty end)
    }
    + (if $tempC != "" then {tempC: ($tempC|tonumber)} else {} end)
    + (if $throttled != "" then {throttled: $throttled} else {} end)
    + (if $height != "" then {height: ($height|tonumber)} else {} end)
    + (if $tip != "" then {tip: ($tip|tonumber)} else {} end)
    + (if $peers != "" then {peers: ($peers|tonumber)} else {} end)
    + (if $difficulty != "" then {difficulty: ($difficulty|tonumber)} else {} end)
    | with_entries(select(.value != null))
  '
)"

if [[ "$DRY_RUN" == "1" ]]; then
  printf '%s\n' "$payload"
  exit 0
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
