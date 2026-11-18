"""think_window_test.py — memv13 (2026-10-07, the designer go): the bounded think
window (§0-§6) + the YAML chat-render contract (§7).

Agent-free: pure functions + bare-core attrs + source-presence guards —
no model, no store, no chronicle, no bridge. Run:
  python3 think_window_test.py
(or pytest think_window_test.py)
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chatrender
import core

OT = core._THINK_OPEN
CT = core._THINK_CLOSE

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bare(**attrs):
    b = core.SagentCore.__new__(core.SagentCore)
    b.instance_id = "test"
    b._history_thinks = "empty"
    b._strip_ts_prefix = False
    b._sanitize_history = False
    b._last_reasoning = ""
    b._history_think_turns = None
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


def _u(text):
    return {"role": "user", "content": text}


def _a(text, think=None, details=None):
    m = {"role": "assistant", "content": text}
    if think is not None:
        m["_think"] = think
    if details is not None:
        m["_think_details"] = details
    return m


# ---------------------------------------------------------------------------
# chatrender — the §7 config contract
# ---------------------------------------------------------------------------

class TestChatRenderConfig(unittest.TestCase):
    def test_defaults(self):
        labels, turns, field = chatrender.validate_chat_config({})
        self.assertEqual(labels, {"system": "system", "user": "user",
                                  "assistant": "assistant"})
        self.assertIsNone(turns)
        self.assertIsNone(field)

    def test_custom_labels_raw(self):
        cfg = {"raw_chatml": True,
               "chat_role_labels": {"user": "my friend", "assistant": "persona-a"}}
        labels, turns, field = chatrender.validate_chat_config(cfg)
        self.assertEqual(labels["user"], "my friend")
        self.assertEqual(labels["assistant"], "persona-a")
        self.assertEqual(labels["system"], "system")
        self.assertIsNone(turns)
        self.assertIsNone(field)

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            chatrender.resolve_role_labels(
                {"chat_role_labels": {"tool": "wrench"}})

    def test_non_mapping_rejected(self):
        with self.assertRaises(ValueError):
            chatrender.resolve_role_labels({"chat_role_labels": "persona-a"})

    def test_label_value_rules(self):
        close_tag_label = core._THINK_CLOSE + "my friend"
        for bad in ("", "   ", "a\nb", "a\tb", "<|foo", "im_end",
                    close_tag_label):
            with self.assertRaises(ValueError, msg=repr(bad)):
                chatrender.resolve_role_labels(
                    {"chat_role_labels": {"user": bad}})

    def test_v1_custom_labels_rejected(self):
        with self.assertRaises(ValueError):
            chatrender.validate_chat_config(
                {"raw_chatml": False,
                 "chat_role_labels": {"user": "my friend"}})

    def test_window_type_rules(self):
        for bad in (-1, True, False, "5", 1.5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                chatrender.validate_chat_config(
                    {"raw_chatml": True, "history_think_turns": bad})

    def test_v1_window_needs_render_field(self):
        with self.assertRaises(ValueError):
            chatrender.validate_chat_config(
                {"raw_chatml": False, "history_think_turns": 5})

    def test_v1_window_with_field_ok(self):
        for field in ("reasoning_content", "reasoning"):
            labels, turns, got = chatrender.validate_chat_config(
                {"raw_chatml": False, "history_think_turns": 5,
                 "history_think_render": field})
            self.assertEqual(turns, 5)
            self.assertEqual(got, field)

    def test_v1_window_bad_field(self):
        with self.assertRaises(ValueError):
            chatrender.validate_chat_config(
                {"raw_chatml": False, "history_think_turns": 5,
                 "history_think_render": "thinking"})

    def test_raw_window_rejects_render_field(self):
        with self.assertRaises(ValueError):
            chatrender.validate_chat_config(
                {"raw_chatml": True, "history_think_turns": 5,
                 "history_think_render": "reasoning_content"})

    def test_render_field_requires_window(self):
        for turns in (None, 0):
            with self.assertRaises(ValueError):
                chatrender.validate_chat_config(
                    {"raw_chatml": False, "history_think_turns": turns,
                     "history_think_render": "reasoning_content"})

    def test_explicit_zero_ok_and_inert(self):
        labels, turns, field = chatrender.validate_chat_config(
            {"raw_chatml": False, "history_think_turns": 0})
        self.assertEqual(turns, 0)
        self.assertIsNone(field)

    def test_raw_window_ok_without_field(self):
        labels, turns, field = chatrender.validate_chat_config(
            {"raw_chatml": True, "history_think_turns": 4})
        self.assertEqual(turns, 4)
        self.assertIsNone(field)


# ---------------------------------------------------------------------------
# §3.2 — the expiry rule
# ---------------------------------------------------------------------------

def _thread(n_completed, think_on_all=True, pending_user=True,
            think="PLAN"):
    """E1..En completed ([user, assistant(_think)]) + optional pending user."""
    h = []
    for i in range(1, n_completed + 1):
        h.append(_u(f"u{i}"))
        h.append(_a(f"a{i}", think=think if think_on_all else None))
    if pending_user:
        h.append(_u(f"u{n_completed + 1}-pending"))
    return h


def _think_of(h, i):
    """The _think on exchange i's assistant entry (1-based), or None."""
    for m in h:
        if m.get("role") == "assistant" and m.get("content") == f"a{i}":
            return m.get("_think")
    return None


