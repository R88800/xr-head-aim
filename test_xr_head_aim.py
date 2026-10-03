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
        p, yaw = x.glasses_point((0, 0, 0, 179., 2., 0), None)
        self.assertEqual(p, [-179., 2.])
        p, yaw = x.glasses_point((0, 0, 0, -179., 2., 0), yaw)
        self.assertAlmostEqual(p[0], -181.)   # crossed +-180 without a 358 deg jump

    def test_stick_skips_dead_zone_and_saturates(self):
        s = x.stick_for_rate([82.5, 0.], (165., 135.), .06)
        self.assertAlmostEqual(s[0], .06 + .94 * .5)
        self.assertEqual(x.stick_for_rate([0., 0.], (165., 135.), .06), [0., 0.])
        self.assertAlmostEqual(x.stick_for_rate([0., -400.], (165., 135.), .06)[1], -1.)

    def test_focus_gate(self):
        s = dict(x.DEFAULTS, extra_classes=['heroic_game'], excluded_classes=['steam_app_1145360'])
        self.assertTrue(x.is_game({'class': 'steam_app_976730'}, s))
        self.assertTrue(x.is_game({'class': 'heroic_game'}, s))
        self.assertFalse(x.is_game({'class': 'steam_app_1145360'}, s))
        self.assertFalse(x.is_game({'class': 'firefox'}, s))
        self.assertFalse(x.is_game({}, dict(s, focus='always')))
        self.assertTrue(x.is_game({'class': 'firefox'}, dict(s, focus='always')))


class MouseTests(unittest.TestCase):
    def test_mouse_motion_equals_head_motion(self):
        out = x.MouseOut.__new__(x.MouseOut)
        out.e = mock.Mock(REL_X=0, REL_Y=1, EV_REL=2)
        out.ui = mock.Mock()
        out.remainder = [0., 0.]
        s = dict(x.DEFAULTS, mouse_sensitivity=40.)
        for _ in range(100):
            out.send([3.3, -1.1], .0087, s)   # 3.3 deg/s for 0.87 s
        sent = [0, 0]
        for call in out.ui.write.call_args_list:
            sent[call.args[1]] += call.args[2]
        self.assertAlmostEqual(sent[0] + out.remainder[0], 3.3 * .87 * 40., places=6)
        self.assertAlmostEqual(sent[1] + out.remainder[1], -1.1 * .87 * 40., places=6)


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
        self.assertEqual(x.tune(['gain=6.5', 'output=gamepad', 'invert_y=1']), 0)
        s = x.load_settings(self.path)
        self.assertEqual((s['gain'], s['output'], s['invert_y']), (6.5, 'gamepad', 1))
        self.assertEqual(x.tune(['output=keyboard']), 2)
        self.assertEqual(x.tune(['nope=1']), 2)
        self.assertEqual(x.tune(['reset']), 0)
        self.assertEqual(x.load_settings(self.path), x.DEFAULTS)

    def test_bad_values_fall_back_to_defaults(self):
        self.path.write_text(json.dumps({'gain': 'fast', 'output': 'joystick', 'port': True}))
        self.assertEqual(x.load_settings(self.path), x.DEFAULTS)
        self.path.write_text('{broken')
        self.assertEqual(x.load_settings(self.path), x.DEFAULTS)


class ControllerTests(unittest.TestCase):
    def test_auto_uses_the_controller_only_while_connected(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(x, 'MouseOut') as mouse:
            state = Path(d) / 'state'
            sock_path = Path(d) / 'head.sock'
            with mock.patch.object(x, 'PAD_STATE', state), mock.patch.object(x, 'PAD_SOCKET', sock_path):
                import socket
                bridge = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                bridge.bind(str(sock_path))
                bridge.settimeout(.5)
                out = x.AutoOut()
                out.send([10., 0.], .008, x.DEFAULTS)
                self.assertEqual(out.kind, 'mouse')
                mouse.return_value.send.assert_called_once()
                state.write_text('{"connected": true}')
                out.checked = 0.
                out.send([10., 0.], .008, x.DEFAULTS)
                self.assertEqual(out.kind, 'controller')
                mouse.return_value.center.assert_called()      # leaving the mouse stops it
                stick = [float(v) for v in bridge.recv(64).split()]
                expected = x.stick_for_rate([10. * x.DEFAULTS['gain'], 0.], (165., 135.), x.DEFAULTS['game_deadzone'])
                self.assertAlmostEqual(stick[0], expected[0], places=4)
                state.write_text('{"connected": false}')
                out.checked = 0.
                out.send([10., 0.], .008, x.DEFAULTS)
                self.assertEqual(out.kind, 'mouse')
                self.assertEqual(bridge.recv(64).split(), [b'0.00000', b'0.00000'])   # stick released
                out.close()
                bridge.close()

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
