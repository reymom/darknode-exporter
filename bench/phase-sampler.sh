#!/bin/bash
# phase-sampler.sh — where a syncing node's time actually goes.
#
# "It takes seven hours" is not an answer. This splits the wall clock into the
# three things it can be spent on, using counters the kernel already keeps, so
# nothing has to be patched into the node:
#
#   CPU            cgroup cpu.stat usage_usec — time actually computing
#   stalled on IO  cgroup io.pressure "some" total — time the cgroup spent
#                  waiting on the block layer (PSI; needs cgroup v2)
#   the rest       wall minus the two above. On a syncing node that is mostly
#                  waiting for peers to send blocks, which is the part nobody
#                  had measured
#
# Plus the volumes, so the rates can be turned into per-block costs:
# bytes read and written by the process, and bytes in and out of the interface.
#
# NETWORK IS MACHINE-WIDE, not per-process: /proc/<pid>/net is a namespace, not a
# process. On a box whose job is running one node that is a fair proxy, and it is
# marked here rather than quietly presented as per-process.
#
#   OUT=~/bench/phases.tsv IFACE=eth0 ./phase-sampler.sh
set -u
: "${DARKFID_UNIT:=darkfid.service}"
: "${BLOCKS:=/var/log/darknode/blocks.tsv}"
: "${OUT:=$HOME/darkfid-bench/phases.tsv}"
: "${IFACE:=$(ip -o -4 route show default | awk '{print $5}' | head -1)}"
: "${EVERY:=5}"
[[ "$DARKFID_UNIT" == *.* ]] || DARKFID_UNIT="${DARKFID_UNIT}.service"
CG="/sys/fs/cgroup/system.slice/${DARKFID_UNIT}"

mkdir -p "$(dirname "$OUT")"
[[ -s "$OUT" ]] || printf 'epoch\theight\tcpu_usec\tio_stall_usec\tmem_stall_usec\tdisk_rd\tdisk_wr\tnet_rx\tnet_tx\tanon\n' > "$OUT"

psi() { awk -v k="$1" '$1==k{for(i=2;i<=NF;i++) if($i ~ /^total=/){sub("total=","",$i); print $i}}' "$2" 2>/dev/null; }

while :; do
  pid=$(systemctl show "$DARKFID_UNIT" -p MainPID --value 2>/dev/null)
  if [[ -n "$pid" && "$pid" != "0" && -r "$CG/cpu.stat" ]]; then
    cpu=$(awk '$1=="usage_usec"{print $2}' "$CG/cpu.stat")
    iop=$(psi some "$CG/io.pressure")
    mep=$(psi some "$CG/memory.pressure")
    rd=$(awk '$1=="read_bytes:"{print $2}' "/proc/$pid/io" 2>/dev/null)
    wr=$(awk '$1=="write_bytes:"{print $2}' "/proc/$pid/io" 2>/dev/null)
    rx=$(cat "/sys/class/net/$IFACE/statistics/rx_bytes" 2>/dev/null)
    tx=$(cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null)
    an=$(awk '$1=="anon"{print $2}' "$CG/memory.stat" 2>/dev/null)
    h=$(tail -1 "$BLOCKS" 2>/dev/null | cut -f2)
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date +%s)" "${h:-}" "${cpu:-}" "${iop:-}" "${mep:-}" \
      "${rd:-}" "${wr:-}" "${rx:-}" "${tx:-}" "${an:-}" >> "$OUT"
  fi
  sleep "$EVERY"
done
