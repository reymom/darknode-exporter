#!/bin/sh
# One line per block darkfid applies: when, height, how many contract calls it
# carried, and how much gas they burned. With the per-minute snapshots this is
# what lets the history show memory and verification cost against block height,
# and the chain's own activity over its whole length.
#
# The journal alone cannot do it: on this board it lives in RAM, so a reboot
# erases it.
#
# Gas: each call prints "Gas used: N/max" several times as it runs (metadata,
# exec, apply), cumulative, so the value just before "Successfully applied" is
# that call's total. Summed per block.
#
# mawk -W interactive: plain mawk buffers a pipe and would sit on the lines for
# hours, which looks exactly like a logger that is not running.
set -eu

: "${HISTORY_DIR:=/var/log/darknode}"
out="$HISTORY_DIR/blocks.tsv"

mkdir -p "$HISTORY_DIR"
[ -s "$out" ] || printf "epoch\theight\tcalls\tgas\tmix\n" > "$out"

while :; do
  journalctl -u darkfid -f -n 0 -o short-unix --no-pager 2>/dev/null \
    | mawk -W interactive -v out="$out" '
        # Contracts announce themselves in their own log lines, as
        # "Contract log: [TransferV1] …", so the call that follows can be named.
        # journalctl keeps the colour escapes, so the first [..] on a line is
        # an ANSI code, not the contract. Anchor on the label.
        /Contract log: \[/ {
            if (match($0, /Contract log: \[[A-Za-z0-9_]+\]/))
                kind = substr($0, RSTART + 15, RLENGTH - 16)
        }
        /Gas used:/            { split($NF, g, "/"); last = g[1] + 0 }
        /Successfully applied/ {
            calls++; gas += last; last = 0
            if (kind != "") { cnt[kind]++; kind = "" } else { cnt["unnamed"]++ }
        }
        /Appended proposal/    {
            mix = ""
            for (k in cnt) { mix = mix (mix == "" ? "" : ",") k ":" cnt[k] }
            split("", cnt)
            printf "%d\t%s\t%d\t%d\t%s\n", $1, $NF, calls, gas, (mix == "" ? "-" : mix) >> out
            fflush(out); calls = 0; gas = 0
        }
      '
  sleep 5
done
