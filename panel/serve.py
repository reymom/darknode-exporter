#!/usr/bin/env python3
"""
darkscope panel — serve your own node's network, from your own machine.

No framework, no build step, no npm, nothing to install. Python's standard
library and one HTML file. That is deliberate: the whole point of this project
is that a node fits on small hardware, and a viewer that needs a toolchain
bigger than the node would be a joke.

    ./serve.py                      # then open http://localhost:8080
    DNET_DIR=/var/log/darknode PORT=8080 ./serve.py

What it reads, both written by the collector next door:

  DNET_DIR/dnet-YYYY-MM-DD.jsonl   one line per P2P message, from darkfid's
                                   own debug feed. This is the network.
  DNET_DIR/snapshots-*.jsonl       height, memory, peers, uptime, once a minute.
                                   The last line of the newest file is "now".

WHAT IT NEVER SERVES: a peer address. Addresses are mapped to p1, p2, … before
anything leaves this process, and the map is kept in DNET_DIR/panel-ids.json so
the names survive a restart. If you point a browser at this from another
machine, the browser still learns nothing about who your peers are.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DNET_DIR = os.environ.get("DNET_DIR", "/var/log/darknode")
SNAPSHOT = os.environ.get("SNAPSHOT", os.path.join(DNET_DIR, "snapshot.json"))
NODE_NAME = os.environ.get("NODE_NAME", "my node")
PORT = int(os.environ.get("PORT", "8080"))
IDS_FILE = os.environ.get("PANEL_IDS", os.path.join(DNET_DIR, "panel-ids.json"))
MAX_EVENTS = 4000

# --- peer pseudonyms ---------------------------------------------------------

_ids: dict[str, str] = {}


def _load_ids() -> None:
    global _ids
    try:
        with open(IDS_FILE) as fh:
            _ids = json.load(fh)
    except Exception:
        _ids = {}


def _save_ids() -> None:
    try:
        tmp = IDS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_ids, fh)
        os.replace(tmp, IDS_FILE)
    except Exception:
        pass


def peer_id(addr: str) -> str:
    """An address goes in, a pseudonym comes out, and only the pseudonym leaves."""
    host = re.sub(r"^[a-z0-9+]+://", "", addr or "").split("/")[0]
    if not host:
        return "p?"
    if host not in _ids:
        _ids[host] = "p%d" % (1 + len(_ids))
        _save_ids()
    return _ids[host]


# --- the dnet feed -----------------------------------------------------------


def live_dnet() -> str | None:
    """The dnet file being written right now, whatever it is called.

    Follow the writer rather than a name built from today's date: this process
    and the collector can disagree about what day it is (they did, for an hour
    every night, until that was found), and a rotation should be followed rather
    than missed.
    """
    names = glob.glob(os.path.join(DNET_DIR, "dnet-*.jsonl"))
    return max(names, key=os.path.getmtime) if names else None


def read_events(since: int) -> tuple[int, list[dict]]:
    path = live_dnet()
    if not path:
        return 0, []
    size = os.path.getsize(path)
    # A new file, or a truncated one: start from the end rather than replaying.
    if since <= 0 or since > size:
        return size, []
    out: list[dict] = []
    with open(path, "r", errors="replace") as fh:
        fh.seek(since)
        for line in fh:
            if len(out) >= MAX_EVENTS:
                break
            try:
                o = json.loads(line)
            except Exception:
                continue
            ev = o.get("event")
            info = o.get("info") or {}
            chan = info.get("chan") or {}
            cmd, addr, rx = info.get("cmd"), chan.get("addr"), o.get("rx")
            if ev in ("send", "recv") and cmd and addr and rx:
                out.append({"t": int(rx), "peer": peer_id(addr), "dir": ev, "cmd": cmd})
        cursor = fh.tell()
    return cursor, out


def latest_snapshot() -> dict | None:
    """The newest snapshot the collector wrote.

    It appends one JSON object per minute to `snapshots-YYYY-MM-DD.jsonl`, so the
    last line of the newest of those is the current state. `SNAPSHOT` overrides it
    with a single-object file if you would rather keep one.
    """
    if os.path.exists(SNAPSHOT):
        try:
            with open(SNAPSHOT) as fh:
                return json.load(fh)
        except Exception:
            pass
    names = glob.glob(os.path.join(DNET_DIR, "snapshots-*.jsonl"))
    if not names:
        return None
    try:
        with open(max(names, key=os.path.getmtime), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            # the last line, without reading a day of samples to get it
            back = min(end, 65536)
            fh.seek(end - back)
            tail = fh.read().splitlines()
        for line in reversed(tail):
            try:
                return json.loads(line)
            except Exception:
                continue
    except Exception:
        pass
    return None


def read_live() -> dict:
    s = latest_snapshot()
    if not s:
        return {"node": NODE_NAME, "ok": False}
    mem = (s.get("memory") or {}).get("darkfid") or {}
    return {
        "node": NODE_NAME,
        "ok": True,
        "height": s.get("height"),
        "tip": s.get("tip"),
        "peers": s.get("peers"),
        "uptime": s.get("uptime"),
        "tempC": s.get("tempC"),
        "held": mem.get("current"),
        "high": mem.get("high"),
        "max": mem.get("max"),
        "at": s.get("exportedAt"),
        "now": int(datetime.datetime.now().timestamp() * 1000),
    }


# --- the server --------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet: this runs next to a node, not in a terminal
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode(), "application/json")

    def _file(self, name: str, ctype: str) -> None:
        try:
            with open(os.path.join(HERE, name), "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(404, b"not found", "text/plain")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if u.path == "/app.js":
            return self._file("app.js", "application/javascript")
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if u.path == "/api/live":
            return self._json(read_live())
        if u.path == "/api/pulse":
            q = parse_qs(u.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            cursor, events = read_events(since)
            return self._json({"cursor": cursor, "events": events, "now": int(datetime.datetime.now().timestamp() * 1000)})
        self._send(404, b"not found", "text/plain")


def main() -> None:
    _load_ids()
    if not os.path.isdir(DNET_DIR):
        print(f"DNET_DIR does not exist: {DNET_DIR}", file=sys.stderr)
        print("Set it to wherever dnet-record is writing, e.g.", file=sys.stderr)
        print("  DNET_DIR=/var/log/darknode ./serve.py", file=sys.stderr)
        sys.exit(1)
    f = live_dnet()
    print(f"darkscope panel on http://localhost:{PORT}")
    print(f"  dnet     {f or '(nothing yet — is dnet-record running?)'}")
    print(f"  snapshot {'ok' if latest_snapshot() else '(none yet — is darknode-export running?)'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
