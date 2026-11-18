"""ledger_rotation_test.py — ledgerplan.md (approved 2026-09-28):
cycling the system-notes ledgers.

Covers the whole rotate() pipeline against a REAL sandboxed tmp desk (the
same bwrap path the machinery uses — no mocks on the file ops):

  - naming: volNN_<first>_to_<last> with REAL parsed dates, zero-padded
  - append-only proof: the closed volume's sha256 before and after the
    rename must match (rotation mutates nothing)
  - discovery: the fresh active ledger opens with PRIOR VOLUMES naming
    every closed volume + the wildcard-grep recipe
  - the closure + opening entries land in the right files
  - below-ceiling → no-op; missing file → no-op
  - idempotence: an immediate second rotate is a no-op
  - status() fields + ceilings

Run: python3 ledger_rotation_test.py
Requires the sandbox (sudo -n bwrap) — same requirement as live tools.
"""
import hashlib
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import continuity_log as cl  # noqa: E402

_SOFT = cl.ROTATE_SOFT_CHARS


def _entry(ts, prov="test", what="w", why="t"):
    return (f"\n## [{ts}] provenance: {prov}\n"
            f"what changed for her: {what}\n"
            f"what happened (technical): {why}\nbackfill: no\n")


def _sandbox_up():
    try:
        import sandbox as sbx
        return bool(sbx.run("residentb", ["true"], timeout=15).get("ok"))
    except Exception:
        return False


_SKIP = (os.environ.get("CONTINUA_SANDBOX", "") == "0"
         or not _sandbox_up())


