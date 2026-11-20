#!/usr/bin/env bash
# ANY PC whose test NIC is a dedicated card (the Aspire, the new test PC): make its test NIC the sender (192.168.50.1) or the receiver (192.168.50.2). Run with sudo, between runs:
#     sudo scripts/nic_role.sh sender        |        sudo scripts/nic_role.sh receiver
# Also keeps 192.168.1.15 on the NIC (+ a route to the switch only) so the SNMP poller can read the switch.
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$ROOT/data/pp_local.env" ] && . "$ROOT/data/pp_local.env"     # NIC=..., EXTRA=... (extra address for reaching the switch; EXTRA="" = none)
NIC=${NIC:-enx207bd51e1472}; EXTRA=${EXTRA-192.168.1.15}
case "${1:-}" in sender) A=192.168.50.1; O=192.168.50.2;; receiver) A=192.168.50.2; O=192.168.50.1;; *) echo "usage: sudo scripts/nic_role.sh sender|receiver"; exit 1;; esac
nmcli connection modify pp-test ipv4.method manual ipv4.addresses "$A/24${EXTRA:+,$EXTRA/24}" ipv6.method disabled
nmcli connection down pp-test >/dev/null 2>&1; nmcli connection up pp-test
# NetworkManager sometimes applies only one of the addresses to a live interface: make the live state match, whatever it did
ip addr del "$O/24" dev "$NIC" 2>/dev/null
ip addr show dev "$NIC" | grep -q "inet $A/24" || ip addr add "$A/24" dev "$NIC"
if [ -n "$EXTRA" ]; then
  ip addr show dev "$NIC" | grep -q "inet $EXTRA/24" || ip addr add "$EXTRA/24" dev "$NIC"
  ip route replace 192.168.1.254/32 dev "$NIC" src "$EXTRA"
fi
sysctl -qw "net.ipv4.conf.$NIC.rp_filter=0"
ip -br a show "$NIC"
ip addr show dev "$NIC" | grep -q "inet $A/24" && echo "this PC is now the ${1^^} ($A)" || { echo "FAILED: $A is not on $NIC"; exit 1; }
