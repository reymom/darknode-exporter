# bench — does your node fit on this box?

A darkfid node's memory is not decided by the node alone. It is decided by the storage
engine, by the memory allocator underneath it, and by the four hundred blocks on the chain
that carry almost all the transactions. This is the rig that separates those.

It exists because on a 4 GB server the default configuration **died four syncs out of seven**,
and one line of configuration fixed it, and neither of those was written down anywhere.

## What it does

One arm = restore the same chain database, start the node under one environment, watch until
it clears a height or dies, record what it cost.

```bash
./snapshot.sh save                    # once: the restore point every arm starts from
./sampler.sh &                        # memory every 5 s, so you get the shape not just a peak

./bench.sh glibc                                                  # as it ships
./bench.sh arena1   MALLOC_ARENA_MAX=1
./bench.sh jemalloc LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
```

Each run appends a row to `runs.tsv`: label, seconds, peak anon, anon at the end, outcome,
CPU seconds and the environment that produced it.

## The one thing that makes it a measurement

**Every arm starts from the same database.** Of about 73,000 blocks on this chain, roughly
four hundred — 64,400 to 64,800 — carry nearly all the contract calls, and that is where a
small box dies. An arm that starts at a different height meets a different amount of that
work, and then you are not comparing allocators, you are comparing starting points.

So take the snapshot just before that stretch. `snapshot.sh` does the copying;
finding where the stretch is on your chain is a matter of looking for where calls per block
jump, and `collector/darkfid-blocks.sh` next door logs exactly that.

## Configuration

All environment variables, all with defaults that suit a stock install.

| | |
|---|---|
| `DARKFID_UNIT` | the systemd unit, default `darkfid.service` |
| `SNAPSHOT` | where the restore point lives |
| `DB` | the node's database |
| `BLOCKS` | the per-block log, used to tell how far an arm got |
| `TARGET_HEIGHT` | the height that counts as passing, default 65000 |
| `TIMEOUT_MIN` | give up after this, default 30 |

## What is in `results/`

The series behind [the measurements published in September 2026](https://reymom.xyz/darknode).
Two machines: an 8 GB ARM Raspberry Pi 5 and a 4 GB x86 VPS.

| file | what it is |
|---|---|
| `curve-*.tsv` | anon MiB against block height through the dense stretch, one file per allocator on the 4 GB box |
| `wide-vps-*.tsv`, `wide-pi-*.tsv` | the same, genesis to tip, on each machine |
| `tx-density-20.tsv`, `wide-tx-200.tsv` | contract calls per block — why the peak is where it is |
| `mem-5s-runs.tsv.gz` | the raw 5-second sampler output covering those runs |
| `runs.log` | the raw outcomes, exactly as the rig wrote them |

**Reading `runs.log` honestly.** It is the unedited record, so `PASO` is passed and `MUERTO`
is killed, and **the three `wasm-*` rows lasting 20 seconds are not data** — those are failed
starts before a rebuild finished, and they were re-run. The rows with a real duration are the
arms. This file is the box's runs; the Pi's are in the `wide-pi-*` curves.

## The module cache

`module-cache.patch` is a separate experiment against the same rig: darkfid compiles the WASM
module on every contract call, and keeping the compiled module takes a call from **121.2 ms
to 61.6**, measured over three arms with the allocator held constant at jemalloc so nothing
is confounded by an OOM.

The unit that has to be kept is the **(engine, module) pair, one per contract** — wasmer's
`Metering` middleware holds per-module state and panics if one engine serves two modules —
and the cache key is the contract's wasm bytes.

**Its untested edge is invalidation.** A redeployment of the same contract is different wasm
bytes and therefore a different key, which should be right, and it has not been exercised.
Said out loud rather than buried, because it is the part that would break.

## Requirements

Linux, systemd, a darkfid you can stop and start, cgroup v2 with the memory controller on,
and enough disk for a second copy of the chain database.
