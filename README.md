# darknode-exporter

Push-only telemetry for a DarkFi node running on a Raspberry Pi. The Pi
assembles **one JSON snapshot** (miner hashrate, cgroup memory budget, SoC
temperature, sync height) and **POSTs it out** to a small web endpoint every
60s. Nothing on the internet ever dials into the Pi: `xmrig` stays on
`127.0.0.1` and `darkfid`'s RPC stays local — the exporter only reads them
locally and pushes a redacted summary outward.

Companion to the write-up [*The Node That Killed Its Own
Host*](https://www.reymom.xyz/blog/security/2026-07-14-node-killing-its-own-host)
and the live panel at [reymom.xyz/darknode](https://www.reymom.xyz/darknode).
The receiver (a Next.js route + KV store) lives in a separate site repo; this
repo is just the Pi side.

```
Pi  ──reads localhost──►  xmrig /2/summary · systemctl memory · vcgencmd · journald
    ──assembles + redacts──►  one JSON
    ──POST (Bearer token)──►  https://<site>/api/node-ingest        ← outbound only
```

## Files

| File | Role |
|---|---|
| `darknode-export.sh` | collector + POST |
| `darknode-export.service` | oneshot unit |
| `darknode-export.timer` | fires every 60s |
| `darknode-export.env.example` | env template → `/etc/darknode-export.env` (mode 600) |
| `install.sh` | installs the above from this directory |
| `sample-snapshot.json` | shape reference |

## Install — no git on the Pi

Get these files onto the Pi (**pick one**), then run `./install.sh`.

**A. Copy from your laptop** (simplest — uses the SSH you already have):

```bash
# on your laptop, from a checkout of this repo:
scp -r . rey@darknode:~/darknode-exporter
ssh rey@darknode
cd ~/darknode-exporter && ./install.sh
```

**B. Download the repo tarball on the Pi** (no git, no clone):

```bash
# on the Pi:
curl -fsSL https://github.com/reymom/darknode-exporter/archive/refs/heads/main.tar.gz | tar xz
cd darknode-exporter-main && ./install.sh
```

`install.sh` copies the collector to `/usr/local/bin`, installs the systemd
units, seeds `/etc/darknode-export.env` (mode 600), and reloads systemd. It
does **not** start anything until you add your token.

## Configure (`/etc/darknode-export.env`)

```ini
# same value you set as NODE_INGEST_TOKEN in the site's env
NODE_INGEST_TOKEN=<generate once: openssl rand -hex 32>
INGEST_URL=https://www.reymom.xyz/api/node-ingest
XMRIG_API=http://127.0.0.1:18088       # xmrig HTTP API, restricted (no token), localhost
# optional overrides:
# DARKFID_UNIT=darkfid.service
# XMRIG_UNIT=xmrig.service
# WASM_TAIL_N=12
```

## Run + verify

```bash
# dry run — prints the exact JSON it would send, POSTs nothing:
DRY_RUN=1 /usr/local/bin/darknode-export.sh | jq .

# one real POST now:
sudo systemctl start darknode-export.service
journalctl -u darknode-export.service -n 20 --no-pager

# enable the 60s timer:
sudo systemctl enable --now darknode-export.timer
systemctl list-timers | grep darknode
```

The optional WASM-log tail needs journal access — if you run the service as a
dedicated non-root user, add it to the journal group:
`sudo usermod -aG systemd-journal <user>`.

## Requirements

`curl`, `jq`, and (on a Pi) `vcgencmd`. The xmrig HTTP API must be enabled in
**restricted mode** — add `--http-host 127.0.0.1 --http-port 18088` to xmrig's
`ExecStart`. No access token: restricted mode is read-only, and binding to
localhost keeps it off the network.

## Privacy

- The wallet `-u` address is never put into the JSON.
- The collector redacts `dark1…` / long base58 / `0x…` from the WASM journal tail.
- Peer **count** only — never peer IPs or a map (DarkFi is an anonymity network).

## License

MIT — see [LICENSE](LICENSE).
