#!/usr/bin/env bash
# Set up XR Head Aim. Run as your normal user; safe to rerun (also after updating the
# plugin). It works out what your system needs, shows the list, and asks once.
#   ./install.sh                  set up everything that applies
#   ./install.sh --yes            don't ask
#   ./install.sh --no-controller  skip the PS5 controller bridge
# Undo with ./uninstall.sh.
set -euo pipefail
yes=0; controller=auto
for arg in "$@"; do
  case $arg in
    --yes|-y) yes=1 ;;
    --controller) controller=yes ;;
    --no-controller) controller=no ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
here=$(cd -- "$(dirname -- "$0")" && pwd)
config=${XDG_CONFIG_HOME:-$HOME/.config}
driver=$config/xr_driver/config.ini
uinput_rule=/etc/udev/rules.d/70-xr-head-aim-uinput.rules
stream_mode=opentrack   # XRLinuxDriver's fixed name for its UDP pose stream
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
todo() { printf '  \033[36m•\033[0m %s\n' "$1"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root."
echo "XR Head Aim"
command -v python3 >/dev/null || die "python3 is missing."
python3 -c 'import evdev' 2>/dev/null || die "python-evdev is missing: sudo pacman -S python-evdev"
command -v hyprctl >/dev/null || die "Hyprland is required (head aim only acts in the focused game window)."
[ -f "$driver" ] || die "XRLinuxDriver isn't set up: install it and plug in your glasses once (https://github.com/wheaney/XRLinuxDriver)."

# -- what this system needs --------------------------------------------------------------
driver_ok() {
  grep -qE "^external_mode=.*$stream_mode" "$driver" && grep -q '^output_mode=external_only' "$driver" &&
    ! grep -qE "^${stream_mode}_app_port=" "$driver"
}
dualsense() {   # paired over Bluetooth, or plugged in
  bluetoothctl devices 2>/dev/null | grep -q 'DualSense' ||
    ls /sys/bus/hid/devices 2>/dev/null | grep -qiE ':054C:(0CE6|0DF2)\.'
}
bridge_current() {
  cmp -s "$here/controller/xr_pad.py" /usr/local/lib/xr-head-aim/xr_pad.py &&
    [ "$(sed "s|@USER@|$(id -un)|" "$here/controller/xr-pad.service")" = "$(cat /etc/systemd/system/xr-pad.service 2>/dev/null)" ] &&
    cmp -s "$here/controller/72-xr-pad.rules" /etc/udev/rules.d/72-xr-pad.rules &&
    systemctl is-active --quiet xr-pad.service
}
need_driver=0; need_uinput=0; need_bridge=0
driver_ok || need_driver=1
[ -w /dev/uinput ] || need_uinput=1
if [ "$controller" = yes ] || { [ "$controller" = auto ] && { dualsense || [ -f /etc/systemd/system/xr-pad.service ]; }; }; then
  need_bridge=1
fi

[ $need_driver = 1 ] && todo "XRLinuxDriver: stream head poses instead of moving the mouse itself (2 lines in config.ini, backup kept)"
[ $need_uinput = 1 ] && todo "udev rule so you can create the virtual mouse (/dev/uinput, sudo)"
[ $need_bridge = 1 ] && ! bridge_current && todo "PS5 controller bridge: your DualSense becomes an Xbox pad in every game, head aim goes into its right stick (system service + udev rule, sudo)"
todo "background service for this session and every login (idles until the glasses stream)"
if [ $yes = 0 ] && [ -t 0 ]; then
  read -r -p "  Set this up? [Y/n] " answer
  [[ ${answer:-y} =~ ^[Yy] ]] || { echo "  Nothing changed."; exit 0; }
fi

# -- do it -----------------------------------------------------------------------------------
if [ $need_driver = 1 ]; then
  cp "$driver" "$driver.before-xr-head-aim"
  real=$(readlink -f "$driver")
  sed -i -E "/^(external_mode|output_mode|${stream_mode}_app_port)=/d" "$real"
  printf 'output_mode=external_only\nexternal_mode=%s\n' "$stream_mode" >> "$real"
  ok "XRLinuxDriver streams poses to 127.0.0.1:4242 (old config: $driver.before-xr-head-aim)"
fi

if [ $need_uinput = 1 ]; then
  sudo install -m 644 /dev/stdin "$uinput_rule" <<'RULE'
# XR Head Aim: the logged-in user may create virtual input devices (removed by uninstall.sh)
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
RULE
  sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput && sleep 1
  [ -w /dev/uinput ] && ok "/dev/uinput access" || todo "log out and back in for /dev/uinput access"
fi

if [ $need_bridge = 1 ]; then
  if systemctl is-active --quiet inputplumber 2>/dev/null; then
    todo "InputPlumber also manages the DualSense: turn it off with sudo systemctl disable --now inputplumber"
  fi
  if bridge_current; then
    ok "Controller bridge up to date"   # untouched: a restart would take the pad from a running game
  else
    sudo install -D -m 755 "$here/controller/xr_pad.py" /usr/local/lib/xr-head-aim/xr_pad.py
    sed "s|@USER@|$(id -un)|" "$here/controller/xr-pad.service" | sudo install -m 644 /dev/stdin /etc/systemd/system/xr-pad.service
    sudo install -m 644 "$here/controller/72-xr-pad.rules" /etc/udev/rules.d/72-xr-pad.rules
    sudo udevadm control --reload
    sudo udevadm trigger --action=change --subsystem-match=input --subsystem-match=hidraw
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet xr-pad.service
    sudo systemctl restart xr-pad.service
    ok "Controller bridge: a PS5 controller shows up as an Xbox pad whenever it connects"
  fi
fi

mkdir -p "$config/systemd/user"
sed "s|@DIR@|$here|g" "$here/xr-head-aim.service.in" > "$config/systemd/user/xr-head-aim.service"
systemctl --user daemon-reload
systemctl --user enable --quiet xr-head-aim.service
systemctl --user restart xr-head-aim.service
ok "Head aim service running"
echo "Done. Put on your glasses and focus a game: head right/up turns right/up."
echo "Aim goes to your controller's right stick while one is connected, otherwise to the mouse."
