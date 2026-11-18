"""fluid_test.py — Phase F (memorybuild2 §5b): the fluid budget module.
Run: python3 fluid_test.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fluid   # noqa: E402


def _msg(role, text):
    return {"role": role, "content": text}


class EnabledTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(fluid.enabled({}))            # absent key = off
        self.assertFalse(fluid.enabled(None))
        self.assertFalse(fluid.enabled(
            {"memory": {"fluid_budget": False}}))

    def test_yaml_enables(self):
        self.assertTrue(fluid.enabled(
            {"memory": {"fluid_budget": True}}))

    def test_kill_switch_wins(self):
        with mock.patch.dict(os.environ, {"CONTINUA_FLUID": "0"}):
            self.assertFalse(fluid.enabled(
                {"memory": {"fluid_budget": True}}))


class IdleBytesTests(unittest.TestCase):
    def test_sum_of_slack(self):
        self.assertEqual(fluid.idle_bytes(
            {"notes": 573, "juggle": 14, "doc": 189000},
            {"notes": 2645, "juggle": 15000, "doc": 189258}),
            2072 + 14986 + 258)     # 2645-573 + 15000-14 + 189258-189000

    def test_over_cap_layer_lends_nothing(self):
        self.assertEqual(fluid.idle_bytes(
            {"notes": 3000}, {"notes": 2645}), 0)

    def test_missing_layer_lends_whole_cap(self):
        self.assertEqual(fluid.idle_bytes({}, {"notes": 2645}), 2645)

    def test_unpaired_keys_ignored(self):
        self.assertEqual(fluid.idle_bytes(
            {"history": 9000}, {"notes": 2000}), 2000)


class FloatTargetTests(unittest.TestCase):
    def test_float_stays_under_cap(self):
        self.assertEqual(fluid.float_target(12000, 8000, 20000), 20000)
        self.assertEqual(fluid.float_target(12000, 3000, 20000), 15000)

    def test_no_idle_stays_at_target(self):
        self.assertEqual(fluid.float_target(12000, 0, 20000), 12000)

    def test_fill_mode_no_float_room(self):
        # residentb: target == cap → nothing to lend into
        self.assertEqual(fluid.float_target(25000, 5000, 25000), 25000)

    def test_bad_target_none(self):
        self.assertIsNone(fluid.float_target(None, 100, 20000))


class TopUpTests(unittest.TestCase):
    def test_readds_newest_first_within_budget(self):
        evicted = [_msg("user", "u-old"), _msg("assistant", "a-old"),
                   _msg("user", "u-new"), _msg("assistant", "a-new")]
        history = [_msg("user", "live")]
        hist, added = fluid.top_up(history, evicted, budget_chars=12)
        # newest pair fits (4+6=10 ≤ 12); the old pair would exceed
        self.assertEqual([m["content"] for m in hist],
                         ["live", "u-new", "a-new"])
        self.assertEqual(added, 10)
        # the fold list keeps ONLY what is still evicted
        self.assertEqual([m["content"] for m in evicted],
                         ["u-old", "a-old"])

    def test_never_opens_on_orphan_assistant(self):
        evicted = [_msg("user", "u-old"), _msg("assistant", "a-old")]
        history = []
        # budget fits only the assistant → it must be skipped entirely
        hist, added = fluid.top_up(history, evicted, budget_chars=6)
        self.assertEqual(hist, [])
        self.assertEqual(added, 0)

    def test_hard_cap_binds(self):
        evicted = [_msg("user", "u-old"), _msg("assistant", "a-old"),
                   _msg("user", "u-new"), _msg("assistant", "a-new")]
        history = [_msg("user", "x" * 90)]
        hist, added = fluid.top_up(history, evicted, budget_chars=100,
                                   hard_cap_chars=100)
        # the cap forces the oldest re-added pair back out
        self.assertEqual([m["content"] for m in hist][-2:],
                         ["u-new", "a-new"])
        self.assertEqual(added, 10)

    def test_no_evicted_no_op(self):
        history = [_msg("user", "live")]
        hist, added = fluid.top_up(history, [], budget_chars=500)
        self.assertEqual(hist, history)
        self.assertEqual(added, 0)

    def test_consecutive_user_messages_ok(self):
        # merged-fragment histories carry consecutive user messages; the
        # re-add walks them newest-first without pair assumptions
        evicted = [_msg("user", "part one"), _msg("user", "part two"),
                   _msg("assistant", "reply")]
        history = []
        hist, added = fluid.top_up(history, evicted, budget_chars=100)
        self.assertEqual([m["content"] for m in hist],
                         ["part one", "part two", "reply"])
        self.assertEqual(evicted, [])


if __name__ == "__main__":
    unittest.main()