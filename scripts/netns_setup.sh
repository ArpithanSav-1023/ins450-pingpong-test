#!/usr/bin/env bash
# Creates namespaces tx (sender) and rx (receiver) so test traffic really goes out the NICs, through the
# switch (GE1 -> GE9) and back in. Host NICs are NOT moved or reconfigured: management stays reachable.
#   tx: veth pair, host end plugged into nm-bridge (-> enp3s0 -> switch GE1)
#   rx: macvlan on the USB NIC (-> switch GE9, 100 Mbit/s = the bottleneck)
# Run: sudo scripts/netns_setup.sh      Undo: sudo scripts/netns_teardown.sh
# Sender on ANOTHER PC (plugged into GE9, 192.168.50.1):  sudo scripts/netns_setup.sh rxonly
#   rx only: veth into nm-bridge (-> enp3s0 -> switch GE1, 1 Gbit/s) = receiver + echo endpoint (role B, 192.168.50.2)
# This PC is the SENDER (the other PC, on GE9, is the receiver at 192.168.50.2):  sudo scripts/netns_setup.sh txonly
#   tx only: veth into nm-bridge (-> enp3s0 -> switch GE1) = sender endpoint (role A, 192.168.50.1)
# Swapped direction:  sudo scripts/netns_setup.sh swap
#   tx: macvlan on the USB NIC (-> switch GE9, 100 Mbit/s uplink)   rx: veth into nm-bridge (-> enp3s0 -> switch GE1, 1 Gbit/s)
set -euo pipefail
BRIDGE=${BRIDGE:-nm-bridge}
RX_NIC=${RX_NIC:-enx207bd51e1472}
TX_IP=${TX_IP:-192.168.50.1/24}
RX_IP=${RX_IP:-192.168.50.2/24}
MODE=${1:-normal}   # normal | swap | rxonly | txonly

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
ip netns list | grep -qwE "tx|rx" && { echo "namespaces already exist (run netns_teardown.sh first)"; exit 1; }

[ "$MODE" = "rxonly" ] || ip netns add tx
[ "$MODE" = "txonly" ] || ip netns add rx

if [ "$MODE" = "txonly" ]; then
  ip link add veth-tx type veth peer name eth0 netns tx
  ip link set veth-tx master "$BRIDGE" up
  ip -n tx addr add "$TX_IP" dev eth0
  ip -n tx link set eth0 up
  ip -n tx link set lo up
  BRPORT=veth-tx
elif [ "$MODE" = "rxonly" ]; then
  ip link add veth-rx type veth peer name eth0 netns rx
  ip link set veth-rx master "$BRIDGE" up
  ip -n rx addr add "$RX_IP" dev eth0
  ip -n rx link set eth0 up
  ip -n rx link set lo up
  BRPORT=veth-rx
elif [ "$MODE" = "swap" ]; then
  # --- sender: macvlan on the USB NIC (GE9)
  ip link add link "$RX_NIC" name mv-tx type macvlan mode bridge
  ip link set mv-tx netns tx
  ip -n tx link set mv-tx name eth0
  ip -n tx addr add "$TX_IP" dev eth0
  ip -n tx link set eth0 up
  ip -n tx link set lo up
  # --- receiver: veth into the existing bridge (GE1)
  ip link add veth-rx type veth peer name eth0 netns rx
  ip link set veth-rx master "$BRIDGE" up
  ip -n rx addr add "$RX_IP" dev eth0
  ip -n rx link set eth0 up
  ip -n rx link set lo up
  BRPORT=veth-rx
else
  # --- sender: veth into the existing bridge
  ip link add veth-tx type veth peer name eth0 netns tx
  ip link set veth-tx master "$BRIDGE" up
  ip -n tx addr add "$TX_IP" dev eth0
  ip -n tx link set eth0 up
  ip -n tx link set lo up

  # --- receiver: macvlan on the USB NIC
  ip link add link "$RX_NIC" name mv-rx type macvlan mode bridge
  ip link set mv-rx netns rx
  ip -n rx link set mv-rx name eth0
  ip -n rx addr add "$RX_IP" dev eth0
  ip -n rx link set eth0 up
  ip -n rx link set lo up
  BRPORT=veth-tx
fi

# IGMP: the switch's querier sends from 192.168.1.254, outside 192.168.50.0/24. With the default reverse-path filter
# (rp_filter=2, no route back) the namespaces silently drop those queries, never answer, and the switch's IGMP snooping
# ages the groups out after ~260 s (multicast stops mid-run). Accept them.
for ns in $(ip netns list | awk '{print $1}' | grep -xE "tx|rx"); do
  for f in all default eth0; do ip netns exec "$ns" sysctl -qw "net.ipv4.conf.$f.rp_filter=0"; done
done

echo "waiting for bridge port to reach forwarding (STP)..."
for i in $(seq 1 40); do
  bridge link show dev "$BRPORT" 2>/dev/null | grep -q "state forwarding" && break
  sleep 1
done
bridge link show dev "$BRPORT"
if [ "$MODE" = "txonly" ]; then
  echo "ping test tx -> receiver PC (${RX_IP%/*}):"
  ip netns exec tx ping -c 3 -W 2 "${RX_IP%/*}" || echo "PING FAILED (is the other PC's NIC set to ${RX_IP} (scripts/nic_role.sh receiver) and cabled to the switch?)"
elif [ "$MODE" = "rxonly" ]; then
  echo "ping test rx -> sender PC (${TX_IP%/*}):"
  ip netns exec rx ping -c 3 -W 2 "${TX_IP%/*}" || echo "PING FAILED (is the sender PC's NIC set to ${TX_IP} and cabled to the switch?)"
else
  echo "ping test tx -> rx:"
  ip netns exec tx ping -c 3 -W 2 "${RX_IP%/*}" || echo "PING FAILED (check cabling: GE1 and GE9 both up on the switch?)"
fi
