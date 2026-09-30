# bench

Measuring what it costs a `darkfid` node to stay on the chain.

Every result below is reproducible with the scripts in this directory and the series in
[`results/`](results/). The one exception is the per-call split, which is read off the node's
own journal and is marked where it appears.

## Results

**The allocator decides whether a 4 GB machine finishes the sync.** Three configurations,
same restored database, same 4 GB x86 VPS, 29 September 2026, `darkfid 0.5.0` built 25
September, fjall backend.

| configuration | outcome | wall² | blocks | peak anon² | held after | ms/block |
|---|---|---|---|---|---|---|
| glibc, as it ships | **killed at 64,721** | 481 s | 163 | 3,101 MiB | — | 2,951 |
| `LD_PRELOAD` jemalloc | passed | 669 s | 477 | 2,388 MiB | 898 MiB | **1,403** |
| jemalloc, decay tuned¹ | passed | 725 s | 437 | 2,351 MiB | 841 MiB | 1,659 |

¹ `MALLOC_CONF=background_thread:true,dirty_decay_ms:0,muzzy_decay_ms:0`

² **Two instruments run over these arms and they do not agree, so both are in `results/`.**
`wall` and `ms/block` are counted from the first height the node reported after the restart
to the last sample, which is what `phases.py` prints from `phases-*.tsv.gz`;
`runs-2026-09-29.tsv` times the same arms from `systemctl start` and reads 661 / 882 / 1,002 s.
`peak anon` above is the rig's, polled every 20 s; the phase sampler polls every 5 s, catches
3,395 / 2,417 / 2,377 MiB, and is the better instrument for a peak.

**Tuning jemalloc did not pay off in this run.** Returning pages to the kernel immediately
buys 37 MiB of peak and costs **18% more time per block**, so of the three the default is the
one to run. These are three points in a large space and I have not swept it.

**Where the wall clock goes**, from cgroup `cpu.stat`, `io.pressure` and `/proc/<pid>/io`
over the same three runs:

| | glibc | jemalloc | jemalloc tuned |
|---|---|---|---|
| CPU busy, 2 cores | 49% | 53% | 54% |
| stalled on block I/O | 8.5% of wall | 5.0% | 4.1% |
| received per block | 58 KiB | 32 KiB | 35 KiB |
| written per block | 5.8 MiB | 7.1 MiB | 7.7 MiB |

**It is not the network and it is not waiting on disk.** The node receives about 32 KiB per
block and spends the wall clock computing. Two things follow and both are worth a look:

- **Write amplification is roughly 230×**: 32 KiB in, 7.1 MiB out to disk, per block.
- **52% of a contract call is starting the WASM runtime**, 66.9 ms of a 129 ms `PoWRewardV1`
  call, n = 14,048, decomposed from the journal's microsecond stamps rather than from
  `results/`. The module is recompiled on every call, so a sync from genesis compiles the
  same contract 73,038 times.

**Keeping the compiled module takes 19.5% off the wall clock** for the same stretch, same
binary and same allocator. `module-cache.patch` is the change I used for that measurement.

| arm | wall | peak anon | held after |
|---|---|---|---|
| module cache off | 821 s | 2,418 MiB | 913 MiB |
| module cache on | **661 s** | 2,463 MiB | 957 MiB |
| cranelift instead of the cache | 742 s | 2,819 MiB | 950 MiB |

The unit that has to be kept is the `(engine, module)` pair, one per contract, because
wasmer's `Metering` middleware holds per-module state and panics if one engine serves two
modules. The cache key is the contract's wasm bytes. **Its untested edge is invalidation:** a
redeployment of the same contract is different wasm bytes and therefore a different key,
which should be correct and has not been exercised.

## Running it

```bash
./snapshot.sh save                    # once: the restore point every arm starts from
./sampler.sh &                        # memory every 5 s
./phase-sampler.sh &                  # cpu, io stall, disk and network

./bench.sh glibc
./bench.sh jemalloc LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
./phases.py ~/darkfid-bench/phases-*.tsv
```

The phase table above is that last command over the files in `results/`, which it reads
gzipped as they are shipped:

