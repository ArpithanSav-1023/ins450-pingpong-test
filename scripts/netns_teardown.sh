#!/usr/bin/env bash
# Removes the tx/rx namespaces and the veth. Moving nothing back is needed: host NICs were never touched.
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
ip link del veth-tx 2>/dev/null || true
ip link del veth-rx 2>/dev/null || true
ip netns del tx 2>/dev/null || true
ip netns del rx 2>/dev/null || true   # destroys the macvlan with it
echo "torn down"; ip netns list
