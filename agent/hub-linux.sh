#!/usr/bin/env bash
# The Mac mini (or any always-on Arch/Omarchy box) as the headless hub.
#
# It keeps its own copy of taskvim's folder through Syncthing (with file versions), runs the
# agent at boot without a desktop, and serves it over HTTPS behind its own Tailscale name, so
# the phone's subscreen and the iPhone's Plan keep working with every computer off, and the
# computers catch up from it when they wake.
#
#   ./agent/hub-linux.sh                install / update everything, print this machine's Syncthing id
#   ./agent/hub-linux.sh --pair ID...   share taskvim with these computers (the ids hub-pair.sh prints)
#   ./agent/hub-linux.sh --status       what is running, temperatures, sync state
#
#   HUB_NAME=macmini   the hostname and Tailscale name (default macmini)
#   NO_TASKVIM=1       skip building taskvim (it takes a while on an old CPU)
#   KEEP_DESKTOP=1     keep booting into the desktop instead of a text console
#
# Safe to run again. Uses sudo for the system parts. MINI.md walks through all of it.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
NAME="${HUB_NAME:-macmini}"
TASKVIM="$HOME/.local/share/taskvim"
CONF="$HOME/.config/subscreen/agent.json"
APPLE_MINI=""
grep -qi apple /sys/class/dmi/id/sys_vendor 2>/dev/null && grep -qi macmini /sys/class/dmi/id/product_name 2>/dev/null && APPLE_MINI=1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [ "${1:-}" = "--status" ]; then
    for u in subscreen-agent syncthing; do
        printf '%-9s %s\n' "$(systemctl --user is-active "$u" 2>/dev/null)" "$u (user)"
    done
    for u in tailscaled mbpfan; do
        systemctl list-unit-files "$u.service" >/dev/null 2>&1 && printf '%-9s %s\n' "$(systemctl is-active "$u" 2>/dev/null)" "$u"
    done
    curl -fsS --max-time 2 http://127.0.0.1:8765/health >/dev/null && echo "agent answers on :8765" || echo "agent does not answer"
    tailscale serve status 2>/dev/null || true
    echo; syncthing cli show connections 2>/dev/null | python3 -c 'import json,sys; [print("peer", k[:7], "connected" if v["connected"] else "offline") for k,v in json.load(sys.stdin)["connections"].items()]' || true
    key=$(syncthing cli config gui apikey get 2>/dev/null || true)
    curl -fsS --max-time 3 -H "X-API-Key: $key" "http://$(syncthing cli config gui raw-address get 2>/dev/null)/rest/db/status?folder=taskvim-data" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("taskvim folder:", d["state"], "·", d["needFiles"], "files to fetch")' || echo "taskvim folder: not set up"
    ls -l "$TASKVIM/data.json" 2>/dev/null || echo "no data.json yet (not paired, or not synced)"
    command -v sensors >/dev/null && { echo; sensors 2>/dev/null | grep -E '^(Package|Core|Exhaust|fan|Left|Right|Main|CPU)' || sensors; }
    exit 0
fi

