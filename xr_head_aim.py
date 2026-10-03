#!/usr/bin/env python3
"""XR head aim: aim in games by turning your head, using XR glasses' IMU.

XRLinuxDriver (https://github.com/wheaney/XRLinuxDriver) streams the glasses' pose as
opentrack UDP datagrams (6 doubles x, y, z, yaw, pitch, roll + a uint32 frame counter,
NWU frame: yaw+ turns left, pitch+ looks down). This daemon turns head rotation into
either relative mouse motion (any mouse-aim game; exact, no stick model) or a virtual
gamepad's right stick, only while a game window is focused (Hyprland).

  xr_head_aim.py run            the daemon (xr-head-aim.service)
  xr_head_aim.py state          one JSON status line (used by the bar widget)
  xr_head_aim.py tune k=v ...   change settings (atomic; the daemon reloads live)
  xr_head_aim.py tune reset     restore defaults

Signals: SIGUSR1 resets the filter, SIGUSR2 pauses/resumes.
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

POSE = struct.Struct('=6d')
FRAME = struct.Struct('=I')
YAW, PITCH = 3, 4
STALE = .12   # s without poses before the output centers / stops

DEFAULTS = {
    'port': 4242,               # XRLinuxDriver opentrack_app_port
    'output': 'mouse',          # 'mouse' or 'gamepad'
    # Head speed shaping (head deg/s), shared by both outputs.
    'still_from': .05,          # below: no output (sensor noise), fading in up to still_to
    'still_to': 1.0,
    'smooth_ms': 20.0,          # tremor averaging below smooth_from; none above smooth_to
    'smooth_from': .4,
    'smooth_to': 1.5,
    'precision': .6,            # slow moves get this share of the sensitivity...
    'precision_to': 8.0,        # ...rising to full by this head speed
    'predict': 1.0,             # fast moves: 3-pose rate estimate (~4 ms less lag)
    'vertical_ratio': .9,       # up/down relative to left/right
    'invert_y': 0,
    # Mouse output: counts per head degree (depends on the game's mouse sensitivity).
    'mouse_sensitivity': 40.0,
    # Gamepad output: camera degrees per head degree, and the game's stick response.
    'gain': 5.0,
    'game_full_rate': 165.0,    # camera deg/s at full stick (horizontal)
    'game_full_rate_y': 135.0,
    'game_deadzone': .06,       # the game's radial dead zone (skipped by the output)
    # Focus gate: 'steam' = Steam game windows (class steam_app_*) and extra_classes;
    # 'always' = whatever window is focused (careful: moves your desktop pointer).
    'focus': 'steam',
    'extra_classes': [],        # window classes that count as games
    'excluded_classes': [],     # e.g. "steam_app_1145360" for a game without aiming
}
NUMERIC = {k for k, v in DEFAULTS.items() if isinstance(v, (int, float))}
CHOICES = {'output': ('mouse', 'gamepad'), 'focus': ('steam', 'always')}


def load_settings(path=SETTINGS_PATH):
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return settings
    except (OSError, ValueError) as exc:
        print(f'Ignoring unreadable {path}: {exc}', flush=True)
        return settings
    for key, value in saved.items():
        if key in NUMERIC and isinstance(value, (int, float)) and not isinstance(value, bool):
            settings[key] = float(value) if isinstance(DEFAULTS[key], float) else int(value)
        elif key in CHOICES and value in CHOICES[key]:
            settings[key] = value
        elif key in ('extra_classes', 'excluded_classes') and isinstance(value, list):
            settings[key] = [str(v) for v in value]
    return settings


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


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


def glasses_point(pose, prev_yaw):
    """Driver pose -> ([deg right, deg down], unwrapped yaw for the next call)."""
    yaw = pose[YAW]
    if prev_yaw is not None:   # yaw wraps at +-180; keep it continuous
        yaw = prev_yaw + (yaw - prev_yaw + 180.) % 360. - 180.
    return [-yaw, pose[PITCH]], yaw


def stick_for_rate(rate, full_rate, deadzone):
    """Camera rate (deg/s per axis) -> stick: linear in rate, full stick at the game's
    full turn rate, the game's dead zone skipped (anti-dead zone)."""
    u = [rate[0] / full_rate[0], rate[1] / full_rate[1]]
    length = math.hypot(*u)
    if length < 1e-9:
        return [0., 0.]
    amount = min(1., deadzone + (1 - deadzone) * length)
    return [u[0] / length * amount, u[1] / length * amount]


class MouseOut:
    """Relative mouse; sub-count remainders carry over, so total motion is exact."""
    def __init__(self):
        from evdev import UInput, ecodes as e
        self.e = e
        self.ui = UInput({e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT], e.EV_REL: [e.REL_X, e.REL_Y]},
                         name='XR Head Aim Mouse')
        self.remainder = [0., 0.]

    def send(self, rate, dt, s):
        e = self.e
        moved = False
        for i, code in enumerate((e.REL_X, e.REL_Y)):
            self.remainder[i] += rate[i] * s['mouse_sensitivity'] * dt
            whole = int(self.remainder[i])
            if whole:
                self.remainder[i] -= whole
                self.ui.write(e.EV_REL, code, whole)
                moved = True
        if moved:
            self.ui.syn()

    def center(self):
        self.remainder = [0., 0.]

    def close(self):
        self.ui.close()


