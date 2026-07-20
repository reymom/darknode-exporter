#!/usr/bin/env bash
# install.sh — install the darknode exporter from the files in THIS directory.
# No git required on the Pi: get these files here first (scp from your laptop,
# or download the repo tarball — see README), then run:  ./install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\n[install] %s\n' "$*"; }

command -v curl >/dev/null || { echo "need curl"; exit 1; }
if ! command -v jq >/dev/null; then
  say "jq not found — installing"
  sudo apt-get update -qq && sudo apt-get install -y jq
fi

say "collector → /usr/local/bin/darknode-export.sh"
sudo install -m 755 "$HERE/darknode-export.sh" /usr/local/bin/darknode-export.sh

say "smoke test: collector must emit a non-empty JSON object"
if ! DRY_RUN=1 /usr/local/bin/darknode-export.sh 2>/dev/null \
     | jq -e 'type == "object" and (keys | length > 0)' >/dev/null; then
  echo "[install] FATAL: collector produced empty or invalid JSON — refusing to continue." >&2
  echo "[install] inspect with: DRY_RUN=1 /usr/local/bin/darknode-export.sh | jq ." >&2
  exit 1
fi

say "systemd units → /etc/systemd/system/"
sudo install -m 644 "$HERE/darknode-export.service" /etc/systemd/system/darknode-export.service
sudo install -m 644 "$HERE/darknode-export.timer" /etc/systemd/system/darknode-export.timer

if [[ -f /etc/darknode-export.env ]]; then
  say "/etc/darknode-export.env already exists — leaving it untouched"
else
  say "creating /etc/darknode-export.env (mode 600) — YOU must edit it"
  sudo install -m 600 "$HERE/darknode-export.env.example" /etc/darknode-export.env
fi

sudo systemctl daemon-reload

cat <<'NEXT'

[install] done. Finish setup:

  1. sudo nano /etc/darknode-export.env
       NODE_INGEST_TOKEN=<same value set on your receiver>
       INGEST_URL=https://your-site.example/api/node-ingest

  2. test with no POST (prints the JSON it would send):
       DRY_RUN=1 /usr/local/bin/darknode-export.sh | jq .

  3. one real POST now:
       sudo systemctl start darknode-export.service
       journalctl -u darknode-export.service -n 20 --no-pager

  4. enable the 60s timer:
       sudo systemctl enable --now darknode-export.timer
       systemctl list-timers | grep darknode

NEXT