if [ "${1:-}" = "--pair" ]; then
    shift
    [ $# -gt 0 ] || { echo "usage: $0 --pair DEVICE-ID..." >&2; exit 1; }
    for id in "$@"; do
        syncthing cli config devices list | grep -qx "$id" || syncthing cli config devices add --device-id "$id"
        syncthing cli config folders taskvim-data devices list | grep -qx "$id" || syncthing cli config folders taskvim-data devices add --device-id "$id"
        echo "sharing taskvim with $id"
    done
    echo "Syncthing now pulls taskvim's folder from the computers; watch it with: $0 --status"
    exit 0
fi

[ "$(id -u)" -ne 0 ] || { echo "run this as your own user, not root (it uses sudo where needed)" >&2; exit 1; }
command -v pacman >/dev/null || { echo "this script is for Arch / Omarchy" >&2; exit 1; }

# ---- packages ----
say "packages"
pkgs=(python git curl syncthing tailscale lm_sensors)
[ -n "${NO_TASKVIM:-}" ] || command -v cargo >/dev/null || pkgs+=(rust)
sudo pacman -S --needed --noconfirm "${pkgs[@]}"

# ---- the machine: its name, never asleep, back on after a power cut ----
say "system"
[ "$(hostnamectl hostname 2>/dev/null || hostname)" = "$NAME" ] || { sudo hostnamectl set-hostname "$NAME"; echo "hostname is now $NAME"; }
sudo systemctl mask --now sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target >/dev/null
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandlePowerKey=poweroff\nHandleLidSwitch=ignore\nIdleAction=ignore\n' | sudo tee /etc/systemd/logind.conf.d/10-hub.conf >/dev/null
sudo loginctl enable-linger "$USER"     # user services (agent, Syncthing) run from boot, with nobody logged in

if [ -n "$APPLE_MINI" ]; then
    # The SMC's fan control: without mbpfan, Linux may leave the fan slow while the CPU heats up.
    echo applesmc | sudo tee /etc/modules-load.d/applesmc.conf >/dev/null
    sudo modprobe applesmc 2>/dev/null || true
    if ! command -v mbpfan >/dev/null; then
        command -v yay >/dev/null && { yay -S --needed --noconfirm mbpfan || yay -S --needed --noconfirm mbpfan-git; } \
            || echo "note: install mbpfan from the AUR by hand (no yay here)"
    fi
    command -v mbpfan >/dev/null && sudo systemctl enable --now mbpfan >/dev/null
    # Power on by itself after a power cut (the Intel LPC bridge's AFTERG3 bit; Mac mini 2012).
    if [ "$(cat /sys/bus/pci/devices/0000:00:1f.0/vendor 2>/dev/null)" = "0x8086" ]; then
        sudo tee /etc/systemd/system/hub-autopoweron.service >/dev/null <<'EOF'
[Unit]
Description=Power the Mac mini back on after a power cut

[Service]
Type=oneshot
ExecStart=/usr/bin/setpci -s 00:1f.0 0xa4.b=0

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now hub-autopoweron.service >/dev/null
    fi
fi

if [ -z "${KEEP_DESKTOP:-}" ]; then
    [ "$(systemctl get-default)" = multi-user.target ] || {
        sudo systemctl set-default multi-user.target
        echo "boots to a text console from now on (undo: sudo systemctl set-default graphical.target)"
    }
fi

# ---- tailscale: its own node and https name; ssh in with `ssh $NAME` ----
say "tailscale"
sudo systemctl enable --now tailscaled >/dev/null
state=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' || true)
if [ "$state" != "Running" ]; then
    echo "Sign in with the link below (the same account as the other devices):"
    sudo tailscale up --hostname="$NAME" --ssh
else
    sudo tailscale set --hostname="$NAME" --ssh
fi
sudo tailscale set --operator="$USER"

# ---- syncthing: taskvim's folder, with 30 days of old versions ----
say "syncthing"
mkdir -p "$TASKVIM"
systemctl --user enable --now syncthing.service >/dev/null
for _ in $(seq 1 30); do syncthing cli show system >/dev/null 2>&1 && break; sleep 1; done
if ! syncthing cli config folders list | grep -qx taskvim-data; then
    # A data.json made here before pairing (say, by trying taskvim) would look newer than the
    # real one and win the first sync. Keep it aside instead.
    [ -f "$TASKVIM/data.json" ] && mv "$TASKVIM/data.json" "$TASKVIM/data.json.before-hub-$(date +%s)"
    syncthing cli config folders add --id taskvim-data --label taskvim --path "$TASKVIM"
fi
syncthing cli config folders taskvim-data fswatcher-delays set 1
syncthing cli config folders taskvim-data versioning type set staggered
syncthing cli config folders taskvim-data versioning params set maxAge 2592000

# ---- the prayer times: the hypr panel's modules, from the dotfiles (not stowed here) ----
say "prayer modules"
if [ ! -f "$HOME/dotfiles/hypr/.config/hypr/panel/timetable.py" ] && [ ! -f "$HOME/.config/hypr/panel/timetable.py" ]; then
    if [ -e "$HOME/dotfiles" ]; then
        echo "note: ~/dotfiles exists but has no hypr panel; the hub will have no prayer times"
    else
        git clone --depth 1 -b desktop https://github.com/mahmoodana1/dotfiles "$HOME/dotfiles"
    fi
fi

# ---- the agent ----
say "agent"
mkdir -p "$(dirname "$CONF")"
python3 - "$CONF" <<'PY'
import json, sys
path = sys.argv[1]
try:
    conf = json.load(open(path))
except (OSError, ValueError):
    conf = {}
conf["kind"] = "Hub"
json.dump(conf, open(path, "w"), indent=2)
PY
"$HERE/install.sh" --headless
tailscale serve --bg 8765 >/dev/null

# ---- taskvim (phone-sync: reloads data.json when the agent or Syncthing changes it) ----
if [ -z "${NO_TASKVIM:-}" ]; then
    say "taskvim (a few minutes on this CPU)"
    TV="$HOME/projects/taskvim"
    [ -d "$TV/.git" ] || git clone -b phone-sync https://github.com/mahmoodana1/taskvim "$TV"
    git -C "$TV" pull --ff-only || true
    # Only build. Never `cargo test` on a machine with real data (see taskvim's history).
    (cd "$TV" && cargo build --release)
    mkdir -p "$HOME/.local/bin"
    install -m755 "$TV/target/release/taskvim" "$HOME/.local/bin/taskvim"
fi

url=$(tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("CertDomains") or [""])[0])')
say "done"
echo "Plan for the iPhone:  https://$url/plan/"
echo "Phone subscreen:      add $NAME:8765 to Computers in the app's menu"
echo
echo "This machine's Syncthing id. On the PC and on the laptop, run:"
echo "  ~/projects/subscreen/agent/hub-pair.sh $(syncthing device-id) $NAME"
echo "and then run here the line each of them prints."
[ -f "$CONF" ] && grep -q ntfy "$CONF" || echo "Reminders: copy the \"ntfy\" line from the PC's ~/.config/subscreen/agent.json into $CONF, then: systemctl --user restart subscreen-agent"
