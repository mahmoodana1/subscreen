#!/usr/bin/env python3
"""Sub-screen client for the phone's Arch/i3 (Termux-X11) session.

Polls the desktop's /state endpoint and draws a large, glanceable frame:
a block clock with a breathing colon, the next prayer floated top-right
over a progress bar through the current prayer window, the top two tasks,
and a daily-goal bar with a seven-day sparkline.

Two rules keep it readable on a phone:

* Solid areas are drawn as *background-coloured spaces*, never as block
  glyphs — a phone proot has few fonts, and a space renders identically
  in all of them.
* The screen is repainted by line diff, never cleared, so nothing
  flickers. Only rows that actually changed are rewritten.

    python3 subscreen_tui.py [host:port]
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Prayer times are resolved on this device, never fetched live. timetable.py
# reads the published Jordan Awqaf timings out of a month-at-a-time JSON
# cache and never touches the network on a read; prayer.py computes them
# astronomically when no cache is there. Preferring the timetable is what
# keeps the phone agreeing with the desktop HUD — the published and computed
# times differ by about a minute. Only tasks and goal need the desktop.
try:
    import prayer as prayer_lib
except Exception:                      # deployed without it: fall back to the server
    prayer_lib = None
try:
    import timetable as timetable_lib
except Exception:                      # no cache module: computed times still work
    timetable_lib = None

HOST = sys.argv[1] if len(sys.argv) > 1 else "10.0.0.26:8765"
FETCH_EVERY = 5.0     # seconds between server polls
FRAME_EVERY = 0.1     # 10fps, enough for the pulses to look smooth

# 3x5 cells per glyph. Tried a 5x7 font for "nicer" and it was worse: at
# this scale single-cell strokes go spindly, and the chunky slab is what
# reads across a room. Do not switch back.
GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ":": ("0", "1", "0", "1", "0"),
}
GLYPH_ROWS = 5
CLOCK_CELLS = 4 * 3 + 1 + 5      # four digits, a colon, a gap after each

RESET = "\033[0m"
SYNC_ON = "\033[?2026h"          # ignored by terminals that lack it
SYNC_OFF = "\033[?2026l"

# Black, white, and a cool rain-soaked green — blue-leaning and muted,
# the green of wet grass under a grey sky.
LIME = "#4e8570"      # the one accent
PALE = "#79b39c"      # the same green lifted, for urgency
WHITE = "#ffffff"
TEXT = "#e6e6e6"
DIM = "#7a7a7a"
FAINT = "#151515"     # unfilled track of a bar
ROW_HOT = "#131313"   # bands stay neutral; the edge carries the meaning
ROW_COOL = "#0b0b0b"
EDGE_COOL = "#2f2f2f"
LINE = "#9a9a9a"      # thin separator — grey, not white, so it stays hairline
LINE_SOFT = "#454545" # the quieter separator between the two tasks

# Exactly one shadow, offset proportionally to the glyph so it reads as a
# slab lifted off the background. More than one step stacks into stripes
# and stops looking like a shadow at all.
SHADOW_MIX = 0.58     # toward black; lower is more visible
SPARK_HI = "#a3dcc4"  # a day that hit the goal
SPARK_EMPTY = "#3f3f3f"   # a day that did not — visible, not invisible

# What the panel shows before the laptop has ever answered: the clock and
# prayer still work, there is simply nothing to say about tasks.
OFFLINE = {"tasks": [], "open_tasks": 0, "goal": {}, "task_error": None, "accent": None}
ALERT = "#ff6b6b"     # overdue only — the one place colour means danger
INK = "#000000"       # the background is pure black

COOL = LIME           # default clock accent
WARM = PALE           # prayer is close
GREEN = LIME

NAG_SECONDS = 90      # how long the i3-nagbar stays up
ALERT_SECONDS = 120   # how long the in-screen banner stays up
CHIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chime.wav")

# Burn-in drift. An OLED showing a near-static clock for hours will ghost
# it, so the whole frame tours a few character offsets. The layout is
# composed into a correspondingly smaller box, so nothing is pushed off
# the right edge — right-aligned text moves with everything else.
SHIFT_EVERY = 180.0   # seconds at each offset
SHIFT_PATH = ((0, 0), (2, 1), (4, 0), (1, 1), (3, 0), (0, 1), (4, 1), (2, 0))
SPARK = "▁▂▃▄▅▆▇█"


def _rgb(colour):
    return tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))


def fg(colour):
    r, g, b = _rgb(colour)
    return f"\033[38;2;{r};{g};{b}m"


def bg(colour):
    r, g, b = _rgb(colour)
    return f"\033[48;2;{r};{g};{b}m"


def mix(a, b, t):
    """Blend two hex colours; t=0 gives a, t=1 gives b."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _rgb(a)
    br, bg_, bb = _rgb(b)
    return "#%02x%02x%02x" % (
        round(ar + (br - ar) * t),
        round(ag + (bg_ - ag) * t),
        round(ab + (bb - ab) * t),
    )