class TestExpiryRule(unittest.TestCase):
    def test_plan_example_n5(self):
        # memv13 §2's example: E1's think rides prompts for exchanges 2-6
        # and is removed once exchange 6 has completed (E7 pending here).
        h = _thread(6)
        core._expire_history_thinks(h, 5)
        self.assertIsNone(_think_of(h, 1))
        for i in range(2, 7):
            self.assertEqual(_think_of(h, i), "PLAN")

    def test_n1_boundary(self):
        h = _thread(3)
        core._expire_history_thinks(h, 1)
        self.assertIsNone(_think_of(h, 1))
        self.assertIsNone(_think_of(h, 2))
        self.assertEqual(_think_of(h, 3), "PLAN")

    def test_window_one_keeps_only_last_completed(self):
        h = _thread(6)
        core._expire_history_thinks(h, 1)
        for i in range(1, 6):
            self.assertIsNone(_think_of(h, i))
        self.assertEqual(_think_of(h, 6), "PLAN")

    def test_zero_strips_all(self):
        h = _thread(4)
        core._expire_history_thinks(h, 0)
        for m in h:
            self.assertNotIn("_think", m)
        # answers untouched
        self.assertEqual([m["content"] for m in h if m["role"] == "assistant"],
                         ["a1", "a2", "a3", "a4"])

    def test_none_is_noop(self):
        h = _thread(2)
        out = core._expire_history_thinks(h, None)
        self.assertIs(out, h)
        self.assertEqual(_think_of(h, 1), "PLAN")

    def test_in_place_mutation_persists(self):
        # the SAME dicts the bridge persists must lose the think
        h = _thread(3)
        a1 = next(m for m in h if m.get("content") == "a1")
        out = core._expire_history_thinks(h, 1)
        self.assertIs(out, h)
        self.assertIs(h[h.index(a1)] if a1 in h else a1, a1)
        self.assertNotIn("_think", a1)

    def test_no_think_exchange_still_consumes_a_turn(self):
        h = _thread(4, think_on_all=False)
        # give E1/E3/E4 thinks manually
        for m in h:
            if m.get("role") == "assistant" and m.get("content") in ("a1", "a3", "a4"):
                m["_think"] = "PLAN"
        core._expire_history_thinks(h, 2)
        self.assertIsNone(_think_of(h, 1))          # 3 completed after E1
        self.assertEqual(_think_of(h, 3), "PLAN")   # 1 after E3
        self.assertEqual(_think_of(h, 4), "PLAN")   # 0 after E4

    def test_tool_rounds_expire_with_their_exchange(self):
        # plan §2's boundary: E1 expires once SIX exchanges have completed
        # (its think rode prompts for E2..E6; E7 is pending here)
        h = [ _u("u1"), _a("a1-call", think="CALL"), 
              {"role": "tool", "content": "r1"},
              _a("a1-final", think="FINAL"),
              _u("u2"), _a("a2", think="KEEP"),
              _u("u3"), _a("a3", think="KEEP"),
              _u("u4"), _a("a4", think="KEEP"),
              _u("u5"), _a("a5", think="KEEP"),
              _u("u6"), _a("a6", think="KEEP"),
              _u("u7-pending")]
        core._expire_history_thinks(h, 5)   # 6 completed; E1 is the oldest
        a1c = next(m for m in h if m.get("content") == "a1-call")
        a1f = next(m for m in h if m.get("content") == "a1-final")
        self.assertNotIn("_think", a1c)
        self.assertNotIn("_think", a1f)
        self.assertIn("_think", next(m for m in h if m.get("content") == "a6"))
        # tool provenance untouched
        self.assertEqual(h[2]["role"], "tool")
        self.assertEqual(h[2]["content"], "r1")

    def test_leading_assistant_group_counts(self):
        h = [_a("pre", think="OLD"), _u("u1"), _a("a1", think="NEW")]
        core._expire_history_thinks(h, 1)
        self.assertNotIn("_think", h[0])
        self.assertEqual(h[2]["_think"], "NEW")

    def test_details_expire_on_the_same_exchange_boundary(self):
        h = _thread(6)
        for m in h:
            if m.get("role") == "assistant":
                m["_think_details"] = [{"type": "reasoning.encrypted",
                                        "data": "opaque"}]
        core._expire_history_thinks(h, 5)
        a1 = next(m for m in h if m.get("content") == "a1")
        a2 = next(m for m in h if m.get("content") == "a2")
        self.assertNotIn("_think_details", a1)
        self.assertIn("_think_details", a2)
        core._expire_history_thinks(h, 0)
        self.assertFalse(any("_think_details" in m for m in h))

    def test_empty_history(self):
        self.assertEqual(core._expire_history_thinks([], 5), [])


