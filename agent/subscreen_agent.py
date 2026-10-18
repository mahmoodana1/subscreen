#!/usr/bin/env python3
"""Subscreen agent: the desktop half of the phone hub.

Runs on every machine the phone should know about (the PC and the laptop),
as a systemd user service. It reads what is already on the machine and
serves it as JSON to the Subscreen app on the phone, over Tailscale:

    GET  /health            "ok"
    GET  /state             everything the phone shows, one JSON document
    GET  /prayer?days=N     the next N days of prayer times (the phone caches them)
    GET  /scene/NAME        a video or picture for a media scene, from SCENE_DIR (the phone keeps a copy)
    GET  /plan/             the iPhone's calendar and tasks app (plan/), over HTTPS from `tailscale serve`
    GET  /plan/data         its data: taskvim's tasks, dashboard and events, and the Yedion classes
    POST /plan/edit         {"op": "task.add", ...} one edit to taskvim (see PLAN_OPS)
    POST /action            {"id": "ws2"} runs one of a fixed set of actions
    POST /task              {"id": 12} completes (or reopens) a taskvim task

Sources, each optional — a machine without dojo simply reports none:
    input idle      /dev/input (the user must be in the "input" group),
                    falling back to watching the Hyprland cursor
    system stats    /proc, hwmon, nvidia-smi or amdgpu
    media           playerctl
    taskvim         ~/.local/share/taskvim/data.json (tasks can be ticked off)
    dojo            ~/.local/share/dojo/dojo.db (read-only)
    claude          ~/.cache/subscreen/claude/*.json, written by claude_hook.py
    prayer          the hypr panel's prayer.py / timetable.py

Reads are open to the tailnet. Actions are accepted only from the phones:
Android and iOS peers on the tailnet, or the addresses in the config file.

With "ntfy" set, the agent also pushes task reminders to that ntfy topic, for
the iPhone (which cannot keep the page running in the background).

Standard library only. Config (optional): ~/.config/subscreen/agent.json
    {"port": 8765, "kind": "Desktop", "allow_actions_from": ["100.x.y.z"],
     "ntfy": "https://ntfy.sh/some-long-secret-topic", "ntfy_token": "tk_..."}

    python3 agent/subscreen_agent.py --test-push    send one test notification and exit
"""

import glob
import hashlib
import ipaddress
import json
import os
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HOME = Path.home()
CONFIG_FILE = HOME / ".config/subscreen/agent.json"
TASKVIM_FILE = HOME / ".local/share/taskvim/data.json"
DOJO_DB = HOME / ".local/share/dojo/dojo.db"
CLAUDE_DIR = HOME / ".cache/subscreen/claude"
SCENE_DIR = HOME / ".config/subscreen/scenes"   # lumiere.mp4 etc.: kept out of the repo
SCENE_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}
YEDION_FILE = HOME / ".local/share/taskvim/yedion.ics"   # taskvim's cached class feed
PLAN_DIR = Path(__file__).resolve().parent.parent / "plan"   # the iPhone's calendar and tasks app
PLAN_TYPES = {".html": "text/html; charset=utf-8", ".png": "image/png", ".js": "text/javascript; charset=utf-8",
              ".webmanifest": "application/manifest+json"}
PANEL_DIRS = [HOME / ".config/hypr/panel", HOME / "dotfiles/hypr/.config/hypr/panel"]
HEAT_DAYS = 16 * 7
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
SOON_DAYS, LATE_DAYS, SOON_MAX = 2, 3, 24   # reminders: days ahead, days overdue still worth a nudge, at most this many
PALETTE = ["blue", "warm", "teal", "rose", "green", "sand"]   # taskvim's course colours, in order


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


CONFIG = load_config()


def log(*parts):
    print(datetime.now().strftime("%H:%M:%S"), *parts, flush=True)


# ---------------------------------------------------------------------------
# Session environment. A user service may start before the compositor has
# exported its variables, so find the live Hyprland and Wayland sockets
# ourselves; hyprctl, grim and playerctl all need them.
# ---------------------------------------------------------------------------

def session_env():
    env = dict(os.environ)
    run = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = run
    sig = env.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig or not Path(run, "hypr", sig).exists():
        dirs = sorted(Path(run, "hypr").glob("*/.socket.sock"), key=lambda p: p.stat().st_mtime, reverse=True)
        if dirs:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = dirs[0].parent.name
    if not env.get("WAYLAND_DISPLAY") or not Path(run, env["WAYLAND_DISPLAY"]).exists():
        socks = sorted(Path(run).glob("wayland-[0-9]"))
        if socks:
            env["WAYLAND_DISPLAY"] = socks[0].name
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run}/bus")
    return env


