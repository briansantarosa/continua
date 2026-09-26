"""llm_debug_test.py — the LLM debug mirror (specs/2026-09-16-llm-debug-mirror.md,
the designer go: root location, tools JSON included, ritual included, tail -F).

Pure, agent-free: tmp roots, no model, no network."""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
import llm_debug


class TestMirrorPath(unittest.TestCase):
    def test_persona_mapping(self):
        self.assertTrue(llm_debug.mirror_path("residenta", root="/tmp").endswith("residenta-debug.md"))
        self.assertTrue(llm_debug.mirror_path("residentb", root="/tmp").endswith("residentb-debug.md"))

    def test_unknown_instance_falls_back(self):
        self.assertTrue(llm_debug.mirror_path("newres", root="/tmp").endswith("newresdebug.md"))


class TestRenderV1Messages(unittest.TestCase):
    def test_roles_and_sizes(self):
        out = llm_debug.render_v1_messages([
            {"role": "system", "content": "Your name is persona-a."},
            {"role": "user", "content": "Do you want to talk?"},
        ])
        self.assertIn("### [system] (23 chars)", out)
        self.assertIn("### [user] (20 chars)", out)
        self.assertIn("Do you want to talk?", out)

    def test_tool_role_rendered_as_sent(self):
        # the FACING list (post orphan-tool transform) is what renders —
        # the mirror shows the request, not the storage
        out = llm_debug.render_v1_messages([
            {"role": "user", "content": "[Tool result] bookmarked"},
        ])
        self.assertIn("### [user]", out)
        self.assertIn("[Tool result] bookmarked", out)

    def test_multimodal_blocks(self):
        out = llm_debug.render_v1_messages([
            {"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ]},
        ])
        self.assertIn("what is this?", out)
        self.assertIn("[image_url block — not inlined]", out)
        self.assertNotIn("base64,", out)          # binary data never inlined

    def test_tools_json_included(self):
        tools = [{"type": "function", "function": {"name": "search_my_memories"}}]
        out = llm_debug.render_v1_messages(
            [{"role": "user", "content": "hi"}], tools)
        self.assertIn("## TOOLS ATTACHED", out)
        self.assertIn("search_my_memories", out)

    def test_no_tools_section_when_absent(self):
        out = llm_debug.render_v1_messages([{"role": "user", "content": "hi"}])
        self.assertNotIn("TOOLS ATTACHED", out)


class TestWriteCall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_overwrite_per_call_never_appends(self):
        p1 = llm_debug.write_call("residenta", {"user": "1000000001", "model": "m"},
                                  "CALL ONE", root=self.tmp)
        p2 = llm_debug.write_call("residenta", {"user": "1000000001", "model": "m"},
                                  "CALL TWO", root=self.tmp)
        self.assertEqual(p1, p2)
        text = open(p2).read()
        self.assertIn("CALL TWO", text)
        self.assertNotIn("CALL ONE", text)
        self.assertTrue(text.startswith("# LLM debug — residenta"))
        self.assertIn("- ts: ", text)

    def test_kill_switch(self):
        os.environ["CONTINUA_LLM_DEBUG"] = "0"
        try:
            p = llm_debug.write_call("residenta", {}, "x", root=self.tmp)
            self.assertEqual(p, "")
            self.assertEqual(os.listdir(self.tmp), [])
        finally:
            del os.environ["CONTINUA_LLM_DEBUG"]

    def test_fail_open_on_bad_path(self):
        p = llm_debug.write_call("residenta", {}, "x",
                                 root=os.path.join(self.tmp, "bad\x00name"))
        self.assertEqual(p, "")

    def test_no_temp_leftovers(self):
        llm_debug.write_call("residenta", {}, "x", root=self.tmp)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_current_instance_fallback(self):
        old = llm_debug._CURRENT["instance"]
        llm_debug._CURRENT["instance"] = "residentb"
        try:
            p = llm_debug.write_call(None, {}, "residentb turn", root=self.tmp)
            self.assertTrue(p.endswith("residentb-debug.md"))
            self.assertIn("residentb turn", open(p).read())
        finally:
            llm_debug._CURRENT["instance"] = old

    def test_explicit_instance_beats_current(self):
        old = llm_debug._CURRENT["instance"]
        llm_debug._CURRENT["instance"] = "residentb"
        try:
            p = llm_debug.write_call("residenta", {}, "n turn", root=self.tmp)
            self.assertTrue(p.endswith("residenta-debug.md"))
        finally:
            llm_debug._CURRENT["instance"] = old

    def test_no_instance_no_write(self):
        old = llm_debug._CURRENT["instance"]
        llm_debug._CURRENT["instance"] = None
        try:
            self.assertEqual(llm_debug.write_call(None, {}, "x", root=self.tmp), "")
        finally:
            llm_debug._CURRENT["instance"] = old


class TestCallSitePresence(unittest.TestCase):
    """Source guards: a future refactor must not silently drop a hook."""

    def test_core_has_both_arms(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "core.py")).read()
        n = len(re.findall(r"llm_debug\.write_call|_lldbg\.write_call", src))
        self.assertEqual(n, 2, "core.py mirror hooks: raw arm + /v1 arm")

    def test_ritual_has_loop_and_three_paths(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ritual.py")).read()
        self.assertIn("llm_debug.set_current_instance(inst)", src)
        self.assertGreaterEqual(
            len(re.findall(r"llm_debug\.write_call", src)), 3,
            "ritual.py mirror hooks: raw + /v1 + /api/chat")

    def test_gitignore_covers_mirrors(self):
        gi = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gitignore")).read()
        self.assertIn("residenta-debug.md", gi)
        self.assertIn("residentb-debug.md", gi)


if __name__ == "__main__":
    unittest.main(verbosity=2)
