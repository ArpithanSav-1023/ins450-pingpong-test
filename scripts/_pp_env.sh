# sourced by sender.sh / receiver.sh: works on BOTH PCs, in either role.
#  PP_PORT : which switch port THIS PC is on. GE9 if the 100 Mbit/s USB NIC is here (the Aspire), else GE1. Override: PP_PORT=GE1
#  pp_exec NS cmd...: runs inside the namespace NS when this PC has one (receiver/sender via scripts/netns_setup.sh rxonly|txonly), else directly
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT" || exit 1
# optional per-PC settings (not in git): NIC, PP_PORT, PP_PEER, PP_PEER_DIR, EXTRA (see docs/RUN_SENDER_RECEIVER.md, "A different PC")
[ -f "$ROOT/data/pp_local.env" ] && . "$ROOT/data/pp_local.env"
if [ -z "${PP_PORT:-}" ]; then
  if ls /sys/class/net/ 2>/dev/null | grep -q '^enx207bd51e1472$'; then PP_PORT=GE9; else PP_PORT=GE1; fi
fi
export PP_PORT
if [ "$PP_PORT" = GE9 ]; then
  export PP_PEER=${PP_PEER:-sav1023@192.168.1.22} PP_PEER_DIR=${PP_PEER_DIR:-Code/net/test}
else
  export PP_PEER=${PP_PEER:-abhijith-m-b@192.168.1.40} PP_PEER_DIR=${PP_PEER_DIR:-ins450-pingpong-test}
fi
PY=python3; [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"
IPBIN=$(readlink -f "$(command -v ip)")
pp_has_ns() { "$IPBIN" netns list 2>/dev/null | awk '{print $1}' | grep -qx "$1"; }
pp_exec() {   # pp_exec NS cmd...
  local ns=$1; shift
  if pp_has_ns "$ns"; then sudo -n "$IPBIN" netns exec "$ns" env PYTHONPATH="$ROOT" PP_PORT="$PP_PORT" PP_PEER="$PP_PEER" PP_PEER_DIR="$PP_PEER_DIR" "$@"
  else PYTHONPATH="$ROOT" "$@"; fi
}