def run(cmd, timeout=3):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=session_env())
        return out.returncode, out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def spawn(cmd):
    """Start a program detached from the agent, so it outlives the request."""
    if shutil.which("uwsm") and cmd[0] != "uwsm":
        cmd = ["uwsm", "app", "--"] + cmd
    try:
        subprocess.Popen(cmd, env=session_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Input idle: how long since this machine was last touched. The phone shows
# whichever machine was touched most recently.
# ---------------------------------------------------------------------------

class Idle:
    def __init__(self):
        self.last = time.time()
        self.method = "none"
        threading.Thread(target=self._loop, daemon=True).start()

    def seconds(self):
        return max(0, time.time() - self.last)

    def _open_devices(self):
        fds = []
        for path in glob.glob("/dev/input/event*"):
            try:
                fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        return fds

    def _loop(self):
        fds, rescan = [], 0
        cursor = None
        while True:
            if time.time() > rescan:
                for fd in fds:
                    os.close(fd)
                fds, rescan = self._open_devices(), time.time() + 30
                self.method = "evdev" if fds else "cursor"
            if fds:
                ready, _, _ = select.select(fds, [], [], 1.0)
                for fd in ready:
                    try:
                        while os.read(fd, 4096):
                            pass
                    except BlockingIOError:
                        pass
                    except OSError:
                        rescan = 0
                if ready:
                    self.last = time.time()
            else:
                # No access to /dev/input: a moving cursor is the next best sign of life.
                _, out = run(["hyprctl", "cursorpos"], timeout=2)
                if out and out != cursor:
                    if cursor is not None:
                        self.last = time.time()
                    cursor = out
                time.sleep(1)


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.cpu_prev = None
        self.net_prev = None
        self.gpu = {"gpu": None, "gpuT": None}
        self.gpu_at = 0

    def _cpu(self):
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle, total = parts[3] + parts[4], sum(parts)
        prev, self.cpu_prev = self.cpu_prev, (idle, total)
        if not prev or total == prev[1]:
            return None
        return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)

    def _temp(self):
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                name = Path(hw, "name").read_text().strip()
            except OSError:
                continue
            if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
                try:
                    return round(int(Path(hw, "temp1_input").read_text()) / 1000, 1)
                except (OSError, ValueError):
                    continue
        return None

    def _gpu(self):
        if time.time() - self.gpu_at < 5:
            return self.gpu
        self.gpu_at = time.time()
        if shutil.which("nvidia-smi"):
            code, out = run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu",
                             "--format=csv,noheader,nounits"], timeout=4)
            if code == 0 and out.strip():
                try:
                    u, t = (float(x) for x in out.strip().splitlines()[0].split(","))
                    self.gpu = {"gpu": u, "gpuT": t}
                    return self.gpu
                except ValueError:
                    pass
        for busy in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
            try:
                u = float(Path(busy).read_text())
                temps = glob.glob(str(Path(busy).parent / "hwmon/hwmon*/temp1_input"))
                t = int(Path(temps[0]).read_text()) / 1000 if temps else None
                self.gpu = {"gpu": u, "gpuT": t}
                return self.gpu
            except (OSError, ValueError):
                continue
        return self.gpu

    def _mem(self):
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        return round((total - avail) / 1048576, 1), round(total / 1048576)

    def _net(self):
        rx = tx = 0
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                if name.strip() in ("lo",) or name.strip().startswith(("tailscale", "docker", "veth")):
                    continue
                cols = data.split()
                rx += int(cols[0])
                tx += int(cols[8])
        now = time.time()
        prev, self.net_prev = self.net_prev, (now, rx, tx)
        if not prev or now <= prev[0]:
            return 0, 0
        dt = now - prev[0]
        return max(0, (rx - prev[1]) / dt), max(0, (tx - prev[2]) / dt)

    def read(self):
        ram, ram_max = self._mem()
        down, up = self._net()
        return {"cpu": self._cpu(), "cpuT": self._temp(), **self._gpu(),
                "ram": ram, "ramMax": ram_max, "down": round(down), "up": round(up)}


# ---------------------------------------------------------------------------
# Media (playerctl)
# ---------------------------------------------------------------------------

def media():
    if not shutil.which("playerctl"):
        return None
    fmt = "{{playerName}}\t{{status}}\t{{title}}\t{{artist}}\t{{position}}\t{{mpris:length}}"
    code, out = run(["playerctl", "-a", "metadata", "--format", fmt])
    if code != 0:
        return None
    best = None
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) < 6 or not cols[2]:
            continue
        item = {"player": cols[0], "status": cols[1], "title": cols[2], "artist": cols[3],
                "pos": round(int(cols[4] or 0) / 1e6), "len": round(int(cols[5] or 0) / 1e6)}
        if best is None or (item["status"] == "Playing" and best["status"] != "Playing"):
            best = item
    return best


# ---------------------------------------------------------------------------
# taskvim (read-only; parsed the way taskvim itself shows it)
# ---------------------------------------------------------------------------

def _align(d, unit):
    if unit == "week":
        return d - timedelta(days=d.weekday())
    if unit == "month":
        return d.replace(day=1)
    return d


def _months_between(a, b):
    return (b.year - a.year) * 12 + b.month - a.month


