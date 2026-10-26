#!/usr/bin/env bash
# Install the subscreen agent on this machine (PC or laptop) as a systemd user
# service, and register the Claude Code hook that feeds the phone's Claude page.
#
#   ./agent/install.sh            install / update, then (re)start the service
#   ./agent/install.sh --no-hooks skip the Claude Code hook
#   ./agent/install.sh --headless run without a desktop session (the Mac mini hub; needs lingering,
#                                 which agent/hub-linux.sh turns on)
#   ./agent/install.sh --remove   stop the service and remove the hook
#
# Safe to run again: it only rewrites its own unit and its own hook entries.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/subscreen-agent.service"
SETTINGS="$HOME/.claude/settings.json"
HOOK="$HERE/claude_hook.py"

hooks() {   # $1 = add | remove
    [ -f "$SETTINGS" ] || { [ "$1" = add ] && mkdir -p "$(dirname "$SETTINGS")" && echo '{}' > "$SETTINGS"; } || return 0
    python3 - "$SETTINGS" "$HOOK" "$1" <<'PY'
import json, sys, shutil
path, hook, mode = sys.argv[1:]
with open(path) as f:
    data = json.load(f)
cmd = f"python3 {hook}"
hooks = data.setdefault("hooks", {})
for event in ("UserPromptSubmit", "PostToolUse", "Notification", "Stop", "SessionEnd"):
    groups = [g for g in hooks.get(event, []) if not any("claude_hook.py" in h.get("command", "") for h in g.get("hooks", []))]
    if mode == "add":
        groups.append({"hooks": [{"type": "command", "command": cmd, "async": True}]})
    if groups:
        hooks[event] = groups
    else:
        hooks.pop(event, None)
shutil.copy(path, path + ".bak-subscreen")
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(("added" if mode == "add" else "removed"), "the Claude Code hook in", path)
PY
}

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now subscreen-agent.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    hooks remove
    echo "subscreen agent removed"
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
[[ " $* " == *" --headless "* ]] || id -nG | grep -qw input || echo "note: $USER is not in the 'input' group; idle detection will fall back to the cursor (sudo usermod -aG input $USER, then log in again)"

# The old serve.py service holds the same port 8765, so the agent could never start.
if [ -f "$UNIT_DIR/subscreen.service" ]; then
    systemctl --user disable --now subscreen.service 2>/dev/null || true
    rm -f "$UNIT_DIR/subscreen.service"
    echo "removed the old subscreen.service (serve.py), which held port 8765"
fi

HEADLESS=""
NO_HOOKS=""
for arg in "$@"; do
    case "$arg" in
        --headless) HEADLESS=1 ;;
        --no-hooks) NO_HOOKS=1 ;;
    esac
done
# A desktop starts the agent with the graphical session; a headless hub starts it at boot.
if [ -n "$HEADLESS" ]; then
    after=network-online.target; partof=""; target=default.target
else
    after="graphical-session.target network-online.target"; partof="PartOf=graphical-session.target"; target=graphical-session.target
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Subscreen agent (serves this machine's state to the phone)
After=$after
$partof

[Service]
ExecStart=/usr/bin/env python3 $HERE/subscreen_agent.py
Restart=always
RestartSec=3

[Install]
WantedBy=$target
EOF

systemctl --user daemon-reload
systemctl --user disable subscreen-agent.service >/dev/null 2>&1 || true   # drop the old target's link
systemctl --user enable subscreen-agent.service >/dev/null 2>&1
systemctl --user restart subscreen-agent.service
[ -n "$NO_HOOKS" ] || hooks add

ok=""
for _ in $(seq 1 30); do
    curl -fsS --max-time 1 http://127.0.0.1:8765/health >/dev/null 2>&1 && { ok=1; break; }
    sleep 0.5
done
if [ -n "$ok" ]; then
    echo "subscreen agent is running on port 8765"
    ip=$(tailscale ip -4 2>/dev/null | head -1)
    [ -n "$ip" ] && echo "the phone reaches it at $(hostname):8765 ($ip)"
else
    echo "the agent did not answer; see: journalctl --user -u subscreen-agent -n 30" >&2
    exit 1
fi
