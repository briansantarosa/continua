"""anchor_handle_test.py — memorybuild.md Phase 4 (S3): handles and anchors
that work. Tmp store (synthetic accepted episodes); production untouched.
Run: python3 anchor_handle_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core              # noqa: E402
import recollections as rec   # noqa: E402
import chronicle as ch   # noqa: E402


def _mk_core(root):
    c = object.__new__(core.SagentCore)
    c.instance_id = "t"
    c.config = {}

    def store():
        return rec.Store("t", root=root)
    c._recollection_store = store
    return c


def _seed(root, n=3, shared_prefix=None, salt=""):
    """Seed n accepted episodes; if shared_prefix, force the ids to share
    it (append unique suffixes via distinct sources)."""
    store = rec.Store("t", root=root)
    jobs = []
    for i in range(n):
        k = f"{salt}{i}"
        src = [{"instance": "t", "person_id": "111", "role": "user",
                "content": f"memory number {k} " + "detail " * 20,
                "ts": f"2026-09-{20+(i%8):02d}T10:00:00-07:00",
                "uid": f"uid-{k}", "ref": f"ref-{k}", "hash": f"h-{k}",
                "path": f"residentb/111/2026-09-{20+(i%8):02d}.jsonl"}]
        job = store.enqueue(src)
        if job is None:
            continue   # already covered (a re-seeded salt) — skip
        store.accept(job, {"job": job,
                           "text": f"the memory text {k} " + "more " * 15,
                           "sources": src, "visibility": ["111"],
                           "event_start": src[0]["ts"],
                           "event_end": src[0]["ts"],
                           "review": {"pass": True}})
        jobs.append(job)
    if shared_prefix:
        # force id collisions: rewrite the job ids to share the prefix
        import sqlite3
        db = sqlite3.connect(os.path.join(root, "t", "shadow.sqlite3"))
        newmap = {}
        for i, j in enumerate(jobs):
            new = shared_prefix + j[:64 - len(shared_prefix)]
            if new == j:
                continue
            for table, cols in (("jobs", "id"), ("revisions", "job"),
                                ("candidates", "job"), ("coverage", "job")):
                db.execute(f"UPDATE {table} SET {cols}=? WHERE {cols}=?",
                           (new, j))
            newmap[j] = new
        db.commit(); db.close()
        jobs = [newmap.get(j, j) for j in jobs]
    return jobs


class HandleResolutionTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="anchors3-")
        self.root = self.td
        self.jobs = _seed(self.root, 3)
        self.core = _mk_core(self.root)

    def test_full_id_anchors(self):
        out = self.core._tool_anchor_memory({"job_id": self.jobs[0]})
        self.assertIn("Anchored", out)
        anchors, _ = rec.read_guards("t", root=self.root)
        self.assertIn(self.jobs[0], anchors)

    def test_handle_prefix_anchors(self):
        out = self.core._tool_anchor_memory({"job_id": "#" + self.jobs[1][:6]})
        self.assertIn("Anchored", out)
        anchors, _ = rec.read_guards("t", root=self.root)
        self.assertIn(self.jobs[1], anchors)

    def test_hash_stripped_and_longer_prefix(self):
        out = self.core._tool_anchor_memory(
            {"job_id": "#" + self.jobs[2][:12]})
        self.assertIn("Anchored", out)

    def test_ambiguous_lists_candidates(self):
        jobs = _seed(self.root, 3, salt="B")   # fresh sources, new ids
        # force TWO of them to share the first 6 chars with jobs[0]
        import sqlite3 as _sq
        db = _sq.connect(os.path.join(self.root, "t", "shadow.sqlite3"))
        target = self.jobs[0][:6]
        for j in jobs[:2]:
            new = target + j[6:]
            for table, col in (("jobs", "id"), ("revisions", "job"),
                               ("candidates", "job"), ("coverage", "job")):
                db.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, j))
            # the BODY carries its own job key (production bodies do too)
            db.execute("UPDATE revisions SET body=REPLACE(body,?,?) "
                       "WHERE job=?", (j, new, new))
        db.commit(); db.close()
        out = self.core._tool_anchor_memory({"job_id": self.jobs[0][:6]})
        self.assertIn("Several memories share", out)
        self.assertIn("#", out)             # candidate handles shown
        self.assertIn("longer prefix", out)
        anchors, _ = rec.read_guards("t", root=self.root)
        self.assertEqual(anchors, set())    # nothing anchored on ambiguity

    def test_unknown_handle_teaches(self):
        out = self.core._tool_anchor_memory({"job_id": "#zzzzzz"})
        self.assertIn("Could not anchor", out)
        self.assertIn("no memory carries the handle", out)
        anchors, _ = rec.read_guards("t", root=self.root)
        self.assertEqual(anchors, set())

    def test_short_ref_refused(self):
        out = self.core._tool_anchor_memory({"job_id": "#ab"})
        self.assertIn("Could not anchor", out)


class WhyTests(unittest.TestCase):
    def test_why_stored_and_read_back(self):
        td = tempfile.mkdtemp(prefix="anchorwhy-")
        jobs = _seed(td, 1)
        core = _mk_core(td)
        out = core._tool_anchor_memory(
            {"job_id": jobs[0], "why": "the morning we found the map"})
        self.assertIn("Your why is recorded with it", out)
        whys = rec.anchor_whys("t", root=td)
        self.assertEqual(whys.get(jobs[0]),
                         "the morning we found the map")

    def test_anchor_without_why_has_no_entry(self):
        td = tempfile.mkdtemp(prefix="anchorwhy2-")
        jobs = _seed(td, 1)
        core = _mk_core(td)
        core._tool_anchor_memory({"job_id": jobs[0]})
        self.assertEqual(rec.anchor_whys("t", root=td), {})

    def test_unanchor_by_handle(self):
        td = tempfile.mkdtemp(prefix="anchorun-")
        jobs = _seed(td, 1)
        core = _mk_core(td)
        core._tool_anchor_memory({"job_id": jobs[0]})
        out = core._tool_unanchor_memory({"job_id": "#" + jobs[0][:8]})
        self.assertIn("Anchor removed", out)
        anchors, _ = rec.read_guards("t", root=td)
        self.assertEqual(anchors, set())


class SchemaTests(unittest.TestCase):
    def test_schema_has_why_and_handle_mention(self):
        tools = core.build_tools({}) if hasattr(core, "build_tools") else None
        # the schemas live in core's tool assembly — find via the config path
        import inspect
        src = inspect.getsource(core)
        self.assertIn('"why"', src)
        self.assertIn("#handle", src)
        self.assertIn("#a3f9c2", src)  # the description's example

    def test_show_handles_key_in_yamls(self):
        import yaml
        for inst in ("residentb", "residenta"):
            cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
            rv = (((cfg.get("continua") or {}).get("ritual") or {})
                  .get("recollection_view") or {})
            # show_handles rides the memory.recollection_view per the build
            # file's config section; check both homes
            got = rv.get("show_handles") or \
                (((cfg.get("memory") or {}).get("recollection_view") or {})
                 .get("show_handles"))
            self.assertIsNotNone(got, inst)


class LegacyParityTests(unittest.TestCase):
    def test_write_essence_resolver_unchanged(self):
        td = tempfile.mkdtemp(prefix="anchorep-")
        jobs = _seed(td, 2)
        core = _mk_core(td)
        # the OLD resolver still returns the first prefix match
        self.assertEqual(core._resolve_episode(jobs[0][:8]), jobs[0])


if __name__ == "__main__":
    unittest.main()