def terminal_size():
    """Ask the tty directly, never the environment.

    shutil.get_terminal_size() prefers $COLUMNS/$LINES, and those leak in
    from the non-interactive ssh launch as 80x24 — which silently renders
    the whole layout into a box the wrong shape.
    """
    try:
        return os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError):
        return shutil.get_terminal_size(fallback=(46, 20))


def drift():
    """Current burn-in offset as (dx, dy).

    Keyed off wall-clock time rather than uptime, so a restart lands on
    the same offset the previous run would have been using instead of
    resetting every session to the top-left.
    """
    return SHIFT_PATH[int(time.time() // SHIFT_EVERY) % len(SHIFT_PATH)]


def shadow_offset(scale_x, scale_y):
    """Where the single drop shadow sits, in cells."""
    return max(1, scale_x // 3), max(1, scale_y // 3)


def glyph_gap(scale_x):
    """Space between digits: half a cell, not a whole one.

    A full cell of air between every glyph costs five cells of width,
    which at large scales is enough to force the whole clock down a size.
    """
    return max(1, scale_x // 2)


def clock_width(text, scale_x):
    """Width in columns of the rendered clock, gaps included."""
    gap = glyph_gap(scale_x)
    return sum(
        (len(GLYPHS[c][0]) if c in GLYPHS else 3) * scale_x + gap for c in text
    )


def big_lines(text, scale_x, scale_y, colour, colon_colour, shadow):
    """Render text as blocks of coloured spaces, over an offset shadow.

    Painted into a cell grid rather than straight to strings, because the
    shadow has to sit *under* the glyph where the two overlap. Runs of one
    colour then collapse into a single escape, which also cuts the bytes
    per frame well below the old per-cell approach.
    """
    gap = glyph_gap(scale_x)
    off_x, off_y = shadow_offset(scale_x, scale_y)
    width = clock_width(text, scale_x) + off_x
    grid = [[None] * width for _ in range(GLYPH_ROWS * scale_y + off_y)]

    def paint(dx, dy, tint_of):
        column = 0
        for char in text:
            glyph = GLYPHS.get(char)
            if glyph is None:
                column += 4 * scale_x
                continue
            tint = tint_of(char)
            if tint is None:          # blinked off: its shadow goes too
                column += len(glyph[0]) * scale_x + gap
                continue
            for row, bits in enumerate(glyph):
                for cell, bit in enumerate(bits):
                    if bit != "1":
                        continue
                    for yy in range(scale_y):
                        for xx in range(scale_x):
                            grid[row * scale_y + yy + dy][
                                column + cell * scale_x + xx + dx
                            ] = tint
            column += len(glyph[0]) * scale_x + gap

    paint(off_x, off_y, lambda char: None if char == ":" and colon_colour is None else shadow)
    paint(0, 0, lambda char: colon_colour if char == ":" else colour)

    lines = []
    for row in grid:
        out, i = "", 0
        while i < len(row):
            tint, j = row[i], i
            while j < len(row) and row[j] == tint:
                j += 1
            out += (bg(tint) + " " * (j - i) + RESET) if tint else " " * (j - i)
            i = j
        lines.append(out.rstrip())   # trailing blanks are the erase-to-EOL's job
    return lines


def rule(width, colour):
    """A thin separator line."""
    return fg(colour) + "─" * width + RESET


def thin_bar(fraction, width, colour, track=LINE_SOFT):
    """A hairline progress bar.

    Drawn with rule glyphs rather than filled cells: a background block is
    a whole row tall, which next to one line of text reads as a slab.
    """
    filled = max(0, min(width, round(fraction * width)))
    return (
        fg(colour) + "━" * filled + fg(track) + "─" * (width - filled) + RESET
    )


def bar(fraction, width, colour, track=FAINT, edge=None):
    """A progress bar of coloured spaces — no glyph dependency.

    `edge` tints the leading cell so the bar reads as moving rather than
    merely long.
    """
    width = max(1, width)
    filled = max(0, min(width, round(fraction * width)))
    out = ""
    if filled:
        if edge and filled > 1:
            out += bg(colour) + " " * (filled - 1) + RESET + bg(edge) + " " + RESET
        else:
            out += bg(edge or colour) + " " * filled + RESET
    if width - filled:
        out += bg(track) + " " * (width - filled) + RESET
    return out


def sparkline(counts, target, accent):
    """Seven days of completions, oldest first, today last.

    Spaced out because at phone font sizes adjacent eighth-blocks merge
    into one indistinct line.
    """
    top = max([target] + list(counts)) or 1
    cells = []
    for offset, count in enumerate(counts):
        today = offset == len(counts) - 1
        level = min(len(SPARK) - 1, round(count / top * (len(SPARK) - 1)))
        if count >= target and target:
            tint = SPARK_HI          # goal met: the brightest thing in the row
        elif count:
            tint = PALE
        else:
            tint = WHITE if today else SPARK_EMPTY
        cells.append(fg(tint) + SPARK[level] + RESET)
    return " ".join(cells)


def chime():
    """Play the prayer chime through the PulseAudio start-dashboard.sh runs.

    PULSE_SERVER has to be set explicitly: the proot inherits nothing, and
    pulse is reachable only over the TCP socket on 127.0.0.1.
    """
    if not os.path.exists(CHIME):
        return
    env = dict(os.environ, PULSE_SERVER=os.environ.get("PULSE_SERVER", "127.0.0.1"))
    try:
        subprocess.Popen(
            ["paplay", CHIME],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def notify(message):
    """Raise an i3-nagbar over whatever is focused.

    i3-nagbar is the only notifier the proot has — no dunst, no
    notify-send — but it ships with i3, so nothing needs installing.
    It has no timeout of its own, hence the expiry we track and kill.
    """
    try:
        proc = subprocess.Popen(
            ["i3-nagbar", "-t", "warning", "-m", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    return proc, time.monotonic() + NAG_SECONDS


def fetch(host):
    """Grab the payload and anchor it to a monotonic reading.

    The clock is driven off the server's timestamp, never the phone's own
    time: the proot carries no timezone, so datetime.now() there reports
    UTC and would disagree with the desktop by hours.
    """
    with urllib.request.urlopen(f"http://{host}/state", timeout=4) as response:
        state = json.load(response)
    state["_base"] = datetime.fromisoformat(state["generated"])
    state["_mono"] = time.monotonic()
    return state


def local_view(cache={}):
    """Clock and prayer computed here, or None if prayer.py is absent.

    ZoneInfo is named explicitly because the proot carries no system
    timezone — datetime.now() there reports UTC. The schedule itself is
    recomputed once a minute; the countdown in between comes from the
    clock, so this costs nothing at 10fps.
    """
    source = timetable_lib or prayer_lib
    if source is None or prayer_lib is None:
        return None
    now = datetime.now(ZoneInfo(prayer_lib.TIMEZONE))
    # Recompute on the minute, and immediately once the countdown reaches
    # the prayer itself — otherwise the rollover, and the chime with it,
    # waits for the cache to expire and lands up to a minute late.
    expired = (
        not cache
        or (now - cache["at"]).total_seconds() > 60
        or now >= cache["schedule"]["next_time"]
    )
    if expired:
        cache.clear()
        cache.update(at=now, schedule=source.schedule(now))
    schedule = cache["schedule"]
    key = schedule["next_prayer"]
    return now, {
        "next": key,
        "next_display": prayer_lib.DISPLAY_NAMES.get(key, key.title()),
        "at": schedule["next_time"].strftime("%H:%M"),
        "iqama": (
            schedule["next_iqama"].strftime("%H:%M")
            if schedule["next_iqama"]
            else None
        ),
        "next_at": schedule["next_time"],
        "tomorrow": schedule["next_is_tomorrow"],
        # Marked when the month's timetable is missing and these are the
        # computed times, which run about a minute off the published ones.
        "estimated": schedule.get("source") == "computed",
    }


def server_view(state):
    """The same shape, rebuilt from the server payload, for the fallback."""
    elapsed = time.monotonic() - state["_mono"]
    now = state["_base"] + timedelta(seconds=elapsed)
    p = state["prayer"]
    return now, {
        "next": p["next"],
        "next_display": p["next_display"],
        "at": p["at"],
        "iqama": p["iqama"],
        "next_at": now + timedelta(seconds=p["seconds_until"] - elapsed),
        "tomorrow": p["tomorrow"],
        "estimated": p.get("estimated", True),
    }


def countdown(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def filled_row(left, right, width, row_bg, edge_colour, left_fg, right_fg):
    """One task as a band of colour running the full width.

    The band, not a bullet, is what separates the two tasks; the edge
    block on the left is what says which one is important.
    """
    edge = bg(edge_colour) + "  "
    # Trim to fit rather than overflow: the tag gives way first, then the
    # label, so a narrow screen still gets a band exactly `width` wide.
    if width - 4 - len(right) < 1:
        right = right[: max(0, width - 5)]
    left = left[: max(0, width - 5 - len(right))]
    gap = width - 4 - len(left) - len(right)
    return (
        edge
        + bg(row_bg)
        + fg(left_fg)
        + " "
        + left
        + " " * max(1, gap)
        + fg(right_fg)
        + right
        + " "
        + RESET
    )


def visible_len(text):
    """Length ignoring SGR escapes, for right-aligning coloured strings."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            i = text.find("m", i) + 1
            continue
        out += 1
        i += 1
    return out


def compose(state, stale, width, height, now, prayer, alert=None):
    """Build the whole frame as a list of lines, sized to the terminal."""
    seconds_left = (prayer["next_at"] - now).total_seconds()
    close = seconds_left <= 15 * 60

    accent = WARM if close else (state.get("accent") or COOL)
    colon = accent      # steady, never blinking

    # Pick the biggest clock that fits, keeping the digits in their natural
    # 3:5 proportion. Scaling x and y independently is what made them look
    # squat: a cell is twice as tall as it is wide, so the vertical scale
    # has to be about half the horizontal one to come out square.
    # Below the clock: the shared date/prayer line, the rule and three
    # column rows — five, plus one of slack. The banner needs four more, so
    # the clock gives way to it rather than being truncated off the bottom.
    # The offline notice and the banner both append below the columns, so
    # the clock has to give up their rows too. Without this the notice is
    # pushed past the last line and truncated — losing precisely the thing
    # that says the tasks on screen are no longer true.
    rows_for_clock = height - 6 - (4 if alert else 0) - (2 if stale else 0)
    scale_x, scale_y = 1, 1
    # Rows are the scarce resource, so size from height first and take the
    # matching width. Deriving height from width instead leaves the clock
    # far narrower than it needs to be whenever rows run out first.
    clock_text = now.strftime("%H:%M")
    for tall in range(6, 0, -1):
        wide = tall * 2                      # a cell is ~2x as tall as wide
        off_x, off_y = shadow_offset(wide, tall)
        while wide > 1 and clock_width(clock_text, wide) + off_x > width - 2:
            wide -= 1
            off_x = shadow_offset(wide, tall)[0]
        if GLYPH_ROWS * tall + off_y <= rows_for_clock:
            scale_x, scale_y = wide, tall
            break
    shadow = mix(accent, INK, SHADOW_MIX)

    body = []
    block = clock_width(clock_text, scale_x) + shadow_offset(scale_x, scale_y)[0]
    pad = max(0, (width - block) // 2)
    body.extend(
        " " * pad + line
        for line in big_lines(clock_text, scale_x, scale_y, accent, colon, shadow)
    )

    # Date and prayer share one line under the clock: date centred, prayer
    # right-aligned. If they would collide the date gives up its centring
    # and sits left, so the two never overlap on a narrow screen.
    label = prayer["next_display"].upper()
    if prayer["tomorrow"]:
        label += "+1"
    corner = f"{label} {prayer['at']}"
    if prayer.get("estimated"):
        corner += "~"        # computed, not the published timetable
    corner += f"  {countdown(seconds_left)}"
    if prayer["iqama"]:
        corner += f"  iq {prayer['iqama']}"
    corner = corner[: max(0, width - 3)]

    date_text = now.strftime("%a  %d  %b").upper() + now.strftime("   %S″")
    start = max(0, (width - len(date_text)) // 2)
    if start + len(date_text) > width - len(corner) - 3:
        start = 1
    gap = width - start - len(date_text) - len(corner) - 1

    line = " " * start + fg(WHITE) + date_text[:-4] + fg(DIM) + date_text[-4:] + RESET
    if gap >= 1:
        line += " " * gap + fg(accent) + corner + RESET
    body.append(line)
    if gap < 1:                       # no room to share — give prayer its own row
        body.append(" " * max(0, width - len(corner) - 1) + fg(accent) + corner + RESET)
    body.append(rule(width, LINE))

    # Lower half is two columns: tasks on the left, progress on the right,
    # divided by a hairline. Both are built to a fixed width so the rule
    # above cannot stretch either of them.
    right_width = max(20, min(32, width // 3))
    left_width = max(10, width - right_width - 3)

    left = []
    if state.get("task_error"):
        left.append(" " + fg(ALERT) + state["task_error"][: left_width - 2] + RESET)
    elif not state.get("tasks"):
        left.append(" " + fg(DIM) + "nothing open" + RESET)
    else:
        for index, task in enumerate(state["tasks"]):
            if index:
                left.append(rule(left_width, LINE_SOFT))   # separates the bands
            tag = task["section"].upper()
            if task["due"]:
                tag += ("   OVERDUE " if task["overdue"] else "   DUE ") + task["due"]
            if task["important"]:
                left.append(
                    filled_row(
                        task["text"], tag, left_width, ROW_HOT, accent,
                        WHITE, mix(accent, WHITE, 0.3),
                    )
                )
            else:
                left.append(
                    filled_row(
                        task["text"], tag, left_width, ROW_COOL, EDGE_COOL,
                        TEXT, ALERT if task["overdue"] else DIM,
                    )
                )

    goal = state.get("goal") or {}
    target = goal.get("target") or 0
    done_today = goal.get("done_today", 0)
    right = []
    if target:
        tint = PALE if done_today >= target else accent
        right.append(
            fg(DIM) + "goal " + RESET
            + thin_bar(min(1.0, done_today / target), 10, tint)
            + fg(DIM) + f" {done_today}/{target}" + RESET
        )
        week = goal.get("week") or []
        if week:
            right.append(fg(DIM) + "week " + RESET + sparkline(week, target, accent))
    right.append(fg(DIM) + f"{state['open_tasks']} open" + RESET)

    for index in range(max(len(left), len(right))):
        cell_l = left[index] if index < len(left) else ""
        cell_r = right[index] if index < len(right) else ""
        body.append(
            cell_l
            + " " * max(0, left_width - visible_len(cell_l))
            + fg(LINE_SOFT) + " │ " + RESET
            + cell_r
        )

    if alert:
        # A band you cannot miss from across the room. It blinks on the
        # same one-second beat as the colon — deliberate, not fluttering.
        band = LIME if now.microsecond < 500_000 else mix(LIME, INK, 0.45)
        body.append(bg(band) + " " * width + RESET)
        body.append(bg(band) + fg(INK) + alert.center(width) + RESET)
        body.append(bg(band) + " " * width + RESET)
    if stale:
        body.append("")
        body.append(" " + fg(DIM) + "laptop offline — tasks not updating" + RESET)

    slack = height - len(body) - 1
    if slack > 1:
        body = [""] * (slack // 2) + body

    return body[: max(1, height - 1)]


class Screen:
    """Repaints by line diff so the display never flickers."""

    def __init__(self):
        self.previous = []
        sys.stdout.write("\033[?25l" + bg(INK) + "\033[2J")
        sys.stdout.flush()

    def draw(self, frame):
        out = [SYNC_ON]
        for row, line in enumerate(frame):
            if row >= len(self.previous) or self.previous[row] != line:
                out.append(f"\033[{row + 1};1H{line}\033[K")
        for row in range(len(frame), len(self.previous)):
            out.append(f"\033[{row + 1};1H\033[K")
        out.append(SYNC_OFF)
        if len(out) > 2:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        self.previous = list(frame)

    def close(self):
        sys.stdout.write(RESET + "\033[?25h\033[2J\033[H")
        sys.stdout.flush()


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    screen = Screen()

    state = None
    last_fetch = 0.0
    stale = False
    last_prayer = None      # which prayer we were counting down to
    alert = None
    alert_until = 0.0
    nagbars = []

    try:
        while True:
            if time.time() - last_fetch >= FETCH_EVERY:
                try:
                    state, stale = fetch(HOST), False
                except (urllib.error.URLError, OSError, ValueError):
                    stale = True     # tasks go stale; clock and prayer do not
                last_fetch = time.time()

            view = local_view() or (server_view(state) if state else None)
            if view is None:
                screen.draw([fg(DIM) + f"  connecting to {HOST} …" + RESET])
                time.sleep(FRAME_EVERY)
                continue
            now, prayer_state = view

            # The countdown rolling over to the next prayer is the edge:
            # whatever it was counting down to has just come in. Driven by
            # whichever clock is in use, so the chime still fires offline.
            if last_prayer is not None and prayer_state["next"] != last_prayer:
                alert = f"{last_prayer.upper()} — time to pray"
                alert_until = time.monotonic() + ALERT_SECONDS
                chime()
                bar_proc = notify(alert)
                if bar_proc:
                    nagbars.append(bar_proc)
            last_prayer = prayer_state["next"]

            if alert and time.monotonic() > alert_until:
                alert = None
            for entry in list(nagbars):
                if time.monotonic() > entry[1]:
                    entry[0].terminate()
                    nagbars.remove(entry)

            size = terminal_size()
            dx, dy = drift()
            frame = compose(
                state or OFFLINE, stale, size.columns - dx, size.lines - dy,
                now, prayer_state, alert,
            )
            frame = [""] * dy + [(" " * dx + line if line else "") for line in frame]

            screen.draw(frame)
            time.sleep(FRAME_EVERY)
    finally:
        for entry in nagbars:
            entry[0].terminate()
        screen.close()


if __name__ == "__main__":
    main()
