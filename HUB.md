# Subscreen hub

The old phone (a Galaxy A73) as a clock and dashboard for the PC and the laptop.

```
 PC ── agent/subscreen_agent.py ─┐
                                 ├─ Tailscale ─► phone: Subscreen app (app/ + web/)
 laptop ── agent/subscreen_agent.py ┘
```

- **agent/**: a small Python service (standard library only) that runs on every
  computer and serves that machine's state as JSON on port 8765: idle time, CPU/GPU/RAM/network,
  what is playing, taskvim, dojo, Claude Code sessions, prayer times. It also runs a fixed set
  of actions (workspaces, terminal, dojo, screenshot, mic, lock, media) when the phone asks.
  Reads are open to the tailnet; actions are accepted only from Android devices on it.
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
