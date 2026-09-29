#!/usr/bin/env bash
# snapshot.sh — the restore point every arm starts from.
#
#   ./snapshot.sh save      stop the node, copy its database aside
#   ./snapshot.sh restore    put that copy back
#
# WHY THIS MATTERS MORE THAN IT LOOKS. The interesting memory behaviour on this
# chain is not spread out: of ~73,000 blocks, about four hundred carry nearly all
# the transactions, and that is where a small box dies. If each arm starts from a
# different height it meets a different amount of that work and the arms are not
# comparable. Take the snapshot just BEFORE that stretch and every arm meets the
# same four hundred blocks.
#
# On the testnet used for these measurements the stretch is blocks
# 64,400–64,800, so the snapshot was taken at 64,566. Find yours by looking for
# where contract calls per block jump: collector/darkfid-blocks.sh logs exactly
# that.
set -u
: "${DARKFID_UNIT:=darkfid.service}"
: "${SNAPSHOT:=$HOME/darkfid-bench/snapshot}"
: "${DB:=$HOME/.local/share/darkfi/darkfid/testnet}"
[[ "$DARKFID_UNIT" == *.* ]] || DARKFID_UNIT="${DARKFID_UNIT}.service"

case "${1:-}" in
  save)
    [[ -d "$DB" ]] || { echo "no database at $DB" >&2; exit 1; }
    systemctl stop "$DARKFID_UNIT" 2>/dev/null; sleep 3
    mkdir -p "$(dirname "$SNAPSHOT")"
    rm -rf "$SNAPSHOT"; cp -a "$DB" "$SNAPSHOT"
    echo "saved $(du -sh "$SNAPSHOT" | cut -f1) → $SNAPSHOT"
    systemctl start "$DARKFID_UNIT"
    ;;
  restore)
    [[ -d "$SNAPSHOT" ]] || { echo "no snapshot at $SNAPSHOT" >&2; exit 1; }
    systemctl stop "$DARKFID_UNIT" 2>/dev/null; sleep 3
    rm -rf "$DB"; cp -a "$SNAPSHOT" "$DB"
    systemctl start "$DARKFID_UNIT"
    echo "restored and started"
    ;;
  *) echo "usage: $0 save|restore" >&2; exit 2 ;;
esac
