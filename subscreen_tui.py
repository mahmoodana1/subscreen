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
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
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

# Colour bindings — populated by _apply_theme() at import time and rebound
# whenever the user asks for a different theme. The default names live in
# THEMES below; "grove" is the original rain-soaked green.
LIME = "#4e8570"
PALE = "#79b39c"
WHITE = "#ffffff"
TEXT = "#e6e6e6"
DIM = "#7a7a7a"
FAINT = "#151515"
OFFLINE_DOT = "#c25b5b"
ROW_HOT = "#131313"
ROW_COOL = "#0b0b0b"
EDGE_COOL = "#2f2f2f"
LINE = "#9a9a9a"
LINE_SOFT = "#454545"
SHADOW_MIX = 0.58
SPARK_HI = "#a3dcc4"
SPARK_EMPTY = "#3f3f3f"
ALERT = "#ff6b6b"
INK = "#000000"
COOL = LIME
WARM = PALE
GREEN = LIME

# Set by _apply_theme(): the current theme's dict, its name, and its
# optional ambient-animation callable. AMBIENT signature is
#     f(width, height, mono) -> list[list[Optional[(fg_hex, char)]]]
# One list per terminal row, one cell per column. Compositing lives in
# paint_row() further down.
AMBIENT = None
THEME = None
THEME_NAME = "grove"
THEME_FILE = os.path.expanduser("~/.cache/subscreen/theme")

# What the panel shows before the laptop has ever answered: the clock and
# prayer still work, there is simply nothing to say about tasks.
OFFLINE = {"tasks": [], "open_tasks": 0, "goal": {}, "task_error": None, "accent": None}

NAG_SECONDS = 90      # how long the i3-nagbar stays up
ALERT_SECONDS = 120   # how long the in-screen banner stays up
CHIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chime.wav")
ATHAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athan.mp3")
ATHAN_RAMP_SECONDS = 7.0   # fade 0 → ATHAN_VOLUME over this window, then hold
# The loudness knob. Applied inside the filter chain, ahead of the
# limiter — mpv's own --volume runs at the output stage, after --af, so
# gain applied there would sail straight past the limiter and clip at the
# DAC. mpv's volume is left at unity and only drives the fade-in.
#
# 400 drives the limiter hard on purpose. Past roughly 250 the extra gain
# stops raising the peaks — they are already at the ceiling — and instead
# squashes the gaps between phrases upward, which is what reads as louder.
# Measured mean level on athan.mp3: 170 gave -10.2 dB, 400 gives -9.5 dB,
# and nothing in software got past about -9.2 dB without clipping.
ATHAN_GAIN_PERCENT = 400
# Deliberate overdrive past the limiter, chosen by ear over a cleaner
# signal. mpv applies this after the chain, so the limiter's output gets
# multiplied into the ceiling and the peaks flat-top — that is the point:
# the clipped version is the one that sounded right. Measured mean level
# at 150: -6.5 dB, against -9.5 dB limited-only. Lower this first if it
# ever starts sounding harsh rather than loud; the chain above stays
# clean, so 100 here always gets the undistorted version back.
ATHAN_VOLUME = 150
ATHAN_VOLUME_MAX = 150
# Catch the peaks this gain pushes over full scale. Without it the output
# stage flat-tops them into square waves: measured on athan.mp3, raw gain
# alone put ~1M samples hard against the ceiling and raised the mean level
# to -8.1 dB — roughly 13x the average power through the voice coil, five
# times a day. The limiter gives up ~2 dB of that for smooth gain
# reduction instead of clipping. The slow release below keeps it from
# pumping the level back up between syllables.
ATHAN_LIMIT = 0.99
ATHAN_LIMIT_RELEASE = 200
# athan.mp3 has no trailing silence — the muezzin's last phrase runs to the
# final sample. PulseAudio drops whatever is still in its buffer when mpv
# exits at EOF, so the ending gets clipped. Pad the stream with silence so
# playback outlives the buffer and the recording is heard to the end. A
# mouse nudge still kills mpv during the pad, so the extra seconds cost
# nothing but a silent process nobody is listening to.
ATHAN_TAIL_PAD_SECONDS = 30

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

def trans(n):
    return f"\033[{n}C" if n > 0 else ""


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


# ---------------------------------------------------------------------------
# Ambient animations. Every motion is driven from sin/cos of monotonic time,
# so there is no per-frame state and 10fps yields buttery motion. Particles
# have deterministic seeds; the same seed at the same time yields the same
# pixel — which is what lets us diff the frame cheaply.
# ---------------------------------------------------------------------------


def _hash(seed):
    """Deterministic 32-bit scramble for particle seeds."""
    h = (seed * 2654435761) & 0xFFFFFFFF
    h ^= (h >> 16)
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= (h >> 13)
    return h


def _empty_grid(width, height):
    return [[None] * width for _ in range(height)]


def _scene_geometry(theme, height):
    """Return (scene_rows, strip_top, pw, ph) for a bottom-strip scene.

    Scenes render only in the bottom `theme["scene_rows"]` terminal rows.
    ph is the pixel-buffer height, 2× because we render two stacked pixels
    per cell via half-block glyphs.
    """
    scene_rows = max(4, min(theme.get("scene_rows", 7), height))
    strip_top = height - scene_rows
    return scene_rows, strip_top, scene_rows * 2


