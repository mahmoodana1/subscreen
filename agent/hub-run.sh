#!/data/data/com.termux/files/usr/bin/bash
# Keep the hub's three programs running on the phone (Termux:Boot runs this, and so
# does hub-termux.sh). Each runs in a loop that restarts it; one already running is left alone.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export STHOMEDIR="$HOME/.local/state/syncthing"
TS_DIR="$HOME/.local/state/tailscale"
LOGS="$HOME/.cache/subscreen"
mkdir -p "$LOGS" "$TS_DIR"

termux-wake-lock 2>/dev/null || true    # keep the CPU awake while the screen is off

keep() {   # name, command...
    local name=$1; shift
    pgrep -f "$name" >/dev/null && return
    nohup bash -c 'while :; do "$@"; sleep 5; done' _ "$@" >>"$LOGS/$name.log" 2>&1 &
}

keep tailscaled tailscaled --tun=userspace-networking --statedir="$TS_DIR" --socket="$TS_DIR/tailscaled.sock" --port=0
keep syncthing syncthing serve --no-browser --no-restart
keep subscreen_agent.py python3 "$HERE/subscreen_agent.py"
