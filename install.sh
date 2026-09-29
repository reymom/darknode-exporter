#!/usr/bin/env bash
# install.sh — put darkscope on the machine that runs your node.
#
#   ./install.sh            everything you need to watch your own node, locally
#   ./install.sh --publish  also push a summary to a site you run
#
# The default installs nothing that talks to the internet. The collector writes
# to disk and the panel reads from disk, both on this machine, and that is the
# whole loop. --publish adds the one piece that sends anything anywhere, and it
# is opt-in on purpose.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUBLISH=0
[[ "${1:-}" == "--publish" ]] && PUBLISH=1

say() { printf '\n[darkscope] %s\n' "$*"; }
die() { printf '\n[darkscope] FATAL: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || die "need curl"
command -v python3 >/dev/null || die "need python3"
if ! command -v jq >/dev/null; then
  say "jq not found — installing"
  sudo apt-get update -qq && sudo apt-get install -y jq
fi

# ---------- collector ---------------------------------------------------------

say "collector → /usr/local/bin/"
sudo install -m 755 "$HERE/collector/darknode-export.sh" /usr/local/bin/darknode-export.sh
sudo install -m 755 "$HERE/collector/dnet-record.sh"     /usr/local/bin/dnet-record.sh
sudo install -m 755 "$HERE/collector/darknode-digest.py" /usr/local/bin/darknode-digest.py
sudo install -m 755 "$HERE/collector/darkfid-blocks.sh"  /usr/local/bin/darkfid-blocks.sh

say "smoke test: the collector must emit a non-empty JSON object"
DRY_RUN=1 /usr/local/bin/darknode-export.sh 2>/dev/null \
  | jq -e 'type == "object" and (keys | length > 0)' >/dev/null \
  || die "collector produced empty or invalid JSON. Inspect with:
      DRY_RUN=1 /usr/local/bin/darknode-export.sh | jq ."

say "units → /etc/systemd/system/"
for u in darknode-export.service darknode-export.timer \
         darknode-digest.service darknode-digest.timer \
         darkfid-blocks.service dnet-record.service; do
  sudo install -m 644 "$HERE/collector/$u" "/etc/systemd/system/$u"
done
[[ "$PUBLISH" == "1" ]] && sudo install -m 644 "$HERE/collector/node-pulse.service" /etc/systemd/system/node-pulse.service
[[ "$PUBLISH" == "1" ]] && sudo install -m 755 "$HERE/collector/node-pulse.py" /usr/local/bin/node-pulse.py

# ---------- panel -------------------------------------------------------------

say "server + panel → /usr/local/share/darkscope/"
[[ -f "$HERE/panel/dist/index.html" ]] || die "panel/dist is missing. It is committed, so
either the clone is incomplete or it was removed. Rebuild with:
      cd panel && npm install && npm run build"
sudo install -d -m 755 /usr/local/share/darkscope
sudo install -m 755 "$HERE/server/server.py" /usr/local/share/darkscope/server.py
sudo cp -a "$HERE/panel/dist" /usr/local/share/darkscope/dist
if [[ -f /etc/darkscope/machines.json ]]; then
  say "/etc/darkscope/machines.json exists — leaving it alone"
else
  sudo install -d -m 755 /etc/darkscope
  sudo install -m 644 "$HERE/server/machines.example.json" /etc/darkscope/machines.json
  say "wrote /etc/darkscope/machines.json — edit it to name your machines"
fi
sudo install -m 644 "$HERE/server/darkscope.service" /etc/systemd/system/darkscope.service

# ---------- config ------------------------------------------------------------

if [[ -f /etc/darknode-export.env ]]; then
  say "/etc/darknode-export.env exists — leaving it alone"
else
  say "writing /etc/darknode-export.env from the example"
  sudo install -m 600 "$HERE/collector/darknode-export.env.example" /etc/darknode-export.env
fi

sudo systemctl daemon-reload

# ---------- start it ----------------------------------------------------------

say "starting the recorder and the panel"
sudo systemctl enable --now dnet-record.service
sudo systemctl enable --now darknode-export.timer
sudo systemctl enable --now darkscope.service

sleep 2
ok=1
for s in dnet-record darkscope; do
  state="$(systemctl is-active "$s" || true)"
  printf '  %-18s %s\n' "$s" "$state"
  [[ "$state" == "active" ]] || ok=0
done

port="$(grep -oP '(?<=^PORT=)\d+' /etc/darkscope.env 2>/dev/null || echo 8080)"

cat <<EOF

────────────────────────────────────────────────────────────
  open  http://localhost:${port}
        (from another machine on your network, use this
         machine's address — the panel serves no peer
         addresses, only pseudonyms)

  it fills up as your node talks. If the graph is empty,
  the node is quiet or dnet-record cannot reach it:
        journalctl -u dnet-record -n 30 --no-pager
────────────────────────────────────────────────────────────
EOF

if [[ "$PUBLISH" == "1" ]]; then
  cat <<'EOF'
  PUBLISHING is installed but not started. It needs two values
  in /etc/darknode-export.env:

      NODE_INGEST_TOKEN=<the same value your site expects>
      INGEST_URL=https://your-site.example/api/node-ingest

  then:
      sudo systemctl enable --now node-pulse.service
      sudo systemctl start darknode-export.service
      journalctl -u darknode-export -n 20 --no-pager

  Read what it sends before you turn it on. It is counts and
  pseudonyms, never addresses — but it is your node, so check.
EOF
fi

[[ "$ok" == "1" ]] || die "something did not come up — see the states above"
