#!/bin/sh
# darkfid's memory every 5 seconds, from its cgroup. Run this on both machines
# you are comparing so the instrument is the same on each.
#
#   anon     what the process asked for and nobody can reclaim — the number that
#            decides whether it fits
#   file     page cache, which the kernel takes back whenever it needs the room
#   current  anon + file + slab, and it is what memory.high is compared against
#   high     how many times the soft ceiling has throttled it
:  "${DARKFID_UNIT:=darkfid.service}"
case "$DARKFID_UNIT" in *.*) ;; *) DARKFID_UNIT="${DARKFID_UNIT}.service" ;; esac
cg=/sys/fs/cgroup/system.slice/$DARKFID_UNIT
out=${OUT:-$HOME/darkfid-bench/mem-5s.tsv}
mkdir -p "$(dirname "$out")"
[ -s "$out" ] || printf "epoch\tanon\tfile\tcurrent\thigh_events\n" > "$out"
while :; do
  if [ -r $cg/memory.stat ]; then
    set -- $(awk '$1=="anon"||$1=="file"{print $2}' $cg/memory.stat)
    printf "%s\t%s\t%s\t%s\t%s\n" "$(date +%s)" "$1" "$2" \
      "$(cat $cg/memory.current)" "$(awk '$1=="high"{print $2}' $cg/memory.events)" >> "$out"
  fi
  sleep 5
done
