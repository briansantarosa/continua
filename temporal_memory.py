"""P2 temporal memory — sagentv3.md Upgrade 2.

Soft-invalidation for conversational long-term memories:
  - New fact contradicts an old one -> old gets payload flags
    ``superseded_by`` / ``superseded_at``; NOTHING is ever hard-deleted
    (data-safety invariant §7.3).
  - Recall prefers live facts; superseded ones are injected only when the
    query is explicitly historical/past-tense.
  - /forget marks matches ``superseded_by="USER_FORGET"`` instead of deleting.

Implementation notes (verified empirically 2026-08-25):
  - mem0.search() whitelists payload keys, so custom flags do NOT surface in
    its results. Flags are read back via
    ``memory.vector_store.client.retrieve(ids, with_payload=True)``.
  - Contradiction judging runs on the qwen card with thinking off (Revision B),
    from the background worker only — never on the interactive path.
  - Off-switch: SAGENT_TEMPORAL=0 disables everything here.
"""

# ============================================================
# CONTINUA FORK — pinned from ~/Sagent @ e276913 (2026-09-07)
# This file is Continua's copy of the Sagent organ. Sagent stays
# live and untouched; this fork evolves independently per
# ~/agentwiki/projects/Continua.md. Deviations are tagged [CONTINUA].
# ============================================================


import logging
import os
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TEMPORAL_ENABLED = os.getenv("SAGENT_TEMPORAL", "1") == "1"
CONFLICT_SEARCH_TOP_K = int(os.getenv("SAGENT_CONFLICT_TOP_K", "6"))
CONFLICT_SIM_THRESHOLD = float(os.getenv("SAGENT_CONFLICT_THRESHOLD", "0.45"))

_client = None


def _judge():
    """Lazy qwen judge client (same endpoint contract as session_memory)."""
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            base_url=os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1"),
            api_key="none",
            max_retries=0,
            timeout=60.0,
        )
    return _client


CONFLICT_SYSTEM = (
    "You compare a NEWLY LEARNED fact against an EXISTING stored fact about "
    "the same person, from an assistant's long-term memory.\n"
    "Decide whether they CONTRADICT each other — i.e. both cannot be true at "
    "the same time about the same subject (changed state counts: 'is doing X' "
    "vs 'finished X'; changed preferences; corrected numbers).\n"
    "Merely related, complementary, or differently-scoped facts are NOT "
    "contradictions. Answer exactly one word: YES or NO."
)

_HISTORICAL_RE = re.compile(
    r"\b(used to|did i (ever|use to)|have i ever|before|previously|in the past|"
    r"last (time|week|month|year)|earlier (this|last)? ?(year|month|week)?|"
    r"what did i (say|think|tell)|back then|at the time|history|"
    r"\b(19|20)\d{2}\b|january|february|march|april|may|june|july|august|"
    r"september|october|november|december|"
    # memfixes post-eval: summary/recap intent is historical intent — the
    # user asks for past-session content ("Summarize what we discussed this
    # week about X" was memeval-110's query and never routed historical).
    # Narrow forms: "summarize what we/i...", "summarize our...", "recap",
    # "what (have|did) we <discuss|talk|cover|decide|agree|go over|work on>".
    # Deliberately NOT matching bare "summarize this <article/doc>".
    r"summarize what (we|i)|summarize our|recap|"
    r"what (have|did) we (discuss|talk|cover|decide|agree|go over|work on)(ed|ing|s)?)\b",
    re.IGNORECASE,
)


def is_historical_query(text: str) -> bool:
    """Heuristic: does this query ask about past/superseded states?"""
    return bool(text and _HISTORICAL_RE.search(text))


# --- date-aware retrieval (post-v3 item #1, 2026-08-25) ----------------------
# Distinct from is_historical_query: this detects RECENT-temporal intent
# ("what did we decide last week"), where recency is a relevance signal.
# Historical queries must NOT get a recency boost — they often target old
# facts (that's why they unlock superseded ones instead).

_RECENT_RE = re.compile(
    r"\b(yesterday|today|tonight|this morning|last night|"
    r"(last|this|past) (week|weekend|month|couple days|few days)|"
    r"(a|couple|few) days? ago|recently|lately|just now|earlier today|"
    r"latest|newest|most recent)\b",
    re.IGNORECASE,
)


def wants_recency(text: str) -> bool:
    """Heuristic: does this query implicitly prefer recently-created facts?"""
    return bool(text and _RECENT_RE.search(text))


