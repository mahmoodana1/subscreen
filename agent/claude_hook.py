#!/usr/bin/env python3
"""Claude Code hook: record each session's state for the phone's Claude page.

Registered for UserPromptSubmit, PostToolUse, Notification, Stop and
SessionEnd. Writes one small JSON file per session into
~/.cache/subscreen/claude/, which the agent reads. Never prints and never
fails loudly: a hook that errors would get in Claude's way.
"""

import json
import os
import sys
import time
from pathlib import Path

DIR = Path.home() / ".cache/subscreen/claude"


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    sid = "".join(c for c in str(event.get("session_id", "")) if c.isalnum() or c == "-")[:64]
    if not sid:
        return
    path = DIR / f"{sid}.json"
    name = event.get("hook_event_name", "")
    if name == "SessionEnd":
        path.unlink(missing_ok=True)
        return

    cwd = event.get("cwd") or ""
    home = str(Path.home())
    shown = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    if name == "UserPromptSubmit":
        state, detail = "working", "Thinking"
    elif name == "PostToolUse":
        state, detail = "working", f"Ran {event.get('tool_name', 'a tool')}"
    elif name == "Notification":
        kind = event.get("notification_type") or ""
        msg = (event.get("message") or "").strip()
        if kind == "idle_prompt" or "waiting for your input" in msg.lower():
            state, detail = "idle", "Waiting for your next message"
        else:
            state, detail = "waiting", msg or "Needs your permission"
    elif name == "Stop":
        state, detail = "idle", "Finished"
    else:
        return

    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"dir": shown, "state": state, "detail": detail[:140], "t": time.time()}))
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
