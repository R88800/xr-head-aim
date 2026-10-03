"""Offline tests: no glasses, no uinput, no Hyprland needed."""
import json
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


if __name__ == '__main__':
    unittest.main()
