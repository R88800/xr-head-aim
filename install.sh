#!/usr/bin/env bash
# Set up XR Head Aim's background service. Run as your user (it asks for sudo only if
# /dev/uinput needs a udev rule, or for the controller bridge). Safe to rerun.
# Undo with ./uninstall.sh.
#   ./install.sh               set up; head aim starts from the bar widget (or `bin/xr-head-aim start`)
#   ./install.sh --autostart   also start it with every login
#   ./install.sh --controller  also make a PS5 DualSense an Xbox pad with head aim on its
#                              right stick (system service, asks for sudo)
set -euo pipefail
autostart=0; controller=ask
for arg in "$@"; do
  case $arg in
    --autostart) autostart=1 ;;
    --controller) controller=yes ;;
    --no-controller) controller=no ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
here=$(cd -- "$(dirname -- "$0")" && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}
unit_dir=$config/systemd/user
rule=/etc/udev/rules.d/70-xr-head-aim-uinput.rules
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root."
echo "XR Head Aim setup"

command -v python3 >/dev/null || die "python3 is missing."
python3 -c 'import evdev' 2>/dev/null || die "python-evdev is missing: sudo pacman -S python-evdev"
ok "python3 + python-evdev"
command -v hyprctl >/dev/null && ok "Hyprland" || warn "hyprctl not found: the game-focus gate needs Hyprland."

# Virtual mouse/gamepad need write access to /dev/uinput.
if [ -w /dev/uinput ]; then
  ok "/dev/uinput is writable"
else
  echo "  Creating a virtual mouse/gamepad needs access to /dev/uinput."
  echo "  This adds $rule (gives the logged-in user access, like Steam's rule)."
  sudo install -m 644 /dev/stdin "$rule" <<'RULE'
# XR Head Aim: the logged-in user may create virtual input devices (remove with uninstall.sh)
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
RULE
  sudo udevadm control --reload
  sudo udevadm trigger --name-match=uinput
  sleep 1
  [ -w /dev/uinput ] && ok "/dev/uinput is writable" || warn "Still no access to /dev/uinput; log out and back in."
fi

# XRLinuxDriver must stream poses over UDP (its external pose-stream mode) instead of
# moving the mouse itself. Set the two keys, keep everything else in the file.
driver=$config/xr_driver/config.ini
stream_mode=opentrack   # XRLinuxDriver's fixed name for its UDP pose stream
port=4242
set_key() {   # set_key KEY VALUE: replace the line, or append it
  if grep -q "^$1=" "$driver"; then sed -i "s|^$1=.*|$1=$2|" "$(readlink -f "$driver")"
  else printf '%s=%s\n' "$1" "$2" >> "$driver"; fi
}
if [ -f "$driver" ]; then
  p=$(sed -n 's/^[a-z]*_app_port=\([0-9]\+\).*/\1/p' "$driver" | tail -1)
  [ -n "$p" ] && port=$p
  if grep -qE "^external_mode=.*$stream_mode" "$driver" && grep -q '^output_mode=external_only' "$driver"; then
    ok "XRLinuxDriver streams poses to 127.0.0.1:$port"
  else
    echo "  XRLinuxDriver must stream head poses and stop moving the mouse itself. This sets"
    echo "  external_mode=$stream_mode and output_mode=external_only in $driver (backup kept)."
    answer=n
    [ -t 0 ] && read -r -p "  Change it now? [Y/n] " answer && answer=${answer:-y}
    if [[ $answer =~ ^[Yy] ]]; then
      cp "$driver" "$driver.before-xr-head-aim"
      set_key external_mode "$stream_mode"
      set_key output_mode external_only
      ok "XRLinuxDriver set to stream poses to 127.0.0.1:$port (backup: $driver.before-xr-head-aim)"
    else
      warn "Not changed. Add those two lines to $driver yourself, then head aim will work."
    fi
  fi
else
  warn "XRLinuxDriver not found. Install it, then rerun this: https://github.com/wheaney/XRLinuxDriver"
fi

# Settings (kept on reinstall), with the driver's port.
mkdir -p "$config/xr-head-aim"
python3 "$here/xr_head_aim.py" tune "port=$port"
ok "Settings: $config/xr-head-aim/settings.json (port $port)"

mkdir -p "$unit_dir"
sed "s|@DIR@|$here|g" "$here/xr-head-aim.service.in" > "$unit_dir/xr-head-aim.service"
systemctl --user daemon-reload
if [ "$autostart" = 1 ]; then
  systemctl --user enable --now xr-head-aim.service
  ok "Service installed, starts with every login"
else
  ok "Service installed (start it from the bar widget, middle-click, or: $here/bin/xr-head-aim start)"
fi
# Optional: PS5 DualSense as an Xbox pad, with head aim blended into its right stick
# (otherwise head aim drives the mouse). Needs root: it reads the controller and hides
# its own nodes so games don't see two controllers.
if [ "$controller" = ask ] && [ -t 0 ] && [ ! -f /etc/systemd/system/xr-pad.service ]; then
  read -r -p "  Use a PS5 controller as an Xbox pad with head aim on its right stick? [y/N] " answer
  [[ ${answer:-n} =~ ^[Yy] ]] && controller=yes
fi
if [ "$controller" = yes ] || { [ "$controller" = ask ] && [ -f /etc/systemd/system/xr-pad.service ]; }; then
  if systemctl is-active --quiet inputplumber 2>/dev/null; then
    warn "InputPlumber is running and also manages the DualSense; disable it: sudo systemctl disable --now inputplumber"
  fi
  sudo install -D -m 755 "$here/controller/xr_pad.py" /usr/local/lib/xr-head-aim/xr_pad.py
  sed "s|@USER@|$(id -un)|" "$here/controller/xr-pad.service" | sudo install -m 644 /dev/stdin /etc/systemd/system/xr-pad.service
  sudo install -m 644 "$here/controller/72-xr-pad.rules" /etc/udev/rules.d/72-xr-pad.rules
  sudo udevadm control --reload
  sudo udevadm trigger --action=change --subsystem-match=input --subsystem-match=hidraw
  sudo systemctl daemon-reload
  sudo systemctl enable xr-pad.service >/dev/null 2>&1
  sudo systemctl restart xr-pad.service
  ok "Controller bridge running: a PS5 controller shows up as an Xbox pad whenever it connects"
fi
echo "Done. In-game: head right/up turns right/up. Aim only moves while a Steam game window is focused."
[ -f /etc/systemd/system/xr-pad.service ] && echo "Head aim goes to the controller's right stick while it is connected, otherwise to the mouse."
true
