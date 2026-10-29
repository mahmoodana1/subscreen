# Repository Guidelines

## Project Structure & Module Organization

This repository turns an old Android phone into a clock and dashboard for the user's computers, with an iPhone calendar and tasks app beside it. `HUB.md` and `MINI.md` describe the setup in full.

- `agent/`: `subscreen_agent.py`, a standard-library Python service that runs on every computer (and on the Mac mini hub) and serves that machine's state as JSON on port 8765. `install.sh` installs it as a systemd user service, `claude_hook.py` feeds Claude Code session state to it, and `hub-linux.sh` / `hub-pair.sh` set up the hub.
- `web/`: the page the phone shows, including its fonts and `athan.mp3`.
- `app/`: the Android wrapper around `web/`, built with Gradle (`build.gradle`, `settings.gradle`, `gradle.properties`).
- `plan/`: the iPhone's Plan app, served by the agent.
- `mockup/`: a standalone web mockup of the phone page.

The agent imports `prayer.py` and `timetable.py` from the local Hypr panel configuration (`~/.config/hypr/panel`), so prayer times need those files on the machine.

## Build, Test, and Development Commands

```bash
./agent/install.sh                         # install/update and restart the agent
python3 agent/subscreen_agent.py           # run the agent in the foreground
curl -fsS http://127.0.0.1:8765/state      # check what the phone will see
python3 -m py_compile agent/*.py           # basic syntax check
gradle :app:assembleRelease                # build the APK locally (CI does this on push)
```

GitHub Actions (`.github/workflows/apk.yml`) builds the APK on every push to `hub` or `master` that touches `app/` or `web/`, and publishes it as the `latest` release.

## Coding Style & Naming Conventions

Use Python 3 with four-space indentation, `snake_case` for functions and variables, and uppercase names for constants. Keep standard-library imports grouped near the top, preserve executable shebangs, and keep shell scripts Bash-compatible with quoted paths and arguments. No formatter or linter is configured; keep changes consistent with the surrounding code and use `python3 -m py_compile` as a basic syntax check.

## Testing Guidelines

No automated tests or coverage requirements are currently present. For changes, perform a syntax check, run the agent, and exercise the affected endpoint or page manually. Do not commit generated `__pycache__` files, screenshots, or other ignored diagnostics.

## Commit & Pull Request Guidelines

The existing history uses short, imperative-style summaries (for example, `Phone: pages stay put; tasks, courses and the agenda scroll in their own boxes`). Follow that pattern and keep each commit focused. Pull requests should describe the user-visible behavior, identify the agent, web, app or Plan files touched, include manual verification commands, and attach screenshots for layout changes. Call out any machine-specific path or configuration assumptions.

## Configuration and Safety

The agent runs actions on the computer when the phone asks. Reads are open to the tailnet and actions are accepted only from Android and iOS devices on it; keep it that way before exposing it further, and do not commit personal paths, device addresses, or credentials.
