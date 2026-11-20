#!/usr/bin/env bash
# ONE-TIME, on a receiver/sender PC with a dedicated test NIC (the new test PC, the Aspire):   sudo scripts/install_sudoless.sh
# Afterwards  scripts/nic_role.sh sender|receiver  needs no password and no sudo.
# How: a ROOT-OWNED copy of nic_role.sh is installed as /usr/local/sbin/pp-nic-role with the NIC name and extra address written into it
# (it does not read your repo, so editing the repo can never give root), plus a sudoers rule that allows exactly
#   pp-nic-role sender   and   pp-nic-role receiver   for your user. Nothing else.
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
U=${SUDO_USER:?run through sudo}; ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$ROOT/data/pp_local.env" ] && . "$ROOT/data/pp_local.env"
NIC=${NIC:-enx207bd51e1472}; EXTRA=${EXTRA-192.168.1.15}
[[ "$NIC" =~ ^[a-zA-Z0-9]+$ ]] || { echo "bad NIC name: $NIC"; exit 1; }
[[ -z "$EXTRA" || "$EXTRA" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad EXTRA address: $EXTRA"; exit 1; }
T=$(mktemp)
# keep the script, but replace the two lines that read the repo's settings with fixed values
grep -v 'pp_local.env' "$ROOT/scripts/nic_role.sh" | sed "s|^NIC=.*|NIC=$NIC; EXTRA=$EXTRA|" > "$T"
grep -q "^NIC=$NIC; EXTRA=$EXTRA\$" "$T" || { echo "could not bake NIC into the copy"; exit 1; }
install -m 755 -o root -g root "$T" /usr/local/sbin/pp-nic-role
rm -f "$T"
cat > /etc/sudoers.d/pp-test <<EOF
$U ALL=(root) NOPASSWD: /usr/local/sbin/pp-nic-role sender, /usr/local/sbin/pp-nic-role receiver
EOF
chmod 440 /etc/sudoers.d/pp-test
visudo -cf /etc/sudoers.d/pp-test >/dev/null || { rm -f /etc/sudoers.d/pp-test; echo "sudoers check failed, rule removed"; exit 1; }
echo "OK: $U can now run  scripts/nic_role.sh sender|receiver  without a password (NIC $NIC${EXTRA:+, extra address $EXTRA})."
