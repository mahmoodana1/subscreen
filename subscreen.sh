#!/data/data/com.termux/files/usr/bin/bash
# Phone half of the sub-screen. Needs nothing but curl.
#
#   ./subscreen.sh [host:port] [interval-seconds]
#
# Redraws in place by homing the cursor and clearing to the end of screen,
# which avoids the full-clear flicker of `clear`.

HOST="${1:-10.0.0.26:8765}"
INTERVAL="${2:-1}"

# Termux ships no tput and TERM is often "dumb", so ask the tty directly.
cols() {
    local size
    size=$(stty size 2>/dev/null) && [ -n "${size#* }" ] && { echo "${size#* }"; return; }
    echo "${COLUMNS:-46}"
}

cleanup() { printf '\033[?25h\n'; }   # restore the cursor on exit
trap cleanup EXIT INT TERM

printf '\033[?25l\033[2J'             # hide cursor, clear once

while :; do
    if frame=$(curl -fsS --max-time 3 "http://$HOST/screen?w=$(cols)" 2>/dev/null); then
        printf '\033[H%s\033[J' "$frame"
    else
        printf '\033[H  offline — retrying (%s)\033[J' "$HOST"
    fi
    sleep "$INTERVAL"
done
