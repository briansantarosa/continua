"""Agent-free coverage tests: only temporary fixtures, no model or network."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import recollections as r
import recollections_coverage as c

AT = "2026-09-17T12:00:00+00:00"


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="coverage-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.src, self.root = self.base / "chronicle", self.base / "stores"

    def row(self, person, role, content, ts="2026-09-16T10:01:00+00:00", cut=False):
        path = self.src / "residentb" / person / "2026-09-16.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(instance="residentb", person_id=person, role=role, content=content,
                   ts=ts, length_cut=cut)
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return r.source_record(path, row, "residentb", self.src)

    def inspect(self):
        # These must never be needed by a report.
        with patch.object(r.Store, "__init__", side_effect=AssertionError("write path")), \
             patch.object(r.LocalModel, "__call__", side_effect=AssertionError("model")):
            return c.inspect_resident("residentb", self.src, self.root, AT)

    def test_partition_and_scanner_parity(self):
        self.row("1", "user", "hello")
        self.row("1", "assistant", "hello again")
        self.row("1", "assistant", "hello again")  # exact duplicate ref
        self.row("2", "user", "x" * 8000)
        self.row("2", "assistant", "y" * 8001, cut=True)
        self.row("3", "user", "one-sided")
        self.row("4", "user", "open", ts=AT)
        self.row("system-wake", "assistant", "my wake")
        self.row("continua:ritual", "assistant", "my ritual")
        report = self.inspect()
        s = report["sources"]
        self.assertEqual(s["1"]["captured"]["duplicate_ref_rows"], 1)
        # chunk-2 planner: complete exchanges eligible even at equal ts
        self.assertEqual(s["1"]["scanner"]["eligible"]["refs"], 2)
        self.assertEqual(
            s["2"]["scanner"]["length_cut_excluded"]["chars"], 8001)
        self.assertIn("incomplete_unanswered", s["3"]["scanner"])
        self.assertIn("not_closed", s["4"]["scanner"])
        self.assertEqual(s["system-wake"]["scanner"]["eligible"]["refs"], 1)
        self.assertEqual(
            s["continua:ritual"]["scanner"]["eligible"]["refs"], 1)
        for source in s.values():
            for partition in ("scanner", "memory"):
                for unit in ("refs", "chars"):
                    self.assertEqual(source["unique"][unit], sum(v[unit] for v in source[partition].values()))
        # Compare current scan on fixtures only, using a temporary writable Store.
        store = r.Store("residentb", self.root)
        scan = r.scan(store, self.src, limit=1000, at=AT)
        self.assertEqual(scan["queued"], 3)  # person-1 + wake + ritual episodes
        self.assertEqual(scan["oversize_single"], 0)
        self.assertEqual(scan["length_cut_skipped"], 1)
        # person-3's one-sided user + person-2's user left unanswered after its
        # cut reply was excluded — both counted, neither enqueued
        self.assertEqual(scan["incomplete_exchanges"], 2)

    def test_store_states_and_no_mutation(self):
        store = r.Store("residentb", self.root)
        for person, state in [("1", "accepted"), ("2", "pending"), ("3", "quarantined"),
                              ("4", "accepted_no_review")]:
            sources = [self.row(person, "user", "said"), self.row(person, "assistant", "replied")]
            job = store.enqueue(sources)
            if state.startswith("accepted"):
                store.accept(job, dict(job=job, instance="residentb", sources=sources,
                                       review={"pass": state == "accepted"}))
            elif state == "quarantined":
                with store.db() as db:
                    db.execute("UPDATE jobs SET status='quarantined' WHERE id=?", (job,))
                store.audit(job, {"errors": ["verifier rejected"]})
        self.row("5", "assistant", "not enqueued")
        snapshot = lambda: {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                     p.stat().st_mtime_ns, p.stat().st_mode)
                            for p in self.base.rglob("*") if p.is_file()}
        before = snapshot()
        report = self.inspect()
        self.assertEqual(before, snapshot())
        s = report["sources"]
        for person, state in [("1", "accepted_evidence"), ("2", "pending"),
                              ("3", "quarantined"), ("4", "accepted_job_without_verified_evidence"),
                              ("5", "not_enqueued")]:
            self.assertIn(state, s[person]["memory"])
        self.assertEqual(report["store"]["candidate_error_rows"], 1)
        self.assertEqual(report["store"]["accepted_evidence_refs"], 2)

    def test_repeated_text_is_not_a_duplicate_and_cut_not_enqueued(self):
        self.row("1", "user", "hi")
        self.row("1", "assistant", "same", cut=True)
        self.row("1", "assistant", "same", ts="2026-09-16T10:02:00+00:00")
        s = self.inspect()["sources"]["1"]
        self.assertEqual(s["unique"]["refs"], 3)
        self.assertEqual(s["captured"]["repeated_text_distinct_ref"], 1)
        # the cut reply is excluded and counted; the later uncut reply answers
        # the user row, so the exchange is complete and eligible
        self.assertIn("length_cut_excluded", s["scanner"])
        self.assertEqual(s["scanner"]["eligible"]["refs"], 2)

    def test_bad_rows_and_missing_store(self):
        self.row("1", "user", "ü")
        path = self.src / "residentb" / "1" / "2026-09-16.jsonl"
        with path.open("a") as f:
            f.write('null\n{"partial":\n')
            f.write(json.dumps(dict(instance="residenta", person_id="1", ts=AT,
                                   role="assistant", content="wrong resident")) + "\n")
        report = self.inspect()
        self.assertFalse(report["complete_read"])
        self.assertEqual(report["sources"]["1"]["captured"]["malformed_rows"], 3)
        self.assertEqual(report["sources"]["1"]["unique"]["chars"], 1)
        self.assertFalse(self.root.exists())
        self.assertEqual(report["store"]["state"], "missing")
        self.assertNotIn("wrong resident", json.dumps(report))

    def test_corrupt_store_is_unknown_not_empty(self):
        self.row("1", "assistant", "hello")
        directory = self.root / "residentb"
        directory.mkdir(parents=True)
        (directory / "shadow.sqlite3").write_text("not sqlite")
        report = self.inspect()
        self.assertEqual(report["store"]["state"], "unavailable")
        self.assertIn("unknown", report["sources"]["1"]["memory"])
        self.assertFalse(report["complete_read"])

    def test_missing_capture_and_namespace_guard(self):
        report = self.inspect()
        self.assertEqual(report["source_state"], "missing")
        self.assertFalse(report["complete_read"])
        self.assertFalse(self.src.exists())
        with self.assertRaises(ValueError):
            c.inspect_resident("../residenta", self.src, self.root, AT)

    def test_schema_error_and_unmatched_refs(self):
        store = r.Store("residentb", self.root)
        sources = [self.row("1", "assistant", "hello")]
        job = store.enqueue(sources)
        store.accept(job, {"job": job, "instance": "residenta", "sources": sources, "review": {"pass": True}})
        self.assertEqual(self.inspect()["store"]["accepted_evidence_refs"], 0)
        with store.db() as db:
            db.execute("INSERT INTO coverage VALUES('unmatched','absent')")
            db.execute("INSERT INTO revisions VALUES(?,2,'null','today')", (job,))
        report = self.inspect()
        self.assertEqual(report["store"]["coverage_refs_not_in_valid_capture"], 1)
        self.assertIn("invalid_revision", report["store"]["issues"])
        self.assertFalse(report["complete_read"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
