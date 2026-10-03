"""Offline tests: no glasses, no uinput, no Hyprland needed."""
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import xr_head_aim as x


def settings(**over):
    s = dict(x.DEFAULTS, still_from=0., still_to=1e-6, precision=1.)
    s.update(over)
    return s


class HeadRateTests(unittest.TestCase):
    def test_accelerating_turn_reports_current_rate(self):
        head = x.HeadRate(settings())
        head.period = .008
        for i in range(12):
            t = i * .008
            rate = head.step([.5 * 2000. * t * t, 0.], i, t)
        self.assertAlmostEqual(rate[0], 2000. * 11 * .008, places=6)
        head.s['predict'] = 0.
        head.reset()
        for i in range(12):
            t = i * .008
            rate = head.step([.5 * 2000. * t * t, 0.], i, t)
        self.assertAlmostEqual(rate[0], 2000. * 10.5 * .008, places=6)

    def test_still_head_gives_exactly_zero(self):
        head = x.HeadRate(dict(x.DEFAULTS))
        for i in range(50):
            rate = head.step([10. + (.0004 if i % 2 else 0.), -3.], i, i * .009)
        self.assertEqual(rate, [0., 0.])

    def test_gap_restarts_instead_of_jumping(self):
        head = x.HeadRate(settings())
        head.step([0., 0.], 0, 0.)
        self.assertEqual(head.step([30., 0.], 100, 1.), [0., 0.])

    def test_frame_counter_wraps(self):
        head = x.HeadRate(settings())
        head.period = .01
        head.step([0., 0.], 0xFFFFFFFF, 0.)
        rate = head.step([.1, 0.], 0, .01)
        self.assertAlmostEqual(rate[0], 10., places=6)

    def test_vertical_ratio_and_invert(self):
        head = x.HeadRate(settings(vertical_ratio=.5, invert_y=1, predict=0.))
        head.period = .01
        head.step([0., 0.], 0, 0.)
        rate = head.step([0., .1], 1, .01)
        self.assertAlmostEqual(rate[1], -5., places=6)


class MappingTests(unittest.TestCase):
    def test_yaw_unwraps_and_axes_point_right_down(self):
        p, yaw = x.head_angles((0, 0, 0, 179., 2., 0), None)
        self.assertEqual(p, [-179., 2.])
        p, yaw = x.head_angles((0, 0, 0, -179., 2., 0), yaw)
        self.assertAlmostEqual(p[0], -181.)   # crossed +-180 without a 358 deg jump

    def test_stick_skips_dead_zone_and_saturates(self):
        s = x.stick_for_rate([82.5, 0.], 165., .06)
        self.assertAlmostEqual(s[0], .06 + .94 * .5)
        self.assertEqual(x.stick_for_rate([0., 0.], 165., .06), [0., 0.])
        self.assertAlmostEqual(x.stick_for_rate([0., -400.], 165., .06)[1], -1.)

    def test_focus_gate(self):
        s = dict(x.DEFAULTS, extra_classes=['heroic_game'], excluded_classes=['steam_app_1145360'])
        self.assertTrue(x.is_game({'class': 'steam_app_976730'}, s))
        self.assertTrue(x.is_game({'class': 'heroic_game'}, s))
        self.assertFalse(x.is_game({'class': 'steam_app_1145360'}, s))
        self.assertFalse(x.is_game({'class': 'firefox'}, s))
        self.assertFalse(x.is_game({}, s))


