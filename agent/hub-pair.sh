#!/usr/bin/env bash
# On a computer: share taskvim's folder with the A73 hub over Syncthing.
#
#   ./agent/hub-pair.sh HUB-DEVICE-ID     the id agent/hub-termux.sh printed on the phone
#
# Also shortens Syncthing's wait before sending a change of taskvim's file to one second,
# so an edit reaches the other devices at once. Safe to run again.

set -euo pipefail

[ $# -eq 1 ] || { echo "usage: $0 HUB-DEVICE-ID" >&2; exit 1; }
id=$1
syncthing cli config folders list | grep -qx taskvim-data || { echo "no taskvim-data folder in this computer's Syncthing" >&2; exit 1; }

syncthing cli config devices list | grep -qx "$id" || syncthing cli config devices add --device-id "$id" --name a73-hub
syncthing cli config folders taskvim-data devices list | grep -qx "$id" || syncthing cli config folders taskvim-data devices add --device-id "$id"
syncthing cli config folders taskvim-data fswatcher-delays set 1

echo "taskvim is shared with the hub. Now, in Termux on the phone:"
echo "  ~/subscreen/agent/hub-termux.sh --pair $(syncthing device-id)"