def _clock_colon_cell(width, height):
    """Approximate the terminal cell at the centre of the clock's ':' glyph.

    Duplicates compose()'s clock-sizing loop so a theme's colon-creature
    sprite lands on the right spot without compose having to publish its
    layout. Returns (cell_col, cell_row, scale_x, scale_y). If width or
    height are too small for any clock, returns None.
    """
    rows_for_clock = height - 6
    if rows_for_clock < GLYPH_ROWS or width < 12:
        return None
    clock_text = "00:00"
    scale_x, scale_y = 1, 1
    for tall in range(THEME.get("clock_max_tall", 6), 0, -1):
        wide = tall * 2
        off_x, off_y = shadow_offset(wide, tall)
        while wide > 1 and clock_width(clock_text, wide) + off_x > width - 2:
            wide -= 1
            off_x = shadow_offset(wide, tall)[0]
        if GLYPH_ROWS * tall + off_y <= rows_for_clock:
            scale_x, scale_y = wide, tall
            break
    gap = glyph_gap(scale_x)
    block = clock_width(clock_text, scale_x) + shadow_offset(scale_x, scale_y)[0]
    pad = max(0, (width - block) // 2)
    # colon lives after two "0" glyphs (3 cells wide each) + two gaps
    colon_left = pad + 2 * (3 * scale_x + gap)
    colon_cx = colon_left + scale_x // 2
    # compose() centres the clock vertically via slack padding; estimate
    # the top offset based on a typical body length (clock rows + 6)
    clock_rows = GLYPH_ROWS * scale_y + shadow_offset(scale_x, scale_y)[1]
    body_len_est = clock_rows + 6
    top_slack = max(0, (height - body_len_est - 1)) // 2
    colon_cy = top_slack + GLYPH_ROWS * scale_y // 2
    return colon_cx, colon_cy, scale_x, scale_y


# A tiny 3x3 pixel creature that sits between the two dots of the clock's
# ':' glyph. Every theme instantiates it with its own colon_a/b/c palette
# so the same silhouette reads as a cat, an owl, a bug, a bird, etc.
COLON_CREATURE = (
    "a.a",     # ears / feather tufts / spikes
    "bCb",     # eyes flanking a face / body
    ".a.",     # tail flick / mouth / lower detail
)


def _place_colon_sprite(pixels, pw, ph, width, height, sprite, cmap):
    """Drop a tiny pixel sprite where the clock's colon renders.

    sprite: iterable of row-strings; '.' or ' ' is transparent, other
    chars index into cmap. The sprite is centred horizontally at the
    colon and vertically at its middle empty row (so it sits between
    the two colon dots).
    """
    metrics = _clock_colon_cell(width, height)
    if metrics is None:
        return
    colon_cx, colon_cy, _, _ = metrics
    sprite_h = len(sprite)
    sprite_w = max(len(row) for row in sprite)
    # cell (col, row) → pixel (col, row * 2). Middle of the cell is at
    # row * 2 + 1. Centre the sprite vertically on the colon cell's midline.
    py_center = colon_cy * 2 + 1
    py_top = py_center - sprite_h // 2
    px_left = colon_cx - sprite_w // 2
    for r, row_str in enumerate(sprite):
        for c, ch in enumerate(row_str):
            if ch in (".", " "):
                continue
            px = px_left + c
            py = py_top + r
            if 0 <= px < pw and 0 <= py < ph and ch in cmap:
                pixels[py][px] = cmap[ch]


def _place_colon_on_grid(grid, width, height, sprite, cmap):
    """Grid-cell variant of _place_colon_sprite for char-based ambients.

    Used by themes that draw glyphs directly (circuitry, murmuration).
    Each character in the sprite (that isn't '.' or ' ') is written as
    a (fg_colour, char) cell at the corresponding position around the
    clock's colon.
    """
    metrics = _clock_colon_cell(width, height)
    if metrics is None:
        return
    colon_cx, colon_cy, _, _ = metrics
    sprite_h = len(sprite)
    sprite_w = max(len(row) for row in sprite)
    row_top = colon_cy - sprite_h // 2
    col_left = colon_cx - sprite_w // 2
    for r, row_str in enumerate(sprite):
        for c, ch in enumerate(row_str):
            if ch in (".", " "):
                continue
            x = col_left + c
            y = row_top + r
            if 0 <= x < width and 0 <= y < height:
                colour = cmap.get(ch)
                if colour:
                    grid[y][x] = (colour, ch)


def _blit_pixels(pixels, grid, strip_top, scene_rows, height, pw, ph):
    """Composite a strip pixel buffer into terminal cells using ▀/▄/█.

    Half-block glyphs carry two stacked pixels per cell (fg=top,
    bg=bottom); when both halves match we collapse to █ for cleanliness.
    That is what makes tiny sprites read as pixel art rather than ASCII.
    """
    for y_local in range(scene_rows):
        y = strip_top + y_local
        if y < 0 or y >= height:
            continue
        row = grid[y]
        y_top_p = y_local * 2
        y_bot_p = y_local * 2 + 1
        for x in range(pw):
            top = pixels[y_top_p][x] if y_top_p < ph else None
            bot = pixels[y_bot_p][x] if y_bot_p < ph else None
            if top is None and bot is None:
                continue
            if top == bot:
                row[x] = (top, "█", None)
            elif bot is None:
                row[x] = (top, "▀", None)
            elif top is None:
                row[x] = (bot, "▄", None)
            else:
                row[x] = (top, "▀", bot)


def abyss_ambient(width, height, mono):
    """A living reef — full-panel pixel-art ocean ecosystem.

    Multi-species scene inspired by pixel-art platformer sea biomes.
    Half-block glyphs render two stacked pixels per cell so sprites read
    as pixel art rather than ASCII.

    Life, front to back:
      * god-rays angling from an unseen sun at the very top
      * marine snow drifting slowly down through the whole water column
      * two jellyfish drifting with pulsing bell sprites and swaying
        tentacles
      * three fish schools traversing in loose V-formations at their own
        depths and speeds, in two colour variants
      * one lone medium fish gliding solo on its own path
      * a rare sea turtle paddling across every ~35 s
      * a sonar ping every ~14 s expanding from a floor rock
      * a kelp forest rising 8-13 pixels from the floor, each frond
        bending on its own sine
      * FIVE coral clusters (fan, round, table, brain, branching) with
        distinct colours seeded per position
      * two sea anemones with waving tentacles that flex on their own
        phase
      * two bubble vents in the coral sending streams to the surface
      * a sandy floor with scattered rocks
    """
    theme = THEME
    grid = _empty_grid(width, height)
    pw = width
    ph = height * 2
    if pw <= 0 or ph <= 0:
        return grid
    pixels = [[None] * pw for _ in range(ph)]

    water_light = theme["water_light"]

    # ---- god-rays across the top ----
    ray_max = min(6, ph)
    for x in range(pw):
        r = math.sin(x * 0.14 + mono * 0.45) + math.sin(x * 0.07 - mono * 0.25 + 1.3)
        r = (r + 2) * 0.25
        if r < 0.55:
            continue
        depth = int(1 + r * ray_max)
        for y in range(depth):
            if _hash(x * 41 + y * 17) % 100 < 55:
                pixels[y][x] = water_light

    # ---- sandy floor + rocks ----
    floor_y = max(0, ph - 2)
    sand = theme["sand"]
    sand_dark = theme["sand_dark"]
    for y in range(floor_y, ph):
        for x in range(pw):
            r = _hash(x * 13 + y * 41) % 100
            pixels[y][x] = sand if r > 25 else sand_dark
    rock = theme["rock"]
    for i in range(max(4, pw // 16)):
        h = _hash(i * 397 + 5)
        rx = h % pw
        for dx in range(1 + ((h >> 3) & 0x1) + 1):
            px = rx + dx
            if 0 <= px < pw:
                pixels[floor_y][px] = rock
        # elevated rock — bounds check the shifted x too so wide terminals
        # can't land it on column pw
        if (h & 0x10) and floor_y - 1 >= 0:
            rx2 = rx + ((h >> 5) & 0x1)
            if 0 <= rx2 < pw:
                pixels[floor_y - 1][rx2] = rock

    # ---- coral clusters — five distinct sprites at fixed fractions ----
    # Each sprite: string of rows. 'p' = pink, 'o' = orange, 'u' = purple,
    # 'm' = magenta. Colours resolve through the theme.
    CORAL_PALETTE = {
        "p": theme["coral_pink"],
        "o": theme["coral_orange"],
        "u": theme["coral_purple"],
        "m": theme["coral_magenta"],
    }
    CORALS = (
        # (x_frac, sprite lines) — sprites rest with their bottom row on floor
        (0.08, ("p.p.p.p",
                "pppppp.",
                ".pppp..",
                "..||...")),                    # fan coral, pink
        (0.22, (".ooo.",
                "ooooo",
                ".ooo.")),                       # brain coral, orange
        (0.36, ("uuuuuuu",
                "...u...",
                "...u...")),                     # table coral, purple
        (0.56, (".m.m.",
                "mmmm.",
                ".m.m.",
                "mmmm.")),                       # branching coral, magenta
        (0.82, ("p.p.p",
                "ppppp",
                ".ppp.",
                "..|..")),                       # fan coral variant
    )
    for base_frac, sprite in CORALS:
        cx = int(base_frac * pw)
        c_h = len(sprite)
        y_bot = floor_y - 1
        y_top_ = y_bot - c_h + 1
        for r, row_str in enumerate(sprite):
            for c, ch in enumerate(row_str):
                if ch not in CORAL_PALETTE:
                    continue
                px = cx + c
                py = y_top_ + r
                if 0 <= px < pw and 0 <= py < ph:
                    pixels[py][px] = CORAL_PALETTE[ch]

    # ---- two sea anemones with waving tentacles ----
    anem_body = theme["anemone_body"]
    anem_tent = theme["anemone_tent"]
    for i, ax_frac in enumerate((0.46, 0.72)):
        ax = int(ax_frac * pw)
        # body (3-wide dome on the floor)
        for dx in range(-1, 2):
            px = ax + dx
            if 0 <= px < pw and floor_y - 1 >= 0:
                pixels[floor_y - 1][px] = anem_body
        # 5 tentacles, each with its own sine phase
        for k in range(5):
            tx = ax + k - 2
            if not (0 <= tx < pw):
                continue
            wave = 2 + int((math.sin(mono * 2.0 + i * 0.9 + k * 0.7) + 1) * 1.4)
            for j in range(wave):
                py = floor_y - 2 - j
                if 0 <= py < ph:
                    pixels[py][tx] = anem_tent

    # ---- kelp forest — tall bending fronds ----
    kelp = theme["kelp"]
    kelp_light = theme["kelp_light"]
    kelp_count = max(4, pw // 20)
    kelp_positions = set()
    for k in range(kelp_count):
        base_x = int((k + 0.5) / kelp_count * pw)
        # nudge slightly by hash so they aren't perfectly evenly spaced
        h = _hash(k * 271 + 19)
        base_x = (base_x + (h % 5) - 2) % pw
        height_k = 8 + (h % 6)
        phase = ((h >> 4) & 0xFF) / 0xFF * 6.0
        for i in range(height_k):
            sway = int(math.sin(mono * 0.7 + phase + i * 0.28) * (i * 0.32))
            py = floor_y - 1 - i
            px = base_x + sway
            if 0 <= px < pw and 0 <= py < ph:
                pixels[py][px] = kelp_light if i > height_k * 0.7 else kelp
                kelp_positions.add((py, px))

    # ---- bubble vents streaming up from the coral ----
    bubble = theme["bubble"]
    for vent_x_frac, count, freq in ((0.60, 4, 3.5), (0.24, 3, 3.0)):
        vent_x = int(vent_x_frac * pw)
        for i in range(count):
            phase = i * 0.85
            y_raw = (ph - 2 - ((mono + phase) * freq)) % (ph - 2)
            x = vent_x + int(math.sin((mono + phase) * 1.1) * 1.5)
            y = int(y_raw)
            if 0 <= x < pw and 0 <= y < ph and pixels[y][x] is None:
                pixels[y][x] = bubble

    # ---- marine snow drifting down ----
    snow_col = theme["marine_snow"]
    for i in range(22):
        h = _hash(i * 313 + 47)
        phase = ((h & 0xFFFF) / 0xFFFF) * 20.0
        fall = 1.5 + ((h >> 4) & 0x7) * 0.20
        base_x = ((h >> 16) & 0xFFFF) / 0xFFFF
        wobble = math.sin((mono + phase) * 0.4) * 1.5
        y_raw = ((mono + phase) * fall) % (ph + 4) - 2
        x = int(base_x * pw + wobble) % pw
        y = int(y_raw)
        if 0 <= y < ph - 2 and pixels[y][x] is None:
            pixels[y][x] = snow_col

    # ---- three fish schools in V-formation ----
    school_offsets = ((0, 0), (3, 1), (3, -1), (6, 2), (6, -2))
    for si in range(3):
        h = _hash(si * 617 + 41)
        period = 18.0 + ((h & 0xF) % 8)
        st = mono % period
        life = period * 0.7
        if st > life:
            continue
        dir_ = 1 if (h & 1) else -1
        base_y = 6 + ((h >> 4) % max(1, ph - 18))
        frac = st / life
        head_x = int(frac * (pw + 16) - 8)
        if dir_ < 0:
            head_x = pw - head_x
        color = theme["fish_a"] if (h & 2) else theme["fish_b"]
        for off_x, off_y in school_offsets:
            wobble = math.sin(st * 2.5 + off_x * 0.4) * 0.9
            x = head_x + dir_ * off_x
            y = base_y + off_y + int(wobble)
            if not (0 <= x < pw and 0 <= y < ph):
                continue
            if pixels[y][x] is None:
                pixels[y][x] = color
            tx = x + dir_
            if 0 <= tx < pw and pixels[y][tx] is None:
                pixels[y][tx] = color

    # ---- lone medium fish gliding on its own path ----
    solo_period = 30.0
    solo_st = mono % solo_period
    solo_life = 22.0
    if solo_st < solo_life:
        solo_frac = solo_st / solo_life
        head_x = int(solo_frac * (pw + 10) - 5)
        base_y = 14 + int(math.sin(mono * 0.35) * 3)
        fish_color = theme["fish_medium"]
        fish_dark = theme["fish_medium_dark"]
        # 5-wide 3-tall fish sprite
        sprite = (
            ".oOO.",
            "oOOOo",
            ".oOO.",
        )
        cmap = {"o": fish_dark, "O": fish_color}
        for r, row_str in enumerate(sprite):
            for c, ch in enumerate(row_str):
                if ch == ".":
                    continue
                px = head_x + c
                py = base_y + r
                if 0 <= px < pw and 0 <= py < ph and pixels[py][px] is None:
                    pixels[py][px] = cmap[ch]

    # ---- jellyfish with pulsing bell sprite + swaying tentacles ----
    for j in range(2):
        h = _hash(j * 421 + 17)
        phase = ((h & 0xFFFF) / 0xFFFF) * 20.0
        base_x_frac = ((h >> 16) & 0xFFF) / 0xFFF
        base_y = 5 + ((h >> 4) & 0x3) * 4
        speed = 0.05 + ((h >> 8) & 0x3) * 0.015
        cx = int((base_x_frac + math.sin((mono + phase) * speed) * 0.35) * pw) % pw
        cy = base_y + int(math.sin((mono + phase) * 0.06) * 1.5)
        pulse = math.sin((mono + phase) * 1.2) * 0.5 + 0.5
        bell = theme["jelly_bright"] if pulse > 0.55 else theme["jelly_mid"]
        # bell sprite — 5 wide, 3 tall, rounded top
        bell_sprite = (
            ".BBB.",
            "BBBBB",
            "BBBBB",
        )
        for r, row_str in enumerate(bell_sprite):
            for c, ch in enumerate(row_str):
                if ch != "B":
                    continue
                px = (cx - 2 + c) % pw
                py = cy + r
                if 0 <= py < ph and pixels[py][px] is None:
                    pixels[py][px] = bell
        # tentacles: 4 rows, each with its own sway
        tent_tints = (theme["tentacle_bright"], theme["tentacle_bright"],
                      theme["tentacle_mid"], theme["tentacle_dim"])
        for r_off in range(4):
            py = cy + 3 + r_off
            if not (0 <= py < ph):
                continue
            sway = int(math.sin((mono + phase + r_off * 0.6) * 1.3) * 1.5)
            for dx in (-1, 0, 1):
                px = (cx + dx + sway) % pw
                if pixels[py][px] is None:
                    pixels[py][px] = tent_tints[r_off]

    # ---- occasional sea turtle drifting past ----
    TURTLE_PERIOD = 35.0
    TURTLE_LIFE = 15.0
    tt = mono % TURTLE_PERIOD
    if tt < TURTLE_LIFE:
        tth = _hash(int(mono // TURTLE_PERIOD) * 811 + 3)
        turtle_dir = 1 if (tth & 1) else -1
        turtle_y = 10 + (tth & 0xF) % max(1, ph - 22)
        frac = tt / TURTLE_LIFE
        head_x = int(frac * (pw + 14) - 7)
        if turtle_dir < 0:
            head_x = pw - head_x
        shell = theme["turtle_shell"]
        shell_d = theme["turtle_shell_dark"]
        flipper = theme["turtle_flipper"]
        flip = int(tt * 2) % 2
        # 7-wide 3-tall turtle, mirrored by direction; flippers alternate
        if turtle_dir > 0:
            top = "..SSSS>"
            mid = "SSSSSSs"
            bot = "F.SS.SF" if flip == 0 else ".FSSSF."
        else:
            top = "<SSSS.."
            mid = "sSSSSSS"
            bot = "F.SS.SF" if flip == 0 else ".FSSSF."
        sprite = (top, mid, bot)
        cmap = {"S": shell, "s": shell_d, "F": flipper, ">": shell_d, "<": shell_d}
        for r, row_str in enumerate(sprite):
            for c, ch in enumerate(row_str):
                if ch == ".":
                    continue
                px = head_x + c
                py = turtle_y + r
                if 0 <= px < pw and 0 <= py < ph:
                    pixels[py][px] = cmap.get(ch, shell)

    # ---- sonar ping every ~14 s ----
    PING_PERIOD = 14.0
    PING_LIFE = 3.5
    t_p = mono % PING_PERIOD
    if t_p < PING_LIFE:
        pi = int(mono // PING_PERIOD)
        h = _hash(pi * 991 + 3)
        cx = 4 + (h % max(1, pw - 8))
        cy = max(0, floor_y - 2)
        radius = t_p / PING_LIFE * min(pw * 0.5, ph * 1.3)
        opacity = 1.0 - t_p / PING_LIFE
        n = max(12, int(radius * 5))
        for j in range(n):
            ang = j / n * math.tau
            xr = int(cx + math.cos(ang) * radius)
            yr = int(cy + math.sin(ang) * radius * 0.5)
            if not (0 <= xr < pw and 0 <= yr < ph):
                continue
            if pixels[yr][xr] is not None:
                continue
            if opacity > 0.65:
                pixels[yr][xr] = theme["sonar_bright"]
            elif opacity > 0.32:
                pixels[yr][xr] = theme["sonar_mid"]

    # ---- tiny sea creature sitting in the clock's colon ----
    _place_colon_sprite(pixels, pw, ph, width, height, COLON_CREATURE, {
        "a": theme["colon_a"], "b": theme["colon_b"], "C": theme["colon_c"],
    })

    _blit_pixels(pixels, grid, 0, height, height, pw, ph)
    return grid


def aurora_ambient(width, height, mono):
    """A snowy pixel-art valley under a dancing aurora — full ecosystem.

    Inspired by winter biomes in pixel-art platformers. Half-block glyphs
    render each cell as two stacked pixels for near-square sprites.

    Living layers, top to bottom:
      * two-layer aurora curtains with hue drift and parallax across the
        upper sky
      * twinkling stars scattered through the mid-sky on independent
        phases
      * a shooting star arcing across every ~17 s
      * an owl flying silhouetted every ~20 s
      * snow drifts down through the whole sky
      * a distant mountain range with snow-capped tallest peaks
      * a nearer, sharper mountain ridge in front of it
      * a pine forest of varied sprite sizes along the tree line, with
        snow tips
      * a frozen lake at the bottom whose surface reflects a dim version
        of the aurora
    """
    theme = THEME
    grid = _empty_grid(width, height)
    pw = width
    ph = height * 2
    if pw <= 0 or ph <= 0:
        return grid
    pixels = [[None] * pw for _ in range(ph)]

    aa, ab, ac = theme["aurora_a"], theme["aurora_b"], theme["aurora_c"]
    ink = theme["ink"]

    # ---- vertical altitude bands (pixel rows) ----
    band_rows = max(6, ph * 5 // 16)        # aurora zone in the top ~5/16
    lake_top = ph - 5                        # frozen lake at very bottom
    pine_baseline = ph - 6                   # pines stand on this pixel row
    mtn_near_top = pine_baseline - 4         # near mountains rise from here
    mtn_far_top = mtn_near_top - 3           # far mountains behind

    # ---- aurora curtains (two layers, hue-drifting) ----
    for x in range(pw):
        front = (
            math.sin(x * 0.11 + mono * 0.30) * 0.5
            + math.sin(x * 0.19 - mono * 0.46) * 0.3
            + math.sin(x * 0.35 + mono * 0.66) * 0.2
        )
        front = (front + 1) * 0.5
        back = (
            math.sin(x * 0.055 + mono * 0.12) * 0.6
            + math.sin(x * 0.09 - mono * 0.18) * 0.4
        )
        back = (back + 1) * 0.5
        hue_f = math.sin(x * 0.04 + mono * 0.10) * 0.5 + 0.5
        hue_b = math.sin(x * 0.03 - mono * 0.07 + 1.3) * 0.5 + 0.5
        col_f = aa if hue_f < 0.34 else (ab if hue_f < 0.67 else ac)
        col_b = aa if hue_b < 0.34 else (ab if hue_b < 0.67 else ac)
        for y in range(band_rows):
            falloff = (1 - y / band_rows) ** 1.5
            fi = front * falloff
            bi = back * falloff * 0.55
            intensity, base = (fi, col_f) if fi >= bi else (bi, col_b)
            if intensity < 0.25:
                continue
            if pixels[y][x] is None:
                pixels[y][x] = mix(ink, base, min(1.0, intensity + 0.30))

    # ---- stars twinkling below the aurora, above the mountains ----
    star_top = band_rows
    star_bot = mtn_far_top
    star_span = max(1, star_bot - star_top)
    star_count = max(18, pw * star_span // 70)
    for i in range(star_count):
        h = _hash(i * 179 + 5)
        sx = h % pw
        sy = star_top + ((h >> 8) % star_span)
        twinkle = (
            math.sin(mono * 1.7 + i * 2.1) + math.sin(mono * 0.9 + i * 1.3)
        ) * 0.5 + 1.0
        twinkle *= 0.5
        if twinkle < 0.55 or pixels[sy][sx] is not None:
            continue
        pixels[sy][sx] = theme["star_bright"] if twinkle > 0.85 else theme["star_dim"]

    # ---- shooting star every ~17 s ----
    SHOOT_PERIOD = 17.0
    SHOOT_LIFE = 0.9
    shoot_t = mono % SHOOT_PERIOD
    if shoot_t < SHOOT_LIFE:
        si = int(mono // SHOOT_PERIOD)
        h = _hash(si * 313 + 11)
        start_x = h % pw
        start_y = 1 + ((h >> 8) % max(1, band_rows + star_span // 2))
        vx = pw * 1.4
        vy = 4.0
        for j in range(12):
            t_j = shoot_t - j * 0.05
            if t_j < 0:
                break
            x_j = int(start_x + vx * t_j) % pw
            y_j = int(start_y + vy * t_j)
            if not (0 <= y_j < mtn_far_top):
                continue
            if j == 0:
                pixels[y_j][x_j] = theme["shoot_head"]
            elif j < 3:
                pixels[y_j][x_j] = theme["shoot_trail"]
            else:
                pixels[y_j][x_j] = theme["shoot_faint"]

    # ---- owl silhouette flying every ~20 s ----
    OWL_PERIOD = 20.0
    OWL_LIFE = 4.5
    ot = mono % OWL_PERIOD
    if ot < OWL_LIFE:
        oh = _hash(int(mono // OWL_PERIOD) * 331 + 5)
        owl_dir = 1 if (oh & 1) else -1
        base_y = 3 + (oh & 0x7) % max(1, star_span - 2) + star_top // 2
        frac = ot / OWL_LIFE
        head_x = int(frac * (pw + 8) - 4)
        if owl_dir < 0:
            head_x = pw - head_x
        flap = int(ot * 5) % 2
        owl_col = theme["owl"]
        # tiny 3-pixel owl with two wing frames
        wings = "^_^" if flap == 0 else "v-v"
        for c, ch in enumerate(wings):
            if ch == " ":
                continue
            px = head_x + c
            py = base_y
            if 0 <= px < pw and 0 <= py < ph and pixels[py][px] is None:
                pixels[py][px] = owl_col

    # ---- far mountain range with snow caps on the tallest peaks ----
    m_far = theme["mountain_far"]
    snow = theme["snow"]
    for x in range(pw):
        h = _hash(x // 3 * 41 + 7)
        base = math.sin(x * 0.07) * 2.0 + math.sin(x * 0.13 + 1.1) * 1.2
        peak = int(2 + base + (h % 2))
        peak = max(1, peak)
        for i in range(peak):
            py = mtn_far_top + i
            if 0 <= py < ph:
                pixels[py][x] = m_far
        if peak >= 4 and 0 <= mtn_far_top < ph:
            pixels[mtn_far_top][x] = snow

    # ---- near mountain ridge — jagged, taller, with snow caps ----
    m_near = theme["mountain_near"]
    for x in range(pw):
        h = _hash(x * 47)
        h2 = _hash((x // 2) * 89 + 3)
        base = math.sin(x * 0.11 - 0.3) * 2.2 + math.sin(x * 0.19 + 0.7) * 1.4
        peak = int(3 + base + (h % 3) + (h2 % 2))
        peak = max(1, peak)
        for i in range(peak):
            py = mtn_near_top + i - 2
            if 0 <= py < ph:
                pixels[py][x] = m_near
        cap_py = mtn_near_top - 2
        if peak >= 4 and 0 <= cap_py < ph and pixels[cap_py][x] == m_near:
            pixels[cap_py][x] = snow

    # ---- pine forest along the tree line ----
    pine_col = theme["pine"]
    pine_snow = theme["pine_snow"]
    PINE_SMALL = (
        ".^.",
        "^^^",
        "^^^",
        ".|.",
    )
    PINE_MED = (
        "..^..",
        ".^^^.",
        ".^^^.",
        "^^^^^",
        "..|..",
    )
    PINE_LARGE = (
        "..^..",
        ".^^^.",
        ".^^^.",
        "^^^^^",
        "^^^^^",
        "..|..",
    )
    tx = 3
    while tx < pw - 3:
        h = _hash(tx * 71 + 3)
        gap = 5 + (h % 6)
        size = 1 + (h % 3)
        sprite = PINE_SMALL if size == 1 else (PINE_MED if size == 2 else PINE_LARGE)
        s_h = len(sprite)
        s_w = len(sprite[0])
        y_top_p = pine_baseline - s_h + 1
        for r, row_str in enumerate(sprite):
            for c, ch in enumerate(row_str):
                if ch == ".":
                    continue
                px = tx + c - s_w // 2
                py = y_top_p + r
                if not (0 <= px < pw and 0 <= py < ph):
                    continue
                # snow-tipped upper rows on some trees
                if ch != "|" and r < 2 and (h & 0x2):
                    pixels[py][px] = pine_snow
                else:
                    pixels[py][px] = pine_col
        tx += gap

    # ---- frozen lake at the bottom + aurora reflection ----
    ice = theme["ice"]
    ice_dark = theme["ice_dark"]
    for y in range(lake_top, ph):
        for x in range(pw):
            r = _hash(x * 13 + y * 7) % 100
            pixels[y][x] = ice if r > 30 else ice_dark
    # reflection: sample a matching x column of the aurora and echo it
    # dimly into the ice. Reflection intensity falls off with depth.
    for x in range(pw):
        for src_y, dst_y in ((2, lake_top), (3, lake_top + 1), (4, lake_top + 2)):
            if src_y >= ph or dst_y >= ph:
                continue
            src = pixels[src_y][x]
            if src is None or src in (ink, m_far, m_near, snow):
                continue
            r = _hash(x * 91 + dst_y * 17) % 100
            if r < 30:
                pixels[dst_y][x] = mix(ice, src, 0.30 - (dst_y - lake_top) * 0.08)

    # ---- snow falling continuously ----
    snow_col = snow
    for i in range(28):
        h = _hash(i * 419 + 31)
        phase = ((h & 0xFFFF) / 0xFFFF) * 20.0
        fall = 2.0 + ((h >> 4) & 0x7) * 0.22
        base_x = ((h >> 16) & 0xFFFF) / 0xFFFF
        wobble = math.sin((mono + phase) * 0.7) * 2.0
        y_raw = ((mono + phase) * fall) % (ph + 4) - 2
        x = int(base_x * pw + wobble) % pw
        y = int(y_raw)
        if 0 <= y < lake_top and pixels[y][x] is None:
            pixels[y][x] = snow_col

    # ---- perched owl in the clock's colon ----
    _place_colon_sprite(pixels, pw, ph, width, height, COLON_CREATURE, {
        "a": theme["colon_a"], "b": theme["colon_b"], "C": theme["colon_c"],
    })

    _blit_pixels(pixels, grid, 0, height, height, pw, ph)
    return grid


def circuitry_ambient(width, height, mono):
    """A full-panel PCB the UI sits inside — signals routing around it.

    Layout: two horizontal buses (one near the top edge, one near the
    bottom), joined by evenly spaced vertical traces that span the full
    height. Each trace carries a pulse with a fading tail; some go
    right, some left, verticals too. A small chip sits in the bottom-left
    (away from the clock) with its pins soldered into the local traces.
    Three LEDs at corner positions blink on independent loops. The
    resulting frame reads as the clock and tasks being mounted on the
    board rather than pasted on top of it.
    """
    theme = THEME
    grid = _empty_grid(width, height)
    if width <= 0 or height < 4:
        return grid

    trace = theme["trace"]
    node = theme["node"]
    top_bus = 1                              # near-top row
    bot_bus = height - 2                     # near-bottom row
    col_step = 10
    cols = list(range(4, width - 4, col_step))
    if not cols:
        return grid

    # horizontal buses
    x_lo, x_hi = cols[0], cols[-1]
    for x in range(x_lo, x_hi + 1):
        if 0 <= top_bus < height:
            grid[top_bus][x] = (trace, "─")
        if 0 <= bot_bus < height:
            grid[bot_bus][x] = (trace, "─")

    # verticals
    for c in cols:
        for y in range(top_bus, bot_bus + 1):
            if not (0 <= y < height):
                continue
            if y == top_bus or y == bot_bus:
                grid[y][c] = (node, "┼")
            else:
                grid[y][c] = (trace, "│")

    # pulses on the two horizontal buses
    for bi, r in enumerate((top_bus, bot_bus)):
        if not (0 <= r < height):
            continue
        h = _hash(bi * 251 + 17)
        speed = 8.0 + ((h >> 4) & 0x3) * 2.5
        direction = 1 if (h & 1) else -1
        offset = ((h >> 8) & 0xFF) / 0xFF * (x_hi - x_lo + 1)
        pos = (offset + direction * mono * speed) % (x_hi - x_lo + 1)
        for j in range(6):
            x_rel = (pos - direction * j) % (x_hi - x_lo + 1)
            x = x_lo + int(x_rel)
            if not (x_lo <= x <= x_hi):
                continue
            if j == 0:
                grid[r][x] = (theme["pulse_head"], "●")
            elif j < 2:
                grid[r][x] = (theme["pulse_mid"], "•")
            elif j < 4:
                grid[r][x] = (theme["pulse_mid"], "·")
            else:
                grid[r][x] = (theme["pulse_dim"], "·")

    # pulses on the vertical traces — slower because they're shorter
    v_span = bot_bus - top_bus
    if v_span > 0:
        for ci, c in enumerate(cols):
            h = _hash(ci * 337 + 41)
            speed = 2.2 + ((h >> 4) & 0x3) * 0.8
            direction = 1 if (h & 1) else -1
            offset = ((h >> 8) & 0xFF) / 0xFF * (v_span + 1)
            pos = (offset + direction * mono * speed) % (v_span + 1)
            for j in range(3):
                y_rel = (pos - direction * j) % (v_span + 1)
                y = top_bus + int(y_rel)
                if not (top_bus <= y <= bot_bus):
                    continue
                if j == 0:
                    grid[y][c] = (theme["pulse_head"], "●")
                elif j == 1:
                    grid[y][c] = (theme["pulse_mid"], "•")
                else:
                    grid[y][c] = (theme["pulse_dim"], "·")

    # LEDs at three fixed spots, blinking on independent cycles
    led_positions = []
    if len(cols) >= 2:
        led_positions.append((cols[0] - 2, top_bus))
        led_positions.append((cols[-1] + 2, top_bus))
        if bot_bus < height - 1:
            led_positions.append((cols[len(cols) // 2] + 4, bot_bus))
    for i, (lx, ly) in enumerate(led_positions):
        if not (0 <= lx < width and 0 <= ly < height):
            continue
        blink = math.sin(mono * (1.2 + i * 0.35) + i * 1.9) * 0.5 + 0.5
        on = blink > 0.55
        col = theme["led_ok"] if i == 1 else theme["led_on"]
        grid[ly][lx] = ((col if on else theme["led_off"]), "◆")

    # ---- tiny circuit-bug perched between the colon dots ----
    _place_colon_on_grid(grid, width, height, ("◆.◆", "●─●"), {
        "◆": theme["colon_a"], "●": theme["colon_b"], "─": theme["colon_c"],
    })

    return grid


def murmuration_ambient(width, height, mono):
    """Dusk sky wrapping the whole panel — flock swirls around the UI.

    Layers: a starfield across the whole sky (twinkling on independent
    phases), a 55-bird flock whose Lissajous centre wanders across the
    full panel with per-bird wobble + flock rotation + aspect breathing,
    and a two-tone distant hill silhouette anchoring the bottom.
    """
    theme = THEME
    grid = _empty_grid(width, height)
    if width <= 0 or height < 4:
        return grid
    strip_top = 0
    scene_rows = height

    sky_rows = height - 2                    # last 2 rows are hills
    hill_y_far = sky_rows
    hill_y_near = height - 1

    # far hills — soft, rolling
    m_far = theme["hill_far"]
    m_near = theme["hill_near"]
    for x in range(width):
        base_far = math.sin(x * 0.08 + 1.1) * 1.3 + math.sin(x * 0.14 + 0.3) * 0.7
        far_h = int(1 + max(0, base_far + (_hash(x * 41) % 2)))
        for i in range(far_h):
            py = hill_y_far + i
            if 0 <= py < height:
                grid[py][x] = (m_far, "█")
        # near hills override in front
        base_near = math.sin(x * 0.11 - 0.4) * 1.5 + math.sin(x * 0.20 + 1.1) * 1.0
        near_h = int(1 + max(0, base_near + (_hash(x * 53 + 7) % 2)))
        for i in range(near_h):
            py = hill_y_near - i
            if 0 <= py < height:
                grid[py][x] = (m_near, "█")

    # stars twinkling in the sky rows
    star_count = max(6, width * sky_rows // 90)
    for i in range(star_count):
        h = _hash(i * 179 + 5)
        sx = h % width
        sy_offset = (h >> 8) % max(1, sky_rows)
        sy = strip_top + sy_offset
        if not (strip_top <= sy < hill_y_far):
            continue
        twinkle = (math.sin(mono * 1.6 + i * 2.1) + math.sin(mono * 0.9 + i * 1.3)) * 0.5
        twinkle = (twinkle + 1) * 0.5
        if twinkle < 0.55:
            continue
        if grid[sy][sx] is not None:
            continue
        grid[sy][sx] = (theme["star_bright"] if twinkle > 0.85 else theme["star_dim"], "·")

    # flock: 55 birds swirling around a Lissajous centre confined to the
    # sky part of the strip
    flock_cx = (
        width * 0.5
        + math.sin(mono * 0.13) * width * 0.30
        + math.sin(mono * 0.08 + 1.7) * width * 0.12
    )
    sky_center = strip_top + sky_rows * 0.5
    flock_cy = (
        sky_center
        + math.sin(mono * 0.17 + 0.9) * sky_rows * 0.32
    )
    rot = mono * 0.4
    stretch = 1 + math.sin(mono * 0.31) * 0.45
    for i in range(55):
        h = _hash(i * 271 + 7)
        a = ((h & 0xFFF) / 0xFFF) * math.tau
        r = math.sqrt(((h >> 12) & 0xFFF) / 0xFFF)
        wobble_a = math.sin(mono * (0.5 + ((h >> 8) & 0x7) * 0.15) + i * 0.5) * 0.45
        wobble_r = math.sin(mono * 1.1 + i * 0.7) * 0.20
        ang = a + rot + wobble_a
        rr = r + wobble_r
        px = math.cos(ang) * rr * width * 0.18 * stretch
        py = math.sin(ang) * rr * sky_rows * 0.40 / stretch
        x = int(flock_cx + px) % width
        y = int(flock_cy + py)
        if not (strip_top <= y < hill_y_far):
            continue
        if grid[y][x] is not None:
            continue
        flick = math.sin(mono * 2.5 + i * 1.3) * 0.5 + 0.5
        if flick > 0.75:
            grid[y][x] = (theme["bird_bright"], "•")
        elif flick > 0.4:
            grid[y][x] = (theme["bird_mid"], "·")
        else:
            grid[y][x] = (theme["bird_dim"], "·")

    # ---- perched bird between the colon dots ----
    _place_colon_on_grid(grid, width, height, ("‹.›", "•─•"), {
        "‹": theme["colon_a"], "›": theme["colon_a"],
        "•": theme["colon_c"], "─": theme["colon_b"],
    })
    return grid


def forest_ambient(width, height, mono):
    """A framed clearing — the UI lives in the middle of a forest.

    Dense pixel-art frames the panel on three sides so the scene wraps
    the UI rather than sitting under it:
      * canopy hangs from the top edge
      * tree silhouettes stand on the LEFT and RIGHT columns
      * grass, soil, and a pond fill the bottom
    The middle is deliberately kept clear — only sparse butterflies and
    the occasional falling leaf drift through, so the clock and tasks
    read as if in a clearing rather than being pasted onto a picture.
    Living elements at the bottom: a rabbit hopping on a 4-frame arc,
    an occasional bird crossing the sky, a fish leaping from the pond.
    """
    theme = THEME
    grid = _empty_grid(width, height)
    pw = width
    ph = height * 2
    if pw <= 0 or ph <= 0:
        return grid
    pixels = [[None] * pw for _ in range(ph)]
    strip_top = 0
    scene_rows = height

    leaf_dark = theme["hill_far"]           # reused as canopy shadow
    canopy = theme["tree_silhouette_lit"]
    canopy_mid = theme["hill_near"]

    # ---- CANOPY along the top edge ----
    # Two pixel rows of dense hanging leaves with rounded lower silhouette
    for x in range(pw):
        base = math.sin(x * 0.18) * 1.2 + math.sin(x * 0.34 + 1.5) * 0.8
        depth = max(1, int(2 + base + (_hash(x * 31) % 3)))
        depth = min(depth, 4)
        for y in range(depth):
            r = _hash(x * 47 + y * 13) % 100
            if r < 40:
                pixels[y][x] = leaf_dark
            elif r < 85:
                pixels[y][x] = canopy_mid
            else:
                pixels[y][x] = canopy

    # ---- SIDE TREES on the LEFT and RIGHT columns ----
    # Two vertical bands of trees framing the panel. Each band is ~5
    # pixel-columns wide (a bit over 2 terminal columns) so the UI still
    # has plenty of horizontal room in the middle.
    frame_w = 5
    ground_start = ph - 6
    tree = theme["tree_silhouette"]
    tree_top_lit = theme["tree_silhouette_lit"]
    for side_left in (True, False):
        for y in range(4, ground_start):
            for dx in range(frame_w):
                x = dx if side_left else pw - 1 - dx
                if not (0 <= x < pw):
                    continue
                # trunk-like column of dark leaves; density falls at the
                # inner edge so the frame reads as a soft mask, not a wall
                edge_factor = 1 - dx / max(1, frame_w - 1)
                r = _hash(x * 37 + y * 19) % 100
                if r < 55 * edge_factor + 15:
                    pixels[y][x] = tree
                elif r < 78 * edge_factor + 15:
                    pixels[y][x] = canopy_mid
        # a couple of highlighted tips on each side
        for i in range(3):
            h = _hash((i + (0 if side_left else 100)) * 71)
            ty = 5 + (h % max(1, ground_start - 8))
            tx = (h % 2) if side_left else pw - 1 - (h % 2)
            if 0 <= tx < pw and 0 <= ty < ph:
                pixels[ty][tx] = tree_top_lit

    # ---- GROUND: grass + soil + pond, spanning the full width ----
    grass_top_row = ground_start
    grass = theme["grass_top"]
    grass_d = theme["grass_dark"]
    soil = theme["soil"]
    soil_d = theme["soil_dark"]
    for y in range(grass_top_row, min(ph, grass_top_row + 2)):
        for x in range(pw):
            r = _hash(x * 17 + y * 13) % 100
            if y == grass_top_row:
                pixels[y][x] = grass if r > 45 else grass_d
            else:
                pixels[y][x] = grass_d if r > 30 else grass
    soil_top_row = min(ph, grass_top_row + 2)
    for y in range(soil_top_row, ph):
        for x in range(pw):
            r = _hash(x * 13 + y * 7) % 100
            pixels[y][x] = soil if r > 25 else soil_d

    # swaying tufts poke up from the grass line
    for x in range(frame_w, pw - frame_w, 2):
        h = _hash(x * 41)
        if h % 3 != 0:
            continue
        tuft = 1 + (h % 2)
        sway = int(math.sin(mono * 1.6 + x * 0.35) * 1.3)
        for i in range(tuft):
            py = grass_top_row - 1 - i
            px = x + (0 if i == 0 else sway)
            if 0 <= px < pw and 0 <= py < ph and pixels[py][px] is None:
                pixels[py][px] = grass if i < tuft - 1 else grass_d

    # ---- POND in the right third of the ground ----
    pond_x_start = pw * 2 // 3
    pond_y_top = grass_top_row + 1
    water = theme["water"]
    water_light = theme["water_light"]
    water_dark = theme["water_dark"]
    for y in range(pond_y_top, ph):
        for x in range(pond_x_start, pw - frame_w):
            pixels[y][x] = water
    for x in range(pond_x_start, pw - frame_w):
        r0 = math.sin(x * 0.45 + mono * 2.0)
        r1 = math.sin(x * 0.32 - mono * 1.4 + 1.7)
        if pond_y_top < ph:
            if r0 > 0.68:
                pixels[pond_y_top][x] = water_light
            elif r0 < -0.72:
                pixels[pond_y_top][x] = water_dark
        if pond_y_top + 1 < ph and r1 > 0.80:
            pixels[pond_y_top + 1][x] = water_light

    # ---- RABBIT hopping along the grass, staying left of the pond ----
    speed = 7.0
    hop_freq = 1.35
    rabbit_lo = frame_w
    rabbit_hi = pond_x_start - 3
    span = max(4, rabbit_hi - rabbit_lo)
    x_base = rabbit_lo + int(mono * speed) % (span + 8) - 4
    hop_t = (mono * hop_freq) % 1.0
    hop_h = math.sin(hop_t * math.pi) * 3.5
    frame = 0 if hop_t < 0.15 else 1 if hop_t < 0.5 else 2 if hop_t < 0.85 else 3
    RABBIT = (
        (".EE..", ".XXE.", "XXXX.", "XX.XX"),
        (".EE..", "XXXE.", ".XXX.", "..X.."),
        ("EE...", "XXXXE", ".XXX.", "....."),
        (".EE..", "XXXX.", "XXXXX", "X...X"),
    )
    sprite = RABBIT[frame]
    cmap = {"E": theme["rabbit_ear"], "X": theme["rabbit_body"], "F": theme["rabbit_foot"]}
    y_bot = grass_top_row - 1 - int(hop_h)
    y_top_ = y_bot - (len(sprite) - 1)
    for r, row_str in enumerate(sprite):
        for c, ch in enumerate(row_str):
            if ch == ".":
                continue
            px = x_base + c
            py = y_top_ + r
            if 0 <= px < pw and 0 <= py < ph:
                pixels[py][px] = cmap[ch]

    # ---- BUTTERFLIES drifting through the clearing (mid area only) ----
    # Constrain vertical range to below the canopy and above the grass so
    # they never sit on top of it — they belong in the open air.
    clearing_top = 5
    clearing_bot = grass_top_row - 3
    if clearing_bot > clearing_top:
        for i in range(2):
            h = _hash(i * 517 + 89)
            phase = ((h & 0xFFFF) / 0xFFFF) * 20.0
            base_x_frac = ((h >> 16) & 0xFF) / 0xFF
            base_y_frac = ((h >> 4) & 0xFF) / 0xFF
            cx = int(frame_w + base_x_frac * (pw - 2 * frame_w)
                     + math.sin((mono + phase) * 0.31) * (pw - 2 * frame_w) * 0.30
                     + math.sin((mono + phase) * 0.71) * 3)
            cx = max(frame_w, min(pw - frame_w - 3, cx))
            cy = int(clearing_top + base_y_frac * (clearing_bot - clearing_top)
                     + math.sin((mono + phase) * 0.55) * 2)
            cy = max(clearing_top, min(clearing_bot, cy))
            flap = int((mono + phase) * 7) % 2
            wing = theme["butterfly_wing"] if i % 2 == 0 else theme["butterfly_wing_alt"]
            body = theme["butterfly_body"]
            b_sprite = ("W.W", ".B.") if flap == 0 else ("...", "WBW")
            bmap = {"W": wing, "B": body}
            for r, row_str in enumerate(b_sprite):
                for c, ch in enumerate(row_str):
                    if ch == ".":
                        continue
                    px = cx + c - 1
                    py = cy + r
                    if 0 <= px < pw and 0 <= py < ph and pixels[py][px] is None:
                        pixels[py][px] = bmap[ch]

    # ---- BIRD crossing the sky every ~15 s (top area, above the UI) ----
    BIRD_PERIOD = 15.0
    BIRD_LIFE = 3.5
    bt = mono % BIRD_PERIOD
    if bt < BIRD_LIFE:
        bp = int(mono // BIRD_PERIOD)
        h = _hash(bp * 373 + 11)
        bird_row = 4 + ((h & 0x3) % 2)      # just below the canopy
        left_to_right = (h & 0x10) != 0
        frac = bt / BIRD_LIFE
        if not left_to_right:
            frac = 1.0 - frac
        bx = int(frac * (pw + 6)) - 3
        flap = int(bt * 6) % 2
        wings = "vᴧv" if flap == 0 else "‾ᴧ‾"
        bird_col = theme["bird"]
        for c, ch in enumerate(wings):
            px = bx + c - 1
            py = bird_row
            if 0 <= px < pw and 0 <= py < ph and pixels[py][px] is None:
                pixels[py][px] = bird_col

    # ---- FISH leap from the pond every ~9 s ----
    JUMP_PERIOD = 9.0
    JUMP_LIFE = 1.2
    t_j = mono % JUMP_PERIOD
    if t_j < JUMP_LIFE:
        pj = int(mono // JUMP_PERIOD)
        h = _hash(pj * 373 + 19)
        fx = pond_x_start + 2 + (h % max(1, (pw - frame_w) - pond_x_start - 4))
        jp = t_j / JUMP_LIFE
        h_arc = math.sin(jp * math.pi) * 4.5
        fy = pond_y_top - int(h_arc)
        fish_col = theme["fish"]
        fish_shadow = theme["fish_shadow"]
        sprite_f = "FFf" if jp < 0.5 else "fFF"
        fmap = {"F": fish_col, "f": fish_shadow}
        for c, ch in enumerate(sprite_f):
            px = fx + c
            py = fy
            if 0 <= px < pw and 0 <= py < ph and pixels[py][px] is None:
                pixels[py][px] = fmap[ch]
        if (jp < 0.10 or jp > 0.90) and pond_y_top < ph:
            for dx in (-2, 2):
                px = fx + 1 + dx
                if 0 <= px < pw and pixels[pond_y_top][px] is not None:
                    pixels[pond_y_top][px] = water_light

    # ---- rusty squirrel between the colon dots ----
    _place_colon_sprite(pixels, pw, ph, width, height, COLON_CREATURE, {
        "a": theme["colon_a"], "b": theme["colon_b"], "C": theme["colon_c"],
    })

    _blit_pixels(pixels, grid, strip_top, scene_rows, height, pw, ph)
    return grid


def sunrise_ambient(width, height, mono):
    """A dreamy pink dawn — a solid gradient sky, a rising sun, and a cat.

    Every pixel above the horizon is FILLED with a colour from a 5-stop
    gradient (lavender → rose → peach → apricot → gold) so there are no
    ink gaps. Every pixel below the horizon is FILLED as land. Only
    intentional pixel-art elements (sun, clouds, cat) sit on top.

    Elements:
      * solid sky gradient, 5-stop, every column identical → clean bands
      * a soft glow ring around the sun
      * a filled half-disc rising sun with a subtle pulse
      * two drifting cloud sprites in soft rose + shadow tones
      * a rolling silhouette land at the bottom
      * a 5×6 pixel-art white cat sitting in the clock's colon (the star)
    """
    theme = THEME
    grid = _empty_grid(width, height)
    pw = width
    ph = height * 2
    if pw <= 0 or ph <= 0:
        return grid
    pixels = [[None] * pw for _ in range(ph)]

    sky_top = theme["sky_top"]
    sky_high = theme["sky_high"]
    sky_mid = theme["sky_mid"]
    sky_low = theme["sky_low"]
    sky_horizon = theme["sky_horizon"]
    horizon_row = ph * 5 // 7

    # ---- SOLID FIVE-STOP SKY GRADIENT — no dither, no gaps ----
    # Precompute one colour per pixel row; every column in that row gets
    # the same colour so the band reads as a clean gradient stripe.
    for y in range(horizon_row):
        t = y / max(1, horizon_row - 1)
        if t < 0.25:
            colour = mix(sky_top, sky_high, t / 0.25)
        elif t < 0.50:
            colour = mix(sky_high, sky_mid, (t - 0.25) / 0.25)
        elif t < 0.75:
            colour = mix(sky_mid, sky_low, (t - 0.50) / 0.25)
        else:
            colour = mix(sky_low, sky_horizon, (t - 0.75) / 0.25)
        for x in range(pw):
            pixels[y][x] = colour

    # ---- SUN: two-tone glow + filled disc ----
    sun_cx = pw // 2
    sun_cy = horizon_row - 1
    sun_r = max(5, min(pw // 8, horizon_row // 3))
    sun_body = theme["sun_body"]
    sun_glow_inner = theme["sun_glow_inner"]
    sun_glow_outer = theme["sun_glow_outer"]
    pulse = math.sin(mono * 0.6) * 0.5 + 0.5
    outer_r2 = 1.35 + pulse * 0.20
    for dy in range(-sun_r - 2, sun_r + 2):
        y = sun_cy + dy
        if not (0 <= y < ph):
            continue
        for dx in range(-sun_r * 2 - 2, sun_r * 2 + 3):
            x = sun_cx + dx
            if not (0 <= x < pw):
                continue
            # aspect-corrected: 2:1 pixel ratio makes a circle look wider
            nx = dx / max(1, sun_r * 2)
            ny = dy / max(1, sun_r)
            r2 = nx * nx + ny * ny
            if r2 < 0.75:
                pixels[y][x] = sun_body
            elif r2 < 1.0:
                pixels[y][x] = sun_glow_inner
            elif r2 < outer_r2:
                pixels[y][x] = sun_glow_outer

    # ---- clouds — 9-wide two-tone sprite, no dither ----
    cloud_light = theme["cloud"]
    cloud_dark = theme["cloud_dark"]
    CLOUD = (
        ".LLLLL.",
        "LLLLLLL",
        "PPLLLPP",
    )
    for i in range(2):
        h = _hash(i * 311 + 17)
        speed = 0.6 + ((h & 0x3)) * 0.25
        phase = ((h >> 4) & 0xFFFF) / 0xFFFF * pw
        base_y = 3 + ((h >> 8) & 0xF) % max(1, horizon_row // 3)
        cloud_x = int((mono * speed + phase)) % (pw + 14) - 7
        for r, row_str in enumerate(CLOUD):
            for c, ch in enumerate(row_str):
                if ch == ".":
                    continue
                px = cloud_x + c
                py = base_y + r
                if 0 <= px < pw and 0 <= py < ph:
                    pixels[py][px] = cloud_light if ch == "L" else cloud_dark

    # ---- solid rolling land silhouette below the horizon ----
    land = theme["land"]
    land_dark = theme["land_dark"]
    for y in range(horizon_row, ph):
        depth_t = (y - horizon_row) / max(1, ph - horizon_row - 1)
        colour = mix(land, land_dark, depth_t)
        for x in range(pw):
            pixels[y][x] = colour
    # gently rolling horizon: raise land in some columns
    for x in range(pw):
        bump = math.sin(x * 0.11) * 1.4 + math.sin(x * 0.18 + 1.2) * 0.8
        h = int(max(0, bump + 1))
        for j in range(h):
            py = horizon_row - 1 - j
            if 0 <= py < ph:
                pixels[py][x] = land

    # ---- horizon glow: a bright golden line where sun meets land ----
    glow = theme["horizon"]
    for x in range(pw):
        if horizon_row - 1 >= 0:
            pixels[horizon_row - 1][x] = glow

    # ---- GRAY TABBY CAT — 9-wide × 9-tall pixel sprite in the colon ----
    # Palette: K black outline, D dark fur, g medium fur, W white patch,
    # P pink blush, N pink nose, E closed-eye arc, e open-eye dot.
    #
    # Idle animation rides the colon's beat: a slow tail flick every ~1.4 s
    # and a brief open-eyed blink every ~4.5 s. Frame choice is keyed off
    # `mono` so the cat freezes with the clock during an offline hold.
    blink = (mono % 4.5) > 4.30
    tail_up = int(mono * 0.7) % 2 == 0
    eye = "e" if blink else "E"
    body_row = ".KgWWWgDK" if tail_up else ".KgWWWggK"
    tail_row = ".KggggDDK" if tail_up else ".KgggggDK"
    SUNRISE_CAT = (
        ".K.....K.",     # ear tips
        "KDK...KDK",     # ears
        "KDPKKKPDK",     # ears meet head, pink inside
        "KDgDgDgDK",     # top of head with tabby stripe
        f"Kg{eye}WNW{eye}gK",     # eyes flanking pink nose
        "KgPWWWPgK",     # cheeks + white muzzle
        body_row,        # chest + tail base (animated)
        tail_row,        # body + tail curl (animated)
        "..KggggK.",     # feet forming
    )
    sunrise_cat_colors = {
        "K": theme["cat_outline"],
        "D": theme["cat_fur_dark"],
        "g": theme["cat_fur"],
        "W": theme["cat_white"],
        "P": theme["cat_blush"],
        "E": theme["cat_eye_closed"],
        "e": theme["cat_eye_open"],
        "N": theme["cat_nose"],
    }
    _place_colon_sprite(pixels, pw, ph, width, height, SUNRISE_CAT, sunrise_cat_colors)

    _blit_pixels(pixels, grid, 0, height, height, pw, ph)
    return grid


# Frames for the "cats" theme live as raw RGB byte files alongside this
# file — no PIL or ffmpeg needed at runtime, which matters because the
# phone runs in a proot with no image libraries. Each frame was already
# cropped to strip the "starting soon" caption and downsampled to
# 320x172 so it fits any panel with nearest-neighbour rescaling.
_CATS_FRAMES = None
_CATS_META = None
_CATS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "cats")
_CATS_FRAME_SECONDS = 0.6              # source runs at 5/3 fps (~0.6 s/frame)


def _load_cats_frames():
    """Load meta.txt + f??.rgb files. Returns (frames_bytes, sw, sh)."""
    try:
        with open(os.path.join(_CATS_DIR, "meta.txt")) as fp:
            parts = fp.read().split()
        sw, sh = int(parts[0]), int(parts[1])
    except (OSError, ValueError, IndexError):
        return [], 0, 0
    frames = []
    try:
        names = sorted(n for n in os.listdir(_CATS_DIR) if n.endswith(".rgb"))
    except OSError:
        return [], sw, sh
    expected = sw * sh * 3
    for name in names:
        try:
            with open(os.path.join(_CATS_DIR, name), "rb") as fp:
                data = fp.read()
            if len(data) == expected:
                frames.append(data)
        except OSError:
            continue
    return frames, sw, sh


def _cats_source_bounds(pw, ph, sw, sh):
    """Cover-fit: return the source-pixel rectangle [x0,x1) × [y0,y1)
    that maps 1:1 onto the panel pixel buffer without letter-boxing.

    Chosen so the *shorter* of the two dimensions fills the panel and the
    longer is cropped equally on both sides. Because the cats sit in the
    middle of the source, a horizontal crop keeps them; a vertical crop
    keeps the meadow.
    """
    # Compare aspects as cross-multiplications so we stay in integer land.
    if sw * ph >= sh * pw:
        # Video is wider than panel → crop sides.
        crop_w = pw * sh // ph
        x0 = (sw - crop_w) // 2
        return x0, x0 + crop_w, 0, sh
    # Video is taller than panel → crop top/bottom.
    crop_h = ph * sw // pw
    y0 = (sh - crop_h) // 2
    return 0, sw, y0, y0 + crop_h


def cats_ambient(width, height, mono):
    """A pixel-art meadow with two cats: plays the 6-frame source as a
    full panel background. Uses a "cover" fit so the panel always fills
    (no floating letter-boxed strip), and nearest-neighbour sampling so
    the source pixels stay as crisp blocks — the assets were already
    LANCZOS-downscaled on the desktop, so runtime is almost always an
    up-sample from a smaller, cleaner source. Pure Python; no PIL.
    """
    global _CATS_FRAMES, _CATS_META
    if _CATS_FRAMES is None:
        frames, sw, sh = _load_cats_frames()
        _CATS_FRAMES = frames
        _CATS_META = (sw, sh)
    grid = _empty_grid(width, height)
    if not _CATS_FRAMES:
        return grid
    pw = width
    ph = height * 2
    if pw <= 0 or ph <= 0:
        return grid

    sw, sh = _CATS_META
    data = _CATS_FRAMES[int(mono / _CATS_FRAME_SECONDS) % len(_CATS_FRAMES)]

    x0, x1, y0, y1 = _cats_source_bounds(pw, ph, sw, sh)
    src_w = x1 - x0
    src_h = y1 - y0

    # Centre-of-bucket sampling: for target column x, pick the source
    # column at the *middle* of its bucket rather than the top-left.
    # That keeps the pixel-art blocks sharp when up-sampling and avoids
    # skipping every-other pixel when down-sampling.
    row_stride = sw * 3
    src_x = [x0 + (x * 2 + 1) * src_w // (pw * 2) for x in range(pw)]
    src_y = [y0 + (y * 2 + 1) * src_h // (ph * 2) for y in range(ph)]
    fmt = "#%02x%02x%02x".__mod__
    pixels = [[None] * pw for _ in range(ph)]
    for y in range(ph):
        row_off = src_y[y] * row_stride
        row = pixels[y]
        for x in range(pw):
            p = row_off + src_x[x] * 3
            row[x] = fmt((data[p], data[p + 1], data[p + 2]))

    _blit_pixels(pixels, grid, 0, height, height, pw, ph)
    return grid


# ---------------------------------------------------------------------------
# Themes. Add one by dropping a dict below; the loader picks it up by name
# from $SUBSCREEN_THEME or ~/.cache/subscreen/theme (polled live).
# ---------------------------------------------------------------------------

THEMES = {
    # Original: rain-soaked green under a grey sky. No ambient animation.
    "grove": {
        "ink": "#000000",
        "text": "#e6e6e6",
        "dim": "#7a7a7a",
        "faint": "#151515",
        "white": "#ffffff",
        "line": "#9a9a9a",
        "line_soft": "#454545",
        "row_hot": "#131313",
        "row_cool": "#0b0b0b",
        "edge_cool": "#2f2f2f",
        "cool": "#4e8570",
        "warm": "#79b39c",
        "pale": "#79b39c",
        "lime": "#4e8570",
        "spark_hi": "#a3dcc4",
        "spark_empty": "#3f3f3f",
        "alert": "#ff6b6b",
        "offline_dot": "#c25b5b",
        "shadow_mix": 0.58,
        "ambient": None,
    },
    # Abyss: jellyfish silhouettes drift through near-black water, sonar
    # pings spread in wide rings, plankton scatters in the wake. Phosphor
    # cyan is the primary; sunlight-orange is the warm accent — what
    # filters down from far above when a prayer window is close.
    "abyss": {
        "ink": "#010208",
        "text": "#cfe0ea",
        "dim": "#4a6172",
        "faint": "#03121b",
        "white": "#eaf9ff",
        "line": "#7fa0b2",
        "line_soft": "#1a3444",
        "row_hot": "#08161f",
        "row_cool": "#02090f",
        "edge_cool": "#134152",
        "cool": "#45cfe8",
        "warm": "#ffb27a",
        "pale": "#8be9d9",
        "lime": "#45cfe8",
        "spark_hi": "#7ef8ff",
        "spark_empty": "#12242f",
        "alert": "#ff8a5e",
        "offline_dot": "#ff6f83",
        "shadow_mix": 0.75,
        # Water & floor
        "water_light": "#4a90a8",
        "sand": "#8a7048",
        "sand_dark": "#4a3a24",
        "rock": "#2c2a30",
        # Coral palette — four hues so a reef never feels monotonous
        "coral_pink": "#e07c9f",
        "coral_orange": "#e08850",
        "coral_purple": "#a274c9",
        "coral_magenta": "#c85fc0",
        # Anemone
        "anemone_body": "#8c4c66",
        "anemone_tent": "#f0708c",
        # Kelp
        "kelp": "#2e5a3b",
        "kelp_light": "#5aa15a",
        # Marine snow, bubbles
        "marine_snow": "#c0d8e0",
        "bubble": "#a0e0f0",
        # Fish species
        "fish_a": "#8cb8d0",             # silver-blue school
        "fish_b": "#f5d472",             # yellow school
        "fish_medium": "#ff9a52",        # solo orange fish
        "fish_medium_dark": "#a04022",   # shadow / dark banding
        # Jellyfish
        "jelly_bright": "#eabcff",
        "jelly_mid": "#a970d8",
        "tentacle_bright": "#8f6ac6",
        "tentacle_mid": "#5c4585",
        "tentacle_dim": "#2f2452",
        # Turtle
        "turtle_shell": "#4a6b3a",
        "turtle_shell_dark": "#233520",
        "turtle_flipper": "#3a5230",
        # Sonar
        "sonar_bright": "#7bf6da",
        "sonar_mid": "#2f8074",
        # Colon creature — a tiny sea-puffer with cyan body
        "colon_a": "#7bf6da",   # spikes / tail
        "colon_b": "#010208",   # eyes (near-ink)
        "colon_c": "#4a90a8",   # body
        "ambient": abyss_ambient,
    },
    # Aurora: two-layer parallax curtains with a hue-drifting triad above
    # a twinkling star field, punctuated by shooting stars every ~19 s.
    # Ink is deep indigo — a night sky, not black.
    "aurora": {
        "ink": "#010214",
        "text": "#dbe0f4",
        "dim": "#5a6685",
        "faint": "#0a0f25",
        "white": "#eef1ff",
        "line": "#8993b8",
        "line_soft": "#212a4a",
        "row_hot": "#0b112a",
        "row_cool": "#040519",
        "edge_cool": "#1e2650",
        "cool": "#cdd8ff",
        "warm": "#a2ffcc",
        "pale": "#c5ffe0",
        "lime": "#7dffb6",
        "spark_hi": "#bcffde",
        "spark_empty": "#242e54",
        "alert": "#ff7ab0",
        "offline_dot": "#ff86a9",
        "shadow_mix": 0.72,
        # Aurora curtain triad
        "aurora_a": "#52ffb4",
        "aurora_b": "#8b8bff",
        "aurora_c": "#ff7cd8",
        # Stars & shooting stars
        "star_bright": "#ffffff",
        "star_dim": "#7d88b0",
        "shoot_head": "#ffffff",
        "shoot_trail": "#c8d4ff",
        "shoot_faint": "#5c68a0",
        # Landscape
        "mountain_far": "#1a2044",
        "mountain_near": "#0a0e26",
        "snow": "#c8d4f0",
        "pine": "#0a1810",
        "pine_snow": "#e0e8f0",
        "ice": "#8cb2ce",
        "ice_dark": "#3a5678",
        "owl": "#0a0812",
        # Colon creature — a perched snowy owl
        "colon_a": "#e0e8f0",   # feather tufts
        "colon_b": "#0a0812",   # eyes
        "colon_c": "#c8d4f0",   # face
        "ambient": aurora_ambient,
    },
    # Circuitry: a living PCB. Dim green traces span the panel with
    # brighter nodes at intersections; cyan pulses race along the wires
    # leaving fading tails. Everything reads as signal.
    "circuitry": {
        "ink": "#01050a",
        "text": "#c8e6cd",
        "dim": "#4b6b52",
        "faint": "#0a1a10",
        "white": "#e2ffe6",
        "line": "#4e7c58",
        "line_soft": "#153015",
        "row_hot": "#0a1c10",
        "row_cool": "#050c07",
        "edge_cool": "#1e4a26",
        "cool": "#58ffaf",
        "warm": "#ffd370",
        "pale": "#a4ffca",
        "lime": "#58ffaf",
        "spark_hi": "#a4ffca",
        "spark_empty": "#1c3a22",
        "alert": "#ff8862",
        "offline_dot": "#ff6a5e",
        "shadow_mix": 0.68,
        "scene_rows": 6,
        "trace": "#1b3a1b",
        "node": "#3f7a4a",
        "pulse_head": "#c8fff5",
        "pulse_mid": "#5ccdb0",
        "pulse_dim": "#25675a",
        "chip": "#182428",
        "chip_dark": "#080f12",
        "chip_label": "#88ccb0",
        "led_on": "#ff5c5c",
        "led_off": "#3a1c1c",
        "led_ok": "#5fff9c",
        # Colon creature — a small electronic bug
        "colon_a": "#58ffaf",   # antennae / legs
        "colon_b": "#01050a",   # eyes
        "colon_c": "#c8fff5",   # shell
        "ambient": circuitry_ambient,
    },
    # Forest: a miniature ecosystem rendered as half-block pixel art —
    # canopy, trunk, stream, and living animals. See forest_ambient for
    # the full scene description; the extra colour keys drive each layer.
    "forest": {
        "ink": "#020604",
        "text": "#e8e2c8",
        "dim": "#5a6b52",
        "faint": "#0a120c",
        "white": "#f8f4dc",
        "line": "#8fa385",
        "line_soft": "#243026",
        "row_hot": "#0d1810",
        "row_cool": "#040804",
        "edge_cool": "#3f7a3d",
        "cool": "#78b862",
        "warm": "#f5b76c",
        "pale": "#c6ee9c",
        "lime": "#78b862",
        "spark_hi": "#c6ee9c",
        "spark_empty": "#1e2820",
        "alert": "#ff8862",
        "offline_dot": "#e07458",
        "shadow_mix": 0.65,
        "scene_rows": 8,
        # Landscape palette
        "haze": "#4a5548",
        "hill_far": "#1e2a24",
        "hill_near": "#12201a",
        "tree_silhouette": "#0a1a10",
        "tree_silhouette_lit": "#3f7a3d",
        "grass_top": "#5aa254",
        "grass_dark": "#2f5a35",
        "soil": "#4a3020",
        "soil_dark": "#2c1c12",
        "water": "#2d5f7a",
        "water_light": "#7fc7d8",
        "water_dark": "#153c50",
        "rabbit_body": "#c8b090",
        "rabbit_ear": "#e0a89c",
        "rabbit_foot": "#8a7864",
        "bird": "#0a0a08",
        "butterfly_wing": "#f5c060",
        "butterfly_wing_alt": "#c99cf0",
        "butterfly_body": "#1a1008",
        "fish": "#e88848",
        "fish_shadow": "#a04020",
        # Colon creature — a rusty squirrel
        "colon_a": "#c8b090",   # ears / tail flick
        "colon_b": "#1a1008",   # eyes
        "colon_c": "#e0a89c",   # face
        "ambient": forest_ambient,
    },
    # Murmuration: dusk-purple sky, warm rose accent. A flock of 60
    # starlings swirls and morphs on a Lissajous path; the per-bird
    # flicker gives the shimmer real flocks have at low light.
    "murmuration": {
        "ink": "#080418",
        "text": "#e8dcf0",
        "dim": "#6a5a80",
        "faint": "#160c26",
        "white": "#f5edff",
        "line": "#9c86b8",
        "line_soft": "#2a1d3f",
        "row_hot": "#180f2a",
        "row_cool": "#080418",
        "edge_cool": "#3a1f52",
        "cool": "#d0b8ff",
        "warm": "#ffb0d8",
        "pale": "#e6d0ff",
        "lime": "#d0b8ff",
        "spark_hi": "#f2d8ff",
        "spark_empty": "#2e1f4a",
        "alert": "#ff7ab0",
        "offline_dot": "#ff7a95",
        "shadow_mix": 0.68,
        "scene_rows": 7,
        "bird_bright": "#f5eaff",
        "bird_mid": "#8f7abc",
        "bird_dim": "#3d2b60",
        "hill_far": "#251a3e",
        "hill_near": "#160c26",
        "star_bright": "#ffffff",
        "star_dim": "#7c6c9c",
        # Colon creature: a perched dusk-blue bird
        "colon_a": "#f5eaff",   # crest / body highlight
        "colon_b": "#3d2b60",   # feet / shadow
        "colon_c": "#8f7abc",   # eye
        "ambient": murmuration_ambient,
    },
    # Sunrise: a dreamy pink dawn. Soft lavender-to-gold sky gradient, a
    # rising half-disc sun with gentle rays, drifting pink clouds, dark
    # grass silhouettes below — and a tiny white cat sitting inside the
    # colon of the clock, the whole theme's reason for existing.
    "sunrise": {
        "ink": "#160820",
        "text": "#fce6d8",
        "dim": "#8f6a80",
        "faint": "#2a1030",
        "white": "#fff2e8",
        "line": "#d8a8c0",
        "line_soft": "#3a1c3c",
        "row_hot": "#2a1230",
        "row_cool": "#160820",
        "edge_cool": "#8c4a76",
        "cool": "#ffb0c0",
        "warm": "#ffd090",
        "pale": "#ffd8e0",
        "lime": "#ffb0c0",
        "spark_hi": "#ffe0a0",
        "spark_empty": "#3c1c34",
        "alert": "#ff7096",
        "offline_dot": "#ff6b7a",
        "shadow_mix": 0.62,
        # 5-stop sky gradient — lavender crown down to horizon gold
        "sky_top":     "#a884d0",       # lavender crown
        "sky_high":    "#dc94c8",       # rose
        "sky_mid":     "#f2a898",       # peach-coral
        "sky_low":     "#ffbb7a",       # apricot
        "sky_horizon": "#ffcc5a",       # warm gold at horizon
        # Sun & horizon glow
        "sun_body":         "#fff4c4",
        "sun_glow_inner":   "#ffd074",
        "sun_glow_outer":   "#ffa848",
        "horizon":          "#ffdc80",
        # Clouds — soft pinks
        "cloud":       "#ffdcea",
        "cloud_dark":  "#c88ab0",
        # Land silhouettes
        "land":        "#582858",
        "land_dark":   "#1a0824",
        # Cat sprite palette — gray tabby with white chest and pink cheeks,
        # matches the reference sprite the user picked for this theme.
        "cat_outline":    "#0f0f14",
        "cat_fur_dark":   "#585866",
        "cat_fur":        "#8f8f9a",
        "cat_highlight":  "#c8c8d0",
        "cat_white":      "#f5f5f0",
        "cat_blush":      "#ffb0b8",
        "cat_nose":       "#ff8898",
        "cat_eye_closed": "#c0505c",
        "cat_eye_open":   "#1a0a1c",
        # Colon creature reuses the tabby palette so any theme code that
        # still reaches for colon_a/b/c doesn't hit KeyError.
        "colon_a":     "#8f8f9a",
        "colon_b":     "#1a0a1c",
        "colon_c":     "#f5f5f0",
        "ambient": sunrise_ambient,
    },
    # Cats: a 6-frame pixel-art meadow drives the whole background. The UI
    # sits over the top of it, so the theme also asks compose() to shrink
    # the clock and the tasks strip a touch — otherwise the cats hide
    # behind them.
    "cats": {
        "ink": "#0a1a12",
        "text": "#f4ecd8",
        "dim": "#8a9a70",
        "faint": "#12241a",
        "white": "#fff8e0",
        "line": "#b9c890",
        "line_soft": "#2a3a24",
        "row_hot": "#132318",
        "row_cool": "#0b1810",
        "edge_cool": "#3a5a30",
        "cool": "#e6d488",
        "warm": "#ffc870",
        "pale": "#f8e4a4",
        "lime": "#c4e08a",
        "spark_hi": "#f8e4a4",
        "spark_empty": "#1f3220",
        "alert": "#ff8a5c",
        "offline_dot": "#ff7a6a",
        "shadow_mix": 0.68,
        # Compact layout so the UI doesn't cover the meadow:
        # cap the block clock's vertical scale and narrow the right column.
        "compact": True,
        "clock_max_tall": 4,
        "right_width_cap": 24,
        # Colon creature — a small warm-yellow spark that reads over the
        # pixel-art meadow without pulling attention off the cats.
        "colon_a": "#f8e4a4",
        "colon_b": "#0a1a12",
        "colon_c": "#ffc870",
        "ambient": cats_ambient,
    },
}


def _apply_theme(name):
    """Bind the module-level colour names from THEMES[name].

    Rebinds globals in place rather than routing every helper through a
    theme object, so the rest of the file stays exactly as it was — only
    the values behind familiar names change. Unknown names silently fall
    back to grove.
    """
    global INK, TEXT, DIM, FAINT, WHITE, LINE, LINE_SOFT, ROW_HOT, ROW_COOL
    global EDGE_COOL, COOL, WARM, PALE, LIME, GREEN, SPARK_HI, SPARK_EMPTY
    global ALERT, OFFLINE_DOT, SHADOW_MIX, AMBIENT, THEME, THEME_NAME
    theme = THEMES.get(name) or THEMES["grove"]
    INK = theme["ink"]
    TEXT = theme["text"]
    DIM = theme["dim"]
    FAINT = theme["faint"]
    WHITE = theme["white"]
    LINE = theme["line"]
    LINE_SOFT = theme["line_soft"]
    ROW_HOT = theme["row_hot"]
    ROW_COOL = theme["row_cool"]
    EDGE_COOL = theme["edge_cool"]
    COOL = theme["cool"]
    WARM = theme["warm"]
    PALE = theme["pale"]
    LIME = theme["lime"]
    GREEN = theme["cool"]
    SPARK_HI = theme["spark_hi"]
    SPARK_EMPTY = theme["spark_empty"]
    ALERT = theme["alert"]
    OFFLINE_DOT = theme["offline_dot"]
    SHADOW_MIX = theme["shadow_mix"]
    AMBIENT = theme.get("ambient")
    THEME = theme
    THEME_NAME = name if name in THEMES else "grove"


def _requested_theme():
    """Env var wins; otherwise a one-line file at ~/.cache/subscreen/theme."""
    env = os.environ.get("SUBSCREEN_THEME")
    if env:
        return env.strip()
    try:
        with open(THEME_FILE) as fp:
            return fp.read().strip() or None
    except OSError:
        return None


_apply_theme(_requested_theme() or "grove")


# ---------------------------------------------------------------------------
# SGR-aware row compositor. Merges an ambient cell grid with a UI string so
# the whole row lands in one write — no clear-then-repaint sequence, so no
# mid-frame flash. UI's cursor-forward escapes leave ambient visible in the
# gaps; UI's real characters overwrite ambient.
# ---------------------------------------------------------------------------

_SGR_RE = re.compile(r"\033\[([^A-Za-z]*)([A-Za-z])")


def _emit_cells(cells):
    out = []
    sentinel = object()
    last_fg = sentinel
    last_bg = sentinel
    for cell_fg, cell_bg, cell_char in cells:
        if cell_fg != last_fg:
            out.append("\033[39m" if cell_fg is None else fg(cell_fg))
            last_fg = cell_fg
        if cell_bg != last_bg:
            out.append("\033[49m" if cell_bg is None else bg(cell_bg))
            last_bg = cell_bg
        out.append(cell_char)
    out.append(RESET)
    return "".join(out)


def paint_row(ui, ambient_row, width, ink):
    # Ambient cells arrive as one of three shapes:
    #   None            transparent — falls back to ink
    #   (fg, char)      dot-style ambient (plankton, birds, pulses…)
    #   (fg, char, bg)  pixel-art ambient using half-block glyphs — bg
    #                   carries the *lower* half's colour, so ▀ paints
    #                   two stacked pixels in one cell
    cells = []
    for cell in ambient_row:
        if cell is None:
            cells.append((None, ink, " "))
        elif len(cell) == 2:
            fg_c, ch = cell
            cells.append((fg_c, ink, ch))
        else:
            fg_c, ch, bg_c = cell
            cells.append((fg_c, bg_c if bg_c is not None else ink, ch))
    col = 0
    cur_fg = None
    cur_bg = ink
    i = 0
    length = len(ui)
    while i < length and col < width:
        if ui[i] == "\033":
            match = _SGR_RE.match(ui, i)
            if not match:
                i += 1
                continue
            final = match.group(2)
            if final == "m":
                params = match.group(1)
                parts = params.split(";") if params else [""]
                p = 0
                while p < len(parts):
                    code = parts[p]
                    if code in ("", "0"):
                        cur_fg = None
                        cur_bg = ink
                        p += 1
                    elif code == "38" and p + 4 < len(parts) and parts[p + 1] == "2":
                        cur_fg = "#%02x%02x%02x" % (
                            int(parts[p + 2]), int(parts[p + 3]), int(parts[p + 4])
                        )
                        p += 5
                    elif code == "48" and p + 4 < len(parts) and parts[p + 1] == "2":
                        cur_bg = "#%02x%02x%02x" % (
                            int(parts[p + 2]), int(parts[p + 3]), int(parts[p + 4])
                        )
                        p += 5
                    elif code == "39":
                        cur_fg = None
                        p += 1
                    elif code == "49":
                        cur_bg = ink
                        p += 1
                    else:
                        p += 1
            elif final == "C":
                col += int(match.group(1) or "1")
            i = match.end()
            continue
        cells[col] = (cur_fg, cur_bg, ui[i])
        col += 1
        i += 1
    return _emit_cells(cells)


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
            out += (bg(tint) + " " * (j - i) + RESET) if tint else trans(j - i)
            i = j
        lines.append(out.rstrip())   # trailing blanks are the erase-to-EOL's job
    return lines


def rule(width, colour):
    """A thin separator line."""
    return fg(colour) + "─" * width + RESET


def thin_bar(fraction, width, colour, track=None):
    """A hairline progress bar.

    Drawn with rule glyphs rather than filled cells: a background block is
    a whole row tall, which next to one line of text reads as a slab.
    """
    if track is None:
        track = LINE_SOFT
    filled = max(0, min(width, round(fraction * width)))
    return (
        fg(colour) + "━" * filled + fg(track) + "─" * (width - filled) + RESET
    )


def bar(fraction, width, colour, track=None, edge=None):
    """A progress bar of coloured spaces — no glyph dependency.

    `edge` tints the leading cell so the bar reads as moving rather than
    merely long.
    """
    if track is None:
        track = FAINT
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


class _AthanController:
    """Handle for a playing athan: fades in, self-stops on mouse activity.

    mpv drives playback so we can adjust volume mid-stream over its JSON
    IPC socket. Two daemon threads run alongside it: a ramp thread walks
    volume from 0 to ATHAN_VOLUME over ATHAN_RAMP_SECONDS, and a watcher
    thread reads /dev/input/mice — any packet there means the person is
    at the machine, so we cut the sound. stop() is idempotent.
    """

    def __init__(self, proc, sock_path, stop_event):
        self._proc = proc
        self._sock = sock_path
        self._stop = stop_event

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._proc.terminate()
        except OSError:
            pass
        try:
            os.unlink(self._sock)
        except OSError:
            pass

    def poll(self):
        return self._proc.poll()


def _mpv_send(sock_path, command):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            s.connect(sock_path)
            s.sendall((json.dumps({"command": command}) + "\n").encode())
    except OSError:
        pass


def athan():
    """Play the full athan with a soft fade-in, loud, stoppable by a mouse nudge.

    Loudness comes from the filter chain — normalise, amplify, limit —
    while mpv's own volume stays at unity and only drives the fade-in.
    Playback runs to the end of the file unless a mouse nudge cuts it.
    Falls back to chime() when mpv or the file isn't available so the
    alert never goes quiet during a prayer rollover. The degraded filter
    chains below are quieter than the full one, since the gain lives in
    the chain they lost — audible, but never silent.
    """
    if not os.path.exists(ATHAN) or shutil.which("mpv") is None:
        chime()
        return None

    env = dict(os.environ, PULSE_SERVER=os.environ.get("PULSE_SERVER", "127.0.0.1"))
    sock_path = f"/tmp/subscreen-athan-{os.getpid()}.sock"
    try:
        os.unlink(sock_path)
    except OSError:
        pass

    def spawn(extra_args):
        return subprocess.Popen(
            [
                "mpv", "--no-video", "--no-terminal", "--really-quiet",
                f"--volume-max={ATHAN_VOLUME_MAX}",
                "--volume=0", f"--input-ipc-server={sock_path}",
                *extra_args, ATHAN,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # dynaudnorm and apad both come from libavfilter, which mpv builds
    # against everywhere we run — but a build missing either would refuse
    # the filter and exit at once. Degrade one step at a time instead of
    # losing the athan: full chain, then padding alone, then bare
    # playback. A quick poll after each spawn catches the immediate exit.
    #
    # dynaudnorm runs first so the pad's silence is never itself
    # normalised up into hiss.
    # Order matters: normalise, then apply gain, then limit what that gain
    # pushed over the ceiling, and only pad once the audio is final.
    attempts = [
        [f"--af=lavfi=[dynaudnorm=f=250:g=7:p=0.9,"
         f"volume={ATHAN_GAIN_PERCENT / 100:.2f},"
         f"alimiter=limit={ATHAN_LIMIT}:attack=5:release={ATHAN_LIMIT_RELEASE},"
         f"apad=pad_dur={ATHAN_TAIL_PAD_SECONDS}]"],
        [f"--af=lavfi=[apad=pad_dur={ATHAN_TAIL_PAD_SECONDS}]"],
        [],
    ]
    try:
        for extra_args in attempts:
            proc = spawn(extra_args)
            time.sleep(0.3)
            if proc.poll() is None:
                break
    except OSError:
        chime()
        return None

    stop_event = threading.Event()

    def ramp():
        # Wait for mpv to create its IPC socket before shouting into the
        # void. If mpv never opens it (bad file, missing sink), give up
        # after a couple of seconds and let the playback exit on its own.
        deadline = time.monotonic() + 2.0
        while not os.path.exists(sock_path) and time.monotonic() < deadline:
            if stop_event.is_set() or proc.poll() is not None:
                return
            time.sleep(0.05)
        start = time.monotonic()
        while not stop_event.is_set() and proc.poll() is None:
            elapsed = time.monotonic() - start
            if elapsed >= ATHAN_RAMP_SECONDS:
                _mpv_send(sock_path, ["set_property", "volume", ATHAN_VOLUME])
                return
            _mpv_send(
                sock_path,
                ["set_property", "volume",
                 round(ATHAN_VOLUME * elapsed / ATHAN_RAMP_SECONDS, 1)],
            )
            time.sleep(0.1)

    def watch_mouse():
        # Two mouse-watch backends, in order of preference. The evdev path
        # (/dev/input/mice) is the fastest and works on real Linux desktops,
        # but Android's Termux-X11 blocks reads on /dev/input/*, so the X11
        # path (polling xdotool getmouselocation) is the fallback that keeps
        # the "nudge to silence" gesture working on the phone. Either backend
        # sets stop_event and terminates mpv the moment it sees movement.
        def cut():
            stop_event.set()
            try:
                proc.terminate()
            except OSError:
                pass

        try:
            fd = os.open("/dev/input/mice", os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            fd = None

        if fd is not None:
            try:
                while not stop_event.is_set() and proc.poll() is None:
                    try:
                        if os.read(fd, 3):
                            cut()
                            return
                    except BlockingIOError:
                        time.sleep(0.05)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return

        # X11 fallback — poll pointer coordinates through xdotool.
        if shutil.which("xdotool") is None or not os.environ.get("DISPLAY"):
            return

        def read_xy():
            try:
                out = subprocess.check_output(
                    ["xdotool", "getmouselocation"],
                    stderr=subprocess.DEVNULL,
                    timeout=0.5,
                ).decode()
            except (subprocess.SubprocessError, OSError):
                return None
            xs = ys = None
            for tok in out.split():
                if tok.startswith("x:"):
                    try:
                        xs = int(tok[2:])
                    except ValueError:
                        pass
                elif tok.startswith("y:"):
                    try:
                        ys = int(tok[2:])
                    except ValueError:
                        pass
            return (xs, ys) if xs is not None and ys is not None else None

        base = read_xy()
        if base is None:
            return
        while not stop_event.is_set() and proc.poll() is None:
            time.sleep(0.1)
            cur = read_xy()
            if cur is not None and cur != base:
                cut()
                return

    threading.Thread(target=ramp, name="athan-ramp", daemon=True).start()
    threading.Thread(target=watch_mouse, name="athan-mouse", daemon=True).start()
    return _AthanController(proc, sock_path, stop_event)


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

    # Hold on the prayer whose window we are inside — from its adhan until
    # its iqama passes — before letting the display advance to the next
    # upcoming prayer. Without this, the label would flip forward the
    # instant the adhan arrives, hiding the prayer that just came in.
    current = schedule.get("current_prayer")
    iqamas = schedule.get("iqamas") or {}
    times = schedule.get("times") or {}
    current_iqama = iqamas.get(current) if current else None
    in_window = current is not None and current_iqama is not None and now < current_iqama
    if in_window:
        key = current
        at_dt = times.get(current) or schedule["next_time"]
        iqama_dt = current_iqama
    else:
        key = schedule["next_prayer"]
        at_dt = schedule["next_time"]
        iqama_dt = schedule["next_iqama"]

    # `next_at` drives the countdown — during a window we count down to
    # the iqama, otherwise to the next adhan.
    countdown_target = iqama_dt if in_window and iqama_dt else at_dt

    return now, {
        "next": key,
        "next_display": prayer_lib.DISPLAY_NAMES.get(key, key.title()),
        "at": at_dt.strftime("%H:%M"),
        "iqama": iqama_dt.strftime("%H:%M") if iqama_dt else None,
        "next_at": countdown_target,
        "in_window": in_window,
        "iqama_at": iqama_dt if in_window else None,
        "tomorrow": (False if in_window else schedule["next_is_tomorrow"]),
        # Marked when the month's timetable is missing and these are the
        # computed times, which run about a minute off the published ones.
        "estimated": schedule.get("source") == "computed",
    }


def server_view(state):
    """The same shape, rebuilt from the server payload, for the fallback.

    Mirrors local_view's "hold until iqama" behaviour: if the server said
    a prayer window is currently open and its iqama is still ahead of us,
    keep the label on that prayer instead of jumping to the upcoming one.
    """
    elapsed = time.monotonic() - state["_mono"]
    now = state["_base"] + timedelta(seconds=elapsed)
    p = state["prayer"]
    iqama_seconds = p.get("current_iqama_seconds")
    current_iqama_at = (
        now + timedelta(seconds=iqama_seconds - elapsed)
        if iqama_seconds is not None
        else None
    )
    in_window = (
        p.get("current")
        and current_iqama_at is not None
        and now < current_iqama_at
    )
    if in_window:
        key = p["current"]
        display = p.get("current_display") or key.title()
        at_str = p.get("current_at") or p["at"]
        iqama_str = p.get("current_iqama") or p["iqama"]
        # Countdown target = the iqama we're waiting for.
        next_at = current_iqama_at
        tomorrow = False
    else:
        key = p["next"]
        display = p["next_display"]
        at_str = p["at"]
        iqama_str = p["iqama"]
        next_at = now + timedelta(seconds=p["seconds_until"] - elapsed)
        tomorrow = p["tomorrow"]
    return now, {
        "next": key,
        "next_display": display,
        "at": at_str,
        "iqama": iqama_str,
        "next_at": next_at,
        "in_window": bool(in_window),
        "iqama_at": current_iqama_at if in_window else None,
        "tomorrow": tomorrow,
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
            m = re.match(r'\033\[(\d+)C', text[i:])
            if m:
                out += int(m.group(1))
                i += len(m.group(0))
                continue
            i = text.find("m", i) + 1
            continue
        out += 1
        i += 1
    return out


def compose(state, width, height, now, prayer, alert=None):
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
    rows_for_clock = height - 6 - (4 if alert else 0)
    scale_x, scale_y = 1, 1
    # Rows are the scarce resource, so size from height first and take the
    # matching width. Deriving height from width instead leaves the clock
    # far narrower than it needs to be whenever rows run out first.
    clock_text = now.strftime("%H:%M")
    for tall in range(THEME.get("clock_max_tall", 6), 0, -1):
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
        trans(pad) + line
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

    line = trans(start) + fg(WHITE) + date_text[:-4] + fg(DIM) + date_text[-4:] + RESET
    if gap >= 1:
        line += " " * gap + fg(accent) + corner + RESET
    body.append(line)
    if gap < 1:                       # no room to share — give prayer its own row
        body.append(trans(max(0, width - len(corner) - 1)) + fg(accent) + corner + RESET)
    body.append(rule(width, LINE))

    # Lower half is two columns: tasks on the left, progress on the right,
    # divided by a hairline. Both are built to a fixed width so the rule
    # above cannot stretch either of them.
    right_cap = THEME.get("right_width_cap", 32)
    right_width = max(20, min(right_cap, width // 3))
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
    right.append(" " * right_width)

    n_rows = max(len(left), len(right))
    for index in range(n_rows):
        cell_l = left[index] if index < len(left) else ""
        cell_r = right[index] if index < len(right) else ""
        left_pad = " " * max(0, left_width - visible_len(cell_l))
        right_pad = " " * max(0, right_width - visible_len(cell_r))
        body.append(
            cell_l
            + left_pad
            + fg(LINE_SOFT) + " │ " + RESET
            + cell_r
            + right_pad
        )

    if alert:
        # A band you cannot miss from across the room. It blinks on the
        # same one-second beat as the colon — deliberate, not fluttering.
        band = LIME if now.microsecond < 500_000 else mix(LIME, INK, 0.45)
        body.append(bg(band) + " " * width + RESET)
        body.append(bg(band) + fg(INK) + alert.center(width) + RESET)
        body.append(bg(band) + " " * width + RESET)

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

    def draw(self, frame, size, mono):
        # Two paths, same guarantee: exactly one write per row so no clear
        # ever leaks through between paints. With an ambient animation the
        # row is composited in Python (rain, plankton, stars…); without one
        # a bg-scoped erase-to-EOL clears to INK before the UI writes.
        ambient_grid = AMBIENT(size.columns, size.lines, mono) if AMBIENT else None
        base = bg(INK)
        ink = INK
        out = [SYNC_ON]
        current = []
        for row in range(size.lines):
            ui = frame[row] if row < len(frame) else ""
            if ambient_grid is not None:
                composed = paint_row(ui, ambient_grid[row], size.columns, ink)
            else:
                composed = f"{base}\033[2K{ui}{RESET}"
            current.append(composed)
            if row < len(self.previous) and self.previous[row] == composed:
                continue
            out.append(f"\033[{row + 1};1H{composed}")
        self.previous = current
        out.append(SYNC_OFF)
        if len(out) > 2:
            sys.stdout.write("".join(out))
            sys.stdout.flush()

    def close(self):
        sys.stdout.write(RESET + "\033[?25h\033[2J\033[H")
        sys.stdout.flush()


# Fetch state is owned by a background thread so the render loop never
# has to wait on the network. When the desktop is unreachable, fetch()
# blocks for its 4 s timeout — doing that on the main thread stuttered
# the frame every fetch cycle. The daemon thread updates _fetch_shared
# under _fetch_lock; the render loop reads it in constant time.
_fetch_shared = {"state": None, "last_success": 0.0}
_fetch_lock = threading.Lock()
_fetch_stop = threading.Event()
_STALE_AFTER = FETCH_EVERY * 2 + 2.0   # mark stale after ~12 s of no update


def _fetch_loop():
    while not _fetch_stop.is_set():
        try:
            payload = fetch(HOST)
        except (urllib.error.URLError, OSError, ValueError):
            payload = None
        if payload is not None:
            with _fetch_lock:
                _fetch_shared["state"] = payload
                _fetch_shared["last_success"] = time.monotonic()
        # sleep FETCH_EVERY, but wake early on shutdown
        _fetch_stop.wait(FETCH_EVERY)


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    screen = Screen()

    fetch_thread = threading.Thread(target=_fetch_loop, name="subscreen-fetch", daemon=True)
    fetch_thread.start()

    last_window_prayer = None   # name of the prayer whose window we last saw
    last_in_window = False      # whether that observation was inside its window
    alert = None
    alert_until = 0.0
    nagbars = []
    athan_ctl = None
    last_theme_check = 0.0

    try:
        while True:
            # Constant-time read of the fetcher's latest state — never blocks.
            with _fetch_lock:
                state = _fetch_shared["state"]
                last_success = _fetch_shared["last_success"]
            stale = state is None or (time.monotonic() - last_success) > _STALE_AFTER

            # Theme file poll piggybacks on the fetch cadence: one small
            # local file read every 5 s, cheap enough to stay on the main
            # thread.
            if time.time() - last_theme_check >= FETCH_EVERY:
                requested = _requested_theme() or "grove"
                if requested != THEME_NAME:
                    _apply_theme(requested)
                    # A theme switch changes ink and every accent, so the
                    # row-diff cache is worthless; flush it and repaint the
                    # background under the new ink before the next frame.
                    screen.previous = []
                    sys.stdout.write(bg(INK) + "\033[2J")
                    sys.stdout.flush()
                last_theme_check = time.time()

            view = local_view() or (server_view(state) if state else None)
            if view is None:
                size = terminal_size()
                screen.draw(
                    [fg(DIM) + f"  connecting to {HOST} …" + RESET],
                    size,
                    time.monotonic(),
                )
                time.sleep(FRAME_EVERY)
                continue
            now, prayer_state = view

            # Adhan edge: we just crossed into a prayer's window. The label
            # now holds on that prayer until iqama, so the trigger is the
            # False→True flip of in_window (or a rename inside a window,
            # which would only happen at midnight fajr). Driven by whichever
            # clock is in use, so the chime still fires offline.
            in_window_now = bool(prayer_state.get("in_window"))
            current_name = prayer_state["next"]
            entered_window = (
                in_window_now
                and last_window_prayer is not None
                and (not last_in_window or current_name != last_window_prayer)
            )
            if entered_window:
                alert = f"{current_name.upper()} — time to pray"
                alert_until = time.monotonic() + ALERT_SECONDS
                if athan_ctl is not None:
                    athan_ctl.stop()
                athan_ctl = athan()
                bar_proc = notify(alert)
                if bar_proc:
                    nagbars.append(bar_proc)
            last_window_prayer = current_name
            last_in_window = in_window_now

            if alert and time.monotonic() > alert_until:
                # The banner has served its purpose; drop it. Leave any
                # in-flight athan alone — it plays through to the end of
                # the file on its own.
                alert = None
            if athan_ctl is not None and athan_ctl.poll() is not None:
                athan_ctl = None
            for entry in list(nagbars):
                if time.monotonic() > entry[1]:
                    entry[0].terminate()
                    nagbars.remove(entry)

            size = terminal_size()
            dx, dy = drift()
            frame = compose(
                state or OFFLINE, size.columns - dx, size.lines - dy,
                now, prayer_state, alert,
            )
            frame = [""] * dy + [(trans(dx) + line if line else "") for line in frame]

            if stale:
                # The whole offline hint: a single dot in the top-right,
                # keyed to the fixed terminal corner rather than the drifted
                # frame so it never drags a tail across the OLED.
                dot_col = max(0, size.columns - 2)
                row0 = frame[0] if frame else ""
                shown = visible_len(row0)
                if shown <= dot_col:
                    row0 = (
                        row0
                        + trans(dot_col - shown)
                        + fg(OFFLINE_DOT) + "●" + RESET
                    )
                    if frame:
                        frame[0] = row0
                    else:
                        frame.append(row0)

            screen.draw(frame, size, time.monotonic())
            time.sleep(FRAME_EVERY)
    finally:
        _fetch_stop.set()
        for entry in nagbars:
            entry[0].terminate()
        if athan_ctl is not None:
            athan_ctl.stop()
        screen.close()


if __name__ == "__main__":
    main()
