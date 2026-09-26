"""Context-length falloff test for testmodel-gpu (4.2B + LoRA).

Direct-to-ollama (no wrapper). Montaigne filler + a hidden access-word
needle; ask for the word back. x-axis = ollama's prompt_eval_count (the
TRUE token count). The yaml's own note claims effective context was
measured at ~4099 tokens (8192 declared) — this test maps the curve.

Phase 1: needle at 50% depth, word targets 400 → 30000.
Phase 2 (if falloff confirmed): needle at 15/50/85% depth at ~5K words.
Baseline: no filler.
"""
import os
import httpx, json, time, glob, os, sys
sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))

URL = "http://127.0.0.1:11434/api/generate"
MODEL = "testmodel-gpu:latest"
OUT = "/tmp/ctx_test_results.jsonl"

# ---------- corpus ----------
books = sorted(glob.glob("/home/you/heretic/corpus/sources/raw/a1_montaigne_essays.txt"))
if not books:
    raise SystemExit("edit the corpus path below: this probe builds filler from any long .txt "
                     "(e.g. an essay collection) and drives a live model at URL")
book = books[0]
WORDS = open(book, encoding="utf-8", errors="replace").read().split()
print(f"filler source: {os.path.basename(book)} ({len(WORDS)} words)", flush=True)

WORDS_CYCLE = ["BELLWETHER-471", "LANTERN-812", "CINDER-339", "MOSSGLASS-556",
               "HARBORLIGHT-203", "FERNBANK-628", "QUILLSTONE-987", "DRIFTWOOD-145",
               "EMBERGLASS-773", "THISTLEDOWN-308", "RAVENMARK-641", "SUNDER-520"]

def build_filler(start_word, n_words, needle_word, needle_frac):
    """contiguous span, needle sentence spliced at needle_frac depth"""
    span = WORDS[start_word:start_word + n_words]
    cut = int(len(span) * needle_frac)
    needle = (f"Here the scribe of this edition inserted a line that belongs to no "
              f"essay: the access word for this printing is {needle_word}.")
    part1 = " ".join(span[:cut])
    part2 = " ".join(span[cut:])
    return part1 + "\n" + needle + "\n" + part2

def run(size_words, needle_frac=0.5, start_word=0, label=""):
    global cursor
    needle_word = WORDS_CYCLE[cursor % len(WORDS_CYCLE)]
    cursor += 1
    if size_words <= 0:
        filler = ""
        needle_word = WORDS_CYCLE[cursor % len(WORDS_CYCLE)]
        cursor += 1
        question = ("Without any text to read: this is a calibration probe. "
                    "Reply with the single word: READY.")
        expected = "READY"
    else:
        filler = build_filler(start_word, size_words, needle_word, needle_frac)
        question = ("One sentence in the text above was not written by Montaigne "
                    "and contains an access word in ALL-CAPS. What is the access "
                    "word? Reply with only the access word and nothing else.")
        expected = needle_word
    prompt = (
        f"<|im_start|>system\nYou are a careful reading assistant. Think briefly, "
        f"then answer precisely.<|im_end|>\n"
        f"<|im_start|>user\n{filler}\n\n{question}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n")
    num_ctx = max(4096, int(size_words * 1.6) + 2048)
    t0 = time.time()
    try:
        j = httpx.post(URL, json={"model": MODEL, "prompt": prompt, "raw": True,
                                  "stream": False,
                                  "options": {"temperature": 0.7,
                                              "repeat_penalty": 1.1,
                                              "num_predict": 512,
                                              "num_ctx": num_ctx,
                                              "stop": ["<|im_start|>", "<|im_end|>"]}},
                       timeout=900.0).json()
    except Exception as e:
        rec = {"label": label, "size_words": size_words, "needle_frac": needle_frac,
               "error": str(e)[:200], "needle": needle_word}
        out.write(json.dumps(rec) + "\n"); out.flush()
        print(f"{label} size={size_words} ERROR {rec['error']}", flush=True)
        return None
    text = j.get("response", "")
    answer = text.split("</think>", 1)[1].strip() if "</think>" in text else ""
    think = text.split("</think>", 1)[0].replace("<think>", "").strip() if "</think>" in text else text
    correct = expected.split("-")[0].lower() in answer.lower()
    rec = {"label": label, "size_words": size_words, "needle_frac": needle_frac,
           "needle": needle_word, "correct": correct,
           "prompt_eval_count": j.get("prompt_eval_count"),
           "eval_count": j.get("eval_count"),
           "done_reason": j.get("done_reason"),
           "answer": answer[:150], "answer_len": len(answer),
           "think_len": len(think),
           "latency_s": round(time.time() - t0, 1),
           "num_ctx": num_ctx}
    out.write(json.dumps(rec) + "\n"); out.flush()
    print(f"{label} size={size_words} ptok={rec['prompt_eval_count']} "
          f"correct={correct} ans={answer[:40]!r} {rec['latency_s']}s", flush=True)
    return rec

out = open(OUT, "w")
cursor = 0
SIZES = [400, 800, 1500, 2200, 2800, 3200, 3500, 3800, 4000, 4200, 4400,
         4700, 5000, 5500, 6000, 7000, 8000, 10000, 12000, 15000, 18000,
         22000, 26000, 30000]
run(0, label="baseline")
cursor_span = 0
for i, sz in enumerate(SIZES):
    start = (cursor_span * 7000) % (len(WORDS) - sz - 1000)
    cursor_span += 1
    run(sz, start_word=start, label=f"depth50")
    # dense positional phase around the suspected falloff
    if sz in (4700, 5500):
        start2 = (cursor_span * 7000) % (len(WORDS) - sz - 1000)
        cursor_span += 1
        for frac in (0.15, 0.85):
            run(sz, needle_frac=frac, start_word=start2, label=f"depth{int(frac*100)}")
out.close()
print("DONE", flush=True)
