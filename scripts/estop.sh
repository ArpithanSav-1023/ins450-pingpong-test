#!/usr/bin/env bash
# EMERGENCY STOP: every sender and the receiver watch this file and exit within ~100 ms.
# (Only helps while the processes are running. A run suspended with Ctrl-Z must be killed instead.)
STOP="${SNMP_DEMO_STOP_FILE:-$(cd "$(dirname "$0")/.." && pwd)/.STOP}"
touch "$STOP" && echo "STOP file created: $STOP"
