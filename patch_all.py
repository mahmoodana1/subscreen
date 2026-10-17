with open("subscreen_tui.py", "r") as f:
    text = f.read()

# 1. Colours and imports
text = text.replace('import urllib.request\n', 'import urllib.request\nimport re\n')
text = text.replace('FAINT = "#151515"     # unfilled track of a bar\nROW_HOT', 'FAINT = "#151515"     # unfilled track of a bar\nRAIN = "#0a1310"\nRAIN_CHAR = "│"\nROW_HOT')

# 2. Rain logic
rain_logic = """
def bg(colour):
    r, g, b = _rgb(colour)
    return f"\\033[48;2;{r};{g};{b}m"

def rain_str(col, width, row, mono):
    if width <= 0: return ""
    chars = []
    for x in range(col, col + width):
        seed = (x * 137) % 100
        speed = 10.0 + (seed % 10)
        pos = (row - seed) % 40.0
        head = (mono * speed) % 40.0
        if (head - pos) % 40.0 < 1.0:
            chars.append(fg(RAIN) + RAIN_CHAR + "\\033[0m")
        else:
            chars.append(" ")
    return "".join(chars)

def trans(n):
    return f"\\033[{n}C" if n > 0 else ""
"""
text = text.replace('def bg(colour):\n    r, g, b = _rgb(colour)\n    return f"\\033[48;2;{r};{g};{b}m"\n', rain_logic)

# 3. visible_len
new_vis = """def visible_len(text):
    \"\"\"Length ignoring SGR escapes, for right-aligning coloured strings.\"\"\"
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\\033":
            m = re.match(r'\\033\\[(\\d+)C', text[i:])
            if m:
                out += int(m.group(1))
                i += len(m.group(0))
                continue
            i = text.find("m", i) + 1
            continue
        out += 1
        i += 1
    return out"""
old_vis = """def visible_len(text):
    \"\"\"Length ignoring SGR escapes, for right-aligning coloured strings.\"\"\"
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\\033":
            i = text.find("m", i) + 1
            continue
        out += 1
        i += 1
    return out"""
text = text.replace(old_vis, new_vis)

# 4. Padding replacements
text = text.replace('out += (bg(tint) + " " * (j - i) + RESET) if tint else " " * (j - i)', 'out += (bg(tint) + " " * (j - i) + RESET) if tint else trans(j - i)')
text = text.replace('body.extend(\n        " " * pad + line', 'body.extend(\n        trans(pad) + line')
text = text.replace('" " * start + fg(WHITE)', 'trans(start) + fg(WHITE)')
text = text.replace('" " * max(0, width - len(corner) - 1) + fg(accent)', 'trans(max(0, width - len(corner) - 1)) + fg(accent)')
text = text.replace('cell_l\n            + " " * max(0, left_width - visible_len(cell_l))', 'cell_l\n            + trans(max(0, left_width - visible_len(cell_l)))')
text = text.replace('frame = [""] * dy + [(" " * dx + line if line else "") for line in frame]', 'frame = [""] * dy + [(trans(dx) + line if line else "") for line in frame]')

# 5. screen.draw
old_draw = """    def draw(self, frame):
        out = [SYNC_ON]
        for row, line in enumerate(frame):
            if row >= len(self.previous) or self.previous[row] != line:
                out.append(f"\\033[{row + 1};1H{line}\\033[K")
        for row in range(len(frame), len(self.previous)):
            out.append(f"\\033[{row + 1};1H\\033[K")
        out.append(SYNC_OFF)
        if len(out) > 2:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        self.previous = list(frame)"""
new_draw = """    def draw(self, frame, size, mono):
        out = [SYNC_ON]
        for row in range(size.lines):
            rain = rain_str(0, size.columns, row, mono)
            ui = frame[row] if row < len(frame) else ""
            out.append(f"\\033[{row + 1};1H{rain}\\r{ui}")
        out.append(SYNC_OFF)
        sys.stdout.write("".join(out))
        sys.stdout.flush()"""
text = text.replace(old_draw, new_draw)

# 6. Call draw
text = text.replace('screen.draw(frame)', 'screen.draw(frame, size, time.monotonic())')

with open("subscreen_tui.py", "w") as f:
    f.write(text)
