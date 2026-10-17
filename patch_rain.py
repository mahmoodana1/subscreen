import re

with open("subscreen_tui.py", "r") as f:
    lines = f.read()

# 1. Add RAIN color
lines = lines.replace('FAINT = "#151515"', 'FAINT = "#151515"\nRAIN = "#0a1310"')

# 2. Add rain generation function
rain_func = """
def bg(colour):
    r, g, b = _rgb(colour)
    return f"\\033[48;2;{r};{g};{b}m"

def rain_str(col, width, row, mono):
    \"\"\"Return a string of background spaces/raindrops of length `width`.\"\"\"
    if width <= 0:
        return ""
    chars = []
    for x in range(col, col + width):
        seed = (x * 137) % 100
        speed = 10.0 + (seed % 10)
        cycle = 40.0
        pos = (row - seed) % cycle
        head = (mono * speed) % cycle
        dist = (head - pos) % cycle
        if dist < 1.0:
            chars.append(fg(RAIN) + "│" + RESET)
        else:
            chars.append(" ")
    return "".join(chars)
"""
lines = lines.replace('def bg(colour):\n    r, g, b = _rgb(colour)\n    return f"\\033[48;2;{r};{g};{b}m"', rain_func.strip())

# 3. Pass mono into big_lines and compose
lines = lines.replace('def big_lines(text, scale_x, scale_y, colour, colon_colour, shadow):', 'def big_lines(text, scale_x, scale_y, colour, colon_colour, shadow, mono, dy, pad):')

lines = lines.replace('out += (bg(tint) + " " * (j - i) + RESET) if tint else " " * (j - i)', 'out += (bg(tint) + " " * (j - i) + RESET) if tint else rain_str(pad + i, j - i, dy + row_idx, mono)')

# We need `row_idx` in big_lines:
lines = lines.replace('for row in grid:', 'for row_idx, row in enumerate(grid):')

# 4. Pass mono into compose
lines = lines.replace('def compose(state, stale, width, height, now, prayer, alert=None):', 'def compose(state, stale, width, height, now, prayer, alert, mono, dx, dy):')
lines = lines.replace('def compose(state, stale, width, height, now, prayer, alert=None, mono=0.0, dx=0, dy=0):', 'def compose(state, stale, width, height, now, prayer, alert, mono, dx, dy):') # Just in case

# Fix the clock call inside compose
lines = lines.replace('big_lines(clock_text, scale_x, scale_y, accent, colon, shadow)', 'big_lines(clock_text, scale_x, scale_y, accent, colon, shadow, mono, dy + len(body), dx + pad)')

# Replace structural padding in compose with rain_str
lines = lines.replace('body.extend(\n        " " * pad + line\n        for line in big_lines', 'body.extend(\n        rain_str(dx, pad, dy + len(body) + i, mono) + line\n        for i, line in enumerate(big_lines')

lines = lines.replace('" " * start + fg(WHITE)', 'rain_str(dx, start, dy + len(body), mono) + fg(WHITE)')

lines = lines.replace('" " * max(0, width - len(corner) - 1) + fg(accent)', 'rain_str(dx, max(0, width - len(corner) - 1), dy + len(body), mono) + fg(accent)')

lines = lines.replace('cell_l\n            + " " * max(0, left_width - visible_len(cell_l))', 'cell_l\n            + rain_str(dx + visible_len(cell_l), max(0, left_width - visible_len(cell_l)), dy + len(body), mono)')

# 5. Provide dx, dy, mono to compose in main
lines = lines.replace('frame = compose(\n                state or OFFLINE, stale, size.columns - dx, size.lines - dy,\n                now, prayer_state, alert,\n            )', 'frame = compose(\n                state or OFFLINE, stale, size.columns - dx, size.lines - dy,\n                now, prayer_state, alert, time.monotonic(), dx, dy,\n            )')

# 6. Apply rain to empty lines or margins
lines = lines.replace('body = [""] * (slack // 2) + body', 'body = [rain_str(dx, width, dy + i, mono) for i in range(slack // 2)] + body')

lines = lines.replace('frame = [""] * dy + [(" " * dx + line if line else "") for line in frame]', 'frame = [rain_str(0, size.columns, y, time.monotonic()) for y in range(dy)] + [(rain_str(0, dx, dy + row_idx, time.monotonic()) + line + rain_str(dx + visible_len(line), size.columns - dx - visible_len(line), dy + row_idx, time.monotonic())) for row_idx, line in enumerate(frame)]')

with open("subscreen_tui.py", "w") as f:
    f.write(lines)
