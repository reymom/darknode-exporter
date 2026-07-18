# darknode-exporter

Push-only telemetry for a [DarkFi](https://dark.fi) node on a Raspberry Pi.

A small collector runs on the Pi, reads the miner and node **locally**, assembles
one redacted JSON snapshot, and **POSTs it out** to an HTTP endpoint every 60s.
Nothing on the internet ever connects *to* the Pi: `xmrig` stays bound to
localhost and `darkfid`'s RPC never leaves the box — the exporter only reads them
locally and pushes a summary outward.

```
Pi ── reads localhost ──► xmrig /2/summary · systemctl memory · vcgencmd · journald
   ── assembles + redacts ──► one JSON
   ── POST (Bearer token) ──► https://<your-site>/api/node-ingest   (outbound only)
```

It pairs with a small web receiver (an HTTP route + a key-value store) that keeps
the last snapshot and renders it. This repo is only the Pi side; the receiver is
your own.

## Requirements

- `curl` and `jq` (installed automatically if missing)
- `vcgencmd` for SoC temperature (present on Raspberry Pi OS; skipped elsewhere)
- an xmrig HTTP API in **restricted** (read-only) mode — add
  `--http-host 127.0.0.1 --http-port 18088` to xmrig's `ExecStart`. No access
  token: restricted mode is read-only and bound to localhost.

## Install

```bash
git clone https://github.com/reymom/darknode-exporter.git
cd darknode-exporter
./install.sh
```

`install.sh` copies the collector to `/usr/local/bin`, installs the systemd
service + timer, and seeds `/etc/darknode-export.env` (mode 600). It starts
nothing until you fill that file in.

## Configure

Edit `/etc/darknode-export.env`:

```ini
# shared secret — the same value must be set on the receiver
NODE_INGEST_TOKEN=<openssl rand -hex 32>
INGEST_URL=https://your-site.example/api/node-ingest
XMRIG_API=http://127.0.0.1:18088
# optional overrides:
# DARKFID_UNIT=darkfid.service
# XMRIG_UNIT=xmrig.service
# WASM_TAIL_N=12
```

## Run

```bash
# dry run — prints the exact JSON it would send, POSTs nothing:
DRY_RUN=1 /usr/local/bin/darknode-export.sh | jq .

# one POST now:
sudo systemctl start darknode-export.service
journalctl -u darknode-export.service -n 20 --no-pager

# enable the 60s timer:
sudo systemctl enable --now darknode-export.timer
```

The optional WASM-log tail needs journal access. If you run the service as a
dedicated non-root user, add it to the journal group:
`sudo usermod -aG systemd-journal <user>`.

## What it sends

See [`sample-snapshot.json`](sample-snapshot.json) for the full shape: miner
hashrate (per-window + per-thread), hugepages, algo, uptime; the cgroup memory
budget for `darkfid` and `xmrig` (current / high / max / swap) plus host memory;
SoC temperature and throttle state; sync height/tip; and an optional tail of
`[WASM]` log lines.

## Local history

The exporter also appends every snapshot to `/var/log/darknode/snapshots-YYYY-MM-DD.jsonl`
(one line per minute, gzipped after a day, expired after `HISTORY_MAX_DAYS`,
default 120). This happens *before* the POST, so history accumulates even when
the site is unreachable — outages are data too. Disable with `HISTORY_DIR=""`.

`dnet-record.sh` (+ `dnet-record.service`) is a companion long-running service
that subscribes to darkfid's own **dnet** P2P instrumentation stream over the
localhost management RPC and appends every event (per-channel send/recv,
peer-discovery states, slot lifecycle) to `/var/log/darknode/dnet-YYYY-MM-DD.jsonl`.
Raw dnet events carry peer addresses, so **they never leave the Pi** — only
aggregates may be published.

Height / tip / peers are now read from darkfid's localhost JSON-RPC
(`blockchain.last_confirmed_block`, `blockchain.best_fork_next_block_height`)
and an `ss` count of established P2P sessions, with the journal scrape kept as
fallback.

## Privacy

- The wallet address is never included in the snapshot.
- The collector strips terminal color codes and redacts `dark1…` / long base58 /
  `0x…` tokens from the WASM log tail.
- Peer **count** only — never peer IPs (DarkFi is an anonymity network).

## License

MIT — see [LICENSE](LICENSE).
