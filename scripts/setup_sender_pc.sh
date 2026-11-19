#!/usr/bin/env bash
# ONE-TIME setup of the SENDER PC (Ubuntu). Run once:   sudo scripts/setup_sender_pc.sh
# Installs docker (for this PC's own Grafana), snmp tools, a python venv; makes the test NIC address permanent; allows the syslog port 514.
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
U=${SUDO_USER:?run through sudo}; ROOT=$(cd "$(dirname "$0")/.." && pwd); NIC=${NIC:-enx207bd51e1472}
apt-get update -y
apt-get install -y docker.io docker-compose-v2 snmp python3-venv rsync git
usermod -aG docker "$U"
cat > /etc/sysctl.d/99-pingpong.conf <<EOF
net.ipv4.ip_unprivileged_port_start=0
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
EOF
sysctl --system >/dev/null
nmcli -t -f NAME connection show | grep -qx pp-test || nmcli connection add type ethernet ifname "$NIC" con-name pp-test ipv4.method manual ipv4.addresses 192.168.50.1/24 ipv6.method disabled connection.autoconnect yes
nmcli connection up pp-test || true
sysctl -w "net.ipv4.conf.$NIC.rp_filter=0" >/dev/null || true
sudo -u "$U" bash -c "cd '$ROOT' && python3 -m venv .venv && .venv/bin/pip install -q prometheus_client==0.26.0"
echo; echo "Done. LOG OUT AND IN AGAIN (or run: newgrp docker) so docker works without sudo, then:  scripts/sender.sh 50 10"
