# XR Head Aim

Aim in games by turning your head with XR glasses. Head aim adds to your mouse or controller; it doesn't replace them.

An [Omarchy](https://omarchy.org) bar widget with a small background service. It reads your glasses' head tracking through [XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver) and turns head movement into your controller's right stick, or the mouse. Optionally it also makes a PS5 DualSense work as an Xbox controller in every game, with no other tools needed.

![XR Head Aim on the Omarchy desktop](preview.png)

## What it does

- **Just works.** It runs in the background all session and only acts while a game window has focus and the glasses are streaming. Put on the glasses, focus a game, aim.
- **Your controller's right stick, or the mouse.** With a PS5 controller connected, head aim is mixed into its real right stick, so your thumb and your head work together. Without one, head aim moves the mouse. *Auto* switches by itself; pick *Controller* or *Mouse* to force one (Controller mode without a PS5 controller bridge uses a virtual pad).
- **PS5 controller as an Xbox pad.** The installer sets this up if you have a DualSense: it shows up in every game as a plain Xbox 360 controller (USB or Bluetooth) with the Xbox button layout, analog triggers and rumble, and it survives Bluetooth drops mid-game.
- **Low lag.** The glasses' IMU is read directly at about 115 Hz. Fast moves use the newest head rate (a 3-sample estimate), and output is sent as soon as a sample arrives.
- **Holds still when you do.** A small dead zone and tremor smoothing apply only to very slow, near-still motion, so real turns get no added lag. Slow, small moves are finer (precision curve).
- **Tuned out of the box.** The defaults come from recorded play sessions. **One sensitivity** (view degrees per head degree) feels the same on the controller and the mouse; everything else is under *Advanced tuning* if you want it.

![Tuning panel](screenshots/panel.png)

## Install

You need Omarchy, `python-evdev` (`sudo pacman -S python-evdev`), and XR glasses supported by [XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver#supported-devices) (VITURE, XREAL, Rokid, RayNeo; tested on a VITURE Luma Pro). Install XRLinuxDriver and plug the glasses in once, then:

```bash
omarchy plugin add https://github.com/R88800/xr-head-aim --enable
~/.config/omarchy/plugins/io.github.r88800.xr-head-aim/install.sh
```

The installer works out what your system needs, lists it, and asks once:

- switches XRLinuxDriver to stream head poses instead of moving the mouse itself (two lines in `config.ini`; the old file is kept as `config.ini.before-xr-head-aim`)
- adds access to `/dev/uinput` for the virtual mouse, if you don't have it yet (sudo)
- if a PS5 controller has been paired or is plugged in: the controller bridge, a small system service (`xr-pad.service`) plus a udev rule that hides the DualSense's own device so games see exactly one Xbox controller (sudo)
- the background service, started now and at every login

`--yes` skips the question, `--no-controller` skips the controller bridge (or `--controller` adds it before you've paired one). Rerun it after `omarchy plugin update`. Don't combine the controller bridge with InputPlumber or another DualSense remapper.

## Use

- **Bar icon:** left-click opens the panel, middle-click turns head aim on or off, right-click recenters.
- **Panel:** on/off, Recenter, Reset sliders, Invert up/down, the output (*Auto* / *Controller* / *Mouse*), *Sensitivity* and *Vertical ratio*. *Advanced tuning* has precision, dead zone, smoothing, lag removal and how the game turns (stick turn speed and dead zone, mouse speed). Sliders change only when you click or drag them; scrolling over one scrolls the panel. *Reset sliders* puts them back to the tuned defaults and keeps your on/off and output choice. Changes apply within a second.
- **Command line:** `bin/xr-head-aim toggle|on|off|recenter|state|tune key=value…|tune reset`. For a key binding in Hyprland: `bind = ALT, E, exec, ~/.config/omarchy/plugins/io.github.r88800.xr-head-aim/bin/xr-head-aim toggle`.
- **Games:** Steam games (`steam_app_*` windows) count automatically. Add other games with `xr-head-aim tune "extra_classes=class1,class2"`, and keep head aim out of a game with `excluded_classes=…` (find a class with `hyprctl activewindow -j`). A rule can also match part of the window title: `class|title fragment`. That's how a game in a browser works without enabling head aim in every tab, for example `xr-head-aim tune "extra_classes=brave-browser|Xbox Cloud Gaming"` for Xbox Cloud Gaming.

### Tuning tips

- **Set *Sensitivity* once:** it's view degrees per head degree, so the same head turn moves the view the same amount on the controller and the mouse. For that to hold, tell it how the game turns (*Advanced tuning*):
  - **Controller:** set the game's look acceleration to 0, *Game stick dead zone* to the game's look dead zone, and *Game turn speed* to how fast the game turns at full stick.
  - **Mouse:** *Game mouse speed* is the view degrees per mouse count. In Source games (CS2, Half-Life, Portal) it's 0.022 × your in-game sensitivity; elsewhere, adjust it until a head turn and the view match.
- **The view drifts while you hold still:** raise *Dead zone*. **Small moves feel twitchy:** lower *Slow-motion precision* or raise *Tremor smoothing*.

### Troubleshooting

- **"Waiting for glasses":** plug in the glasses and give XRLinuxDriver a few seconds to calibrate (`systemctl --user status xr-driver`).
- **A game gets no controller input:** it grabbed a controller while starting. Restart the game with the controller connected.
- **Steam shows two controllers:** check `systemctl status xr-pad` and that no other remapper is running; reconnecting the controller reapplies the udev rule.
- **Logs:** `journalctl --user -u xr-head-aim` (head aim), `journalctl -u xr-pad` (controller).

## Uninstall

```bash
~/.config/omarchy/plugins/io.github.r88800.xr-head-aim/uninstall.sh   # --purge also deletes settings
omarchy plugin remove io.github.r88800.xr-head-aim
```

This removes the services and udev rules, and makes the controller visible to games directly again. Your old XRLinuxDriver config is still in `config.ini.before-xr-head-aim`.

## How it works

Two small Python programs:

- **`xr_head_aim.py`** (user service) reads XRLinuxDriver's UDP stream on `127.0.0.1:4242`: six doubles (x, y, z, yaw, pitch, roll) and a frame counter per pose. It turns the change in angle into a head speed, timed by the frame counter so network jitter adds no noise, shapes it (dead zone, tremor smoothing, precision curve, lag removal), and scales it by the one sensitivity, and sends it to the controller bridge's right stick, a virtual mouse (converted with the game's mouse speed) or, in Controller mode without the bridge, a virtual pad. Settings live in `~/.config/xr-head-aim/settings.json`.
- **`controller/xr_pad.py`** (system service, optional) grabs the DualSense's input devices and mirrors them onto a virtual "Microsoft X-Box 360 pad". It takes head aim from `/run/xr-pad/head.sock` and blends it as `physical + head × (1 − |physical|)`, forwards rumble, and keeps the virtual pad for 10 minutes after a disconnect so a running game keeps its controller.

Run the tests with `python3 -m unittest test_xr_head_aim`. They need no glasses, controller, uinput or Hyprland.

## Built with Claude

This plugin was built by Riley together with [Claude Code](https://claude.com/claude-code), Anthropic's AI coding assistant. The aiming algorithm was tuned against recorded play sessions on a VITURE Luma Pro, and Claude wrote and tested the code with Riley's direction and in-game feedback. Please review it as you would any community code before installing.

## Contributing

Issues and pull requests are welcome from anyone. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE). XRLinuxDriver and the glasses' SDKs are separate projects with their own licenses and are not included here.