# ---------------------------------------------------------------------------
# §3.3 — the /v1 adapter
# ---------------------------------------------------------------------------

class TestV1ThinkAdapter(unittest.TestCase):
    def test_strips_private_key_everywhere(self):
        msgs = [_u("u", ), {"role": "system", "content": "s", "_think": "x"},
                _a("a", think="t")]
        out = core._apply_history_thinks_v1(msgs, None)
        for m in out:
            self.assertNotIn("_think", m)
        self.assertEqual(out[1]["content"], "s")

    def test_no_field_is_pure_strip(self):
        msgs = [_a("a", think="t")]
        out = core._apply_history_thinks_v1(msgs, None)
        self.assertNotIn("_think", out[0])
        self.assertNotIn("reasoning_content", out[0])

    def test_maps_to_field_assistant_only(self):
        msgs = [_a("a", think="  t  "),
                {"role": "system", "content": "s", "_think": "x"}]
        out = core._apply_history_thinks_v1(msgs, "reasoning_content")
        self.assertEqual(out[0]["reasoning_content"], "t")
        self.assertNotIn("_think", out[0])
        self.assertNotIn("reasoning_content", out[1])

    def test_empty_think_not_attached(self):
        msgs = [_a("a", think="   ")]
        out = core._apply_history_thinks_v1(msgs, "reasoning")
        self.assertNotIn("reasoning", out[0])
        self.assertNotIn("_think", out[0])

    def test_openrouter_details_are_echoed_and_private_key_removed(self):
        details = [{"type": "reasoning.encrypted", "data": "opaque"}]
        msgs = [_a("a", think="plain", details=details)]
        out = core._apply_history_thinks_v1(msgs, "reasoning")
        self.assertEqual(out[0]["reasoning"], "plain")
        self.assertEqual(out[0]["reasoning_details"], details)
        self.assertNotIn("_think", out[0])
        self.assertNotIn("_think_details", out[0])

    def test_details_without_plaintext_are_still_echoed(self):
        details = [{"type": "reasoning.summary", "summary": [
            {"type": "summary_text", "text": "continuity"}]}]
        out = core._apply_history_thinks_v1(
            [_a("a", details=details)], "reasoning")
        self.assertEqual(out[0]["reasoning_details"], details)
        self.assertNotIn("reasoning", out[0])

    def test_passthrough_identity(self):
        m = _u("u")
        out = core._apply_history_thinks_v1([m], "reasoning_content")
        self.assertIs(out[0], m)


