# Repository Guidelines

## Project Structure & Module Organization

This repository is a lightweight desktop/phone “sub-screen” utility rather than a packaged application. The main Python entry points are:

- `serve.py`: desktop HTTP server exposing status and control endpoints.
- `subscreen_tui.py`: terminal UI client that renders the phone-facing display.
- `make_chime.py`: generates the tracked `chime.wav` asset.
- `patch*.py`: one-off maintenance scripts for adjusting local behavior.

The Bash entry points (`subscreen.sh`, `subscreen-x11.sh`, and `phonectl.sh`) launch or control the system. `prayer.py` and `timetable.py` are symlinks into a local Hypr panel configuration, so those integrations require the corresponding files on the developer’s machine. There is no dedicated source, test, or asset directory.

## Build, Test, and Development Commands

There is no build system or dependency manifest. Use the scripts directly:

```bash
python3 serve.py --help       # inspect server options
python3 serve.py --port 8080 # start the desktop server
./subscreen-x11.sh            # launch the X11 desktop flow
./subscreen.sh                # launch the Termux/phone flow
./phonectl.sh                 # invoke phone control actions
python3 -m py_compile serve.py subscreen_tui.py make_chime.py
```

Run the server and client in separate terminals when manually testing network behavior. Avoid running the TUI in non-interactive CI shells.

## Coding Style & Naming Conventions

Use Python 3 with four-space indentation, `snake_case` for functions and variables, and uppercase names for constants. Keep standard-library imports grouped near the top, preserve executable shebangs, and keep shell scripts POSIX/Bash-compatible with quoted paths and arguments. No formatter or linter is configured; keep changes consistent with the surrounding code and use `python3 -m py_compile` as a basic syntax check.

## Testing Guidelines

No automated tests or coverage requirements are currently present. For changes, perform a syntax check, start `serve.py`, and exercise the affected endpoint or UI path manually. Do not commit generated `__pycache__` files, screenshots, or other ignored diagnostics.

## Commit & Pull Request Guidelines

The existing history uses short, imperative-style summaries (for example, `Add the phone sub-screen: desktop server, phone clients, control script`). Follow that pattern and keep each commit focused. Pull requests should describe the user-visible behavior, identify desktop/phone scripts touched, include manual verification commands, and attach screenshots for layout changes. Call out any machine-specific symlink or configuration assumptions.

## Configuration and Safety

The server can expose local control functionality over the bound interface. Review `--bind` and `--port` before sharing it beyond the trusted local network, and do not commit personal paths, device addresses, or credentials.
