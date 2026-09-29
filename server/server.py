#!/usr/bin/env python3
"""
darkscope receiver — collects from every machine you run and serves the panel.

This is the middle of three parts that do not know about each other:

    collector (on each node)  →  THIS  →  panel (a pure receiver)

The collector pushes; this holds a short buffer and hands it out; the panel
draws whatever it is given. The panel never talks to a node, never sees an
address, and does not care how anything was collected. Run this wherever you
like — the same Pi, a laptop, a box on the internet.

    PORT=8080 MACHINES='[{"id":"my-pi","label":"Raspberry Pi 5"},
                         {"id":"my-vps","label":"rented server"}]' ./server.py

Or put the same JSON in machines.json next to this file.

AUTHENTICATION is one shared token, in DARKSCOPE_TOKEN. Collectors send it as a
bearer. Reading is open by default because the data is counts and pseudonyms;
set DARKSCOPE_READ_TOKEN too if you would rather it were not.
"""
from __future__ import annotations

import json
import os
import sys
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.environ.get("DIST", os.path.join(HERE, "..", "panel", "dist"))
PORT = int(os.environ.get("PORT", "8080"))
TOKEN = os.environ.get("DARKSCOPE_TOKEN", "")
READ_TOKEN = os.environ.get("DARKSCOPE_READ_TOKEN", "")
KEEP_MS = int(os.environ.get("KEEP_MS", str(5 * 60 * 1000)))
MAX_BATCHES = 4000

# A pseudonym, and nothing that could be an address. The collector already maps
# addresses to p1, p2, … before sending, but "no address leaves the machine"
# should not rest on one function in another process: anything that does not
# look like a pseudonym is dropped here too.
PEER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")

_lock = threading.Lock()
_batches: deque = deque(maxlen=MAX_BATCHES)  # (seq, batch)
_seq = 0
_live: dict[str, dict] = {}


def machines() -> list[dict]:
    raw = os.environ.get("MACHINES")
    if not raw:
        try:
            with open(os.environ.get("MACHINES_FILE", os.path.join(HERE, "machines.json"))) as fh:
                raw = fh.read()
        except OSError:
            return []
    try:
        m = json.loads(raw)
        return [{"id": x["id"], "label": x.get("label", x["id"]), "desc": x.get("desc", "")} for x in m]
    except Exception as exc:
        print(f"[darkscope] MACHINES is not valid JSON: {exc}", file=sys.stderr)
        return []


MACHINES = machines()
KNOWN = {m["id"] for m in MACHINES}


def _prune(now: int) -> None:
    while _batches and _batches[0][1]["at"] < now - KEEP_MS:
        _batches.popleft()


class Handler(BaseHTTPRequestHandler):
    server_version = "darkscope"

    def log_message(self, *a):
        pass

    # --- plumbing ---

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _authed(self, required: str) -> bool:
        if not required:
            return True
        return self.headers.get("authorization", "") == f"Bearer {required}"

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("content-length", "0"))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return None

    # --- collectors push here ---

    def do_POST(self) -> None:
        if not self._authed(TOKEN):
            return self._json({"error": "unauthorized"}, 401)
        u = urlparse(self.path)
        b = self._body()
        if b is None:
            return self._json({"error": "bad json"}, 400)
        now = int(time.time() * 1000)

        if u.path == "/api/node-pulse":
            node = b.get("node")
            if node not in KNOWN:
                return self._json({"error": f"unknown machine {node!r}"}, 400)
            evs = [
                e for e in (b.get("events") or [])
                if isinstance(e, dict)
                and isinstance(e.get("peer"), str) and PEER_RE.match(e["peer"])
                and isinstance(e.get("cmd"), str) and CMD_RE.match(e["cmd"])
                and e.get("dir") in ("send", "recv")
                and isinstance(e.get("t"), int)
            ]
            if evs:
                global _seq
                with _lock:
                    _seq += 1
                    _batches.append((_seq, {"node": node, "at": now, "events": evs}))
                    _prune(now)
            return self._json({"ok": True})

        if u.path == "/api/node-ingest":
            node = b.get("node")
            if node not in KNOWN:
                return self._json({"error": f"unknown machine {node!r}"}, 400)
            b["receivedAt"] = now
            with _lock:
                _live[node] = b
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)

    # --- the panel reads here ---

    def do_GET(self) -> None:
        u = urlparse(self.path)

        if u.path.startswith("/api/"):
            if not self._authed(READ_TOKEN):
                return self._json({"error": "unauthorized"}, 401)
            now = int(time.time() * 1000)

            if u.path == "/api/config":
                return self._json({"machines": MACHINES})

            if u.path == "/api/node-pulse":
                try:
                    since = int(parse_qs(u.query).get("since", ["0"])[0])
                except ValueError:
                    since = 0
                with _lock:
                    _prune(now)
                    out = [b for s, b in _batches if s > since]
                    cursor = _batches[-1][0] if _batches else since
                return self._json({"now": now, "cursor": str(cursor), "batches": out})

            if u.path == "/api/node-live":
                with _lock:
                    nodes = []
                    for m in MACHINES:
                        s = _live.get(m["id"])
                        if not s:
                            continue
                        mem = (s.get("memory") or {}).get("darkfid") or s.get("memory") or {}
                        num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                        nodes.append({
                            "node": m["id"],
                            "height": num(s.get("height")),
                            "tip": num(s.get("tip")),
                            "peers": num(s.get("peers")),
                            "uptime": num(s.get("uptime")),
                            "memory": {"current": num(mem.get("current")), "high": num(mem.get("high")),
                                       "max": num(mem.get("max"))},
                            "receivedAt": num(s.get("receivedAt")),
                        })
                return self._json({"now": now, "nodes": nodes})

            return self._json({"error": "not found"}, 404)

        # the built panel
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        root = os.path.realpath(DIST)
        path = os.path.realpath(os.path.join(root, rel))
        if path != root and not path.startswith(root + os.sep):
            return self._send(403, b"no", "text/plain")
        if not os.path.isfile(path):
            path = os.path.join(DIST, "index.html")
        ctype = {
            ".html": "text/html; charset=utf-8", ".js": "application/javascript",
            ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
            ".json": "application/json", ".ico": "image/x-icon",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        try:
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    if not MACHINES:
        print("[darkscope] no machines configured.", file=sys.stderr)
        print("Set MACHINES or write machines.json, e.g.", file=sys.stderr)
        print('  [{"id":"my-pi","label":"Raspberry Pi 5"}]', file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(DIST):
        print(f"[darkscope] no built panel at {DIST}", file=sys.stderr)
        print("  cd panel && npm install && npm run build", file=sys.stderr)
        sys.exit(1)
    print(f"darkscope on http://localhost:{PORT}")
    for m in MACHINES:
        print(f"  {m['id']:16s} {m['label']}")
    if not TOKEN:
        print("  WARNING: DARKSCOPE_TOKEN is unset, so anything can push to this", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
