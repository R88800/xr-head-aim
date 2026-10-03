#!/usr/bin/env bash
# Remove XR Head Aim's service and udev rule. --purge also deletes your settings.
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
if [ "${1:-}" = --purge ]; then
  rm -rf "$config/xr-head-aim"
  echo "Settings removed."
fi
