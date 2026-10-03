#!/usr/bin/env bash
# Remove XR Head Aim's services and udev rules. --purge also deletes your settings.
# The plugin folder itself is removed with: omarchy plugin remove io.github.r88800.xr-head-aim
set -uo pipefail
config=${XDG_CONFIG_HOME:-$HOME/.config}
rule=/etc/udev/rules.d/70-xr-head-aim-uinput.rules
systemctl --user disable --now xr-head-aim.service 2>/dev/null
rm -f "$config/systemd/user/xr-head-aim.service"
systemctl --user daemon-reload
echo "Service removed."
if [ -f "$rule" ]; then
  sudo rm -f "$rule" && sudo udevadm control --reload && echo "uinput rule removed."
fi
if [ -f /etc/systemd/system/xr-pad.service ]; then
  sudo systemctl disable --now xr-pad.service 2>/dev/null
  sudo rm -f /etc/systemd/system/xr-pad.service /etc/udev/rules.d/72-xr-pad.rules
  sudo rm -rf /usr/local/lib/xr-head-aim
  sudo systemctl daemon-reload
  sudo udevadm control --reload
  sudo udevadm trigger --action=change --subsystem-match=input --subsystem-match=hidraw
  echo "Controller bridge removed (the controller is visible to games directly again)."
fi
if [ "${1:-}" = --purge ]; then
  rm -rf "$config/xr-head-aim"
  echo "Settings removed."
fi
