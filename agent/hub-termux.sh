#!/data/data/com.termux/files/usr/bin/bash
# The old Android phone (the Galaxy A73) as the always-on hub, in Termux.
#
# It keeps its own copy of taskvim's data.json through Syncthing and runs the agent
# behind its own Tailscale name (a73-hub), so the iPhone's Plan can read and edit
# with every computer off, and the computers catch up from it when they wake.
#
#   ./agent/hub-termux.sh               install / update, start everything, print this phone's Syncthing id
#   ./agent/hub-termux.sh --pair ID...  share taskvim with these computers (the ids hub-pair.sh prints)
#   ./agent/hub-termux.sh --status      what is running
#
# Safe to run again. Runs from Termux (F-Droid build); Termux:Boot starts it after a reboot.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export STHOMEDIR="$HOME/.local/state/syncthing"
TS_DIR="$HOME/.local/state/tailscale"
SOCK="$TS_DIR/tailscaled.sock"
TASKVIM="$HOME/.local/share/taskvim"
CONF="$HOME/.config/subscreen/agent.json"
BOOT="$HOME/.termux/boot/subscreen-hub"
NAME="a73-hub"

ts() { tailscale --socket="$SOCK" "$@"; }
up() { "$HERE/hub-run.sh"; }    # (re)starts whatever is not running

if [ "${1:-}" = "--status" ]; then
    for p in tailscaled syncthing subscreen_agent.py; do
        pgrep -f "$p" >/dev/null && echo "running  $p" || echo "STOPPED  $p"
    done
    ts status 2>/dev/null | head -5 || true
    ts serve status 2>/dev/null || true
    curl -fsS --max-time 2 http://127.0.0.1:8765/health >/dev/null && echo "agent answers on :8765" || echo "agent does not answer"
    exit 0
fi

if [ "${1:-}" = "--pair" ]; then
    shift
    [ $# -gt 0 ] || { echo "usage: $0 --pair DEVICE-ID..." >&2; exit 1; }
    for id in "$@"; do
        syncthing cli config devices add --device-id "$id" 2>/dev/null || true
        syncthing cli config folders taskvim-data devices add --device-id "$id" 2>/dev/null || true
        echo "sharing taskvim with $id"
    done
    echo "Syncthing now pulls taskvim's file from the computers; check with: ls -l $TASKVIM"
    exit 0
fi

# ---- packages ----
pkg install -y python git curl syncthing tailscale >/dev/null 2>&1 || pkg install -y python git curl syncthing tailscale
command -v tailscaled >/dev/null || { echo "Termux has no tailscale package here; the hub needs it (pkg search tailscale)" >&2; exit 1; }

# ---- config ----
mkdir -p "$TS_DIR" "$TASKVIM" "$HOME/.cache/subscreen" "$(dirname "$CONF")" "$(dirname "$BOOT")"
python3 - "$CONF" "$SOCK" <<'PY'
import json, sys
path, sock = sys.argv[1:]
try:
    conf = json.load(open(path))
except (OSError, ValueError):
    conf = {}
conf.update(kind="Hub", tailscale_socket=sock)
json.dump(conf, open(path, "w"), indent=2)
PY
[ -f "$STHOMEDIR/config.xml" ] || syncthing generate >/dev/null

cat > "$BOOT" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot: start the subscreen hub after the phone restarts.
exec "$HERE/hub-run.sh"
EOF
chmod +x "$BOOT" "$HERE/hub-run.sh"

up
sleep 4

# ---- syncthing: one folder, taskvim's ----
for _ in $(seq 1 20); do syncthing cli show system >/dev/null 2>&1 && break; sleep 1; done
syncthing cli config folders list | grep -qx taskvim-data \
    || syncthing cli config folders add --id taskvim-data --label taskvim --path "$TASKVIM"
syncthing cli config folders taskvim-data fswatcher-delays set 1

# ---- tailscale: its own node, so it has its own https name ----
state=$(ts status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' || true)
if [ "$state" != "Running" ]; then
    echo "Sign in to Tailscale with the link below (the same account as the other devices):"
    ts up --hostname="$NAME"
fi
ts serve --bg 8765 >/dev/null
url=$(ts status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("CertDomains") or [""])[0])')

echo
echo "Hub is up:  https://$url/plan/"
echo "This phone's Syncthing id (give it to ./agent/hub-pair.sh on each computer):"
echo "  $(syncthing device-id)"
echo
echo "Once per phone: install Termux:Boot (F-Droid) and open it once, and set Termux's"
echo "battery use to Unrestricted, so Android leaves the hub running."
