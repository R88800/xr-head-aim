#!/usr/bin/env bash
# Set up XR Head Aim's background service. Run as your user (it asks for sudo only if
# /dev/uinput needs a udev rule). Safe to rerun. Undo with ./uninstall.sh.
#   ./install.sh            set up; head aim starts from the bar widget (or `bin/xr-head-aim start`)
#   ./install.sh --autostart  also start it with every login
set -euo pipefail
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

# XRLinuxDriver must broadcast poses (its "opentrack" external mode).
driver=$config/xr_driver/config.ini
port=4242
if [ -f "$driver" ]; then
  p=$(sed -n 's/^opentrack_app_port=\([0-9]\+\).*/\1/p' "$driver" | tail -1)
  [ -n "$p" ] && port=$p
  if grep -qE '^external_mode=.*opentrack' "$driver"; then
    ok "XRLinuxDriver sends poses to 127.0.0.1:$port"
  else
    warn "XRLinuxDriver isn't sending poses yet. Add these lines to $driver:"
    echo "        external_mode=opentrack"
    echo "        output_mode=external_only    # stops the driver's own mouse movement"
  fi
else
  warn "XRLinuxDriver not found. Install it first: https://github.com/wheaney/XRLinuxDriver"
fi

# Settings (kept on reinstall), with the driver's port.
mkdir -p "$config/xr-head-aim"
python3 "$here/xr_head_aim.py" tune "port=$port"
ok "Settings: $config/xr-head-aim/settings.json (port $port)"

mkdir -p "$unit_dir"
sed "s|@DIR@|$here|g" "$here/xr-head-aim.service.in" > "$unit_dir/xr-head-aim.service"
systemctl --user daemon-reload
if [ "${1:-}" = --autostart ]; then
  systemctl --user enable --now xr-head-aim.service
  ok "Service installed, starts with every login"
else
  ok "Service installed (start it from the bar widget, middle-click, or: $here/bin/xr-head-aim start)"
fi
echo "Done. In-game: head right/up turns right/up. Aim only moves while a Steam game window is focused."
