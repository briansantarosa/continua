"""tool_boundary_test.py — the five-boundary contract regression tests
(fixtoolcalls.md P0a, 2026-11-10).

Boundaries under test (fixtoolcalls.md §2.2):
  3. STORAGE  — _flatten_for_disk never strips tool contract fields
  4. REBUILD  — _render_history_message carries tool_calls / tool_call_id /
                name through EVERY return path (both ts modes)
  SEND        — _llm_facing_messages: ALL-OR-NOTHING run decision, dangling
                array repair, orphan conversion (P0a-6 / P0a-8)
  5. EVICTION — _trim_history_with_evicted never splits a tool round; a
                pre-split round in storage is repaired at the send boundary
  PARSE       — FM2 function:-prefix strip (P0a-2) + writes-solo gate (P0a-7)

Agent-free: no network, no model, no production data.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core
import bridge


def _native_assistant(content="", ids=("call_A", "call_B"), with_ts=True):
    entry = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": i, "type": "function",
             "function": {"name": "search_my_memories",
                          "arguments": json.dumps({"query": "x"})}}
            for i in ids
        ],
    }
    if with_ts:
        entry["ts"] = "2026-11-10T09:00:00"
    return entry


def _tool_result(call_id, content="result", with_ts=True):
    entry = {"role": "tool", "tool_call_id": call_id, "name": "search_my_memories",
             "content": content}
    if with_ts:
        entry["ts"] = "2026-11-10T09:00:01"
    return entry


class TestRebuildPreservesToolFields(unittest.TestCase):
    """Boundary 4 — core._render_history_message (P0a-3)."""

    def test_assistant_with_ts_keeps_tool_calls(self):
        msg = _native_assistant(with_ts=True)
        out = core.SagentCore._render_history_message(msg)
        self.assertIn("tool_calls", out)
        self.assertEqual(len(out["tool_calls"]), 2)
        self.assertTrue(out["content"].startswith("["))  # stamp still applied

    def test_assistant_without_ts_keeps_tool_calls(self):
        # the no-ts early return used to strip the array (the FM5 path)
        msg = _native_assistant(with_ts=False)
        out = core.SagentCore._render_history_message(msg)
        self.assertIn("tool_calls", out)
        self.assertEqual(out["tool_calls"], msg["tool_calls"])

    def test_tool_result_keeps_id_and_name(self):
        msg = _tool_result("call_A", with_ts=True)
        out = core.SagentCore._render_history_message(msg)
        self.assertEqual(out.get("tool_call_id"), "call_A")
        self.assertEqual(out.get("name"), "search_my_memories")
        self.assertEqual(out.get("role"), "tool")

    def test_tool_result_without_ts_keeps_fields(self):
        msg = _tool_result("call_A", with_ts=False)
        out = core.SagentCore._render_history_message(msg)
        self.assertEqual(out.get("tool_call_id"), "call_A")
        self.assertEqual(out.get("name"), "search_my_memories")

    def test_multimodal_assistant_keeps_tool_calls(self):
        msg = _native_assistant(with_ts=True)
        msg["content"] = [{"type": "text", "text": "look"}]
        out = core.SagentCore._render_history_message(msg)
        self.assertIn("tool_calls", out)
        self.assertEqual(out["content"][0]["type"], "text")

    def test_stored_entry_never_mutated(self):
        msg = _native_assistant(with_ts=True)
        before = json.dumps(msg, sort_keys=True)
        core.SagentCore._render_history_message(msg)
        self.assertEqual(json.dumps(msg, sort_keys=True), before)

    def test_plain_user_unaffected(self):
        out = core.SagentCore._render_history_message(
            {"role": "user", "content": "hi", "ts": "2026-11-10T09:00:00"})
        self.assertEqual(out["content"], "[2026-11-10 09:00] hi")
        self.assertNotIn("name", out)


class TestFlattenPreservesToolFields(unittest.TestCase):
    """Boundary 3 — bridge._flatten_for_disk list branch (P0a-3)."""

    def test_string_content_passthrough_untouched(self):
        msgs = [_native_assistant(with_ts=False)]
        flat = bridge._flatten_for_disk(msgs)
        self.assertIs(flat[0], msgs[0])  # same object — untouched

    def test_multimodal_assistant_keeps_tool_calls(self):
        msg = _native_assistant(with_ts=True)
        msg["content"] = [{"type": "text", "text": "look"},
                          {"type": "image_url", "image_url": {"url": "x"}}]
        flat = bridge._flatten_for_disk([msg])
        self.assertIn("tool_calls", flat[0])
        self.assertEqual(flat[0]["tool_calls"], msg["tool_calls"])
        self.assertEqual(flat[0]["ts"], "2026-11-10T09:00:00")
        self.assertIsInstance(flat[0]["content"], str)

    def test_multimodal_tool_result_keeps_id(self):
        msg = _tool_result("call_A", with_ts=True)
        msg["content"] = [{"type": "text", "text": "photo of the result"}]
        flat = bridge._flatten_for_disk([msg])
        self.assertEqual(flat[0].get("tool_call_id"), "call_A")
        self.assertEqual(flat[0].get("name"), "search_my_memories")


class TestSendBoundaryAllOrNothing(unittest.TestCase):
    """P0a-6 + P0a-8 — core._llm_facing_messages."""

    def test_full_native_run_kept(self):
        msgs = [_native_assistant(),
                _tool_result("call_A"), _tool_result("call_B")]
        out = core._llm_facing_messages(msgs)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertIn("tool_calls", out[0])          # array intact
        self.assertEqual(out[1]["role"], "tool")     # results native
        self.assertEqual(out[2]["role"], "tool")

    def test_partial_match_downgrades_whole_run(self):
        # Sagent ALL-OR-NOTHING: one missing result must not leave a
        # partially-native run the template can dangle on
        msgs = [_native_assistant(),
                _tool_result("call_A")]              # call_B result missing
        out = core._llm_facing_messages(msgs)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertNotIn("tool_calls", out[0])       # array dropped (request-only)
        self.assertEqual(out[1]["role"], "user")     # result downgraded
        # the designer item 6: attribution rides the downgrade (native results never
        # embedded the marker at creation)
        self.assertTrue(out[1]["content"].startswith(
            "[Internal Tool Result: search_my_memories]"))

    def test_extra_result_id_downgrades_whole_run(self):
        msgs = [_native_assistant(ids=("call_A",)),
                _tool_result("call_A"), _tool_result("call_UNKNOWN")]
        out = core._llm_facing_messages(msgs)
        self.assertNotIn("tool_calls", out[0])
        self.assertEqual([m["role"] for m in out[1:]], ["user", "user"])

    def test_dangling_array_repaired(self):
        # eviction/split-round artifact: array with NO following results
        msgs = [_native_assistant(),
                {"role": "user", "content": "next"}]
        out = core._llm_facing_messages(msgs)
        self.assertNotIn("tool_calls", out[0])       # repaired
        self.assertEqual(out[0]["content"], "")      # words preserved
        self.assertEqual(out[1]["role"], "user")

    def test_orphan_tool_result_converted(self):
        # the text-path shape (synthetic id, no array) — the 09-13 fix.
        # the designer item 6: attribution rides every conversion.
        msgs = [{"role": "assistant", "content": "calling"},
                _tool_result("toolu_synthetic")]
        out = core._llm_facing_messages(msgs)
        self.assertEqual(out[1]["role"], "user")
        self.assertTrue(out[1]["content"].startswith(
            "[Internal Tool Result: search_my_memories]"))

    def test_text_path_marker_never_doubled(self):
        # the text path embeds the marker at creation — conversion must
        # pass it through, not stamp a second one
        msgs = [{"role": "assistant", "content": "calling"},
                _tool_result("toolu_synthetic",
                             content="[Internal Tool Result: check_mail]\nno mail")]
        out = core._llm_facing_messages(msgs)
        self.assertEqual(out[1]["content"].count("[Internal Tool Result:"), 1)

    def test_request_only_storage_untouched(self):
        msgs = [_native_assistant(),
                _tool_result("call_A")]              # partial
        before = json.dumps(msgs, sort_keys=True)
        core._llm_facing_messages(msgs)
        self.assertEqual(json.dumps(msgs, sort_keys=True), before)

    def test_plaintext_flow_identical(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
        out = core._llm_facing_messages(msgs)
        self.assertEqual(out, msgs)


def _trim_host(max_chars=100000):
    """Bare SagentCore for _trim_history_with_evicted (window_allocator pattern)."""
    host = object.__new__(core.SagentCore)
    host.max_history_chars = max_chars
    host.instance_id = "test"
    host._history_think_turns = None
    host._history_thinks = "none"
    return host


class TestTrimRoundAtomic(unittest.TestCase):
    """Boundary 5 — eviction never splits a tool round."""

    def test_assistant_and_results_evict_together(self):
        host = _trim_host(max_chars=100)  # forces the oversized ROUND out whole
        _big = "payload payload " * 40    # the round is the byte-heavy part
        history = [
            {"role": "user", "content": "old question"},
            _native_assistant(content=_big, with_ts=False),
            _tool_result("call_A", content=_big, with_ts=False),
            _tool_result("call_B", content=_big, with_ts=False),
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "reply2"},
            {"role": "user", "content": "more2"},
        ]
        kept, evicted = host._trim_history_with_evicted(history)
        self.assertEqual(kept[-1]["content"], "more2")
        # the round died whole: assistant and both results in evicted, in order
        roles = [m["role"] for m in evicted]
        self.assertIn("assistant", roles)
        self.assertEqual(roles.count("tool"), 2)
        # and KEPT has no dangling array / orphan
        for i, m in enumerate(kept):
            if m.get("tool_calls"):
                following = [k for k in kept[i + 1:] if k.get("role") == "tool"]
                self.assertEqual(len(following), len(m["tool_calls"]))

    def test_pre_split_orphan_repaired_at_send(self):
        # storage carries a split round (old-writer artifact): the send
        # boundary must repair it even though trim keeps it
        host = _trim_host(max_chars=100000)  # no eviction pressure
        history = [
            {"role": "user", "content": "q"},
            _native_assistant(with_ts=False),
            {"role": "user", "content": "interrupt"},
            _tool_result("call_A", with_ts=False),   # orphaned by the split
        ]
        kept, _ev = host._trim_history_with_evicted(history)
        facing = core._llm_facing_messages(kept)
        self.assertNotIn("tool_calls", facing[1])    # dangling repaired
        self.assertEqual(facing[-1]["role"], "user") # orphan → user


class TestFM2Guard(unittest.TestCase):
    """P0a-2 — the function:-prefix strip."""

    def test_strip(self):
        self.assertEqual(core._strip_function_prefix("function:search_x"), "search_x")
        self.assertEqual(core._strip_function_prefix("search_x"), "search_x")
        self.assertEqual(core._strip_function_prefix(""), "")
        self.assertEqual(core._strip_function_prefix(None), "")

    def test_text_parser_output_strippable(self):
        # the parser itself passes a prefixed JSON name through; the loop's
        # choke point (tested via _strip_function_prefix) normalizes it
        out = core._invoke_tool_from_response(
            '{"name": "function:save_my_memory", "arguments": {"content": "x"}}')
        self.assertIsNotNone(out)
        self.assertEqual(core._strip_function_prefix(out["name"]),
                         "save_my_memory")


class TestWritesSolo(unittest.TestCase):
    """P0a-7 — side-effect classification + the batch gate."""

    def test_unclassified_defaults_to_write(self):
        self.assertEqual(core._tool_side_effect("brand_new_tool"), "write")
        self.assertEqual(core._tool_side_effect(""), "write")
        self.assertEqual(core._tool_side_effect(None), "write")

    def test_known_reads(self):
        for t in ("search_my_memories", "list_my_memories", "deep_recall",
                  "search_searchie", "job_status", "sandbox_read", "news"):
            self.assertEqual(core._tool_side_effect(t), "read", t)

    def test_state_mutating_lookups_are_not_reads(self):
        # check_mail marks letters read — budget-free ≠ side-effect-free
        self.assertNotEqual(core._tool_side_effect("check_mail"), "read")
        self.assertNotEqual(core._tool_side_effect("read_letter"), "read")

    def test_destructive_wins(self):
        for t in ("remove_note", "unmute_person", "consolidate_memories"):
            self.assertEqual(core._tool_side_effect(t), "destructive", t)

    def test_read_and_destructive_disjoint(self):
        self.assertFalse(
            core._TOOL_SIDE_EFFECT_READS & core._TOOL_SIDE_EFFECT_DESTRUCTIVE)

    def test_batch_gate(self):
        reads = [("a", "search_my_memories", "{}", {}),
                 ("b", "search_searchie", "{}", {})]
        mixed = [("a", "search_my_memories", "{}", {}),
                 ("b", "save_my_memory", "{}", {})]
        self.assertEqual(core._writes_solo_max_workers(reads, 4), 4)
        self.assertEqual(core._writes_solo_max_workers(mixed, 4), 1)
        self.assertEqual(core._writes_solo_max_workers([], 4), 4)
        # classification failure = failsafe
        self.assertEqual(core._writes_solo_max_workers([("a", None, "{}", {})], 4), 1)


if __name__ == "__main__":
    unittest.main()


class TestDiskRoundTrip(unittest.TestCase):
    """Item 2 (the designer, 2026-11-10): P0a-3 proven END-TO-END on disk, without
    waiting for a live conversation. save → flatten → reload → render must
    preserve the contract fields. The REAL acceptance check remains: re-run
    scripts/audit_native_chains.py after residentb's first post-restart
    conversation — it should read native_assistants > 0 once she produces
    a native round in production."""

    def test_native_round_survives_disk(self):
        import json as _json
        import os as _os
        import tempfile as _tempfile
        import bridge as _bridge
        _orig = _bridge.HISTORY_DIR if hasattr(_bridge, "HISTORY_DIR") else None
        with _tempfile.TemporaryDirectory() as tmp:
            cfg = _os.path.join(tmp, "residentb.yaml")
            open(cfg, "w").write("instance_id: test\n")
            history = [
                {"role": "user", "content": "remember the rain",
                 "ts": "2026-11-10T11:00:00"},
                _native_assistant(content="saving it", ids=("call_A",),
                                  with_ts=True),
                _tool_result("call_A", content="saved", with_ts=True),
            ]
            _bridge._save_history_to_disk(cfg, "1000000001", history)
            path = _bridge._history_path(cfg, "1000000001") \
                if hasattr(_bridge, "_history_path") else None
            # fall back to locating the file
            if path is None or not _os.path.exists(path):
                import glob as _glob
                cands = _glob.glob(_os.path.join(tmp, "**", "*.json"),
                                   recursive=True)
                cands = [c for c in cands if ".summary" not in c]
                path = cands[0]
            with open(path) as fh:
                reloaded = _json.load(fh)
            msgs = reloaded if isinstance(reloaded, list) else (
                reloaded.get("messages") or reloaded.get("history"))
            self.assertEqual(len(msgs), 3)
            self.assertIn("tool_calls", msgs[1])
            self.assertEqual(msgs[1]["tool_calls"][0]["id"], "call_A")
            self.assertEqual(msgs[2].get("tool_call_id"), "call_A")
            # and the reload renders contract-intact
            out = core._llm_facing_messages(
                [core.SagentCore._render_history_message(m) for m in msgs])
            self.assertIn("tool_calls", out[1])       # full run stays native
            self.assertEqual(out[2]["role"], "tool")  # result stays a result


class TestPersistenceKeepsNativeCarriers(unittest.TestCase):
    """The acceptance-check catch (2026-11-10): residentb's first native round
    (4 calls, pure-call turn, empty content) persisted its four tool
    results but LOST the tool_calls carrier — the the designer-09-08
    'silence does not enter her context' filter dropped empty-content
    assistant entries, and a pure-call native turn IS empty-content. The
    filter now exempts tool_calls carriers: an action record is not
    silence. Source-anchored (the extend is inline in generate_response)."""

    def test_filter_exempts_tool_calls_carriers(self):
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn('not (_m.get("tool_calls"))', src)

    def test_filter_still_drops_true_silence(self):
        # the original 09-08 rule preserved: empty AND call-less assistant
        # turns stay out of her context
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn('and not (_m.get("content") or "").strip()))', src)


class TestContextFitterHonestMeasurement(unittest.TestCase):
    """The cutover-night catch (2026-11-10): the native fitter measured
    json.dumps(facing) — the envelope inflates ~40% — against a
    rendered-prompt char cap, so residenta's first native turn died with
    ContextBudgetExceeded against a prompt that fit. The native call
    site must pass the honest render (content bytes)."""

    def test_native_call_site_passes_render(self):
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn("render=_facing_content", src)

    def test_render_measures_content_not_envelope(self):
        msgs = [{"role": "system", "content": "S" * 500},
                {"role": "user", "content": "U" * 100}]
        self.assertEqual(len(core._facing_content(msgs)), 600)
        # and json.dumps would have been larger — the bug's shape
        self.assertGreater(len(json.dumps(msgs)), 600)

    def test_fitter_with_render_admits_honest_sizes(self):
        import context_budget as cb
        msgs = [{"role": "system", "content": "S" * 400},
                {"role": "user", "content": "U" * 100}]
        # ceiling 550: honest content = 500 → fits; the json envelope
        # (~575) exceeds it → would raise under the old measurement
        with self.assertRaises(cb.ContextBudgetExceeded):
            cb.fit(msgs, 550, reserve=0)
        out, budget = cb.fit(msgs, 550, reserve=0,
                             render=core._facing_content)
        self.assertEqual(len(out), 2)
        self.assertEqual(budget["chars"], 500)


class TestEnvelopeWindow(unittest.TestCase):
    """The rung-1 gate instrument (fixtoolcalls.md §P2): envelope-shaped
    args are counted and logged, so the lecture's retirement window is
    measured, not assumed."""

    def test_flat_envelope_counted(self):
        import core as _c
        before = _c._ENVELOPE_EVENTS["count"]
        _c._count_envelope_args(
            {"parameter": 'name="query"', "value": "weather"}, "t", "native")
        self.assertEqual(_c._ENVELOPE_EVENTS["count"], before + 1)

    def test_nested_envelope_counted(self):
        import core as _c
        before = _c._ENVELOPE_EVENTS["count"]
        _c._count_envelope_args({'parameter': {'"query"': '"weather"'}},
                                "t", "native")
        self.assertEqual(_c._ENVELOPE_EVENTS["count"], before + 1)

    def test_real_args_not_counted(self):
        import core as _c
        before = _c._ENVELOPE_EVENTS["count"]
        for args in ({"query": "weather"}, {"content": "a note"},
                     {"path": "notes/scratch.md"}, {}, None):
            _c._count_envelope_args(args, "t", "native")
        self.assertEqual(_c._ENVELOPE_EVENTS["count"], before)

    def test_fails_open(self):
        # None args, weird shapes — never raises
        core._count_envelope_args(None, "t", "native")
        core._count_envelope_args({"a": 1}, None, "native")
