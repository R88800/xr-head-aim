#!/usr/bin/env python3
"""XR Head Aim: aim in games by turning your head with XR glasses.

XRLinuxDriver streams the glasses' pose to 127.0.0.1:4242 (six doubles x, y, z, yaw,
pitch, roll and a uint32 frame counter; yaw+ turns left, pitch+ looks down). This daemon
turns head rotation into aim while a game window has focus. One sensitivity (view
degrees per head degree) applies to every output:

- auto (default): your controller's right stick while one is connected (through the
  controller bridge, controller/xr_pad.py), otherwise the mouse
- controller: always a right stick (the bridge's, or a virtual pad without the bridge)
- mouse: always the mouse

It runs for the whole session and idles until the glasses stream. On/off is a setting,
so it survives restarts.

  xr_head_aim.py run             the daemon (xr-head-aim.service)
  xr_head_aim.py state           one JSON status line (bar widget)
  xr_head_aim.py toggle|on|off   head aim on/off
  xr_head_aim.py tune k=v ...    change settings; the daemon applies them within a second
  xr_head_aim.py tune reset      sliders back to the defaults (keeps on/off, output, game lists)

SIGUSR1 recenters (restarts the filter from the current pose).
"""
import json
import math
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'xr-head-aim'
SETTINGS_PATH = CONFIG_DIR / 'settings.json'
STATE_PATH = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'xr-head-aim.json'
BRIDGE = Path('/run/xr-pad')   # controller bridge (xr-pad.service), if installed

PORT = 4242                    # XRLinuxDriver's pose-stream port
POSE = struct.Struct('=6d')
FRAME = struct.Struct('=I')
YAW, PITCH = 3, 4
STALE = .12                    # s without poses before aim stops

# Tuned on recorded play with a VITURE Luma Pro. Only the first block is a matter of taste.
DEFAULTS = {
    'enabled': 1,
    'output': 'auto',           # 'auto', 'controller' or 'mouse'
    'gain': 5.9,                # view degrees per head degree, the same for every output
    'vertical_ratio': .9,       # up/down relative to left/right
    'invert_y': 0,
    # Advanced: how head speed (deg/s) is shaped.
    'still_from': .05,          # below: no output (sensor noise), fading in up to still_to
    'still_to': 1.0,
    'smooth_ms': 20.0,          # tremor averaging below smooth_from; none above smooth_to
    'smooth_from': .4,
    'smooth_to': 1.5,
    'precision': .6,            # slow moves get this share of the sensitivity...
    'precision_to': 8.0,        # ...rising to full by this head speed
    'predict': 1.0,             # fast moves: 3-pose rate estimate (~4 ms less lag)
    # Advanced: how the game turns, so a head degree is the same view turn on every output.
    'game_full_rate': 165.0,    # controller: view deg/s at full stick
    'game_deadzone': .06,       # controller: the game's look dead zone (skipped)
    'game_mouse_deg': .15,      # mouse: view degrees per mouse count (Source games: 0.022 x sensitivity)
    # Window classes: extra games (non-Steam), and games to leave alone.
    'extra_classes': [],
    'excluded_classes': [],
}
LISTS = ('extra_classes', 'excluded_classes')
OUTPUTS = ('auto', 'controller', 'mouse')
KEPT_ON_RESET = ('enabled', 'output', *LISTS)   # reset is for the sliders


# -- settings -------------------------------------------------------------------------

def load_settings(path=None):
    path = path or SETTINGS_PATH
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return settings
    except (OSError, ValueError) as exc:
        print(f'Ignoring unreadable {path}: {exc}', flush=True)
        return settings
    for key, value in saved.items():
        if key in LISTS and isinstance(value, list):
            settings[key] = [str(v) for v in value]
        elif key == 'output':
            if value in OUTPUTS:
                settings[key] = value
        elif key in DEFAULTS and key not in LISTS and key != 'output' and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            settings[key] = type(DEFAULTS[key])(value)
    return settings


def save_settings(settings, path=None):
    """Atomic: the daemon must never read a half-written file."""
    path = path or SETTINGS_PATH
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=real.name + '.')
    with os.fdopen(fd, 'w') as f:
        json.dump(settings, f, indent=2)
        f.write('\n')
    os.replace(tmp, real)


