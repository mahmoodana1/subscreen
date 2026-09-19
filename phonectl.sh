#!/usr/bin/env bash
# Drive the phone sub-screen from the desktop.
#
#   ./phonectl.sh status            what is up, on both ends
#   ./phonectl.sh deploy            copy the client to the phone and relaunch
#   ./phonectl.sh start             X + i3 + sub-screen on the phone
#   ./phonectl.sh launch            sub-screen only (i3 already running)
#   ./phonectl.sh stop              close the sub-screen window
#   ./phonectl.sh color '#ff5555'   re-tint the clock
#   ./phonectl.sh reset             back to the default blue
#   ./phonectl.sh theme aurora      swap palette + ambient animation
#                                   names: grove | abyss | aurora | circuitry | murmuration
#
# Seeing anything still needs the Termux:X11 *app* open on the phone —
# that is an Android activity, nothing here can start it.

set -uo pipefail

ACCENT_FILE="$HOME/.cache/subscreen/accent"
HOST_CACHE="$HOME/.cache/subscreen/phone-host"
SERVER_PORT=8765
PHONE=phone-lan
PHONE_PORT="$(ssh -G "$PHONE" 2>/dev/null | awk '/^port /{print $2; exit}')"
PHONE_PORT="${PHONE_PORT:-8022}"
SSH_OPTS=(-o ConnectTimeout=25 -o BatchMode=yes)

# Is anything listening on the phone's ssh port at $1? One cheap TCP probe,
# no auth — used both as a liveness check and as the LAN-scan filter.
port_open() { timeout 2 bash -c "cat </dev/null >/dev/tcp/$1/$PHONE_PORT" 2>/dev/null; }

# Where do we currently think the phone is? The cache wins (it holds the
# last address a rescan proved), otherwise whatever ssh_config resolves to.
phone_host() {
    if [ -s "$HOST_CACHE" ]; then
        cat "$HOST_CACHE"
    else
        ssh -G "$PHONE" 2>/dev/null | awk '/^hostname /{print $2; exit}'
    fi
}

# Android hands the phone a new DHCP lease whenever it feels like it, which
# used to mean every command here died on "connection refused" until the
# ssh_config was hand-edited. Instead: sweep the local /24 for the ssh port
# and keep the first address that answers. Identity is not taken on trust —
# ssh still verifies the pinned HostKeyAlias key on every later command, so
# a stranger squatting on the port fails verification rather than getting
# our commands.
rediscover_phone() {
    local subnet ip found=""
    subnet="$(ip -4 route get 1.1.1.1 2>/dev/null |
              awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
    [ -n "$subnet" ] || return 1
    subnet="${subnet%.*}"
    echo "phone unreachable — scanning ${subnet}.0/24 for port $PHONE_PORT …" >&2
    for ip in $(seq 1 254); do
        ( port_open "$subnet.$ip" && echo "$subnet.$ip" ) &
    done >"${TMPDIR:-/tmp}/phonectl-scan.$$" 2>/dev/null
    wait
    found="$(head -1 "${TMPDIR:-/tmp}/phonectl-scan.$$" 2>/dev/null)"
    rm -f "${TMPDIR:-/tmp}/phonectl-scan.$$"
    [ -n "$found" ] || { echo "no host answering on port $PHONE_PORT" >&2; return 1; }
    mkdir -p "$(dirname "$HOST_CACHE")"
    printf '%s\n' "$found" > "$HOST_CACHE"
    echo "phone found at $found (cached; host key still checked)" >&2
    return 0
}

# Run once before any command: if the remembered address is dead, rescan.
# SSH_OPTS then carries an explicit HostName so scp inherits the fix too.
ensure_phone() {
    local host
    host="$(phone_host)"
    if [ -z "$host" ] || ! port_open "$host"; then
        rediscover_phone || return 1
        host="$(phone_host)"
    fi
    SSH_OPTS+=(-o "HostName=$host")
}

ensure_phone || echo "warning: phone not found on the LAN — commands will fail" >&2

# Wifi power-save makes the first connect time out; one retry is normal.
phone() { timeout "${PHONE_TIMEOUT:-90}" ssh "${SSH_OPTS[@]}" "$PHONE" "$@" 2>&1 | grep -v 'proot warning'; }
proot() { phone "~/start-arch.sh \"$1\""; }

case "${1:-status}" in
status)
    if ss -tlnp 2>/dev/null | grep -q ":$SERVER_PORT"; then
        echo "desktop server : up on :$SERVER_PORT"
    else
        echo "desktop server : DOWN — python3 $HOME/projects/subscreen/serve.py &"
    fi
    if [ -r "$ACCENT_FILE" ]; then
        echo "accent         : $(cat "$ACCENT_FILE")"
    else
        echo "accent         : default"
    fi
    proot 'if pgrep -x i3 >/dev/null; then echo phone-i3:running; else echo phone-i3:stopped; fi
           if pgrep -x xterm >/dev/null; then echo phone-subscreen:running; else echo phone-subscreen:stopped; fi' |
        awk -F: '{printf "%-15s: %s\n", $1, $2}'
    ;;
start)
    echo "starting X + i3 on the phone …"
    phone './start-dashboard.sh > /dev/null 2>&1 &'
    sleep 8
    "$0" launch
    echo "now open the Termux:X11 app on the phone to see it"
    ;;
