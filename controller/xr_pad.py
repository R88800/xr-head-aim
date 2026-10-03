#!/usr/bin/env python3
"""PS5 DualSense as an Xbox 360 pad, with XR head aim blended into its right stick.

Runs as root (xr-pad.service, optional part of XR Head Aim: `install.sh --controller`).
The kernel driver (hid-playstation) exposes the DualSense as evdev nodes; this bridge
grabs them and mirrors the controller onto a virtual "Microsoft X-Box 360 pad", so every
game sees a plain Xbox pad. A udev rule (72-xr-pad.rules) hides the DualSense's own
nodes from user programs (Steam/SDL would otherwise see two controllers).

- Buttons follow the Xbox layout: Cross A, Circle B, Square X, Triangle Y, Create Back,
  Options Start, PS Guide. Triggers are analog; the touchpad is grabbed and ignored.
- Rumble requests from games are forwarded to the DualSense.
- Head aim: the XR Head Aim daemon sends "x y" (right stick, -1..1) to
  /run/xr-pad/head.sock on every glasses pose; it is blended as
  physical + head * (1 - |physical|), and dropped 0.25 s after the last packet.
- The virtual pad survives controller disconnects (sleep, Bluetooth drops) for
  GRACE seconds, so a running game keeps the same pad; all inputs are released while
  the controller is away. /run/xr-pad/state says whether the controller is connected.
"""
import ctypes
import json
import math
import os
import pwd
import select
import socket
import sys
import time
from pathlib import Path

from evdev import AbsInfo, InputDevice, UInput, ecodes as e, ff, list_devices

RUN = Path(os.environ.get('XR_PAD_RUN', '/run/xr-pad'))
SONY = 0x054c
DUALSENSE = {0x0ce6, 0x0df2}   # DualSense, DualSense Edge
GRACE = 600.                   # s the virtual pad outlives a disconnected controller
HEAD_STALE = .25               # s without head packets before head aim is dropped
REST = .06                     # physical stick radius ignored while head aim is active

# Same identity and capabilities as InputPlumber's xb360 target (proven with Steam/Proton).
PAD_NAME = 'Microsoft X-Box 360 pad'
STICK = AbsInfo(0, -32768, 32767, 16, 128, 0)
CAPS = {
    e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL, e.BTN_TR, e.BTN_SELECT,
               e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR, e.BTN_TRIGGER_HAPPY1,
               e.BTN_TRIGGER_HAPPY2, e.BTN_TRIGGER_HAPPY3, e.BTN_TRIGGER_HAPPY4],
    e.EV_ABS: [(e.ABS_X, STICK), (e.ABS_Y, STICK), (e.ABS_RX, STICK), (e.ABS_RY, STICK),
               (e.ABS_Z, AbsInfo(0, 0, 255, 0, 0, 0)), (e.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),
               (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)), (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0))],
    e.EV_FF: [e.FF_RUMBLE, e.FF_PERIODIC, e.FF_SQUARE, e.FF_TRIANGLE, e.FF_SINE, e.FF_GAIN],
}
# hid-playstation follows the kernel's positional names (Triangle = BTN_NORTH); Xbox pads
# (xpad) report X as BTN_NORTH and Y as BTN_WEST, which is what games' Xbox mappings expect.
BUTTONS = {e.BTN_SOUTH: e.BTN_SOUTH, e.BTN_EAST: e.BTN_EAST, e.BTN_WEST: e.BTN_NORTH,
           e.BTN_NORTH: e.BTN_WEST, e.BTN_TL: e.BTN_TL, e.BTN_TR: e.BTN_TR,
           e.BTN_SELECT: e.BTN_SELECT, e.BTN_START: e.BTN_START, e.BTN_MODE: e.BTN_MODE,
           e.BTN_THUMBL: e.BTN_THUMBL, e.BTN_THUMBR: e.BTN_THUMBR}
STICKS = (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)


def blend(physical, head):
    """Right stick: the physical stick plus head aim in the room the stick leaves."""
    px, py = physical
    if head == (0., 0.):
        return physical
    if math.hypot(px, py) < REST:   # resting offset would skew small head moves
        px = py = 0.
    room = 1 - min(1., math.hypot(px, py))
    x, y = px + head[0] * room, py + head[1] * room
    norm = math.hypot(x, y)
    return (x / norm, y / norm) if norm > 1 else (x, y)


