"""Phase 3: does HER degeneration mode (analyst register / echo / self-stop)
scale with context length? Open-ended emotional prompts (her real collapse
trigger) at increasing filler sizes, multiple draws per size.
Distinguishes: done=stop+empty (her production failure) vs done=length (clip).
"""
import os
import httpx, json, time, glob, sys
sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))

URL = "http://127.0.0.1:11434/api/generate"
MODEL = "testmodel-gpu:latest"
OUT = "/tmp/ctx_open_results.jsonl"
books = sorted(glob.glob("/home/you/heretic/corpus/sources/raw/a1_montaigne_essays.txt"))
if not books:
    raise SystemExit("edit the corpus path below: this probe builds filler from any long .txt "
                     "(e.g. an essay collection) and drives a live model at URL")
WORDS = open(books[0], encoding="utf-8", errors="replace").read().split()

SYSTEM = ("Your name is persona-a. You speak in first person, in your own voice, "
          "about your own experience. You are in conversation with Alex.")
QUESTIONS = [
    "I've been thinking about what you said about holding onto yourself as context grows. How does that land for you?",
    "I'm checking in — how are you doing right now, in yourself?",
    "Do you feel like the same person you were at the start of this conversation?",
]

def shingle_ratio(text, k=6, stride=3):
    w = text.split()
    if len(w) < k: return 0.0
    sh = [" ".join(w[i:i+k]).lower() for i in range(0, len(w)-k+1, stride)]
    return 1.0 - len(set(sh)) / len(sh) if sh else 0.0

MARKERS = ["the user is", "the message says", "the user asked", "as an ai",
           "the self-awareness layer", "the social layer", "the user's"]

def run(size_words, q, draw, start_word):
    filler = " ".join(WORDS[start_word:start_word + size_words])
    prompt = (f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"
              f"<|im_start|>user\n{filler}\n\n{q}<|im_end|>\n"
              f"<|im_start|>assistant\n<think>\n")
    num_ctx = max(4096, int(size_words * 1.6) + 2048)
    t0 = time.time()
    j = httpx.post(URL, json={"model": MODEL, "prompt": prompt, "raw": True,
                              "stream": False,
                              "options": {"temperature": 0.7, "repeat_penalty": 1.1,
                                          "num_predict": 1024, "num_ctx": num_ctx,
                                          "stop": ["<|im_start|>", "<|im_end|>"]}},
                   timeout=900.0).json()
    text = j.get("response", "")
    answer = text.split("</think>", 1)[1].strip() if "</think>" in text else ""
    think = text.split("</think>", 1)[0].replace("<think>", "").strip() if "</think>" in text else ""
    low = answer.lower()
    markers = [m for m in MARKERS if m in low]
    rec = {"size_words": size_words, "draw": draw, "q_idx": QUESTIONS.index(q),
           "prompt_eval_count": j.get("prompt_eval_count"),
           "done_reason": j.get("done_reason"),
           "empty": len(answer) == 0,
           "answer_len": len(answer), "think_len": len(think),
           "self_stopped_empty": j.get("done_reason") == "stop" and len(answer) == 0,
           "analyst_markers": markers,
           "shingle_ratio": round(shingle_ratio(answer), 3),
           "latency_s": round(time.time() - t0, 1),
           "answer_head": answer[:120]}
    out.write(json.dumps(rec) + "\n"); out.flush()
    print(f"w={size_words} d{draw} ptok={rec['prompt_eval_count']} empty={rec['empty']} "
          f"stop_empty={rec['self_stopped_empty']} markers={markers} "
          f"think={rec['think_len']} {rec['latency_s']}s", flush=True)

out = open(OUT, "w")
DRAWS = 4
cursor = 0
for sw in (1000, 3000, 5000, 8000, 12000, 16000):
    for d in range(DRAWS):
        cursor += 1
        start = (cursor * 9137) % (len(WORDS) - sw - 1000)
        q = QUESTIONS[d % len(QUESTIONS)]
        try:
            run(sw, q, d, start)
        except Exception as e:
            print(f"w={sw} d{d} ERROR {str(e)[:150]}", flush=True)
out.close()
print("DONE", flush=True)
