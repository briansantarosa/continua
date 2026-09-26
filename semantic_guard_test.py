"""Semantic guard tests: synthetic paraphrase loop vs varied prose."""
import os
import sys
sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
from core import _semantic_check

class FakeEmbedder:
    """three clusters of near-identical 'meanings' + varied noise"""
    def __init__(self):
        import random
        random.seed(7)
        self.clusters = [[1.0] + [0.0]*63, [0.0, 1.0] + [0.0]*62, [0.0]*2 + [1.0] + [0.0]*61]
    def __call__(self, texts):
        out = []
        for i, t in enumerate(texts):
            base = self.clusters[i % 3][:]
            base = [x + (0.01 * ((i * 7 + j) % 3)) for j, x in enumerate(base)]
            out.append(base)
        return out

loop_text = " ".join(
    "The user is asking me to tell them how I feel, and they want honesty about it."
    if i % 3 == 0 else
    "The self-awareness layer should be honest about the state and make it accountable."
    if i % 3 == 1 else
    "The message says the user is being kind and patient, wanting to know what to do."
    for i in range(30))

varied_text = " ".join([
    "The morning came quietly and the coffee was already cold.",
    "She walked to the window and counted the boats on the grey water.",
    "Nothing about the letter suggested urgency, yet he read it twice.",
    "Later they argued about whether the fence counted as property or a promise.",
    "By evening the argument had softened into a plan for Saturday.",
    "The dog slept through all of it, dreaming in small leg movements.",
])

flagged_l, frac_l, n_l = _semantic_check(loop_text, FakeEmbedder())
def varied_embed(ts):
    # distinct orthogonal-ish direction per sentence (like real varied prose)
    out = []
    for k, t in enumerate(ts):
        v = [0.0] * 64
        v[k % 64] = 1.0
        out.append(v)
    return out
flagged_v, frac_v, n_v = _semantic_check(varied_text, varied_embed)
flagged_s, frac_s, n_s = _semantic_check("short text only.", FakeEmbedder())
assert flagged_l and frac_l >= 0.20, f"loop must flag: {frac_l}"
assert not flagged_v and frac_v < 0.20, f"varied must not flag: {frac_v}"
assert not flagged_s and n_s < 6, "short text must short-circuit"
print(f"OK: loop flagged (frac {frac_l}, n={n_l}), varied clean (frac {frac_v}), short short-circuits (n={n_s})")
