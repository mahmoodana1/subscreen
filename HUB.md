# Subscreen hub

The old phone (a Galaxy A73) as a clock and dashboard for the PC and the laptop.

```
 PC ── agent/subscreen_agent.py ─┐
                                 ├─ Tailscale ─► phone: Subscreen app (app/ + web/)
 laptop ── agent/subscreen_agent.py ┘            iPhone: Plan (plan/), Shortcuts, ntfy
```

- **agent/**: a small Python service (standard library only) that runs on every
  computer and serves that machine's state as JSON on port 8765: idle time, CPU/GPU/RAM/network,
  what is playing, taskvim, dojo, Claude Code sessions, prayer times. It also runs a fixed set
  of actions (workspaces, terminal, dojo, screenshot, mic, lock, media) when the phone asks.
  Reads are open to the tailnet; actions are accepted only from Android and iOS devices on it.
  It also serves the iPhone's Plan app and edits taskvim for it, and can push task reminders through ntfy.
- **web/**: the page the phone shows. It polls every computer, and the one you touched last
  becomes the active one. Prayer times are cached for weeks, so the clock, the prayer
  countdown and the athan keep working with every computer off.
- **app/**: an Android wrapper around web/: full screen, landscape, screen kept on, starts
  after a reboot, plays the athan natively. GitHub Actions builds it on every push and
  publishes `subscreen.apk` as the `latest` release.

## Set up a computer

```sh
git clone https://github.com/mahmoodana1/subscreen ~/projects/subscreen   # or git pull
cd ~/projects/subscreen && git switch hub
./agent/install.sh
```

This installs the `subscreen-agent` systemd user service and a Claude Code hook
(`agent/claude_hook.py`) that feeds the Claude page. `./agent/install.sh --remove` undoes both.
For idle detection the user should be in the `input` group.

The **Lumière** scene plays a clip or picture instead of a drawn sky: put `lumiere.mp4` (or `.webm`,
`.jpg`, `.png`, `.webp`) in `~/.config/subscreen/scenes/` on any computer. The phone downloads it once,
keeps it, and fetches it again only when the file changes. It never goes in the repo.

## Set up the phone

1. Open the [latest release](https://github.com/mahmoodana1/subscreen/releases/tag/latest)
   on the phone, download `subscreen.apk`, and install it.
2. Keep Tailscale on.
3. In the app's menu (top right): **Use as the home screen** makes it what the phone shows
   after every boot; **Start after a reboot** is the alternative if you keep another launcher.
4. The menu's **Computers** field lists the agents, `omarchy:8765, archlinux:8765` by default.

New versions install over the old one; the signing key is in the repo's Actions secrets.

## The iPhone: Plan

The iPhone has its own app, **Plan** (`plan/`): taskvim's calendar and tasks, to read and edit. It is not
the subscreen. The Calendar tab is a week strip over the chosen day: Yedion classes, your events,
timed tasks, then that day's tasks without a time (and, today, the overdue dashboard tasks). The
Tasks tab is taskvim's dashboard: Important, General, Agent.

- Tap **+** to add a task or an event; tap anything to edit it, tick it, or delete it.
- Hold a row, then drag: up and down moves its time by quarter hours, onto a day in the strip
  moves it to that day. On the Tasks tab, dragging moves a task between and within the sections.
- Dragging one day of a repeating event moves just that day (it becomes an event of its own).
  Classes come from Yedion: their time, title and room can be changed locally, or a meeting hidden,
  exactly as in taskvim.

Every edit goes through an agent (`POST /plan/edit`) into taskvim's `data.json`, the way taskvim
itself would make it; a running taskvim reloads the file and Syncthing carries it to the other
computer. Only phones may edit. With every computer off the app still opens and shows what it last
saw, read-only.

**Set up.** Plan needs HTTPS (iOS only keeps an app's copy for HTTPS pages). On each computer, once:

```sh
sudo tailscale set --operator=$USER    # so serve works without sudo
tailscale serve --bg 8765               # https://<name>.<tailnet>.ts.net → the agent; survives reboots
```

Then on the iPhone, with Tailscale on, open `https://omarchy.<tailnet>.ts.net/plan/` in Safari
(`tailscale status --json` shows the name under `CertDomains`) and Share › **Add to Home Screen**.

**Reminders.** Put one ntfy topic in `~/.config/subscreen/agent.json` on every computer (the same
topic everywhere, a long random name: anyone who knows it can read it):

```json
{"ntfy": "https://ntfy.sh/subscreen-<random>"}
```

Restart the agent (`systemctl --user restart subscreen-agent`), install the **ntfy** app and
subscribe to that topic, then check with `python3 agent/subscreen_agent.py --test-push`. The agents
push a timed task two hours and fifteen minutes before it is due (an event one hour and ten minutes
before), and a dated task at 08:00 on its day; when both computers are on, the second sees the
first's message on the topic and does not send it again. Task names go through ntfy.sh; a
self-hosted ntfy server works the same (`"ntfy_token"` if it needs one).

**Shortcuts.** Each control is one *Get Contents of URL* action: URL
`https://omarchy.<tailnet>.ts.net/action`, Method **POST**, Request Body **JSON** with `id` set to
one of `lock`, `playpause`, `next`, `previous`, `volup`, `voldown`, `mic`, `quiet`, `nightlight`,
`screenshot`, `terminal`, `browser`, `dojo`, `ws1`…`ws9`. iOS devices on the tailnet are trusted.