def tune(args):
    settings = load_settings()
    if args == ['reset']:
        settings = {**DEFAULTS, **{k: settings[k] for k in KEPT_ON_RESET}}
    for arg in [] if args == ['reset'] else args:
        key, sep, value = arg.partition('=')
        if not sep or key not in DEFAULTS:
            print(f'Unknown setting: {arg}', file=sys.stderr)
            return 2
        if key in LISTS:
            settings[key] = [v for v in value.split(',') if v]
            continue
        if key == 'output':
            if value not in OUTPUTS:
                print(f"output must be one of: {', '.join(OUTPUTS)}", file=sys.stderr)
                return 2
            settings[key] = value
            continue
        try:
            settings[key] = type(DEFAULTS[key])(float(value))
        except ValueError:
            print(f'{key} needs a number', file=sys.stderr)
            return 2
    save_settings(settings)
    return 0


def switch(command):
    settings = load_settings()
    on = {'on': 1, 'off': 0}.get(command, 0 if settings['enabled'] else 1)
    return tune([f'enabled={on}'])


# -- head motion ------------------------------------------------------------------------

def smoothstep(x):
    x = min(1., max(0., x))
    return x * x * (3 - 2 * x)


class HeadRate:
    """Glasses angles (deg, right/down positive) -> shaped head rate (deg/s).

    The IMU is clean enough to differentiate directly. Time steps come from the driver's
    frame counter at a learned period, so packet-arrival jitter adds no noise. Slow,
    near-still motion is averaged (tremor) and faded out (noise); fast motion passes
    straight through, with a 3-pose backward difference so the rate is the current one.
    """
    def __init__(self, settings):
        self.s = settings
        self.period = 1 / 115.
        self.reset()

    def reset(self):
        self.prev = self.older = None
        self.smooth = [0., 0.]
        self.rate = [0., 0.]
        self.dt = 0.

    def step(self, p, frame, t):
        s = self.s
        older, prev, self.prev = self.older, self.prev, (p, frame, t)
        self.older = None
        self.rate = [0., 0.]
        self.dt = 0.
        if prev is None:
            return self.rate
        frames = (frame - prev[1]) & 0xFFFFFFFF if frame is not None and prev[1] is not None else 0
        elapsed = t - prev[2]
        if 0 < frames < 64 and 0 < elapsed < .25:
            self.period += .02 * (min(.05, max(.002, elapsed / frames)) - self.period)
            dt = frames * self.period
        elif frame is None and 0 < elapsed < STALE:
            dt = elapsed
        else:
            return self.rate            # gap or restart: start over from this pose
        raw = [(p[i] - prev[0][i]) / dt for i in (0, 1)]
        if older is not None and abs(dt - older[1]) < .5 * dt:
            lead = smoothstep((math.hypot(*raw) - s['smooth_to']) / max(1e-6, 2 * s['smooth_to']))
            k = s['predict'] * lead * .5
            raw = [raw[i] + k * (raw[i] - (prev[0][i] - older[0][i]) / older[1]) for i in (0, 1)]
        self.older = (prev[0], dt)
        self.dt = dt
        a = 1 - math.exp(-dt / max(1e-3, s['smooth_ms'] / 1000))
        self.smooth = [self.smooth[i] + a * (raw[i] - self.smooth[i]) for i in (0, 1)]
        direct = smoothstep((math.hypot(*raw) - s['smooth_from'])
                            / max(1e-6, s['smooth_to'] - s['smooth_from']))
        rate = [direct * raw[i] + (1 - direct) * self.smooth[i] for i in (0, 1)]
        speed = math.hypot(*rate)
        fade = smoothstep((speed - s['still_from']) / max(1e-6, s['still_to'] - s['still_from']))
        precision = s['precision'] + (1 - s['precision']) * smoothstep(speed / max(1e-6, s['precision_to']))
        k = fade * precision
        self.rate = [rate[0] * k, rate[1] * k * s['vertical_ratio'] * (-1 if s['invert_y'] else 1)]
        return self.rate


def head_angles(pose, prev_yaw):
    """Driver pose -> ([deg right, deg down], unwrapped yaw for the next call)."""
    yaw = pose[YAW]
    if prev_yaw is not None:   # yaw wraps at +-180; keep it continuous
        yaw = prev_yaw + (yaw - prev_yaw + 180.) % 360. - 180.
    return [-yaw, pose[PITCH]], yaw


def stick_for_rate(rate, full_rate, deadzone):
    """View rate (deg/s) -> stick: linear in rate, full stick at the game's full turn
    rate, the game's dead zone skipped (like Steam Input's gyro-to-joystick camera)."""
    u = [rate[0] / full_rate, rate[1] / full_rate]
    length = math.hypot(*u)
    if length < 1e-9:
        return [0., 0.]
    amount = min(1., deadzone + (1 - deadzone) * length)
    return [u[0] / length * amount, u[1] / length * amount]


