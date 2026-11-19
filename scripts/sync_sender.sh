#!/usr/bin/env bash
# Copies the sender code to the remote sender PC (Python 3 only, no root needed there).
#   scripts/sync_sender.sh user@host [remote_dir]      default remote_dir: ~/pp
HOST=${1:?usage: scripts/sync_sender.sh user@host [remote_dir]}
DIR=${2:-pp}
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
ssh -i "${PP_SSH_KEY:-$HOME/.ssh/id_pp_sender}" -o BatchMode=yes "$HOST" "mkdir -p $DIR/samples $DIR/runs" || exit 1
rsync -a -e "ssh -i ${PP_SSH_KEY:-$HOME/.ssh/id_pp_sender}" --delete --exclude __pycache__ traffic scenarios "$HOST:$DIR/" || exit 1
rsync -a -e "ssh -i ${PP_SSH_KEY:-$HOME/.ssh/id_pp_sender}" samples/charts "$HOST:$DIR/samples/" || exit 1
echo "synced to $HOST:$DIR"