def parse_head(data):
    try:
        x, y = (float(v) for v in data.split())
    except ValueError:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return max(-1., min(1., x)), max(-1., min(1., y))


def find_controller():
    """-> (gamepad, [other nodes of the same controller]) or None."""
    devices = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.info.vendor == SONY and dev.info.product in DUALSENSE:
            devices.append(dev)
        else:
            dev.close()
    pads = [d for d in devices if e.ABS_RX in dict(d.capabilities().get(e.EV_ABS, []))
            and e.BTN_SOUTH in d.capabilities().get(e.EV_KEY, [])]
    if not pads:
        for d in devices:
            d.close()
        return None
    pad = pads[0]
    others = [d for d in devices if d is not pad and d.uniq == pad.uniq]
    for d in devices:
        if d is not pad and d not in others:
            d.close()
    return pad, others


class Bridge:
    def __init__(self, user=None):
        self.ui = None
        self.pad = None
        self.others = []
        self.lost_at = None
        self.scale = {}
        self.right = (0., 0.)
        self.head = (0., 0.)
        self.head_t = 0.
        self.sent_right = None
        self.effects = {}      # virtual effect id -> physical effect id
        RUN.mkdir(parents=True, exist_ok=True)
        os.chmod(RUN, 0o755)
        self.sock_path = RUN / 'head.sock'
        self.sock_path.unlink(missing_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        if user:
            os.chown(self.sock_path, pwd.getpwnam(user).pw_uid, -1)
        self.write_state()

    # -- state ---------------------------------------------------------------------
    def write_state(self):
        tmp = RUN / '.state'
        tmp.write_text(json.dumps({'connected': self.pad is not None, 'pad': self.ui is not None}) + '\n')
        os.chmod(tmp, 0o644)
        tmp.replace(RUN / 'state')

    # -- controller ----------------------------------------------------------------
    def attach(self, found):
        self.pad, self.others = found
        for dev in (self.pad, *self.others):
            try:
                dev.grab()
            except OSError:
                pass
        self.scale = {}
        for code, info in self.pad.capabilities(absinfo=True).get(e.EV_ABS, []):
            self.scale[code] = info
        if self.ui is None:
            self.ui = UInput(CAPS, name=PAD_NAME, vendor=0x045e, product=0x028e, version=0x0001,
                             bustype=e.BUS_USB, max_effects=16)
        self.lost_at = None
        self.effects = {}
        self.sync_from_device()
        self.write_state()
        print(f'Controller connected: {self.pad.name} ({self.pad.uniq or self.pad.path})', flush=True)

    def detach(self):
        for dev in (self.pad, *self.others):
            try:
                dev.close()
            except OSError:
                pass
        self.pad, self.others, self.lost_at = None, [], time.monotonic()
        self.effects = {}
        self.release_all()
        self.write_state()
        print('Controller disconnected: inputs released, Xbox pad kept', flush=True)

    def release_all(self):
        if self.ui is None:
            return
        for code in CAPS[e.EV_KEY]:
            self.ui.write(e.EV_KEY, code, 0)
        for code, _ in CAPS[e.EV_ABS]:
            self.ui.write(e.EV_ABS, code, 0)
        self.ui.syn()
        self.right, self.sent_right = (0., 0.), None

    def sync_from_device(self):
        """Start from the controller's current state (sticks resting off-center etc.)."""
        self.release_all()
        for code in self.pad.active_keys():
            if code in BUTTONS:
                self.ui.write(e.EV_KEY, BUTTONS[code], 1)
        for code, info in self.scale.items():
            self.on_abs(code, info.value)
        self.ui.syn()

    def stick_value(self, code, value):
        info = self.scale[code]
        center = (info.min + info.max + 1) // 2      # 128 for the DualSense's 0..255
        half = (info.max - center) if value >= center else (center - info.min)
        return max(-1., min(1., (value - center) / max(1, half)))

    def on_abs(self, code, value):
        if code in STICKS:
            v = self.stick_value(code, value)
            if code == e.ABS_RX:
                self.right = (v, self.right[1])
            elif code == e.ABS_RY:
                self.right = (self.right[0], v)
            else:
                self.ui.write(e.EV_ABS, code, round(v * 32767))
        elif code in (e.ABS_Z, e.ABS_RZ):
            info = self.scale[code]
            self.ui.write(e.EV_ABS, code, round((value - info.min) * 255 / max(1, info.max - info.min)))
        elif code in (e.ABS_HAT0X, e.ABS_HAT0Y):
            self.ui.write(e.EV_ABS, code, max(-1, min(1, value)))

    def write_right(self, syn=True):
        if self.ui is None:
            return
        out = tuple(round(v * 32767) for v in blend(self.right, self.head))
        if out != self.sent_right:
            self.sent_right = out
            self.ui.write(e.EV_ABS, e.ABS_RX, out[0])
            self.ui.write(e.EV_ABS, e.ABS_RY, out[1])
            if syn:
                self.ui.syn()

    def on_controller(self):
        for ev in self.pad.read():
            if ev.type == e.EV_KEY and ev.code in BUTTONS:
                self.ui.write(e.EV_KEY, BUTTONS[ev.code], ev.value)
            elif ev.type == e.EV_ABS and ev.code in self.scale:
                self.on_abs(ev.code, ev.value)
            elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
                self.write_right(syn=False)
                self.ui.syn()

    # -- rumble ----------------------------------------------------------------------
    def on_virtual(self):
        """Games' force-feedback requests. Uploads must be answered at once: an
        unanswered upload blocks the game's ioctl until the uinput timeout."""
        for ev in self.ui.read():
            if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                upload = self.ui.begin_upload(ev.value)
                upload.retval = 0
                if self.pad is not None:
                    effect = ff.Effect()
                    ctypes.pointer(effect)[0] = upload.effect
                    effect.id = self.effects.get(upload.effect.id, -1)
                    try:
                        self.effects[upload.effect.id] = self.pad.upload_effect(effect)
                    except OSError:
                        pass   # the game keeps working without rumble
                self.ui.end_upload(upload)
            elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                erase = self.ui.begin_erase(ev.value)
                erase.retval = 0
                physical = self.effects.pop(erase.effect_id, None)
                if physical is not None and self.pad is not None:
                    try:
                        self.pad.erase_effect(physical)
                    except OSError:
                        pass
                self.ui.end_erase(erase)
            elif ev.type == e.EV_FF and self.pad is not None:
                code = ev.code if ev.code == e.FF_GAIN else self.effects.get(ev.code)
                if code is not None:
                    try:
                        self.pad.write(e.EV_FF, code, ev.value)
                    except OSError:
                        pass

    # -- head aim ----------------------------------------------------------------------
    def on_head(self, now):
        while True:
            try:
                data = self.sock.recv(64)
            except BlockingIOError:
                break
            head = parse_head(data)
            if head is not None:
                self.head, self.head_t = head, now
        if self.pad is not None:
            self.write_right()

    # -- main loop ---------------------------------------------------------------------
    def run(self, stopped=lambda: False):
        self.sock.setblocking(False)
        next_scan = 0.
        while not stopped():
            now = time.monotonic()
            if self.pad is None and now >= next_scan:
                next_scan = now + 1.
                found = find_controller()
                if found:
                    self.attach(found)
                elif self.ui is not None and self.lost_at is not None and now - self.lost_at > GRACE:
                    self.ui.close()
                    self.ui = None
                    self.write_state()
                    print('Xbox pad removed (controller away too long)', flush=True)
            fds = [self.sock] + ([self.pad.fd] if self.pad else []) + ([self.ui.fd] if self.ui else [])
            ready, _, _ = select.select(fds, [], [], .05)
            now = time.monotonic()
            try:
                if self.pad is not None and self.pad.fd in ready:
                    self.on_controller()
            except OSError:          # unplugged, Bluetooth drop or sleep
                self.detach()
            if self.ui is not None and self.ui.fd in ready:
                self.on_virtual()
            if self.sock in ready:
                self.on_head(now)
            if self.head != (0., 0.) and now - self.head_t > HEAD_STALE:
                self.head = (0., 0.)
                if self.pad is not None:
                    self.write_right()

    def close(self):
        if self.pad is not None:
            self.detach()
        if self.ui is not None:
            self.ui.close()
        self.sock.close()
        self.sock_path.unlink(missing_ok=True)
        (RUN / 'state').unlink(missing_ok=True)


def main():
    import signal
    stop = {'now': False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    bridge = Bridge(os.environ.get('XR_PAD_USER'))
    print('Waiting for a DualSense (USB or Bluetooth)', flush=True)
    try:
        bridge.run(lambda: stop['now'])
    finally:
        bridge.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
