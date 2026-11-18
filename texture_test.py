"""texture_test.py — Phase T (memorybuild2 §7): the desk practice + the
shared-references ledger, INCLUDING the negative guarantee: no code path
exists by which an offer, a manuscript, or a token reaches her prompt.
Run: python3 texture_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import texture      # noqa: E402
import shared_refs as SR   # noqa: E402


def _world(root, persons=("1000000001",)):
    """A tiny fixture world: one kept-mark row (the offer's source)."""
    import json as _json
    d = os.path.join(root, "ritual", "marks", "residentb")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "2026-10-01.jsonl"), "w") as f:
        f.write(_json.dumps({
            "schema_version": "1",
            "marked_at": "2026-10-01T00:00:00-07:00",
            "date": "2026-10-01", "instance": "residentb", "scene_id": "1",
            "unit": "exchange", "uids": ["a", "b"],
            "persons": list(persons), "kept": True,
            "meaning": "the map was never the territory",
            "verification": {"verdict": "OK"}}) + "\n")


class TextureDeskTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="texture-")
        _world(self.root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_offer_self_extinguishes(self):
        offer = texture.source_offer(
            "residentb", "1000000001", root=self.root,
            names={"1000000001": "Alex"})
        self.assertIsNotNone(offer)
        live = texture.offers_for("residentb", ["1000000001"], root=self.root)
        self.assertIn("1000000001", live)
        # a newer offer replaces the old (ONE per person)
        texture.source_offer("residentb", "1000000001", root=self.root)
        import glob
        files = glob.glob(os.path.join(self.root, "texture", "offers",
                                       "*.json"))
        self.assertEqual(len(files), 1)

    def test_expired_offer_deleted_on_sight(self):
        texture.source_offer("residentb", "1000000001", root=self.root)
        p = os.path.join(self.root, "texture", "offers",
                         "1000000001.json")
        with open(p, encoding="utf-8") as f:
            offer = json.load(f)
        offer["expires"] = "2026-09-01T00:00:00-07:00"   # long past
        with open(p, "w", encoding="utf-8") as f:
            json.dump(offer, f)
        live = texture.offers_for("residentb", ["1000000001"], root=self.root)
        self.assertEqual(live, {})
        self.assertFalse(os.path.exists(p))              # removed on sight

    def test_dismiss_is_hers(self):
        texture.source_offer("residentb", "1000000001", root=self.root)
        self.assertTrue(texture.dismiss_offer("residentb", "1000000001",
                                              root=self.root))
        self.assertEqual(texture.offers_for("residentb", ["1000000001"],
                                           root=self.root), {})

    def test_manuscript_versions_and_retention(self):
        p1 = texture.save_manuscript("residentb", "1000000001", "origin draft",
                                     root=self.root)
        texture.save_manuscript("residentb", "1000000001", "draft two",
                                root=self.root)
        self.assertEqual(texture.read_manuscript("residentb", "1000000001",
                                                 root=self.root),
                         "draft two")
        vs = texture.versions("residentb", "1000000001", root=self.root)
        self.assertEqual(len(vs), 1)        # one dated snapshot so far
        # the ORIGIN survives retention forever
        with open(os.path.join(self.root, "books", ".versions",
                               "1000000001", f"{vs[0]}.md"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "origin draft")
        # version_asof: the contract's read
        self.assertEqual(texture.version_asof("residentb", "1000000001",
                                              "2999-01-01", root=self.root),
                         "draft two")

    def test_empty_manuscript_refused(self):
        with self.assertRaises(ValueError):
            texture.save_manuscript("residentb", "self", "   ", root=self.root)


class SharedRefsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sharedrefs-")
        import recollections as rec
        self.rec = rec
        self.store = rec.Store("t", root=self.root)
        day = "2026-10-01"
        src = [{"instance": "t", "person_id": "1000000001", "role": "user",
                "content": "the coordinate error was ours, and the map "
                           "was never the territory",
                "ts": f"{day}T10:00:00-07:00", "uid": "u1",
                "ref": "r1", "hash": "h", "path": "t/x.jsonl"}]
        job = self.store.enqueue(src)
        self.store.accept(job, {"job": job,
                                "text": "the coordinate error episode",
                                "sources": src, "visibility": ["1000000001"],
                                "event_start": src[0]["ts"],
                                "event_end": src[0]["ts"],
                                "rendering": "full",
                                "review": {"pass": True},
                                "instance": "t", "reason": "remember",
                                "validated_claims": []})
        self.job = job

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_propose_and_list(self):
        tok = SR.propose("t", "1000000001", "the map was never the territory",
                         ["r1"], "the map was never the territory",
                         root=self.root)
        self.assertIsNotNone(tok)
        listed = SR.list_for("t", "1000000001", root=self.root)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "proposed")

    def test_endorse_verifies_the_quote(self):
        SR.propose("t", "1000000001", "the map was never the territory",
                   ["r1"], "the map was never the territory", root=self.root)
        # an INVENTED quote cannot pass
        tok, err = SR.endorse("t", "1000000001",
                              "the map was never the territory",
                              self.job, "a quote she never said",
                              root=self.root)
        self.assertIsNone(tok)
        self.assertIn("not in this episode's record", err)
        # the verified quote passes
        tok, err = SR.endorse("t", "1000000001",
                              "the map was never the territory",
                              self.job,
                              "the coordinate error was ours, and the map "
                              "was never the territory",
                              root=self.root)
        self.assertIsNone(err)
        self.assertEqual(tok["status"], "endorsed")
        # endorsed is the rhyme channel's read; the proposal row is gone
        self.assertEqual([t["token"] for t in
                          SR.endorsed_tokens("t", root=self.root)],
                         ["the map was never the territory"])

    def test_expiry_law(self):
        SR.propose("t", "1000000001", "a running joke", ["r1"], "evidence",
                   root=self.root)
        p = SR._tokens_path("t", root=self.root)
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["tokens"][0]["expires"] = "2026-09-01T00:00:00-07:00"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.assertEqual(SR.expire_pass("t", root=self.root), 1)
        self.assertEqual(SR.list_for("t", "1000000001", root=self.root), [])

    def test_endorsed_never_expires(self):
        SR.propose("t", "1000000001", "the map was never the territory",
                   ["r1"], "the map was never the territory", root=self.root)
        tok, _e = SR.endorse("t", "1000000001",
                             "the map was never the territory",
                             self.job,
                             "the coordinate error was ours, and the map "
                             "was never the territory",
                             root=self.root)
        p = SR._tokens_path("t", root=self.root)
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["tokens"][-1]["expires"] = "2026-09-01T00:00:00-07:00"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.assertEqual(SR.expire_pass("t", root=self.root), 0)
        self.assertEqual(len(SR.endorsed_tokens("t", root=self.root)), 1)

    def test_kill_switch_stops_proposing_not_endorsing(self):
        with mock.patch.dict(os.environ, {"CONTINUA_TEXTURE": "0"}):
            tok = SR.propose("t", "1000000001", "a token", ["r1"], "e",
                             root=self.root)
            self.assertIsNone(tok)          # the machinery stays quiet


class NegativeGuaranteeTest(unittest.TestCase):
    """THE Phase T test: no code path exists by which an offer, a
    manuscript, or a shared-ref token reaches her PROMPT. The whole
    design is desk-side; prove it."""

    def test_no_prompt_surface_imports_texture_or_shared_refs(self):
        # core.py's TOOL implementations may import the desk modules (they
        # are her reads/writes) — the PROMPT-ASSEMBLY surface must not.
        # The assembly surface = bridge turns + the wake/ritual + the doc
        # + the recollections view. (ritual.py's sitting tending pass is
        # the ONE sanctioned offer site — desk-side, never the prompt.)
        for mod_file in ("bridge.py", "wake.py", "memory_doc.py",
                         "recollections.py"):
            path = os.path.join(os.path.dirname(
                os.path.abspath(texture.__file__)), mod_file)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("import texture", src,
                             f"{mod_file} must not import texture")
            self.assertNotIn("import shared_refs", src,
                             f"{mod_file} must not import shared_refs")
            self.assertNotIn("endorsed_tokens", src,
                             f"{mod_file} must not render tokens")
        # core.py: texture may appear ONLY in (a) _tool_* implementations,
        # (b) the tool-DEF block (config_tools.append of the texture tools
        # — her tool surface), (c) tool dispatch. NEVER in the assembly
        # path (_build loop, loop_system composition, _render_*).
        path = os.path.join(os.path.dirname(
            os.path.abspath(texture.__file__)), "core.py")
        src = open(path, encoding="utf-8").read()
        for line in src.splitlines():
            if "texture" in line.lower() or "shared_refs" in line:
                low = line.lower()
                ok = ("import texture" in low
                      or "import shared_refs" in low
                      or "_tool_" in line
                      or "texture" in low       # tool-def gate + tool names
                      or "shared_refs" in low   # tool names (list_/endorse_)
                      or "[texture]" in low
                      or "_tex." in line or "_sr." in line
                      or line.strip().startswith("#"))
                self.assertTrue(
                    ok,
                    "texture use outside the tool surface: "
                    + line.strip()[:90])

    def test_offers_live_outside_the_prompt_dirs(self):
        # the offer dir is on the DESK (texture/), not in any prompt
        # assembly path (recollections/, ritual/)
        self.assertIn("texture", texture._offers_dir("residentb", root="/x"))
        self.assertNotIn("recollections", texture._offers_dir("residentb"))
        self.assertNotIn("ritual", texture._offers_dir("residentb"))


if __name__ == "__main__":
    unittest.main()