import re

with open("subscreen_tui.py", "r") as f:
    text = f.read()

# Add trans and update visible_len
trans_func = """
def trans(n):
    return f"\\033[{n}C" if n > 0 else ""

def visible_len(text):
    \"\"\"Length ignoring SGR escapes, for right-aligning coloured strings.\"\"\"
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\\033":
            m = __import__("re").match(r'\\033\\[(\\d+)C', text[i:])
            if m:
                out += int(m.group(1))
                i += len(m.group(0))
                continue
            i = text.find("m", i) + 1
            continue
        out += 1
        i += 1
    return out
"""
text = re.sub(r'def visible_len\(text\):.*?return out\n', trans_func.strip() + '\n', text, flags=re.DOTALL)

# Update big_lines
text = text.replace('out += (bg(tint) + " " * (j - i) + RESET) if tint else " " * (j - i)', 'out += (bg(tint) + " " * (j - i) + RESET) if tint else trans(j - i)')

# Update compose padding
text = text.replace('" " * pad + line', 'trans(pad) + line')
text = text.replace('" " * start + fg(WHITE)', 'trans(start) + fg(WHITE)')
text = text.replace('" " * max(0, width - len(corner) - 1) + fg(accent)', 'trans(max(0, width - len(corner) - 1)) + fg(accent)')
text = text.replace('cell_l\n            + " " * max(0, left_width - visible_len(cell_l))', 'cell_l\n            + trans(max(0, left_width - visible_len(cell_l)))')

# Replace the frame padding at the end of compose
text = text.replace('frame = [""] * dy + [(" " * dx + line if line else "") for line in frame]', 'frame = [""] * dy + [(trans(dx) + line if line else "") for line in frame]')

# Update Screen.draw
new_draw = """    def draw(self, frame, size, mono):
        out = [SYNC_ON]
        for row in range(size.lines):
            rain = rain_str(0, size.columns, row, mono)
            ui = frame[row] if row < len(frame) else ""
            out.append(f"\\033[{row + 1};1H{rain}\\r{ui}")
        out.append(SYNC_OFF)
        sys.stdout.write("".join(out))
        sys.stdout.flush()
"""
text = re.sub(r'    def draw\(self, frame\):.*?sys\.stdout\.flush\(\)', new_draw.strip(), text, flags=re.DOTALL)

# Update screen.draw call in main
text = text.replace('screen.draw(frame)', 'screen.draw(frame, size, time.monotonic())')

with open("subscreen_tui.py", "w") as f:
    f.write(text)
