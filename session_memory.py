"""P1 session summaries — sagentv3.md Upgrade 1.

Compresses history messages that fall out of the working-memory char budget
into an evolving per-(agent, user) session summary, persisted NEXT TO the raw
history JSON (never rewriting it — data-safety invariant §7.4).

All LLM work routes to the dedicated qwen card (Revision B): no-thinking
kwargs + non-thinking sampling, never residentb. Calls happen only from the
background memory-worker thread, so interactive chat latency is untouched.

Rollback: SAGENT_SESSION_SUMMARY=0 disables everything here (no client, no
calls, maybe_update_summary becomes a no-op). Raw histories are always intact
on disk regardless.
"""

# ============================================================
# CONTINUA FORK — pinned from ~/Sagent @ e276913 (2026-09-07)
# This file is Continua's copy of the Sagent organ. Sagent stays
# live and untouched; this fork evolves independently per
# ~/agentwiki/projects/Continua.md. Deviations are tagged [CONTINUA].
# ============================================================


import json
import logging
import os
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

# --- configuration -----------------------------------------------------------
SESSION_SUMMARY_ENABLED = os.getenv("SAGENT_SESSION_SUMMARY", "1") == "1"
QWEN_BASE_URL = os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1")
QWEN_MODEL = os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp")
SUMMARY_MAX_CHARS = int(os.getenv("SAGENT_SUMMARY_MAX_CHARS", "3000"))
MAX_EVICTED_CHARS_PER_FOLD = int(os.getenv("SAGENT_SUMMARY_MAX_EVICTED_CHARS", "12000"))

_client = None
_client_lock = threading.Lock()


