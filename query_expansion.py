"""Post-v3 backlog item #2: LLM query decomposition/expansion for memory recall.

Temporal and multi-hop questions often use question-phrasing vocabulary that
never overlaps with how the underlying fact was stored (eval-proven 2026-08-25:
remaining temporal failures are search-stage misses, gold absent from top-20).
Rewriting the user's question into 2-3 standalone *fact-shaped* search queries
closes that vocabulary gap.

All LLM work routes to the dedicated qwen card (non-thinking), per project
constraint. Fail-open design: any error returns the original query unchanged —
expansion must never break or stall the recall path.
"""

import json
import logging
import os
import re
import time

from openai import OpenAI

logger = logging.getLogger("sagent.query_expansion")

EXPANSION_ENABLED = os.getenv("SAGENT_QUERY_EXPAND", "1") == "1"

# Post-v3.6 Phase M1 (2026-08-26): widen recall for aggregation-intent
# questions and weak first-pass recalls, not just temporal ones. Judge v3
# showed every remaining eval failure is an IN-STORE fact the single query
# never surfaces: aggregation enumerations ('Muse Glimmer' inside a
# model-list question) and vocabulary-mismatch facts ('Artifact Tests'
# stored vs 'games' asked).
AGGREGATE_RECALL_ENABLED = os.getenv("SAGENT_AGGREGATE_RECALL", "1") == "1"

# Conservative aggregate-intent probe: enumeration/synthesis asks that need
# cluster-wide coverage rather than top-1 similarity.
_AGG_INTENT_RE = re.compile(
    r"\b(summarize|summarise|sum it up|everything|all the|full |whole |"
    r"compare|comparison|options\b|inventory\b|checklist\b|recap\b)",
    re.I,
)
# 'Which X have we talked about' / 'What X did we discuss' style enumeration.
_ENUM_ASK_RE = re.compile(
    r"\b(which|what)\s+\w[^?]{0,80}\?*$", re.I
)
_WE_HAVE_RE = re.compile(r"\b(we|i)\s+(have )?(talked|discussed|explored|tested|suggested|tried)", re.I)
# Weak first-pass recall thresholds (mirror of production weak-widening).
WEAK_MIN_RESULTS = 3
WEAK_MIN_BEST_SCORE = 0.42
# memfixes post-eval fix (Phase 5 diagnosis): the weak-recall widening branch
# over-fans short everyday turns ("chicken for the grill?" pulled gravel-
# supplier facts at 0.71) because a small-but-decent first pass still widens.
# STRICT mode raises the bar for the intent-less widening branch: widen only
# when the first pass is EMPTY (n_results == 0) or best_score collapses below
# SAGENT_WEAK_RECALL_STRICT_FLOOR (default 0.30 = the injection floor).
# Default OFF — eval-gated, enable via env (SAGENT_WEAK_RECALL_STRICT=1).
WEAK_RECALL_STRICT = os.getenv("SAGENT_WEAK_RECALL_STRICT", "0") == "1"
WEAK_RECALL_STRICT_FLOOR = float(os.getenv("SAGENT_WEAK_RECALL_STRICT_FLOOR", "0.30"))


def is_aggregate_intent(query: str) -> bool:
    """Aggregation/enumeration questions need cluster coverage, not top-1."""
    if not AGGREGATE_RECALL_ENABLED or not query:
        return False
    if _AGG_INTENT_RE.search(query):
        return True
    return bool(_ENUM_ASK_RE.search(query) and _WE_HAVE_RE.search(query))


def is_weak_recall(n_results: int, best_score: float) -> bool:
    """First pass surfaced too little to answer with confidence."""
    return n_results < WEAK_MIN_RESULTS or best_score < WEAK_MIN_BEST_SCORE


_EXPAND_SYSTEM = (
    "You rewrite a user's question into standalone search queries for a "
    "personal-memory database. Each rewrite must be phrased like a stored "
    "fact or keyword list (no question words), covering a different aspect "
    "or phrasing of the original. Output STRICT JSON: a list of 2-4 strings. "
    "No explanations."
)


def should_expand(query: str) -> bool:
    """Conservative trigger: temporal/historical intent OR aggregate intent.
    Weak-recall widening lives in should_recall_widen() (needs search stats)."""
    if not EXPANSION_ENABLED or not query:
        return False
    import temporal_memory as tm
    return (
        tm.wants_recency(query)
        or tm.is_historical_query(query)
        or is_aggregate_intent(query)
    )


def should_recall_widen(query: str, n_results: int = 99, best_score: float = 1.0) -> bool:
    """Single source of truth for when recall may widen beyond one plain
    search: intent triggers (temporal / aggregate) OR a weak first-pass pool.
    Consumed by both core.py production recall and run_eval.py p6 mirror."""
    if not EXPANSION_ENABLED or not query:
        return False
    import temporal_memory as tm
    if tm.wants_recency(query) or tm.is_historical_query(query):
        return True
    if is_aggregate_intent(query):
        return True
    if AGGREGATE_RECALL_ENABLED and is_weak_recall(n_results, best_score):
        if WEAK_RECALL_STRICT and not (
                n_results == 0 or best_score < WEAK_RECALL_STRICT_FLOOR):
            logger.info(
                "[QExpand] weak-recall widening SUPPRESSED (strict: n=%d, "
                "best=%.2f) for '%s'", n_results, best_score, query[:60],
            )
            return False
        logger.info(
            "[QExpand] weak-recall widening (n=%d, best=%.2f) for '%s'",
            n_results, best_score, query[:60],
        )
        return True
    return False


def expand_query(query: str, attempts: int = 2) -> list:
    """Return [original] + rewritten sub-queries. Never raises; never empty."""
    base = [query]
    if not EXPANSION_ENABLED:
        return base
    for attempt in range(attempts):
        try:
            client = OpenAI(
                base_url=os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1"),
                api_key=os.getenv("SAGENT_QWEN_KEY", "not-needed"),
                timeout=20,
            )
            resp = client.chat.completions.create(
                model=os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp"),
                messages=[
                    {"role": "system", "content": _EXPAND_SYSTEM},
                    {"role": "user",
                     "content": f"Question: {query[:400]}"},
                ],
                # M1b: 0.3 (was 0.7) — cross-run eval flips traced to
                # volatile rewrites; near-deterministic fact-shaped output
                # is the goal, diversity comes from per-run store state.
                temperature=0.3, top_p=0.8, presence_penalty=1.5,
                max_tokens=150,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            txt = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\[.*\]", txt, re.DOTALL)
            if not m:
                raise ValueError(f"no JSON list in response: {txt[:120]}")
            alts = json.loads(m.group(0))
            cleaned = []
            for a in alts if isinstance(alts, list) else []:
                if isinstance(a, str):
                    a = a.strip()
                    # drop degenerate outputs
                    if 8 <= len(a) <= 200 and a.lower() != query.lower():
                        cleaned.append(a)
            out = base + cleaned[:3]
            logger.info("[QExpand] '%s' -> %d variants", query[:60], len(out))
            return out
        except Exception as e:
            logger.warning("[QExpand] attempt %d failed: %s", attempt + 1, e)
            time.sleep(1.0 * (attempt + 1))
    return base  # fail open
