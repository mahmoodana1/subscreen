#!/usr/bin/env bash
# On a computer: share taskvim's folder with the always-on hub (the Mac mini) over Syncthing.
#
#   ./agent/hub-pair.sh HUB-DEVICE-ID [NAME]    the id agent/hub-linux.sh printed on the hub
#
# Also shortens Syncthing's wait before sending a change of taskvim's file to one second,
# so an edit reaches the other devices at once. Safe to run again.

set -euo pipefail

[ $# -ge 1 ] && [ $# -le 2 ] || { echo "usage: $0 HUB-DEVICE-ID [NAME]" >&2; exit 1; }
id=$1
name=${2:-macmini}
syncthing cli config folders list | grep -qx taskvim-data || { echo "no taskvim-data folder in this computer's Syncthing" >&2; exit 1; }

syncthing cli config devices list | grep -qx "$id" || syncthing cli config devices add --device-id "$id" --name "$name"
syncthing cli config folders taskvim-data devices list | grep -qx "$id" || syncthing cli config folders taskvim-data devices add --device-id "$id"
syncthing cli config folders taskvim-data fswatcher-delays set 1

echo "taskvim is shared with $name. Now, on $name:"
echo "  ~/projects/subscreen/agent/hub-linux.sh --pair $(syncthing device-id)"
