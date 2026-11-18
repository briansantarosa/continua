"""autoresident_test.py — the drop-a-YAML onboarding pipeline, 2026-10-08.

Covers the autoresident.md spec: validation (incl. the duplicate
instance/collection/token collision class), the silent plan, idempotent
provisioning (desk contract: owner continua, mode 700 — never retouched once
it exists), hot-add activation reporting, passive verification, and the
manifest as the dry-run's replacement. Everything runs against a temp
SAGENT_BASE; the privileged ops go through a fake sudo (no real desks are
created or touched here).
Run: python3 autoresident_test.py
"""
import asyncio
import glob
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys_path_hack = os.path.dirname(os.path.abspath(__file__))
if sys_path_hack not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_hack)

import autoresident  # noqa: E402
import bridge        # noqa: E402


def _cfg_body(instance="t1", token=None, wake="false", identity=True):
    token = token or ("123456789:AA" + "x" * 30)
    lines = [
        'app:',
        f'  instance_id: "{instance}"',
        f'  collection_name: "{instance}_memories"',
        'llm:',
        '  model: test-model',
        '  base_url: http://127.0.0.1:9/v1',
        'telegram:',
        f'  token: "{token}"',
    ]
    if identity:
        lines += ['prompts:', f'  identity: "You are {instance}."']
    lines += ['continua:', '  wake:', f'    enabled: {wake}']
    return "\n".join(lines) + "\n"


class _RC:
    def __init__(self, returncode, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class FakeSudo:
    """In-memory stand-in for `sudo -n`: tracks every call, simulates mkdir
    persistence so `test -d` reflects what was 'created'."""

    def __init__(self):
        self.calls = []
        self.dirs = set()

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "test" and args[1] == "-d":
            return _RC(0 if args[2] in self.dirs else 1)
        if args[0] == "mkdir" and args[1] == "-p":
            self.dirs.add(args[2])
            return _RC(0)
        if args[0] == "stat":
            return _RC(0, stdout=b"700 continua")
        return _RC(0)  # chown / chmod succeed

    def count(self, verb):
        return sum(1 for c in self.calls if c and c[0] == verb)


class FakeTask:
    def __init__(self):
        self._done = False

    def done(self):
        return self._done


def _make_bind(recorder, result="task"):
    async def bind(config_path, filename, **kwargs):
        recorder.append((config_path, filename, kwargs))
        if result == "task":
            return FakeTask()
        return None  # simulate a failed arming
    return bind


class _Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="autores-test-")
        os.makedirs(os.path.join(self.base, "configs"))
        self.sudo = FakeSudo()
        self._sudo_patcher = patch.object(autoresident, "_sudo_run", self.sudo)
        self._sudo_patcher.start()
        self._warm_patcher = patch.object(autoresident, "warm_new_agent")
        self._warm_patcher.start()
        # isolate the bridge's dynamic-task registry (activate registers into it)
        self._saved_dynamic = bridge._dynamic_tasks
        bridge._dynamic_tasks = []
        self.addCleanup(self._sudo_patcher.stop)
        self.addCleanup(self._warm_patcher.stop)
        self.addCleanup(self._restore_dynamic)
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def _restore_dynamic(self):
        bridge._dynamic_tasks = self._saved_dynamic


