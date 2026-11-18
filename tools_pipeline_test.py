"""tools_pipeline_test.py — agent-free tests for the desk-to-capability
pipeline (toolplan.md): lint rules, the review gate (fake desk), the
runtime (loader, kill switches, timeout, audit, fail-open), the my_
namespace rule, and the core wiring (stubbed openai).

Run:  python3 tools_pipeline_test.py
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tools_lint as tl
import tools_runtime as tr
import tools_review as tv

GOOD_CODE = '''"""my_counter — counts things she asks it to count."""
import json


def run(args, house):
    n = int(args.get("n") or 0)
    return f"counted to {n}"
'''

GOOD_MANIFEST = {
    "name": "my_counter",
    "what": "Counts to a number, in my own way.",
    "args": {"n": {"type": "integer", "required": False}},
    "capabilities": {"files": "desk", "network": []},
    "kill_switch": "CONTINUA_TOOL_MY_COUNTER",
}


class TestLint(unittest.TestCase):
    def test_clean_tool_passes(self):
        r = tl.lint_source(GOOD_CODE)
        self.assertTrue(r["ok"], r["errors"])

    def test_manifest_gate(self):
        r = tl.lint_manifest(GOOD_MANIFEST, {"news"})
        self.assertTrue(r["ok"], r["errors"])

    def test_dynamic_exec_banned(self):
        for snip in ("eval('1+1')", "exec('x=1')", "compile('x', 'f', 'e')",
                     "os.system('ls')", "os.popen('ls')"):
            r = tl.lint_source("import os\n" + snip)
            self.assertFalse(r["ok"], snip)

    def test_stdlib_only_v1(self):
        r = tl.lint_source("import requests\nimport socket")
        self.assertFalse(r["ok"])
        self.assertTrue(any("requests" in e for e in r["errors"]))
        self.assertTrue(any("socket" in e for e in r["errors"]))

    def test_secrets_banned(self):
        # synthetic token assembled at runtime — never write the full shape
        # into source (the publication gate scans for the token shape)
        r = tl.lint_source("# k = '8612345678:AAF" + "lOUz4-skJFfkbbYnEmPzjtpqw3Y0zSgM'")
        self.assertFalse(r["ok"])
        self.assertTrue(any("banned pattern" in e for e in r["errors"]))

    def test_host_path_banned(self):
        r = tl.lint_source("p = '/tmp/data/chronicle'")
        self.assertFalse(r["ok"])

    def test_size_cap(self):
        r = tl.lint_source("\n".join(["x = 1"] * 500))
        self.assertFalse(r["ok"])
        self.assertTrue(any("cap" in e for e in r["errors"]))

    def test_unparseable_fails_closed(self):
        r = tl.lint_source("def broken(:")
        self.assertFalse(r["ok"])

    def test_namespace_rule(self):
        r = tl.lint_manifest({"name": "send_message", "what": "x",
                              "args": {}, "kill_switch": "CONTINUA_TOOL_X",
                              "capabilities": {}})
        self.assertFalse(r["ok"])
        self.assertTrue(any("my_" in e for e in r["errors"]))
        r2 = tl.lint_manifest({"name": "my_news", "what": "x", "args": {},
                               "kill_switch": "CONTINUA_TOOL_MY_NEWS",
                               "capabilities": {}}, house_tool_names=set())
        self.assertTrue(r2["ok"])  # my_news ≠ house 'news' — namespace holds


class TestRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_dir = tr.MYTOOLS_DIR
        tr.MYTOOLS_DIR = self.tmp
        tr.AUDIT_LOG = os.path.join(self.tmp, "audit.log")
        self.old_env = os.environ.get("CONTINUA_MYTOOLS")
        os.environ.pop("CONTINUA_MYTOOLS", None)
        with open(os.path.join(self.tmp, "my_counter.py"), "w") as f:
            f.write(GOOD_CODE)
        import yaml
        with open(os.path.join(self.tmp, "my_counter.yaml"), "w") as f:
            yaml.safe_dump(GOOD_MANIFEST, f)

    def tearDown(self):
        tr.MYTOOLS_DIR = self.old_dir
        tr.AUDIT_LOG = os.path.join(self.tmp, "audit.log")
        if self.old_env is not None:
            os.environ["CONTINUA_MYTOOLS"] = self.old_env
        else:
            os.environ.pop("CONTINUA_MYTOOLS", None)

    def test_loader_defs(self):
        defs = tr.load_tool_defs()
        self.assertEqual(len(defs), 1)
        d = defs[0]
        self.assertEqual(d["name"], "my_counter")
        self.assertTrue(d["my_tool"])
        self.assertIn("n", d["parameters"])

    def test_run_ok(self):
        out = tr.run_tool("residenta", "my_counter", {"n": 5})
        self.assertIn("counted to 5", out)

    def test_fail_open_error(self):
        with open(os.path.join(self.tmp, "my_boom.py"), "w") as f:
            f.write("def run(args, house):\n    raise ValueError('oops')\n")
        import yaml
        with open(os.path.join(self.tmp, "my_boom.yaml"), "w") as f:
            yaml.safe_dump({"name": "my_boom", "what": "boom test",
                            "args": {},
                            "kill_switch": "CONTINUA_TOOL_MY_BOOM",
                            "capabilities": {"files": "desk"}}, f)
        out = tr.run_tool("residenta", "my_boom", {})
        self.assertIn("failed", out)
        self.assertIn("oops", out)

    def test_master_switch(self):
        os.environ["CONTINUA_MYTOOLS"] = "0"
        try:
            out = tr.run_tool("residenta", "my_counter", {"n": 1})
            self.assertIn("switched off", out)
        finally:
            os.environ.pop("CONTINUA_MYTOOLS", None)

    def test_per_tool_switch(self):
        os.environ["CONTINUA_TOOL_MY_COUNTER"] = "0"
        try:
            out = tr.run_tool("residenta", "my_counter", {"n": 1})
            self.assertIn("switched off", out)
        finally:
            os.environ.pop("CONTINUA_TOOL_MY_COUNTER", None)

    def test_audit_append_only(self):
        tr.run_tool("residenta", "my_counter", {"n": 1})
        tr.run_tool("residenta", "my_counter", {"n": 2})
        lines = open(tr.AUDIT_LOG).read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        rec = json.loads(lines[1])
        self.assertEqual(rec["tool"], "my_counter")
        self.assertEqual(rec["outcome"], "ok")


class TestReviewGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # a FAKE desk: sandbox.run is monkeypatched to read/write the tmp
        self.desk = {}
        self.old_run = tv.sb.run

        def fake_run(instance, argv, timeout=None, stdin_text=None):
            cmd = argv[-1] if len(argv) > 2 else " ".join(argv)
            if cmd.startswith("ls "):
                # the desk is a dict; filter by the requested directory
                # (the glob itself is the shell's job — the fake honors
                # the prefix so moved-out candidates stop appearing)
                d = cmd.split()[1].split("*")[0]
                keys = [k for k in self.desk if k.startswith(d)]
                return {"ok": True, "exit": 0,
                        "stdout": "\n".join(sorted(keys)),
                        "stderr": ""}
            if "mkdir -p" in cmd and "cat > " in cmd:
                path = cmd.split("cat > ", 1)[1].strip().strip("'")
                self.desk[path] = stdin_text or ""
                return {"ok": True, "exit": 0, "stdout": "", "stderr": ""}
            if "cat " in cmd:
                path = cmd.split("cat ", 1)[1].strip().strip("'")
                body = self.desk.get(path)
                if body is None:
                    # faithful missing-file: the real shell exits 1
                    return {"ok": True, "exit": 1, "stdout": "",
                            "stderr": "no such file"}
                return {"ok": True, "exit": 0,
                        "stdout": body,
                        "stderr": ""}
            if "mv " in cmd:
                parts = cmd.split()
                src, dst = parts[-2].strip("'"), parts[-1].strip("'")
                if src in self.desk:
                    self.desk[dst] = self.desk.pop(src)
                    return {"ok": True, "exit": 0, "stdout": "", "stderr": ""}
                return {"ok": True, "exit": 1, "stdout": "", "stderr": "no"}
            return {"ok": True, "exit": 0, "stdout": "", "stderr": ""}

        tv.sb.run = fake_run
        # git is stubbed: a test must never stage/commit the real repo
        self.old_git = tv._git
        tv._git = lambda *a: (True, "stubbed-git")
        self.old_mytools = tv.MYTOOLS_DIR
        tv.MYTOOLS_DIR = os.path.join(self.tmp, "mytools")
        self.old_notified = tv.NOTIFIED_FILE
        tv.NOTIFIED_FILE = os.path.join(self.tmp, "notified.json")
        self.old_notify = tv.notify_dan
        self.notified = []
        tv.notify_dan = lambda text, instance='residenta': (self.notified.append((instance, text)) or True)
        self.old_env = os.environ.get("CONTINUA_TOOLREVIEW")
        os.environ.pop("CONTINUA_TOOLREVIEW", None)

    def tearDown(self):
        tv.sb.run = self.old_run
        tv._git = self.old_git
        tv.MYTOOLS_DIR = self.old_mytools
        tv.NOTIFIED_FILE = self.old_notified
        tv.notify_dan = self.old_notify
        if self.old_env is not None:
            os.environ["CONTINUA_TOOLREVIEW"] = self.old_env
        else:
            os.environ.pop("CONTINUA_TOOLREVIEW", None)

    def _submit(self, name="my_counter", code=GOOD_CODE, man=GOOD_MANIFEST):
        import yaml
        self.desk[f"tools/_candidates/{name}.py"] = code
        self.desk[f"tools/_candidates/{name}.yaml"] = yaml.safe_dump(man)

    def test_scan_candidates_through_fake_sandbox(self):
        self._submit()
        cands = tv.scan_candidates(["residenta"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["name"], "my_counter")
        self.assertTrue(cands[0]["hash"])

    def test_approve_installs_and_decides(self):
        self._submit()
        cand = tv.scan_candidates(["residenta"])[0]
        out = tv.approve(cand, cand["hash"])
        self.assertIn("installed", out)
        # git-tracked file exists with provenance to commit
        self.assertTrue(os.path.exists(
            os.path.join(tv.MYTOOLS_DIR, "my_counter.py")))
        # decision landed in her desk outbox
        self.assertIn("APPROVED by the designer",
                      self.desk["tools/_outbox/my_counter.decision.md"])
        # idempotence: decided candidates leave the queue
        self.assertEqual(tv.scan_candidates(["residenta"]), [])

    def test_hash_lock_refuses_moving_target(self):
        self._submit()
        cand = tv.scan_candidates(["residenta"])[0]
        out = tv.approve(cand, "00000000")
        self.assertIn("version changed", out)
        # nothing installed
        self.assertFalse(os.path.exists(
            os.path.join(tv.MYTOOLS_DIR, "my_counter.py")))

    def test_lint_failure_never_reaches_dan(self):
        self._submit(code="import socket\neval('1')")
        n = tv.notify_all()
        self.assertEqual(n, 0)  # failed lint = returned to her, not to the designer
        self.assertIn("RETURNED AT LINT",
                      self.desk["tools/_outbox/my_counter.decision.md"])

    def test_notify_sends_request_with_hash(self):
        self._submit()
        tv.notify_all()
        self.assertTrue(self.notified)
        self.assertEqual(self.notified[0][0], "residenta")  # requests ride the candidate's own bot
        self.assertIn("[SYSTEM] Tool approval request", self.notified[0][1])
        self.assertIn("reply right here", self.notified[0][1])
        self.assertIn(self.notified[0][1].split("Version: ")[1].split("\n")[0],
                      tv.scan_candidates(["residenta"])[0]["hash"])

    # --- §ACTIVATION AT-1: dedup per content hash; fail-open; state ---
    def test_notify_dedup_same_hash(self):
        self._submit()
        self.assertEqual(tv.notify_all(instances=["residenta"]), 1)
        self.assertEqual(tv.notify_all(instances=["residenta"]),
                         0)  # same hash → announced once
        self.assertEqual(len(self.notified), 1)

    def test_notify_repeats_only_on_edit(self):
        self._submit()
        tv.notify_all(instances=["residenta"])
        self.desk["tools/_candidates/my_counter.py"] = GOOD_CODE + "\n# touched\n"
        self.assertEqual(tv.notify_all(instances=["residenta"]),
                         1)  # the edit is a new candidate
        self.assertEqual(len(self.notified), 2)

    def test_lint_return_once_per_hash(self):
        self._submit(code="import socket\neval('1')")
        tv.notify_all(instances=["residenta"])
        n = len(tv._load_notified())
        self.assertEqual(n, 1)
        tv.notify_all(instances=["residenta"])
        self.assertEqual(len(tv._load_notified()), n)  # recorded once

    def test_notify_fail_open_and_retry(self):
        self._submit()
        self.old_notify2 = tv.notify_dan
        try:
            tv.notify_dan = lambda text, instance='residenta': False
            self.assertEqual(tv.notify_all(instances=["residenta"]), 0)
            self.assertEqual(tv._load_notified(),
                             {})  # not recorded → retries
            tv.notify_dan = lambda text, instance='residenta': (
                self.notified.append((instance, text)) or True)
            self.assertEqual(tv.notify_all(instances=["residenta"]), 1)
        finally:
            tv.notify_dan = self.old_notify2

    # --- §ACTIVATION AT-2: the seeded rules; the outbox count ---
    def test_seed_house_rules_once(self):
        self.assertTrue(tv.seed_house_rules("residenta"))
        self.assertIn("tools/HOUSE_RULES.md", self.desk)
        self.assertIn("house rules", self.desk["tools/HOUSE_RULES.md"].lower())
        self.desk["tools/HOUSE_RULES.md"] += "\nher annotation"
        self.assertFalse(tv.seed_house_rules("residenta"))  # never overwrites
        self.assertIn("her annotation", self.desk["tools/HOUSE_RULES.md"])

    def test_pending_decisions_count(self):
        self.assertEqual(tv.pending_decisions("residenta"), 0)
        self.desk["tools/_outbox/a.decision.md"] = "x"
        self.desk["tools/_outbox/b.decision.md"] = "y"
        self.assertEqual(tv.pending_decisions("residenta"), 2)

    def test_kill_switch(self):
        os.environ["CONTINUA_TOOLREVIEW"] = "0"
        try:
            self.assertFalse(tv.enabled())
            self._submit()
            n = tv.notify_all()
            self.assertEqual(n, 0)
        finally:
            os.environ.pop("CONTINUA_TOOLREVIEW", None)


class TestCoreWiring(unittest.TestCase):
    def test_injection_and_dispatch(self):
        sys.modules.pop("core", None)
        fake = type(sys)("openai")
        fake.OpenAI = object
        fake.APIError = Exception
        sys.modules["openai"] = fake
        tmp = tempfile.mkdtemp()
        old_dir = tr.MYTOOLS_DIR
        tr.MYTOOLS_DIR = tmp
        with open(os.path.join(tmp, "my_counter.py"), "w") as f:
            f.write(GOOD_CODE)
        import yaml
        with open(os.path.join(tmp, "my_counter.yaml"), "w") as f:
            yaml.safe_dump(GOOD_MANIFEST, f)
        try:
            import core
            defs = core._get_function_definition(
                [], {}, mytools_enabled=True)
            names = [t["function"]["name"] for t in defs]
            self.assertIn("my_counter", names)
            # house tool never overridable: yaml 'news' + loader skip
            defs = core._get_function_definition(
                [{"name": "news", "description": "house"}], {},
                news_enabled=True, mytools_enabled=True)
            names = [t["function"]["name"] for t in defs]
            self.assertEqual(names.count("news"), 1)
            # dispatch end-to-end
            class FS:
                instance_id = "residenta"
            out = core.SagentCore._execute_function_call(
                FS(), "my_counter", {"n": 7})
            self.assertIn("counted to 7", out)
        finally:
            tr.MYTOOLS_DIR = old_dir


class TestHouseFetch(unittest.TestCase):
    def test_allowlist_enforced_at_runtime(self):
        h = tr.HouseHelpers("residenta", {"capabilities": {
            "files": "desk",
            "network": ["api.open-meteo.com"]}})
        # a disallowed host is refused BEFORE any network happens
        with self.assertRaises(PermissionError):
            h.fetch("https://evil.example.com/x")
        # the allowlisted host passes the gate (network mocked via a fake
        # httpx module — fetch imports it at call time)
        import types
        fake_httpx = types.ModuleType("httpx")
        calls = []

        class _Resp:
            text = "ok"

            def raise_for_status(self):
                pass

        class _Session:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                calls.append(url)
                return _Resp()

        fake_httpx.Client = lambda *a, **k: _Session()
        old_mod = sys.modules.get("httpx")
        sys.modules["httpx"] = fake_httpx
        try:
            self.assertEqual(h.fetch("https://api.open-meteo.com/v1/x"),
                             "ok")
        finally:
            if old_mod is not None:
                sys.modules["httpx"] = old_mod
            else:
                sys.modules.pop("httpx", None)
        self.assertEqual(len(calls), 1)
        # subdomains are NOT implied
        h2 = tr.HouseHelpers("residenta", {"capabilities": {"network": ["example.com"]}})
        with self.assertRaises(PermissionError):
            h2.fetch("https://api.example.com/x")


class TestWakeToolsLine(unittest.TestCase):
    """toolplan §ACTIVATION AT-2: the packet line's gating and quietness."""

    CFG_ON = {"continua": {"mytools": {"enabled": True}}}

    def test_gate_off_renders_nothing(self):
        import wake
        self.assertEqual(wake.tools_line("residenta", {}), [])
        self.assertEqual(wake.tools_line(
            "residenta", {"continua": {"mytools": {"enabled": False}}}), [])
        old = os.environ.get("CONTINUA_MYTOOLS")
        os.environ["CONTINUA_MYTOOLS"] = "0"
        try:
            self.assertEqual(wake.tools_line("residenta", self.CFG_ON), [])
        finally:
            if old is None:
                os.environ.pop("CONTINUA_MYTOOLS", None)
            else:
                os.environ["CONTINUA_MYTOOLS"] = old

    def test_on_renders_with_decisions(self):
        import wake
        old = tv.pending_decisions
        tv.pending_decisions = lambda inst: 2
        try:
            lines = wake.tools_line("residenta", self.CFG_ON)
            self.assertTrue(any("YOUR TOOLS" in x for x in lines))
            self.assertTrue(any("decisions waiting" in x and ": 2" in x
                                for x in lines))
            tv.pending_decisions = lambda inst: 0
            lines = wake.tools_line("residenta", self.CFG_ON)
            self.assertTrue(any("YOUR TOOLS" in x for x in lines))
            self.assertFalse(any("decisions waiting" in x for x in lines))
        finally:
            tv.pending_decisions = old


class TestHouseFetchAllowlist(unittest.TestCase):
    """v1.1's network door exists: manifest-declared domains only."""

    def test_exact_host_allowlist(self):
        h = tr.HouseHelpers("residenta", {"capabilities": {"network": [
            "api.open-meteo.com"]}})
        self.assertEqual(h.allowed_hosts, {"api.open-meteo.com"})
        # not on the list → refused BEFORE any network happens
        with self.assertRaises(PermissionError):
            h.fetch("https://evil.example.com/x")


if __name__ == "__main__":
    unittest.main(verbosity=2)