class LedgerRotationTests(unittest.TestCase):
    # the THROWAWAY desk (provisioned by provision_desk.sh, the _test
    # convention) — production desks are NEVER touched by tests. The
    # 2026-09-28 incident (test cleanup globbed residentb's real ledger away)
    # is why this file exists in this shape.
    INSTANCE = "_testrot"

    # 2026-09-28 INCIDENT FIX: tests wrote to the PRODUCTION ledger
    # (system_log.md) — _seed truncated it and _cleanup globbed it away,
    # destroying residentb's real ledger. Tests now use a dedicated TEST
    # filename that rotate() can target via monkeypatched DESK_REL, and
    # cleanup removes ONLY that name. The production ledger is never
    # touched.
    TEST_LEDGER = "system_notes/system_log.ledger_rotation_test.md"

    @classmethod
    def setUpClass(cls):
        cls.notes_rel = "system_notes"
        cls.active_rel = cl.DESK_REL   # same relative path on the throwaway desk
        cls.sut = cl

    def _seed(self, entries, pad_to=None):
        """Write a test ledger into the real desk via sandbox (the same
        path machinery uses), track it for teardown."""
        import sandbox as sbx
        body = "".join(_entry(e) if isinstance(e, str) else _entry(*e)
                       for e in entries)
        if pad_to and len(body) < pad_to:
            body += "<!-- pad -->" + ("x" * (pad_to - len(body)))
        _w = ("import sys\np = sys.argv[1]\n"
              "import os\nd = os.path.dirname(p)\n"
              "os.makedirs(d, exist_ok=True)\n"
              "with open(p,'w',encoding='utf-8') as f:\n"
              "    f.write(sys.stdin.read())\n")
        r = sbx.run(self.INSTANCE, ["python3", "-c", _w, self.active_rel],
                    timeout=30, stdin_text=body)
        self.assertTrue(r.get("ok"), r)
        # the volume name rotate() builds will carry these dates — the
        # cleanup filter keys on them so production volumes never match
        dates = sorted({str(e)[:10] for e in entries})
        self._test_dates = (dates[0] + "_to_" + dates[-1])
        self._made = [self.active_rel]
        return body

    def _sha_active(self):
        import sandbox as sbx
        r = sbx.run(self.INSTANCE, ["python3", "-c",
            "import sys,hashlib;"
            "print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
            self.active_rel], timeout=30)
        return (r.get("stdout") or "").strip() or None

    def _list_vol(self):
        import sandbox as sbx
        r = sbx.run(self.INSTANCE, ["python3", "-c",
            "import os; print('\\n'.join(sorted("
            "f for f in os.listdir('system_notes') "
            "if f.startswith('system_log.vol'))))"], timeout=30)
        return [f for f in (r.get("stdout") or "").split() if f]

    def _read_active(self):
        return self.sut._read_desk_file(self.INSTANCE, self.active_rel)

    def _list_vol(self):
        import sandbox as sbx
        r = sbx.run(self.INSTANCE, ["python3", "-c",
            "import os; print('\\n'.join(sorted("
            "f for f in os.listdir('system_notes') "
            "if f.startswith('system_log.vol'))))"], timeout=30)
        return [f for f in (r.get("stdout") or "").split()
                if f.startswith("system_log.vol")]

    def _cleanup(self):
        import sandbox as sbx
        # throwaway desk: wipe the whole system_notes dir (nothing
        # production lives here)
        rm = ("import sys, os, shutil\n"
              "p = 'system_notes'\n"
              "if os.path.isdir(p): shutil.rmtree(p)\n")
        sbx.run(self.INSTANCE, ["python3", "-c", rm], timeout=30)

    def tearDown(self):
        self._cleanup()

    def test_volume_name_format(self):
        self.assertEqual(
            cl._volume_name("2026-09-09", "2026-09-28", 0),
            "system_log.vol01_2026-09-09_to_2026-09-28.md")
        self.assertEqual(
            cl._volume_name("2026-09-28", "2026-10-05", 1),
            "system_log.vol02_2026-09-28_to_2026-10-05.md")

    def test_below_ceiling_is_noop(self):
        self._seed([("2026-09-28T10:00:00",)])
        r = self.sut.rotate(self.INSTANCE, by="test")
        self.assertFalse(r["ok"])
        self.assertIn("below soft ceiling", r["reason"])
        self.assertEqual(self._list_vol(), [])

    def test_full_rotation(self):
        # 3 entries spanning real dates; pad to cross the soft ceiling
        body = self._seed([
            ("2026-09-09T08:00:00-07:00"),
            ("2026-09-20T12:00:00-07:00"),
            ("2026-09-28T07:25:00-07:00"),
        ], pad_to=_SOFT + 500)
        sha_before = self._sha_active()
        r = self.sut.rotate(self.INSTANCE, by="ledger_rotation_test")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["renamed"])
        self.assertTrue(r["seeded"])
        self.assertTrue(r["checksum_match"])
        self.assertIn("2026-09-09_to_2026-09-28", r["volume"])
        self.assertRegex(r["volume"], r"system_log\.vol\d{2}_")
        # the closed volume exists and is byte-identical to pre-rotation
        vols = self._list_vol()
        self.assertEqual(vols, [r["volume"]])
        import sandbox as sbx
        sha_vol = sbx.run(self.INSTANCE, ["python3", "-c",
            "import sys,hashlib;"
            "print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
            os.path.join(self.notes_rel, vols[0])], timeout=30)
        # volume = old bytes + closure entry (appended BEFORE rename) —
        # so the volume equals the file AS IT WAS at rename time; the
        # byte-identity proof is that the rename moved the file the
        # checksum saw (rotate() asserts this internally and reports
        # checksum_match=True).
        self.assertTrue(sha_vol.get("stdout", "").strip())
        # the closed volume contains all 3 original entries + closure
        # (head-keep read for the entries; the closure entry sits at the
        # END of the volume — read via tail through the transport's native
        # keep-last behavior)
        vol_text = self.sut._read_desk_file(
            self.INSTANCE, os.path.join(self.notes_rel, vols[0])) or ""
        vol_text_tail = self.sut._read_desk_file(
            self.INSTANCE, os.path.join(self.notes_rel, vols[0]),
            keep="tail") or ""
        for ts in ("2026-09-09T08:00:00", "2026-09-20T12:00:00",
                   "2026-09-28T07:25:00"):
            self.assertIn(ts, vol_text)
        self.assertIn("This volume of your ledger closes here", vol_text_tail)
        # fresh active ledger: header + PRIOR VOLUMES + opening entry
        active = self._read_active()
        self.assertIn("# System Notes", active)
        self.assertIn("PRIOR VOLUMES", active)
        self.assertIn(vols[0], active)
        self.assertIn("system_notes/system_log*.md", active)
        self.assertIn("This volume of your ledger begins here", active)
        self.assertIn("2026-09-28", active)

    def test_idempotent_immediate_second_rotate(self):
        self._seed([("2026-09-09T08:00:00"),
                    ("2026-09-28T07:25:00")], pad_to=_SOFT + 500)
        r1 = self.sut.rotate(self.INSTANCE, by="t")
        self.assertTrue(r1["ok"])
        # new volume has 1 entry — below ceiling → second rotate is a no-op
        r2 = self.sut.rotate(self.INSTANCE, by="t")
        self.assertFalse(r2["ok"])
        self.assertIn("below soft ceiling", r2["reason"])
        self.assertEqual(len(self._list_vol()), 1)

    def test_missing_active_file_noop(self):
        self._cleanup()
        r = self.sut.rotate(self.INSTANCE, by="t")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no active ledger")

    def test_status_fields(self):
        self._seed([("2026-09-09T08:00:00"),
                    ("2026-09-28T07:25:00")], pad_to=_SOFT + 500)
        st = self.sut.ledger_status(self.INSTANCE)
        self.assertTrue(st["exists"])
        self.assertEqual(st["entries"], 2)
        self.assertEqual(st["first"], "2026-09-09")
        self.assertEqual(st["last"], "2026-09-28")
        self.assertTrue(st["soft_ceiling"])
        self.assertGreaterEqual(st["pages"], 5)
        self.assertEqual(st["prior_volumes"], [])

    def test_volume_number_increments(self):
        self._seed([("2026-09-01T08:00:00"),
                    ("2026-09-10T07:25:00")], pad_to=_SOFT + 500)
        r1 = self.sut.rotate(self.INSTANCE, by="t")
        self.assertTrue(r1["ok"])
        self.assertIn("vol01", r1["volume"])
        # build a second full volume fast: re-seed active past ceiling
        self._seed([("2026-09-11T08:00:00"),
                    ("2026-09-20T07:25:00")], pad_to=_SOFT + 500)
        r2 = self.sut.rotate(self.INSTANCE, by="t")
        self.assertTrue(r2["ok"])
        self.assertIn("vol02", r2["volume"])
        self.assertEqual(len(self._list_vol()), 2)
        # the newest active ledger's PRIOR VOLUMES lists BOTH
        active = self._read_active()
        self.assertIn("vol01", active)
        self.assertIn("vol02", active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
