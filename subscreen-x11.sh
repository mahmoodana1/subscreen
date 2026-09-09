#!/bin/bash
# Launch the sub-screen in the phone's Arch/i3 X session, zoomed way up.
# Run this INSIDE the proot (i3 already running), e.g. from dmenu or:
#   ~/start-arch.sh "export DISPLAY=127.0.0.1:0; ~/subscreen-x11.sh"

export DISPLAY="${DISPLAY:-127.0.0.1:0}"

HOST="${SUBSCREEN_HOST:-10.0.0.26:8765}"
# The cell size sets everything: a bigger cell means readable task text
# and fewer rows, which in turn scales the block clock down. 24 is the
# balance between a tall clock and legible tasks under it.
FONT_SIZE="${SUBSCREEN_FONT_SIZE:-27}"
FONT_FAMILY="${SUBSCREEN_FONT:-Adwaita Mono}"

# -fullscreen is a hint to a window manager. With no WM running, xterm
# keeps its 80x24 default and spills off the screen, so work out a fit
# from the display size and the font's approximate cell.
GEOMETRY=()
if ! i3-msg -t get_version >/dev/null 2>&1; then
    read -r SCREEN_W SCREEN_H <<<"$(
        xdpyinfo | awk '/dimensions:/ {split($2, d, "x"); print d[1], d[2]}'
    )"
    if [ -n "$SCREEN_W" ]; then
        PX=$(( FONT_SIZE * 96 / 72 ))            # points -> pixels at 96dpi
        COLS=$(( SCREEN_W * 10 / (PX * 6) ))     # cell advance ~= 0.6em
        ROWS=$(( SCREEN_H * 10 / (PX * 12) ))    # line height ~= 1.2em
        GEOMETRY=(-geometry "${COLS}x${ROWS}+0+0")
    fi
fi

exec xterm \
    -class subscreen \
    -fullscreen \
    "${GEOMETRY[@]}" \
    -fa "$FONT_FAMILY" -fs "$FONT_SIZE" \
    -bg '#000000' -fg '#c9d1d9' \
    -b 8 \
    +sb \
    -xrm 'xterm*cursorBlink: false' \
    -e python3 "$HOME/subscreen_tui.py" "$HOST"
