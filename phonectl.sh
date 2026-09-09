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
#
# Seeing anything still needs the Termux:X11 *app* open on the phone —
# that is an Android activity, nothing here can start it.

set -uo pipefail

ACCENT_FILE="$HOME/.cache/subscreen/accent"
SERVER_PORT=8765
PHONE=phone-lan
SSH_OPTS=(-o ConnectTimeout=25 -o BatchMode=yes)

# Wifi power-save makes the first connect time out; one retry is normal.
phone() { timeout 90 ssh "${SSH_OPTS[@]}" "$PHONE" "$@" 2>&1 | grep -v 'proot warning'; }
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
        subscreen_tui.py subscreen-x11.sh prayer.py timetable.py chime.wav \
        "$PHONE:~/arch-fs/root/" || { echo "scp failed" >&2; exit 1; }
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
