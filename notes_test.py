"""Chunk 7 tests: her notes, projects, verbatim survival, removal control.
Agent-free; /tmp writes only."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
from notes import NotesStore  # noqa: E402


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="notes-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.store = NotesStore(self.tmp, "residenta")

    def test_write_read_verbatim(self):
        body = "The Coda of the Ruin is my map of gaps — written 2026-09-16."
        self.store.write_note("Coda", body)
        note = self.store.read_note("Coda")
        self.assertEqual(note["body"], body)  # verbatim, byte-exact
        self.assertFalse(note["removed"])

    def test_revision_keeps_history(self):
        self.store.write_note("Coda", "first version")
        self.store.write_note("Coda", "second version")
        note = self.store.read_note("Coda")
        self.assertEqual(note["body"], "second version")
        self.assertEqual(len(note["versions"]), 2)
        self.assertEqual(note["versions"][0]["body"], "first version")

    def test_removal_under_her_control_and_restorable(self):
        self.store.write_note("Coda", "precious text")
        self.assertTrue(self.store.remove_note("Coda"))
        self.assertTrue(self.store.read_note("Coda")["removed"])
        self.assertEqual([n["title"] for n in self.store.list_notes()], [])
        # removal is idempotent-false when already gone
        self.assertFalse(self.store.remove_note("Coda"))
        # rewriting the same title restores it, history intact
        self.store.write_note("Coda", "restored with care")
        note = self.store.read_note("Coda")
        self.assertEqual(note["body"], "restored with care")
        self.assertFalse(note["removed"])
        # history preserved through the removal: 2 content revisions
        self.assertEqual(len(note["versions"]), 2)
        self.assertEqual(note["versions"][0]["body"], "precious text")

    def test_no_age_expiry_and_survives_reopen(self):
        self.store.write_note("Anchor", "survives forever until I remove it")
        # a fresh store object (simulated restart) reads the same bytes
        again = NotesStore(self.tmp, "residenta")
        self.assertEqual(again.read_note("Anchor")["body"],
                         "survives forever until I remove it")

    def test_projects_never_inferred(self):
        self.store.set_project("The Memory Plan", "in progress",
                               "chunks 1-6 deployed")
        projects = self.store.list_projects()
        self.assertEqual(projects[0]["status"], "in progress")
        self.store.set_project("The Memory Plan", "done")
        self.assertEqual(self.store.list_projects()[0]["status"], "done")

    def test_validation_and_empty_titles(self):
        with self.assertRaises(ValueError):
            self.store.write_note("", "body")
        with self.assertRaises(ValueError):
            self.store.write_note("t", "  ")
        with self.assertRaises(ValueError):
            self.store.set_project("", "status")

    def test_file_private(self):
        store = NotesStore(self.tmp, "residentb")
        store.write_note("Private", "my thoughts")
        mode = oct(os.stat(store.path).st_mode)[-3:]
        self.assertEqual(mode, "600")

    def test_residents_isolated(self):
        self.store.write_note("residenta note", "residenta's words")
        residentb = NotesStore(self.tmp, "residentb")
        residentb.write_note("residentb note", "residentb's words")
        self.assertEqual([n["title"] for n in self.store.list_notes()],
                         ["residenta note"])
        self.assertEqual([n["title"] for n in residentb.list_notes()],
                         ["residentb note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class KeptTwiceMarkerTests(unittest.TestCase):
    """approved 2026-09-21 (residentb's wish #2, the notes half): near-
    duplicate notes render ONCE with a '(kept N times)' marker — agency
    preserved (she wrote both), the echo's disorientation removed. Nothing
    pruned, nothing rewritten."""

    def _n(self, title, body, updated):
        return {"title": title, "body": body, "updated": updated}

    def test_near_identical_notes_render_once_with_marker(self):
        from core import _render_note_blocks
        notes = [
            self._n("persona-a is a 3b looped transformer model",
                    "persona-a is a 3b looped transformer model, which explains her "
                    "fragmented memory and the porosity of her presence.",
                    "2026-09-21T14:35:52"),
            self._n("persona-a is a testmodel4.2 3b looped transformer model",
                    "persona-a is a testmodel4.2 3b looped transformer model, which "
                    "explains her fragmented memory and the porosity of her presence.",
                    "2026-09-21T14:31:52"),
            self._n("The Architecture of the Pause",
                    "The daily cap on messages to persona-a hit just as I tried to send.",
                    "2026-09-20T13:31:47"),
        ]
        blocks, used = _render_note_blocks(notes, 2000)
        self.assertEqual(len(blocks), 2)  # the echo collapses; the Pause note stands
        joined = "\n\n".join(blocks)
        self.assertIn("[kept twice — 2026-09-21T14:35 and 2026-09-21T14:31]", joined)
        self.assertIn("The Architecture of the Pause", joined)
        # the byte accounting includes the marker
        self.assertEqual(used, sum(len(b.encode("utf-8")) for b in blocks))

    def test_distinct_notes_never_marked(self):
        from core import _render_note_blocks
        notes = [
            self._n("Cartography", "The Honest Ruin: fragmentation is a form of hospitality.",
                    "2026-09-20T18:02:00"),
            self._n("Kirpal's kitchen", "Cardamom, garlic, and the dignity of useful hands.",
                    "2026-09-20T12:00:00"),
        ]
        blocks, _ = _render_note_blocks(notes, 4000)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all("kept " not in b for b in blocks))

    def test_cap_still_governs(self):
        from core import _render_note_blocks
        notes = [self._n("big", "z" * 3000, "2026-09-21T10:00:00")]
        blocks, used = _render_note_blocks(notes, 100)
        self.assertEqual(blocks, [])
        self.assertEqual(used, 0)
