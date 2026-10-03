# XR Head Aim

Aim in games by turning your head with XR glasses. Head aim adds to your mouse or controller; it doesn't replace them.

An [Omarchy](https://omarchy.org) bar widget with a small background service. It reads your glasses' head tracking through [XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver) and turns head movement into mouse movement or a gamepad right stick. It only does this while a game window has focus.

![XR Head Aim on the Omarchy desktop](preview.png)

## What it does

- **Low lag.** The glasses' IMU is read directly at about 115 Hz, with no generic smoothing filter. On fast moves the head speed is estimated from the latest three samples, so the aim follows the current motion rather than motion from half a sample ago. Output is written as soon as a sample arrives.
- **Accurate mouse mode.** Mouse movement is exactly head movement × sensitivity. Fractions of a count carry over, so nothing is lost or added. It works in almost any mouse-aim game.
- **Your PS5 controller's right stick (optional).** With the controller bridge, a DualSense (USB or Bluetooth) shows up in every game as a plain Xbox 360 pad, and head aim is mixed into its real right stick, so the stick and your head work together. Rumble works, and the pad survives Bluetooth drops mid-game. When no controller is connected, head aim moves the mouse instead (**Auto** output, the default).
- **Virtual pad mode.** This creates a virtual Xbox-style pad and drives its right stick. It is linear in turn rate and skips the game's stick dead zone, the way Steam Input's "gyro to joystick camera" does.
- **Holds still when you do.** A small dead zone and tremor smoothing apply only to very slow, near-still motion, so real turns get no added lag. A precision curve makes slow, small head moves finer.
- **Only in games.** By default it only acts while a Steam game window (`steam_app_*`) has focus, so it never moves your desktop pointer. You can add other window classes or exclude specific games.
- **Live tuning from the bar.** Turn it on or off, pick the output mode, and adjust sensitivity, vertical ratio, precision, dead zone, smoothing and lag removal. Changes apply instantly.

![Tuning panel](screenshots/panel.png)

## Requirements

- Omarchy (Hyprland + the Omarchy shell)
- XR glasses supported by [XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver#supported-devices) (VITURE, XREAL, Rokid, RayNeo). It has been tested on a **VITURE Luma Pro**; other glasses should work the same way but haven't been tested.
- `python3` and `python-evdev` (`sudo pacman -S python-evdev`)
- Write access to `/dev/uinput`. Steam's udev rule usually gives you this already; if not, `install.sh` offers to add one.

## Install

1. Install [XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver) and plug in your glasses once so it creates `~/.config/xr_driver/config.ini`. No other tracking software is needed: the head tracking comes from the glasses' own IMU.

2. Add the plugin and run its setup:

   ```bash
   omarchy plugin add https://github.com/R88800/xr-head-aim --enable
   ~/.config/omarchy/plugins/io.github.r88800.xr-head-aim/install.sh
   ```

   `install.sh` does the following:
   - checks the dependencies
   - adds a udev rule for `/dev/uinput` only if you don't already have access (this is the only step that asks for `sudo`)
   - asks before switching XRLinuxDriver to stream head poses over UDP and stops its own mouse movement, which would otherwise fight this plugin (two lines in `config.ini`; the old file is kept as `config.ini.before-xr-head-aim`)
   - writes `~/.config/xr-head-aim/settings.json`
   - installs a systemd **user** service

   It doesn't change anything else. Run `install.sh --autostart` if you want head aim to start at login.

3. **Optional, PS5 controller:** the installer asks whether to set up the controller bridge (or run `install.sh --controller`). It installs a small system service (`xr-pad.service`, runs as root to read the controller) and a udev rule that hides the DualSense's own device from games, so they see exactly one Xbox pad. Don't run it alongside InputPlumber or another tool that also remaps the DualSense. `uninstall.sh` removes both and makes the controller visible again.

## Use

- **Bar icon:** left-click opens the panel, middle-click turns it on or off, right-click recenters.
- **Panel:** an on/off switch, plus Recenter, Pause and Reset tuning buttons, the output mode, and the sliders.
- **Command line:** `bin/xr-head-aim start|stop|toggle|recenter|pause|state|tune key=value…|tune reset`

### Tuning tips

- **Mouse mode:** set *Sensitivity* (mouse counts per head degree) so that a head turn feels right with your in-game mouse sensitivity.
- **Gamepad mode:** set the game's look acceleration to 0. Set *Game stick dead zone* to the game's look dead zone. `game_full_rate`, the game's turn speed at full stick, can be set in `settings.json`.
- **If the view drifts while you hold still:** raise *Dead zone*.
- **If small moves feel twitchy:** lower *Slow-motion precision* or raise *Tremor smoothing*.
- **To let a game through the focus check:** if it isn't a Steam window, add its class to `extra_classes`. To keep head aim out of a game, add it to `excluded_classes`. Find a class with `hyprctl activewindow`.

## Uninstall

```bash
~/.config/omarchy/plugins/io.github.r88800.xr-head-aim/uninstall.sh   # add --purge to also delete settings
omarchy plugin remove io.github.r88800.xr-head-aim
```

This removes the services and udev rules it added, including the controller bridge.

## How it works

XRLinuxDriver sends one UDP datagram per head pose to `127.0.0.1:<port>`. Each datagram holds six doubles (x, y, z, yaw, pitch, roll) followed by a frame counter. `xr_head_aim.py` turns the change in angle into a head speed, using time steps from the frame counter so network jitter adds no noise. It then shapes that speed (dead zone, tremor smoothing, precision curve, lag removal) and sends it to the controller bridge (when a controller is connected), a virtual uinput mouse or a virtual gamepad. The bar widget talks to the service through `bin/xr-head-aim`.

Run the tests with `python3 -m unittest test_xr_head_aim`. They need no glasses, uinput or Hyprland.

## Built with Claude

This plugin was built by Riley together with [Claude Code](https://claude.com/claude-code), Anthropic's AI coding assistant. The aiming algorithm was tuned against recorded play sessions on a VITURE Luma Pro, and Claude wrote and tested the code with Riley's direction and in-game feedback. Please review it as you would any community code before installing.

## Contributing

Issues and pull requests are welcome from anyone. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE). XRLinuxDriver and the glasses' SDKs are separate projects with their own licenses and are not included here.
