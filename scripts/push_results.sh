#!/usr/bin/env bash
# On the SENDER PC: publish the sender-side results of one run so the receiver PC can `git pull` them.
#   scripts/push_results.sh runs/<run_dir>
RUN=${1:?usage: scripts/push_results.sh runs/<run_dir>}
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
B="results-$(basename "$RUN")"
git checkout -q -B "$B" || exit 1
git add -f "$RUN"/stats_A.json "$RUN"/feed_A.log 2>/dev/null
git commit -q -m "sender results $(basename "$RUN")" && git push -q -f origin "$B" && echo "pushed branch $B"
git checkout -q main
