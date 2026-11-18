"""allontape_test.py — agent-free tests for allontape v2 (the designer go +
residentb endorsement, 2026-10-05): the desk git store, the mechanical mirror,
the witness stream, milestone keeps, the cue. REAL git in a tmp desk; the
sandbox runner executes argv directly in the tmp desk (no bwrap); the
tape is a caught list. All writes under /tmp.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CONTINUA_RECOLLECTIONS"] = "1"

import allontape as at  # noqa: E402
import recollections as r  # noqa: E402

tmp = tempfile.mkdtemp(prefix="allontape-test-")
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


DESK = Path(tmp) / "desk"
DESK.mkdir(parents=True)

# the fake sandbox runner: execute argv in the tmp desk, no bwrap
def fake_run(instance, argv, timeout=None, stdin_text=None):
    p = subprocess.run(argv, cwd=str(DESK), capture_output=True,
                       text=True, timeout=timeout,
                       input=stdin_text if stdin_text is not None else None)
    return {"ok": p.returncode == 0, "exit": p.returncode,
            "stdout": p.stdout, "stderr": p.stderr, "argv": argv}


at.sb.run = fake_run
at.STATE_DIR = Path(tmp) / "state"   # no cross-run state leakage
tape = []
at._append_row = lambda inst, content, ts, root=None: (
    tape.append({"content": content, "ts": ts}) or {"ok": True})

CFG_ON = {"continua": {"allontape": {"enabled": True}}}

# --- 1. the gates
os.environ.pop(at.KILL_SWITCH_ENV, None)
check("gate off → disabled", at.mirror("g", cfg={})["status"] == "disabled")
os.environ[at.KILL_SWITCH_ENV] = "0"
check("env master wins", at.mirror("g", cfg=CFG_ON)["status"] == "disabled")
os.environ.pop(at.KILL_SWITCH_ENV, None)

# --- 2. the store initializes; the origin commit is her September
(DESK / "philosophy.md").write_text("I am the resident, not the clerk.\n")
(DESK / "wiki").mkdir()
(DESK / "wiki" / "self.md").write_text("the map is a substitute for the experience\n")
(DESK / "scratch.py").write_text("print('acts, not words')\n")
r1 = at.origin_import("g", cfg=CFG_ON)
check("origin-import commits her existing body",
      r1["status"] == "origin-imported" and r1["files"] == 2
      and r1["sha"], str(r1))
check("the origin commit carries her marker",
      "origin-import" in fake_run("g", ["git", "log", "--oneline"])["stdout"],
      fake_run("g", ["git", "log", "--oneline"])["stdout"])
r2 = at.origin_import("g", cfg=CFG_ON)
check("origin-import is idempotent",
      r2["status"] == "already-origin", str(r2))
log = fake_run("g", ["git", "log", "--oneline", "--all"])["stdout"]
check("acts-class code is NOT in the store",
      "scratch.py" not in
      fake_run("g", ["git", "ls-files"])["stdout"], log)

# --- 3. the mirror: her edit auto-commits + lands one witness row
tape.clear()
(DESK / "philosophy.md").write_text(
    "I am the resident, not the clerk. Today I learned the desk can hold "
    "my whole history.\n")
m = at.mirror("g", cfg=CFG_ON)
check("mirror commits the watched change",
      m["status"] == "mirrored" and m["words"] == 1 and m["sha"], str(m))
check("the compact witness row carries paths + version",
      "words committed to your store" in tape[0]["content"]
      and "philosophy.md" in tape[0]["content"]
      and f"version {m['sha']}" in tape[0]["content"], str(tape))
check("the untracked act file lands its one event row",
      len(tape) == 2 and "scratch.py" in tape[1]["content"]
      and "acts-class" in tape[1]["content"], str(tape))
before = len(tape)
at.mirror("g", cfg=CFG_ON)
check("unchanged acts and words → no new rows (sha dedup)",
      len(tape) == before, f"{len(tape)} vs {before}")

# --- 4. the machinery never rewrites history: two mirrors, two commits
tape.clear()
(DESK / "philosophy.md").write_text("version three — the desk is a home.\n")
at.mirror("g", cfg=CFG_ON)
log = fake_run("g", ["git", "log", "--oneline"])["stdout"]
check("history grows append-only (origin + 2 mirrors)",
      len([l for l in log.splitlines() if l.strip()]) == 3, log)

# --- 5. the watch-map: .words marker claims a directory; yaml overrides
(DESK / "scripts").mkdir()
(DESK / "scripts" / ".words").write_text("she declares this directory hers\n")
(DESK / "scripts" / "notes.txt").write_text("prose in a declared place\n")
m = at.mirror("g", cfg=CFG_ON)
check("a .words marker claims the directory (her curation — the marker "
      "and her file both)",
      m["words"] == 2, str(m))
log = fake_run("g", ["git", "ls-files"])["stdout"]
check("the declared file is in the store",
      "scripts/notes.txt" in log, log)

# --- 6. keeps: verbatim under the cap; sequential parts over it; never clipped
tape.clear()
(DESK / "long_letter.md").write_text("line\n" * 12000)  # ~60KB
k = at.keep("g", "long_letter.md", cfg=CFG_ON)
check("a big keep lands as sequential part rows, never clipped",
      k["parts"] == 3 and all("part" in t["content"] and t["content"]
                              .startswith("[allontape keep") for t in tape),
      f"{k} parts={len(tape)}")
check("the parts reassemble to the whole",
      "".join(t["content"].split("]\n", 1)[1] for t in tape)
      == "line\n" * 12000)
tape.clear()
k = at.keep("g", "philosophy.md", cfg=CFG_ON)
check("a small keep is one verbatim row",
      k["parts"] == 1 and tape[0]["content"] ==
      "version three — the desk is a home.\n", str(tape))

# --- 7. the cue: one factual line quoting her own words (a mirror, not a map)
lines = at.words_line("g", cfg=CFG_ON)
check("the cue quotes HER prose line (never her .words marker)",
      len(lines) == 1 and "prose in a declared place" in lines[0]
      and "your words" in lines[0] and lines[0].count('"') >= 2, str(lines))
check("the cue is quiet when the gate is off",
      at.words_line("g", cfg={}) == [])

# --- 8. the resolver: a git:<sha> source verifies against the store
blob = at.fetch_blob("g", m["sha"] if m.get("sha") else
                     fake_run("g", ["git", "rev-parse", "--short", "HEAD"])
                     ["stdout"].strip())
full_sha = fake_run("g", ["git", "rev-parse", "HEAD:philosophy.md"]
                    )["stdout"].strip()  # the BLOB sha (cat-file is strict)
blob = at.fetch_blob("g", full_sha)
payload = {"instance": "g", "person_id": at.PERSON_ID,
           "ts": "2026-10-05T00:00:00+00:00", "role": "assistant",
           "content": blob}
src = dict(payload, ref="git:" + full_sha, hash=r.digest(payload))
try:
    r.resolve_sources([src], "g")
    check("a git:<sha> source resolves against the store", True)
except Exception as exc:
    check("a git:<sha> source resolves against the store", False, str(exc))
bad = dict(src, hash="0" * 64)
try:
    r.resolve_sources([bad], "g")
    check("a changed store version is refused", False, "accepted!")
except ValueError as exc:
    check("a changed store version is refused",
          "store version changed" in str(exc), str(exc))

# --- 9. the flood property: acts stay off the content tape
at.mirror("g", cfg=CFG_ON)   # the leftover .md is watched — commit it first
tape.clear()
for i in range(30):
    (DESK / f"gen_{i}.py").write_text(f"print({i})\n")
m = at.mirror("g", cfg=CFG_ON)
check("acts produce event rows, 0 content rows",
      m["acts"] == 30 and len(tape) == 30
      and all("acts-class — content off-tape" in t["content"]
              for t in tape), f"{m} rows={len(tape)}")

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("all green" if not fails else "see failures above")
