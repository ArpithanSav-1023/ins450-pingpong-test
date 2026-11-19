#!/usr/bin/env bash
# ONE-TIME, run with sudo:  sudo scripts/enable_sudoless.sh        Undo:  sudo rm /etc/sudoers.d/net-test
# Lets your user run `ip netns exec tx|rx <cmd>` without a password, so traffic.runner / run_all.sh need no sudo.
# Trade-off: that is root-equivalent for your user (anything can be run inside those namespaces). Fine for a private lab PC, remove it afterwards.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
U=${SUDO_USER:?run via sudo from your own login}
IP=$(readlink -f "$(command -v ip)")
F=/etc/sudoers.d/net-test
TMP=$(mktemp)
echo "$U ALL=(root) NOPASSWD: $IP netns exec tx *, $IP netns exec rx *, $IP netns list" > "$TMP"
visudo -cf "$TMP"
install -m 0440 -o root -g root "$TMP" "$F"; rm -f "$TMP"
echo "installed $F:"; cat "$F"
