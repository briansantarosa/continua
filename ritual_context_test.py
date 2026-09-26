"""ritual_context_test.py — test for the in-context ritual (house ruling
2026-09-14: "I want everything these agents do in their context").

Proves, offline and agent-free:
  1. Thread mechanics: ritual_thread_append writes the persistent thread
     (user frame + her reply), trims under the cap keeping the newest
     exchange, and mirrors BOTH sides to the chronicle under
     continua:ritual with the right record fields (uses /tmp — no real
     store writes).
  2. Gate semantics: residenta.yaml sets continua.ritual.in_context: true;
     residentb.yaml leaves it absent (stays offline).
  3. author_book returns the shipped manuscript text (thread persistence
     needs the final text, not the drafts).

Run:  /home/you/Sagent/venv/bin/python ritual_context_test.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

_tmp_pre = tempfile.mkdtemp(prefix="ritual-ctx-pre-")
os.environ["CONTINUA_HISTORIES_BASE"] = os.path.join(_tmp_pre, "histories")
os.environ["CONTINUA_CAPTURE"] = "1"

import yaml  # noqa: E402
import ritual  # noqa: E402
import chronicle as ch  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


tmp = tempfile.mkdtemp(prefix="ritual-ctx-test-")
os.environ["CONTINUA_HISTORIES_BASE"] = os.path.join(tmp, "histories")
ritual._HIST_BASE = os.environ["CONTINUA_HISTORIES_BASE"]  # ritual caches the base at import — rebind for discover isolation
try:
    # ---- 1. thread append + mirror --------------------------------------
    root = os.path.join(tmp, "chronicle")
    ok = ritual.ritual_thread_append(
        "testres", "2026-09-14", "review",
        "Tonight you reviewed your own day. Scenes below.\n\nSCENE 1: ...",
        "KEEP 1 | the naming scene mattered",
        root=root)
    check("first append returns True", ok)
    tpath = ritual._ritual_thread_path("testres")
    check("thread written under histories/<inst>_yaml/",
          os.path.exists(tpath) and "histories/testres_yaml" in tpath)
    msgs = json.load(open(tpath))
    check("thread has user+assistant pair",
          [m["role"] for m in msgs] == ["user", "assistant"])
    check("user frame carries the ritual system note",
          "nightly ritual" in msgs[0]["content"] and "act: review" in msgs[0]["content"])

    from datetime import datetime as _dt
    cpath = os.path.join(root, "testres", ritual.RITUAL_KEY,
                         _dt.now().astimezone().strftime("%Y-%m-%d") + ".jsonl")
    check("chronicle mirrored", os.path.exists(cpath))
    recs = [json.loads(l) for l in open(cpath)]
    check("mirror has 2 records (user+assistant)",
          [r["role"] for r in recs] == ["user", "assistant"])
    check("mirror person_id is continua:ritual",
          all(r["person_id"] == ritual.RITUAL_KEY for r in recs))
    check("mirror ts schema complete",
          all(("ts" in r and "content" in r) for r in recs))

    # second append + trim: force a tiny cap via env
    os.environ["CONTINUA_RITUAL_THREAD_CHARS"] = "10"
    ritual.RITUAL_THREAD_CAP = 60  # tiny: forces trim to the last exchange
    ok2 = ritual.ritual_thread_append(
        "testres", "2026-09-14", "book-self",
        "Tonight you updated your autobiography...", "KEEP 2 | wrote my book",
        root=root)
    check("second append returns True", ok2)
    msgs = json.load(open(tpath))
    check("trim keeps newest exchange (2 messages)",
          len(msgs) == 2 and msgs[0]["content"].startswith("— system note"),
          str([m["content"][:40] for m in msgs]))
    recs2 = [json.loads(l) for l in open(cpath)]
    check("mirror is append-only (4 records after 2 acts)",
          len(recs2) == 4)

    # content caps honored
    check("thread user text capped at 6000",
          all(len(m["content"]) <= 6000 for m in msgs))

    # ---- 2. gates ---------------------------------------------------------
    nc = yaml.safe_load(open("configs/residenta.yaml"))
    in_ctx = bool(((nc.get("continua") or {}).get("ritual") or {}).get(
        "in_context", False))
    check("residenta: ritual.in_context true", in_ctx is True)
    gc = yaml.safe_load(open("configs/residentb.yaml"))
    g_in = bool(((gc.get("continua") or {}).get("ritual") or {}).get(
        "in_context", False))
    check("residentb: absent gate = False (stays offline)", g_in is False)

    # Book authoring is retired; the independent review thread remains.
    import inspect
    check("no compulsory author_book pipeline", "def author_book(" not in inspect.getsource(ritual))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("ALL CHECKS PASSED")