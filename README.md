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

`darkfid-blocks.sh` (+ `darkfid-blocks.service`) follows darkfid's journal and
appends one line per applied block to `/var/log/darknode/blocks.tsv`: when it was
applied, its height, how many contract calls it carried and how much gas they
burned. The journal itself is volatile here (it lives in RAM, so a reboot erases
it), and this is what lets the digest show the chain's own activity over its
whole length rather than over the recording window.

Height / tip / peers are now read from darkfid's localhost JSON-RPC
(`blockchain.last_confirmed_block`, `blockchain.best_fork_next_block_height`)
and an `ss` count of established P2P sessions, with the journal scrape kept as
fallback.

## What the digest adds (2026-09-24)

The digest is aggregate-only and additive: fields get added, never renamed, so an
older receiver keeps working against a newer digest. The latest additions, all
optional:

| Field | What it is |
|---|---|
| `series.hashrate`, `series.difficulty` | the miner's hash rate and the network difficulty it is working against, per bucket |
| `series.blocksPerHour` | block production, counted only across samples where the node was within 5 blocks of the tip **and** moving no faster than four times the chain's target. The lag test alone is not enough: while resyncing, darkfid reports its own chain, so height equals tip and a node replaying thousands of blocks looks caught up |
| `retention` | anon now against the node's floor just after its last restart: what the process is holding and not using |
| `chainActivity` | per-height totals over the whole chain: blocks seen, blocks carrying more than the miner's own reward, calls, gas, calls bucketed by height, `callsByKind` (named from the contracts' own log lines, so it only covers blocks applied since the logger learned to read them) and `perDay` (only blocks the node saw arrive, never a replay) |
| `sources` | how often each published number was answered by darkfid's RPC rather than scraped from its journal, per field. The two are not the same claim, and until now nothing on the page said which one it was showing |
| `overlay.sessions.histogram` | session lengths in log buckets; the median is 33 s and the longest is over a day, so a linear histogram says nothing |
| `overlay.rtt.byCeiling` | round trips split by whether the node was against its memory ceiling at the time |

Snapshots also carry `darkfidStartedAt`, so a restart is an exact observation rather than
something inferred from a drop in memory, and a `sources` object naming where `height`,
`tip`, `peers` and `difficulty` each came from on that sample.

## Privacy

- The wallet address is never included in the snapshot.
- The collector strips terminal color codes and redacts `dark1…` / long base58 /
  `0x…` tokens from the WASM log tail.
- Peer **count** only — never peer IPs (DarkFi is an anonymity network).

## License

MIT — see [LICENSE](LICENSE).
