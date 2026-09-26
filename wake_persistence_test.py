"""chunk 3 exit tests (memory plan §6g): persistent wake threads.

Consecutive wakes see their previous exchanges, restart retains them,
residents stay isolated, no tool pair is split by the trim, and evicted
exchanges reach the summary fold. Agent-free except for pure logic;
all writes under /tmp.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import core  # noqa: E402
import session_memory  # noqa: E402


def bare_trim_agent(instance_id="residentb", max_history_chars=20000):
    """A minimal stand-in exposing only what _trim_history_with_evicted uses."""
    agent = object.__new__(core.SagentCore)
    agent.instance_id = instance_id
    agent.max_history_chars = max_history_chars
    return agent


def tool_call_msg(name="save_memory", args='{"text": "remember this"}'):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def wake_turn(i, tool=True):
    """One complete wake exchange: prompt, (assistant+tool result), reply."""
    msgs = [{"role": "user", "content": f"[wake {i}] state packet"}]
    if tool:
        msgs.append(tool_call_msg())
        msgs.append({"role": "tool", "tool_call_id": "call_1",
                     "content": f"[wake {i}] tool result"})
    msgs.append({"role": "assistant",
                 "content": f"[wake {i}] her reply: I sat with the silence. "
                            + "The quiet held. " * 40})
    return msgs


class WakePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wake-persist-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.config = os.path.join(self.tmp, "residenta_yaml")
        os.makedirs(self.config, exist_ok=True)
        self.agent = bare_trim_agent()

    # --- restart retains: disk roundtrip ---------------------------------
    def test_history_roundtrip_survives_restart(self):
        hist = wake_turn(1)
        bridge._save_history_to_disk(self.config, "system-wake", hist)
        loaded = bridge._load_history_from_disk(self.config, "system-wake")
        self.assertEqual(loaded, hist)

    # --- residents stay isolated ------------------------------------------
    def test_residents_isolated(self):
        self.assertNotEqual(bridge._history_filepath(self.config, "system-wake"),
                            bridge._history_filepath(self.config + "2", "system-wake"))

    # --- no tool pair is split across the eviction boundary ---------------
    def test_trim_never_splits_a_tool_pair(self):
        # five complete wake exchanges; budget forces eviction of the oldest
        hist = []
        for i in range(5):
            hist.extend(wake_turn(i))
        kept, evicted = self.agent._trim_history_with_evicted(
            hist, target_chars=2500)
        self.assertTrue(kept and evicted)
        # invariant 1: every evicted tool result's assistant is evicted too
        evicted_assistants = {id(m) for m in evicted}
        for idx, m in enumerate(evicted):
            if m.get("role") == "tool":
                # its assistant (with tool_calls) must be evicted BEFORE it
                self.assertTrue(any(e.get("tool_calls") for e in evicted[:idx]),
                                "tool result evicted without its assistant")
        # invariant 2: in kept, no orphan tool result at the front
        if kept and kept[0].get("role") == "tool":
            self.fail("kept history starts with an orphan tool result")
        # invariant 3: every kept tool result is preceded by its assistant
        for idx, m in enumerate(kept):
            if m.get("role") == "tool":
                prev = kept[idx - 1] if idx else None
                self.assertTrue(prev and prev.get("tool_calls"),
                                "kept tool result lost its assistant")

    # --- eviction handoff: nothing silently dropped -----------------------
    def test_evicted_exchanges_reach_the_summary_fold(self):
        try:
            session_memory._get_client().models.list()
        except Exception as e:
            self.skipTest(f"summary fold needs a live OpenAI-compatible LLM "
                          f"(SAGENT_QWEN_MODEL): {e}")
        hist = []
        for i in range(5):
            hist.extend(wake_turn(i))
        kept, evicted = self.agent._trim_history_with_evicted(
            hist, target_chars=2500)
        self.assertTrue(evicted)
        hist_path = bridge._history_filepath(self.config, "system-wake")
        session_memory.maybe_update_summary(hist_path, kept,
                                            evicted_messages=evicted)
        summary_path = session_memory.summary_filepath(hist_path)
        with open(summary_path) as f:
            data = json.load(f)
        self.assertGreater(data.get("n_summarized", 0), 0)
        # the fold is LLM-summarized: assert it carried the evicted material
        # forward (summary written, or parked pending a reachable model) —
        # not the exact wording, which is sampling-dependent
        self.assertTrue(data.get("summary") or data.get("pending_evicted"),
                        json.dumps(data)[:300])

    # --- consecutive wakes chain: the end-to-end consumer semantics -------
    def test_consecutive_wakes_see_previous_exchanges(self):
        # wake 1: fresh history + prompt -> (simulated turn) -> save chain
        hist1 = list(wake_turn(1))
        bridge._save_history_to_disk(self.config, "system-wake", hist1)
        # wake 2: loads disk, appends its prompt -> the previous exchange is
        # IN the working window
        loaded = bridge._load_history_from_disk(self.config, "system-wake")
        working = list(loaded) + [{"role": "user", "content": "[wake 2] packet"}]
        texts = [m.get("content") or "" for m in working]
        self.assertTrue(any("her reply: I sat with the silence" in t for t in texts),
                        "wake 2 cannot see wake 1's reply")
        # wake 2's own chain appends and persists
        hist2 = working + wake_turn(2, tool=False)[1:]
        bridge._save_history_to_disk(self.config, "system-wake", hist2)
        again = bridge._load_history_from_disk(self.config, "system-wake")
        self.assertEqual(len(again), len(hist2))

    # --- oversized wake history trims, never grows unbounded --------------
    def test_wake_history_trims_to_window(self):
        hist = []
        for i in range(6):
            hist.extend(wake_turn(i))
        kept, evicted = self.agent._trim_history_with_evicted(
            hist, target_chars=4000)
        total = sum(len(m.get("content") or "") for m in kept)
        self.assertLessEqual(total, 4000 + 1)  # +1: boundary tolerance
        self.assertTrue(evicted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