class ValidateTests(unittest.TestCase):
    def test_valid_minimal(self):
        errors, warnings = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body()), "t1.yaml", [])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_filename_instance_mismatch(self):
        errors, _ = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body()), "other.yaml", [])
        self.assertTrue(any("does not agree" in e for e in errors))

    def test_bad_instance_name(self):
        errors, _ = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body(instance="Bad-Name")), "Bad-Name.yaml", [])
        self.assertTrue(any("does not match" in e for e in errors))

    def test_missing_collection(self):
        cfg = yaml.safe_load(_cfg_body())
        del cfg["app"]["collection_name"]
        errors, _ = autoresident.validate_candidate(cfg, "t1.yaml", [])
        self.assertTrue(any("collection_name" in e for e in errors))

    def test_missing_llm_model_and_urls(self):
        cfg = yaml.safe_load(_cfg_body())
        del cfg["llm"]["model"]
        del cfg["llm"]["base_url"]
        errors, _ = autoresident.validate_candidate(cfg, "t1.yaml", [])
        self.assertTrue(any("llm.model" in e for e in errors))
        self.assertTrue(any("base_url" in e for e in errors))

    def test_placeholder_and_malformed_tokens(self):
        errors, _ = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body(token="YOUR_TELEGRAM_TOKEN")), "t1.yaml", [])
        self.assertTrue(any("placeholder" in e for e in errors))
        errors, _ = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body(token="abc")), "t1.yaml", [])
        self.assertTrue(any("bot token" in e for e in errors))

    def test_missing_identity_is_warning_only(self):
        errors, warnings = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body(identity=False)), "t1.yaml", [])
        self.assertEqual(errors, [])
        self.assertTrue(any("identity" in w for w in warnings))

    def test_duplicate_instance_collection_token(self):
        other = ("residenta.yaml", yaml.safe_load(_cfg_body(instance="t1")))
        errors, _ = autoresident.validate_candidate(
            yaml.safe_load(_cfg_body()), "t1.yaml", [other])
        self.assertTrue(any("instance_id" in e for e in errors))
        self.assertTrue(any("collection_name" in e for e in errors))
        self.assertTrue(any("token" in e for e in errors))

    def test_non_mapping_yaml(self):
        errors, _ = autoresident.validate_candidate(["nope"], "t1.yaml", [])
        self.assertTrue(any("mapping" in e for e in errors))


class PlanTests(_Base):
    def test_plan_fields_and_env_overrides(self):
        cfg = yaml.safe_load(_cfg_body())
        with patch.dict(os.environ, {"CONTINUA_SANDBOX": "0"}):
            plan = autoresident.build_plan(cfg, "t1.yaml", "hash1",
                                           base_dir=self.base)
        self.assertEqual(plan["instance"], "t1")
        self.assertEqual(plan["collection"], "t1_memories")
        self.assertTrue(plan["desk"]["path"].endswith(
            os.path.join("sandbox", "home", "t1")))
        self.assertEqual(len(plan["stores"]), 4)
        self.assertTrue(any("histories" in s and s.endswith("t1_yaml")
                            for s in plan["stores"]))
        self.assertEqual(plan["env"]["CONTINUA_SANDBOX"], "0")
        self.assertEqual(plan["env"]["CONTINUA_PAUSE_T1"], "(unset)")
        self.assertFalse(plan["wake"]["enabled"])
        self.assertFalse(plan["paused"])

    def test_plan_wake_and_pause(self):
        cfg = yaml.safe_load(_cfg_body(wake="true"))
        cfg["continua"]["paused"] = True
        plan = autoresident.build_plan(cfg, "t1.yaml", "h", base_dir=self.base)
        self.assertTrue(plan["wake"]["enabled"])
        self.assertTrue(plan["paused"])


