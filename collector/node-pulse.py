#!/usr/bin/env python3
"""node-pulse — feed the live graph, one message at a time.

Reads what dnet-record is already writing and turns it into the smallest thing a
renderer needs: when, which peer, which direction, which command. Batches once a
second and POSTs them out. Addresses never leave the machine: every peer gets a
stable pseudonym, and the ones both machines share get a well-known one so the
two graphs can be drawn as one network.

It also writes every batch it sends to a local file. That file *is* the fallback
for the stage: the same player can be fed from it, so a venue with no wifi shows
the real thing replayed rather than a screenshot.

  NODE_ID            darknode | obs
  PULSE_URL          https://…/api/node-pulse      (unset = collect only)
  NODE_INGEST_TOKEN  bearer
  DNET_DIR           /var/log/darknode             (where dnet-*.jsonl live)
  PULSE_OUT          /var/log/darknode/pulse-YYYY-MM-DD.jsonl
"""
import json, os, time, glob, urllib.request, urllib.error, sys, datetime

NODE = os.environ.get("NODE_ID", "darknode")
URL = os.environ.get("PULSE_URL", "").strip()
TOKEN = os.environ.get("NODE_INGEST_TOKEN", "").strip()
DNET_DIR = os.environ.get("DNET_DIR", "/var/log/darknode")
IDS_FILE = os.environ.get("PULSE_IDS", "/var/log/darknode/pulse-ids.json")
BATCH_S = 1.0
# Anything older than this at startup is history, not liveness.
MAX_AGE_S = 10

# The peers both machines have in common get the same name on both, which is what
# lets one graph be drawn from two nodes. Everyone else is p1, p2, … in order of
# first sight, kept in a file so a restart does not reshuffle the picture.
WELL_KNOWN = {
    "lilith0.dark.fi": "seed-0",
    "lilith1.dark.fi": "seed-1",
    "node0.testnet.dark.fi": "node-0",
    "node1.testnet.dark.fi": "node-1",
}

# Peers that are themselves machines you run get that machine's name instead of a
# pseudonym, which is what draws the direct link between two of your own nodes
# rather than an anonymous peer on each side of the graph.
#
# ADDRESSES BELONG IN YOUR CONFIG, NEVER IN THIS FILE. Set PEER_NODE_MAP to a
# JSON object in the environment file:
#
#   PEER_NODE_MAP={"203.0.113.7":"my-vps","pi.example":"my-pi"}
#
# Nothing here is published: the map turns an address into a name locally, and
# only the name is ever sent.
PEERS_ARE_NODES = {}
try:
    PEERS_ARE_NODES = dict(json.loads(os.environ.get("PEER_NODE_MAP", "{}")))
except Exception:
    print("[pulse] PEER_NODE_MAP is not valid JSON — ignoring it", file=sys.stderr)
_other_addr = os.environ.get("OTHER_NODE_ADDR", "").strip()
if _other_addr:
    PEERS_ARE_NODES[_other_addr] = os.environ.get("OTHER_NODE_NAME", "other-node")

def load_ids():
    try:
        with open(IDS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}

def save_ids(ids):
    try:
        tmp = IDS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(ids, fh)
        os.replace(tmp, IDS_FILE)
    except Exception:
        pass

ids = load_ids()

def peer_id(addr: str) -> str:
    """A stable pseudonym, always `[a-z0-9-]` — the route rejects anything else,
    which is the rule that keeps an address from ever leaving by accident."""
    host = addr.split("//", 1)[-1].rstrip("/")
    host = host.rsplit(":", 1)[0] if ":" in host else host
    if host in PEERS_ARE_NODES:
        return PEERS_ARE_NODES[host]
    for known, name in WELL_KNOWN.items():
        if host == known:
            return name
    if host not in ids:
        ids[host] = "p%d" % (1 + sum(1 for v in ids.values() if v.startswith("p")))
        save_ids(ids)
    return ids[host]

def today_path(prefix):
    # LOCAL date, not UTC: everything else on this box names its files with
    # `date +%F`, which is local. Naming the pulse log in UTC put it a day behind
    # its siblings for an hour every night under BST.
    day = datetime.datetime.now().strftime("%Y-%m-%d")
    return os.path.join(DNET_DIR, f"{prefix}-{day}.jsonl")

def live_dnet():
    """The dnet file being written right now, whichever it is called.

    Follow the WRITER, not a name we compute. This used to be
    `today_path("dnet")`, and that cost an hour of silence every night: this
    process built the name in UTC while dnet-record.sh built it with a local
    `date +%F`, so from local midnight until UTC midnight it tailed yesterday's
    finished file — and since follow() only reopened when the name CHANGED, it
    sat on a dead file, service active, for the whole hour. (2026-09-28: that
    window swallowed a take of the Tor recording, and nothing reported an error.)

    Reading the directory removes the whole class: no date convention to agree
    on, and a rotation or a restart that lands events in a differently-named file
    is followed rather than missed. gzip'd history never matches the glob.
    """
    names = glob.glob(os.path.join(DNET_DIR, "dnet-*.jsonl"))
    return max(names, key=os.path.getmtime) if names else None

def follow():
    """Tail the current dnet file, rolling over at midnight, tolerating a gap."""
    path, fh = None, None
    while True:
        want = live_dnet() or today_path("dnet")
        if want != path:
            if fh:
                fh.close()
            try:
                fh = open(want, "r")
                fh.seek(0, os.SEEK_END)      # only what happens from now on
                path = want
            except OSError:
                time.sleep(2)
                continue
        line = fh.readline()
        if not line:
            yield None                        # nothing right now
            time.sleep(0.1)
            continue
        yield line

def post(batch):
    if not URL or not TOKEN:
        return
    body = json.dumps({"node": NODE, "events": batch}, separators=(",", ":")).encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", "Bearer " + TOKEN)
    try:
        urllib.request.urlopen(req, timeout=4).read()
    except Exception as exc:
        # A dropped batch is a dropped second of animation, never a crash.
        print(f"[pulse] post failed: {exc}", file=sys.stderr)

def main():
    out = open(today_path("pulse"), "a", buffering=1)
    out_day = datetime.datetime.now().day
    batch, last = [], time.time()
    sent = 0

    for line in follow():
        now = time.time()
        if line:
            try:
                o = json.loads(line)
            except Exception:
                o = None
            if o:
                ev = o.get("event")
                info = o.get("info") or {}
                cmd = info.get("cmd")
                chan = info.get("chan") or {}
                addr = chan.get("addr")
                rx = o.get("rx")
                if ev in ("send", "recv") and cmd and addr and rx:
                    if (now - rx / 1000.0) < MAX_AGE_S:
                        batch.append({"t": int(rx) * 1000, "peer": peer_id(addr),
                                      "dir": ev, "cmd": cmd})

        if now - last >= BATCH_S and batch:
            if datetime.datetime.now().day != out_day:
                out.close()
                out = open(today_path("pulse"), "a", buffering=1)
                out_day = datetime.datetime.now().day
            payload = {"node": NODE, "at": int(now * 1000), "events": batch}
            out.write(json.dumps(payload, separators=(",", ":")) + "\n")
            post(batch)
            sent += len(batch)
            batch, last = [], now

if __name__ == "__main__":
    main()