def _get_client():
    """Lazy singleton OpenAI client bound to the qwen card."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from openai import OpenAI

                _client = OpenAI(
                    base_url=QWEN_BASE_URL,
                    api_key="none",
                    max_retries=0,  # our own bounded retry below
                    timeout=90.0,
                )
    return _client


FOLD_SYSTEM = (
    "You maintain a running SESSION SUMMARY so an assistant remembers earlier "
    "parts of a long conversation that have scrolled out of its visible window.\n"
    "Merge the PRIOR SUMMARY and the NEW EXCERPTS into one updated summary.\n"
    "Keep: topics discussed, decisions made, concrete facts (names, numbers, "
    "dates, places), user preferences, open questions / unfinished threads.\n"
    "Drop: greetings, small talk, filler. Be dense and factual. Use short "
    "bullet-like lines. If the merged content would exceed the length limit, "
    "compress the OLDEST material first and keep newer details specific — but "
    "always finish your last sentence cleanly. Output ONLY the summary text — "
    "no preamble, no quotes."
)


def fold_summary(prior_summary: str, evicted_texts: List[str]) -> Optional[str]:
    """Fold newly-evicted message texts into the running summary via qwen.

    Returns the updated summary text, or None on failure (caller keeps the
    prior summary — degradation, never data loss).
    """
    if not evicted_texts:
        return prior_summary
    # Bound the payload; drop oldest overflow if a huge backlog accumulated.
    joined, used = [], 0
    for t in reversed(evicted_texts):  # newest-first: keep the freshest material
        if used + len(t) > MAX_EVICTED_CHARS_PER_FOLD and joined:
            break
        joined.append(t)
        used += len(t)
    excerpts = "\n".join(f"- {t}" for t in reversed(joined))

    user_msg = (
        f"LENGTH LIMIT: {SUMMARY_MAX_CHARS} characters max.\n"
        f"PRIOR SUMMARY:\n{prior_summary or '(none yet)'}\n\n"
        f"NEW EXCERPTS (oldest to newest):\n{excerpts}\n\n"
        "Output the merged summary only."
    )
    for attempt in range(3):
        try:
            resp = (
                _get_client()
                .chat.completions.create(
                    model=QWEN_MODEL,
                    messages=[
                        {"role": "system", "content": FOLD_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    # Non-thinking sampling per Qwen recommendation
                    # (sagentv3.md Revision B / Model-Endpoints §Thinking Modes)
                    temperature=0.7,
                    top_p=0.80,
                    presence_penalty=1.5,
                    max_tokens=800,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
            )
            text = (resp.choices[0].message.content or "").strip()
            reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
            if reasoning:
                logger.warning(
                    "[SessionSummary] qwen returned reasoning_content despite "
                    "enable_thinking=false — kwarg may be ignored by this model!"
                )
            if not text:
                raise ValueError("empty summary response")
            if len(text) > SUMMARY_MAX_CHARS:
                # Soft-clip at a sentence/line boundary instead of a hard cut
                text = text[:SUMMARY_MAX_CHARS]
                for sep in ("\n- ", ". ", "."):
                    cut = text.rfind(sep)
                    if cut > SUMMARY_MAX_CHARS // 2:
                        text = text[:cut + len(sep.strip())].rstrip(" -")
                        break
            return text
        except Exception as e:
            logger.warning(
                "[SessionSummary] fold attempt %d failed: %s", attempt + 1, e
            )
            time.sleep(2 * (attempt + 1))
    return None


# --- persistence -------------------------------------------------------------
def summary_filepath(history_filepath: str) -> str:
    """Summary lives NEXT TO the raw history JSON; raw files stay untouched."""
    return history_filepath + ".summary.json"


def load_summary(history_filepath: str) -> str:
    """Return the current summary text ('' if none/disabled/corrupt)."""
    if not SESSION_SUMMARY_ENABLED:
        return ""
    try:
        with open(summary_filepath(history_filepath), "r", encoding="utf-8") as f:
            return json.load(f).get("summary", "") or ""
    except (OSError, json.JSONDecodeError):
        return ""


def clear_summary(history_filepath: str) -> None:
    """Remove the summary file (used by /clear). Best-effort."""
    try:
        os.remove(summary_filepath(history_filepath))
    except FileNotFoundError:
        pass


def _save_summary_atomic(path: str, summary: str, n_summarized: int, pending_evicted: Optional[List[str]] = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": summary,
                "n_summarized": n_summarized,
                "pending_evicted": list(pending_evicted or []),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "schema": 1,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _msg_text(content) -> str:
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def maybe_update_summary(history_filepath: str, history: list,
                         evicted_messages: Optional[list] = None) -> None:
    """Background-thread hook: fold messages that fell out of the trim budget
    into the persistent summary.

    M1 (memfixes82826.md) — two paths:

    1. Evicted-threaded (preferred). ``evicted_messages`` carries the exact
       messages the request thread just trimmed out of the working window
       (core passes them through the memory-worker queue). They are folded
       directly — no index cursor, so the fold can never silently wedge when
       history files are pruned/rebuilt externally (sagent_default sat at
       n_summarized=36 > window=32 for days → zero folds). On fold failure
       (qwen card down) the texts are parked in ``pending_evicted`` and
       merged into the next successful fold — the old cursor design's retry
       semantics survive.

    2. Legacy cursor path (callers that don't pass evicted messages).
       Stale cursors (start > window) are clamped instead of early-returning
       forever, so folds resume after external history churn.

    Never raises — summary failure must not break the memory worker loop.
    """
    if not SESSION_SUMMARY_ENABLED or not history_filepath:
        return
    path = summary_filepath(history_filepath)
    state = {"summary": "", "n_summarized": 0, "pending_evicted": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            state["summary"] = loaded.get("summary", "") or ""
            state["n_summarized"] = int(loaded.get("n_summarized", 0))
            state["pending_evicted"] = list(loaded.get("pending_evicted", []) or [])
    except (OSError, json.JSONDecodeError):
        pass

    if evicted_messages is not None:
        # --- M1 evicted-threaded path -----------------------------------
        pending = list(state["pending_evicted"])

        def _dated_text(m):
            """HISTTS (2026-09-05): anchor evicted texts with their date so
            folded summaries can say 'On Sep 2, ...' instead of an unanchored
            'the user said ...'. Date-only — summaries are coarse."""
            text = _msg_text(m.get("content")).strip()
            if not text:
                return ""
            ts = m.get("ts")
            if ts:
                try:
                    from datetime import datetime as _dt
                    return f"[{_dt.fromisoformat(str(ts)).strftime('%Y-%m-%d')}] {text}"
                except (ValueError, TypeError):
                    return text
            return text

        new_texts = [_dated_text(m) for m in (evicted_messages or [])]
        new_texts = [t for t in new_texts if t]
        to_fold = pending + new_texts
        if to_fold:
            updated = fold_summary(state["summary"], to_fold)
            if updated is not None:
                try:
                    _save_summary_atomic(path, updated, len(history), pending_evicted=[])
                    logger.info(
                        "[SessionSummary] folded %d msgs (%s chars, incl %d parked) for %s (len=%d)",
                        len(to_fold), sum(len(t) for t in to_fold), len(pending),
                        os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path),
                        len(updated),
                    )
                except OSError as e:
                    logger.warning("[SessionSummary] persist failed: %s", e)
            else:
                # Fold failed — park ALL not-yet-summarized texts for the next
                # turn. Cap the debt at the 12 newest texts so a long outage
                # can't balloon the file; older material was already dropped
                # from the window and is additionally covered by mem0 facts.
                parked = to_fold[-12:]
                try:
                    _save_summary_atomic(path, state["summary"], len(history), pending_evicted=parked)
                    logger.warning(
                        "[SessionSummary] fold failed; %d evicted texts parked for retry path=%s",
                        len(parked), path,
                    )
                except OSError as e:
                    logger.warning("[SessionSummary] persist failed (retry debt lost): %s", e)
            return
        # Nothing evicted this turn — still re-sync the cursor to the current
        # window length so the legacy path never folds window content again
        # (this is what heals the stale sagent_default cursor).
        if state["n_summarized"] != len(history):
            try:
                _save_summary_atomic(path, state["summary"], len(history),
                                     pending_evicted=pending)
            except OSError:
                pass
        return

    # --- Legacy cursor path --------------------------------------------
    n = len(history)
    # Safety floor mirrors core._trim_history: keep the last 4 out of bounds.
    foldable_end = max(0, n - 4)
    start = state["n_summarized"]
    if start > foldable_end:
        # memfixes M1: stale cursor (window pruned/rebuilt externally).
        # Clamp instead of early-returning forever so folds resume.
        logger.warning(
            "[SessionSummary] stale cursor %d > foldable end %d; clamping path=%s",
            start, foldable_end, path,
        )
        start = foldable_end
    if foldable_end <= start:
        return  # nothing new beyond what's already folded

    evicted = [
        _msg_text(m.get("content")).strip()
        for m in history[start:foldable_end]
    ]
    evicted = [t for t in evicted if t]
    if not evicted:
        state["n_summarized"] = foldable_end
        try:
            _save_summary_atomic(path, state["summary"], foldable_end)
        except OSError:
            pass
        return

    updated = fold_summary(state["summary"], evicted)
    if updated is None:
        logger.warning(
            "[SessionSummary] fold failed; keeping prior summary "
            "(will retry on next turn) path=%s", path,
        )
        return  # leave n_summarized untouched -> retried next turn

    try:
        _save_summary_atomic(path, updated, foldable_end)
        logger.info(
            "[SessionSummary] folded %d msgs (%s chars) for %s (len=%d)",
            len(evicted), sum(len(t) for t in evicted),
            os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path),
            len(updated),
        )
    except OSError as e:
        logger.warning("[SessionSummary] persist failed: %s", e)
