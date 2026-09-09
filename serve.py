#!/usr/bin/env python3
"""Desktop half of the phone sub-screen.

Serves a tiny payload the phone polls over the LAN (or Tailscale):

    GET /state         JSON  — clock, next prayer, top taskvim task
    GET /screen?w=NN   text  — the same thing pre-rendered as a terminal frame
    GET /health        text  — "ok"

The pre-rendered /screen route exists because Termux has no python, only
curl. The phone client is a curl loop; all formatting happens here.

Run:  python3 serve.py [--port 8765] [--bind 0.0.0.0]
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

TASKS_FILE = Path.home() / ".local/share/taskvim/data.json"
PRAYER_MODULE = Path.home() / ".config/hypr/panel/prayer.py"

# Write a hex colour here to re-tint the phone's clock on its next poll.
# Doubles as the honest end-to-end check that the link is live.
ACCENT_FILE = Path.home() / ".cache/subscreen/accent"


def accent_override():
    try:
        value = ACCENT_FILE.read_text().strip()
    except OSError:
        return None
    ok = len(value) == 7 and value[0] == "#"
    return value if ok and all(c in "0123456789abcdefABCDEF" for c in value[1:]) else None


# The panel dir goes on sys.path rather than loading prayer.py by path,
# because timetable.py imports prayer itself and needs to find it.
sys.path.insert(0, str(PRAYER_MODULE.parent))
import prayer                                        # noqa: E402

try:
    # Published Jordan Awqaf timings from the monthly cache. Prefer it, so
    # the server agrees with the HUD and the phone rather than serving the
    # computed times, which run about a minute off.
    import timetable                                 # noqa: E402
except Exception:
    timetable = None

SCHEDULE = timetable or prayer


def top_tasks(limit=2):
    """The few tasks worth showing, most important first.

    taskvim's dash_section carries the priority: 'important' wins outright.
    Only one important task is usually open, so the rest of the slots are
    filled from everything else — the phone should never show a blank row
    just because the important list is short.

    Within each pool: dated before undated, soonest due first, id as the
    tiebreak.
    """
    try:
        data = json.loads(TASKS_FILE.read_text())
    except (OSError, ValueError) as exc:
        return [], 0, f"taskvim unreadable: {exc.__class__.__name__}"

    # data["tasks"] holds everything ever created — course items, completed
    # repeats, old junk. The dashboard is the explicit id list in
    # data["dashboard"], and that is the only thing taskvim itself shows.
    by_id = {t["id"]: t for t in data.get("tasks", [])}
    order = {task_id: i for i, task_id in enumerate(data.get("dashboard", []))}
    open_tasks = [
        by_id[task_id]
        for task_id in data.get("dashboard", [])
        if task_id in by_id and not by_id[task_id].get("completed")
    ]
    if not open_tasks:
        return [], 0, None

    today = date.today().isoformat()
    rank = {"important": 0, "general": 1, "agent": 2}

    def sort_key(task):
        due = task.get("due")
        return (
            rank.get(task.get("dash_section"), 1),
            0 if due else 1,
            due or "",
            order.get(task["id"], 0),
        )

    chosen = sorted(open_tasks, key=sort_key)[:limit]

    return (
        [
            {
                "id": t["id"],
                "text": t.get("text") or "",
                "due": t.get("due"),
                "project": t.get("project"),
                "section": t.get("dash_section") or "general",
                "important": t.get("dash_section") == "important",
                "overdue": bool(t.get("due") and t["due"] < today),
            }
            for t in chosen
        ],
        len(open_tasks),
        None,
    )


def goal_stats():
    """Today's completions against taskvim's daily goal, plus a week of history.

    taskvim's log is one entry per completion, dated, so counting per day
    is enough — no need to reproduce its streak rule.
    """
    try:
        data = json.loads(TASKS_FILE.read_text())
    except (OSError, ValueError):
        return {}

    counts = {}
    for entry in data.get("log", []):
        counts[entry.get("date")] = counts.get(entry.get("date"), 0) + 1

    today = date.today()
    week = [
        counts.get((today - timedelta(days=offset)).isoformat(), 0)
        for offset in range(6, -1, -1)      # oldest first, today last
    ]
    return {
        "target": data.get("daily_goal") or 0,
        "done_today": counts.get(today.isoformat(), 0),
        "week": week,
    }


def build_state():
    schedule = SCHEDULE.schedule()
    now = schedule["now"]
    tasks, open_count, task_error = top_tasks(2)

    return {
        "generated": now.isoformat(),
        "clock": now.strftime("%H:%M:%S"),
        "date": now.strftime("%a %d %b"),
        "prayer": {
            "next": schedule["next_prayer"],
            "next_display": prayer.DISPLAY_NAMES.get(
                schedule["next_prayer"], schedule["next_prayer"].title()
            ),
            "next_arabic": prayer.ARABIC_NAMES.get(schedule["next_prayer"], ""),
            "at": schedule["next_time"].strftime("%H:%M"),
            "iqama": schedule["next_iqama"].strftime("%H:%M")
            if schedule["next_iqama"]
            else None,
            "countdown": prayer.format_countdown(schedule["until_next"]),
            "seconds_until": int(schedule["until_next"].total_seconds()),
            # Span of the window we are currently inside, so the phone can
            # draw progress rather than just a number counting down.
            "window_seconds": (
                int((schedule["next_time"] - schedule["times"][schedule["current_prayer"]]).total_seconds())
                if schedule.get("current_prayer") in schedule["times"]
                else None
            ),
            "tomorrow": schedule["next_is_tomorrow"],
            # prayer.schedule() reports no source and is always computed;
            # timetable.schedule() says which of the two it used.
            "estimated": schedule.get("source", "computed") == "computed",
            "current": schedule["current_prayer"],
        },
        "tasks": tasks,
        "open_tasks": open_count,
        "task_error": task_error,
        "goal": goal_stats(),
        "accent": accent_override(),
    }


def _clip(text, width):
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def render(state, width=46):
    """Draw the frame. Kept deliberately plain so it survives any font."""
    width = max(28, min(width, 100))
    inner = width - 2
    p = state["prayer"]

    lines = ["┌" + "─" * inner + "┐"]

    head = f" {state['clock']}"
    tail = f"{state['date']} "
    gap = inner - len(head) - len(tail)
    lines.append("│" + head + " " * max(1, gap) + tail + "│")
    lines.append("├" + "─" * inner + "┤")

    label = p["next_display"].upper()
    if p["tomorrow"]:
        label += " (tmrw)"
    lines.append("│" + _clip(f" {label}  {p['at']}", inner).ljust(inner) + "│")

    sub = f"  in {p['countdown']}"
    if p["iqama"]:
        sub += f"   iqama {p['iqama']}"
    lines.append("│" + _clip(sub, inner).ljust(inner) + "│")
    lines.append("├" + "─" * inner + "┤")

    if state["task_error"]:
        lines.append("│" + _clip(f" ! {state['task_error']}", inner).ljust(inner) + "│")
    elif not state["tasks"]:
        lines.append("│" + " nothing open — inbox clear".ljust(inner) + "│")
    else:
        for task in state["tasks"]:
            marker = "*" if task["important"] else "-"
            lines.append(
                "│" + _clip(f" {marker} {task['text']}", inner).ljust(inner) + "│"
            )
            meta = [task["section"]]
            if task["due"]:
                meta.append(("overdue " if task["overdue"] else "due ") + task["due"])
            lines.append("│" + _clip("     " + " · ".join(meta), inner).ljust(inner) + "│")
        lines.append(
            "│" + _clip(f" {state['open_tasks']} open", inner).ljust(inner) + "│"
        )

    lines.append("└" + "─" * inner + "┘")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = urlparse(self.path)
        try:
            if route.path == "/health":
                return self._send("ok\n", "text/plain; charset=utf-8")
            if route.path == "/state":
                return self._send(
                    json.dumps(build_state(), ensure_ascii=False, indent=1) + "\n",
                    "application/json; charset=utf-8",
                )
            if route.path in ("/", "/screen"):
                params = parse_qs(route.query)
                try:
                    width = int(params.get("w", ["46"])[0])
                except ValueError:
                    width = 46
                return self._send(
                    render(build_state(), width), "text/plain; charset=utf-8"
                )
        except Exception as exc:  # a broken frame must not kill the server
            return self._send(f"error: {exc}\n", "text/plain; charset=utf-8")

        self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"{datetime.now():%H:%M:%S} {self.address_string()} {fmt % args}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"subscreen serving on {args.bind}:{args.port}")
    print(render(build_state()), end="")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
