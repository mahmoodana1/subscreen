# The Mac mini hub

The Mac mini (2012, i5/i7 Ivy Bridge, 8 GB, 512 GB, Omarchy) is the always-on hub. It runs headless,
with no desktop, and does four things:

1. **taskvim**: keeps its own copy of taskvim's folder (`~/.local/share/taskvim`, Syncthing folder
   `taskvim-data`), with 30 days of old versions, so the computers catch up from it when they wake. taskvim
   itself is installed too, for use over ssh.
2. **The phone server**: runs `agent/subscreen_agent.py` at boot as kind `Hub`. The phone's subscreen
   uses it only when no computer answers: prayer times, taskvim, the calendar, reminders.
3. **Calendar and tasks**: serves the iPhone's **Plan** app (`plan/`) over HTTPS at
   `https://macmini.<tailnet>.ts.net/plan/`, with every computer off.
4. **Reminders**: pushes task reminders through ntfy, like the computers (they never send one twice).

The machine is slow and dusty, so it does not build heavy projects or run long jobs.

## For Claude Code on the mini

You are on the hub. Set it up by following the steps below, in order, checking each one before the next.
The other machines are **omarchy** (the desktop PC) and **archlinux** (the laptop). Don't change them from here.
Anything they need, give the user as commands to run there.

Rules:
- **Never run taskvim's tests** (`cargo test`). Old versions wrote a test fixture over the real `data.json`
  and Syncthing spread it to every machine. Only `cargo build`.
- **Don't start `taskvim` here before the first sync is done** (`data.json` has arrived from a computer).
  A fresh `data.json` made here could win over the real one.
- **Don't stow the dotfiles here.** `~/dotfiles` is only cloned so the agent can read the prayer-time modules.
- Don't share any other Syncthing folder (Documents, college files) until the user says so (see the last section).
- The script uses `sudo`. If you can't answer sudo prompts, ask the user to run it with `! <command>`.

## 1. Before anything

- Ethernet if possible; Wi-Fi works too (iwd keeps it up without anyone logged in).
- Check the time zone: `timedatectl`. It must be the user's (prayer times and reminders depend on it).
- Get the repo:
  ```sh
  git clone -b hub https://github.com/mahmoodana1/subscreen ~/projects/subscreen
  ```
  (or `git -C ~/projects/subscreen pull` if it's already there).

## 2. Run the setup

```sh
~/projects/subscreen/agent/hub-linux.sh
```

It's safe to rerun, and it does all of this:
- installs python, git, syncthing, tailscale, lm_sensors, rust; on the Mac mini, `mbpfan` (fan control)
  and a service that powers it back on after a power cut
- names the machine `macmini` (`HUB_NAME=...` to change it), masks sleep and suspend, enables lingering
  so user services run from boot with nobody logged in, and boots to a text console from then on
  (`KEEP_DESKTOP=1` skips that; undo with `sudo systemctl set-default graphical.target`)
- signs in to Tailscale as `macmini`, with Tailscale SSH on. **The user must open the printed link.**
- creates the `taskvim-data` Syncthing folder (one-second send delay, staggered versioning, 30 days)
- clones the dotfiles (only for the prayer modules), installs the agent as a headless user service
  with `kind: "Hub"`, and serves it with `tailscale serve --bg 8765`
- builds taskvim from the `phone-sync` branch into `~/.local/bin/taskvim` (`NO_TASKVIM=1` skips it)

At the end it prints the Plan URL and the mini's Syncthing id.

## 3. Pair taskvim's folder

The user runs this **on the PC and on the laptop**, with the id the script printed:

```sh
cd ~/projects/subscreen && git pull && ./agent/hub-pair.sh <MINI-ID> macmini
```

Each one prints a line to run back **here**:

```sh
~/projects/subscreen/agent/hub-linux.sh --pair <COMPUTER-ID>
```

Then wait for the sync: `hub-linux.sh --status` should show the peers connected, the folder `idle` with 0
files to fetch, and a `data.json` the same size as the PC's. Only now is `taskvim` safe to start here.

## 4. Reminders (ntfy)

The topic is a secret, so it's not in the repo. Ask the user for the `"ntfy"` line from the PC's
`~/.config/subscreen/agent.json` (`cat ~/.config/subscreen/agent.json` there) and add it to this
machine's `~/.config/subscreen/agent.json`, keeping `"kind": "Hub"`. Then:

```sh
systemctl --user restart subscreen-agent
python3 ~/projects/subscreen/agent/subscreen_agent.py --test-push   # the iPhone should get a notification
```

## 5. The phones

- **Android (the subscreen app):** in the app's menu, set **Computers** to
  `omarchy:8765, archlinux:8765, macmini:8765` (new installs already have it). Update the app from the menu:
  the page now treats a `Hub` as active only when no computer answers, and the hub never wakes it from rest.
- **iPhone (Plan):** open `https://macmini.<tailnet>.ts.net/plan/` in Safari (the script printed it) and
  Share › **Add to Home Screen**, replacing the old icon that pointed at omarchy. The app reaches the
  computers too when they're on.
- iPhone **Shortcuts** can keep pointing at omarchy: those control the PC, not the hub.

## 6. Check it all

- `~/projects/subscreen/agent/hub-linux.sh --status`: agent, Syncthing, tailscaled and mbpfan active;
  the agent answers; `tailscale serve status` shows `https://macmini…` → `127.0.0.1:8765`.
- `curl -s localhost:8765/state | python3 -m json.tool | head -40`: `"kind": "Hub"`, taskvim tasks
  present, `"prayer": true`; `curl -s localhost:8765/prayer | head -c 300` shows today's times.
- `curl -s localhost:8765/plan/data | head -c 400`: tasks, events, and the Yedion classes (`yedion.ics`
  arrives with taskvim's folder).
- Reboot (`sudo reboot`), wait, then `ssh macmini` from another machine and run `--status` again.
  Everything must come back by itself, with nobody logged in.
- Temperatures: `sensors`. At idle it should stay under about 60 °C. If it's much hotter or the fan
  never speeds up, check `systemctl status mbpfan` and tell the user the heatsink needs cleaning.

## Updating

```sh
git -C ~/projects/subscreen pull && ~/projects/subscreen/agent/hub-linux.sh
```

## Later, only when the user asks: documents and college files

The user may want the mini to hold Documents (college files included) as a third copy. Today `~/Documents`
(Syncthing folder `documents`) is sent by the laptop and only received by the PC. To add the mini:
create the folder here with the same id, set it to send-receive with staggered versioning, and
share it with the laptop (and the PC). Ask before doing this. Also say it plainly: **the hub is not a backup**.
One old disk in one room needs an off-site copy too (restic or Backblaze B2, for example).