class MouseTests(unittest.TestCase):
    def test_mouse_motion_equals_head_motion(self):
        out = x.Mouse.__new__(x.Mouse)
        out.e = mock.Mock(REL_X=0, REL_Y=1, EV_REL=2)
        out.ui = mock.Mock()
        out.remainder = [0., 0.]
        s = dict(x.DEFAULTS, gain=4., game_mouse_deg=.1)   # 40 counts per head degree
        for _ in range(100):
            out.aim([3.3, -1.1], .0087, s)   # 3.3 deg/s for 0.87 s
        sent = [0, 0]
        for call in out.ui.write.call_args_list:
            sent[call.args[1]] += call.args[2]
        self.assertAlmostEqual(sent[0] + out.remainder[0], 3.3 * .87 * 40., places=6)
        self.assertAlmostEqual(sent[1] + out.remainder[1], -1.1 * .87 * 40., places=6)

    def test_same_sensitivity_on_every_output(self):
        s = dict(x.DEFAULTS, gain=4., game_mouse_deg=.05, game_full_rate=200., game_deadzone=0.)
        self.assertEqual(x.view_rate([10., -5.], s), [40., -20.])
        # mouse: 40 deg/s of view for 1 s = 800 counts at 0.05 deg/count
        mouse = x.Mouse.__new__(x.Mouse)
        mouse.e = mock.Mock(REL_X=0, REL_Y=1, EV_REL=2)
        mouse.ui = mock.Mock()
        mouse.remainder = [0., 0.]
        for _ in range(100):
            mouse.aim([10., 0.], .01, s)
        self.assertEqual(sum(c.args[2] for c in mouse.ui.write.call_args_list if c.args[1] == 0), 800)
        # stick: 40 deg/s of view at a 200 deg/s full-stick game = 20% stick
        self.assertAlmostEqual(x.stick_for_rate(x.view_rate([10., 0.], s), 200., 0.)[0], .2)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'settings.json'
        self.patch = mock.patch.object(x, 'SETTINGS_PATH', self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_tune_validates_and_round_trips(self):
        self.assertEqual(x.tune(['gain=6.5', 'invert_y=1', 'excluded_classes=steam_app_1,steam_app_2']), 0)
        s = x.load_settings(self.path)
        self.assertEqual((s['gain'], s['invert_y'], s['excluded_classes']), (6.5, 1, ['steam_app_1', 'steam_app_2']))
        self.assertEqual(x.tune(['gain=fast']), 2)
        self.assertEqual(x.tune(['nope=1']), 2)
        self.assertEqual(x.tune(['output=keyboard']), 2)
        self.assertEqual(x.tune(['output=controller']), 0)
        self.assertEqual(x.load_settings(self.path)['output'], 'controller')

    def test_reset_keeps_on_off_and_game_lists(self):
        x.tune(['gain=9', 'still_from=1', 'enabled=0', 'output=mouse', 'extra_classes=heroic'])
        x.tune(['reset'])
        s = x.load_settings(self.path)
        self.assertEqual((s['gain'], s['still_from'], s['enabled'], s['output'], s['extra_classes']),
                         (x.DEFAULTS['gain'], x.DEFAULTS['still_from'], 0, 'mouse', ['heroic']))

    def test_toggle_on_off(self):
        x.switch('toggle')
        self.assertEqual(x.load_settings(self.path)['enabled'], 0)
        x.switch('toggle')
        self.assertEqual(x.load_settings(self.path)['enabled'], 1)
        x.switch('off')
        x.switch('off')
        self.assertEqual(x.load_settings(self.path)['enabled'], 0)

    def test_old_and_bad_values_are_ignored(self):
        self.path.write_text(json.dumps({'gain': 'fast', 'output': 'gamepad', 'port': 4244, 'focus': 'always', 'mouse_sensitivity': 48,
                                         'still_to': True, 'vertical_ratio': 1}))
        self.assertEqual(x.load_settings(self.path), dict(x.DEFAULTS, vertical_ratio=1.))
        self.path.write_text('{broken')
        self.assertEqual(x.load_settings(self.path), x.DEFAULTS)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bridge = Path(self.dir.name)
        self.patches = [mock.patch.object(x, 'BRIDGE', self.bridge),
                        mock.patch.object(x, 'Mouse', type('Mouse', (mock.Mock,), {})),
                        mock.patch.object(x, 'VirtualPad', type('VirtualPad', (mock.Mock,), {}))]
        for p in self.patches:
            p.start()
        x.Output.NAMES = {x.Mouse: 'mouse', x.ControllerStick: 'controller', x.VirtualPad: 'virtual pad'}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        x.Output.NAMES = {x.Mouse: 'mouse', x.ControllerStick: 'controller', x.VirtualPad: 'virtual pad'}
        self.dir.cleanup()

    def output(self, mode):
        out = x.Output(dict(x.DEFAULTS, output=mode))
        out.follow()
        return out

    def connect(self, connected):
        (self.bridge / 'state').write_text(json.dumps({'connected': connected}))

    def test_auto_follows_the_controller(self):
        import socket
        bridge = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        bridge.bind(str(self.bridge / 'head.sock'))
        bridge.settimeout(.5)
        out = self.output('auto')
        self.assertEqual(out.name, 'mouse')
        self.connect(True)
        out.checked = 0.
        out.aim([10., 0.], .008)
        self.assertEqual(out.name, 'controller')
        stick = [float(v) for v in bridge.recv(64).split()]
        expected = x.stick_for_rate([10. * x.DEFAULTS['gain'], 0.], 165., x.DEFAULTS['game_deadzone'])
        self.assertAlmostEqual(stick[0], expected[0], places=4)
        self.connect(False)
        out.checked = 0.
        out.aim([10., 0.], .008)
        self.assertEqual(out.name, 'mouse')
        self.assertEqual(bridge.recv(64).split(), [b'0.00000', b'0.00000'])   # stick released
        out.close()
        bridge.close()

    def test_controller_mode_uses_the_bridge_or_a_virtual_pad(self):
        self.assertEqual(self.output('controller').name, 'controller')
        self.bridge.rmdir()
        self.assertEqual(self.output('controller').name, 'virtual pad')
        self.assertEqual(self.output('mouse').name, 'mouse')

    def test_leaving_controller_mode_removes_the_virtual_pad(self):
        self.bridge.rmdir()
        out = self.output('controller')
        pad = out.target
        out.s['output'] = 'mouse'
        out.checked = 0.
        out.follow()
        self.assertEqual(out.name, 'mouse')
        pad.close.assert_called_once()
        self.assertNotIn(x.VirtualPad, out.devices)

class ControllerTests(unittest.TestCase):
    def test_bridge_blend(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent / 'controller'))
        import xr_pad
        self.assertEqual(xr_pad.blend((.3, -.2), (0., 0.)), (.3, -.2))            # head off: physical as is
        self.assertEqual(xr_pad.blend((.03, .02), (.5, 0.)), (.5, 0.))           # resting offset ignored
        bx, by = xr_pad.blend((.6, 0.), (.5, 0.))
        self.assertAlmostEqual(bx, .6 + .5 * .4)                                  # head fills the remaining room
        self.assertAlmostEqual(math.hypot(*xr_pad.blend((1., 0.), (0., 1.))), 1.)  # never past full stick
        self.assertEqual(xr_pad.parse_head(b'0.25 -2'), (.25, -1.))
        self.assertIsNone(xr_pad.parse_head(b'nan 0'))
        self.assertIsNone(xr_pad.parse_head(b'oops'))


if __name__ == '__main__':
    unittest.main()