class ProvisionTests(_Base):
    def test_stores_created_and_idempotent(self):
        r1 = autoresident.provision_stores("t1", "t1.yaml", base_dir=self.base)
        self.assertEqual(len(r1["created"]), 4)
        self.assertTrue(os.path.isdir(os.path.join(self.base, "agents", "t1")))
        self.assertTrue(os.path.isdir(os.path.join(self.base, "histories", "t1_yaml")))
        r2 = autoresident.provision_stores("t1", "t1.yaml", base_dir=self.base)
        self.assertEqual(r2["created"], [])
        self.assertEqual(len(r2["skipped"]), 4)
        self.assertEqual(r2["errors"], [])

    def test_desk_created_once_never_retouched(self):
        r1 = autoresident.provision_desk("t1", base_dir=self.base)
        self.assertEqual(r1["errors"], [])
        self.assertEqual(len(r1["created"]), 1)
        # the fresh desk got owner+mode; mkdir/chown/chmod all issued once
        mkdirs, chowns, chmods = (self.sudo.count("mkdir"),
                                  self.sudo.count("chown"), self.sudo.count("chmod"))
        self.assertEqual(mkdirs, 3)   # desk, tmp, job_logs
        self.assertEqual(chowns, 3)
        self.assertEqual(chmods, 3)
        # second run: desk exists → skipped, and NOTHING is re-touched
        r2 = autoresident.provision_desk("t1", base_dir=self.base)
        self.assertEqual(r2["created"], [])
        self.assertEqual(len(r2["skipped"]), 1)
        self.assertEqual(self.sudo.count("mkdir"), mkdirs)
        self.assertEqual(self.sudo.count("chown"), chowns)
        self.assertEqual(self.sudo.count("chmod"), chmods)

    def test_desk_bad_instance_name(self):
        r = autoresident.provision_desk("../evil", base_dir=self.base)
        self.assertFalse(r["ok"])
        self.assertEqual(self.sudo.calls, [])


class HandleTests(_Base):
    def test_end_to_end_success(self):
        with open(os.path.join(self.base, "configs", "t1.yaml"), "w") as f:
            f.write(_cfg_body())
        calls = []
        m = asyncio.run(autoresident.handle_new_config(
            "t1.yaml", autoresident._hash_file(
                os.path.join(self.base, "configs", "t1.yaml")),
            base_dir=self.base, bind_fn=_make_bind(calls)))
        self.assertEqual(m["outcome"], "activated")
        self.assertEqual(m["kind"], "activation")
        # the hot-add went through the bind path with raise_on_fail=False
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "t1.yaml")
        self.assertFalse(calls[0][2].get("raise_on_fail", True))
        self.assertTrue(m["stages"]["activate"]["bound"])
        # stores on disk
        self.assertTrue(os.path.isdir(os.path.join(self.base, "agents", "t1")))
        self.assertTrue(os.path.isdir(os.path.join(self.base, "wakes", "t1", "done")))
        self.assertTrue(os.path.isdir(os.path.join(self.base, "histories", "t1_yaml")))
        # verification ran, desk present (via the fake sudo)
        self.assertEqual(m["stages"]["verify"]["desk"], "present")
        self.assertIn("bot_task_alive", m["stages"]["verify"])
        # the manifest exists — the dry-run's replacement, written down
        manifests = glob.glob(os.path.join(self.base, "logs", "autoresident",
                                           "*-t1-activation.json"))
        self.assertEqual(len(manifests), 1)

    def test_validation_failure_never_binds(self):
        with open(os.path.join(self.base, "configs", "t1.yaml"), "w") as f:
            f.write(_cfg_body())
        other = ("residenta.yaml", yaml.safe_load(_cfg_body(instance="t1")))
        calls = []
        m = asyncio.run(autoresident.handle_new_config(
            "t1.yaml", "hash-does-not-matter", base_dir=self.base,
            bind_fn=_make_bind(calls), others=[other]))
        self.assertEqual(m["kind"], "failure")
        self.assertEqual(m["stage"], "validate")
        self.assertEqual(calls, [])
        self.assertTrue(glob.glob(os.path.join(self.base, "logs", "autoresident",
                                               "*-t1-failure.json")))

    def test_bind_failure_reported_not_fatal(self):
        with open(os.path.join(self.base, "configs", "t1.yaml"), "w") as f:
            f.write(_cfg_body())
        m = asyncio.run(autoresident.handle_new_config(
            "t1.yaml", "h", base_dir=self.base, bind_fn=_make_bind([], "none")))
        self.assertEqual(m["kind"], "failure")
        self.assertEqual(m["stage"], "activate")

    def test_yaml_parse_failure(self):
        with open(os.path.join(self.base, "configs", "t1.yaml"), "w") as f:
            f.write("app:\n  - [broken\n")
        m = asyncio.run(autoresident.handle_new_config(
            "t1.yaml", "h", base_dir=self.base, bind_fn=_make_bind([])))
        self.assertEqual(m["kind"], "failure")
        self.assertEqual(m["stage"], "validate")
        self.assertTrue(any("parse" in e for e in m["errors"]))