# ---------------------------------------------------------------------------
# §3.1 — capture (full think; precedence; channels; hygiene)
# ---------------------------------------------------------------------------

class TestHistEntry(unittest.TestCase):
    def test_full_think_attached(self):
        b = _bare(_history_think_turns=5)
        b._last_reasoning = "X" * 600
        e = b._assistant_hist_entry("answer")
        self.assertEqual(e["_think"], "X" * 600)
        self.assertEqual(e["content"], "answer")

    def test_no_fabrication(self):
        b = _bare(_history_think_turns=5)
        b._last_reasoning = ""
        e = b._assistant_hist_entry("answer")
        self.assertNotIn("_think", e)

    def test_wake_channel_attaches_now(self):
        # house ruling 2026-10-07 evening: the window is hers EVERYWHERE —
        # the old human-only exemption is gone
        b = _bare(_history_think_turns=5)
        b._last_reasoning = "wake think"
        e = b._assistant_hist_entry("answer")
        self.assertEqual(e["_think"], "wake think")

    def test_provider_reasoning_details_capture_with_window(self):
        details = [{"type": "reasoning.summary", "summary": [
            {"type": "summary_text", "text": "short summary"}]},
            {"type": "reasoning.encrypted", "data": "opaque"}]
        b = _bare(_history_think_turns=5)
        b._last_reasoning = "plaintext reasoning"
        b._last_reasoning_details = details
        e = b._assistant_hist_entry("answer")
        self.assertEqual(e["_think_details"], details)

    def test_provider_reasoning_details_jsonifies_sdk_objects(self):
        class Detail:
            def model_dump(self, **kwargs):
                return {"type": "reasoning.encrypted", "data": "opaque"}
        self.assertEqual(core._reasoning_details_plain([Detail()]),
                         [{"type": "reasoning.encrypted", "data": "opaque"}])

    def test_native_tool_assistant_entry_can_keep_details(self):
        details = [{"type": "reasoning.encrypted", "data": "opaque"}]
        b = _bare(_history_think_turns=5)
        b._last_reasoning = "tool-call thought"
        b._last_reasoning_details = details
        entry = b._assistant_hist_entry("")
        entry["tool_calls"] = [{"id": "call_1"}]
        self.assertEqual(entry["_think_details"], details)
        self.assertEqual(entry["tool_calls"][0]["id"], "call_1")

    def test_zero_no_attach(self):
        b = _bare(_history_think_turns=0)
        b._last_reasoning = "think"
        e = b._assistant_hist_entry("answer")
        self.assertNotIn("_think", e)

    def test_legacy_real_compressed(self):
        b = _bare(_history_thinks="real")
        b._last_reasoning = "Sentence one. " * 60
        e = b._assistant_hist_entry("answer")
        self.assertIn("_think", e)
        self.assertLessEqual(len(e["_think"]), 240)

    def test_explicit_zero_overrides_legacy(self):
        b = _bare(_history_thinks="real", _history_think_turns=0)
        b._last_reasoning = "think"
        e = b._assistant_hist_entry("answer")
        self.assertNotIn("_think", e)

    def test_explicit_window_overrides_legacy_full_not_compressed(self):
        b = _bare(_history_thinks="real", _history_think_turns=5)
        b._last_reasoning = "Y" * 600
        e = b._assistant_hist_entry("answer")
        self.assertEqual(e["_think"], "Y" * 600)

    def test_hygiene_still_applies(self):
        b = _bare(_history_think_turns=5, _strip_ts_prefix=True,
                  _sanitize_history=True)
        b._last_reasoning = "think"
        e = b._assistant_hist_entry("[2026-10-07 10:00] hello <p>there</p>")
        self.assertEqual(e["content"], "hello there")


