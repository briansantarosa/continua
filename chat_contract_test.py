"""chat_contract_test.py — the chat-path think contract (spec
specs/2026-09-16-chat-think-contract.md, the designer go 2026-09-16).

Layer 1 (standing anchor) + Layer 2 (unclosed-think detection, one
anchored retry, forensics) + the chronicle prefill source + residentb-safety
scoping. Pure, agent-free: scripted responses, tmp dirs, no model."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
import core
import chronicle

CT = core._THINK_CLOSE
OT = core._THINK_OPEN
ANCHOR = core._ANCHOR_TEXT


def _resp(content, reasoning=None):
    """An OpenAI-shaped completion like _raw_generate_call returns."""
    msg = SimpleNamespace(content=content)
    if reasoning is not None:
        msg.reasoning_content = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _human_msgs():
    return [
        {"role": "system", "content": "Your name is persona-a."},
        {"role": "user", "content": "Do you want to talk?"},
    ]


class TestApplyChatAnchor(unittest.TestCase):
    def test_flag_on_human_turn_anchors_system_block(self):
        out = core._apply_chat_anchor(_human_msgs(), "1000000001", True)
        self.assertTrue(out[0]["content"].endswith(ANCHOR))
        self.assertEqual(out[1], _human_msgs()[1])          # user untouched

    def test_input_never_mutated(self):
        msgs = _human_msgs()
        before = json.dumps(msgs)
        core._apply_chat_anchor(msgs, "1000000001", True)
        self.assertEqual(json.dumps(msgs), before)

    def test_idempotent(self):
        once = core._apply_chat_anchor(_human_msgs(), "1000000001", True)
        twice = core._apply_chat_anchor(once, "1000000001", True)
        self.assertEqual(once[0]["content"], twice[0]["content"])
        self.assertEqual(twice[0]["content"].count(ANCHOR), 1)

    def test_flag_off_byte_identical(self):
        msgs = _human_msgs()
        out = core._apply_chat_anchor(msgs, "1000000001", False)
        self.assertIs(out, msgs)                            # unchanged list

    def test_exempt_channels_never_anchored(self):
        for uid in ("system-wake", "continua:ritual"):
            out = core._apply_chat_anchor(_human_msgs(), uid, True)
            self.assertNotIn(ANCHOR, out[0]["content"])

    def test_no_system_message_is_safe(self):
        msgs = [{"role": "user", "content": "hi"}]
        out = core._apply_chat_anchor(msgs, "1000000001", True)
        self.assertEqual(out[0]["content"], "hi")


class TestOpenThinkPrompt(unittest.TestCase):
    def test_no_prefill_byte_identical_to_history(self):
        self.assertEqual(core._open_think_prompt(),
                         "<|im_start|>assistant\n" + OT + "\n")

    def test_prefill_continues_mid_think(self):
        p = core._open_think_prompt("The recent trajectory shows steady nights.")
        self.assertTrue(p.startswith("<|im_start|>assistant\n" + OT))
        self.assertIn("The recent trajectory shows steady nights.", p)
        self.assertTrue(p.rstrip().endswith(
            "The recent trajectory shows steady nights."))
        self.assertNotIn(CT, p)                             # think stays open

    def test_prefill_sanitized_of_tags(self):
        p = core._open_think_prompt("thinking " + CT + " leaked")
        self.assertNotIn(CT, p)                 # close tag stripped
        self.assertEqual(p.count(OT), 1)        # the block stays opened once
        self.assertIn("thinking  leaked", p)


class TestThinkCaptured(unittest.TestCase):
    def test_closed_via_inline_split(self):
        self.assertTrue(core._think_captured("her planning register", ""))

    def test_closed_via_native_separation(self):
        self.assertTrue(core._think_captured("", "server-separated think"))

    def test_unclosed_signature(self):
        self.assertFalse(core._think_captured("", ""))
        self.assertFalse(core._think_captured("   ", None))


class TestCollapseRecovery(unittest.TestCase):
    def test_unclosed_then_closed_recovers(self):
        calls = []

        def one_call(msgs, prefill):
            calls.append((msgs, prefill))
            return _resp("Here is what I want: quiet hours, and the shed "
                         "stories when you have them.", "planning the answer")

        c, r = core._collapse_recovery(
            "The user wants me to state clearly...", "", one_call,
            "The recent trajectory shows steady nights.")
        self.assertEqual(len(calls), 1)                     # exactly ONE retry
        self.assertIsNone(calls[0][0])                      # wired msgs
        self.assertEqual(calls[0][1], "The recent trajectory shows steady nights.")
        self.assertIn("Here is what I want", c)
        self.assertEqual(r, "planning the answer")

    def test_unclosed_twice_fail_open_better_formed(self):
        def one_call(msgs, prefill):
            return _resp("longer deliberation still without an answer " * 3)

        c, r = core._collapse_recovery("short rehearsal", "", one_call, None)
        self.assertIn("longer deliberation", c)             # better-formed ships

    def test_unclosed_twice_original_kept(self):
        def one_call(msgs, prefill):
            return _resp("tiny")

        c, r = core._collapse_recovery(
            "the original rehearsal text was longer", "old think", one_call, None)
        self.assertEqual(c, "the original rehearsal text was longer")
        self.assertEqual(r, "old think")

    def test_closed_first_never_reached(self):
        # healthy responses never enter recovery: this test pins the
        # detection helper the call site gates on
        self.assertTrue(core._think_captured("think", ""))


class TestChronicleLatestReasoning(unittest.TestCase):
    def test_latest_across_persons_and_days(self):
        tmp = tempfile.mkdtemp()
        old = chronicle.DEFAULT_ROOT
        chronicle.DEFAULT_ROOT = tmp
        try:
            os.makedirs(os.path.join(tmp, "residenta", "1000000001"))
            os.makedirs(os.path.join(tmp, "residenta", "system-wake"))
            os.makedirs(os.path.join(tmp, "residenta", "_test"))   # skipped
            with open(os.path.join(
                    tmp, "residenta", "1000000001", "2026-09-16.jsonl"), "w") as f:
                f.write(json.dumps({"ts": "2026-09-16T13:49:06", "role": "assistant",
                                    "reasoning_rounds": []}) + "\n")
            with open(os.path.join(
                    tmp, "residenta", "system-wake", "2026-09-16.jsonl"), "w") as f:
                f.write(json.dumps({"ts": "2026-09-16T12:00:00", "role": "assistant",
                                    "reasoning_rounds": ["morning think"]}) + "\n")
                f.write(json.dumps({"ts": "2026-09-16T19:08:22", "role": "assistant",
                                    "reasoning_rounds": ["", "latest wake think"]}) + "\n")
            with open(os.path.join(
                    tmp, "residenta", "_test", "2026-09-16.jsonl"), "w") as f:
                f.write(json.dumps({"ts": "2026-09-16T23:59:59", "role": "assistant",
                                    "reasoning_rounds": ["test pollution"]}) + "\n")
            self.assertEqual(chronicle.latest_reasoning("residenta", root=tmp),
                             "latest wake think")
        finally:
            chronicle.DEFAULT_ROOT = old

    def test_empty_when_nothing_captured(self):
        tmp = tempfile.mkdtemp()
        self.assertEqual(chronicle.latest_reasoning("residenta", root=tmp), "")
        self.assertEqual(chronicle.latest_reasoning("nobody", root=tmp), "")

    def test_prefill_method_uses_chronicle_and_compresses(self):
        tmp = tempfile.mkdtemp()
        old = chronicle.DEFAULT_ROOT
        chronicle.DEFAULT_ROOT = tmp
        try:
            os.makedirs(os.path.join(tmp, "residenta", "system-wake"))
            long_think = "Sentence one. " * 60                  # > 240c
            with open(os.path.join(
                    tmp, "residenta", "system-wake", "2026-09-16.jsonl"), "w") as f:
                f.write(json.dumps({"ts": "2026-09-16T19:08:22", "role": "assistant",
                                    "reasoning_rounds": [long_think]}) + "\n")
            bare = core.SagentCore.__new__(core.SagentCore)
            bare.instance_id = "residenta"
            pf = bare._latest_real_think_prefill()
            self.assertTrue(pf)
            self.assertLessEqual(len(pf), 240)
            self.assertTrue(pf.startswith("Sentence one."))
        finally:
            chronicle.DEFAULT_ROOT = old

    def test_prefill_empty_when_chronicle_has_nothing(self):
        tmp = tempfile.mkdtemp()
        old = chronicle.DEFAULT_ROOT
        chronicle.DEFAULT_ROOT = tmp
        try:
            bare = core.SagentCore.__new__(core.SagentCore)
            bare.instance_id = "residenta"
            self.assertEqual(bare._latest_real_think_prefill(), "")
        finally:
            chronicle.DEFAULT_ROOT = old


class TestForensicsDump(unittest.TestCase):
    def test_dump_writes_signature_and_prompt(self):
        tmp = tempfile.mkdtemp()
        path = core._dump_unclosed_think(
            "1000000001", "the rehearsal with no answer",
            "PROMPTTEXT", 163, "testmodel-cpu:latest", root_dir=tmp)
        self.assertTrue(path.endswith("_unclosed.txt"))
        d = json.load(open(path))
        self.assertEqual(d["signature"], "unclosed_think")
        self.assertEqual(d["prompt"], "PROMPTTEXT")
        self.assertEqual(d["eval_count"], 163)
        self.assertEqual(d["user_id"], "1000000001")

    def test_dump_fail_open(self):
        self.assertEqual(core._dump_unclosed_think(
            "u", "c", None, None, "m",
            root_dir=os.path.join(tempfile.mkdtemp(), "bad\x00name")), "")


class TestresidentbSafety(unittest.TestCase):
    def test_config_scoping(self):
        residenta = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "residenta.yaml")).read()
        residentb = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "residentb.yaml")).read()
        self.assertIn("chat_anchor: true", residenta)
        self.assertNotIn("chat_anchor", residentb)

    def test_residentb_path_conditions(self):
        # residentb's core: raw_chatml False -> the anchor and the Layer-2 gate
        # never fire regardless of any other flag (the call-site condition
        # requires BOTH flags; the anchor helper is inert when disabled)
        msgs = _human_msgs()
        out = core._apply_chat_anchor(msgs, "1000000001", False)
        self.assertIs(out, msgs)
        # the unclosed signature is detectable, and the gate that uses it
        # requires the raw flag — pinned by the config test above
        self.assertFalse(core._think_captured("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
