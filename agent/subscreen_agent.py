#!/usr/bin/env python3
"""Subscreen agent: the desktop half of the phone hub.

Runs on every machine the phone should know about (the PC and the laptop),
as a systemd user service. It reads what is already on the machine and
serves it as JSON to the Subscreen app on the phone, over Tailscale:

    GET  /health            "ok"
    GET  /state             everything the phone shows, one JSON document
    GET  /prayer?days=N     the next N days of prayer times (the phone caches them)
    POST /action            {"id": "ws2"} runs one of a fixed set of actions

Sources, each optional — a machine without dojo simply reports none:
    input idle      /dev/input (the user must be in the "input" group),
                    falling back to watching the Hyprland cursor
    system stats    /proc, hwmon, nvidia-smi or amdgpu
    media           playerctl
    taskvim         ~/.local/share/taskvim/data.json (read-only)
    dojo            ~/.local/share/dojo/dojo.db (read-only)
    claude          ~/.cache/subscreen/claude/*.json, written by claude_hook.py
    prayer          the hypr panel's prayer.py / timetable.py

Reads are open to the tailnet. Actions are accepted only from the phone:
Android peers on the tailnet, or the addresses in the config file.

Standard library only. Config (optional): ~/.config/subscreen/agent.json
    {"port": 8765, "kind": "Desktop", "allow_actions_from": ["100.x.y.z"]}
"""

import glob
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

HOME = Path.home()
CONFIG_FILE = HOME / ".config/subscreen/agent.json"
TASKVIM_FILE = HOME / ".local/share/taskvim/data.json"
DOJO_DB = HOME / ".local/share/dojo/dojo.db"
CLAUDE_DIR = HOME / ".cache/subscreen/claude"
PANEL_DIRS = [HOME / ".config/hypr/panel", HOME / "dotfiles/hypr/.config/hypr/panel"]
HEAT_DAYS = 16 * 7
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
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

    value = {
        "mtime": mtime,
        "groups": groups,
        "goal": {"target": max(1, int(data.get("daily_goal") or 2)), "done": counts.get(today.isoformat(), 0)},
        "streak": streak,
        "heat": heat,
        "courses": courses,
        "next": [{"title": title, "at": at.isoformat(timespec="minutes")} for at, title in upcoming[:3]],
    }
    _taskvim_cache.update(mtime=mtime, value=value, day=today)
    return value


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
# Who may run actions: the phone. Android peers on this tailnet are trusted
# automatically; the config can add more.
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
                    if (peer.get("OS") or "").lower() == "android":
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        return ip[7:] if ip.startswith("::ffff:") else ip

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
        except Exception as exc:  # a broken source must not kill the server
            log("GET", url.path, "failed:", repr(exc))
            return self._json({"error": str(exc)}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        ip = self.client_ip()
        if urlparse(self.path).path != "/action":
            return self._json({"error": "not found"}, 404)
        if not action_allowed(ip):
            log("refused action from", ip)
            return self._json({"error": "actions are only accepted from the phone"}, 403)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
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
    port = int(CONFIG.get("port", 8765))
    server = Server((CONFIG.get("bind", "::"), port), Handler)
    threading.Thread(target=refresh_timetable_daily, daemon=True).start()
    log(f"subscreen agent on :{port} · idle via {IDLE.method} · prayer {'yes' if timetable else 'no'}"
        f" · taskvim {'yes' if TASKVIM_FILE.exists() else 'no'} · dojo {'yes' if DOJO_DB.exists() else 'no'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
