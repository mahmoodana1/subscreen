"""Ad-hoc test harness for athan(): fires the fade-in and honours a mouse
nudge as a cut, exactly the way a prayer rollover would. Duration comes
from argv so phonectl can bound it."""

import sys
import time

sys.path.insert(0, "/root")
from subscreen_tui import athan

# athan.mp3 runs 3 m 42 s; the default has to clear that plus the 30 s tail
# pad, or the harness cuts the recording short instead of letting it finish.
duration = float(sys.argv[1]) if len(sys.argv) > 1 else 270.0

ctl = athan()
if ctl is None:
    print("athan() returned None — mpv or athan.mp3 missing")
    sys.exit(0)

print(f"athan playing for up to {duration:.0f}s — move the mouse to stop")
start = time.monotonic()
while time.monotonic() - start < duration:
    if ctl.poll() is not None:
        print(f"  stopped at t={time.monotonic() - start:.2f}s")
        break
    time.sleep(0.2)
ctl.stop()
print("done.")
