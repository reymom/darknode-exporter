# darkscope

Watch every DarkFi node you run on one live graph, drawn from what the nodes already
report about themselves, on hardware you own.

```bash
git clone https://github.com/reymom/darkscope && cd darkscope && ./install.sh
# then open http://localhost:8080
```

There is no account and no dashboard service, it runs on your machine and the data stays
there, and the panel is committed already built so cloning and running needs no npm. You
only need npm if you want to change the graph.

![the panel](docs/panel.png)

---

## Why this exists

I ran a node on a Raspberry Pi for three months and it told me things that were not written
down anywhere, like that its memory was tracking whatever ceiling I gave it rather than what
it needed, and that a four gigabyte server dies in the same four hundred blocks every time,
and that one transaction of mine announced which machine it had come from. I could not see
any of that until I could see the node.

The failure modes that only appear when a machine is short of room have very few people
looking at them, because the people building these systems are working on machines that have
room, which is reasonable. So every operator who can see their own node is another pair of
eyes, and this is the seeing part.

## What you get

The panel puts your machines on one graph with the peers they are talking to around them,
and draws every message as it happens. For each machine it shows height, memory against
whatever limit it has, peer count and uptime, and underneath it shows what share of the
traffic is peers asking each other who exists, which is most of it.

The collector writes to disk on your own machine, once a minute for the machine state and
continuously for the P2P feed. That history is what the panel reads and it is yours: plain
JSONL, one file a day, gzipped after a day, and expired after `HISTORY_MAX_DAYS`.

## What is real in the picture and what is not

The events and their times are real, to the millisecond, straight from `darkfid`'s own `dnet`
feed, so nothing on the graph is simulated or inferred. How long a dot takes to cross the
screen is not real, it comes from how long that line is, and it is the only thing on the page
that is drawn rather than measured. And the whole feed plays two seconds behind, because
events arrive in batches and playing them the moment they land gives you clumps instead of
motion.

## It never handles a peer address

Addresses are mapped to `p1`, `p2` and so on inside the server, before anything is
serialised, so the browser cannot learn who your peers are and neither can anyone you show
the page to. The map lives in `panel-ids.json` next to your logs so the names survive a
restart.

That matters more than it sounds, because a panel like this is a deanonymization surface and
getting it wrong is easy. There is an open issue about the parts of it I have not fixed yet,
and it is about this repository rather than about anyone else's code.

## Layout

Three parts that do not know about each other:

```
collector (on each node)  →  server (receives)  →  panel (a pure receiver)
```

The panel talks to no node and holds no address. It asks the server what machines exist and
draws whatever it is given, which is why it works for one machine or for six.

```
collector/    what runs next to each node
  dnet-record.sh       subscribes to darkfid's dnet feed → dnet-YYYY-MM-DD.jsonl
  darknode-export.sh   one machine snapshot a minute → snapshots-YYYY-MM-DD.jsonl
  darkfid-blocks.sh    one line per applied block: height, calls, gas, contracts
  darknode-digest.py   daily rollup
  node-pulse.py        pushes pseudonyms and counts to your server — never an address
  darkfid-memguard.*   OPTIONAL — a watchdog for when you are still finding your limits
server/       receives from every machine, holds five minutes, serves the panel
  server.py            stdlib only. Bearer token from collectors, read endpoints out
  machines.example.json
panel/        the graph
  src/                 three.js + 3d-force-graph. The same one at reymom.xyz/darknode
  dist/                committed, so cloning needs no npm
bench/        does your node fit on this box?
  bench.sh      one arm: same database, one environment, what it cost
  snapshot.sh   the restore point every arm starts from
  sampler.sh    memory every 5 s
  results/      the series behind the measurements, and the raw run log
install.sh
```

## Also here: the bench

[`bench/`](bench/) is the rig that found the memory behaviour in the first place. It restores
the same chain database, changes one environment variable, and sees whether the node survives
the four hundred blocks that carry the transactions. On a four gigabyte server the default
configuration died four syncs out of seven. The raw series are in `bench/results/`.

## Running it

**Locally, which is the default.** Nothing leaves the machine.

```bash
./bench/        does your node fit on this box?
  bench.sh      one arm: same database, one environment, what it cost
  snapshot.sh   the restore point every arm starts from
  sampler.sh    memory every 5 s
  results/      the series behind the measurements, and the raw run log
install.sh
```

**Publishing to a server you run**, which is what I do, and which adds the only component
that sends anything anywhere:

```bash
./install.sh --publish
# then set NODE_INGEST_TOKEN and INGEST_URL in /etc/darknode-export.env
```

**Your machines**, in `server/machines.json`. The order decides the colours, and a machine
keeps its colour when you add another:

```json
[
  {"id": "my-pi",  "label": "my-pi",  "desc": "Raspberry Pi 5 · 8 GB · ARM"},
  {"id": "my-vps", "label": "my-vps", "desc": "rented server · 4 GB"}
]
```

**Changing the graph** needs the toolchain, and only then:

```bash
cd panel && npm install && npm run build
```

## Configuration

Everything is environment variables, in `/etc/darknode-export.env` for the collector and
`/etc/darkscope.env` for the panel.

| | |
|---|---|
| `DARKFID_UNIT` | the systemd unit your node runs as, so memory can be read from its cgroup |
| `DARKFID_MGMT_RPC_PORT` | where `dnet` is, default `18346` |
| `HISTORY_DIR` | where everything is written, default `/var/log/darknode` |
| `HISTORY_MAX_DAYS` | how long to keep it |
| `PORT` | the server |
| `DARKSCOPE_TOKEN` | what collectors must present to push |
| `DARKSCOPE_READ_TOKEN` | optional, if reading should not be open either |
| `PEER_NODE_MAP` | JSON: which peer addresses are your own machines, so they are drawn as machines rather than as an anonymous peer on each side. The address never leaves the file |
| `INGEST_URL`, `NODE_INGEST_TOKEN` | only if you are publishing |

## Requirements

A Linux machine running `darkfid` under systemd, with `curl`, `jq` and `python3`. The
collector is standard library only. Node is needed only to rebuild the panel. It was built on
a Raspberry Pi 5 and a 4 GB x86 VPS, and it assumes nothing else.

## Licence

See [LICENSE](LICENSE). Use it and change it, and if you find something on your own hardware
that nobody has written down, it is worth saying so somewhere.
