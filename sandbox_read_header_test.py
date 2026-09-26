"""approved 2026-09-21 (residentb's 'perception boundary'): every
sandbox_read now opens with an honest-bounds header — where the page
starts and ends, the file's real length, the next offset. The Ghost-
Message incident (a belief built on an invisible truncation) cannot
recur silently: the boundary is a map, not a void."""
import os
import subprocess
import sys
import tempfile
import unittest

from core import _SANDBOX_READ_CODE


class SandboxReadHeaderTests(unittest.TestCase):
    def _read(self, path, start="0"):
        r = subprocess.run([sys.executable, "-c", _SANDBOX_READ_CODE,
                            path, start], capture_output=True, text=True)
        return r.stdout

    def test_small_file_says_full(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("short ledger\n")
        self.addCleanup(os.unlink, f.name)
        out = self._read(f.name)
        self.assertIn("[full file shown -- 13 chars]", out)
        self.assertIn("short ledger", out)

    def test_long_file_shows_bounds_and_next_offset(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("x" * 103412)
        self.addCleanup(os.unlink, f.name)
        out = self._read(f.name)
        self.assertIn("[chars 0-20000 of 103412 | page 1 of 6 | next: start=20000]", out)
        self.assertNotIn("(end of file)", out)

    def test_offset_page_and_end_marker(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("y" * 25000)
        self.addCleanup(os.unlink, f.name)
        out = self._read(f.name, start="20000")
        self.assertIn("[chars 20000-25000 of 25000 | page 2 of 2 | (end of file)]", out)

    def test_missing_file_fails_honestly(self):
        out = self._read("/nonexistent/nothing.md")
        self.assertIn("[read failed:", out)


if __name__ == "__main__":
    unittest.main()