# ---------------------------------------------------------------------------
# §7 — the raw composer + generation opener
# ---------------------------------------------------------------------------

COMPOSE_MSGS = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "U"},
    _a("A", think="T"),
    {"role": "tool", "content": "R"},
]
LEGACY_CANONICAL = (
    "<|im_start|>system\nSYS<|im_end|>\n"
    "<|im_start|>user\nU<|im_end|>\n"
    "<|im_start|>assistant\n" + OT + "\nT\n" + CT + "\n\nA<|im_end|>\n"
    "<|im_start|>user\n[Tool result] R<|im_end|>\n")


class TestRawComposerLabels(unittest.TestCase):
    def test_default_byte_identical(self):
        self.assertEqual(core._raw_chatml_render(COMPOSE_MSGS),
                         LEGACY_CANONICAL)
        self.assertEqual(
            core._raw_chatml_render(
                COMPOSE_MSGS, role_labels=chatrender.canonical_labels()),
            LEGACY_CANONICAL)

    def test_custom_labels_render(self):
        labels = {"system": "meta", "user": "my friend", "assistant": "persona-a"}
        out = core._raw_chatml_render(COMPOSE_MSGS, role_labels=labels)
        expected = (
            "<|im_start|>meta\nSYS<|im_end|>\n"
            "<|im_start|>my friend\nU<|im_end|>\n"
            "<|im_start|>persona-a\n" + OT + "\nT\n" + CT + "\n\nA<|im_end|>\n"
            "<|im_start|>my friend\n[Tool result] R<|im_end|>\n")
        self.assertEqual(out, expected)

    def test_empty_marker_with_labels(self):
        labels = {"assistant": "persona-a"}
        out = core._raw_chatml_render(
            [{"role": "assistant", "content": ""}],
            render_empty_markers=True, role_labels=labels)
        self.assertEqual(
            out,
            "<|im_start|>persona-a\n" + OT + "\n\n" + CT + "\n\n"
            + core._EMPTY_TURN_MARKER + "<|im_end|>\n")

    def test_sanitize_with_labels(self):
        labels = {"user": "my friend", "assistant": "persona-a"}
        out = core._raw_chatml_render(
            [{"role": "user", "content": "U"},
             _a("a<b>x</b>", think="t<i>y</i>")],
            sanitize=True, role_labels=labels)
        self.assertIn("<|im_start|>my friend\nU<|im_end|>\n", out)
        self.assertIn("<|im_start|>persona-a\n" + OT + "\nty\n" + CT, out)

    def test_open_think_prompt_default(self):
        self.assertEqual(core._open_think_prompt(),
                         "<|im_start|>assistant\n" + OT + "\n")

    def test_open_think_prompt_custom_label(self):
        self.assertEqual(core._open_think_prompt(None, "persona-a"),
                         "<|im_start|>persona-a\n" + OT + "\n")
        p = core._open_think_prompt("the planning register", "persona-a")
        # historical shape: the prefill continues immediately after the
        # open glyph (chat_contract_test pins it) — label swap only
        self.assertEqual(p,
                         "<|im_start|>persona-a\n" + OT + "the planning register\n")
        self.assertNotIn(CT, p)


# ---------------------------------------------------------------------------
# §3.4 — the window accounting (thinks are not free)
# ---------------------------------------------------------------------------

