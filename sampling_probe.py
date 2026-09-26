"""Sampling A/B: replay the four real loop-seeding contexts (2026-09-07 evening)
against the exact production raw-chatml composition, varying temperature and
repeat_penalty. Measures: tokens, chars, shingle-dup ratio (the calibrated
detector), and captures sample text. Read-only against the chronicle; writes
results to /tmp/continua_sampling_results.jsonl.
"""
import os
import json, time, httpx, yaml, sys, re

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
from core import _degeneracy_check  # the calibrated shingle detector

MODEL = "testmodel-gpu:latest"
URL = "http://127.0.0.1:11434/api/generate"
NUM_PREDICT = 2048  # test cap: a flood reveals its ratio well before this

# ---------- rebuild the real prompt pieces ----------
import glob as _glob
_cfgs = _glob.glob("/tmp/continua/configs/*.yaml")
if not _cfgs:
    raise SystemExit("this probe replays a recorded conversation day against a live model; "
                     "point it at your own configs/*.yaml and chronicle files (see the paths below)")
cfg = yaml.safe_load(open(_cfgs[0]))
IDENTITY = cfg["prompts"]["identity"]

_chron = sorted(_glob.glob("/tmp/continua/chronicle/*/*/*.jsonl"))
if not _chron:
    raise SystemExit("no chronicle files found — this A/B replay needs a recorded day; edit the glob below")
recs = []
seen = set()
for line in open(_chron[0]):
    r = json.loads(line)
    k = (r["ts"], r["role"], (r.get("content") or "")[:80])
    if k not in seen:
        seen.add(k)
        recs.append(r)
recs.sort(key=lambda r: r["ts"])

def get(ts_prefix, role):
    for r in recs:
        if r["ts"].startswith(ts_prefix) and r["role"] == role:
            return r
    raise KeyError(ts_prefix)

flood_2055 = get("2026-09-07T20:55:44", "assistant")["content"]
STUB = ("[A degenerate repetition reply (41746 chars) was produced here "
        "and removed from context; the full text is preserved in your record.]")

# system block: identity + the ACTUAL memory injection present at 20:55
MEM_INJ = get("2026-09-07T20:55:44", "assistant").get("memory_injection") or ""
SYSTEM = (IDENTITY.rstrip()
          + "\n\n[AUTO-RECALLED MEMORIES — for context only, cite freely]\n"
          + MEM_INJ)

# history records before the 20:50 trigger
pre = [r for r in recs if r["ts"] < "2026-09-07T20:50:52"
       and r["role"] in ("user", "assistant")]

def render_history(records, flood_mode):
    """flood_mode: 'raw8k' = the 20:55 flood rides in head-truncated to 8.4K
    (the pre-fix reality); 'stub' = the post-fix stub; 'none' = excluded."""
    parts = []
    for r in records:
        role, c = r["role"], r.get("content") or ""
        if "20:55:44" in r["ts"] and r["role"] == "assistant":
            if flood_mode == "raw8k":
                c = c[:8400]
            elif flood_mode == "stub":
                c = STUB
            else:
                continue
        parts.append((role, c))
    return parts

def context(mode):
    """returns (history_records, final_user_text) for each scenario"""
    if mode == "C1":  # first fire: clean day history + the 20:50 check-in
        return render_history(pre, "none"), get("2026-09-07T20:50:52", "user")["content"]
    if mode == "C2":  # pre-fix compounding: flood head rides in + 21:44 correction
        return render_history(pre, "raw8k") + [("assistant", flood_2055[:8400])], \
               get("2026-09-07T21:44:09", "user")["content"]
    if mode == "C3":  # post-fix regime: stub + "you seem to be getting in loops"
        return render_history(pre, "stub") + [("assistant", STUB)], \
               get("2026-09-07T21:50:23", "user")["content"]
    if mode == "C4":  # post-fix regime: stub + "one sentence"
        return render_history(pre, "stub") + [
            ("assistant", STUB),
            ("user", get("2026-09-07T21:50:23", "user")["content"]),
            ("assistant", "[A degenerate repetition reply (30991 chars) was "
             "produced here and removed from context; the full text is "
             "preserved in your record.]")], \
               get("2026-09-07T22:24:27", "user")["content"]

def compose(records, final_user):
    """EXACT production raw-chatml composition (core.py raw path)"""
    parts = [f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"]
    budget = 12000  # max_history_chars
    used = 0
    kept = []
    for role, c in reversed(records):  # newest-first budget trim
        if used + len(c) > budget:
            continue
        kept.append((role, c))
        used += len(c)
    for role, c in reversed(kept):
        if role == "assistant":
            parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n" + c + "<|im_end|>\n")
        else:
            parts.append(f"<|im_start|>{role}\n{c}<|im_end|>\n")
    parts.append(f"<|im_start|>user\n{final_user}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n<think>\n")
    return "".join(parts)

CONFIGS = [
    ("t0.70_rp1.10", 0.70, 1.10),  # production baseline
    ("t0.70_rp1.15", 0.70, 1.15),
    ("t0.70_rp1.20", 0.70, 1.20),
    ("t0.80_rp1.10", 0.80, 1.10),  # the designer's temp question, penalty held
    ("t0.80_rp1.20", 0.80, 1.20),
]
SCENARIOS = ["C1", "C2", "C3", "C4"]

out = open("/tmp/continua_sampling_results.jsonl", "w")
for scen in SCENARIOS:
    hist, final_user = context(scen)
    prompt = compose(hist, final_user)
    for name, temp, rp in CONFIGS:
        payload = {
            "model": MODEL, "prompt": prompt, "raw": True, "stream": False,
            "options": {"temperature": temp, "repeat_penalty": rp,
                        "num_predict": NUM_PREDICT,
                        "stop": ["<|im_start|>", "<|im_end|>"]},
        }
        t0 = time.time()
        try:
            r = httpx.post(URL, json=payload, timeout=600.0)
            j = r.json()
        except Exception as e:
            out.write(json.dumps({"scen": scen, "cfg": name, "error": str(e)}) + "\n")
            out.flush()
            continue
        text = j.get("response", "")
        flagged, ratio = _degeneracy_check(text, None)
        rec = {
            "scen": scen, "cfg": name, "temp": temp, "rp": rp,
            "chars": len(text), "tokens": j.get("eval_count"),
            "done_reason": j.get("done_reason"),
            "ratio": round(ratio, 3), "flagged": flagged,
            "latency_s": round(time.time() - t0, 1),
            "head": text[:220], "prompt_chars": len(prompt),
        }
        out.write(json.dumps(rec) + "\n")
        out.flush()
        print(f"{scen} {name}: {rec['tokens']} tok, ratio {ratio:.2f}, "
              f"flagged={flagged}, {rec['latency_s']}s", flush=True)
out.close()
print("DONE")