class ScanTests(_Base):
    def test_only_top_level_yaml_no_dotfiles(self):
        cfgdir = os.path.join(self.base, "configs")
        for name in ("a.yaml", ".hidden.yaml", "notes.txt"):
            with open(os.path.join(cfgdir, name), "w") as f:
                f.write("x: 1\n")
        os.makedirs(os.path.join(cfgdir, "subdir.yaml"))
        scanned = autoresident._scan_configs(cfgdir)
        self.assertEqual(sorted(scanned), ["a.yaml"])
        self.assertEqual(len(scanned["a.yaml"]), 64)


class WatchLoopTests(_Base):
    def _run(self, coro):
        return asyncio.run(coro)

    async def _scenario_new_file(self, calls, base):
        task = asyncio.create_task(autoresident.watch_loop(
            ["live.yaml"], interval_s=0.05, bind_fn=_make_bind(calls),
            base_dir=base, min_age_s=0.0))
        await asyncio.sleep(0.15)
        with open(os.path.join(base, "configs", "t1.yaml"), "w") as f:
            f.write(_cfg_body())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_new_file_onboarded_once(self):
        with open(os.path.join(self.base, "configs", "live.yaml"), "w") as f:
            f.write(_cfg_body(instance="live",
                              token="999999999:AA" + "y" * 30))
        calls = []
        self._run(self._scenario_new_file(calls, self.base))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "t1.yaml")
        manifests = glob.glob(os.path.join(self.base, "logs", "autoresident",
                                           "*-t1-activation.json"))
        self.assertEqual(len(manifests), 1)

    async def _scenario_failed_not_retried(self, base):
        task = asyncio.create_task(autoresident.watch_loop(
            [], interval_s=0.05, bind_fn=_make_bind([]),
            base_dir=base, min_age_s=0.0))
        await asyncio.sleep(0.1)
        with open(os.path.join(base, "configs", "t1.yaml"), "w") as f:
            f.write(_cfg_body(token="bad"))  # fails validation
        await asyncio.sleep(0.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_failed_candidate_not_retried_every_poll(self):
        self._run(self._scenario_failed_not_retried(self.base))
        failures = glob.glob(os.path.join(self.base, "logs", "autoresident",
                                          "*-t1-failure.json"))
        self.assertEqual(len(failures), 1)  # reported ONCE, then quiet

    async def _scenario_changed_live(self, base):
        task = asyncio.create_task(autoresident.watch_loop(
            ["live.yaml"], interval_s=0.05, bind_fn=_make_bind([]),
            base_dir=base, min_age_s=0.0))
        await asyncio.sleep(0.15)
        with open(os.path.join(base, "configs", "live.yaml"), "a") as f:
            f.write("# a live-resident edit\n")
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_changed_live_config_recorded_not_rebound(self):
        with open(os.path.join(self.base, "configs", "live.yaml"), "w") as f:
            f.write(_cfg_body(instance="live",
                              token="999999999:AA" + "y" * 30))
        calls = []
        self._run(self._scenario_changed_live(self.base))
        self.assertEqual(calls, [])  # never rebound
        changed = glob.glob(os.path.join(self.base, "logs", "autoresident",
                                         "*-live-changed.json"))
        self.assertEqual(len(changed), 1)
        import json
        with open(changed[0]) as f:
            payload = json.load(f)
        self.assertIn("not hot-reloaded", payload["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
