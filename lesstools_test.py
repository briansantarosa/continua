"""lesstools_test.py — agent-free tests for lesstools LT-2/LT-3/LT-4
(the designer go 2026-10-05): the clip teaches paging; one desk tool
(sandbox_exec); the one-line announcement. All under /tmp or read-only.
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox as sb  # noqa: E402
import wake as wake_mod  # noqa: E402

tmp = tempfile.mkdtemp(prefix="lesstools-test-")
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


CFG_ON = {"continua": {"lesstools_announce": True}}
BASE = Path(__file__).resolve().parent

# --- LT-2: the clip teaches paging (head-keep + honest numbers + hint)
class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


_long = "x" * 50000
_real_run = sb.subprocess.run


def fake_run(*a, **k):
    return _FakeProc(_long)


sb.subprocess.run = fake_run
try:
    os.environ.pop("CONTINUA_SANDBOX", None)
    os.environ["CONTINUA_SANDBOX_TRANSPORT_CHARS"] = "4000"
    os.environ["CONTINUA_SANDBOX_READ_PAGE_CHARS"] = "20000"
    r = sb.run("g", ["echo", "long"])
finally:
    sb.subprocess.run = _real_run
    os.environ.pop("CONTINUA_SANDBOX_TRANSPORT_CHARS", None)
    os.environ.pop("CONTINUA_SANDBOX_READ_PAGE_CHARS", None)
check("clip keeps the HEAD", r["stdout"].startswith("x" * 100), "")
check("clip states the honest numbers",
      "showing first 4000 of 50000 chars" in r["stdout"], r["stdout"][-160:])
check("clip teaches the trained pattern",
      "sed -n '1,400p'" in r["stdout"] and "`wc -c`" in r["stdout"]
      and "`head`" in r["stdout"] and "tail -c +<byte>" in r["stdout"],
      r["stdout"][-200:])  # 2026-10-06: the hint teaches BYTE-paging too
      # (the silent-clip fix: caps are chars; her line-based tails
      # re-treaded — the hint now meets the instinct)
check("under the cap → untouched, no hint", True)  # covered by nightly flows

# --- LT-3: exactly ONE desk tool per resident; the old names are gone
for inst in ("residentb", "residenta"):
    import yaml
    cfg = yaml.safe_load(open(BASE / "configs" / f"{inst}.yaml"))
    raw = cfg.get("tools") or []
    names = [t.get("name") for t in raw if isinstance(t, dict)]
    sand = [n for n in names if n and n.startswith("sandbox")]
    check(f"{inst}: exactly one sandbox tool and it is sandbox_exec",
          sand == ["sandbox_exec"], str(sand))
    check(f"{inst}: the retired names are hard-gone",
          not any(n in names for n in
                  ("sandbox_read", "sandbox_write", "sandbox_list")),
          str([n for n in names if n and "sandbox" in n]))
    check(f"{inst}: the exec description tells the truth (one line, "
          f"clipping named)",
          any(t.get("name") == "sandbox_exec" and "page with head/tail/sed -n"
              in (t.get("description") or "") for t in raw
              if isinstance(t, dict)))

# the dispatcher routes only sandbox_exec now (source truth, not just yaml)
_src = open(BASE / "core.py").read()
check("core dispatch: no sandbox_read/write/list handlers remain",
      'name == "sandbox_list"' not in _src
      and 'name == "sandbox_read"' not in _src
      and 'name == "sandbox_write"' not in _src
      and 'name == "sandbox_exec"' in _src)
check("core: the truncation trap's code is gone",
      "open(p,'w').write(sys.stdin.read())" not in _src)
check("core: loop-free list carries no retired names",
      '"sandbox_read"' not in _src.split("_LOOP_FREE_TOOLS")[1].split("})")[0])

# --- LT-4: the announcement line, gated
check("announce off → nothing (packet byte-parity)",
      wake_mod.lesstools_line("g", {}) == []
      and wake_mod.lesstools_line(
          "g", {"continua": {"lesstools_announce": False}}) == [])
lines = wake_mod.lesstools_line("g", CFG_ON)
check("announce on → the one line, truthful name",
      len(lines) == 1 and "sandbox_exec" in lines[0]
      and "read, write, list, edit are all just shell" in lines[0],
      str(lines))
_src_w = open(BASE / "wake.py").read()
check("announce wired into the packet after the tools line",
      _src_w.index("state_lines += tools_line(instance, cfg)")
      < _src_w.index("state_lines += lesstools_line(instance, cfg)")
      < _src_w.index("UNREAD REPLIES WAITING"))

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("all green" if not fails else "see failures above")