deploy)
    # prayer.py is a symlink into the hypr panel, so the phone and the
    # desktop HUD can never drift apart on prayer maths; scp copies the
    # file it points at. The client reads its source once at startup, so
    # deploying without relaunching changes nothing — hence the relaunch.
    cd "$(dirname "$0")" || exit 1
    timeout 120 scp "${SSH_OPTS[@]}" \
        subscreen_tui.py subscreen-x11.sh prayer.py timetable.py chime.wav athan.mp3 \
        "$PHONE:~/arch-fs/root/" || { echo "scp failed" >&2; exit 1; }
    # Themes that ship pixel-art frames (currently just "cats") need their
    # asset dir next to subscreen_tui.py on the phone — the TUI resolves
    # them via __file__, so the path structure has to match the desktop.
    timeout 120 scp -r "${SSH_OPTS[@]}" \
        assets \
        "$PHONE:~/arch-fs/root/" || { echo "scp assets failed" >&2; exit 1; }
    proot 'chmod +x /root/subscreen-x11.sh /root/subscreen_tui.py; echo deployed'
    "$0" sync-times
    "$0" stop >/dev/null 2>&1
    sleep 1
    "$0" launch
    ;;
launch)
    proot 'export DISPLAY=127.0.0.1:0; nohup /root/subscreen-x11.sh >/root/sub.log 2>&1 & sleep 3; tail -2 /root/sub.log'
    echo "launched (check /root/sub.log on the phone if nothing appears)"
    ;;
stop)
    # Killing the xterm can leave the python orphaned; the bracket keeps
    # the pattern from matching this command's own proot bash -c line.
    proot 'pkill -x xterm; pkill -f "[s]ubscreen_tui.py"; echo stopped'
    ;;
sync-times)
    # Refresh the published Awqaf timetable on the laptop, then push this
    # month and next to the phone. Roughly one network request a month;
    # the phone itself never fetches, it only reads the cache. Pushing two
    # months means a month boundary can never strand it on computed times.
    PANEL="$HOME/.config/hypr/panel"
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/hypr-panel"
    python3 -c "
import sys; sys.path.insert(0, '$PANEL')
import timetable
print('timetable refreshed:', timetable.refresh())" || echo "refresh failed (using existing cache)" >&2

    months=$(date +%Y-%m; date -d '+1 month' +%Y-%m)
    files=()
    for m in $months; do
        [ -f "$CACHE/timings-$m.json" ] && files+=("$CACHE/timings-$m.json")
    done
    if [ ${#files[@]} -eq 0 ]; then
        echo "no cached timetable to push — phone will use computed times" >&2
        exit 1
    fi
    phone 'mkdir -p ~/arch-fs/root/.cache/hypr-panel' >/dev/null
    timeout 90 scp "${SSH_OPTS[@]}" "${files[@]}" \
        "$PHONE:~/arch-fs/root/.cache/hypr-panel/" >/dev/null \
        || { echo "pushing timetable failed" >&2; exit 1; }
    echo "pushed timetable: $(basename -a "${files[@]}" | tr '\n' ' ')"
    ;;
test-alert)
    # Fires the chime and the nagbar the way a real prayer time would,
    # so the notifier can be checked without waiting for Fajr.
    proot 'export DISPLAY=127.0.0.1:0 PULSE_SERVER=127.0.0.1
           paplay /root/chime.wav 2>&1 | head -2
           nohup i3-nagbar -t warning -m "TEST — prayer notifier" >/dev/null 2>&1 &
           sleep 6; pkill -x i3-nagbar; echo "chime played, nagbar raised and dismissed"'
    ;;
test-athan)
    # Fires the athan the way a real prayer rollover would: 7 s fade-in,
    # then hold at max; moving the mouse cuts it. Runs for up to
    # ${2:-270} seconds — the whole 3 m 42 s recording plus its 30 s tail
    # pad — before self-terminating. The ssh timeout has to outlast that,
    # or the dying session takes mpv down with it just before the end.
    dur="${2:-270}"
    export PHONE_TIMEOUT=$((dur + 40))
    cd "$(dirname "$0")" || exit 1
    timeout 30 scp "${SSH_OPTS[@]}" test_athan.py \
        "$PHONE:~/arch-fs/root/" >/dev/null || { echo "scp test_athan.py failed" >&2; exit 1; }
    proot "export DISPLAY=127.0.0.1:0 PULSE_SERVER=127.0.0.1
           command -v mpv >/dev/null || { echo 'mpv missing on phone — install it first'; exit 1; }
           ls -l /dev/input/mice 2>/dev/null | head -1
           cd /root && python3 /root/test_athan.py $dur"
    ;;
theme)
    # The client polls ~/.cache/subscreen/theme once per fetch (5 s), so
    # the switch is live — no relaunch required. Unknown names silently
    # fall back to grove on the phone.
    [ $# -ge 2 ] || { echo "usage: $0 theme <grove|abyss|aurora|circuitry|forest|murmuration|sunrise>" >&2; exit 1; }
    proot "mkdir -p /root/.cache/subscreen && printf '%s\n' '$2' > /root/.cache/subscreen/theme && echo theme=$2"
    ;;
color)
    [ $# -ge 2 ] || { echo "usage: $0 color '#rrggbb'" >&2; exit 1; }
    mkdir -p "$(dirname "$ACCENT_FILE")"
    printf '%s\n' "$2" > "$ACCENT_FILE"
    echo "accent -> $2  (phone picks it up within ~5s)"
    ;;
reset)
    rm -f "$ACCENT_FILE"
    echo "accent -> default"
    ;;
*)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