class TestTrimThinkAccounting(unittest.TestCase):
    def _hist(self):
        return [
            _u("u" + "x" * 45), _a("a" + "x" * 45, think="T" * 800),
            _u("u" + "x" * 45), _a("a" + "x" * 45),
            _u("u" + "x" * 45), _a("a" + "x" * 45),
        ]

    def test_window_counts_thinks(self):
        b = _bare(_history_think_turns=5, max_history_chars=1200)
        kept, evicted = b._trim_history_with_evicted(self._hist())
        # 1100 chars counted (300 content + 800 think) > 840 effective:
        # pops to the 4-message floor; the think-bearing a1 goes with it.
        self.assertEqual(len(kept), 4)
        self.assertEqual(len(evicted), 2)
        self.assertEqual(evicted[1].get("content", "")[0], "a")
        self.assertEqual(evicted[1].get("_think"), "T" * 800)

    def test_window_counts_openrouter_details(self):
        b = _bare(_history_think_turns=5, max_history_chars=1200)
        hist = self._hist()
        hist[1]["_think"] = ""
        hist[1]["_think_details"] = [{"type": "reasoning.encrypted",
                                      "data": "X" * 1100}]
        kept, evicted = b._trim_history_with_evicted(hist, target_chars=1200)
        # Without the structured detail bytes, all six short messages fit
        # the 40% floor; counting them evicts the oldest complete exchange.
        self.assertEqual(len(kept), 4)
        self.assertEqual(len(evicted), 2)
        self.assertIn("_think_details", evicted[1])

    def test_legacy_ignores_thinks(self):
        b = _bare(_history_think_turns=None, max_history_chars=1200)
        kept, evicted = b._trim_history_with_evicted(self._hist())
        # 300 content chars — everything fits, legacy byte-behavior
        self.assertEqual(len(kept), 6)
        self.assertEqual(evicted, [])


# ---------------------------------------------------------------------------
# wiring presence (the llm_debug source-guard pattern)
# ---------------------------------------------------------------------------

class TestWiringPresence(unittest.TestCase):
    def test_core_wiring(self):
        src = io.open(os.path.join(_HERE, "core.py"), encoding="utf-8").read()
        self.assertIn("chat_history = _expire_history_thinks(", src)
        # house ruling 2026-10-07 evening: NO channel gate — the window is
        # hers everywhere (wakes + ritual sitting included)
        self.assertNotIn("and str(user_id) not in _RAW_ANCHOR_EXEMPT)",
                         src[src.index("_expire_history_thinks("):
                             src.index("_expire_history_thinks(") + 400])
        self.assertIn("_facing = _apply_history_thinks_v1(_facing, "
                      "_think_field)", src)
        self.assertIn('role_labels=getattr(self, "_chat_role_labels", None)',
                      src)
        self.assertIn('from chatrender import validate_chat_config', src)
        # Host-specific request controls: residentb's vLLM gate is config-driven;
        # providers such as OpenRouter use reasoning/include_reasoning instead.
        self.assertIn('self._history_think_template_kwargs = dict(_htk or {})', src)
        self.assertIn('_extra_body["chat_template_kwargs"] = _template_kwargs', src)
        self.assertIn('_extra_body["reasoning"]', src)
        self.assertIn('_extra_body["include_reasoning"] = True', src)
        self.assertIn('_extra_body["reasoning"] = _reasoning_options', src)
        self.assertIn('"reasoning_details"', src)
        self.assertIn('"_think_details"', src)
        self.assertIn('_assistant_call_entry = self._assistant_hist_entry(', src)

    def test_ritual_wiring(self):
        src = io.open(os.path.join(_HERE, "ritual.py"),
                      encoding="utf-8").read()
        self.assertIn("def _render_chatml(messages: list, role_labels",
                      src)
        self.assertIn('"prompt": _render_chatml(messages, role_labels),', src)
        self.assertIn('"role_labels": _rl,', src)

    def test_no_think_key_leaks_to_v1_send(self):
        # the /v1 send path must map `_think` away BEFORE the request
        src = io.open(os.path.join(_HERE, "core.py"), encoding="utf-8").read()
        i = src.index("_facing = _llm_facing_messages(_use)")
        j = src.index("chat.completions.create", i)
        seg = src[i:j]
        self.assertIn("_apply_history_thinks_v1", seg)
        self.assertNotIn('"_think"', seg)


if __name__ == "__main__":
    unittest.main(verbosity=1)
