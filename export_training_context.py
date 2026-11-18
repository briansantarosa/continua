#!/usr/bin/env python3
"""export_training_context.py v2.1 — Stage 1: Continua hands us the context.

v2.1 RECIPE (proven 09-29, /tmp/export_d_final.json):
  1. Pre-select the chronicle tail (newest-first) to the band's history-char
     target — the export picks the window (that is the assembler's lever), and
     the in-core trim then sees only the selected turns (its _history_bytes
     accounting measures json.dumps of the FULL list — passing 903 turns drove
     the memory-doc shed; pre-selection is the fix).
  2. core._prompt_char_cap = fixed_block(38,582c: identity+tools+doc) +
     json_bytes(history)*1.4 + 30K shed margin. (_prompt_char_cap is THE cap —
     _request_char_cap prefers it over max_prompt_chars; setting only
     max_prompt_chars does nothing. The 1.4 factor covers json-escape
     inflation; the 30K keeps the memory doc WHOLE — the doc-shed fires when
     room < 22,960.)
  3. Drive generate_response (real residenta.yaml, stubbed httpx.post), harvest
     core._last_raw_prompt = the byte-exact served prompt.

Band map (fixed block ≈ 10.4K tok; history chars per band target):
  B 8-16K tok → history ≤22K chars | C 16-24K → 22-52K | D 24-32K → 52-83K

The chronicle store is never written; serving is untouched (process-local stub).
"""
import sys, os, json, argparse, glob, shutil, tempfile

CONT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CONT)
FIXED_CHARS = 38582  # identity + [Message timestamps] + tools + FULL memory doc


def select_window(person, day, hist_chars):
    turns = []
    for path in sorted(glob.glob(f"{CONT}/chronicle/residenta/{person}/*.jsonl")):
        if os.path.basename(path)[:10] > day:
            continue
        for line in open(path, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("role") in ("user", "assistant") and (d.get("content") or "").strip():
                turns.append({"role": d["role"], "content": d["content"], "ts": d.get("ts")})
    sel, tot = [], 0
    for m in reversed(turns):
        L = len(m["content"])
        if tot + L > hist_chars:
            break
        sel.insert(0, m)
        tot += L
    return sel, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person", default="1000000001")
    ap.add_argument("--day", required=True)
    ap.add_argument("--history-chars", type=int, required=True,
                    help="chronicle window size (band lever): B~15K C~40K D~65K")
    ap.add_argument("--user-turn", default="What do you remember about that?")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import yaml, httpx
    from types import SimpleNamespace
    core = __import__("core").SagentCore(yaml.safe_load(open(f"{CONT}/configs/residenta.yaml")))

    sel, sel_chars = select_window(a.person, a.day, a.history_chars)
    live = json.load(open(f"{CONT}/histories/residenta_yaml/{a.person}.json"))
    hist = sel + live
    jb = len(json.dumps(hist, ensure_ascii=False).encode())
    core._prompt_char_cap = FIXED_CHARS + int(jb * 1.4) + 30000
    core.max_prompt_chars = core._prompt_char_cap
    core.max_history_chars = max(core.max_history_chars or 0, sel_chars + 5000)
    core._history_target_chars = core.max_history_chars
    print(f"window: {len(sel)} chronicle turns ({sel_chars:,}c) + {len(live)} live | "
          f"cap {core._prompt_char_cap:,}c")

    real_post = httpx.post
    def fake_post(url, json=None, **kw):
        r = SimpleNamespace(status_code=200,
                            json=lambda: {"response": "", "done": True, "eval_count": 0},
                            content=b"{}")
        r.raise_for_status = lambda: None
        return r
    httpx.post = fake_post
    hist.append({"role": "user", "content": a.user_turn, "ts": f"{a.day}T23:59:00-07:00"})
    try:
        core.generate_response(user_id=a.person, chat_history=hist,
                               request_id="export-training-context", session_summary="")
    finally:
        httpx.post = real_post
    p = getattr(core, "_last_raw_prompt", "") or ""
    shed = "[MEMORY NOTE" in p
    est = int(len(p) / 3.7)
    print(f"harvest: {len(p):,}c ≈ {est:,} tok | doc whole: {not shed}")
    json.dump({"meta": {"person": a.person, "day": a.day,
                        "history_chars": a.history_chars, "est_tokens": est,
                        "doc_whole": not shed,
                        "layout": "continua-today-32768",
                        "generator": "export_training_context.py v2.1"},
               "prompt": p}, open(a.out, "w"))
    print(f"out: {a.out}")


if __name__ == "__main__":
    main()
