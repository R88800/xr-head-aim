# Contributing

Contributions are welcome from anyone: bug reports, tuning feedback, support for more glasses, and code.

- **Bugs and ideas:** open an issue. For aiming problems, include your glasses model, the game, the output mode (mouse or gamepad), and your `~/.config/xr-head-aim/settings.json`.
- **Code:** fork the repo, make your change on a branch, and open a pull request.
  - Keep `python3 -m unittest test_xr_head_aim` passing, and add a test for new behavior.
  - Run `omarchy plugin validate .` before submitting.
  - Describe what you tested. "Tested with XREAL Air 2 in Doom Eternal" is ideal, since only VITURE glasses have been tested so far.
- **Especially wanted:** reports from XREAL, Rokid and RayNeo owners, results from gamepad mode in different games, and support for Wayland compositors other than Hyprland.

By submitting a contribution you agree that it is licensed under the project's [MIT License](LICENSE).