# -- where aim goes ----------------------------------------------------------------------

def view_rate(rate, s):
    """Head rate -> view rate (deg/s): the one sensitivity every output shares."""
    return [rate[0] * s['gain'], rate[1] * s['gain']]


class Mouse:
    """Relative mouse; sub-count remainders carry over, so total motion is exact."""
    def __init__(self):
        from evdev import UInput, ecodes as e
        self.e = e
        self.ui = UInput({e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT], e.EV_REL: [e.REL_X, e.REL_Y]},
                         name='XR Head Aim Mouse')
        self.remainder = [0., 0.]

    def aim(self, rate, dt, s):
        moved = False
        for i, (code, view) in enumerate(zip((self.e.REL_X, self.e.REL_Y), view_rate(rate, s))):
            self.remainder[i] += view * dt / max(1e-4, s['game_mouse_deg'])
            whole = int(self.remainder[i])
            if whole:
                self.remainder[i] -= whole
                self.ui.write(self.e.EV_REL, code, whole)
                moved = True
        if moved:
            self.ui.syn()

    def stop(self):
        self.remainder = [0., 0.]

    def close(self):
        self.ui.close()


class ControllerStick:
    """The controller bridge's right stick. Sent on every pose: the bridge drops head
    aim 0.25 s after the last packet, so a crash can't leave the stick deflected."""
    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def aim(self, rate, dt, s):
        self.send(stick_for_rate(view_rate(rate, s), s['game_full_rate'], s['game_deadzone']))

    def send(self, stick):
        try:
            self.sock.sendto(f'{stick[0]:.5f} {stick[1]:.5f}'.encode(), str(BRIDGE / 'head.sock'))
        except OSError:
            pass   # bridge restarting: the next pose tries again

    def stop(self):
        self.send([0., 0.])

    def close(self):
        self.stop()
        self.sock.close()


class VirtualPad:
    """Controller output without the bridge: a virtual Xbox-style pad whose right stick
    carries the head aim (games may see it as a second controller)."""
    def __init__(self):
        from evdev import UInput, AbsInfo, ecodes as e
        self.e = e
        axes = [(c, AbsInfo(0, -32768, 32767, 16, 128, 0)) for c in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]
        self.ui = UInput({e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_START, e.BTN_SELECT],
                          e.EV_ABS: axes}, name='XR Head Aim Pad', vendor=0x045e, product=0x028e, version=0x110)
        self.last = None

    def aim(self, rate, dt, s):
        self.send(stick_for_rate(view_rate(rate, s), s['game_full_rate'], s['game_deadzone']))

    def send(self, stick):
        value = tuple(round(max(-1., min(1., v)) * 32767) for v in stick)
        if value != self.last:
            self.last = value
            self.ui.write(self.e.EV_ABS, self.e.ABS_RX, value[0])
            self.ui.write(self.e.EV_ABS, self.e.ABS_RY, value[1])
            self.ui.syn()

    def stop(self):
        self.send([0., 0.])

    def close(self):
        self.stop()
        self.ui.close()


def bridge_installed():
    return BRIDGE.is_dir()


def controller_connected():
    try:
        return bool(json.loads((BRIDGE / 'state').read_text()).get('connected'))
    except (OSError, ValueError):
        return False


class Output:
    """Sends aim where the output setting says. Devices are created only when used, so
    no stray virtual pad or mouse exists for games to pick up."""
    NAMES = {Mouse: 'mouse', ControllerStick: 'controller', VirtualPad: 'virtual pad'}

    def __init__(self, settings):
        self.s = settings
        self.devices = {}
        self.kind = self.target = None
        self.checked = 0.

    @property
    def name(self):
        return self.NAMES.get(self.kind, 'none')

    def wanted(self):
        mode = self.s['output']
        if mode == 'mouse':
            return Mouse
        if mode == 'controller':
            return ControllerStick if bridge_installed() else VirtualPad
        return ControllerStick if controller_connected() else Mouse

    def follow(self):
        now = time.monotonic()
        if now - self.checked < .5 and self.target is not None:
            return
        self.checked = now
        kind = self.wanted()
        if kind is self.kind:
            return
        if self.target is not None:
            self.target.stop()
        if kind is not VirtualPad:   # a leftover virtual pad would confuse games
            pad = self.devices.pop(VirtualPad, None)
            if pad:
                pad.close()
        if kind not in self.devices:
            self.devices[kind] = kind()
        self.kind, self.target = kind, self.devices[kind]

    def aim(self, rate, dt):
        self.follow()
        self.target.aim(rate, dt, self.s)

    def stop(self):
        self.follow()
        self.target.stop()

    def close(self):
        for device in self.devices.values():
            device.close()