def habit_period_start(today, rep):
    try:
        anchor = date.fromisoformat(rep.get("anchor", ""))
    except ValueError:
        anchor = today
    unit, every = rep.get("unit", "day"), max(1, int(rep.get("every", 1)))
    base, target = _align(anchor, unit), _align(today, unit)
    if unit == "month":
        steps = _months_between(base, target) // every * every
        y, m = divmod(base.month - 1 + steps, 12)
        return date(base.year + y, m + 1, 1)
    span = 7 if unit == "week" else 1
    steps = ((target - base).days // span) // every * every
    return base + timedelta(days=steps * span)


def habit_label(rep):
    n, unit = max(1, int(rep.get("every", 1))), rep.get("unit", "day")
    if n == 1:
        return {"day": "daily", "week": "weekly", "month": "monthly"}.get(unit, "daily")
    return f"every {n}{ {'day': 'd', 'week': 'w', 'month': 'mo'}.get(unit, 'd') }"


_taskvim_cache = {"mtime": None, "value": None, "day": None}


def taskvim():
    try:
        mtime = TASKVIM_FILE.stat().st_mtime
    except OSError:
        return None
    today = date.today()
    if _taskvim_cache["mtime"] == mtime and _taskvim_cache["day"] == today:
        return _taskvim_cache["value"]
    try:
        data = json.loads(TASKVIM_FILE.read_text())
    except (OSError, ValueError):
        return None

    projects = {p["id"]: p for p in data.get("projects", [])}
    by_id = {t["id"]: t for t in data.get("tasks", [])}
    groups = {"important": [], "general": [], "agent": []}
    for tid in data.get("dashboard", []):
        t = by_id.get(tid)
        if not t:
            continue
        rep = t.get("repeat")
        if rep:
            done = bool(t.get("completed")) and t.get("period") == habit_period_start(today, rep).isoformat()
        else:
            done = bool(t.get("completed"))
            if done:
                continue
        proj = projects.get(t.get("project"))
        groups.setdefault(t.get("dash_section") or "general", []).append({
            "id": t["id"], "text": t.get("text", ""), "done": done,
            "due": None if rep else t.get("due"),
            "habit": habit_label(rep) if rep else None,
            "course": proj["name"] if proj else None,
            "color": PALETTE[proj.get("color", 0) % len(PALETTE)] if proj else None,
        })

    counts = {}
    for entry in data.get("log", []):
        counts[entry.get("date")] = counts.get(entry.get("date"), 0) + 1
    d = today if counts.get(today.isoformat()) else today - timedelta(days=1)
    streak = 0
    while counts.get(d.isoformat(), 0) > 0:
        streak += 1
        d -= timedelta(days=1)
    start = today - timedelta(days=HEAT_DAYS)
    heat = {k: v for k, v in counts.items() if k and k >= start.isoformat()}

    courses = []
    for p in data.get("projects", []):
        if p.get("archived"):
            continue
        mine = [t for t in data.get("tasks", []) if t.get("project") == p["id"] and not t.get("repeat")]
        if not mine:
            continue
        courses.append({"name": p["name"], "color": PALETTE[p.get("color", 0) % len(PALETTE)],
                        "pct": round(100 * sum(1 for t in mine if t.get("completed")) / len(mine))})

    now = datetime.now()
    upcoming = []
    for ev in data.get("calendar_events", []):
        try:
            at = datetime.fromisoformat(f"{ev['date']}T{ev.get('start_time') or '00:00'}")
        except (KeyError, ValueError):
            continue
        if at >= now:
            upcoming.append((at, ev.get("title", "")))
    for t in data.get("tasks", []):
        if t.get("completed") or not t.get("due") or not t.get("start_time"):
            continue
        try:
            at = datetime.fromisoformat(f"{t['due']}T{t['start_time']}")
        except ValueError:
            continue
        if at >= now:
            proj = projects.get(t.get("project"))
            upcoming.append((at, f"{proj['name']} · {t['text']}" if proj else t["text"]))
    upcoming.sort()

    # Everything with a date in the next few days (and anything overdue), for the phone's
    # reminders. Times are left as they are so the phone works out how close each one is.
    soon = []
    horizon = (today + timedelta(days=SOON_DAYS)).isoformat()
    stale = (today - timedelta(days=LATE_DAYS)).isoformat()
    for t in data.get("tasks", []):
        if t.get("completed") or t.get("repeat") or not stale <= (t.get("due") or "") <= horizon:
            continue
        proj = projects.get(t.get("project"))
        soon.append({
            "kind": "task", "id": t["id"], "text": t.get("text", ""),
            "at": f"{t['due']}T{t['start_time']}" if t.get("start_time") else t["due"],
            "course": proj["name"] if proj else None,
            "color": PALETTE[proj.get("color", 0) % len(PALETTE)] if proj else None,
        })
    for ev in data.get("calendar_events", []):
        d = ev.get("date") or ""
        if not ev.get("start_time") or not today.isoformat() <= d <= horizon:
            continue
        soon.append({"kind": "event", "id": f"ev-{ev.get('id', d + ev['start_time'])}",
                     "text": ev.get("title", ""), "at": f"{d}T{ev['start_time']}"})
    soon.sort(key=lambda x: x["at"])

    value = {
        "mtime": mtime,
        "groups": groups,
        "goal": {"target": max(1, int(data.get("daily_goal") or 2)), "done": counts.get(today.isoformat(), 0)},
        "streak": streak,
        "heat": heat,
        "courses": courses,
        "next": [{"title": title, "at": at.isoformat(timespec="minutes")} for at, title in upcoming[:3]],
        "soon": soon[:SOON_MAX],
    }
    _taskvim_cache.update(mtime=mtime, value=value, day=today)
    return value


_task_lock = threading.Lock()


def toggle_task(tid):
    """Complete or reopen one task exactly the way taskvim's toggle_complete does:
    flip `completed`, and add (or remove the newest) log entry that feeds the
    heatmap, streak and daily goal. Written atomically; a running taskvim
    notices the new file and reloads it."""
    with _task_lock:
        data = json.loads(TASKVIM_FILE.read_text())
        t = next((x for x in data.get("tasks", []) if x.get("id") == tid), None)
        if t is None:
            return False, "that task no longer exists"
        was = bool(t.get("completed"))
        t["completed"] = not was
        log_ = data.setdefault("log", [])
        if not was:
            proj = next((p for p in data.get("projects", []) if p.get("id") == t.get("project")), None)
            sec = next((x for x in data.get("sections", []) if x.get("id") == t.get("section")), None)
            ids = [x.get("id", 0) for key in ("tasks", "projects", "sections", "log") for x in data.get(key, [])]
            nid = max([data.get("next_id", 1)] + [i + 1 for i in ids])
            data["next_id"] = nid + 1
            now = datetime.now()
            log_.append({"id": nid, "text": t.get("text", ""), "project": t.get("project"),
                         "project_name": proj["name"] if proj else "", "color": proj.get("color", 0) if proj else 0,
                         "section": t.get("section"), "section_name": sec["name"] if sec else "",
                         "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "task": tid})
            if t.get("repeat"):
                t["period"] = habit_period_start(date.today(), t["repeat"]).isoformat()
        else:
            for i in range(len(log_) - 1, -1, -1):
                e = log_[i]
                same = e.get("task") == tid if e.get("task") is not None else (
                    e.get("text") == t.get("text") and e.get("project") == t.get("project") and e.get("section") == t.get("section"))
                if same:
                    del log_[i]
                    break
        tmp = TASKVIM_FILE.with_name(TASKVIM_FILE.name + ".subscreen-tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, TASKVIM_FILE)
        _taskvim_cache["mtime"] = None
        return True, "reopened" if was else "done"


# ---------------------------------------------------------------------------
# Planner: the iPhone's calendar and tasks (plan/). Reads and edits taskvim's
# data.json the way taskvim itself does (src/app/actions.rs), so a running
# taskvim reloads it and Syncthing carries it to the other computer.
# ---------------------------------------------------------------------------

FEED_START = "2026-10-11"    # taskvim's own cut-off: feed entries before this semester are dropped
SECTIONS = ("important", "general", "agent")


class PlanError(Exception):
    """An edit the phone asked for that cannot be made; the message is shown there."""


def room_code(room):
    # "כיתה C-104 (מינוס) קמפוס המכללה" → "C-104", as taskvim's calendar::room_code
    for word in room.split():
        if word[:1].isascii() and word[:1].isalpha() and any(c.isdigit() for c in word):
            return word
    return room.replace("חדר לימוד :", "").replace("קמפוס המכללה", "").strip()


def yedion_feed():
    """The class timetable from taskvim's cached Yedion feed, oldest first."""
    try:
        text = YEDION_FILE.read_text()
    except OSError:
        return []
    lines = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line.lstrip()
        else:
            lines.append(line.rstrip("\r"))
    unescape = lambda v: v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    def when(v):
        v = v.strip().rstrip("Z")
        try:
            d = datetime.strptime(v[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return None
        return d, (f"{v[9:11]}:{v[11:13]}" if len(v) >= 13 else "00:00")
    out, fields = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            fields = {}
        elif line == "END:VEVENT":
            start = when(fields.get("DTSTART", "")) if fields is not None else None
            if start and fields.get("STATUS") != "CANCELLED" and start[0] >= FEED_START:
                end = when(fields.get("DTEND", "")) or start
                out.append({"id": fields.get("UID", ""), "title": unescape(fields.get("SUMMARY", "אירוע")),
                            "date": start[0], "start": start[1], "end": end[1],
                            "note": room_code(unescape(fields.get("LOCATION", "")))})
            fields = None
        elif fields is not None and ":" in line:
            name, value = line.split(":", 1)
            fields[name.split(";")[0].upper()] = value
    out.sort(key=lambda c: (c["date"], c["start"]))
    return out


def plan_data():
    """Everything the iPhone draws, in one document. Repeating events go as rules;
    the phone works out their days, as taskvim's calendar::occurs_on does."""
    try:
        st = TASKVIM_FILE.stat()
        data = json.loads(TASKVIM_FILE.read_text())
    except (OSError, ValueError):
        return {"error": "no taskvim data on this computer"}
    today = date.today()
    projects = {p["id"]: p for p in data.get("projects", [])}
    tasks = []
    for t in data.get("tasks", []):
        rep = t.get("repeat")
        done = bool(t.get("completed")) and (not rep or t.get("period") == habit_period_start(today, rep).isoformat())
        if done and not rep:
            continue
        tasks.append({"id": t["id"], "text": t.get("text", ""), "project": t.get("project"),
                      "due": None if rep else t.get("due"), "start": t.get("start_time"), "end": t.get("end_time"),
                      "section": t.get("dash_section") or "general", "habit": habit_label(rep) if rep else None,
                      "done": done})
    hidden = set(data.get("calendar_hidden_feed", []))
    edits = {e["id"]: e for e in data.get("calendar_class_edits", [])}
    classes = []
    for c in yedion_feed():
        if c["id"] in hidden or c["date"] < (today - timedelta(days=21)).isoformat():
            continue
        e = edits.get(c["id"], {})
        classes.append({**c, "title": e.get("title") or c["title"], "note": e.get("note") if e.get("note") is not None else c["note"],
                        "start": e.get("start_time") or c["start"], "end": e.get("end_time") or c["end"]})
    return {
        "host": socket.gethostname(),
        "version": st.st_mtime_ns,
        "projects": [{"id": p["id"], "name": p["name"], "color": PALETTE[p.get("color", 0) % len(PALETTE)],
                      "archived": bool(p.get("archived"))} for p in projects.values()],
        "tasks": tasks,
        "dashboard": data.get("dashboard", []),
        "events": [{"id": e["id"], "title": e.get("title", ""), "date": e.get("date"), "start": e.get("start_time", ""),
                    "end": e.get("end_time", ""), "note": e.get("note", ""), "repeat": e.get("repeat"),
                    "skip": e.get("skip", [])} for e in data.get("calendar_events", [])],
        "classes": classes,
    }


def _date(v, optional=True):
    if v in (None, "") and optional:
        return None
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise PlanError(f"not a date: {v}")


def _time(v, optional=True):
    if v in (None, "") and optional:
        return None
    try:
        return datetime.strptime(str(v), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise PlanError(f"not a time: {v}")


def _text(v, field="title"):
    v = " ".join(str(v or "").split())[:300]
    if not v:
        raise PlanError(f"give it a {field}")
    return v


def _span(req):
    """A start–end pair, both or neither; an end before the start becomes an hour after it."""
    start, end = _time(req.get("start")), _time(req.get("end"))
    if not start:
        return None, None
    if not end or end <= start:
        h, m = map(int, start.split(":"))
        end = f"{min(h + 1, 23):02d}:{m if h < 23 else 59:02d}"
    return start, end


def _repeat(v, first):
    if not v:
        return None
    unit = v.get("unit")
    if unit not in ("day", "week", "month"):
        raise PlanError("repeat: daily, weekly or monthly")
    until = _date(v.get("until"))
    if until and until < first:
        raise PlanError("it stops repeating before it starts")
    days = sorted({int(d) for d in v.get("days") or [] if 0 <= int(d) <= 6}) if unit == "week" else []
    return {"every": max(1, min(int(v.get("every") or 1), 99)), "unit": unit, "days": days, "until": until}


def _new_id(data):
    ids = [x.get("id", 0) for key in ("tasks", "projects", "sections", "log", "calendar_events") for x in data.get(key, [])]
    nid = max([data.get("next_id", 1)] + [i + 1 for i in ids])
    data["next_id"] = nid + 1
    return nid


def _find(items, i, what):
    found = next((x for x in items if x.get("id") == i), None)
    if found is None:
        raise PlanError(f"that {what} no longer exists")
    return found


def _task_add(data, req):
    due = _date(req.get("due"))
    start, end = _span(req)
    section = req.get("section") if req.get("section") in SECTIONS else "general"
    project = req.get("project") if any(p["id"] == req.get("project") for p in data.get("projects", [])) else None
    nid = _new_id(data)
    data["tasks"].append({"id": nid, "text": _text(req.get("text"), "name"), "project": project, "section": None,
                          "due": due, "start_time": start if due else None, "end_time": end if due else None,
                          "completed": False, "dash_section": section, "repeat": None, "period": None, "relink": False})
    data.setdefault("dashboard", []).append(nid)   # added from the calendar = linked to the dashboard, as in taskvim


def _task_set(data, req):
    t = _find(data["tasks"], req.get("id"), "task")
    if "text" in req:
        t["text"] = _text(req["text"], "name")
    if "project" in req:
        project = req["project"] if any(p["id"] == req["project"] for p in data.get("projects", [])) else None
        if project != t.get("project"):
            t["project"], t["section"] = project, None    # a course's sections are its own
    if "due" in req and not t.get("repeat"):     # a habit never carries a due date
        t["due"] = _date(req["due"])
    if "start" in req:
        t["start_time"], t["end_time"] = _span(req)
    if not t.get("due"):
        t["start_time"] = t["end_time"] = None


def _task_delete(data, req):
    t = _find(data["tasks"], req.get("id"), "task")
    data["tasks"].remove(t)
    data["dashboard"] = [i for i in data.get("dashboard", []) if i != t["id"]]


def _task_place(data, req):
    """Into a dashboard section, before another task (or last in it): taskvim's 1/2/3 and J/K."""
    t = _find(data["tasks"], req.get("id"), "task")
    section = req.get("section")
    if section not in SECTIONS:
        raise PlanError("no such section")
    t["dash_section"] = section
    order = [i for i in data.get("dashboard", []) if i != t["id"]]
    before = req.get("before")
    order.insert(order.index(before) if before in order else len(order), t["id"])
    data["dashboard"] = order


def _event_fields(req, ev):
    ev["title"] = _text(req.get("title"))
    ev["note"] = " ".join(str(req.get("note") or "").split())[:200]
    first = _date(req.get("date"), optional=False)
    start, end = _span(req)
    if not start:
        raise PlanError("an event needs a time")
    repeat = _repeat(req.get("repeat"), first)
    if ev.get("date") != first or ev.get("repeat") != repeat:
        ev["skip"] = []     # a new start or rule re-lays every occurrence
    ev.update(date=first, start_time=start, end_time=end, repeat=repeat)


def _event_add(data, req):
    ev = {"id": _new_id(data), "skip": []}
    _event_fields(req, ev)
    data.setdefault("calendar_events", []).append(ev)


def _event_set(data, req):
    _event_fields(req, _find(data.get("calendar_events", []), req.get("id"), "event"))


def _event_move(data, req):
    """Drag one occurrence to another day or time. A repeating event keeps its series;
    the moved day becomes an event of its own, like Google Calendar's "this event"."""
    ev = _find(data.get("calendar_events", []), req.get("id"), "event")
    day, start, end = _date(req.get("date"), optional=False), *_span(req)
    if not start:
        raise PlanError("an event needs a time")
    if not ev.get("repeat"):
        ev.update(date=day, start_time=start, end_time=end)
        return
    ev.setdefault("skip", []).append(_date(req.get("from"), optional=False))
    data["calendar_events"].append({"id": _new_id(data), "date": day, "title": ev["title"], "start_time": start,
                                    "end_time": end, "note": ev.get("note", ""), "repeat": None, "skip": []})


def _event_delete(data, req):
    ev = _find(data.get("calendar_events", []), req.get("id"), "event")
    if ev.get("repeat") and req.get("date"):
        ev.setdefault("skip", []).append(_date(req["date"]))   # just this occurrence
    else:
        data["calendar_events"].remove(ev)


def _class_set(data, req):
    """Local overrides of a feed class, keeping only what differs from the feed, as taskvim does."""
    cls = next((c for c in yedion_feed() if c["id"] and c["id"] == req.get("id")), None)
    if not cls:
        raise PlanError("that class is not in the feed any more")
    start, end = _span(req)
    want = {"title": " ".join(str(req.get("title") or "").split()) or cls["title"],
            "note": " ".join(str(req.get("note") or "").split()), "start_time": start or cls["start"], "end_time": end or cls["end"]}
    feed = {"title": cls["title"], "note": cls["note"], "start_time": cls["start"], "end_time": cls["end"]}
    edit = {"id": cls["id"], **{k: (v if v != feed[k] else None) for k, v in want.items()}}
    edits = [e for e in data.get("calendar_class_edits", []) if e.get("id") != cls["id"]]
    if any(edit[k] is not None for k in feed):
        edits.append(edit)
    data["calendar_class_edits"] = edits


def _class_hide(data, req):
    hidden = data.setdefault("calendar_hidden_feed", [])
    if req.get("id") and req["id"] not in hidden:
        hidden.append(req["id"])


PLAN_OPS = {
    "task.add": _task_add, "task.set": _task_set, "task.delete": _task_delete, "task.place": _task_place,
    "event.add": _event_add, "event.set": _event_set, "event.move": _event_move, "event.delete": _event_delete,
    "class.set": _class_set, "class.hide": _class_hide,
}


def plan_edit(req):
    if req.get("op") == "task.done":
        ok, msg = toggle_task(req.get("id"))
        if not ok:
            raise PlanError(msg)
        return
    fn = PLAN_OPS.get(req.get("op"))
    if not fn:
        raise PlanError("unknown edit")
    with _task_lock:
        data = json.loads(TASKVIM_FILE.read_text())
        fn(data, req)
        tmp = TASKVIM_FILE.with_name(TASKVIM_FILE.name + ".subscreen-tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, TASKVIM_FILE)
        _taskvim_cache["mtime"] = None


# ---------------------------------------------------------------------------
# dojo (read-only)
# ---------------------------------------------------------------------------

_dojo_cache = {"key": None, "value": None}


def dojo():
    try:
        key = (DOJO_DB.stat().st_mtime, Path(str(DOJO_DB) + "-wal").stat().st_mtime if Path(str(DOJO_DB) + "-wal").exists() else 0,
               datetime.now().strftime("%Y-%m-%d %H"))
    except OSError:
        return None
    if _dojo_cache["key"] == key:
        return _dojo_cache["value"]
    try:
        con = sqlite3.connect(f"file:{DOJO_DB}?mode=ro", uri=True, timeout=2)
        cur = con.cursor()
        xp = cur.execute("SELECT COALESCE(SUM(xp),0) FROM daily_stats").fetchone()[0]
        solved = cur.execute("SELECT COUNT(*) FROM problems WHERE status='solved'").fetchone()[0]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        due = cur.execute(
            "SELECT p.title, p.frontend_id, p.difficulty FROM reviews r JOIN problems p ON p.id = r.problem_id "
            "WHERE r.due_at <= ? ORDER BY r.due_at", (now,)).fetchall()
        since = (date.today() - timedelta(days=HEAT_DAYS)).isoformat()
        days = {d: [s, a] for d, s, a in cur.execute(
            "SELECT day, solved, attempts FROM daily_stats WHERE day >= ?", (since,))}
        last = cur.execute("SELECT MAX(day) FROM daily_stats WHERE solved > 0").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        log("dojo:", exc)
        return None
    value = {"xp": xp, "solved": solved, "due": [list(r) for r in due[:8]], "dueCount": len(due),
             "days": days, "lastSolve": last}
    _dojo_cache.update(key=key, value=value)
    return value


# ---------------------------------------------------------------------------
# Claude Code sessions (state files written by claude_hook.py)
# ---------------------------------------------------------------------------

def claude():
    out = []
    for f in CLAUDE_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        age = time.time() - s.get("t", 0)
        if age > 12 * 3600 or (s.get("state") == "idle" and age > 3 * 3600):
            try:
                f.unlink()
            except OSError:
                pass
            continue
        out.append({"dir": s.get("dir", ""), "state": s.get("state", "idle"),
                    "detail": s.get("detail", ""), "age": round(age)})
    rank = {"waiting": 0, "working": 1, "idle": 2}
    out.sort(key=lambda s: (rank.get(s["state"], 3), s["age"]))
    return out


# ---------------------------------------------------------------------------
# Prayer times, from the hypr panel's own modules
# ---------------------------------------------------------------------------

timetable = prayer = None
for _d in PANEL_DIRS:
    if (_d / "timetable.py").exists():
        sys.path.insert(0, str(_d))
        try:
            import prayer          # noqa: E402
            import timetable       # noqa: E402
        except Exception as exc:   # a broken panel module must not take the agent down
            log("prayer modules:", exc)
            timetable = prayer = None
        break


def prayer_days(n):
    if not timetable:
        return None
    today = date.today()
    out = []
    for i in range(max(1, min(n, 62))):
        day = today + timedelta(days=i)
        try:
            times, source, _ = timetable.times_for(day)
        except Exception:
            continue
        out.append({"date": day.isoformat(), "source": source,
                    "times": {k: v.strftime("%H:%M") for k, v in times.items()}})
    return {"days": out, "iqama": dict(prayer.IQAMA_OFFSETS),
            "names": dict(prayer.DISPLAY_NAMES), "arabic": dict(prayer.ARABIC_NAMES)}


def refresh_timetable_daily():
    while timetable:
        try:
            timetable.refresh()
        except Exception as exc:
            log("timetable refresh:", exc)
        time.sleep(6 * 3600)


# ---------------------------------------------------------------------------
# Actions: a fixed list, nothing arbitrary
# ---------------------------------------------------------------------------

def hypr_workspace(n):
    # This Hyprland may use the Lua config, which evaluates dispatch arguments as Lua.
    code, out = run(["hyprctl", "dispatch", f"hl.dsp.focus({{ workspace = {n} }})"])
    if code != 0 or "error" in out.lower() or "invalid" in out.lower():
        code, out = run(["hyprctl", "dispatch", "workspace", str(n)])
    return code == 0


def first(*names):
    for n in names:
        p = shutil.which(n) or (str(Path(n).expanduser()) if Path(n).expanduser().exists() else None)
        if p:
            return p
    return None


def mic_muted():
    code, out = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"])
    return code == 0 and "MUTED" in out


def screenshot():
    folder = Path(os.environ.get("XDG_PICTURES_DIR", HOME / "Pictures")) / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    code, _ = run(["grim", str(folder / f"phone-{datetime.now():%Y%m%d-%H%M%S}.png")], timeout=8)
    return code == 0


def toggle_quiet():
    cmd = first("omarchy-toggle-notification-silencing")
    if cmd:
        return run([cmd])[0] == 0
    return run(["makoctl", "mode", "-t", "do-not-disturb"])[0] == 0


ACTIONS = {
    **{f"ws{i}": (lambda i=i: hypr_workspace(i)) for i in range(1, 10)},
    "terminal": lambda: spawn([first("xdg-terminal-exec", "alacritty", "kitty", "foot")]),
    "browser": lambda: spawn([first("omarchy-launch-browser", "firefox", "chromium")]),
    "dojo": lambda: bool(first("~/projects/dojo/scripts/launch-dojo.sh")) and spawn(["sh", first("~/projects/dojo/scripts/launch-dojo.sh")]),
    "screenshot": screenshot,
    "mic": lambda: run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"])[0] == 0,
    "quiet": lambda: toggle_quiet(),
    "nightlight": lambda: bool(first("omarchy-toggle-nightlight")) and run([first("omarchy-toggle-nightlight")])[0] == 0,
    "lock": lambda: run(["loginctl", "lock-session"])[0] == 0,
    "playpause": lambda: run(["playerctl", "play-pause"])[0] == 0,
    "next": lambda: run(["playerctl", "next"])[0] == 0,
    "previous": lambda: run(["playerctl", "previous"])[0] == 0,
    "volup": lambda: run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"])[0] == 0,
    "voldown": lambda: run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"])[0] == 0,
}


# ---------------------------------------------------------------------------
# Who may run actions: the phones. Android and iOS peers on this tailnet are
# trusted automatically; the config can add more.
# ---------------------------------------------------------------------------

_phones = {"ips": set(), "at": 0}


def action_allowed(ip):
    if ip in ("127.0.0.1", "::1") or ip in CONFIG.get("allow_actions_from", []):
        return True
    if time.time() - _phones["at"] > 300:
        code, out = run(["tailscale", "status", "--json"], timeout=5)
        ips = set()
        if code == 0:
            try:
                for peer in (json.loads(out).get("Peer") or {}).values():
                    if (peer.get("OS") or "").lower() in ("android", "ios"):
                        ips.update(peer.get("TailscaleIPs") or [])
            except ValueError:
                pass
        _phones.update(ips=ips, at=time.time())
    return ip in _phones["ips"]


def read_allowed(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or any(addr in net for net in TAILNET) or ip in CONFIG.get("allow_reads_from", [])


# ---------------------------------------------------------------------------
# Push: task reminders, as ntfy notifications for the iPhone. The page does
# the same on the Android phone; iOS stops a page in the background, so here
# the computers do the remembering instead. Every computer
# with "ntfy" set pushes, so each one waits its own few seconds and checks the
# topic first: whatever the other computer already sent is not sent twice.
# ---------------------------------------------------------------------------

PUSH_WINDOW = timedelta(minutes=10)    # a step that is this late is skipped, so a restart never sends a backlog


def ntfy_request(url, body=None):
    req = Request(url, data=json.dumps(body).encode() if body else None, headers={"Content-Type": "application/json"})
    if CONFIG.get("ntfy_token"):
        req.add_header("Authorization", f"Bearer {CONFIG['ntfy_token']}")
    with urlopen(req, timeout=10) as r:
        return r.read().decode()


def ntfy_send(title, message, priority=3):
    # JSON to the server root rather than headers to the topic, so titles may be Arabic.
    server, _, topic = CONFIG["ntfy"].rstrip("/").rpartition("/")
    ntfy_request(server, {"topic": topic, "title": title, "message": message, "priority": priority})


def push(title, message, priority=3):
    # Wait this machine's own few seconds, then skip it if another computer already sent it.
    time.sleep(int(hashlib.sha1(socket.gethostname().encode()).hexdigest(), 16) % 20)
    try:
        recent = [json.loads(line) for line in ntfy_request(CONFIG["ntfy"].rstrip("/") + "/json?poll=1&since=20m").splitlines() if line]
        if any(m.get("title") == title and m.get("message") == message for m in recent):
            return
    except (OSError, ValueError) as exc:
        log("push: could not read the topic:", exc)
    try:
        ntfy_send(title, message, priority)
        log("pushed", title, "·", message)
    except OSError as exc:
        log("push failed:", exc)


def push_steps(now):
    """Everything worth a notification around now: (key, when, title, message, priority)."""
    steps = []
    for it in (taskvim() or {}).get("soon", []):
        name = f"{it['course']} · {it['text']}" if it.get("course") else it["text"]
        if "T" in it["at"]:
            at = datetime.fromisoformat(it["at"])
            event = it["kind"] == "event"
            heads, pre = (timedelta(hours=1), timedelta(minutes=10)) if event else (timedelta(hours=2), timedelta(minutes=15))
            verb = "Starts" if event else "Due"
            steps.append((f"{it['kind']}-{it['id']}-{at:%FT%H%M}-heads", at - heads, name,
                          f"{verb} at {at:%H:%M}", 3))
            steps.append((f"{it['kind']}-{it['id']}-{at:%FT%H%M}-pre", at - pre, name,
                          f"{verb} in {int(pre.total_seconds() // 60)} minutes · {at:%H:%M}", 4))
        elif it["at"] == now.date().isoformat():
            steps.append((f"{it['kind']}-{it['id']}-{it['at']}-today", now.replace(hour=8, minute=0, second=0, microsecond=0),
                          name, "Due today", 3))
    return steps


def push_loop():
    sent = set()
    while True:
        now = datetime.now()
        for key, at, title, message, priority in push_steps(now):
            if key not in sent and at <= now < at + PUSH_WINDOW:
                sent.add(key)
                threading.Thread(target=push, args=(title, message, priority), daemon=True).start()
        if len(sent) > 500:
            sent.clear()
        time.sleep(20)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

IDLE = Idle()
STATS = Stats()
_state_lock = threading.Lock()
_state = {"at": 0, "value": None}


def build_state():
    with _state_lock:
        if time.time() - _state["at"] < 1.0 and _state["value"]:
            return _state["value"]
        value = {
            "host": socket.gethostname(),
            "kind": CONFIG.get("kind") or ("Laptop" if glob.glob("/sys/class/power_supply/BAT*") else "Desktop"),
            "t": time.time(),
            "idle": round(IDLE.seconds(), 1),
            "idleMethod": IDLE.method,
            "stats": STATS.read(),
            "media": media(),
            "taskvim": taskvim(),
            "dojo": dojo(),
            "claude": claude(),
            "toggles": {"mic": mic_muted()},
            "prayer": bool(timetable),
            "actions": sorted(ACTIONS),
        }
        _state.update(at=time.time(), value=value)
        return value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "subscreen-agent"

    def _headers(self, code, ctype, length):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, If-None-Match")
        self.send_header("Access-Control-Expose-Headers", "ETag")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._headers(code, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._headers(204, "text/plain", 0)

    def client_ip(self):
        ip = self.client_address[0]
        ip = ip[7:] if ip.startswith("::ffff:") else ip
        # Behind `tailscale serve` every request arrives from loopback; the phone's
        # own address is the one tailscale adds as X-Forwarded-For.
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded and ipaddress.ip_address(ip).is_loopback:
            return forwarded.split(",")[-1].strip()
        return ip

    def do_GET(self):
        if not read_allowed(self.client_ip()):
            return self._json({"error": "not on the tailnet"}, 403)
        url = urlparse(self.path)
        try:
            if url.path == "/health":
                body = b"ok\n"
                self._headers(200, "text/plain", len(body))
                return self.wfile.write(body)
            if url.path == "/state":
                return self._json(build_state())
            if url.path == "/prayer":
                days = int(parse_qs(url.query).get("days", ["40"])[0])
                data = prayer_days(days)
                return self._json(data if data else {"error": "no prayer module on this machine"}, 200 if data else 404)
            if url.path.startswith("/scene/"):
                return self.send_scene(url.path[len("/scene/"):])
            if url.path == "/plan/data":
                return self._json(plan_data())
            if url.path in ("/", "/plan"):
                self.send_response(302)
                self.send_header("Location", "/plan/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            if url.path.startswith("/plan/"):
                path = (PLAN_DIR / (url.path[len("/plan/"):] or "index.html")).resolve()
                if path.is_relative_to(PLAN_DIR) and path.is_file() and path.suffix in PLAN_TYPES:
                    return self.send_file(path, PLAN_TYPES[path.suffix])
        except Exception as exc:  # a broken source must not kill the server
            log("GET", url.path, "failed:", repr(exc))
            return self._json({"error": str(exc)}, 500)
        self._json({"error": "not found"}, 404)

    def send_scene(self, name):
        if not name.isascii() or not name.replace("-", "").isalnum():
            return self._json({"error": "bad name"}, 400)
        path = next((SCENE_DIR / (name + ext) for ext in SCENE_TYPES if (SCENE_DIR / (name + ext)).is_file()), None)
        if not path:
            return self._json({"error": f"no {name} in {SCENE_DIR}"}, 404)
        self.send_file(path, SCENE_TYPES[path.suffix.lower()])

    def send_file(self, path, ctype):
        st = path.stat()
        tag = f'"{path.suffix[1:]}-{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "ETag")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", tag)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "ETag")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        with open(path, "rb") as fp:
            shutil.copyfileobj(fp, self.wfile, 1 << 20)

    def do_POST(self):
        ip = self.client_ip()
        route = urlparse(self.path).path
        # The body is read before anything is refused: `tailscale serve` reuses the
        # connection, and an unread body would be taken for the next request.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 65536:
            self.close_connection = True
            return self._json({"error": "bad request"}, 400)
        body = self.rfile.read(length)
        if route not in ("/action", "/task", "/plan/edit"):
            return self._json({"error": "not found"}, 404)
        if not action_allowed(ip):
            log("refused action from", ip)
            return self._json({"error": "actions are only accepted from the phone"}, 403)
        try:
            req = json.loads(body[:4096] or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if route == "/plan/edit":
            try:
                plan_edit(req)
            except PlanError as exc:
                return self._json({"error": str(exc)}, 400)
            except (OSError, ValueError) as exc:
                log("plan edit", req.get("op"), "failed:", repr(exc))
                return self._json({"error": str(exc)}, 500)
            log("plan", req.get("op"), "from", ip)
            with _state_lock:
                _state["at"] = 0
            return self._json(plan_data())
        if route == "/task":
            try:
                ok, msg = toggle_task(int(req.get("id")))
            except (OSError, ValueError, TypeError) as exc:
                ok, msg = False, str(exc)
            log("task", req.get("id"), "from", ip, msg)
            with _state_lock:
                _state["at"] = 0
            return self._json({"ok": ok, "result": msg} if ok else {"ok": False, "error": msg})
        fn = ACTIONS.get(req.get("id"))
        if not fn:
            return self._json({"error": "unknown action"}, 404)
        ok = bool(fn())
        log("action", req.get("id"), "from", ip, "ok" if ok else "FAILED")
        with _state_lock:
            _state["at"] = 0
        self._json({"ok": ok})

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6     # dual stack: MagicDNS names resolve to Tailscale's IPv6 too

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # HTTPServer.server_bind does a reverse DNS lookup of the bind address,
        # which stalls ~5 s on 0.0.0.0. The name is never used.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "subscreen", self.server_address[1]


def main():
    if "--test-push" in sys.argv:
        if not CONFIG.get("ntfy"):
            sys.exit(f"no \"ntfy\" in {CONFIG_FILE}")
        ntfy_send("Subscreen", "If this reached the iPhone, task reminders will too.")
        return print("sent to", CONFIG["ntfy"])
    port = int(CONFIG.get("port", 8765))
    server = Server((CONFIG.get("bind", "::"), port), Handler)
    threading.Thread(target=refresh_timetable_daily, daemon=True).start()
    if CONFIG.get("ntfy"):
        threading.Thread(target=push_loop, daemon=True).start()
    log(f"subscreen agent on :{port} · idle via {IDLE.method} · prayer {'yes' if timetable else 'no'}"
        f" · taskvim {'yes' if TASKVIM_FILE.exists() else 'no'} · dojo {'yes' if DOJO_DB.exists() else 'no'}"
        f" · push {'yes' if CONFIG.get('ntfy') else 'no'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
