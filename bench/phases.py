#!/usr/bin/env python3
"""
phases.py — turn phase-sampler output into "where did the time go".

    ./phases.py ~/darkfid-bench/phases-*.tsv

For each arm it prints what the wall clock was spent on and what it moved, and
then the same numbers divided by blocks applied, which is the only form in which
two arms of different lengths can be compared.

READING THE OUTPUT, and this is where it is easy to fool yourself:

  cpu          total CPU across all threads. darkfid runs about fourteen, so on
               a two-core box this can be up to twice the wall clock. It is not
               "time spent", it is work done.
  busy         cpu / (wall x cores). 100% means both cores were saturated for
               the whole run: the node was computing, not waiting.
  io stalled   PSI "some": the share of wall during which at least one task in
               the cgroup was blocked on the block layer. It is an upper bound
               on disk cost, not time lost — other threads may have run.
  net in/out   MACHINE-WIDE, not per-process, because /proc/<pid>/net is a
               namespace. On a box whose job is one node it is a fair proxy and
               it is labelled rather than quietly presented as per-process.

If `busy` is high the node is compute-bound and the WASM runtime is most of it
(see the per-call phase split). If `busy` is low and `io stalled` is low, the
node is waiting for peers to send blocks, and no amount of local tuning moves it.
"""
from __future__ import annotations

import os
import sys


def load(path: str) -> list[dict]:
    rows = []
    with open(path) as fh:
        head = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            v = line.rstrip("\n").split("\t")
            if len(v) != len(head):
                continue
            d = {}
            for k, x in zip(head, v):
                d[k] = int(x) if x.strip().lstrip("-").isdigit() else None
            rows.append(d)
    return rows


def sync_segment(rows: list[dict]) -> list[dict]:
    """The part of the file that is this arm's sync, and nothing else.

    A sampler file spans the restart: the node is stopped, the database is put
    back, and the node starts again. Across that boundary the height jumps DOWN
    by thousands of blocks and every per-process counter resets to zero, so a
    plain last-minus-first reads as a large negative number and quietly produces
    nonsense. The first version of this script did exactly that.

    So: cut at the last point where the height fell, and take what follows.
    """
    hs = [(i, r.get("height")) for i, r in enumerate(rows) if r.get("height") is not None]
    cut = 0
    for k in range(1, len(hs)):
        if hs[k][1] < hs[k - 1][1]:
            cut = hs[k][0]
    seg = rows[cut:]
    # and inside it, a counter that still falls means a restart we did not see
    for key in ("cpu_usec", "disk_rd", "disk_wr"):
        xs = [(i, r[key]) for i, r in enumerate(seg) if r.get(key) is not None]
        for k in range(1, len(xs)):
            if xs[k][1] < xs[k - 1][1]:
                seg = seg[xs[k][0]:]
                break
    return seg


def delta(rows: list[dict], key: str) -> int:
    xs = [r[key] for r in rows if r.get(key) is not None]
    return (xs[-1] - xs[0]) if len(xs) >= 2 else 0


def gib(b: int) -> str:
    return f"{b / 1073741824:.2f} G" if b >= 1073741824 else f"{b / 1048576:.0f} M"


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        sys.exit(2)

    cores = os.cpu_count() or 1
    print(f"cores assumed: {cores}   (override with CORES=)")
    cores = int(os.environ.get("CORES", cores))
    print()

    for p in sorted(paths):
        rows = sync_segment(load(p))
        if len(rows) < 3:
            print(f"{os.path.basename(p)}: too few samples")
            continue
        label = os.path.basename(p).replace("phases-", "").replace(".tsv", "")
        wall = rows[-1]["epoch"] - rows[0]["epoch"]
        cpu = delta(rows, "cpu_usec") / 1e6
        io_stall = delta(rows, "io_stall_usec") / 1e6
        mem_stall = delta(rows, "mem_stall_usec") / 1e6
        rd, wr = delta(rows, "disk_rd"), delta(rows, "disk_wr")
        rx, tx = delta(rows, "net_rx"), delta(rows, "net_tx")
        hs = [r["height"] for r in rows if r.get("height") is not None]
        blocks = (hs[-1] - hs[0]) if len(hs) >= 2 else 0
        anon = [r["anon"] for r in rows if r.get("anon") is not None]

        busy = 100 * cpu / (wall * cores) if wall else 0
        stalled = 100 * io_stall / wall if wall else 0

        print(f"── {label}")
        print(f"   wall {wall:5.0f} s   blocks {blocks:5d}   peak anon {max(anon) / 1048576:6.0f} MiB"
              f"   ends at {anon[-1] / 1048576:6.0f} MiB")
        print(f"   cpu {cpu:7.0f} s  ->  busy {busy:5.1f}% of {cores} cores")
        print(f"   io stalled {io_stall:6.1f} s  ->  {stalled:4.1f}% of wall      mem stalled {mem_stall:.1f} s")
        print(f"   disk  read {gib(rd):>8}   written {gib(wr):>8}")
        print(f"   net   in   {gib(rx):>8}   out     {gib(tx):>8}   (machine-wide)")
        if blocks:
            print(f"   per block:  {1000 * wall / blocks:6.1f} ms wall   {1000 * cpu / blocks:6.1f} ms cpu"
                  f"   {wr / blocks / 1024:7.0f} KiB written   {rx / blocks / 1024:7.0f} KiB in")
        print()


if __name__ == "__main__":
    main()
