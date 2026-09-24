#!/usr/bin/env python3
"""darknode-digest.py — turn the local JSONL history into one publishable digest.

The Pi keeps everything: a snapshot a minute plus every dnet P2P event. Neither
is publishable as-is — the snapshot series is far too big for a page and the
dnet events carry peer addresses, which by standing rule never leave this
machine. So we aggregate here and POST only the result.

Contract: portfolio-site `src/lib/node-history.ts` (schema v1). Keep the two in
step; the ingest route validates strictly and will 400 on drift. Fields are
added, never renamed or removed, so `v` only moves when something already
published changes shape. Added 2026-09-24, and the site does not read them yet:
`series.hashrate`, `series.blocksPerHour`, `retention`, `chainActivity`,
`overlay.sessions.histogram` and `overlay.rtt.byCeiling`.

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
import resource
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
# anon within this fraction of the limit in force = a memory-ceiling episode.
CEILING_FRAC = 0.95
# a zero-peer stretch shorter than this is a restart finding its peers again,
# not the network being lost.
ZERO_PEER_MIN_MS = 5 * 60_000
# anon falling by more than this in one step = the process restarted.
RESTART_DROP_MIB = 2000
# height frozen at least this long, with peers, = a stall.
STALL_MS = 30 * 60_000
# Silence longer than this in the dnet stream = the recorder was down, not the
# overlay being quiet. Matches DNET_STALL_S in dnet-record.sh.
DNET_COVERAGE_GAP_MS = 300_000

# The zoomed series: the most recent stretch at a resolution where a sync or a
# restart is visible, which the whole-window series averages away.
ZOOM_MS = 48 * 3_600_000

# Lag, in blocks, within which the node counts as "at the tip" — so the block
# rate it reports is the network's production and not its own catching up.
AT_TIP = 5
# A bucket needs this many hours of usable samples before a rate is emitted.
MIN_RATE_H = 0.05
# How long after a restart to look for the node's floor, and how quiet the
# window has to be for that floor to mean anything.
RETENTION_WINDOW_MS = 30 * 60_000
# Session lengths, in seconds, as a log-ish histogram.
SESSION_EDGES = [0, 1, 3, 10, 30, 100, 300, 1000, 3600, 10800]

GIB = 1024 * MIB
# Windows where the recorded limits were wrong. Until 22-S the exporter read
# them from `systemctl show`, which reports the configured value, and on the
# night of 21-S they were raised live in the cgroup to profile the fjall sync.
# Times are from the session log, good to a few minutes: (from, to, high, max)
# in bytes, 0 = unlimited, same convention as the exporter.
LIMIT_OVERRIDES = [
    (1790029620_000, 1790030700_000, 6 * GIB, 6656 * MIB),  # 21-S 22:27Z  6 G / 6.5 G
    (1790030700_000, 1790054840_000, 0, 7 * GIB),           # 22:45Z → reboot 05:27Z, no soft limit
]

# Things that changed the node, drawn on the memory chart.
MARKERS = [
    {"at": 1790005834_000, "label": "sled → fjall"},  # fjall darkfid up, 21-S 15:50:34Z
]


def store_files(store: str, prefix: str) -> list[str]:
    return sorted(glob.glob(os.path.join(store, f"{prefix}-*.jsonl*")))


def iter_jsonl(path: str):
    """One parsed row at a time. Never hold a whole file of dicts.

    The first version loaded the entire store into one list. With 55 days of
    dnet events (~200k a day) that no longer fits in 8 GB: from mid-August the
    daily run filled RAM and swap, the Pi stopped petting its hardware watchdog
    and rebooted — nearly every morning, for five weeks (knowledge-os §61).
    """
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line during rotation is not fatal
    except (OSError, EOFError) as exc:
        print(f"[digest] skipping {path}: {exc}", file=sys.stderr)


class Snap:
    """The dozen fields the digest reads from a snapshot, and nothing else."""

    __slots__ = ("t", "dark", "anon", "cache", "current", "high", "max",
                 "peers", "tip", "height", "load", "temp", "thr", "hr",
                 "diff", "started")

    def __init__(self, r: dict):
        m = (r.get("memory") or {}).get("darkfid") or {}
        self.t = r["exportedAt"]
        self.dark = bool(m)
        self.anon = m.get("anon")
        self.cache = m.get("cache")
        self.current = m.get("current")
        self.high = m.get("high")
        self.max = m.get("max")
        self.peers = r.get("peers")
        self.tip = r.get("tip")
        self.height = r.get("height")
        self.load = ((r.get("resources") or {}).get("load_average") or [None])[0]
        self.temp = r.get("tempC")
        self.thr = r.get("throttled")
        self.hr = ((r.get("hashrate") or {}).get("total") or [None])[0]
        self.diff = r.get("difficulty")
        self.started = r.get("darkfidStartedAt")
        for t_from, t_to, high, mx in LIMIT_OVERRIDES:
            if t_from <= self.t < t_to:
                self.high, self.max = high, mx


def load_snaps(store: str) -> list[Snap]:
    return [
        Snap(r)
        for path in store_files(store, "snapshots")
        for r in iter_jsonl(path)
        if isinstance(r, dict) and "exportedAt" in r
    ]


def pctl(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[i]


def mean(vals):
    return sum(vals) / len(vals) if vals else None


def build_series(snaps: list[Snap], t0: int, t1: int) -> dict:
    """Downsample to <= MAX_BUCKETS, keeping the step a round number of minutes."""
    span = max(t1 - t0, 60_000)
    raw_step = span / MAX_BUCKETS
    # round up to the next whole minute so bucket edges stay legible
    step = max(60_000, int((raw_step + 59_999) // 60_000) * 60_000)
    n = int(span // step) + 1
    # a span that divides exactly gives MAX_BUCKETS + 1, which the site rejects
    if n > MAX_BUCKETS:
        step += 60_000
        n = int(span // step) + 1

    buckets: list[list[Snap]] = [[] for _ in range(n)]
    for r in snaps:
        i = int((r.t - t0) // step)
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

    anon = rnd(series(lambda r: (r.anon or 0) / MIB), 0)
    cache = rnd(series(lambda r: (r.cache or 0) / MIB), 0)
    peers = rnd(series(lambda r: r.peers), 1)
    lag = rnd(
        series(
            lambda r: (r.tip - r.height)
            if r.tip is not None and r.height is not None
            else None
        ),
        1,
    )
    load = rnd(series(lambda r: r.load), 2)
    temp = rnd(series(lambda r: r.temp), 1)

    # Limits are a step function, so a bucket takes its highest value rather
    # than an average no limit ever had. Unlimited (0) breaks the line.
    def limit(fn):
        out = []
        for b in buckets:
            vals = [v for v in (fn(r) for r in b) if v]
            out.append(round(max(vals) / MIB) if vals else None)
        return out

    # Blocks per hour, counted only across pairs of samples where the node was
    # at the tip. A node catching up moves through blocks far faster than the
    # network makes them, and that is its own speed, not the network's.
    produced = [0.0] * n
    covered_h = [0.0] * n
    for a, b in zip(snaps, snaps[1:]):
        dt = b.t - a.t
        if dt <= 0 or dt > GAP_MS:
            continue
        if None in (a.height, b.height, a.tip, b.tip):
            continue
        if (a.tip - a.height) > AT_TIP or (b.tip - b.height) > AT_TIP:
            continue
        delta = b.height - a.height
        if delta < 0:            # a resync replaying the chain
            continue
        i = int((b.t - t0) // step)
        if 0 <= i < n:
            produced[i] += delta
            covered_h[i] += dt / 3_600_000
    blocks_ph = [
        round(produced[i] / covered_h[i], 1) if covered_h[i] >= MIN_RATE_H else None
        for i in range(n)
    ]

    return {
        "step": step,
        "t0": t0,
        "anon": anon,
        "cache": cache,
        "peers": peers,
        "lag": lag,
        "load": load,
        "tempC": temp,
        "high": limit(lambda r: r.high),
        "max": limit(lambda r: r.max),
        "hashrate": rnd(series(lambda r: r.hr), 1),
        "blocksPerHour": blocks_ph,
        "difficulty": rnd(series(lambda r: r.diff), 0),
    }


def find_episodes(snaps: list[Snap]) -> list[dict]:
    eps: list[dict] = []

    # --- gaps in the recording -------------------------------------------
    # These come first because everything below has to be able to ask "was the
    # instrument even running?". A blind exporter looks exactly like a frozen
    # chain, and publishing the one as the other would be a lie about the node.
    gap_spans: list[tuple[int, int]] = []
    for a, b in zip(snaps, snaps[1:]):
        d = b.t - a.t
        if d > GAP_MS:
            gap_spans.append((a.t, b.t))
            eps.append(
                {
                    "kind": "gap",
                    "from": a.t,
                    "to": b.t,
                    "note": "no snapshots recorded",
                }
            )

    def spans_a_gap(t_from: int, t_to: int) -> bool:
        return any(g0 < t_to and g1 > t_from for g0, g1 in gap_spans)

    # --- memory-ceiling excursions ---------------------------------------
    # The working set pressing the limit in force at the time: MemoryHigh, or
    # MemoryMax when there is no soft limit. This used to be "cgroup total above
    # 4000 MiB", which counts reclaimable cache and called a whole night with no
    # soft limit at all a 12-hour ceiling.
    cur = None
    for r in snaps:
        c = (r.anon or 0) / MIB
        t = r.t
        lim = (r.high or r.max or 0) / MIB
        if lim and c >= CEILING_FRAC * lim:
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
    # The note says only what was measured. It used to read "before the cgroup
    # wall", on the belief that every restart was memguard's. Thirty of them were
    # the Pi rebooting under the old digest, and some were sled panicking. A
    # collapse seen across a recording gap cannot be attributed at all.
    prev_anon = prev_t = None
    for i, r in enumerate(snaps):
        a = r.anon
        if a is None:
            continue
        a_mib = a / MIB
        if prev_anon is not None and prev_anon - a_mib > RESTART_DROP_MIB:
            t = r.t
            h_now = r.height
            recovered = None
            # Only meaningful if the recording was continuous across the catch-up.
            # After a blind stretch the node is draining a backlog it accumulated
            # unobserved, which is not the same claim as "a restart recovered N
            # blocks in minutes" — so look back well past the gap edge.
            if not spans_a_gap(t - 30 * 60_000, t + 12 * 60_000):
                for later in snaps[i : i + 12]:  # next ~12 minutes
                    if later.height is not None and h_now is not None:
                        recovered = max(recovered or 0, later.height - h_now)
            eps.append(
                {
                    "kind": "restart",
                    "from": t,
                    "to": t,
                    **({"blocksRecovered": int(recovered)} if recovered else {}),
                    "note": (
                        f"across a {round((t - prev_t) / 60_000)}-min recording gap"
                        if t - prev_t > GAP_MS
                        else f"anon {round(prev_anon)} → {round(a_mib)} MiB"
                    ),
                }
            )
        prev_anon, prev_t = a_mib, r.t

    # --- stalls: height frozen while peers are up ------------------------
    h_rows = [r for r in snaps if r.height is not None]
    if h_rows:
        start = h_rows[0].t
        last_h = h_rows[0].height
        had_peers = False
        for r in h_rows[1:]:
            if r.height != last_h:
                dur = r.t - start
                if dur > STALL_MS and had_peers and not spans_a_gap(start, r.t):
                    eps.append(
                        {
                            "kind": "stall",
                            "from": start,
                            "to": r.t,
                            "stuckAt": last_h,
                            "note": "height frozen with peers connected",
                        }
                    )
                start, last_h, had_peers = r.t, r.height, False
            if (r.peers or 0) > 0:
                had_peers = True

    eps.sort(key=lambda e: e["from"])
    return eps[:500]


def _dnet_day(path: str) -> list[tuple]:
    """One day of dnet events, cut down to the fields read below and in rx order.

    A day is ~200k events: as dicts that is hundreds of MB, as these tuples tens.
    Files are named by day, so sorting inside each one and walking the files in
    name order gives the same global order the old whole-store sort did.
    """
    out = []
    intern = sys.intern
    for e in iter_jsonl(path):
        if not isinstance(e, dict) or "rx" not in e:
            continue
        info = e.get("info") or {}
        chan = info.get("chan") or {}
        kind, addr, cmd = e.get("event"), chan.get("addr"), info.get("cmd")
        slot_addr = info.get("addr")
        out.append((
            e["rx"],
            intern(kind) if isinstance(kind, str) else kind,
            intern(addr) if isinstance(addr, str) else addr,
            intern(cmd) if isinstance(cmd, str) else cmd,
            chan.get("id"),
            info.get("time"),
            info.get("slot", "?"),
            intern(slot_addr) if isinstance(slot_addr, str) else slot_addr,
        ))
    out.sort(key=lambda r: r[0])
    return out


def _block_rows(store: str) -> list[tuple[int, int, int, int]]:
    """(applied_at, height, calls, gas) from the block logger, deduplicated.

    The logger appends one line per block darkfid applies. A resync replays the
    whole chain, so the same height appears twice with different timestamps;
    the height is the identity, not the line.
    """
    paths = [os.path.join(store, "blocks.tsv")]
    legacy = os.environ.get("DARKNODE_BLOCKS_LEGACY", "").strip()
    if legacy:
        paths.append(legacy)
    rows: list[tuple[int, int, int, int]] = []
    for path in paths:
        try:
            fh = open(path, "rt")
        except OSError:
            continue
        with fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) < 4 or not f[0].isdigit():
                    continue
                try:
                    rows.append((int(f[0]), int(f[1]), int(f[2]), int(f[3])))
                except ValueError:
                    continue
    rows.sort()
    return rows


def chain_activity(store: str) -> dict | None:
    """What the chain carried, per height: calls, gas, and where the traffic is.

    This is the one part of the digest that grows with the chain rather than
    with the window, so it is bucketed by height, not by time.
    """
    rows = _block_rows(store)
    if len(rows) < 100:
        return None

    by_height: dict[int, tuple[int, int]] = {}
    for _t, h, c, g in rows:
        by_height[h] = (c, g)      # the last application of a height wins

    heights = sorted(by_height)
    h0, h1 = heights[0], heights[-1]
    calls = sum(c for c, _ in by_height.values())
    gas = sum(g for _, g in by_height.values())
    # One call per block is the miner claiming its own reward. More than one is
    # somebody else using the chain.
    with_tx = sum(1 for c, _ in by_height.values() if c > 1)
    max_calls = max(c for c, _ in by_height.values())

    span = max(h1 - h0, 1)
    step = max(1, -(-span // MAX_BUCKETS))          # ceil, so n <= MAX_BUCKETS
    step = max(step, 50) if span > 50 * MAX_BUCKETS else step
    n = span // step + 1
    by_bucket = [0] * n
    for h, (c, _g) in by_height.items():
        i = (h - h0) // step
        if 0 <= i < n:
            by_bucket[i] += c

    return {
        "fromHeight": h0,
        "toHeight": h1,
        "blocksSeen": len(by_height),
        "blocksWithTx": with_tx,
        "calls": calls,
        "gas": gas,
        "maxCallsInBlock": max_calls,
        "step": step,
        "callsByHeight": by_bucket,
    }


def overlay_aggregates(store: str, ceilings: list[tuple[int, int]] | None = None) -> dict | None:
    """dnet → statistics only. Addresses are used as grouping keys and dropped.

    One pass, one day in memory at a time. Everything that used to need the
    whole sorted list (coverage, holes, "does this session straddle a hole")
    only ever looks backwards, so it can be carried along as running state.
    """
    t0 = t1 = prev_rx = None
    n_events = 0

    # Recorded hours are the hours actually covered, NOT the span from first to
    # last event. The recorder was dead for twelve days in July, and dividing by
    # the span turned 57 disconnects/h into 7 — a rate diluted by time when
    # nothing could have been observed. Sum only the intervals where the stream
    # was demonstrably alive; anything longer than the stall threshold is a hole.
    covered_ms = 0
    # Holes in the stream, so nothing below gets measured across one.
    holes: list[tuple[int, int]] = []
    last_hole_end = None

    addrs = set()
    cmds: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    pending: dict[int, int] = {}
    rtts: list[float] = []
    rtt_at: list[tuple[int, float]] = []   # (rx of the pong, round trip in ms)

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

    # outbound slot lifecycle → completed session durations
    opened: dict[object, int] = {}
    sessions: list[float] = []

    for path in store_files(store, "dnet"):
        for rx, kind, addr, cmd, cid, tns, slot, slot_addr in _dnet_day(path):
            n_events += 1
            if t0 is None:
                t0 = rx
            t1 = rx
            if prev_rx is not None:
                d = rx - prev_rx
                if 0 <= d <= DNET_COVERAGE_GAP_MS:
                    covered_ms += d
                elif d > DNET_COVERAGE_GAP_MS:
                    holes.append((prev_rx, rx))
                    last_hole_end = rx
            prev_rx = rx

            # the outbound slot events carry the address directly, not under chan
            if kind in ("outbound_slot_connecting", "outbound_slot_connected"):
                if slot_addr:
                    dialed.add(slot_addr)
                    addrs.add(slot_addr)
                    first_seen.setdefault(slot_addr, rx)
            if addr:
                addrs.add(addr)
                first_seen.setdefault(addr, rx)
                if kind in ("send", "recv"):
                    p_msgs[addr][0 if kind == "send" else 1] += 1
            if cmd:
                cmds[cmd][0 if kind == "send" else 1] += 1

            if kind == "recorder_start":
                # A fresh subscription knows nothing about what was open before it.
                opened.clear()
            elif kind == "outbound_slot_connected":
                # keep the address with the slot: the matching disconnect event
                # carries only the slot, so this is the sole point where a session
                # can be attributed to a peer.
                opened[slot] = (rx, slot_addr)
            elif kind == "outbound_slot_disconnected" and slot in opened:
                start, s_addr = opened.pop(slot)
                # A session that appears to straddle a hole is an artefact of the
                # recorder restarting, not a peer that stayed up for twelve days.
                # Every hole seen so far ends at or before now, so "a hole inside
                # [start, now]" is just "the last hole ended after start".
                if last_hole_end is None or last_hole_end <= start:
                    dur = (rx - start) / 1000
                    sessions.append(dur)
                    if s_addr:
                        # (start_ms, duration_s) — the churn charts need when, not
                        # just how long.
                        p_sessions[s_addr].append((start, dur))

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
                    rtt_at.append((rx, dt))
                    if owner:
                        p_rtts[owner].append(dt)

    if n_events < 100:
        return None
    hours = max(covered_ms / 3_600_000, 0.01)

    rtts.sort()
    sessions.sort()
    return {
        "from": t0,
        "to": t1,
        "events": n_events,
        "hours": round(hours, 2),
        "distinctPeers": len(addrs),
        "rtt": {
            "medianMs": round(pctl(rtts, 0.5), 1),
            "p90Ms": round(pctl(rtts, 0.9), 1),
            "p99Ms": round(pctl(rtts, 0.99), 1),
            "samples": len(rtts),
            "byCeiling": _rtt_by_ceiling(rtt_at, ceilings),
        },
        "sessions": {
            "completed": len(sessions),
            "medianS": round(pctl(sessions, 0.5), 1),
            "p90S": round(pctl(sessions, 0.9), 1),
            "churnPerHour": round(len(sessions) / hours, 2),
            "longestS": round(sessions[-1], 1) if sessions else 0,
            "histogram": _session_histogram(sessions),
        },
        "wireMix": [
            {"cmd": c, "send": v[0], "recv": v[1]}
            for c, v in sorted(cmds.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:24]
        ],
        "peers": _per_peer(addrs, first_seen, p_msgs, p_rtts, p_sessions, dialed),
        **_churn(t0, t1, p_sessions, first_seen, holes),
        "rttSeries": _rtt_series(t0, t1, rtt_at),
    }


def _session_histogram(sessions: list[float]) -> list[dict]:
    """Session lengths in log-ish buckets. The median is 33 s and the longest is
    a day and a half, so a linear histogram is one bar and a rumour."""
    if not sessions:
        return []
    out = []
    edges = SESSION_EDGES + [None]
    for lo, hi in zip(edges, edges[1:]):
        n = sum(1 for v in sessions if v >= lo and (hi is None or v < hi))
        out.append({"fromS": lo, "toS": hi, "n": n})
    return out


def _rtt_by_ceiling(rtt_at: list[tuple[int, float]], ceilings) -> dict | None:
    """Round trips split by whether the node was against its memory ceiling.

    From inside, a throttled node answers its own pings late — so this is the
    node measuring itself, not the network. Splitting it is the only way to say
    that out loud with numbers.
    """
    if not ceilings or not rtt_at:
        return None
    wins = sorted(ceilings)
    under, quiet = [], []
    i = 0
    for rx, ms in sorted(rtt_at):
        while i < len(wins) and wins[i][1] < rx:
            i += 1
        (under if i < len(wins) and wins[i][0] <= rx <= wins[i][1] else quiet).append(ms)
    under.sort()
    quiet.sort()
    if len(under) < 30 or len(quiet) < 30:
        return None
    return {
        "underCeiling": {
            "medianMs": round(pctl(under, 0.5), 1),
            "p90Ms": round(pctl(under, 0.9), 1),
            "samples": len(under),
        },
        "quiet": {
            "medianMs": round(pctl(quiet, 0.5), 1),
            "p90Ms": round(pctl(quiet, 0.9), 1),
            "samples": len(quiet),
        },
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


def _rtt_series(t0, t1, rtt_at):
    """Round-trip time over the window, median and p90 per bucket.

    Same buckets as the churn lanes, so the two read against each other. The
    per-peer medians only say what a peer was like on average; this shows
    whether the network got slower or faster over the weeks. A bucket with no
    ping answered is None: the recorder was blind, or nobody was connected.
    """
    span = max(t1 - t0, 1)
    step = max(int(span / CHURN_BUCKETS), 60_000)
    nb = int(span / step) + 1
    buckets: list[list[float]] = [[] for _ in range(nb)]
    for t, v in rtt_at:
        buckets[min(nb - 1, max(0, int((t - t0) / step)))].append(v)
    median, p90, samples = [], [], []
    for b in buckets:
        if b:
            b.sort()
            median.append(round(pctl(b, 0.5), 1))
            p90.append(round(pctl(b, 0.9), 1))
        else:
            median.append(None)
            p90.append(None)
        samples.append(len(b))
    return {"t0": t0, "step": step, "median": median, "p90": p90, "samples": samples}


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


def retention(snaps: list[Snap], episodes: list[dict]) -> dict | None:
    """What the process is holding that it is not using.

    The floor is the quietest sample in the half hour after the last restart:
    the node rebuilt its working set from nothing and that is what the work
    costs. Anything it holds above that line later has been kept, not used —
    which is the whole of the allocator story, as one number.
    """
    if not snaps:
        return None
    # The node reports when it started, so use that: a restart that frees less
    # than the episode threshold is still a restart.
    starts = [r.started for r in snaps if r.started]
    at = max(starts) if starts else None
    if at is None:
        restarts = [e for e in episodes if e["kind"] == "restart"]
        if not restarts:
            return None
        at = restarts[-1]["from"]
    after = [
        r.anon for r in snaps
        if r.anon and at <= r.t <= at + RETENTION_WINDOW_MS
    ]
    now = next((r.anon for r in reversed(snaps) if r.anon), None)
    if not after or not now:
        return None
    floor = min(after)
    return {
        "restartAt": at,
        "floorMiB": round(floor / MIB),
        "nowMiB": round(now / MIB),
        "heldMiB": round((now - floor) / MIB),
        "hoursSince": round((snaps[-1].t - at) / 3_600_000, 1),
    }


def build_digest(store: str) -> dict:
    snaps = load_snaps(store)
    if not snaps:
        raise SystemExit("[digest] no snapshots found — nothing to publish")
    snaps.sort(key=lambda r: r.t)

    t0, t1 = snaps[0].t, snaps[-1].t
    span = max(t1 - t0, 60_000)
    expected = int(span / 60_000)
    gaps = sum(1 for a, b in zip(snaps, snaps[1:]) if b.t - a.t > GAP_MS)

    dark0 = next((r for r in snaps if r.dark), None)
    limits = {
        "high": round(((dark0.high if dark0 else None) or 0) / MIB),
        "max": round(((dark0.max if dark0 else None) or 0) / MIB),
    }

    heights = [r.height for r in snaps if r.height is not None]
    lags = sorted(
        r.tip - r.height
        for r in snaps
        if r.tip is not None and r.height is not None
    )
    hours = span / 3_600_000

    peer_vals = [r.peers for r in snaps if r.peers is not None]
    histogram: dict[str, int] = defaultdict(int)
    for p in peer_vals:
        histogram[str(int(p))] += 1
    # Only stretches that last, and never measured across a recording gap:
    # every restart spends its first minute or so with no peers.
    zero_eps, z_from, prev_t = 0, None, None
    for r in snaps:
        if r.peers is None:
            continue
        if prev_t is not None and r.t - prev_t > GAP_MS:
            z_from = None
        if r.peers == 0:
            if z_from is None:
                z_from = r.t
        else:
            if z_from is not None and r.t - z_from >= ZERO_PEER_MIN_MS:
                zero_eps += 1
            z_from = None
        prev_t = r.t
    if z_from is not None and prev_t - z_from >= ZERO_PEER_MIN_MS:
        zero_eps += 1

    temps = sorted(r.temp for r in snaps if r.temp is not None)
    thr = [r.thr for r in snaps if r.thr is not None]
    clean = sum(1 for v in thr if v in ("0x0", "0"))
    hrs = sorted(r.hr for r in snaps if r.hr)

    episodes = find_episodes(snaps)
    ceilings = [
        (e["from"], e.get("to") or t1)
        for e in episodes
        if e["kind"] == "memory_ceiling"
    ]

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
        "zoom": build_series(
            [r for r in snaps if r.t >= t1 - ZOOM_MS], max(t0, t1 - ZOOM_MS), t1
        ),
        "markers": [m for m in MARKERS if t0 <= m["at"] <= t1],
        "episodes": episodes,
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
        "retention": retention(snaps, episodes),
        "chainActivity": chain_activity(store),
        "overlay": overlay_aggregates(store, ceilings),
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
        f"chain {digest['chainActivity']['blocksSeen'] if digest['chainActivity'] else 0} blocks · "
        f"{len(blob)/1024:.1f} KB · "
        f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MiB",
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
