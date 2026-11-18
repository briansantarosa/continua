"""genresp_scope_test.py — regression guard for the 2026-09-28 incident
(helpnany follow-up): code inside generate_response may reference ONLY
instance attributes and module globals — never __init__ locals (llm_cfg,
config, app_cfg). The 09-28 llm_cfg NameError killed every turn for 9.5h
BECAUSE no offline test executed the trim-target branch end-to-end.

This test statically compiles generate_response's AST and fails if it
reads any name that exists only in __init__'s local scope. Cheap, agent-
free, and it would have caught the incident.
Run: python3 genresp_scope_test.py
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

INIT_LOCALS = {"llm_cfg", "app_cfg", "mem_cfg", "_mem_cfg", "config"}


class GenRespScopeTests(unittest.TestCase):
    def _method_src(self, name):
        src = inspect_src = open(core.__file__).read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "SagentCore":
                for f in node.body:
                    if isinstance(f, ast.FunctionDef) and f.name == name:
                        return f
        self.fail(f"method {name} not found")

    def test_generate_response_never_reads_init_locals(self):
        fn = self._method_src("generate_response")
        bad = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in INIT_LOCALS \
                    and isinstance(node.ctx, ast.Load):
                        bad.append((node.id, node.lineno))
        self.assertEqual(bad, [],
                         "generate_response reads __init__ locals — these "
                         "raise NameError at RUNTIME (the 09-28 incident): "
                         f"{bad}")

    def test_nested_callables_also_clean(self):
        # the incident lived INSIDE the method body — nested defs included
        fn = self._method_src("generate_response")
        found = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and n.id == "llm_cfg"
                 and isinstance(n.ctx, ast.Load)]
        self.assertEqual(found, [])

    def test_init_reads_the_flag_into_scope(self):
        # the flag must be readable at init: self._history_target_chars set
        init_fn = None
        tree = ast.parse(open(core.__file__).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "SagentCore":
                for f in node.body:
                    if isinstance(f, ast.FunctionDef) and f.name == "__init__":
                        init_fn = f
        self.assertIsNotNone(init_fn)
        assigns = ast.dump(init_fn)
        self.assertIn("_history_target_chars", assigns,
                      "llm.history_target_chars must be read in __init__")


if __name__ == "__main__":
    unittest.main(verbosity=2)