# -- only in games -----------------------------------------------------------------------

def is_game(window, s):
    cls = str(window.get('class') or window.get('initialClass') or '')
    if not cls or cls in s['excluded_classes']:
        return False
    return cls.startswith('steam_app_') or cls in s['extra_classes']


class GameFocus(threading.Thread):
    """Polls Hyprland's focused window; fails closed if the answer is stale."""
    def __init__(self, settings):
        super().__init__(daemon=True)
        self.s = settings
        self.game, self.updated, self.window = False, 0., ''
        self.stopped = threading.Event()

    def allowed(self):
        return self.game and time.monotonic() - self.updated < .6

    def run(self):
        while not self.stopped.wait(.25):
            try:
                out = subprocess.run(['hyprctl', 'activewindow', '-j'], capture_output=True,
                                     text=True, timeout=.4).stdout
                window = json.loads(out or '{}')
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
            self.window = str(window.get('class') or '')
            self.game, self.updated = is_game(window, self.s), time.monotonic()


# -- the daemon --------------------------------------------------------------------------

def newest_pose(sock):
    """Drain the socket (~115 Hz; only the newest pose matters) -> (pose, frame) or None."""
    newest = None
    while True:
        try:
            data = sock.recv(128)
        except BlockingIOError:
            return newest
        if len(data) >= POSE.size:
            pose = POSE.unpack_from(data)
            frame = FRAME.unpack_from(data, POSE.size)[0] if len(data) >= POSE.size + FRAME.size else None
            if all(map(math.isfinite, pose)):
                newest = pose, frame


def run():
    settings = load_settings()
    mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.
    flags = {'stop': False, 'recenter': False}
    signal.signal(signal.SIGTERM, lambda *_: flags.update(stop=True))
    signal.signal(signal.SIGINT, lambda *_: flags.update(stop=True))
    signal.signal(signal.SIGUSR1, lambda *_: flags.update(recenter=True))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', PORT))
    sock.setblocking(False)
    output = Output(settings)
    focus = GameFocus(settings)
    focus.start()
    head = HeadRate(settings)
    yaw, last_pose = None, 0.
    next_check = next_state = 0.
    print(f'Listening for XRLinuxDriver poses on 127.0.0.1:{PORT}', flush=True)

    def publish(now):
        tmp = STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps({'running': True, 'enabled': bool(settings['enabled']),
                                   'output': output.name, 'glasses': now - last_pose < 1.,
                                   'game': focus.allowed(), 'window': focus.window,
                                   'pid': os.getpid()}))
        tmp.replace(STATE_PATH)

    try:
        while not flags['stop']:
            ready, _, _ = select.select([sock], [], [], .1)
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1.
                m = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.
                if m != mtime:
                    mtime = m
                    settings.clear()
                    settings.update(load_settings())
                    print('Settings applied', flush=True)
            if now >= next_state:
                next_state = now + .5
                publish(now)
            if flags['recenter']:
                flags['recenter'] = False
                head.reset()
                output.stop()
            received = newest_pose(sock) if ready else None
            if received is None:
                if now - last_pose > STALE:
                    output.stop()
                continue
            pose, frame = received
            angles, yaw = head_angles(pose, yaw if now - last_pose < 1. else None)
            last_pose = now
            if not settings['enabled'] or not focus.allowed():
                head.reset()
                output.stop()
                continue
            rate = head.step(angles, frame, now)
            output.aim(rate, head.dt)   # head.dt: mouse motion = head motion, exactly
    finally:
        focus.stopped.set()
        output.close()
        sock.close()
        STATE_PATH.unlink(missing_ok=True)
    return 0


def state():
    try:
        data = json.loads(STATE_PATH.read_text())
        os.kill(int(data.get('pid', 0)), 0)
    except (OSError, ValueError):
        data = {'running': False}
    data['settings'] = load_settings()
    data['enabled'] = bool(data['settings']['enabled'])
    data['bridge'] = bridge_installed()
    unit = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd' / 'user' / 'xr-head-aim.service'
    data['installed'] = unit.exists()
    print(json.dumps(data))
    return 0


def main(argv):
    command = argv[0] if argv else ''
    if command == 'run':
        return run()
    if command == 'state':
        return state()
    if command in ('toggle', 'on', 'off'):
        return switch(command)
    if command == 'tune' and len(argv) > 1:
        return tune(argv[1:])
    print('\n'.join(l for l in __doc__.splitlines() if l.startswith('  xr_head_aim.py')), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
