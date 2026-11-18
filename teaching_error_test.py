"""teaching_error_test.py — Phase 2 of helpnany.md (2026-09-28): the
teaching-error skeleton must name REAL parameters, never schema machinery.

The wound (verified in the residentadebug mirror, 09-15 14:53 ×3): the empty-argument
teaching message rendered `<parameter name="type">… name="properties">…
name="required">` — the keys of the nested JSON-schema OBJECT — as if they
were parameter names. persona-a copied the broken template verbatim twice before
abandoning the call. Both tool-def shapes exist in the codebase:

  flat (built-ins, core.py built-in registration):
      {"name", "parameters": {"query": {...}}}          — keys are names
  nested OpenAI (_get_function_definition):
      {"function": {"parameters": {"type": "object",
                                    "properties": {...}}}} — names under
                                                              properties

Pure tests, agent-free, no sandbox. Run:
  python3 teaching_error_test.py
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

SCHEMA_KEYS = ("type", "properties", "required")


def _nested(fn_name, props, required=None):
    """The exact shape _get_function_definition emits (core.py:778+)."""
    return {"type": "function", "function": {
        "name": fn_name, "description": "d",
        "parameters": {"type": "object", "properties": props,
                       "required": required or list(props)}}}


def _flat(fn_name, params):
    """The exact shape the built-in registrations emit (core.py:504+)."""
    return {"name": fn_name, "description": "d", "parameters": params}


class TestToolParamNames(unittest.TestCase):
    def test_nested_shape_names_properties_keys(self):
        d = _nested("search_my_memories", {"query": {"type": "string"},
                                           "top_k": {"type": "integer"}})
        self.assertEqual(core._tool_param_names(d), ["query", "top_k"])

    def test_flat_shape_keys_are_names(self):
        d = _flat("search_my_memories", {"query": {"type": "string",
                                                   "required": True},
                                         "top_k": {"type": "integer"}})
        self.assertEqual(core._tool_param_names(d), ["query", "top_k"])

    def test_schema_machinery_never_leaks(self):
        for d in (_nested("search_my_memories", {"query": {"type": "string"}}),
                  _flat("search_my_memories", {"query": {"type": "string"}})):
            names = core._tool_param_names(d)
            for bad in SCHEMA_KEYS:
                self.assertNotIn(bad, names)

    def test_empty_params_falls_back_to_tool_primary(self):
        for fn, primary in core._TOOL_PRIMARY.items():
            d = _nested(fn, {}) if fn != "list_my_memories" else \
                _flat(fn, {})
            d.setdefault("function", d.get("function") or {})["name"] = fn \
                if "function" in d else d.update({"name": fn}) or d["name"]
            names = core._tool_param_names(d)
            self.assertEqual(names, [primary],
                             f"{fn}: fallback must be the primary param")

    def test_no_fallback_available_gives_empty_not_garbage(self):
        d = {"function": {"name": "unknown_tool", "parameters": {}}}
        self.assertEqual(core._tool_param_names(d), [])

    def test_none_and_garbage_fail_open_to_empty(self):
        self.assertEqual(core._tool_param_names(None), [])
        self.assertEqual(core._tool_param_names("junk"), [])
        self.assertEqual(core._tool_param_names({}), [])


class TestTeachingSkeletonEndToEnd(unittest.TestCase):
    """The skeleton STRING the teaching errors build — repro of the exact
    09-15 14:53 message shape, asserting the fix at the delivery surface."""

    def _skeleton(self, tool_def, fn_name):
        names = core._tool_param_names(tool_def)
        return ("<call><function>" + fn_name + "</function>"
                + "".join(f'<parameter name="{p}">value</parameter>'
                          for p in names)
                + "</call>")

    def test_search_my_memories_skeleton_names_query(self):
        # repro of the live incident: built-in search_my_memories, empty query
        d = _flat("search_my_memories",
                  {"query": {"description": "What to look for",
                             "type": "string", "required": True},
                   "top_k": {"description": "Max", "type": "integer"}})
        skel = self._skeleton(d, "search_my_memories")
        self.assertIn('name="query"', skel)
        self.assertNotIn('name="type"', skel)
        self.assertNotIn('name="properties"', skel)
        self.assertNotIn('name="required"', skel)

    def test_nested_yaml_tool_skeleton_names_query(self):
        d = _nested("search_my_memories", {"query": {"type": "string"}})
        skel = self._skeleton(d, "search_my_memories")
        self.assertIn('name="query"', skel)
        self.assertNotIn('name="required"', skel)


class TestLiveDefs(unittest.TestCase):
    """The REAL defs both residents run — every registered tool must yield
    sane names (its own params, never schema machinery)."""

    def test_all_registered_tools_produce_clean_names(self):
        import yaml
        for inst in ("residenta", "residentb"):
            cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
            mt = core._normalize_memory_tools(cfg.get("memory") or {})
            defs = core._get_function_definition(cfg.get("tools") or [], mt)
            for d in defs:
                names = core._tool_param_names(d)
                fn_name = d["function"]["name"]
                for bad in SCHEMA_KEYS:
                    self.assertNotIn(
                        bad, names,
                        f"{inst}/{fn_name}: schema key {bad!r} leaked as a "
                        "parameter name — the teaching skeleton would lie")
                self.assertTrue(names or fn_name in
                                ("sandbox_list", "check_mail",
                                 "list_my_memories", "list_notes",
                                 "list_projects", "list_essences",
                                 "my_trajectory", "consolidate_memories",
                                 "read_my_ledger"),
                                f"{inst}/{fn_name}: no names, no primary")


if __name__ == "__main__":
    unittest.main(verbosity=2)
