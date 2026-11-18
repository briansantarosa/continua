"""pause_test.py — the rest state (continua.paused), 2026-09-29.
Run: python3 pause_test.py
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pause          # noqa: E402
import wake           # noqa: E402
import ritual         # noqa: E402
import bridge         # noqa: E402


def _fixture_root(tmp, paused, reason=None):
    os.makedirs(os.path.join(tmp, "configs"), exist_ok=True)
    body = ["continua:", "  paused: " + ("true" if paused else "false"),
            "  wake:", "    enabled: true", "    interval_min: 60"]
    if reason:
        body.append('  paused_reason: "%s"' % reason)
    with open(os.path.join(tmp, "configs", "t.yaml"), "w") as f:
        f.write("\n".join(body) + "\n")
    return tmp


class IsPausedTests(unittest.TestCase):
    def test_yaml_true_false(self):
        t1 = _fixture_root(tempfile.mkdtemp(), True)
        t2 = _fixture_root(tempfile.mkdtemp(), False)
        self.assertTrue(pause.is_paused("t", root=t1))
        self.assertFalse(pause.is_paused("t", root=t2))

    def test_env_beats_yaml(self):
        t = _fixture_root(tempfile.mkdtemp(), True)
        with patch.dict(os.environ, {"CONTINUA_PAUSE_T": "0"}):
            self.assertFalse(pause.is_paused("t", root=t))
        t2 = _fixture_root(tempfile.mkdtemp(), False)
        with patch.dict(os.environ, {"CONTINUA_PAUSE_T": "1"}):
            self.assertTrue(pause.is_paused("t", root=t2))

    def test_fail_open(self):
        self.assertFalse(pause.is_paused("nonexistent-inst"))
        self.assertFalse(pause.is_paused("t", cfg=None, root="/nonexistent"))

    def test_reason(self):
        t = _fixture_root(tempfile.mkdtemp(), True, reason="resting")
        self.assertEqual(pause.paused_reason("t", root=t), "resting")


class SchedulerGatesTests(unittest.TestCase):
    def test_enabled_instances_skips_paused(self):
        """Boot discovery: a resting resident arms no consumer. Note the
        convention difference: enabled_instances takes the CONFIGS DIR;
        is_paused takes the parent root."""
        t = _fixture_root(tempfile.mkdtemp(), True)
        self.assertNotIn("t", wake.enabled_instances(os.path.join(t, "configs")))
        t2 = _fixture_root(tempfile.mkdtemp(), False)
        self.assertIn("t", wake.enabled_instances(os.path.join(t2, "configs")))

    def test_ritual_loop_skips_paused(self):
        """The nightly loop: paused residents are not pulsed (the explicit
        --instance deliberate run still honors env OVERRIDES only)."""
        import inspect
        src = inspect.getsource(ritual.main)
        self.assertIn("_pause.is_paused(i)", src)
        self.assertIn("if not args.instance", src)

    def test_timer_loop_skips_paused(self):
        import inspect
        src = inspect.getsource(wake)
        self.assertIn("is resting (continua.paused)", src)


class RouterPauseTests(unittest.TestCase):
    def test_arrival_recorder(self):
        """A paused arrival lands in the chronicle: user role, the text,
        the machinery provenance — the fact recorded, the reply silent."""
        import json, tempfile, shutil, glob
        import chronicle as ch
        td = tempfile.mkdtemp(prefix="paused-")
        with patch.object(ch, "DEFAULT_ROOT", td):
            bridge._record_paused_arrival("residenta", "123",
                                          "hello while you rest")
        hits = []
        for f in glob.glob(os.path.join(td, "**", "*.jsonl"), recursive=True):
            for line in open(f, encoding="utf-8", errors="ignore"):
                if "paused-arrival" in line:
                    hits.append(json.loads(line))
        self.assertTrue(hits, "the arrival was not recorded")
        row = hits[-1]
        self.assertEqual(row.get("role"), "user")
        self.assertEqual(row.get("person_id"), "123")
        self.assertIn("machinery", str(row.get("content", "")))   # the marked voice
        self.assertEqual(row.get("source"), "paused")
        self.assertIn("hello while you rest", str(row.get("content", "")))

    def test_router_branch_present(self):
        import inspect
        src = inspect.getsource(bridge._run_agent_turn)
        self.assertIn("_is_paused", src)
        self.assertIn("_record_paused_arrival", src)
        self.assertIn("she is resting right now", src)
        self.assertIn("not her words", src)


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _null():
    import contextlib
    return contextlib.nullcontext()


if __name__ == "__main__":
    unittest.main()