```bash
CORES=2 ./phases.py results/phases-*.tsv.gz
```

Each arm appends a row to `runs.tsv` with the label, wall time, peak anon, anon at the end,
outcome, CPU seconds and the environment that produced it.

## Why every arm restores the same database

This chain runs flat at **one contract call per block**, the miner's reward, across all
73,000 of them. It rises in one place: blocks **64,400 to 64,800 hold 1,022 calls against a
baseline of 400** — the two busiest two-hundred-block buckets on the chain, 488 and 534
against a next highest of 244, and **almost half of every call the chain carries above that
baseline**. That is where a small machine fails. An arm that starts at a different height
meets a different amount of that work, so without a common restore point the comparison is
between starting points rather than between configurations.

`snapshot.sh` handles the copying. Finding the stretch on another chain is a matter of
looking for where calls per block jump, which `collector/darkfid-blocks.sh` logs.

**The rig refuses to start when another systemd drop-in is in force**, because a leftover
`LD_PRELOAD` makes a control arm run as the treatment while reporting itself as the control.
`BENCH_TAKEOVER=1` moves them aside for the run and restores them on exit. Every arm prints
the environment actually in force before it starts.

## Configuration

| | |
|---|---|
| `DARKFID_UNIT` | the systemd unit, default `darkfid.service` |
| `SNAPSHOT` | where the restore point lives |
| `DB` | the node's database |
| `BLOCKS` | the per-block log, used to tell how far an arm got |
| `TARGET_HEIGHT` | the height that counts as passing, default 65000 |
| `TIMEOUT_MIN` | give up after this, default 30 |

## Scope and limits

- **Three configurations on x86 only.** The ARM board has no snapshot at a comparable height
  and making one means a full resync from genesis, so its curves in `results/` come from
  complete syncs rather than from this rig.
- **Small n, and three sittings.** Counting every allocator arm this box has run: glibc as it
  ships was **killed in 5 of 8**, `MALLOC_ARENA_MAX=1` survived 3 of 3, `MALLOC_ARENA_MAX=2`
  1 of 1, and jemalloc **7 of 7** including the tuned arm — nineteen arms over 23, 25 and 29
  September. Outcome lines are in `runs.log` for the second sitting and `runs-2026-09-29.tsv`
  for the third. **The first sitting predates this rig and has no outcome log**; what is here
  is its unlabelled five-second memory series, `mem-5s-runs.tsv.gz`, in which the arms are
  seven segments peaking at 3,463 / 3,417 / 2,397 / 3,012 / 3,285 / 3,348 / 2,398 MiB — the
  five above 3 GiB are the default allocator and two of them were killed.
- **Network figures are machine-wide**, taken from the interface counters, because
  `/proc/<pid>/net` is a namespace rather than a process. On a machine whose only job is the
  node it is a fair proxy, and it is not a per-process measurement.
- **PSI must be enabled** for the stall columns. Kernels booted without `psi=1` do not have
  it, and the sampler leaves those columns empty rather than failing.

## What is in `results/`

| file | what it is |
|---|---|
| `runs-2026-09-29.tsv` | the three arms in the first table |
| `phases-*.tsv.gz` | cpu, io stall, disk and network every 5 s through each of them |
| `curve-*.tsv` | anon MiB against block height, one file per setting |
| `wide-vps-*.tsv`, `wide-pi-*.tsv` | the same, genesis to tip, on each machine |
| `tx-density-20.tsv`, `wide-tx-200.tsv` | contract calls, in 20-block and 200-block buckets. The second covers the whole chain, and **every count in it is doubled**: two copies of the block logger ran while it was collected, so halve it |
| `mem-5s-runs.tsv.gz` | the 23 September sitting: 7.5 h of 5-second samples, unlabelled |
| `runs.log` | the September run log, unedited. `PASO` is passed, `MUERTO` is killed, and three `wasm-*` rows lasting 20 seconds are failed starts rather than data |

## Requirements

Linux with systemd, a `darkfid` you can stop and start, cgroup v2 with the memory controller
enabled, and enough disk for a second copy of the chain database.