class GamepadOut:
    """Virtual Xbox 360-style pad whose right stick carries the head aim."""
    def __init__(self):
        from evdev import UInput, AbsInfo, ecodes as e
        self.e = e
        axes = [(c, AbsInfo(0, -32768, 32767, 16, 128, 0)) for c in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]
        self.ui = UInput({e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_START, e.BTN_SELECT],
                          e.EV_ABS: axes}, name='XR Head Aim Pad', vendor=0x045e, product=0x028e, version=0x110)
        self.last = None

    def send(self, rate, dt, s):
        camera = [rate[0] * s['gain'], rate[1] * s['gain']]
        stick = stick_for_rate(camera, (s['game_full_rate'], s['game_full_rate_y']), s['game_deadzone'])
        self.write(stick)

    def write(self, stick):
        value = tuple(round(max(-1., min(1., v)) * 32767) for v in stick)
        if value == self.last:
            return
        self.last = value
        self.ui.write(self.e.EV_ABS, self.e.ABS_RX, value[0])
        self.ui.write(self.e.EV_ABS, self.e.ABS_RY, value[1])
        self.ui.syn()

    def center(self):
        self.write([0., 0.])

    def close(self):
        self.center()
        self.ui.close()


def is_game(window, s):
    cls = str(window.get('class') or window.get('initialClass') or '')
    if not cls or cls in s['excluded_classes']:
        return False
    if s['focus'] == 'always':
        return True
    return cls.startswith('steam_app_') or cls in s['extra_classes']


class Focus(threading.Thread):
    """Polls Hyprland's focused window; output is allowed only while a game has focus."""
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


def make_output(kind):
    return GamepadOut() if kind == 'gamepad' else MouseOut()


def run():
    settings = load_settings()
    mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.
    flags = {'stop': False, 'reset': False, 'paused': False}
    signal.signal(signal.SIGTERM, lambda *_: flags.update(stop=True))
    signal.signal(signal.SIGINT, lambda *_: flags.update(stop=True))
    signal.signal(signal.SIGUSR1, lambda *_: flags.update(reset=True))
    signal.signal(signal.SIGUSR2, lambda *_: flags.update(paused=not flags['paused'], reset=True))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', int(settings['port'])))
    sock.setblocking(False)
    output, kind = make_output(settings['output']), settings['output']
    focus = Focus(settings)
    focus.start()
    head = HeadRate(settings)
    yaw = None
    last_pose = 0.
    next_check = next_state = 0.
    print(f"Listening for XRLinuxDriver poses on 127.0.0.1:{settings['port']} ({kind} output)", flush=True)

    def publish(now):
        atomic_json(STATE_PATH, {'running': True, 'paused': flags['paused'], 'output': kind,
                                 'glasses': now - last_pose < 1., 'game': focus.allowed(),
                                 'window': focus.window, 'pid': os.getpid()})
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
                    if settings['output'] != kind:
                        output.close()
                        output, kind = make_output(settings['output']), settings['output']
                    print('Settings reloaded', flush=True)
            if now >= next_state:
                next_state = now + .5
                publish(now)
            if flags['reset']:
                flags['reset'] = False
                print('Paused' if flags['paused'] else 'Resumed', flush=True)
                head.reset()
                output.center()
            if not ready:
                if now - last_pose > STALE:
                    output.center()
                continue
            newest = frame = None
            while True:   # only the newest pose matters
                try:
                    data = sock.recv(128)
                except BlockingIOError:
                    break
                if len(data) >= POSE.size:
                    newest = POSE.unpack_from(data)
                    frame = FRAME.unpack_from(data, POSE.size)[0] if len(data) >= POSE.size + FRAME.size else None
            if newest is None or not all(map(math.isfinite, newest)):
                continue
            point, yaw = glasses_point(newest, yaw if now - last_pose < 1. else None)
            last_pose = now
            if flags['paused'] or not focus.allowed():
                head.reset()
                output.center()
                continue
            rate = head.step(point, frame, now)
            output.send(rate, head.dt, settings)   # head.dt: mouse motion = head motion, exactly
    finally:
        focus.stopped.set()
        output.close()
        sock.close()
        try:
            STATE_PATH.unlink()
        except OSError:
            pass
    return 0


def state():
    try:
        data = json.loads(STATE_PATH.read_text())
        pid = int(data.get('pid', 0))
        os.kill(pid, 0)
    except (OSError, ValueError):
        data = {'running': False}
    data['settings'] = load_settings()
    unit = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd' / 'user' / 'xr-head-aim.service'
    data['installed'] = unit.exists()
    print(json.dumps(data))


def tune(args):
    if args == ['reset']:
        atomic_json(SETTINGS_PATH, DEFAULTS)
        return 0
    settings = load_settings()
    for arg in args:
        key, sep, value = arg.partition('=')
        if not sep or key not in DEFAULTS:
            print(f'Unknown setting: {arg}', file=sys.stderr)
            return 2
        if key in NUMERIC:
            try:
                settings[key] = float(value) if isinstance(DEFAULTS[key], float) else int(float(value))
            except ValueError:
                print(f'{key} needs a number', file=sys.stderr)
                return 2
        elif key in CHOICES:
            if value not in CHOICES[key]:
                print(f"{key} must be one of: {', '.join(CHOICES[key])}", file=sys.stderr)
                return 2
            settings[key] = value
        else:
            settings[key] = [v for v in value.split(',') if v]
    atomic_json(SETTINGS_PATH, settings)
    return 0


def main(argv):
    if argv[:1] == ['run']:
        return run()
    if argv[:1] == ['state']:
        return state()
    if argv[:1] == ['tune'] and len(argv) > 1:
        return tune(argv[1:])
    print(__doc__.split('\n\n')[2], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
