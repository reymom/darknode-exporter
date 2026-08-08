#!/usr/bin/env python3
"""darknode-digest.py — turn the local JSONL history into one publishable digest.

The Pi keeps everything: a snapshot a minute plus every dnet P2P event. Neither
is publishable as-is — the snapshot series is far too big for a page and the
dnet events carry peer addresses, which by standing rule never leave this
machine. So we aggregate here and POST only the result.

Contract: portfolio-site `src/lib/node-history.ts` (schema v1). Keep the two in
step; the ingest route validates strictly and will 400 on drift.

Usage:
    darknode-digest.py                 # compute and POST
    darknode-digest.py --dry-run       # compute and print a summary
    darknode-digest.py --out d.json    # compute and write to a file
    darknode-digest.py --store DIR     # read history from somewhere else
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import statistics
import sys
import urllib.error
import urllib.request
from collections import defaultdict

SCHEMA_VERSION = 1
MAX_BUCKETS = 720
MIB = 1024 * 1024

# A hole longer than this is a gap in the recording, not a slipped sample.
GAP_MS = 150_000
# cgroup total above this = the memory-ceiling episode the memguard net covers.
CEILING_MIB = 4000
# anon falling by more than this in one step = the process restarted.
RESTART_DROP_MIB = 2000
# height frozen at least this long, with peers, = a stall.
STALL_MS = 30 * 60_000
# Silence longer than this in the dnet stream = the recorder was down, not the
# overlay being quiet. Matches DNET_STALL_S in dnet-record.sh.
DNET_COVERAGE_GAP_MS = 300_000


def load_jsonl(store: str, prefix: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(store, f"{prefix}-*.jsonl*"))):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a torn last line during rotation is not fatal
        except OSError as exc:
            print(f"[digest] skipping {path}: {exc}", file=sys.stderr)
    return rows


def pctl(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[i]


def mean(vals):
    return sum(vals) / len(vals) if vals else None


def build_series(snaps: list[dict], t0: int, t1: int) -> dict:
    """Downsample to <= MAX_BUCKETS, keeping the step a round number of minutes."""
    span = max(t1 - t0, 60_000)
    raw_step = span / MAX_BUCKETS
    # round up to the next whole minute so bucket edges stay legible
    step = max(60_000, int((raw_step + 59_999) // 60_000) * 60_000)
    n = int(span // step) + 1

    buckets: list[list[dict]] = [[] for _ in range(n)]
    for r in snaps:
        i = int((r["exportedAt"] - t0) // step)
        if 0 <= i < n:
            buckets[i].append(r)

    def series(fn):
        out = []
        for b in buckets:
            vals = [v for v in (fn(r) for r in b) if v is not None]
            out.append(mean(vals) if vals else None)
        return out

    def rnd(vals, digits):
        return [None if v is None else round(v, digits) for v in vals]

    anon = rnd(series(lambda r: (r.get("memory", {}).get("darkfid", {}).get("anon") or 0) / MIB), 0)
    cache = rnd(series(lambda r: (r.get("memory", {}).get("darkfid", {}).get("cache") or 0) / MIB), 0)
    peers = rnd(series(lambda r: r.get("peers")), 1)
    lag = rnd(
        series(
            lambda r: (r["tip"] - r["height"])
            if r.get("tip") is not None and r.get("height") is not None
            else None
        ),
        1,
    )
    load = rnd(series(lambda r: (r.get("resources", {}).get("load_average") or [None])[0]), 2)
    temp = rnd(series(lambda r: r.get("tempC")), 1)

    return {
        "step": step,
        "t0": t0,
        "anon": anon,
        "cache": cache,
        "peers": peers,
        "lag": lag,
        "load": load,
        "tempC": temp,
    }


def find_episodes(snaps: list[dict]) -> list[dict]:
    eps: list[dict] = []

    # --- gaps in the recording -------------------------------------------
    # These come first because everything below has to be able to ask "was the
    # instrument even running?". A blind exporter looks exactly like a frozen
    # chain, and publishing the one as the other would be a lie about the node.
    gap_spans: list[tuple[int, int]] = []
    for a, b in zip(snaps, snaps[1:]):
        d = b["exportedAt"] - a["exportedAt"]
        if d > GAP_MS:
            gap_spans.append((a["exportedAt"], b["exportedAt"]))
            eps.append(
                {
                    "kind": "gap",
                    "from": a["exportedAt"],
                    "to": b["exportedAt"],
                    "note": "no snapshots recorded",
                }
            )

    def spans_a_gap(t_from: int, t_to: int) -> bool:
        return any(g0 < t_to and g1 > t_from for g0, g1 in gap_spans)

    # --- memory-ceiling excursions ---------------------------------------
    cur = None
    for r in snaps:
        c = (r.get("memory", {}).get("darkfid", {}).get("current") or 0) / MIB
        t = r["exportedAt"]
        if c > CEILING_MIB:
            if cur is None:
                cur = {"kind": "memory_ceiling", "from": t, "to": t, "peakMiB": c}
            else:
                cur["to"] = t
                cur["peakMiB"] = max(cur["peakMiB"], c)
        elif cur is not None:
            cur["peakMiB"] = round(cur["peakMiB"])
            eps.append(cur)
            cur = None
    if cur is not None:
        cur["peakMiB"] = round(cur["peakMiB"])
        eps.append(cur)

    # --- restarts (anon collapse) + how fast the chain caught up ----------
    prev_anon = None
    for i, r in enumerate(snaps):
        a = r.get("memory", {}).get("darkfid", {}).get("anon")
        if a is None:
            continue
        a_mib = a / MIB
        if prev_anon is not None and prev_anon - a_mib > RESTART_DROP_MIB:
            t = r["exportedAt"]
            h_now = r.get("height")
            recovered = None
            # Only meaningful if the recording was continuous across the catch-up.
            # After a blind stretch the node is draining a backlog it accumulated
            # unobserved, which is not the same claim as "a restart recovered N
            # blocks in minutes" — so look back well past the gap edge.
            if not spans_a_gap(t - 30 * 60_000, t + 12 * 60_000):
                for later in snaps[i : i + 12]:  # next ~12 minutes
                    if later.get("height") is not None and h_now is not None:
                        recovered = max(recovered or 0, later["height"] - h_now)
            eps.append(
                {
                    "kind": "restart",
                    "from": t,
                    "to": t,
                    **({"blocksRecovered": int(recovered)} if recovered else {}),
                    "note": "before the cgroup wall",
                }
            )
        prev_anon = a_mib

    # --- stalls: height frozen while peers are up ------------------------
    h_rows = [r for r in snaps if r.get("height") is not None]
    if h_rows:
        start = h_rows[0]["exportedAt"]
        last_h = h_rows[0]["height"]
        had_peers = False
        for r in h_rows[1:]:
            if r["height"] != last_h:
                dur = r["exportedAt"] - start
                if dur > STALL_MS and had_peers and not spans_a_gap(start, r["exportedAt"]):
                    eps.append(
                        {
                            "kind": "stall",
                            "from": start,
                            "to": r["exportedAt"],
                            "stuckAt": last_h,
                            "note": "height frozen with peers connected",
                        }
                    )
                start, last_h, had_peers = r["exportedAt"], r["height"], False
            if (r.get("peers") or 0) > 0:
                had_peers = True

    eps.sort(key=lambda e: e["from"])
    return eps[:500]


def overlay_aggregates(store: str) -> dict | None:
    """dnet → statistics only. Addresses are used as grouping keys and dropped."""
    ev = load_jsonl(store, "dnet")
    ev = [e for e in ev if isinstance(e, dict) and "rx" in e]
    if len(ev) < 100:
        return None
    ev.sort(key=lambda e: e["rx"])

    t0, t1 = ev[0]["rx"], ev[-1]["rx"]

    # Recorded hours are the hours actually covered, NOT the span from first to
    # last event. The recorder was dead for twelve days in July, and dividing by
    # the span turned 57 disconnects/h into 7 — a rate diluted by time when
    # nothing could have been observed. Sum only the intervals where the stream
    # was demonstrably alive; anything longer than the stall threshold is a hole.
    covered_ms = sum(
        b["rx"] - a["rx"]
        for a, b in zip(ev, ev[1:])
        if 0 <= b["rx"] - a["rx"] <= DNET_COVERAGE_GAP_MS
    )
    hours = max(covered_ms / 3_600_000, 0.01)

    addrs = set()
    cmds: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    pending: dict[int, int] = {}
    rtts: list[float] = []

    # --- per-peer, pseudonymised -------------------------------------------
    # Peers are published as P1..Pn ordered by FIRST APPEARANCE, never as a hash
    # of the address. A truncated hash would be reversible here: the candidate
    # address space of a DarkFi node's peers is small enough to enumerate, so
    # anyone holding a list of suspected addresses could confirm membership by
    # hashing it. A first-seen index leaks nothing beyond "there were n of them",
    # and is stable across runs because the digest recomputes over the whole
    # retained history each time.
    first_seen: dict[str, int] = {}
    p_msgs: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # [send, recv]
    p_rtts: dict[str, list[float]] = defaultdict(list)
    p_sessions: dict[str, list[float]] = defaultdict(list)
    dialed: set[str] = set()
    cid_addr: dict[object, str] = {}

    for e in ev:
        info = e.get("info") or {}
        chan = info.get("chan") or {}
        addr = chan.get("addr")
        kind = e.get("event")
        # the outbound slot events carry the address directly, not under chan
        if kind in ("outbound_slot_connecting", "outbound_slot_connected"):
            slot_addr = info.get("addr")
            if slot_addr:
                dialed.add(slot_addr)
                addrs.add(slot_addr)
                first_seen.setdefault(slot_addr, e["rx"])
        if addr:
            addrs.add(addr)
            first_seen.setdefault(addr, e["rx"])
            if kind in ("send", "recv"):
                p_msgs[addr][0 if kind == "send" else 1] += 1
        cmd = info.get("cmd")
        if cmd:
            cmds[cmd][0 if kind == "send" else 1] += 1
        cid, tns = chan.get("id"), info.get("time")
        if cid is None or tns is None:
            continue
        try:
            tns = int(tns)
        except (TypeError, ValueError):
            continue
        if kind == "send" and cmd == "ping":
            pending[cid] = tns
            if addr:
                cid_addr[cid] = addr
        elif kind == "recv" and cmd == "pong" and cid in pending:
            dt = (tns - pending.pop(cid)) / 1e6
            owner = cid_addr.pop(cid, addr)
            # a pong matched across a reconnect is not a round trip
            if 0 <= dt < 30_000:
                rtts.append(dt)
                if owner:
                    p_rtts[owner].append(dt)

    # Holes in the stream, so nothing below gets measured across one.
    holes = [
        (a["rx"], b["rx"])
        for a, b in zip(ev, ev[1:])
        if b["rx"] - a["rx"] > DNET_COVERAGE_GAP_MS
    ]

    def spans_hole(t_from: int, t_to: int) -> bool:
        return any(h0 < t_to and h1 > t_from for h0, h1 in holes)

    # outbound slot lifecycle → completed session durations
    opened: dict[object, int] = {}
    sessions: list[float] = []
    for e in ev:
        kind = e.get("event")
        if kind == "recorder_start":
            # A fresh subscription knows nothing about what was open before it.
            opened.clear()
            continue
        if kind not in ("outbound_slot_connected", "outbound_slot_disconnected"):
            continue
        info = e.get("info") or {}
        slot = info.get("slot", "?")
        if kind == "outbound_slot_connected":
            # keep the address with the slot: the matching disconnect event
            # carries only the slot, so this is the sole point where a session
            # can be attributed to a peer.
            opened[slot] = (e["rx"], info.get("addr"))
        elif slot in opened:
            start, s_addr = opened.pop(slot)
            # A session that appears to straddle a hole is an artefact of the
            # recorder restarting, not a peer that stayed up for twelve days.
            if not spans_hole(start, e["rx"]):
                dur = (e["rx"] - start) / 1000
                sessions.append(dur)
                if s_addr:
                    # (start_ms, duration_s) — the churn charts need when, not
                    # just how long.
                    p_sessions[s_addr].append((start, dur))

    rtts.sort()
    sessions.sort()
    return {
        "from": t0,
        "to": t1,
        "events": len(ev),
        "hours": round(hours, 2),
        "distinctPeers": len(addrs),
        "rtt": {
            "medianMs": round(pctl(rtts, 0.5), 1),
            "p90Ms": round(pctl(rtts, 0.9), 1),
            "p99Ms": round(pctl(rtts, 0.99), 1),
            "samples": len(rtts),
        },
        "sessions": {
            "completed": len(sessions),
            "medianS": round(pctl(sessions, 0.5), 1),
            "p90S": round(pctl(sessions, 0.9), 1),
            "churnPerHour": round(len(sessions) / hours, 2),
            "longestS": round(sessions[-1], 1) if sessions else 0,
        },
        "wireMix": [
            {"cmd": c, "send": v[0], "recv": v[1]}
            for c, v in sorted(cmds.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:24]
        ],
        "peers": _per_peer(addrs, first_seen, p_msgs, p_rtts, p_sessions, dialed),
        **_churn(t0, t1, p_sessions, first_seen, holes),
    }


CHURN_BUCKETS = 480          # ~= the series budget; step falls out of the window
RECENT_H = 6                 # raw-session detail window
RECENT_CAP = 1500            # hard cap on emitted intervals


def _churn(t0, t1, p_starts, first_seen, holes):
    """Session-opening density per peer, plus a recent raw-interval window.

    Two scales, because one is dishonest: over a 490 h window a median 33 s
    session is 0.02 px wide, so the full span can only be drawn as density.
    The detail window carries real intervals and is deliberately the MOST
    RECENT six hours rather than the busiest — this is a live panel, and if the
    node had no sessions last night that is the news, not something to hide by
    panning to a prettier window.
    """
    order = {a: n for n, a in enumerate(sorted(first_seen, key=lambda a: first_seen[a]), 1)}
    span = max(t1 - t0, 1)
    step = max(int(span / CHURN_BUCKETS), 60_000)
    nb = int(span / step) + 1

    lanes = []
    for addr, ivals in p_starts.items():
        if not ivals:
            continue
        counts = [0] * nb
        for s, _ in ivals:
            counts[min(nb - 1, max(0, int((s - t0) / step)))] += 1
        lanes.append({"n": order.get(addr, 0), "counts": counts})
    lanes.sort(key=lambda l: l["n"])

    r_from = t1 - RECENT_H * 3_600_000
    recent = []
    for addr, ivals in p_starts.items():
        for s, d in ivals:
            if s + d * 1000 >= r_from:
                recent.append([order.get(addr, 0), s - r_from, round(d, 1)])
    recent.sort(key=lambda r: r[1])
    truncated = len(recent) > RECENT_CAP

    return {
        "churn": {"t0": t0, "step": step, "buckets": nb, "lanes": lanes},
        "recent": {
            "from": r_from,
            "to": t1,
            "truncated": truncated,
            "sessions": recent[:RECENT_CAP],
        },
        "holes": [[a, b] for a, b in holes][-40:],
    }


def _per_peer(addrs, first_seen, p_msgs, p_rtts, p_sessions, dialed):
    """Per-peer overlay stats under a first-seen pseudonym. Addresses drop here.

    The split this exposes is the point of it: peers the node DIALED have a
    session lifecycle (connect -> disconnect on an outbound slot), and peers
    that dialed US have none at all, because darkfid emits no inbound slot
    events. Every session and churn number in this digest is therefore
    outbound-only, and on the recorded window the two never-dialed peers carry
    about a third of all wire traffic. Aggregates alone hide that entirely.
    """
    total = sum(sum(v) for v in p_msgs.values()) or 1
    out = []
    for n, addr in enumerate(sorted(addrs, key=lambda a: first_seen.get(a, 0)), 1):
        send, recv = p_msgs.get(addr, [0, 0])
        ss = sorted(d for _, d in p_sessions.get(addr, []))
        rt = sorted(p_rtts.get(addr, []))
        out.append(
            {
                "n": n,
                "dialed": addr in dialed,
                "firstSeen": first_seen.get(addr, 0),
                "send": send,
                "recv": recv,
                "sharePct": round(100 * (send + recv) / total, 2),
                "sessions": len(ss),
                "upS": round(sum(ss), 1),
                "medianS": round(pctl(ss, 0.5), 1) if ss else 0,
                "longestS": round(ss[-1], 1) if ss else 0,
                "rttMedianMs": round(pctl(rt, 0.5), 1) if rt else None,
                "rttSamples": len(rt),
            }
        )
    return out


def build_digest(store: str) -> dict:
    snaps = [r for r in load_jsonl(store, "snapshots") if isinstance(r, dict) and "exportedAt" in r]
    if not snaps:
        raise SystemExit("[digest] no snapshots found — nothing to publish")
    snaps.sort(key=lambda r: r["exportedAt"])

    t0, t1 = snaps[0]["exportedAt"], snaps[-1]["exportedAt"]
    span = max(t1 - t0, 60_000)
    expected = int(span / 60_000)
    gaps = sum(1 for a, b in zip(snaps, snaps[1:]) if b["exportedAt"] - a["exportedAt"] > GAP_MS)

    dark0 = next((r["memory"]["darkfid"] for r in snaps if r.get("memory", {}).get("darkfid")), {})
    limits = {
        "high": round((dark0.get("high") or 0) / MIB),
        "max": round((dark0.get("max") or 0) / MIB),
    }

    heights = [r["height"] for r in snaps if r.get("height") is not None]
    lags = sorted(
        r["tip"] - r["height"]
        for r in snaps
        if r.get("tip") is not None and r.get("height") is not None
    )
    hours = span / 3_600_000

    peer_vals = [r["peers"] for r in snaps if r.get("peers") is not None]
    histogram: dict[str, int] = defaultdict(int)
    for p in peer_vals:
        histogram[str(int(p))] += 1
    zero_eps, inzero = 0, False
    for p in peer_vals:
        if p == 0 and not inzero:
            zero_eps += 1
            inzero = True
        elif p != 0:
            inzero = False

    temps = sorted(r["tempC"] for r in snaps if r.get("tempC") is not None)
    thr = [r.get("throttled") for r in snaps if r.get("throttled") is not None]
    clean = sum(1 for v in thr if v in ("0x0", "0"))
    hrs = sorted(
        r["hashrate"]["total"][0]
        for r in snaps
        if (r.get("hashrate", {}).get("total") or [None])[0]
    )

    return {
        "v": SCHEMA_VERSION,
        "generatedAt": int(__import__("time").time() * 1000),
        "window": {"from": t0, "to": t1},
        "coverage": {
            "snapshots": len(snaps),
            "expected": expected,
            "pct": round(100 * len(snaps) / max(expected, 1), 2),
            "days": round(span / 86_400_000, 2),
            "gaps": gaps,
        },
        "limits": limits,
        "series": build_series(snaps, t0, t1),
        "episodes": find_episodes(snaps),
        "chain": {
            "firstHeight": heights[0] if heights else 0,
            "lastHeight": heights[-1] if heights else 0,
            "blocksPerHour": round((heights[-1] - heights[0]) / hours, 2) if len(heights) > 1 else 0,
            "medianLag": round(pctl(lags, 0.5), 1),
            "p95Lag": round(pctl(lags, 0.95), 1),
        },
        "peers": {
            "min": int(min(peer_vals)) if peer_vals else 0,
            "max": int(max(peer_vals)) if peer_vals else 0,
            "histogram": dict(histogram),
            "zeroEpisodes": zero_eps,
        },
        "host": {
            "tempMinC": round(temps[0], 1) if temps else 0,
            "tempMedianC": round(statistics.median(temps), 1) if temps else 0,
            "tempMaxC": round(temps[-1], 1) if temps else 0,
            "throttleCleanPct": round(100 * clean / len(thr), 2) if thr else 100.0,
            "hashrateMedian": round(statistics.median(hrs), 1) if hrs else 0,
        },
        "overlay": overlay_aggregates(store),
    }


def post(digest: dict, url: str, token: str) -> None:
    body = json.dumps(digest, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[digest] {resp.status} {resp.read().decode()[:200]}")
    except urllib.error.HTTPError as exc:
        print(f"[digest] HTTP {exc.code}: {exc.read().decode()[:300]}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"[digest] POST failed: {exc}", file=sys.stderr)
        raise SystemExit(1)


def default_url() -> str:
    """The digest endpoint is the sibling of the snapshot one.

    The Pi is already configured with INGEST_URL and NODE_INGEST_TOKEN for the
    live panel, and the digest goes to the same host with the same token — so
    derive it rather than making the operator maintain a second copy of the
    same URL. HISTORY_INGEST_URL still overrides, for the odd case where the
    two really do differ.
    """
    override = os.environ.get("HISTORY_INGEST_URL", "").strip()
    if override:
        return override
    base = os.environ.get("INGEST_URL", "").strip()
    if not base:
        return ""
    return base.rsplit("/", 1)[0] + "/node-history" if "/" in base else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=os.environ.get("HISTORY_DIR", "/var/log/darknode"))
    ap.add_argument("--url", default=default_url())
    ap.add_argument("--token", default=os.environ.get("NODE_INGEST_TOKEN", ""))
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    digest = build_digest(args.store)
    blob = json.dumps(digest, separators=(",", ":"))

    s = digest["series"]
    print(
        f"[digest] {digest['coverage']['snapshots']} snapshots · "
        f"{digest['coverage']['days']}d · coverage {digest['coverage']['pct']}% · "
        f"{len(s['anon'])} buckets @ {s['step']//60000}min · "
        f"{len(digest['episodes'])} episodes · "
        f"overlay {'yes' if digest['overlay'] else 'none'} · "
        f"{len(blob)/1024:.1f} KB",
        file=sys.stderr,
    )

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(blob)
        print(f"[digest] wrote {args.out}", file=sys.stderr)
    if args.dry_run:
        return
    if not args.url or not args.token:
        # Not an error: without INGEST_URL/NODE_INGEST_TOKEN there is nowhere to
        # publish, and a red journal would suggest a fault where there is none.
        print(
            "[digest] no INGEST_URL / NODE_INGEST_TOKEN — computed but not published",
            file=sys.stderr,
        )
        return
    post(digest, args.url, args.token)


if __name__ == "__main__":
    main()