# --- direct vector-store flag access ----------------------------------------

def fetch_flags(memory, ids: List[str], collection_name: str) -> Dict[str, dict]:
    """Batch-read supersession flags for ids. Returns {id: {"superseded_by":…}}."""
    if not TEMPORAL_ENABLED or not ids:
        return {}
    try:
        pts = memory.vector_store.client.retrieve(
            collection_name=collection_name, ids=ids, with_payload=True
        )
        out = {}
        for p in pts:
            pl = p.payload or {}
            if pl.get("superseded_by"):
                out[str(p.id)] = {
                    "superseded_by": str(pl["superseded_by"]),
                    "superseded_at": str(pl.get("superseded_at", "")),
                }
        return out
    except Exception as e:
        logger.warning("[Temporal] fetch_flags failed (non-fatal): %s", e)
        return {}


def mark_superseded(memory, collection_name: str, old_ids: List[str],
                    superseded_by: str) -> int:
    """Soft-invalidate: set supersession payload flags. Returns count marked."""
    if not TEMPORAL_ENABLED or not old_ids:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    ok = 0
    for oid in old_ids:
        try:
            memory.vector_store.client.set_payload(
                collection_name=collection_name,
                payload={"superseded_by": superseded_by, "superseded_at": now},
                points=[oid],
            )
            ok += 1
        except Exception as e:
            logger.warning("[Temporal] mark_superseded failed for %s: %s", oid, e)
    if ok:
        logger.info("[Temporal] marked %d/%d memories superseded by %s",
                    ok, len(old_ids), superseded_by)
    return ok


def unmark_superseded(memory, collection_name: str, ids: List[str]) -> int:
    """Restore soft-invalidated memories (used if a conflict was a mistake).
    Removes the flag payloads entirely."""
    if not TEMPORAL_ENABLED or not ids:
        return 0
    ok = 0
    for oid in ids:
        try:
            memory.vector_store.client.set_payload(
                collection_name=collection_name,
                payload={"superseded_by": None, "superseded_at": None},
                points=[oid],
            )
            ok += 1
        except Exception as e:
            logger.warning("[Temporal] unmark failed for %s: %s", oid, e)
    return ok


# --- contradiction detection --------------------------------------------------

def detect_conflicts(memory, new_text: str, user_id: str, instance_id: str,
                     exclude_ids: Optional[List[str]] = None) -> List[dict]:
    """Find existing user memories that the new text contradicts.

    Returns list of {"id", "memory"} for conflicting OLD facts (empty if none /
    disabled / any error). Runs a qwen judge per candidate — call from the
    background worker only.
    """
    if not TEMPORAL_ENABLED or not new_text.strip():
        return []
    exclude = set(exclude_ids or [])
    try:
        res = memory.search(
            query=new_text[:1000],
            filters={"user_id": user_id, "instance_id": instance_id},
            top_k=CONFLICT_SEARCH_TOP_K,
            threshold=CONFLICT_SIM_THRESHOLD,
        )
    except Exception as e:
        logger.warning("[Temporal] conflict search failed (non-fatal): %s", e)
        return []
    cands = [
        r for r in (res.get("results", []) if isinstance(res, dict) else res)
        if r.get("id") and r["id"] not in exclude
    ]
    conflicts = []
    for cand in cands:
        verdict = _judge_pair(new_text, cand.get("memory") or cand.get("data") or "")
        if verdict:
            conflicts.append({"id": cand["id"], "memory": cand.get("memory") or cand.get("data")})
    return conflicts


def _judge_pair(new_text: str, old_text: str, attempts: int = 2) -> bool:
    for attempt in range(attempts):
        try:
            resp = (
                _judge()
                .chat.completions.create(
                    model=os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp"),
                    messages=[
                        {"role": "system", "content": CONFLICT_SYSTEM},
                        {"role": "user",
                         "content": f"NEW FACT: {new_text[:600]}\n\n"
                                    f"EXISTING FACT: {old_text[:600]}"},
                    ],
                    temperature=0.0,
                    max_tokens=4,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
            )
            txt = (resp.choices[0].message.content or "").strip().upper()
            m = re.search(r"\b(YES|NO)\b", txt)
            return bool(m and m.group(1) == "YES")
        except Exception as e:
            logger.warning("[Temporal] judge attempt %d failed: %s", attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    return False  # fail open: never supersede on judge failure
