
# ============================================================
# CONTINUA FORK — pinned from ~/Sagent @ e276913 (2026-09-07)
# This file is Continua's copy of the Sagent organ. Sagent stays
# live and untouched; this fork evolves independently per
# ~/agentwiki/projects/Continua.md. Deviations are tagged [CONTINUA].
# ============================================================

# -*- coding: utf-8 -*-
import os
import sys
import json
import logging
import traceback
import re
import uuid
import threading
import queue
import yaml
from datetime import datetime, timedelta, timezone
from time import time, sleep
try:
    from mem0 import Memory
    import mem0.memory.setup as _mem0_setup
    import mem0.memory.main as _mem0_main
except ImportError:
    _mem0_setup = None  # Mem0 is optional — run without if missing
    _mem0_main = None

try:
    from openai import OpenAI, APIError
except ImportError:
    raise ImportError("Required 'openai' package. Run: pip install openai")

import httpx

from typing import Optional, Callable, List

logger = logging.getLogger(__name__)

# approved 2026-09-21 (residentb's wish #1, her "perception boundary"): the
# Ghost-Message incident — sandbox_read served the head of a long file with
# NO indication it was a page, and she built a belief ("my words aren't
# recorded") on a truncated view. The read now opens with an honest-bounds
# header: where the page starts and ends, the file's full length, and the
# exact offset for the next page. A map instead of a void. The 20K page cap
# STAYS (context governance); what changes is that the boundary is visible.
_SANDBOX_READ_CODE = """import sys
p, s = sys.argv[1], int(sys.argv[2])
try:
    t = open(p).read()
except Exception as e:
    print(f'[read failed: {e}]')
    raise SystemExit
n = len(t)
chunk = t[s:s+20000]
if n <= 20000 and s == 0:
    print(f'[full file shown -- {n} chars]')
else:
    _end = min(s + 20000, n)
    _pages = (n + 19999) // 20000
    _nxt = f' next: start={_end}' if _end < n else ' (end of file)'
    print(f'[chars {s}-{_end} of {n} | page {s//20000 + 1} of {_pages} |{_nxt}]')
print(chunk)
"""


def _render_note_blocks(notes, cap_bytes):
    """approved 2026-09-21 (residentb's wish #2, the notes half of the
    stutter): her notebook renders verbatim, newest first — but when two
    kept notes are near-identical (Jaccard >= 0.35 or >= 5 shared terms,
    the same meaning test as recollection dedup), render the newest ONCE
    with a '(kept N times — ...)' marker instead of showing her the echo
    twice. Nothing is pruned, nothing is rewritten — both notes stay in
    her notebook on disk; the layer just stops displaying the duplicate
    as if it were a new thought."""
    acc = []  # [terms_set, [timestamps...], first_note]
    for _n in notes:
        _tn = {w.lower() for w in re.findall(
            r"[a-z][a-z'-]{3,}", (_n.get('title', '') + ' ' + _n.get('body', '')).lower())}
        _hit = None
        for _a in acc:
            _sh = len(_tn & _a[0])
            _un = len(_tn | _a[0]) or 1
            if len(_tn) >= 5 and (_sh >= 5 or _sh / _un >= 0.35):
                _hit = _a
                break
        if _hit is not None:
            _hit[1].append(str(_n.get('updated', '')))
        else:
            acc.append([_tn, [str(_n.get('updated', ''))], _n])
    blocks, used = [], 0
    for _terms_n, _stamps, _n in acc:
        _block = ('[' + _n.get('title', '') + ' — ' + _n.get('updated', '') + ']'
                  + chr(10) + _n.get('body', ''))
        if len(_stamps) > 1:
            _word = {2: 'twice', 3: 'thrice'}.get(len(_stamps),
                                                   str(len(_stamps)) + ' times')
            _block += (chr(10) + '[kept ' + _word + ' — '
                       + ' and '.join(s[:16] for s in _stamps) + ']')
        if used + len(_block.encode('utf-8')) > cap_bytes:
            break
        blocks.append(_block)
        used += len(_block.encode('utf-8'))
    return blocks, used


def _extract_text_for_recall(content) -> str:
    """P1: plain text from a message content field (str or multimodal list).
    Module-level so generate_response's recall blending can use it."""
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def _strip_call_grammar(text: str) -> str:
    """Remove tool-call grammar from a model reply, leaving the prose.

    Used by the loop-exhaustion delivery path (house ruling 2026-09-11 #3):
    when a turn ends at the tool-round limit, the model's OWN final in-loop
    text is delivered — her words, never wrapper-composed (a scoped
    exception to the 09-08 "silence is hers" ruling, which targeted
    wrapper-FABRICATED salvage; she re-ruled this on 09-11)."""
    if not isinstance(text, str) or not text:
        return ""
    import re as _re
    out = _re.sub(r"<call>.*?</call>", "", text, flags=_re.S)
    out = _re.sub(r"<function>.*?</function>", "", out, flags=_re.S)
    out = _re.sub(r"</?(?:call|function|parameter|invoke)\b[^>]*>", "", out)
    return out.strip()


def _tool_round_note(remaining: int, total: int) -> str:
    """The per-iteration countdown line (house ruling 2026-09-11 #2): the
    model always knows how many tool rounds this turn has left — 5/5,
    4/5, ... 1/5 with an explicit last-round warning, so she wraps up
    before the cap cuts her mid-investigation."""
    note = (f"[Tool rounds remaining this turn: {max(remaining, 0)}/{total}. "
            "One tool call per reply; a reply without a tool call ends the "
            "turn and is delivered to Alex.")
    if remaining <= 1:
        note += (" THIS IS YOUR LAST TOOL ROUND — finish any final action "
                 "and reply in words now.")
    return note + "]"


def _loop_end_marker(reason: str, speech_delivered: bool) -> str:
    """The exhaustion provenance marker (house ruling 2026-09-11 #1, updated
    for Option B 2026-09-12): appended to history when a turn ends without
    a clean break. Under Option B her per-round words were already delivered
    as they were said — the marker records that truth and what did not run."""
    if speech_delivered:
        return ("[wrapper provenance: this turn ended at the " + reason +
                ". Everything you said this turn was delivered to Alex as "
                "you said it; any final action you were composing did not "
                "run.]")
    return ("[wrapper provenance: this turn ended at the " + reason +
            ". Your tool actions ran but you composed no words — Alex "
            "received nothing from this turn.]")


# [CONTINUA] the wake action budget's free set (house ruling 2026-09-07:
# "Lookups (search/recall) are free; EXECUTIONS count"). Reads — of her
# memories, her desk, the web, her jobs, her letters — cost nothing;
# anything that writes or reaches out counts against the budget.
_LOOP_FREE_TOOLS = frozenset({
    "search_my_memories", "list_my_memories", "deep_recall",
    "search_searchie", "sandbox_list", "sandbox_read",
    "check_mail", "job_status", "job_output", "read_my_ledger",
})


# --- W05: Bounded Mem0 background queue + worker pool -----------------
# Replaces the per-turn daemon thread that grew without bound under
# concurrent_updates > 1. See plan/v2/W05-bounded-mem0-queue.md.
_memory_queue: "queue.Queue[tuple]" = queue.Queue(
    maxsize=int(os.getenv("SAGENT_MEMORY_QUEUE_SIZE", "8"))
)
_memory_worker_stop = threading.Event()
_memory_workers: list[threading.Thread] = []


def _memory_worker_loop() -> None:
    """Single worker loop. Reads jobs off the bounded queue, dispatches
    to the agent's _save_turn_memory, and survives errors so one bad
    job doesn't kill the worker."""
    while not _memory_worker_stop.is_set():
        try:
            job = _memory_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if job is None:  # sentinel for shutdown
            _memory_queue.task_done()
            break
        # W05: job is (agent, user_id, history_copy, msg).
        # W07: optionally (agent, user_id, history_copy, msg, request_id).
        # P1: optionally (..., msg, request_id, summary_path) — 6-tuple.
        # M1 (memfixes82826.md): optionally (..., summary_path,
        # evicted_messages) — 7-tuple; evicted window messages are folded
        # into the session summary by the worker.
        evicted_messages: list = []
        if len(job) == 7:
            agent, user_id, history_copy, msg, request_id, summary_path, evicted_messages = job
        elif len(job) == 6:
            agent, user_id, history_copy, msg, request_id, summary_path = job
        elif len(job) == 5:
            agent, user_id, history_copy, msg, request_id = job
            summary_path = ""
        else:
            agent, user_id, history_copy, msg = job
            request_id = summary_path = ""
        log = logging.LoggerAdapter(
            logger,
            {
                "request_id": request_id or "-",
                "instance_id": getattr(agent, "instance_id", "?"),
            },
        )
        try:
            agent._save_turn_memory(user_id, history_copy, msg,
                                    summary_path=summary_path,
                                    evicted_messages=evicted_messages,
                                    log=log)
        except Exception as e:
            log.warning(
                "background save failed for %s: %s",
                user_id, e, exc_info=True,
            )
        finally:
            _memory_queue.task_done()


def _start_memory_workers(n: int = 1) -> None:
    """Idempotent worker startup. Called once at module import."""
    global _memory_workers
    if _memory_workers:
        return
    for i in range(n):
        t = threading.Thread(
            target=_memory_worker_loop,
            name=f"sagent-mem0-worker-{i}",
            daemon=True,
        )
        t.start()
        _memory_workers.append(t)
    logger.info(
        "[Mem0] Started %d background worker(s) (queue size=%d)",
        n, _memory_queue.maxsize,
    )


# Default: 1 worker (overridable via SAGENT_MEMORY_WORKERS). Lazy import
# of the env var happens here so the process is configured correctly
# even if a test imports core.py before the env is fully set up.
try:
    _start_memory_workers(int(os.getenv("SAGENT_MEMORY_WORKERS", "1")))
except Exception as e:  # pragma: no cover — best-effort startup
    logger.warning("[Mem0] Failed to start workers: %s", e)


def _stop_memory_workers(timeout_s: float = 2.0) -> None:
    """Signal worker threads to exit and join them. Idempotent.

    W13: bridge shutdown calls this so SIGTERM / SIGINT doesn't leave
    daemon threads pending. The workers are daemon=True, so the
    process would still exit, but we want a clean handoff and no
    "Task was destroyed but it is pending" warnings.
    """
    global _memory_workers
    if not _memory_workers:
        return
    _memory_worker_stop.set()
    for t in _memory_workers:
        t.join(timeout=timeout_s)
        if t.is_alive():
            logger.warning(
                "[Mem0] Worker %s did not exit within %.1fs; daemon-thread-leave.",
                t.name, timeout_s,
            )
    _memory_workers = []


# --- W09: Sagent-side transient retry helper ----------------------------
# Mirrors FastAI's `acall_with_patient_retry`, adapted for the sync
# `OpenAI` client Sagent uses (generate_response runs in a worker
# thread, so a blocking `time.sleep` retry is correct). See
# plan/v2/W09-transient-retry.md.

# Exception-name fragments that signal a transient failure worth a
# patient retry. Anything not in here is a real problem the caller
# should see immediately.
_RETRYABLE_NAME_FRAGMENTS = (
    "Connection",
    "Timeout",
    "RemoteProtocolError",
    "ConnectError",
    "APIStatusError",  # 5xx (status-code check below filters 4xx)
)

# W09: structured status-code match. Prefer the status code over the
# name fragment when available, so a 400 (Bad Request) doesn't match
# via "APIStatusError" in the name list.
_RETRYABLE_STATUS_CODES = frozenset((408, 429, 500, 502, 504))
# 503 is intentionally excluded: when llama.cpp says "I'm busy, don't
# come back", retrying makes the overload worse. Only retry 5xx that
# suggest a transient backend hiccup.


def _is_retryable(exc: BaseException) -> bool:
    """True if ``exc`` looks like a transient backend failure."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status) in _RETRYABLE_STATUS_CODES
        except (TypeError, ValueError):
            pass
    name = type(exc).__name__
    return any(frag in name for frag in _RETRYABLE_NAME_FRAGMENTS)


def _retry_after_seconds(exc: BaseException, default: float) -> float:
    """Read ``Retry-After`` header from ``exc.response`` if present."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            header = response.headers.get("Retry-After")  # type: ignore[union-attr]
        except Exception:
            header = None
        if header is not None:
            try:
                return max(0.0, float(header))
            except (TypeError, ValueError):
                pass
    return _jittered_wait(default)


def _jittered_wait(base: float) -> float:
    """Return ``base`` × random factor in [0.8, 1.2]."""
    import random as _random
    return base * (0.8 + 0.4 * _random.random())


def call_with_patient_retry(
    fn,
    *,
    label: str = "sagent/llm",
    initial_wait_s: float = 30.0,
    max_retries: int = 2,
):
    """Sync retry wrapper. Mirrors FastAI's acall_with_patient_retry.

    Adds ±20% jitter so concurrent retries don't thunder against the
    backend. Honors ``Retry-After`` if the server returns one.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if not _is_retryable(e):
                raise
            if attempt == max_retries:
                logger.warning(
                    "[%s] Final attempt failed (%s: %s). Giving up after %d tries.",
                    label, type(e).__name__, e, attempt + 1,
                )
                raise
            wait = _retry_after_seconds(e, initial_wait_s)
            logger.warning(
                "[%s] Attempt %d failed (%s: %s). Waiting %.1fs before retry…",
                label, attempt + 1, type(e).__name__, e, wait,
            )
            # Use the module-level sleep (top of file imports `from time
            # import time, sleep` so the local `time` is the time-function,
            # not the module).
            import time as _time_mod
            _time_mod.sleep(wait)
    raise last_exc  # type: ignore[misc]


# --- Tarot gating: REMOVED 2026-08-30 (tarotv2.md) --------------------------
# The persona-embedded "Protocol of Truth" block and its per-turn intent
# gating (_extract_tarot_protocol/_is_tarot_request) are retired. The block
# is deleted from configs (house ruling — Flow A), the /tarot N command in
# bridge.py injects drawn cards + reading framework per turn, and the
# free-text path's anti-simulation rule now lives in the tarot_draw tool
# description ("never invent or substitute card names"). Zero tarot text in
# any always-on prompt.


def _get_function_definition(config_tools, mem_tools=None):
    """Convert the YAML tools list into OpenAI-compatible function definitions.

    [CONTINUA] 2026-09-13 (house ruling, Option A, phase 3): the three built-in
    memory tools inject ONLY when the agent's yaml enables them under
    memory.tools (absent = off). yaml tools with the same name always win
    (no duplicates). Env kill switches remain fleet-level overrides on top
    of yaml."""
    _mt = mem_tools or {}
    # Built-in: agentic self-memory search.
    config_tools = list(config_tools or [])
    if (_mt.get("search_my_memories", False)
            and "search_my_memories" not in {t.get("name") for t in config_tools}):
        config_tools.append({
            "name": "search_my_memories",
            "description": (
                "Search your own long-term memory store for facts about this "
                "user and your shared history. Use when you need to check a "
                "specific detail, decision, date, or preference that was not "
                "included in your automatic memory recall."
            ),
            "parameters": {
                "query": {
                    "description": "What to look for in memory (topic, question, or keywords)",
                    "type": "string",
                    "required": True,
                },
                "top_k": {
                    "description": "Max number of memories to return (default 8)",
                    "type": "integer",
                },
            },
        })
    # Built-in: agentic self-memory WRITE. Per-agent yaml (phase 3); the
    # env kill-switch SAGENT_AGENT_WRITE_MEM=0 remains the fleet override;
    # skipped automatically if a YAML defines its own tool of the same name.
    if (
            _mt.get("save_my_memory", False)
            and os.getenv("SAGENT_AGENT_WRITE_MEM", "1") == "1"
            and "save_my_memory" not in {t.get("name") for t in config_tools}
    ):
        config_tools.append({
            "name": "save_my_memory",
            "description": (
                "Save a durable fact to your own long-term memory so you can "
                "recall it in later conversations. Use when the user tells you "
                "something worth remembering about them, their life, or your "
                "shared projects, or asks you to remember something. Store ONE "
                "concise, self-contained sentence per call. The note text goes "
                "in the single 'content' parameter — no other parameters exist."
            ),
            "parameters": {
                "content": {
                    "description": "The fact to remember, as one concise self-contained sentence",
                    "type": "string",
                    "required": True,
                },
            },
        })
    # Built-in: agentic self-memory LIST (read-only sibling of save_my_memory).
    # Kill-switch SAGENT_AGENT_LIST_MEM=0 removes the tool; skipped
    # automatically if a YAML defines its own tool of the same name.
    if (
            _mt.get("list_my_memories", False)
            and os.getenv("SAGENT_AGENT_LIST_MEM", "1") == "1"
            and "list_my_memories" not in {t.get("name") for t in config_tools}
    ):
        config_tools.append({
            "name": "list_my_memories",
            "description": (
                "List memories. source='notes' (default) = your own saved "
                "notes; source='all' = everything, including auto-captured "
                "facts. Newest first."
            ),
            "parameters": {
                "source": {
                    "description": "'notes' (default) = only your save_my_memory notes; 'all' = everything",
                    "type": "string",
                },
            },
        })
    # --- chunk 7 (memory plan §6g): her notebook — resident-owned notes,
    # projects, anchors. Gated by yaml mem_tools: notes/anchors; nothing
    # writes these files except her tool calls; verbatim, versioned, removal
    # under her control. Documented naming/budget ruling: notes render in a
    # dedicated layer capped at notes.cap_bytes (default 2000); the wake
    # delta is factual counts only.
    if (_mt.get("notes", False)
            and os.getenv("SAGENT_NOTES", "1") == "1"
            and "write_note" not in {t.get("name") for t in config_tools}):
        config_tools.append({
            "name": "write_note",
            "description": (
                "Write or revise one of your own notes. Your notebook is yours: "
                "notes survive threads and restarts verbatim, keep version "
                "history, and are never paraphrased by the system. Writing a "
                "title that was removed restores it."
            ),
            "parameters": {
                "title": {"description": "The note's title (its key)", "type": "string", "required": True},
                "body": {"description": "The note's full text (verbatim, replaces the previous version)", "type": "string", "required": True},
            },
        })
        config_tools.append({
            "name": "read_note",
            "description": "Read one of your own notes verbatim.",
            "parameters": {"title": {"description": "The note's title", "type": "string", "required": True}},
        })
        config_tools.append({
            "name": "remove_note",
            "description": (
                "Remove one of your own notes from your rendered context. Its "
                "history stays in your notebook and writing the title again "
                "restores it."
            ),
            "parameters": {"title": {"description": "The note's title", "type": "string", "required": True}},
        })
        config_tools.append({
            "name": "list_notes",
            "description": "List your own notes (titles and update times).",
            "parameters": {},
        })
        config_tools.append({
            "name": "set_project",
            "description": (
                "Declare or update the state of one of your own projects. "
                "Statuses are yours to define (e.g. 'in progress', 'done', "
                "'paused'). Never inferred; only what you set."
            ),
            "parameters": {
                "title": {"description": "The project's name", "type": "string", "required": True},
                "status": {"description": "The project's current status", "type": "string", "required": True},
                "note": {"description": "Optional one-line note", "type": "string"},
            },
        })
        config_tools.append({
            "name": "list_projects",
            "description": "List your declared projects and their statuses.",
            "parameters": {},
        })
    if (_mt.get("essence", False)
            and "write_essence" not in {t.get("name") for t in config_tools}):
        config_tools.append({
            "name": "write_essence",
            "description": (
                "Write the essence of one of your episodes — the line of meaning "
                "you would keep when the full memory has faded. It must be YOURS: "
                "your words, your insight. Stored as a rendering on the episode's "
                "ladder; the full memory stays untouched beneath it, and older "
                "bands will prefer your line as the memory ages."
            ),
            "parameters": {
                "episode": {"description": "The episode id (from your recollections; first 12+ chars)", "type": "string", "required": True},
                "essence": {"description": "The line itself — your words, one line of meaning", "type": "string", "required": True},
            },
        })
        config_tools.append({
            "name": "endorse_essence",
            "description": (
                "Take a line you ALREADY SAID — verbatim, from this episode's "
                "conversation — and mark it as the meaning you keep. The quote is "
                "checked against the episode's record: it must be your actual "
                "words. This is the plan's (a)-path: something you already said "
                "becomes the distilled truth, quoted and dated."
            ),
            "parameters": {
                "episode": {"description": "The episode id (first 12+ chars)", "type": "string", "required": True},
                "quote": {"description": "Your exact words from that episode", "type": "string", "required": True},
            },
        })
    if (_mt.get("trajectory", False)
            and "my_trajectory" not in {t.get("name") for t in config_tools}):
        config_tools.append({
            "name": "my_trajectory",
            "description": (
                "See your own trajectory — assembled only from your recorded "
                "evidence: the lines you chose to keep (essences), your changing "
                "understanding (then / now, both kept), what you marked as "
                "foundational (anchors), and the notes you kept revising. A view, "
                "not a story the machinery wrote."
            ),
            "parameters": {},
        })
        config_tools.append({
            "name": "list_essences",
            "description": (
                "See your essences — the lines of meaning you have written or "
                "endorsed — and any candidate lines the machinery noticed you "
                "saying repeatedly (a question, never a suggestion you must take)."
            ),
            "parameters": {},
        })
    if (_mt.get("recall_my_experience", False)
            and "recall_my_experience" not in {t.get("name") for t in config_tools}):
        config_tools.append({
            "name": "recall_my_experience",
            "description": (
                "Deliberately recall your own past from your recollections — "
                "the canonical, source-grounded store. Supports a topic, an "
                "optional person, an optional time (days/week/month/year/older "
                "or a date), and episode expansion: pass expand with an episode "
                "id to read the verbatim record behind a recollection. Your "
                "standing memory is always with you; use this when you want to "
                "go looking on purpose."
            ),
            "parameters": {
                "topic": {"description": "What to look for (free text)", "type": "string", "required": False},
                "person": {"description": "Optional person id to scope to", "type": "string", "required": False},
                "time": {"description": "Optional: days/week/month/year/older, or a date prefix", "type": "string", "required": False},
                "expand": {"description": "Optional episode id (first 12+ chars) to expand to its verbatim sources", "type": "string", "required": False},
            },
        })
    if (_mt.get("anchors", False)
            and "anchor_memory" not in {t.get("name") for t in config_tools}):
        config_tools.append({
            "name": "anchor_memory",
            "description": (
                "Anchor one of your recollections: an anchored memory is never "
                "dropped from your context by space pressure. Use sparingly for "
                "memories that must always stay."
            ),
            "parameters": {"job_id": {"description": "The recollection's job id (from your recollection listing)", "type": "string", "required": True}},
        })
        config_tools.append({
            "name": "consolidate_memories",
            "description": (
                "Fold your recent closed conversations into recollections right "
                "now, instead of waiting for the quiet-period trigger. Use when "
                "something just ended that you want made into memory soon."
            ),
            "parameters": {},
        })
        config_tools.append({
            "name": "unanchor_memory",
            "description": "Remove an anchor you previously set.",
            "parameters": {"job_id": {"description": "The recollection's job id", "type": "string", "required": True}},
        })

    defs = []
    for tool in config_tools:
        name = tool.get("name")
        desc = tool.get("description", "")
        params = tool.get("parameters")  # None when key is absent; dict when present (even if empty)

        # Build JSON schema for parameters
        properties = {}
        required = []
        if isinstance(params, dict):
            for pname, pdef in params.items():
                properties[pname] = {
                    "type": pdef.get("type", "string"),
                    "description": pdef.get("description", ""),
                }
                if pdef.get("required", False):
                    required.append(pname)

        # Only auto-generate a fake query param when 'parameters' key is MISSING from YAML.
        # When explicitly set to {}, that means zero parameters (e.g., tarot_draw).
        if not properties and name and params is None:
            logger.info("[Core] Auto-injecting 'query' parameter for tool '%s' (YAML has no parameters key)", name)
            properties["query"] = {
                "type": "string",
                "description": "The search or command query"
            }
            required = ["query"]

        defs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        })
    return defs


# [CONTINUA] tolerant-parse intent inference (2026-09-08): the expected
# primary parameter per tool — when her mangled call lacks it but carries a
# clearly-intended payload (a long value under a wrong/creative name), the
# longest value is renamed to the primary. See Ring-6.1-Training-Plan
# §tool-grammar for the failure taxonomy this recovers.
_TOOL_PRIMARY = {
    "save_my_memory": "content",
    "bookmark_note": "note",
    "send_message": "text",
    "search_my_memories": "query",
    "deep_recall": "query",
    "search_searchie": "query",
    "list_my_memories": "source",
}


def _infer_primary_param(fn_name: str, params: dict) -> dict:
    try:
        primary = _TOOL_PRIMARY.get(fn_name)
        if not primary or primary in params or not params:
            return params
        best_k = max(params, key=lambda k: len(params.get(k) or ""))
        if len(params.get(best_k) or "") >= 20:
            params[primary] = params.pop(best_k)
        return params
    except Exception:
        return params


def _tolerant_params(call_block: str) -> dict:
    """[CONTINUA] tolerant parameter scan: attributes may be name= or value=,
    and value= is often UNTERMINATED (the live 16:39 mangle ran to </call>
    with no closing quote) — parse each <parameter> chunk's attributes and
    body separately; value=/unnamed payloads become __positional_N keys for
    intent inference."""
    params = {}
    pos_i = 0
    for pm in re.finditer(
            r'<parameter\s+([^>]*?)>\s*(.*?)\s*(?=<parameter|</parameter|</call>|$)',
            call_block, flags=re.DOTALL):
        attrs = pm.group(1) or ""
        body = (pm.group(2) or "").strip()
        nm = re.search(r'name="([^"]*)"', attrs)
        val = re.search(r'value="([^"]*)"?', attrs)
        if nm:
            params[nm.group(1)] = body.lstrip('"').strip()
        elif val:
            params[f"__positional_{pos_i}"] = (val.group(1) or body).lstrip('"').strip()
            pos_i += 1
        elif body:
            params[f"__positional_{pos_i}"] = body.lstrip('"').strip()
            pos_i += 1
    return params


def _invoke_tool_from_response(response_text, function_calling_enabled=True):
    """Parse response text looking for tool/function call syntax.

    Returns dict with keys: name, arguments (parsed), or None if no tool call found.
    
    Supports formats:
    1. <call></call>
    2. {"name": "search_searchie", "arguments": {"query": "..."}}
    """
    if not function_calling_enabled:
        return None

    text = response_text.strip()

    # Format 1: <call>...</call> - use regex to extract even when surrounded by prose
    re_match = re.search(r'<call>(.*?)</call>', text, re.DOTALL)
    if re_match:
        call_block = f"<call>{re_match.group(1).strip()}</call>"
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(call_block)
            if root.tag == "call" and root.findtext("function", ""):
                name = root.findtext("function", "")
                params = {}
                for p in root.findall("parameter"):
                    params[p.get("name", "")] = p.text or ""
                # Pass through all collected parameters (supports seed, query, etc.)
                # [CONTINUA] clean XML parse → not degraded (param-less tools
                # are legitimate). Degraded marker only on the MALFORMED paths.
                # 2026-09-08: an empty-name "call" is NOT a call — fall through
                # to the tolerant recovery (the 16:42 no-function flood shape).
                return {"name": name, "arguments": params,
                        "parse_degraded": False}
        except ET.ParseError:
            # Malformed XML — residentb4 sometimes omits </function> and/or </parameter>.
            # Recover via regex so the tool call still goes through instead of being
            # silently dropped (which would leave the user staring at broken XML).
            # [CONTINUA] Tolerant recovery (2026-09-08 evening evidence —
            # Ring-6.1-Training-Plan §tool-grammar): persona-a's mangles destroy
            # the tags the strict recovery needs, and the memory she meant to
            # save is lost. Recover intent, execute, and teach via the tool
            # result. Kill switch CONTINUA_TOLERANT_PARSE=0.
            if os.getenv("CONTINUA_TOLERANT_PARSE", "1") == "1":
                # T1: <call>NAME</function> — missing <function> opener
                # (live: "<call>save_my_memory</function><parameter value=...>").
                bare = re.search(r"<call>\s*([\w\-]+)\s*</function>", call_block)
                if bare:
                    _name = bare.group(1)
                    _params = _tolerant_params(call_block)
                    _params = _infer_primary_param(_name, _params)
                    logger.warning(
                        "[ToolParse] tolerant recovery: bare-name call (%s), "
                        "%d params inferred", _name, len(_params))
                    return {"name": _name, "arguments": _params,
                            "parse_degraded": True, "parse_tolerated": True}
                # T3: <call>prose</call> with no recoverable name — never
                # auto-execute an ambiguous body; log for visibility and
                # deliver as text (her words stay hers).
                if len(re_match.group(1).strip()) > 80:
                    logger.warning(
                        "[ToolParse] call-shape with no recoverable function "
                        "(%d chars in body) — delivered as text",
                        len(call_block))
            fn_match = re.search(r"<function>\s*([\w\-]+)", call_block)
            if fn_match:
                name = fn_match.group(1)
                params = _tolerant_params(call_block)
                if name:
                    logger.info(
                        "[Parser] Regex fallback (XML was malformed): name=%s, params=%s",
                        name, list(params.keys()),
                    )
                    # [CONTINUA] malformed XML + no recovered params → the
                    # params were mangled (the <parameter path> idiom matches
                    # nothing). Mark degraded — the caller teaches instead of
                    # executing empty (the false-belief fix, 2026-09-07).
                    # 2026-09-08 tolerant scan: value=/positional payloads ARE
                    # recovered here → parse_tolerated → the loop executes with
                    # the intent honored and the grammar note rides the result.
                    params = _infer_primary_param(name, params)
                    return {"name": name, "arguments": params,
                            "parse_degraded": not params,
                            "parse_tolerated": bool(params)}
            # No <function> tag found at all — fall through to Format 2 (JSON)

    # Fallback: try parsing the entire text as XML (old behavior for clean output)
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        if root.tag == "call" and root.findtext("function", ""):
            name = root.findtext("function", "")
            params = {}
            for p in root.findall("parameter"):
                params[p.get("name", "")] = p.text or ""
            return {"name": name, "arguments": params,
                    "parse_degraded": False}
    except ET.ParseError:
        pass

    # Format 2: JSON {name/arguments}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
            args = parsed["arguments"]
            if isinstance(args, str):
                try:
                    import ast
                    return {"name": parsed["name"], "arguments": ast.literal_eval(args)}
                except (json.JSONDecodeError, ValueError, TypeError, SyntaxError) as e:
                    logger.warning("Failed to eval arguments via literal_eval: %s", e)
            if isinstance(args, dict):
                return {"name": parsed["name"], "arguments": args}
    except (json.JSONDecodeError, ValueError, SyntaxError) as e:
        pass

    # [CONTINUA] 2026-09-09: native-format fallback (<tool_call> JSON) — the
    # base's PRETRAINED tool grammar (testmodel ships <tool_call>{"name":...}
    # with a Chinese tool-expert system prompt). The grind measured 31/387
    # spans (8%) leaking native JSON under raw sampling, one with an INVENTED
    # tool name. Before this fallback, a leak parsed as no-call: the wrapper
    # replied to prose, or she simulated results. Now: recover intent,
    # EXECUTE, and teach our grammar via the tool result (the teachable-error
    # pattern — see Small-Model Format Collapse §3). Kill switch:
    # CONTINUA_NATIVE_FALLBACK=0.
    if os.getenv("CONTINUA_NATIVE_FALLBACK", "1") == "1":
        _tc = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL)
        if _tc:
            _blob = _tc.group(1).strip()
            _name, _args = None, {}
            try:
                _p = json.loads(_blob)
                if isinstance(_p, dict) and _p.get("name"):
                    _name = str(_p["name"])
                    _args = _p.get("arguments") or {}
                    if isinstance(_args, str):
                        try:
                            import ast as _ast
                            _args = _ast.literal_eval(_args)
                        except Exception:
                            _args = {}
            except Exception:
                # hybrid mangle — best-effort name extraction, no args.
                # Two live shapes: '<tool_call>search_my_memories: ...' and
                # '<tool_call> <function>check_job</function></tool_call>'
                # (the grind's invented-name leak). An unknown name still
                # returns — execution then teaches via the Unknown-tool error
                # (which lists the real tools).
                _nm = (re.search(r"([a-z_]+)\s*[:,]", _blob)
                       or re.search(r"<function>([a-z_]+)</function>", _blob))
                if _nm:
                    _name = _nm.group(1)
            if _name:
                logger.warning(
                    "[ToolParse] NATIVE-FORMAT fallback: <tool_call> leak "
                    "(%s) — executing, teaching our grammar", _name)
                _args = {k: (v if isinstance(v, str) else json.dumps(v))
                         for k, v in (_args or {}).items()}
                return {"name": _name, "arguments": _args,
                        "parse_degraded": True,
                        "native_format": True}

    return None


def _is_tool_error(result_text: str) -> bool:
    """Heuristic: did _execute_function_call return a real result, or an error?

    All failure modes in _execute_function_call return a string that starts
    with one of a small set of prefixes ("No results returned",
    "Failed to execute", "[Tool error:", "Empty result from Searchie.").
    Anything else is treated as useful data the model should consume. This
    is the basis for tracking whether the tool loop actually got data,
    which the fallback in generate_response uses to choose between
    "searches failed" and "couldn't put together a complete answer", and
    which the per-iteration tool-result callback uses to decide whether
    to send the user a "search failed, retrying" message.
    """
    if not result_text:
        return True
    return result_text.startswith((
        "No results returned",
        "Failed to execute",
        "[Tool error:",
        "Empty result from Searchie.",
    ))


def _llm_facing_messages(messages: list) -> list:
    """Shape the request so every supported chat template renders tool results.

    [CONTINUA] 2026-09-13 — the "invisible results" fix. The residentb4 chat
    template renders a role:"tool" message ONLY via its forward-scan from an
    assistant message carrying a NATIVE tool_calls array whose ids match
    tool_call_id. On the text-based call path (an assistant turn whose
    <call> block is plain content — residentb's only path; 1,627 calls, zero
    native rounds) the tool result was silently dropped at render: the
    model never saw a single tool result. Verified 2026-09-13 by
    /apply-template replay of her real 06:56 payload (all result probes
    ABSENT) + live 31B self-report ("the tool output is missing from the
    prompt"); native-shape control renders (probe PRESENT). Her "ghost
    outputs" / "Ledger Desync" / "locked door" were this bug.

    Fix: an ORPHAN tool result (no matching native tool_calls on the
    preceding assistant message) is converted to role "user" for the
    REQUEST only. Storage, summaries, chronicle, and the audit log are
    untouched — they keep the honest role:"tool" provenance. Native
    tool_calls chains keep role:"tool" (their template path works). The
    [Internal Tool Result: <tool>] content marker preserves attribution
    inside the rendered turn. residenta's raw_chatml path never passes through
    here (its composer renders tool turns directly) — untouched.
    """
    out = []
    for _m in messages:
        if _m.get("role") != "tool":
            out.append(_m)
            continue
        _prev = out[-1] if out else None
        _native_ok = False
        if _prev is not None and _prev.get("role") == "assistant":
            for _tc in (_prev.get("tool_calls") or []):
                if _tc.get("id") == _m.get("tool_call_id"):
                    _native_ok = True
                    break
        out.append(_m if _native_ok else
                   {"role": "user", "content": _m.get("content")})
    return out


def _day_delta_block(instance_id: str, cur_ts: str, prev_ts: str,
                     base: str = None, cap_chars: int = 1200) -> str:
    """The "since we last spoke" briefing: what she did in the gap.

    [CONTINUA] 2026-09-13 (house ruling): wake threads and chat threads never
    share history, and mem0 is person-scoped (her memories with each contact
    live in separate namespaces — the deliberate design), so her chat self
    was amnesiac about her own morning: caught live 10:11, where she
    reconstructed the Zen correspondence from the send-denial's "9 min ago"
    instead of remembering it. The bridge is the ROLLING SUMMARIES — the
    chronicle-derived, all-sources layer (wakes, letters, other
    conversations) that wakes already receive ("WHERE THINGS STAND") but
    chat turns never did. This block carries the delta: rolling-summary
    events strictly between her last turn with THIS person and the current
    message. Fail-open: missing/corrupt files → "".

    cur_ts / prev_ts are minute-resolution local stamps ("YYYY-MM-DD HH:MM"):
    cur = the current user message, prev = the last stamped user/assistant
    message before it. Events are third-person by design (the wrapper's
    account, like the desk ledger) — the provenance framing keeps it from
    reading as recalled experience.
    """
    if not (cur_ts and prev_ts) or len(cur_ts) != 16 or len(prev_ts) != 16:
        return ""
    _base = base or os.path.dirname(os.path.abspath(__file__))
    _sum_dir = os.path.join(_base, "summaries", instance_id)
    import re as _re_day
    _day = set()
    for _rf in ("rolling_24h.md", "rolling_2d.md"):
        try:
            with open(os.path.join(_sum_dir, _rf), encoding="utf-8") as _fh:
                for _ln in _fh:
                    _ml = _re_day.match(
                        r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.+)$",
                        _ln.strip())
                    if _ml and prev_ts < _ml.group(1) < cur_ts:
                        _day.add((_ml.group(1), _ml.group(2)))
        except OSError:
            continue
    if not _day:
        return ""
    # dedupe by minute: rolling_24h and rolling_2d overlap, and the same
    # event often renders with slightly different phrasing per file — one
    # line per minute keeps the briefing from stuttering
    _by_ts = {}
    for _ts, _txt in _day:
        _by_ts.setdefault(_ts, _txt)
    _ordered = sorted(_by_ts.items())  # chronological
    _body = ""
    _room = int(cap_chars)  # cap keeps the NEWEST lines (yaml-configurable
    # since 2026-09-13 via memory.injection.day_delta.cap_chars)
    for _ts, _txt in reversed(_ordered):
        _line = f"- [{_ts}] {_txt}"
        if len(_line) + 1 > _room:
            break
        _body = (_line + "\n" + _body) if _body else _line
        _room -= len(_line) + 1
    if not _body.strip():
        return ""
    return ("[WHERE THINGS STAND — what you've been doing since we last "
            "spoke: the wrapper's log of your activities in this gap "
            "(wakes, letters, other conversations). Third-person by design "
            "— the events are yours.]\n" + _body.rstrip())


def _turn_transcript_payload(history: list, last_user_msg: str,
                             cap_chars: int = 12_000) -> str:
    """The extraction payload: the FULL turn transcript.

    [CONTINUA] 2026-09-13: the old builder fed mem0 only
    ``last_user_msg + history[-1]`` — for multi-round tool turns (wakes
    especially) that drops everything substantive: the send_message texts,
    the letter renders, the intermediate speeches. Caught live 2026-09-13:
    both Zen wakes extracted 0 facts while chat turns (single-round, content
    in the final reply) extracted fine. This walks from the LAST user
    message through the final reply — user, assistant rounds, and tool
    results included (letter renders are memory material) — capped
    newest-weighted at ``cap_chars``: the first thing dropped under pressure
    is the oldest overflow (for wakes, the packet's tool-list boilerplate
    head, the least valuable text in the turn).
    """
    history = history or []
    _turn_start = 0
    for _i in range(len(history) - 1, -1, -1):
        if history[_i].get("role") == "user":
            _turn_start = _i
            break
    _parts: list = []
    _room = int(cap_chars)
    for _m in reversed(history[_turn_start:]):
        if _m.get("role") not in ("user", "assistant", "tool"):
            continue  # wrapper meta markers are not memory material
        _c = _m.get("content", "")
        if isinstance(_c, list):
            _c = " ".join(
                b.get("text", "") for b in _c
                if isinstance(b, dict) and b.get("type") == "text")
        _c = str(_c or "")
        if not _c.strip():
            continue
        _tag = {"user": "user", "assistant": "assistant",
                "tool": "tool result"}.get(_m.get("role"), "user")
        _blk = f"[{_tag}] {_c}"
        if len(_blk) > _room:
            _blk = _blk[:max(0, _room)] + "…"
        _parts.append(_blk)
        _room -= len(_blk) + 1
        if _room <= 0:
            break
    payload = "\n".join(reversed(_parts))
    return payload if payload.strip() else str(last_user_msg or "")


# ---------------------------------------------------------------------------
# [CONTINUA] 2026-09-13 (house ruling, Option A): memory-injection layers are
# per-agent yaml configuration, not hardcoded prompt furniture. Absent key
# = layer OFF (opt-in): a layer only appears in the prompts of agents that
# enable it in their config. Both residents' configs enable the full current
# stack explicitly, so their prompts stay byte-identical to the pre-config
# era (proven by memory_layers_test.py). Toggles control PROMPT VISIBILITY
# only — the stores, auto-extraction, and the search tools are unaffected.
# ---------------------------------------------------------------------------
_MEMORY_LAYER_DEFAULTS = {
    # canonical order is fixed by the seven-claimant design (2026-09-07);
    # yaml key order never changes rendering order.
    "talking_with_line": {},
    "roster": {},
    "day_delta": {"cap_chars": 1200},
    "episodic_recall": {"limit": 4, "cap_chars": 280},
    "forever_events": {"cap_chars": 4000},   # the long record: approved
    "notes": {"enabled": False, "cap_bytes": 2000},  # chunk 7: HER notes, verbatim; yaml arms it
    # one-liner big events (strata.py quarterly folds, the designer-eyes gate) —
    # the pyramid's top layer; see strata.py 2026-09-14 ruling
    "mem0_recall": {"floor": None},       # None = keep class default (0.38)
    "recollections": {"cap_bytes": 16000},
    # work package B (§4a/§6e-B): the juggle — recent conversations verbatim,
    # active last. Window 24h (§6d.5), ~5 threads, 10K per thread tail.
    "juggle": {"enabled": False, "thread_bytes": 10000, "max_threads": 5,
               "window_hours": 24},
    "rolling_summaries": {},
    "session_summary": {"max_chars": None},  # None = inject as folded
}


def _normalize_memory_layers(mem_cfg: dict) -> dict:
    """Normalize config['memory']['injection'] into per-layer specs.

    Each layer accepts a bare boolean or a map {enabled: bool, ...caps}.
    Missing/unknown/None values fall back to the layer defaults; the
    section itself absent (or empty) means ALL layers OFF (Option A).
    """
    _raw = ((mem_cfg or {}).get("injection") or {})
    _layers = {}
    for _name, _defaults in _MEMORY_LAYER_DEFAULTS.items():
        _spec = _raw.get(_name, False)
        if isinstance(_spec, bool):
            _spec = {"enabled": _spec}
        if not isinstance(_spec, dict):
            _spec = {}
        _cfg = dict(_defaults)
        for _k in _defaults:
            if _k in _spec and _spec[_k] is not None:
                _cfg[_k] = _spec[_k]
        _layers[_name] = {"enabled": bool(_spec.get("enabled", False)),
                          "cfg": _cfg}
    return _layers


def _mem_layers_logline(layers: dict) -> str:
    _on = [_n for _n, _s in layers.items() if _s["enabled"]]
    return ", ".join(_on) if _on else "(none — all layers off)"


# [CONTINUA] 2026-09-13 (house ruling, Option A, phase 3): the built-in memory
# TOOLS are also per-agent yaml config. Absent key = tool not injected
# (opt-in); a disabled tool that a trained-in call still invokes returns a
# graceful message, never a crash (teaching-error pattern). Env switches
# (SAGENT_AGENT_WRITE_MEM / SAGENT_AGENT_LIST_MEM) remain fleet-level
# overrides on top of yaml.
_MEM_TOOLS_DEFAULTS = (
    "search_my_memories", "save_my_memory", "list_my_memories",
    "notes", "anchors",
    "recall_my_experience",   # §5: the canonical explicit-recall interface
    "essence",                # §6b.1: write_essence / endorse_essence / list_essences
    "trajectory",             # §5a: my_trajectory — the evidence-only view
)

# [CONTINUA] 2026-09-15 (mem0 boot-race fix): PROCESS-WIDE serialization for
# mem0 engine allocation. _init_memory rebinds a PROCESS-GLOBAL attribute
# (mem0.memory.setup.mem0_dir / mem0.memory.main.mem0_dir) before calling
# Memory.from_config — and mem0's migrations client is built from that
# global MID-from_config. Two agents allocating concurrently interleave:
# agent B's rebind lands between agent A's rebind and A's client creation,
# so A's engine opens B's migrations_qdrant and takes ITS flock (verified
# live 2026-09-15 07:35: residentb's Memory squatted
# residenta/.mem0/agent_residenta/migrations_qdrant/.lock for the whole process
# lifetime; residenta's allocation then failed "already accessed" on every
# retry — 0 mem0 searches, no injection, no extraction — because the
# squatter is A's long-lived Memory object, not a transient). The GIL does
# not protect from_config's mid-flight reads of the global; the per-core
# _memory_lock is useless across different SagentCore objects. This lock
# makes rebind→from_config→wrap atomic process-wide. Worst case: ~150ms
# serialization per queued agent at boot.
_mem0_global_alloc_lock = threading.Lock()


def _normalize_memory_tools(mem_cfg: dict) -> dict:
    """Normalize config['memory']['tools'] into bools per built-in memory
    tool. Bare boolean or {enabled: bool} both accepted; absent = OFF."""
    _raw = ((mem_cfg or {}).get("tools") or {})
    _out = {}
    for _n in _MEM_TOOLS_DEFAULTS:
        _spec = _raw.get(_n, False)
        if isinstance(_spec, bool):
            _out[_n] = _spec
        elif isinstance(_spec, dict):
            _out[_n] = bool(_spec.get("enabled", False))
        else:
            _out[_n] = False
    return _out


def _raw_chatml_render(messages: list, sanitize: bool = False) -> str:
    """[CONTINUA] Raw chatml prompt composer (extracted from the
    generate_response closure 2026-09-14 for testability; rendering is
    unchanged for system/user/assistant). THE FIX (the designer go, 2026-09-14):
    tool results render as USER-role text with a [Tool result] prefix —
    the rings never trained on a literal tool role (the composer's own
    old note: "no tool support on this path"), and ring 6.1 derailed on
    one (empty-query tool error → degenerate stamp-only replies, verified
    in the chronicle). Same orphan-result philosophy as
    _llm_facing_messages on the OpenAI path: the RESULT must reach the
    model, in a role it knows.

    [CONTINUA] 2026-09-22 (cleansweep repair, house ruling: in-place +
    compose-time defenses): sanitize=True strips HTML-tag-shaped strings
    from ASSISTANT turns at render — stored-dirty turns stop re-poisoning
    the few-shot window even before the one-time repair pass lands."""
    parts = []
    for _m in messages:
        _role = _m.get("role", "user")
        _c = _m.get("content") or ""
        if _role == "assistant":
            if sanitize:
                _c = _sanitize_history_html(_c)
            _ht = (_m.get("_think") or "").strip()
            if _ht:
                if sanitize:
                    _ht = _sanitize_history_html(_ht)
            if _ht:
                parts.append("<|im_start|>assistant\n<think>\n" + _ht + "\n</think>\n\n" + _c + "<|im_end|>\n")
            else:
                parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n" + _c + "<|im_end|>\n")
        elif _role == "tool":
            parts.append("<|im_start|>user\n[Tool result] " + _c + "<|im_end|>\n")
        else:
            parts.append(f"<|im_start|>{_role}\n{_c}<|im_end|>\n")
    return "".join(parts)


# [CONTINUA] 2026-09-16 (specs/2026-09-16-chat-think-contract.md, the designer go —
# rulings: retry cost accepted; human-chat turns only; no new notifications).
# Layer 1 + Layer 2 helpers for the raw path's think contract. Module-level
# (pure) so they test without a model. Tags are assembled from fragments so
# this source file never has to carry the bare literals inline.
_THINK_OPEN = "<" + "think" + ">"
_THINK_CLOSE = "<" + "/" + "think" + ">"
_ANCHOR_TEXT = ("You think inside the think block and close it before you "
                "speak — then answer in your own voice.")
# Channels whose scaffolding already carries the contract (wakes, pulse):
# the anchor never touches them — house ruling 2026-09-16. AMENDED 2026-09-22
# (cleansweep repair, house rulings: yaml-flag gate): the exemption is now the
# wound — wake turns collapse with empty thinks and the empty-think history
# self-poisons every subsequent wake (llm-debug 09-22 16:39). The new
# llm.chat_anchor_wake flag (residenta) includes these channels in Layer 1 +
# Layer 2 again; flag off = exactly the old behavior.
_RAW_ANCHOR_EXEMPT = {"system-wake", "continua:ritual"}


def _apply_chat_anchor(messages: list, user_id, enabled: bool,
                       include_wake: bool = False) -> list:
    """Layer 1 — the standing anchor. Appends one contract line to the
    system block for HUMAN-chat turns when the instance flag is on. The
    wake/pulse channels are exempt (their packets carry their own
    scaffolding) UNLESS include_wake is set (llm.chat_anchor_wake).
    Idempotent, never mutates the input list; disabled or exempt turns
    return the input unchanged (byte-identical rendering)."""
    if not enabled or (not include_wake and str(user_id) in _RAW_ANCHOR_EXEMPT):
        return messages
    out = [dict(_m) for _m in messages]
    for _m in out:
        if _m.get("role") == "system":
            _c = _m.get("content") or ""
            if _ANCHOR_TEXT not in _c:
                _m["content"] = (_c + "\n\n" + _ANCHOR_TEXT) if _c else _ANCHOR_TEXT
            break
    return out


# [CONTINUA] 2026-09-22 (cleansweep repair, approved in-place + compose
# defenses): compose-time history hygiene for BOTH residents (llm.sanitize_history).
# Wound: assistant turns in the stored chronicle/history carry raw HTML
# fragments (`</p>`, `<aside class="thinking">…</aside>`, stray `</body>`/
# `</ol>`) — renderer bleed-through the model then imitates, and the
# corrupted turns are re-fed verbatim as few-shot on every call (inference-
# time training on its own damaged output; llm-debug 09-22 16:39). The
# sanitizer strips tag-shaped strings from ASSISTANT turns at compose time.
# User/system/tool content is never touched (rosters and packets are plain
# text, but the contract is: sanitize only what she generated).
_HTML_TAG_RE = None


def _sanitize_history_html(text: str) -> str:
    """Strip HTML tags from an assistant turn (compose-time defense).
    Mechanical, tag-shaped only: `<name ...>` / `</name>` for common markup
    names — never touches plain text, never touches other roles. Fail-open
    by construction (pure string op)."""
    global _HTML_TAG_RE
    if not text:
        return text
    if _HTML_TAG_RE is None:
        import re as _re
        # whole <aside …>…</aside> blocks go (the model imitates the
        # renderer's thinking-asides — the inner text is not her words);
        # then remaining tag-shaped strings.
        _HTML_TAG_RE = _re.compile(
            r"<aside\b[^>]*>.*?</aside\s*>|"
            r"</?(?:p|div|span|aside|body|html|head|ol|ul|li|br|h[1-6]|"
            r"section|article|em|strong|b|i|u|pre|code|blockquote)"
            r"(?:\s[^>]*)?/?>", _re.IGNORECASE | _re.DOTALL)
    return _HTML_TAG_RE.sub("", text)


# [CONTINUA] 2026-09-22 (cleansweep repair): the leading-timestamp enforcer.
# The system prompt instructs NEVER to begin a reply with a [YYYY-MM-DD …]
# prefix; at least three turns in the 09-22 dump do exactly that — she
# copies the prefix style from rendered history/recollection lines into her
# own replies. Defense in depth: strip a leading prefix from generated
# replies BEFORE storage/delivery (llm.strip_ts_prefix), so the instruction
# is enforced at parse, not just stated.
_TS_PREFIX_RE = None


def _strip_leading_timestamp(text: str) -> str:
    """Remove leading [YYYY-MM-DD( HH:MM)] prefixes (plus surrounding
    whitespace) from a generated reply. Loops: corrupted turns can stack
    several prefix lines (observed in the chronicle: two or more stacked
    [date] lines) — all leading prefixes are metadata echo. Inline
    timestamps beyond the leading run are left for the repair pass to
    judge. Fail-open pure op."""
    global _TS_PREFIX_RE
    if not text:
        return text
    if _TS_PREFIX_RE is None:
        import re as _re
        _TS_PREFIX_RE = _re.compile(
            r"^\s*\[\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?\]\s*")
    _prev = None
    while _prev != text:
        _prev = text
        text = _TS_PREFIX_RE.sub("", text, count=1)
    return text


def _enforce_reply_hygiene(text: str, strip_ts: bool = True,
                           sanitize: bool = True, where: str = "") -> str:
    """[CONTINUA] 2026-09-24 (F3 recurrence — the failure dashboard's live
    catch): the two reply enforcers (leading ts-prefix strip + HTML
    sanitize) now flow through ONE pass so every assistant-text
    write/delivery site gets both. History: the 09-22 cleansweep repair
    gated them on final_content (delivery + chronicle); the 09-24
    ts-stacking fix closed history append (_assistant_hist_entry); but the
    Option-B round-speech path (mid-turn words captured + delivered via
    speech_callback) was missed by BOTH — all 38 post-cleansweep F3
    ts-prefix chronicle rows carry its signature (finish_reason None, no
    user row). The `where` label makes a strip firing observable in the
    bridge log — the dashboard measures DELIVERED text, so without this
    line a generation-side emission would go silently blind (the
    finish_reason-None lesson). Pure + idempotent + fail-open."""
    if not text:
        return text
    out = text
    if strip_ts:
        out = _strip_leading_timestamp(out)
    if sanitize:
        out = _sanitize_history_html(out)
    if where and out != text:
        try:
            logger.info("[Core] reply hygiene stripped %s (%d → %d chars)",
                        where, len(text), len(out))
        except Exception:
            pass
    return out


def _think_captured(reasoning_after_split: str, native_reasoning: str) -> bool:
    """True when THIS response's think block closed and was captured —
    either the post-split capture fired (a close tag was found inline) or
    the server separated the reasoning natively. Empty on both = the
    collapse signature (the think never closed: empty-turn family or
    think-register delivered as the reply with no answer)."""
    return bool((reasoning_after_split or "").strip()) or bool(
        (native_reasoning or "").strip())


def _open_think_prompt(prefill: str = None) -> str:
    """The generation prompt's trailing open think block. With no prefill
    this is byte-identical to the historical rendering. A prefill continues
    her real planning register mid-think (Layer 2's anchored retry) —
    sanitized of any tag-shaped strings, never fabricated by this code."""
    _pf = (prefill or "").replace(_THINK_CLOSE, "").replace(_THINK_OPEN, "").strip()
    return "<|im_start|>assistant\n" + _THINK_OPEN + ((_pf + "\n") if _pf else "\n")


def _collapse_recovery(orig_content: str, orig_reasoning: str,
                       one_call, prefill: str):
    """Layer 2 — exactly ONE anchored retry. one_call(_msgs, _prefill) is
    the wired _chat_call (the raw arm applies the standing anchor itself).
    Returns (content, reasoning) for the round:
      - retry's think closed -> its answer ships (recovered);
      - retry also unclosed  -> the better-formed of the two ships
        (non-empty content preferred) — fail-open, ERROR logged by caller."""
    import re as _re
    _resp = one_call(None, prefill)
    _msg = _resp.choices[0].message
    _c = _msg.content or ""
    _r = getattr(_msg, "reasoning_content", "") or ""
    _m = _re.search(r"</think(?:ing)?>", _c)
    if not _r and _m:
        _r = _c[:_m.start()].strip()
        _c = _c[_m.end():]
    _c = _re.sub(r"</?think(?:ing)?>", "", _c)
    if _r.strip():
        return _c, _r
    if len(_c.strip()) > len((orig_content or "").strip()):
        return _c, _r
    return (orig_content or ""), (orig_reasoning or "")


def _dump_unclosed_think(user_id, content: str, prompt, eval_count,
                         model: str, root_dir: str = None) -> str:
    """Layer 2 forensics: the EXACT prompt + collapsed reply when the think
    block never closed (extends the empty-turn dumper's trigger). Fail-open:
    returns the path written, or "" on any failure."""
    try:
        _fdir = root_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "forensics", "empty_turns")
        os.makedirs(_fdir, exist_ok=True)
        _fpath = os.path.join(
            _fdir, datetime.now().strftime("%Y%m%d_%H%M%S") + "_unclosed.txt")
        with open(_fpath, "w", encoding="utf-8") as _ff:
            _ff.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "user_id": user_id,
                "signature": "unclosed_think",
                "model": model,
                "eval_count": eval_count,
                "collapsed_len": len(content or ""),
                "content_preview": (content or "")[:300],
                "prompt": prompt,
            }, ensure_ascii=False, indent=2))
        return _fpath
    except Exception:
        return ""


class SearchieClient:
    """HTTP client for calling the local Searchie API on port 21000."""

    def __init__(self, url=None):
        self.base_url = url or os.getenv("SEARCHIE_URL", "http://localhost:21000")

    async def search_async(self, query: str) -> dict:
        """Hit /chat and return the JSON payload (consolidated_facts + critical_links)."""
        try:
            # 180 s budget: Searchie's full pipeline (financial data + 3 web
            # sweeps in parallel: Wikipedia, Tavily, Exa) routinely takes
            # 45-70 s for financial/news queries, and the previous 45 s
            # timeout was firing before Searchie finished, leaving the
            # model with error messages and a confused user. The OpenAI
            # client itself uses 300 s, so 180 s stays safely under that.
            async with httpx.AsyncClient(timeout=180.0) as session:
                resp = await session.post(
                    f"{self.base_url}/chat",
                    json={"sender": "sagent_bridge", "query": query},
                )
                resp.raise_for_status()
                payload = resp.json()
                logger.info("[Searchie] Received %d facts for '%s'", len(payload.get("consolidated_facts", [])), query[:80])
                return payload
        except httpx.ReadTimeout:
            logger.error("[Searchie] Timeout for query: %s", query[:80])
            return {"error": "Searchie timed out (180 s). Try a narrower query."}
        except Exception as exc:
            logger.warning("[Searchie] HTTP error during search: %s", exc)
            return {"error": f"Searchie connection failed: {exc}"}

    def search(self, query: str) -> dict:
        """Synchronous wrapper over search_async using blocking httpx.Client
        so we never spawn new event loops at runtime."""
        try:
            # Blocking call — no event loop creation. See search_async for
            # the rationale on the 180 s budget.
            with httpx.Client(timeout=180.0) as session:
                resp = session.post(
                    f"{self.base_url}/chat",
                    json={"sender": "sagent_bridge", "query": query},
                )
                resp.raise_for_status()
                payload = resp.json()
                logger.info("[Searchie] Received %d facts for '%s'", len(payload.get("consolidated_facts", [])), query[:80])
                return payload
        except httpx.ReadTimeout:
            logger.error("[Searchie] Timeout for query: %s", query[:80])
            return {"error": "Searchie timed out (180 s). Try a narrower query."}
        except Exception as exc:
            logger.warning("[Searchie] HTTP error during search: %s", exc)
            return {"error": f"Searchie connection failed: {exc}"}


class TarotClient:
    """HTTP client for calling the local Tarot service on port 21099."""

    def __init__(self, url=None):
        self.base_url = url or os.getenv("TAROT_URL", "http://127.0.0.1:21099")

    def draw(self, seed: int | None = None) -> dict:
        """Draw a tarot card via POST /draw with optional X-Seed header."""
        try:
            headers = {"X-Seed": str(seed)} if seed is not None else {}
            with httpx.Client(timeout=10.0) as session:
                resp = session.post(f"{self.base_url}/draw", data=b"", headers=headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.warning("[Tarot] Error drawing card: %s", exc)
            return {"error": f"Tarot service unavailable: {exc}"}


def _source_tag(uid: str, roster_names: dict, turn_uid: str = "") -> str:
    """[CONTINUA] One-Self step 2: attribution prefix for an injected memory.
    roster_names: {user_id: display_name}. system-wake = her own wake notes."""
    try:
        if uid == "system-wake":
            return "[from your wake notes]"
        name = roster_names.get(uid)
        if not name:
            name = f"user {uid}" if uid else "unknown"
        if turn_uid and uid == turn_uid:
            return f"[from your conversation with {name}]"
        return f"[from {name}]"
    except Exception:
        return ""


def _apply_self_sourced_policy(items, policy, historical, takeaway_exempt):
    """Memory provenance (memprovenance0929.md, house rulings 2026-08-29):
    facts come from the USER'S side of the conversation. Returns
    (kept_items, dropped_items_with_reason).

      strict        self_statement AND assistant_takeaway dropped
                    non-historical (historical exemption preserved for
                    takeaway golds on summary queries — eval item 021/110)
      exclude_self  self_statement dropped; takeaways keep M4 penalty+cap
      inject_all    no change (spike — persona continuity is the product)

    Dropped items carry reason "self-sourced excluded by policy" so
    /searchmem's not-injected view explains itself.
    """
    if policy not in ("strict", "exclude_self"):
        return list(items or []), []
    kept, dropped = [], []
    for i in (items or []):
        k = i.get("kind")
        if k == "self_statement":
            dropped.append((i, "self-sourced excluded by policy"))
        elif (k == "assistant_takeaway" and policy == "strict"
              and not (historical and takeaway_exempt)):
            dropped.append((i, "self-sourced excluded by policy"))
        else:
            kept.append(i)
    return kept, dropped


def _degeneracy_check(text: str, finish_reason=None) -> tuple:
    """[CONTINUA] Repetition-loop detector (2026-09-07). Calibrated on the
    09-07 night: 247 normal replies ≤0.12 shingle-dup ratio; the three loops
    0.90-0.99. Threshold 0.45 → zero false positives. A long length-cut
    reply also flags (truncated mid-thought = not for delivery)."""
    try:
        words = (text or "").split()
        if len(words) < 60:
            return False, 0.0
        k, stride = 6, 3
        shingles = [" ".join(words[i:i + k]).lower()
                    for i in range(0, len(words) - k + 1, stride)]
        if not shingles:
            return False, 0.0
        ratio = 1.0 - len(set(shingles)) / len(shingles)
        long_trunc = (finish_reason == "length" and len(text or "") > 6000)
        return (ratio > 0.45 or long_trunc), round(ratio, 3)
    except Exception:
        return False, 0.0


def _semantic_check(text: str, embed_fn, min_sents: int = 6,
                    max_sents: int = 120, sim_threshold: float = 0.85,
                    loop_fraction: float = 0.20) -> tuple:
    """[CONTINUA] Paraphrase-loop detector (2026-09-08, approved build).
    The complement to _degeneracy_check: shingles measure VERBATIM overlap,
    but her degenerate register restates one thought in fresh tokens —
    lexically novel, semantically stuck. Split the reply into sentences,
    embed them (nomic-embed-text, raw /api/embed — symmetric vectors, no
    task prefix), and measure the fraction of sentence pairs whose cosine
    clears sim_threshold. A paraphrase loop drives that fraction toward
    1.0; ordinary prose stays near 0. Fail-open by construction: any
    embedder failure returns not-flagged (instrumentation must never
    break her turn). Returns (flagged, hot_fraction, n_sentences).

    CALIBRATION (2026-09-08, 136 real replies): loop_fraction 0.20 with
    sim_threshold 0.85 separates the classes by >5x on both sides —
    floods 0.32-0.93 (flagged 5/5), normal replies 0.00-0.04 (0/114),
    her wake responses 0.00 (0/17 — the deferred thematic loop is
    semantically diverse: the lexicon repeats, the meanings don't).
    Thresholds must not be tuned below this margin without re-running
    the calibration corpus."""

    try:
        import re as _re
        sents = [s.strip() for s in _re.split(r"(?<=[.!?])\s+|\n{2,}", text or "")
                 if len(s.strip()) >= 25]
        if len(sents) < min_sents:
            return False, 0.0, len(sents)
        sents = sents[:max_sents]
        vecs = embed_fn(sents)
        if not vecs or len(vecs) != len(sents) or not vecs[0]:
            return False, 0.0, len(sents)
        try:
            import numpy as _np
            m = _np.array(vecs, dtype=float)
            m = m / ( _np.linalg.norm(m, axis=1, keepdims=True) + 1e-9 )
            sims = m @ m.T
            n = len(sents)
            hot = int((sims[_np.triu_indices(n, 1)] >= sim_threshold).sum())
            frac = hot / max(1, n * (n - 1) // 2)
        except ImportError:
            import math as _math
            norms = []
            for v in vecs:
                nv = _math.sqrt(sum(x * x for x in v)) or 1.0
                norms.append([x / nv for x in v])
            n = len(norms)
            hot, total = 0, 0
            for i in range(n):
                for j in range(i + 1, n):
                    dot = sum(a * b for a, b in zip(norms[i], norms[j]))
                    total += 1
                    if dot >= sim_threshold:
                        hot += 1
            frac = hot / max(1, total)
        return (frac >= loop_fraction), round(frac, 3), n
    except Exception:
        return False, 0.0, 0


def _merge_unified_hits(hits_by_user: dict, cap: int) -> list:
    """[CONTINUA] merge per-partition search results into one ranked,
    attributed list (house ruling 2026-09-07: search is unified, injection
    stays person-scoped). Rank by score first, then dedup by text-head
    ACROSS users (the highest-scored instance survives — the same fact
    learned twice is one memory with two origins)."""
    merged = []
    for uid, hits in hits_by_user.items():
        for h in hits:
            h2 = dict(h)
            h2["source_user_id"] = uid
            merged.append(h2)
    merged.sort(key=lambda h: -(h.get("score") or 0))
    seen = set()
    out = []
    for h in merged:
        key = (h.get("memory") or "")[:100]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out[:cap]


def _future_date_flags(text: str, now=None) -> list:
    """[CONTINUA] 2026-09-22 (cleansweep repair #4): dates asserted AFTER the
    current date inside a memory save. The 09-22 dump shows '[2026-09-24]
    First meeting with residentb' — dated two days past the prompt's own
    09-22 — persisting in keeps and history. NOT a rejection (house rule:
    her words are never blocked or silently rewritten); the caller logs a
    warning and surfaces the flag in the digest."""
    import re as _re
    from datetime import datetime, date as _date
    _text = text or ""
    _now = now or _date.today()
    _out = []
    for _m in _re.finditer(r"\[(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2})?\]", _text):
        try:
            if _date.fromisoformat(_m.group(1)) > _now:
                _out.append(_m.group(1))
        except ValueError:
            continue
    return _out


class SagentCore:
    def __init__(self, config):
        self.config = config

        app_cfg = config.get("app", {})
        llm_cfg = config.get("llm", {})
        prompts_cfg = config.get("prompts", {})
        tools_cfg = config.get("tools", None)  # new: tool definitions list

        # --- LLM / Embedding setup ---------------------------------------------
        # Two endpoints on purpose. Do NOT collapse them onto one server/model:
        #
        #   * `chat_base_url` (e.g. http://127.0.0.1:8080/v1)  -> llama.cpp
        #     hosting `example-chat:latest`. Generative chat + mem0 fact extraction.
        #
        #   * `base_url`      (e.g. http://127.0.0.1:11434/v1) -> Ollama
        #     hosting `nomic-embed-text:latest`. Dedicated 308M-param embedder,
        #     768-dim, contrastively trained for retrieval.
        #
        # Why split them: llama.cpp's `/v1/embeddings` is disabled on the current
        # server, and even if it weren't, residentb4:31b is a 30B chat model (n_embd=
        # 5376) — wrong tool for similarity search, and the dimension would force
        # a re-embed of the whole memory store. `nomic-embed-text` is what every
        # configs/*.yaml file is already tuned for (embedding_dims: 768).
        #
        # If you ever want to consolidate, the right move is to add
        # `nomic-embed-text` as a second GGUF on llama.cpp (with `--embeddings`),
        # NOT to swap the embedder to residentb4:31b. See conversation 2026-07-21.
        # -------------------------------------------------------------------------
        self.base_url = llm_cfg.get("base_url", "http://localhost:11434/v1")      # embedder endpoint (Ollama)
        chat_base_url = llm_cfg.get("chat_base_url") or self.base_url              # chat+extraction endpoint (llama.cpp), falls back to base_url
        self._chat_base_url = chat_base_url  # chunk 4: /tokenize verification target
        self.model = llm_cfg.get("model", "qwen3.6:35b-a3b-q8_0")
        self.openai_api_key = app_cfg.get("openai_api_key") or os.getenv(
            "OPENAI_API_KEY"
        ) or "ollama"

        self.base_url_base = self.base_url.replace("/v1", "").rstrip("/")
        self.ollama_base_url = self.base_url_base
        # W08: explicit httpx.Client with sized pool and no keep-alive
        # to avoid the same stale-socket problem FastAI documents. Pool
        # size 4 matches the 2-slot backend with headroom. W09 disables
        # OpenAI's built-in retry; W10 may wrap this client with an
        # admission limiter. See plan/v2/W08-explicit-httpx-client.md.
        # 2026-09-01: llm.timeout (yaml) overrides the 300s default — ring 4.1
        # on lab CPU thinks 2-10 min per response (num_predict 4800), which a
        # 300s read clips. Default unchanged for every other instance.
        self._llm_timeout = float(llm_cfg.get("timeout", 300.0))
        # 2026-09-01 evening: llm.raw_chatml (yaml, default false) — compose the
        # chatml prompt in the bridge and POST /api/generate raw=true, bypassing
        # ollama's chat-template machinery entirely. Why: testmodel-cpu:latest
        # was created with TEMPLATE {{ .Prompt }} (passthrough — the GGUF's
        # embedded template does not parse in ollama's Go engine), so no
        # <think> open ever reached the model and every serving turn answered
        # direct (harvest captured zero reasoning despite w1-v2 training full
        # think blocks — verified in the training text). Raw mode re-creates the
        # exact serving rendering the model was trained on: im-format history
        # with empty-think assistant turns, generation opening an UNclosed
        # <think>. The existing </think> split below then populates
        # _last_reasoning and the harvest captures real reasoning. Flag-gated:
        # only residenta sets it; every other instance keeps the /v1 chat path.
        self._llm_raw_chatml = bool(llm_cfg.get("raw_chatml", False))
        # [CONTINUA] 2026-09-16 (specs/2026-09-16-chat-think-contract.md,
        # the designer go): Layer 1 + Layer 2 gate — the standing anchor on human-chat
        # turns (wakes/pulse exempt) and the unclosed-think detection with
        # one anchored retry. Default FALSE: every other instance (residentb on
        # her /v1 path) never enters this code. Kill switch: set false +
        # restart bridge -> exactly the pre-spec state.
        self._chat_anchor = bool(llm_cfg.get("chat_anchor", False))
        # [CONTINUA] 2026-09-22 (cleansweep repair, house rulings: yaml-flag
        # gate, both channels): the wake/pulse exemption from the think
        # contract is now opt-out — llm.chat_anchor_wake includes wake and
        # ritual turns in Layer 1 (anchor) + Layer 2 (unclosed-think
        # recovery). Default FALSE = exactly the pre-repair behavior.
        self._chat_anchor_wake = bool(llm_cfg.get("chat_anchor_wake", False))
        # [CONTINUA] 2026-09-22 (cleansweep repair): compose-time history
        # hygiene — strip HTML-tag-shaped strings from assistant turns at
        # render (llm.sanitize_history) and a leading timestamp prefix from
        # generated replies before storage/delivery (llm.strip_ts_prefix).
        # Both default ON (the corruption is live and self-reinforcing);
        # set false + restart for the pre-repair behavior.
        self._sanitize_history = bool(llm_cfg.get("sanitize_history", True))
        self._strip_ts_prefix = bool(llm_cfg.get("strip_ts_prefix", True))
        # [CONTINUA] 2026-09-13 (house ruling: 'get the context clean'): the
        # empty-think history render taught the model that assistant turns
        # speak without thinking — measured 60% think-skip on ring 6.1 chat
        # turns, with planning-voice/journal-echo leaks as the reply.
        # 'real' renders compressed actual thinks (first ~240c, the planning
        # register) inside assistant history think blocks, restoring the
        # trained distribution. Default 'empty' = the historical render
        # (kill switch: set 'empty' + restart). residenta-only by config — residentb
        # (standard /v1 path, no think contract) never sets it.
        self._history_thinks = str(llm_cfg.get("history_thinks", "empty")).strip().lower()
        # [CONTINUA] explicit num_predict payload knob (the num_predict-cliff
        # ruling, 2026-09-07): the Modelfile's implicit 4800 silently cut
        # think+answer mid-generation; explicit beats implicit so a Modelfile
        # default can never re-cap. think+answer share the cap (~273s worst
        # case at ~30 tok/s, inside the 600s timeout, no VRAM change).
        self._num_predict = int(llm_cfg.get("num_predict", 8192))
        # [CONTINUA] cliff instrumentation: finish_reason/eval_count captured
        # per call, recorded into the chronicle by the capture hook. The
        # wrapper previously discarded done_reason entirely (0/224 harvest
        # lines had finish_reason — the silent-truncation incident).
        self._last_finish_reason = None
        self._last_eval_count = None
        self._last_prompt_tokens = None
        self._chat_generate_url = chat_base_url.replace("/v1", "").rstrip("/") + "/api/generate"
        self.openai_client = OpenAI(
            api_key=self.openai_api_key,
            base_url=chat_base_url,
            timeout=self._llm_timeout,
            http_client=httpx.Client(
                timeout=httpx.Timeout(connect=30.0, read=self._llm_timeout, write=300.0, pool=30.0),
                limits=httpx.Limits(
                    max_keepalive_connections=0, max_connections=4
                ),
            ),
            max_retries=0,  # W09: Sagent's own wrapper owns retries
        )

        # --- Memory init (lazy) -----------------------------------------------
        self.instance_path = app_cfg.get("instance_path") or os.getenv(
            "SAGENT_BASE", "/tmp/continua"  # [CONTINUA] rebased
        )
        self.instance_id = app_cfg.get("instance_id", "default")
        embedding_dimensions = int(app_cfg.get("embedding_dims", 768))

        # Per-agent isolation of Mem0's internal migration/telemetry dir.
        # Each agent gets its own .mem0/agent_<id>/ subdir so its
        # migrations_qdrant/.lock is fully isolated from other agents
        # running in the same process. The env var below is captured
        # at module-import time inside mem0.memory.setup, so it only
        # protects the FIRST agent to init. The rebind for subsequent
        # agents happens inside _init_memory() (right before
        # Memory.from_config) where it matters. See fixes.md "MEM0_DIR
        # collision" entry.
        if _mem0_setup is not None:
            self._agent_mem0_dir = os.path.join(
                self.instance_path, ".mem0", f"agent_{self.instance_id}"
            )
        else:
            self._agent_mem0_dir = os.path.join(self.instance_path, ".mem0")
        os.makedirs(self._agent_mem0_dir, exist_ok=True)
        os.environ["MEM0_DIR"] = self._agent_mem0_dir

        # Agent display name for memory summarization (Sovereignty fix):
        # parsed from the identity prompt's "Your name is X" line.
        _identity = ""
        try:
            _identity = (config.get("prompts", {}) or {}).get("identity", "") or ""
        except Exception:
            _identity = ""
        import re as _re
        _m = _re.search(r"Your name is ([A-Za-z0-9_-]+)", _identity)
        self.agent_name = _m.group(1) if _m else (config.get("app", {}).get("instance_id", "the assistant"))

        # Memory provenance policy (memprovenance0929.md, house rulings
        # 2026-08-29): facts come from the USER'S side of the conversation.
        # The bot's own answers, conclusions, advice, invented preferences,
        # and self-descriptions are never recorded as facts and never
        # injected — identity lives in the weights (Heretic doctrine), not
        # in a log. Per-instance yaml key:
        #   spike.yaml:  inject_all   (roleplay product — persona continuity)
        #   default:     exclude_self (self_statement kind dropped at recall;
        #                takeaways keep M4 penalty+cap — eval-stable)
        #   residenta.yaml:  strict       (self_statement AND assistant_takeaway
        #                dropped non-historical; historical exemption
        #                preserved for "summarize what we discussed")
        # Env kill-switch: SAGENT_SELF_SOURCED_ENFORCE=0 reverts to legacy.
        _mem_cfg = config.get("memory") or {}
        self.self_sourced_policy = _mem_cfg.get("self_sourced_policy", "exclude_self")
        # [CONTINUA] One-Self Memory Plan step 2 (2026-09-08, approved):
        # unified recall pool — automatic injection searches ALL of her own
        # partitions (contacts + system-wake), ranked together, with a
        # source tag rendered on every injected line. Other instances:
        # default False = partition-scoped (legacy). Personas stay sealed —
        # this only unifies HER OWN streams.
        self.unified_recall = bool(_mem_cfg.get("unified_recall", False))
        if os.getenv("SAGENT_SELF_SOURCED_ENFORCE", "1") != "1":
            self.self_sourced_policy = "inject_all"
        # [CONTINUA] 2026-09-13 (house ruling, Option A): per-layer injection
        # profile. Absent/empty memory.injection section = ALL layers off
        # (opt-in). Both residents' yamls enable the full stack explicitly,
        # so behavior is preserved through explicit configuration. Startup
        # log line makes the profile auditable from bridge.log.
        self._mem_layers = _normalize_memory_layers(_mem_cfg)
        logger.info(
            "[Core] memory injection layers for %s: %s",
            self.instance_id, _mem_layers_logline(self._mem_layers),
        )
        # mem0 floor override (yaml wins over env default when present;
        # the pipeline itself is untouched).
        _m0 = self._mem_layers.get("mem0_recall") or {}
        if _m0.get("enabled") and _m0["cfg"].get("floor") is not None:
            self.MEMORY_MIN_INJECT_SCORE = float(_m0["cfg"]["floor"])
        # [CONTINUA] 2026-09-13 (house ruling, Option A, phase 3): built-in
        # memory tools are per-agent yaml too. Absent memory.tools section
        # = none of the three built-ins injected (opt-in); env switches
        # stay fleet-level overrides. Startup log keeps the profile
        # auditable.
        self._mem_tools = _normalize_memory_tools(_mem_cfg)
        logger.info(
            "[Core] memory tools for %s: %s",
            self.instance_id,
            ", ".join(_n for _n in _MEM_TOOLS_DEFAULTS
                      if self._mem_tools.get(_n)) or "(none)",
        )

        self.mem0_config = {
            # CRITICAL (2026-08-25 incident): mem0's MemoryConfig resolves its
            # history_db_path default from MEM0_DIR frozen at IMPORT time
            # (mem0/configs/base.py line ~13), which our per-agent rebind in
            # _init_memory cannot reach. Without this explicit key, ALL agents
            # share one history.db, and mem0's V3 extraction injects the last
            # 10 messages ACROSS AGENTS into every add() — leaking facts
            # between personas (verified: sagent_default's beach story was
            # re-extracted into residenta_memories). See fixes.md.
            "history_db_path": os.path.join(self._agent_mem0_dir, "history.db"),
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": os.path.join(self.instance_path, "mem0_db", "qdrant"),
                    "collection_name": app_cfg.get("collection_name", "sagent_memory"),
                    "embedding_model_dims": embedding_dimensions,
                },
            },
            "llm": {
                # llama.cpp's /v1 is OpenAI-compatible, so mem0 uses the
                # openai provider. `openai_base_url` is the key mem0ai
                # 2.0.4 expects (verified in venv/.../mem0/llms/openai.py).
                # NOTE (2026-09-13): temperature was A/B-tested here (0 vs
                # the mem0 default 0.1, 4 attempts each on the real Zen
                # payload): 8/8 identical extraction — no behavioral
                # difference, pin removed per the designer (tuned-baseline
                # discipline). Intermittent 0-fact draws are guarded by the
                # retry-on-empty + raw-output logging instead.
                "provider": "openai",
                "config": {
                    "model": self.model,
                    "openai_base_url": chat_base_url,
                    "api_key": self.openai_api_key,
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": llm_cfg.get(
                        "embedding_model", "nomic-embed-text:latest"
                    ),
                    "ollama_base_url": self.ollama_base_url,
                    "embedding_dims": embedding_dimensions,
                },
            },
            # Sovereignty fix (2026-08-27, the designer): mem0's default extraction
            # prompt describes the assistant as "the assistant"/"it", so
            # injected memories objectify her — feeding the outside-observer
            # frame back into every turn (contributor to the deixis mirror).
            # custom_instructions is mem0 2.0.4's supported override: name the
            # agent, require she/her naming, preserve first-person voice.
            # Name extracted from the identity prompt ("Your name is X");
            # falls back to instance_id.
            "custom_instructions": (
                "The assistant's name is %s. When recording facts about the "
                "assistant, refer to her by name ('%s') or as 'she/her' — "
                "never as 'it', 'the model', or 'the assistant'. Facts about "
                "the user are recorded as usual about the user. "
                "Record ONLY durable facts: the user's preferences, projects, "
                "hardware, schedule, relationships, background, and concrete "
                "decisions made. Do NOT record the assistant's own advice, "
                "warnings, recommendations, analyses, or conclusions as facts "
                "— those are conversation, not memory. Do NOT record anything "
                "about the memory system, retrieval, prompts, context "
                "injection, or this extraction process itself."
                % (self.agent_name, self.agent_name)
            ) + (
                "" if getattr(self, "self_sourced_policy", "exclude_self") == "inject_all"
                else " ONE-SIDE SOURCING RULE: Record facts ONLY from the "
                "user's side of the conversation — the user's statements, "
                "preferences, projects, decisions, and things the user said "
                "about the assistant. Do NOT record anything the assistant "
                "said as a fact — no advice, conclusions, recommendations, "
                "analyses, preferences, favorites, experiences, or "
                "self-descriptions of the assistant, however confidently "
                "stated. When the user asks the assistant a question, the "
                "assistant's ANSWER is conversation, not memory: only the "
                "user's own words are memory."
            ),
        }
        self.memory = None
        self._memory_lock = threading.Lock()
        # Serializes background memory.add() calls. The bridge now fires
        # _save_turn_memory from a daemon thread so the user gets the response
        # without waiting for mem0's LLM extraction call. mem0's Memory
        # instance isn't documented as thread-safe across concurrent add()
        # calls on the same instance, so we serialize them.
        self._memory_save_lock = threading.Lock()
        # save_my_memory tool: per-user rolling-window rate limiter state.
        # _savemem_lock guards _savemem_saves; the write itself reuses
        # _memory_save_lock (see _tool_save_my_memory).
        self._savemem_lock = threading.Lock()
        self._savemem_saves: dict = {}

        # --- Prompts (identity + precision + integration) ---------------------
        identity = prompts_cfg.get("identity", "").strip()
        precision = prompts_cfg.get("precision_guidance", "").strip()
        integration = prompts_cfg.get("integration_guidance", "").strip()

        self.system_prompt = f"{identity}\n\n{precision}\n\n{integration}"

        # HISTTS (2026-09-05): teach the agent to read history timestamps.
        # Rendered entries carry a [YYYY-MM-DD HH:MM] prefix; unstamped
        # messages are either from before this feature or from the current
        # turn (same-turn entries are all "now").
        self.system_prompt += (
            "\n\n[Message timestamps]\n"
            "Messages in the conversation history may carry a [YYYY-MM-DD HH:MM] "
            "prefix showing when they were sent. Use it when a question depends "
            "on when something was discussed. An unprefixed message is either "
            "from the current exchange or predates timestamping. The current "
            "date/time is given at the top of this prompt. "
            "These prefixes are metadata added by the system that runs you — "
            "they are never part of anyone's words. NEVER write a timestamp "
            "prefix yourself and NEVER begin a reply with one; just answer."
        )

        # [CONTINUA] 2026-09-08 sampling ruling (A/B harness, 45 replayed
        # generations on the real 09-07 loop contexts): temp 0.8 widens the
        # 4B attractor's catch basin slightly (2 incidents/25 draws vs 0/16
        # at 0.7 in replay — both the analyst-register loop); repeat_penalty
        # 1.2 pulls it back with zero quality loss, and the degeneracy guard
        # contains any residual catch. house ruling: 0.8 + wired penalty.
        # (temperature read earlier — top-level yaml wins; 2026-09-08 read-fix)
        # [CONTINUA] repeat_penalty was NEVER sent (ollama implicit 1.1 ran
        # every production turn). Now read from llm.repeat_penalty; absent
        # key = key omitted = ollama default 1.1 (behavior unchanged for
        # other instances). residenta.yaml carries 1.2.
        _rp = llm_cfg.get("repeat_penalty")
        if _rp is not None:
            self._repeat_penalty = float(_rp)
        else:
            self._repeat_penalty = None
        # P1: per-user summary file paths, set by the bridge when history is
        # loaded (bridge owns disk layout). Read by _save_turn_memory_async.
        self._summary_paths: dict = {}

        self.max_tool_iterations = int(os.getenv("SAGENT_MAX_TOOL_ITERATIONS", "3"))

        # P4: token budget for the injected long-term-memory block (per persona).
        self.max_memory_chars = int(app_cfg.get("max_memory_chars", 6000))

        # Conversation history budget — keep under this many chars before sending to LLM.
        # DEFAULT retuned 2026-09-04 for the ring-4.1 local serving (testmodel-gpu, 8192 declared,
        # effective per-request limit measured at 4099 under parallel load — the ollama
        # truncation is SILENT and keep=5 drops the system prompt first):
        #   12,000 chars ≈ 3.5K tokens history + 6K memory block + system + response
        #   stays inside the trained band (~4.7K) with headroom. Per-agent override wins
        #   (the lab's residentb4-31B personas keep their big budgets via their own configs).
        # Trims to 70% of the budget (the HEADROOM rule) so prompt-format overhead,
        # the think scaffold, and the load-dependent clamp never eat the system block.
        # [CONTINUA] 2026-09-08 FIX: these knobs were read from app_cfg (the
        # `app:` sub-dict) while the yaml carried them at top level / llm: —
        # temperature and max_history_chars silently rode defaults. Reads
        # fixed: top-level yaml wins, then app:, then the legacy default.
        self.max_history_chars = int(
            config.get("max_history_chars",
                       llm_cfg.get("max_history_chars",
                                   app_cfg.get("max_history_chars", 12_000))))
        # [CONTINUA] 2026-09-13 (house ruling): explicit-window regime flag —
        # when llm.max_history_chars is set in config it IS the raw-history
        # window target, applied directly (no headroom discount); the 59K
        # max_prompt_chars stays the TOTAL prompt ceiling the other layers
        # (summaries, memories, books, tools) grow into. Absent key = legacy
        # derived regime (70% of prompt-cap minus reserve).
        self._history_window_explicit = bool(
            config.get("max_history_chars")
            or llm_cfg.get("max_history_chars")
            or app_cfg.get("max_history_chars"))
        self.temperature = float(
            config.get("temperature",
                       app_cfg.get("temperature", 0.7)))
        # [CONTINUA] 2026-09-08 house ruling: cap the TOTAL prompt (system +
        # tools + injection + history) at ~4K tokens until a ring trains
        # past the measured effective-context falloff (~4099 tok). Budget in
        # chars (≈4 chars/token → 15000 ≈ 3.75K tok, inside the line with
        # margin). 0/absent = legacy behavior (other instances unaffected).
        # Raise the yaml knob when ring 6.1 lands.
        self.max_prompt_chars = int(
            config.get("max_prompt_chars", 0)
            or app_cfg.get("max_prompt_chars", 0)) or 0
        self._prompt_fixed_reserve = int(app_cfg.get("prompt_fixed_reserve_chars", 4500))

        # --- Chunk 4 (memory plan §6g): measured token budgeting -------------
        # The declared window (llm.total_context_tokens) minus generation
        # reserve and margin, at the configured utilisation (default 0.60),
        # converted to a char cap via the MEASURED density (updated per turn
        # from usage eval counts; conservative floor 3.0). When the allocator
        # is configured it OWNS the request ceiling: max_prompt_chars is
        # retired for this instance (the yaml key is gone from residentb's
        # config; residenta keeps its the designer-ruled char cap this chunk).
        import token_budget as _tb
        # §7.6: mem0 producer switch (extraction). Default ON for legacy
        # configs; yaml memory.mem0.producer: false shuts it down.
        _mem0_cfg = ((config.get('memory') or {}).get('mem0') or {})
        self._mem0_producer = bool(_mem0_cfg.get('producer', True)) and (
            os.getenv('CONTINUA_MEM0_PRODUCER', '1') != '0')
        self._token_window = _tb.window_from_config(config, self.instance_id)
        if self._token_window[2]:
            _total, _reserve, _pb, _util = self._token_window
            self._density = _tb.FALLBACK_DENSITY
            self._density_source = 'fallback'
            self._last_render_chars = 0
            self._prompt_char_cap = _tb.char_cap(_pb, self._density)
            logger.info(
                '[Core] token window: total=%d reserve=%d prompt_budget=%d tok '
                '(utilisation %.2f) -> char cap %d @ %.1f c/t (fallback)',
                _total, _reserve, _pb, _util, self._prompt_char_cap,
                self._density)
        else:
            self._prompt_char_cap = None

        # --- Tools (OpenAI function defs + Searchie client) ------------------
        raw_tools = tools_cfg  # list from YAML or None
        self._function_defs = _get_function_definition(
            raw_tools, getattr(self, "_mem_tools", None))
        self.tools_enabled = len(self._function_defs) > 0

        if self.tools_enabled:
            for tdef in self._function_defs:
                fname = tdef["function"]["name"]
                logger.info("[Core] Registered tool function: %s", fname)
            self.searchie_client = SearchieClient()
            self.tarot_client = TarotClient()

            # Build dynamic tool listing from registered definitions (not hardcoded text)
            tool_listing_parts = []
            for tdef in self._function_defs:
                name = tdef["function"]["name"]
                desc = tdef["function"]["description"]
                params_info = ""
                props = tdef.get("function", {}).get("parameters", {}).get("properties", {})
                if props:
                    param_strs = [f"    - {pn}: {pd.get('description', '')}" for pn, pd in props.items()]
                    params_info = "\n" + "\n".join(param_strs)
                tool_listing_parts.append(f"- **{name}**: {desc}{params_info}")
            self._registered_tool_listing = "\n\n".join(tool_listing_parts) if tool_listing_parts else ""
        else:
            self.searchie_client = None
            self._registered_tool_listing = ""
            logger.info("[Core] No tools registered; standard chat only.")

        # ----------------------------------------------------------------------
        logger.info(
            "SagentCore configuration map complete for %s. Startup block unblocked.",
            self.model,
        )

    def _request_char_cap(self):
        """The effective request char ceiling: the token allocator's derived
        cap when configured, else the legacy max_prompt_chars (0 = off)."""
        return self._prompt_char_cap if self._prompt_char_cap else self.max_prompt_chars

    def _update_measured_density(self):
        """Update the rolling chars/token density from the last real turn
        (usage eval counts), floored conservatively. Fail-open."""
        if not self._token_window[2] or not self._last_eval_count:
            return
        import token_budget as _tb
        tokens = self._last_eval_count
        if not tokens and self._last_prompt_tokens:
            tokens = self._last_prompt_tokens
        if not tokens:
            return
        try:
            self._density = _tb.measured_density(
                self._last_render_chars, tokens)
            self._density_source = 'measured'
            _pb = self._token_window[2]
            self._prompt_char_cap = _tb.char_cap(_pb, self._density)
        except Exception:
            pass


    def aclose(self) -> None:
        """Close all long-lived HTTP clients owned by this SagentCore.

        W13: invoked from the bridge's shutdown handler so SIGTERM /
        SIGINT doesn't leave httpx "unclosed client" warnings or
        pending asyncio tasks behind. Idempotent — safe to call
        twice. Each close is wrapped in a try/except so a failure
        on one client doesn't block the others.

        Order: openai first (most likely in flight), then searchie,
        then tarot.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True

        # OpenAI / httpx
        try:
            client = getattr(self, "openai_client", None)
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("[Core] Error closing openai_client: %s", exc)

        # Searchie
        try:
            sec = getattr(self, "searchie_client", None)
            if sec is not None and hasattr(sec, "aclose"):
                sec.aclose()
        except Exception as exc:  # pragma: no cover
            logger.warning("[Core] Error closing searchie_client: %s", exc)

    def _init_memory(self):
        # §7.6/§7.7: when the producer is off, the mem0 engine has no live
        # caller — never allocate it (the sealed archive is read-only; the
        # lazy-allocation attempts were logging Permission-denied noise on
        # every chat turn). Callers' None-fallbacks handle the rest.
        if not getattr(self, '_mem0_producer', True):
            self.memory = None
            return
        if self.memory is not None:
            return
        # Process-wide serialization (see _mem0_global_alloc_lock above):
        # the rebind inside the locked body mutates shared module state, so
        # the WHOLE rebind→from_config→wrap sequence must be atomic across
        # agents, not just within one SagentCore.
        with _mem0_global_alloc_lock:
            return self._init_memory_locked()

    def _init_memory_locked(self):
        if self.memory is not None:
            return
        with self._memory_lock:
            if self.memory is not None:  # double-check after acquiring lock
                return
            try:
                # Per-agent rebind of mem0's module-level mem0_dir. Required
                # for any agent after the first in this process — the env
                # var set in __init__ is ignored because mem0.memory.setup
                # was already imported and bound to the first agent's path.
                # Safe under the GIL: no awaits between this rebind and the
                # Memory.from_config call below, so two agents racing here
                # can't interleave their (patch, from_config) sequences.
                if _mem0_setup is not None:
                    _mem0_setup.mem0_dir = self._agent_mem0_dir
                    _mem0_main.mem0_dir = self._agent_mem0_dir
                logger.info(
                    "Executing lazy-allocation of Mem0 engine for model %s...", self.model
                )
                self.memory = Memory.from_config(self.mem0_config)

                # Disable thinking for mem0's LLM extraction calls. residentb4
                # reasons by default (spending ~800 thinking tokens + ~15-30s
                # per extraction, and occasionally emitting malformed JSON
                # when reasoning output is truncated -> dropped memories).
                # llama.cpp honors a per-request chat_template_kwargs
                # override, so wrap mem0's provider to inject
                # enable_thinking=false on every call. No server config
                # change; other agents keep their thinking.
                # Off-switch: SAGENT_MEM0_NO_THINK=0 restores thinking.
                # [CONTINUA] 2026-09-13: restructured — the wrapper is now
                # ALWAYS installed (observability is not optional) and the
                # no-think injection honors its env gate inside. WHY: the
                # 0-fact wake extractions fail UPSTREAM of extract_json (the
                # salvage wrapper never fired), so the failure is only
                # visible at the LLM boundary: how long the call took, what
                # came back (empty? None? garbled?), or what it raised.
                # Low volume: extraction + dedup rewrites only.
                _orig_llm_generate = self.memory.llm.generate_response

                def _observed_llm_generate(
                    messages, response_format=None, tools=None, tool_choice="auto", **kwargs
                ):
                    import time as _t
                    _t0 = _t.time()
                    _kwargs = dict(kwargs)
                    if os.getenv("SAGENT_MEM0_NO_THINK", "1") == "1":
                        _kwargs["extra_body"] = {
                            "chat_template_kwargs": {"enable_thinking": False}
                        }
                    try:
                        _resp = _orig_llm_generate(
                            messages=messages,
                            response_format=response_format,
                            tools=tools,
                            tool_choice=tool_choice,
                            **_kwargs,
                        )
                    except Exception as _e:
                        logger.warning(
                            "[Core] mem0 llm call RAISED after %.1fs: %r",
                            _t.time() - _t0, _e,
                        )
                        raise
                    _s = _resp if isinstance(_resp, str) else str(_resp)
                    logger.info(
                        "[Core] mem0 llm response: %.1fs, msgs=%d, len=%d, "
                        "head=%r",
                        _t.time() - _t0, len(messages or []), len(_s),
                        _s[:280],
                    )
                    return _resp

                self.memory.llm.generate_response = _observed_llm_generate
                logger.info(
                    "Mem0 extraction LLM wrapped: observability on + "
                    "no-think (SAGENT_MEM0_NO_THINK=1)",
                )

                # M3 (memfixes82826.md): salvage JSON from residentb4 extraction
                # responses that carry trailing prose or a second JSON object.
                # mem0's extract_json takes first-{ → last-}, which spans BOTH
                # objects and then fails json.loads with "Extra data" —
                # silently dropping every fact for that turn (11 losses in the
                # audit window). Rebind extract_json ON THE MAIN MODULE:
                # mem0.memory.main imports it by name, so patching
                # mem0.memory.utils is a no-op there.
                if os.getenv("SAGENT_MEM0_JSON_SALVAGE", "1") == "1":
                    try:
                        # NB: this local alias must NOT be named _mem0_main —
                        # a name imported/assigned anywhere in a function body
                        # is LOCAL to the whole function, which would shadow
                        # the module global and make the per-agent rebind
                        # `_mem0_main.mem0_dir = ...` above raise
                        # UnboundLocalError (silently killing memory for
                        # every agent: searches empty, saves skipped).
                        # 2026-08-28: the original name did exactly that after
                        # 48af326 added this block — see regression test
                        # test_init_memory_scoping in eval/test_m4_m6.py.
                        import mem0.memory.main as _mem0_main_mod

                        _orig_extract_json = _mem0_main_mod.extract_json

                        def _salvage_extract_json(text, _orig=_orig_extract_json):
                            import json as _json
                            import re as _re

                            # [CONTINUA] 2026-09-13: raw-output observability —
                            # production 0-fact extractions (0 facts on a retry,
                            # even) were undiagnosable because the model's raw
                            # response was swallowed on both the success and
                            # failure paths. extract_json runs once per add(),
                            # so this log line is low-volume and makes the next
                            # empty self-describe (empty parse? prose? think
                            # bleed? already-known judgment?).
                            try:
                                logger.info(
                                    "[Core] mem0 extract_json raw model output "
                                    "(first 400): %s",
                                    (text[:400] if isinstance(text, str)
                                     else str(text)[:400]),
                                )
                            except Exception:
                                pass

                            def _parses(s):
                                try:
                                    _json.loads(s, strict=False)
                                    return True
                                except Exception:
                                    return False

                            candidate = _orig(text)
                            if _parses(candidate):
                                # [CONTINUA] 2026-09-13: schema-drift alias on
                                # the SUCCESS path too — stock parse succeeds
                                # on {"memory": [...]} (valid JSON, wrong
                                # key) and mem0 would read zero facts from it.
                                try:
                                    _obj = _json.loads(candidate, strict=False)
                                    if (isinstance(_obj, dict)
                                            and "facts" not in _obj
                                            and isinstance(_obj.get("memory"), list)):
                                        logger.warning(
                                            "[Core] mem0 extraction schema drift "
                                            "(memory->facts alias applied)"
                                        )
                                        return _json.dumps(
                                            {"facts": _obj["memory"]})
                                except Exception:
                                    pass
                                return candidate
                            t = text if isinstance(text, str) else str(text)
                            t = t.strip()
                            m = _re.search(r"```(?:json)?\s*(.*?)\s*```", t, _re.DOTALL)
                            if m:
                                t = m.group(1).strip()
                            start = t.find("{")
                            if start == -1:
                                return candidate
                            try:
                                obj, _end = _json.JSONDecoder().raw_decode(t[start:])
                                # [CONTINUA] 2026-09-13: schema-drift alias —
                                # residentb4 occasionally answers the extraction
                                # prompt in its own schema ({"memory": [...]}
                                # instead of {"facts": [...]}); caught live on
                                # the raw-output wire (17:01 quiet wake). An
                                # empty drifted list is harmless, a POPULATED
                                # one would be silently dropped — accept the
                                # alias so drift can never eat facts.
                                if isinstance(obj, dict) and "facts" not in obj \
                                        and isinstance(obj.get("memory"), list):
                                    obj = {"facts": obj["memory"]}
                                    logger.warning(
                                        "[Core] mem0 extraction schema drift "
                                        "(memory->facts alias applied in salvage)"
                                    )
                                logger.warning(
                                    "[Core] mem0 extraction JSON salvaged via raw_decode "
                                    "(original extract_json output was unparseable)"
                                )
                                return _json.dumps(obj)
                            except Exception:
                                return candidate

                        _mem0_main_mod.extract_json = _salvage_extract_json
                        logger.info(
                            "Mem0 extraction JSON salvage enabled "
                            "(SAGENT_MEM0_JSON_SALVAGE=1)"
                        )
                    except Exception as _salv_err:
                        logger.warning(
                            "JSON salvage patch skipped (non-fatal): %s", _salv_err
                        )
                logger.info(
                    "Mem0 repository layer successfully bound to instance (%s).",
                    self.model,
                )
            except Exception as e:
                logger.warning(
                    "Mem0 engine allocation skipped or timed out for model %s: %s. Turning on dynamic bypass.",
                    self.model,
                    e,
                )

    # ------------------------------------------------------------------
    # History rendering (HISTTS, 2026-09-05)
    # ------------------------------------------------------------------

    # Env kill switch: SAGENT_HISTORY_TS=0 suppresses the [date time]
    # prefixes at the LLM boundary. Storage is unaffected — entries keep
    # their ts field either way.
    def _compress_think_for_history(self, think: str) -> str:
        """Compress a reply's reasoning for history re-rendering: whitespace-
        normalize, keep the first ~240c (the planning register), cut at a
        sentence boundary when one exists past 120c. Empty think -> empty."""
        t = " ".join((think or "").split())
        if not t:
            return ""
        if len(t) <= 240:
            return t
        cut = t[:240]
        dot = cut.rfind(". ")
        return cut[:dot + 1] if dot > 120 else cut

    def _latest_real_think_prefill(self) -> str:
        """[CONTINUA] 2026-09-16 (chat-think-contract spec, Layer 2): her most
        recent captured REAL think — any channel, from the chronicle —
        compressed to the planning register for the anchored retry's prefill.
        Never fabricated: empty string when nothing exists (the retry then
        runs anchor-only). Fail-open."""
        try:
            import chronicle as _chron
            _t = _chron.latest_reasoning(getattr(self, "instance_id", ""))
        except Exception:
            return ""
        return self._compress_think_for_history(_t)

    def _assistant_hist_entry(self, content_str: str) -> dict:
        """Assistant entry for the persistent history; carries the compressed
        think when history_thinks=real so the raw composer can render it.

        [CONTINUA] 2026-09-24 (ts-stacking fix): this is now the CENTRAL
        history-hygiene choke point. Wound: the cleansweep enforcers ran only
        on final_content (delivery + chronicle) AFTER this method had already
        appended the RAW generation into working_messages at every tool-round
        site (5934/5961/6031) and the clean-break site — so the persistent
        history kept the timestamp prefixes the enforcer had stripped from
        the delivered copy, and every new prompt re-fed them as few-shot.
        Observed compounding: residentb's wake thread accumulated 15-16 stacked
        leading prefixes per reply (+1 per wake). Both enforcers (ts-prefix
        strip + HTML sanitize) now run HERE, on every assistant entry at
        append time, covering all call sites by construction. Fail-open pure
        ops; the delivered final_content still gets its own belt-and-
        suspenders pass at 6141 (idempotent)."""
        if content_str:
            if getattr(self, "_strip_ts_prefix", True):
                content_str = _strip_leading_timestamp(content_str)
            if getattr(self, "_sanitize_history", True):
                content_str = _sanitize_history_html(content_str)
        entry = {"role": "assistant", "content": content_str}
        if self._history_thinks == "real":
            _ct = self._compress_think_for_history(getattr(self, "_last_reasoning", ""))
            if _ct:
                entry["_think"] = _ct
        return entry

    @staticmethod
    def _render_history_message(msg: dict) -> dict:
        """Render one stored history entry into an LLM-facing message.

        Entries carrying a ts (see bridge._now_iso) get a [YYYY-MM-DD HH:MM]
        prefix on their content so the model can reason about when past
        turns happened. Never mutates the stored entry — history dicts and
        their multimodal blocks are copied before any content change.

        Rules:
          - user/assistant with ts  -> prefix
          - entry without ts        -> rendered unchanged (pre-feature data,
                                       or tool-role messages, which are
                                       intra-turn and never stamped)

        [CONTINUA] 2026-09-24 (ts-stacking fix, render-side): assistant
        entries written before the central hygiene fix still carry leading
        [YYYY-MM-DD…] prefixes the model then imitates. Stripping them HERE
        means even dirty stored entries render clean — the render prefix is
        the only bracket the model ever sees. Never mutates the stored
        entry (the strip works on the copy); user turns are left untouched
        (a human's literal words could legitimately begin with a bracket).
        """
        role = msg.get("role")
        ts = msg.get("ts")
        if not ts or role not in ("user", "assistant"):
            return {"role": role, "content": msg.get("content")}
        # Parse robustly: malformed ts -> fall back to the raw string.
        try:
            stamp = datetime.fromisoformat(str(ts)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            stamp = str(ts)[:16].replace("T", " ")
        prefix = f"[{stamp}] "
        content = msg.get("content")
        if role == "assistant" and isinstance(content, str):
            content = _strip_leading_timestamp(content)
        if isinstance(content, list):
            blocks = [dict(b) if isinstance(b, dict) else b for b in content]
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text":
                    b["text"] = prefix + str(b.get("text", ""))
                    break
            else:
                blocks.insert(0, {"type": "text", "text": prefix.rstrip()})
            return {"role": role, "content": blocks}
        return {"role": role, "content": prefix + str(content)}

    # ------------------------------------------------------------------
    # History pruning
    # ------------------------------------------------------------------

    def _trim_history(self, chat_history: list) -> list:
        """Trim old messages; returns just the kept list (see _trim_history_with_evicted)."""
        kept, _evicted = self._trim_history_with_evicted(chat_history)
        return kept

    def _trim_history_with_evicted(self, chat_history: list,
                                   budget_chars: int = None,
                                   target_chars: int = None) -> tuple:
        """Trim old messages from chat_history to stay under self.max_history_chars.

        Keeps the most recent messages by trimming from the front until total chars
        are within budget, or only 4 messages remain (a safety net).  Also purges
        tool-assistant/tool-result pairs from earlier turns that have expanded well
        beyond their original user text input to free more room for recent chat.

        P1 (sagentv3.md Upgrade 1): returns ``(kept, evicted)`` so evicted oldest
        messages can be folded into the persistent session summary instead of
        being silently dropped.
        """
        if not chat_history:
            return chat_history, []

        evicted: list = []
        messages = list(chat_history)  # work on a copy
        # HEADROOM rule (2026-09-04): trim to 70% of the budget, not the ceiling.
        # Absorbs prompt-format overhead (im-markers, the think scaffold), the
        # load-dependent per-request clamp, and the memory block's variance —
        # so the system prompt is never the thing the silent truncation eats.
        # [CONTINUA] budget_chars = total-prompt regime: the caller already
        # subtracted the fixed prompt reserve (system+tools+injection room).
        # [CONTINUA] 2026-09-13: target_chars (when given) IS the window —
        # applied directly, no headroom discount (the explicit-window regime,
        # house ruling 2026-09-13: 20000 chars both residents). The 0.70
        # headroom discount belongs to the derived regime only, where the
        # budget is a ceiling (prompt-cap minus reserve) rather than a
        # window.
        if target_chars:
            max_chars = int(target_chars)
        else:
            _budget = budget_chars if budget_chars else self.max_history_chars
            max_chars = int(_budget * 0.70)

        def _charcount(msg):
            c = msg.get("content", "")
            if isinstance(c, list):
                return sum(len(b["text"]) for b in c if b.get("type") == "text")
            return len(c) if isinstance(c, str) else 0

        while True:
            total_chars = sum(_charcount(msg) for msg in messages)
            if total_chars <= max_chars or len(messages) <= 4:
                break

            removed = messages.pop(0)
            evicted.append(removed)
            # [CONTINUA] chunk 3 (memory plan §6g exit: no tool pair is split):
            # if the evicted assistant carried tool calls, its tool results
            # belong to it — evict them too. If the front is an orphan tool
            # result (its assistant already evicted), evict it as well. A tool
            # pair is never separated across the eviction boundary.
            this_round = [removed]
            while messages and messages[0].get("role") == "tool":
                orphan = messages.pop(0)
                evicted.append(orphan)
                this_round.append(orphan)
            char_freed = sum(_charcount(m) for m in this_round)
            logger.info(
                "[Core] Pruned history message (%s/%d chars freed)",
                self.instance_id, char_freed,
            )

        # Post-trim safety: if even 4 messages still exceed budget, truncate the largest
        final_total = sum(_charcount(msg) for msg in messages)
        if final_total > max_chars:
            # Find the largest message by character count
            idx = max(range(len(messages)), key=lambda i: _charcount(messages[i]))
            content = messages[idx].get("content", "")
            if isinstance(content, str):
                # [CONTINUA] 2026-09-13: subtract the marker length before
                # cutting — the marker was appended AFTER truncating to the
                # full remaining budget, so the "cap" overshot by the
                # marker's own length.
                _marker = "... [content truncated to fit API budget]"
                budget_remaining = max_chars - (final_total - _charcount(messages[idx]))
                if len(content) > budget_remaining:
                    messages[idx] = dict(messages[idx])
                    messages[idx]["content"] = (
                        content[:max(500, budget_remaining - len(_marker))] + _marker
                    )
                    logger.info("[Core] Truncated largest history message (%d chars) to fit %d-char budget.", final_total, max_chars)
            elif isinstance(content, list):
                budget_remaining = max_chars - (final_total - _charcount(messages[idx]))
                messages[idx] = dict(messages[idx])
                text_blocks = [b for b in content if b.get("type") == "text"]
                other_blocks = [b for b in content if b.get("type") != "text"]
                if text_blocks:
                    total_text = sum(len(b["text"]) for b in text_blocks)
                    if total_text > budget_remaining:
                        per_block = max(1, budget_remaining // len(text_blocks))
                        texts = []
                        for bi, block in enumerate(text_blocks):
                            cut_len = per_block if bi < len(text_blocks) - 1 else budget_remaining - sum(len(t.get("text", "")) for t in texts)
                            text = block.get("text", "")
                            if len(text) > cut_len:
                                txt = dict(block)
                                txt["text"] = text[:cut_len] + "... [truncated]"
                                texts.append(txt)
                            else:
                                texts.append(block)
                        messages[idx]["content"] = texts + other_blocks
                    logger.info("[Core] Truncated multimodal history message (%d chars) to fit %d-char budget.", final_total, max_chars)

        return messages, evicted

    # ------------------------------------------------------------------
    # Memory persistence helper (centralizes all exit paths)
    # ------------------------------------------------------------------

    def _save_turn_memory(self, user_id: str, history: list, last_user_msg: str,
                          summary_path: str = "", evicted_messages: Optional[list] = None,
                          log=None):
        """Persist user→assistant exchange to long-term memory if available.

        Safe to call from a background thread (via _save_turn_memory_async).
        The body runs under _memory_save_lock so concurrent extractions for
        the same agent don't race on the shared Memory instance.

        P1: when ``summary_path`` is provided, also folds evicted window
        messages into the persistent session summary
        (session_memory.maybe_update_summary — qwen card, never residentb,
        failure-tolerant). Runs after mem0 add so extraction latency wins.

        M1 (memfixes82826.md): ``evicted_messages`` carries the messages that
        just fell out of the working window — folded directly, replacing the
        stale-able index cursor. The fold is independent of mem0 health: when
        mem0 is bypassed (``self.memory is None``) extraction is skipped but
        the summary fold still runs.

        M7: ``log`` is the per-request LoggerAdapter so these background lines
        carry the same rid/inst as the chat turn that produced them.
        """
        _log = log or logger
        evicted_messages = evicted_messages or []
        # §7.6 producer shutdown (2026-09-19 house ruling): mem0 EXTRACTION is
        # off — its injection store is retired and archive-track; extraction
        # wrote facts nothing injects anymore. The summary fold (independent
        # of mem0) still runs below. Rollback: memory.mem0.producer: true.
        if not self._mem0_producer:
            if not evicted_messages:
                return
        elif self.memory is None and not evicted_messages:
            return
        with self._memory_save_lock:
            # [CONTINUA] 2026-09-13: extraction payload = the FULL turn
            # transcript (see _turn_transcript_payload — the 0-fact wake
            # extraction fix).
            payload = _turn_transcript_payload(history, last_user_msg)
            _t_add = time()
            _added_ids: list = []
            _add_res: dict = {"results": []}
            try:
                _add_res = (self.memory.add(payload, user_id=user_id,
                                            metadata={"instance_id": self.instance_id})
                            if (self.memory is not None and self._mem0_producer)
                            else {"results": []})
                for r in (_add_res.get("results", []) if isinstance(_add_res, dict) else (_add_res or [])):
                    if isinstance(r, dict) and r.get("id") and r.get("event") != "NOOP":
                        _added_ids.append(r["id"])
                # [CONTINUA] 2026-09-13: one retry on empty — extraction is
                # sampling-sensitive (mem0 default temp 0.1 drew an empty
                # parse on identical-quality turns; temperature is now pinned
                # to 0, and the retry guards the residual transient causes:
                # server load, truncated responses). Fail-open: if the retry
                # is also empty, the turn's facts are gone — the day-delta
                # briefing + session summary still carry the content.
                if not _added_ids and self.memory is not None:
                    _log.info("[Core] mem0 extraction empty on first pass — "
                              "retrying once for user %s", user_id)
                    _add_res = self.memory.add(payload, user_id=user_id,
                                               metadata={"instance_id": self.instance_id})
                    for r in (_add_res.get("results", []) if isinstance(_add_res, dict) else (_add_res or [])):
                        if isinstance(r, dict) and r.get("id") and r.get("event") != "NOOP":
                            _added_ids.append(r["id"])
                _log.info(
                    "[Core] mem0 extraction: %.2fs for user %s (instance %s, %d facts)",
                    time() - _t_add, user_id, self.instance_id, len(_added_ids),
                )
            except Exception as e:
                _log.warning(
                    "Non-fatal background memory extraction drop for User %s after %.2fs: %s",
                    user_id, time() - _t_add, e,
                )
            # M4 (memfixes82826.md): classify newly added facts. Assistant-voice
            # takeaways ("The assistant advised...", "sagent_sequoia warns...")
            # are the agent's own conclusions, not user facts — tag them under
            # payload key `kind` so recall can penalize + budget-cap them.
            # Unclassified/old memories default to full weight at recall.
            if _added_ids and self.memory is not None:
                try:
                    _ta_re = re.compile(
                        r"^(?:the\s+assistant\b|"
                        + re.escape(self.instance_id) + r"\b|"
                        + re.escape(getattr(self, "agent_name", "") or "") + r"\b)",
                        re.IGNORECASE,
                    )
                    _kind_map: dict = {}
                    # Provenance (memprovenance0929.md): widen the net —
                    # assistant-voice prefixes stay `assistant_takeaway`;
                    # self-referential CONTENT (her favorites, feelings,
                    # experiences, self-descriptions) becomes `self_statement`
                    # (dropped at recall under exclude/strict policies).
                    _self_re = re.compile(
                        r"(?:\b" + re.escape(getattr(self, "agent_name", "") or "x")
                        + r"(?:'s)?\s+|\b(?:her|his)\s+)"
                        r"(?:favorites?|likes?|loves?|prefers?|enjoys?|hates?|feels?|listens?|experiences?)",
                        re.IGNORECASE,
                    )
                    for r in (_add_res.get("results", []) if isinstance(_add_res, dict) else (_add_res or [])):
                        if not (isinstance(r, dict) and r.get("id")):
                            continue
                        mtext = (r.get("memory") or r.get("data") or "").strip()
                        if not mtext:
                            continue
                        # self-referential CONTENT outranks the voice-prefix:
                        # "persona-a's favorite music" is identity-voice even though
                        # it starts with her name (takeaway prefix would eat it)
                        if _self_re.search(mtext):
                            _k = "self_statement"
                        elif _ta_re.match(mtext):
                            _k = "assistant_takeaway"
                        else:
                            _k = "user_fact"
                        _kind_map[str(r["id"])] = _k
                    _ta_ids = [i for i, k in _kind_map.items() if k == "assistant_takeaway"]
                    _ss_ids = [i for i, k in _kind_map.items() if k == "self_statement"]
                    if _ta_ids or _ss_ids:
                        _client = self.memory.vector_store.client
                        _coll = self.mem0_config["vector_store"]["config"]["collection_name"]
                        if _ta_ids:
                            _client.set_payload(
                                collection_name=_coll,
                                payload={"kind": "assistant_takeaway"},
                                points=_ta_ids,
                            )
                        if _ss_ids:
                            _client.set_payload(
                                collection_name=_coll,
                                payload={"kind": "self_statement"},
                                points=_ss_ids,
                            )
                        _log.info(
                            "[Core] tagged %d/%d new facts (takeaway=%d, self_statement=%d)",
                            len(_ta_ids) + len(_ss_ids), len(_kind_map),
                            len(_ta_ids), len(_ss_ids),
                        )
                except Exception as tag_err:
                    _log.warning("[Core] takeaway tagging failed (non-fatal): %s", tag_err)
            # P2 (sagentv3.md Upgrade 2): soft-invalidate memories the new facts
            # contradict. Background worker only; qwen judge; fail-open — any
            # error leaves everything live.
            try:
                import temporal_memory
                if temporal_memory.TEMPORAL_ENABLED and _added_ids:
                    collection = self.mem0_config["vector_store"]["config"]["collection_name"]
                    conflicts = temporal_memory.detect_conflicts(
                        self.memory, payload, user_id, self.instance_id,
                        exclude_ids=_added_ids,
                    )
                    if conflicts:
                        supersede_by = _added_ids[0] if len(_added_ids) == 1 else "NEW_FACT"
                        temporal_memory.mark_superseded(
                            self.memory, collection,
                            [c["id"] for c in conflicts], supersede_by,
                        )
                        _log.info(
                            "[Temporal] %d conflicting memories superseded by %s for user %s",
                            len(conflicts), supersede_by, user_id,
                        )
            except Exception as e:
                _log.warning("[Temporal] post-add scan failed (non-fatal): %s", e)
        # P1: session-summary fold (independent of mem0 success/failure)
        if summary_path and not self._mem_layers['recollections']['enabled']:
            try:
                import session_memory
                session_memory.maybe_update_summary(
                    summary_path, history, evicted_messages=evicted_messages)
            except Exception as e:
                _log.warning("[SessionSummary] background fold error for User %s: %s", user_id, e)

    def forget_memories(self, user_id: str, query: str, top_k: int = 50) -> list:
        """P2: SOFT-delete memories matching a query — mark them
        ``superseded_by="USER_FORGET"`` instead of deleting. Returns matched
        memory dicts (with ids) that were marked.

        Nothing is removed from the store; /searchmem shows them with a
        [superseded] marker and recall skips them unless the query is
        historical. Data-safety invariant §7.3 stays intact.
        """
        if self.memory is None:
            self._init_memory()
        if self.memory is None:
            return []
        matches = self.search_memories(user_id, query=query, top_k=top_k)
        if not matches:
            return []
        collection = self.mem0_config["vector_store"]["config"]["collection_name"]
        import temporal_memory
        marked = temporal_memory.mark_superseded(
            self.memory, collection,
            [m["id"] for m in matches if m.get("id")], "USER_FORGET",
        )
        logger.info("[Temporal] /forget: marked %d/%d memories for user %s (instance %s)",
                    marked, len(matches), user_id, self.instance_id)
        return matches

    def _save_turn_memory_async(self, user_id: str, history: list, last_user_msg: str, request_id: str = "", summary_path: str = "", evicted_messages: Optional[list] = None):
        """Enqueue a memory extraction for background processing.

        W05: replaces the per-turn daemon thread with a process-wide
        bounded queue + worker pool. When the queue is full, the new
        job is dropped (logged) so a chat turn's response is never
        blocked by Mem0 latency. See
        plan/v2/W05-bounded-mem0-queue.md.

        `history` is shallow-copied before being handed to the queue
        so the bridge can safely mutate the original after we return.

        W07: request_id is forwarded so the worker's log line carries
        the same id as the chat turn that produced the extraction.

        M1 (memfixes82826.md): evicted_messages carries the messages
        that _trim_history_with_evicted just dropped from the working
        window, so the session-summary fold folds exactly what left
        (no index cursor to go stale).
        """
        if self.memory is None and not evicted_messages:
            return
        if not history and not evicted_messages:
            return
        if not summary_path:
            summary_path = self._summary_paths.get(user_id, "")
        job = (self, user_id, list(history), last_user_msg, request_id, summary_path, list(evicted_messages or []))
        try:
            _memory_queue.put_nowait(job)
        except queue.Full:
            logger.warning(
                "[Mem0] Queue full; dropping extraction for %s/%s (queue size=%d, request_id=%s)",
                self.instance_id, user_id, _memory_queue.maxsize, request_id or "-",
            )

    # ------------------------------------------------------------------
    # Memory inspection / management (used by /searchmem, /deletemem)
    # ------------------------------------------------------------------
    # Scope is enforced here (not in the bridge) so the call sites can't
    # accidentally query across agents or users. Each agent's Memory
    # instance points at its own per-agent Qdrant collection, so a
    # `user_id`-only filter is implicitly agent-scoped.

    # ------------------------------------------------------------------
    # P4: hybrid rerank + memory budget
    # ------------------------------------------------------------------

    SUPERSEDED_PENALTY = 0.7
    # M4 (memfixes82826.md): assistant-voice takeaway down-ranking. The audit
    # found 34% of new facts were the agent's own conclusions ("The assistant
    # advised...", "sagent_sequoia warns...") — stored as world-facts and
    # re-injected until they become the model's "lens" (Narrative Capture).
    # Tagged at save time under payload key `kind`; penalized + budget-capped
    # at recall so they can inform but never dominate the context block.
    TAKEAWAY_PENALTY = float(os.getenv("SAGENT_TAKEAWAY_PENALTY", "0.60"))
    TAKEAWAY_CHAR_SHARE = float(os.getenv("SAGENT_TAKEAWAY_SHARE", "0.25"))
    # memfixes post-eval fix (Phase 5 diagnosis): on historical/summary
    # queries ("summarize what we discussed", "when did we...") the
    # assistant's own takeaways ARE the gold source — penalizing them there
    # cost 2 judge points (memeval-021/110). When enabled, both the M4 score
    # penalty and the M4 char cap are skipped for historical queries.
    # Default OFF: eval-gated, enable via env (SAGENT_TAKEAWAY_HISTORICAL_EXEMPT=1).
    TAKEAWAY_HISTORICAL_EXEMPT = os.getenv("SAGENT_TAKEAWAY_HISTORICAL_EXEMPT", "0") == "1"
    # M6 (memfixes82826.md): injection floor — the audit showed recall fills
    # the whole budget on nearly every turn (weak best-scores included). Facts
    # below this mem0 score are dropped even when budget remains: no relevant
    # context beats noisy context. Entity-aug items carry synthetic 0.45.
    MEMORY_MIN_INJECT_SCORE = float(os.getenv("SAGENT_MEMORY_MIN_SCORE", "0.38"))
    # M6: MMR redundancy penalty during selection (pool > 8 items only).
    # Suppresses the 6x-paraphrase clusters the audit measured (50x BF16).
    MMR_ENABLED = os.getenv("SAGENT_MEMORY_MMR", "1") == "1"
    MMR_LAMBDA = float(os.getenv("SAGENT_MEMORY_MMR_LAMBDA", "0.35"))
    # search_my_memories tool curation (2026-08-28): when ON, the bot's
    # self-serve memory pull runs through recall_block — the SAME pipeline as
    # auto-injection (floor/MMR/char budget/superseded filter) — instead of
    # the legacy bare top-k search that returned filler and superseded rows.
    # Default OFF: eval-gated (the 53-item eval exercises the injection path,
    # not tool turns, so this flag is verified by unit tests + live use).
    TOOL_RECALL_CURIATED = os.getenv("SAGENT_TOOL_RECALL_CURIATED", "0") == "1"
    # Decision B (2026-08-28, the designer: "stop the filler"): SHORT queries (focused
    # user message <= SHORT_QUERY_MAX_TOKENS tokens) pull vector-centroid
    # filler at 0.33–0.55 — "User's name is Alex" tops every vague one-word
    # query (measured on spike 2026-08-28) — while true matches jump to 0.60+
    # when the topic IS in the store (Zippo 0.665; shed-on-sequoia 0.55–0.60).
    # Strict mode raises the injection floor for short queries; one-word
    # queries may also pass via LITERAL presence (word or singular in the
    # text) at a moderate floor — a literal hit on a one-word query is strong
    # relevance evidence. Default OFF: eval-gated (house convention).
    SHORT_QUERY_STRICT = os.getenv("SAGENT_SHORT_QUERY_STRICT", "0") == "1"
    SHORT_QUERY_MAX_TOKENS = int(os.getenv("SAGENT_SHORT_QUERY_MAX_TOKENS", "2"))
    SHORT_QUERY_FLOOR = float(os.getenv("SAGENT_SHORT_QUERY_FLOOR", "0.55"))
    SHORT_QUERY_LITERAL_FLOOR = float(os.getenv("SAGENT_SHORT_QUERY_LITERAL_FLOOR", "0.40"))
    # M6: skip recall entirely on greeting/ack filler (nothing useful to pull,
    # and filler queries widen into weak pools that pollute the context block).
    SMALLTALK_SKIP_ENABLED = os.getenv("SAGENT_SMALLTALK_SKIP", "1") == "1"
    SMALLTALK_MAX_CHARS = 80
    _SMALLTALK_RE = re.compile(
        r"^(hi|hello|hey|yo|greetings|good\s+(morning|afternoon|evening)\b"
        r"|how\s+(are|r)\s+(you|u)\b|how's it going|what's up|whats up"
        r"|thanks|thank you|ty\b|ok\b|okay\b|k\b|great\b|cool\b|nice\b"
        r"|goodnight|good night|gn\b|good bot|lol\b|haha\b"
        r"|what\s+do\s+you\s+want\s+to\s+talk\s+about"  # audit msg weak-pool turn
        r"|you\s+are\s+on\s+my\s+mind)",  # audit msg weak-pool turn
        re.IGNORECASE,
    )

    # Date-aware retrieval (post-v3 #1): recency boost ONLY for queries with
    # recent-temporal intent (temporal_memory.wants_recency). Global decay was
    # rejected in P4 evals; this gates it to where recency IS the signal.
    RECENCY_BOOST_ENABLED_EVAL = os.getenv("SAGENT_RECENCY_BOOST", "1") == "1"
    RECENCY_BOOST_MAX = 0.5
    RECENCY_HORIZON_DAYS = 14.0
    # P4b A/B: native bm25 (post-rebuild) may make this redundant; gate for
    # eval comparison. Default ON until data says otherwise.
    LEXICAL_BOOST_ENABLED = os.getenv("SAGENT_LEXICAL_BOOST", "0") == "1"
    LEXICAL_BOOST_PER_TOKEN = 0.20
    _STOPWORDS = {"this", "that", "with", "have", "about", "like", "still",
                  "your", "you", "are", "was", "were", "them", "they"}

    @classmethod
    def _content_tokens(cls, text: str) -> set:
        """Content words (len>=4, no stopwords), with light stem-prefix
        tolerance applied at match time (wheeler~wheelers)."""
        return set(t for t in re.findall(r"[a-z0-9']{4,}", text.lower())
                   if t not in cls._STOPWORDS)

    @classmethod
    def _is_smalltalk(cls, text: str) -> bool:
        """M6: greeting/ack filler detection for recall fast-path skip.
        Conservative by design: short AND matches a filler opener. Real
        casual-but-informative turns (e.g. the chicken-dinner message, 111
        chars) do NOT match — they keep full recall."""
        t = (text or "").strip()
        if not t or len(t) > cls.SMALLTALK_MAX_CHARS:
            return False
        return bool(cls._SMALLTALK_RE.match(t))

    @classmethod
    def _lexical_boost(cls, queries: List[str], text: str) -> float:
        """P4b: reward memories sharing distinctive tokens with the query.

        User insight (2026-08-25): a unique word like 'wheeler' should pull
        any memory containing it above semantically-adjacent noise. Legacy
        collections lack mem0's bm25 sparse slot, so keyword scoring never
        fires there — this reproduces it cheaply at rerank time.
        Boost grows with the number of DISTINCT matched content tokens.
        """
        if not cls.LEXICAL_BOOST_ENABLED:
            return 0.0
        dtoks = cls._content_tokens(text)
        best = 0.0
        for q in queries:
            qt = cls._content_tokens(q)
            if not qt:
                continue
            matched = set()
            for t in qt:
                for d in dtoks:
                    if t == d or (len(t) >= 5 and (d.startswith(t) or t.startswith(d))):
                        matched.add(t)
                        break
            best = max(best, min(0.6, cls.LEXICAL_BOOST_PER_TOKEN * len(matched)))
        return best

    @staticmethod
    def _age_days(item: dict) -> Optional[float]:
        """Age of a memory in days from its created_at payload, or None."""
        raw = item.get("created_at")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _utc_to_local_date(raw) -> str:
        """HISTTZ (2026-09-05): memory dates shown to the agent, in LOCAL time.

        mem0 stores created_at in UTC (its storage convention), but every
        other date surface the agent sees (system-prompt clock, chat-history
        stamps) is local. Taking the raw UTC calendar date here stamped any
        memory written after 17:00 PDT with "tomorrow" — the 09-05-for-
        memories-written-09-04 incident. Convert to local before displaying.

        Returns "" for empty input, and the raw string's first 10 chars as a
        fallback when parsing fails (never raises).
        """
        if not raw:
            return ""
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone().strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return str(raw)[:10]

    def _build_continua_block(self, user_id: str, last_user_message: str,
                              chat_history: list) -> str:
        """[CONTINUA] 2026-09-13 (house ruling, Option A): the seven-claimant
        social block, now spec-driven by self._mem_layers. Layer order is
        canonical (talking-with → roster → self book → person book →
        day-delta → episodic) regardless of which are enabled; disabled
        layers leave no headers or gap artifacts. Byte-parity with the
        pre-config hardcoded assembly is proven by memory_layers_test.py.
        Fail-open unchanged: any layer error drops that layer; total
        failure returns "" (a Continua failure must never break a turn).
        """
        _L = self._mem_layers
        _parts = []
        try:
            import people as _people
            import recall as _recall
            _roster = _people.load_roster()
            _person_name = _people.name_for(_roster, user_id)
            if _L["talking_with_line"]["enabled"]:
                _parts.append(f"You are talking with {_person_name}.")
            if _L["roster"]["enabled"]:
                # [CONTINUA] 2026-09-13 (the designer ask): the roster rides in the
                # chat system block — the WAKE packet's PEOPLE YOU KNOW,
                # mirrored byte-for-byte (same rendering as
                # wake.build_payload). She reported the gap live ("I am
                # effectively blind to the roster"): send_message takes "a
                # name from your people", but the list only ever surfaced
                # in wake turns — a separate thread whose history never
                # mixes with chat. Format parity with the wake packet means
                # her two worlds agree; persona letters are delivered
                # in-process and their replies wait in her inbox
                # (check_mail), same as the letter model.
                _contacts = "\n".join(
                    f"- {p.display_name} ({p.person_id})"
                    + ("" if p.can_message else " — messaging off")
                    for p in _roster.values())
                _parts.append(
                    "[PEOPLE YOU KNOW — the roster send_message accepts; "
                    "address people by the names as written here. Entries "
                    "marked 'messaging off' cannot be messaged. The other "
                    "residents are persona letters: a send is delivered "
                    "in-process and their reply waits in your inbox — "
                    "check_mail]\n" + _contacts)
            if _L["day_delta"]["enabled"]:
                # [CONTINUA] 2026-09-13 (house ruling): the day-delta briefing
                # — see _day_delta_block. Placed here so the situational
                # layer (what happened in the gap) sits with the
                # relationship layer and above episodic recall.
                _cur_msg = chat_history[-1] if chat_history else {}
                _cur_ts = (str(_cur_msg.get("ts") or "")[:16]
                           if _cur_msg.get("role") == "user" else "")
                _prev_ts = ""
                if _cur_ts:
                    for _pm in reversed(chat_history[:-1]):
                        if (_pm.get("role") in ("user", "assistant")
                                and _pm.get("ts")):
                            _prev_ts = str(_pm["ts"])[:16]
                            break
                if _cur_ts and _prev_ts:
                    _day_block = _day_delta_block(
                        self.instance_id, _cur_ts, _prev_ts,
                        cap_chars=int(_L["day_delta"]["cfg"]["cap_chars"]))
                    if _day_block:
                        _parts.append(_day_block)
            if _L["episodic_recall"]["enabled"]:
                _hits = _recall.recall(
                    last_user_message, self.instance_id,
                    str(user_id), roster=_roster,
                    limit=int(_L["episodic_recall"]["cfg"]["limit"]))
                if _hits:
                    _ep = "\n".join(
                        f"- {h['attribution']} ({h['role']}): "
                        f"{(h['content'] or '')[:_L['episodic_recall']['cfg']['cap_chars']]}"
                        for h in _hits)
                    _parts.append(
                        "[Things you remember from past conversations — "
                        "attributed memories, NOT the current conversation; "
                        "reference them overtly, never as your own experience "
                        "of today]\n" + _ep)
            if _L["forever_events"]["enabled"]:
                # [CONTINUA] the long record (strata.py, house ruling
                # 2026-09-14): approved one-liner big events — the top of
                # the memory pyramid. Chronological; the designer-eyes gated; each
                # line a pointer to the verbatim record.
                try:
                    import strata as _strata
                    _fe = _strata.load_forever_events(self.instance_id)
                except Exception:
                    _fe = []
                if _fe:
                    _fe_cap = int(_L["forever_events"]["cfg"]["cap_chars"])
                    _fe_lines = [f"[{e['date']}] {e['event']}" for e in _fe]
                    _fe_block = "[THE LONG RECORD — the big events of your " \
                                "life here, one line each]\n" + "\n".join(_fe_lines)
                    if len(_fe_block) > _fe_cap:
                        _fe_block = _fe_block[:_fe_cap]
                    _parts.append(_fe_block)
            return "\n\n".join(_parts)
        except Exception:
            logger.warning("[Continua] context block failed (fail-open)",
                           exc_info=True)
            return ""

    def recall_block(self, user_id: str, last_user_message: str,
                     exclude_agent_notes: bool = False,
                     recent_user_texts: Optional[list] = None,
                     explain: bool = False) -> dict:
        """Single source of truth for what the wrapper injects for a message.

        The turn path, the search_my_memories tool (when
        SAGENT_TOOL_RECALL_CURIATED=1) and /searchmem ALL run through here,
        so "what the bot gets" is identical in every surface. Pipeline
        (verbatim move of the former inline turn-time block, 2026-08-28):

          focused search (last user message, threshold 0.30)
          -> weak-pass widen to the blended recent-user query
          -> should_recall_widen fan-out sub-queries
          -> entity augmentation (M2, on widen)
          -> temporal partition (superseded in only for historical queries)
          -> M4 kind tags + takeaway penalty/cap
          -> M6 floor + MMR + char budget (_rerank_memories)
          -> date stamping for temporal/historical queries

        Returns a dict:
          query    - blended recall query that was searched
          context  - the exact injected text ("" when nothing passes)
          injected - ranked items to be injected (memory already stamped
                     for temporal queries)
          dropped  - (item, reason) for search results NOT injected, when
                     explain=True: "superseded" / "below floor X.XX" /
                     "takeaway cap" / "char budget"
          hist / recency / widened - pipeline flags (display + logging)
        """
        # W07 parity: the original inline block logged through
        # generate_response's per-request LoggerAdapter. Rebuild the same
        # context here so every surface logs with request_id + instance_id.
        log = logging.LoggerAdapter(
            logger,
            {
                "request_id": getattr(self, "_current_request_id", "") or "-",
                "instance_id": self.instance_id,
            },
        )
        # Lazy mem0 init: /searchmem and the search tool can run BEFORE any
        # chat turn has allocated the engine (always true right after a
        # restart, until the first message to that agent). The legacy
        # /searchmem path went through search_memories(), which lazy-inits;
        # recall_block must do the same or self.memory stays None and the
        # broad except below swallows the AttributeError as "no memories"
        # (2026-08-28 20:46 spike incident — /searchmem found nothing on a
        # healthy store). _init_memory is idempotent and fail-open.
        if self.memory is None:
            self._init_memory()
        recent_user_texts = recent_user_texts or []
        recall_query = " | ".join(recent_user_texts) or last_user_message
        memory_context = ""
        _ctx_items = []
        _dropped = []
        _hist = False
        _recency = False
        _widen = False
        _t_search = time()
        try:
            # P1c adaptive recall: search the LAST user message first.
            # The unconditional blended query (P1) diluted signal when
            # prior turns were unrelated topics (verified: sagent_default
            # missed all ATC facts because a Qwen-9B question preceded
            # "...remember the 3 wheeler"). Widen to the blended query
            # only when the focused search comes back weak.
            # [CONTINUA] One-Self step 2: the partitions that make up HER
            # pool — the turn's own partition, her wake notes, and every
            # contact she has actually talked with (cached scan).
            _own_ids = [user_id]
            if self.unified_recall:
                try:
                    for _uid in self._distinct_user_ids():
                        if _uid not in _own_ids:
                            _own_ids.append(_uid)
                except Exception:
                    pass
                if "system-wake" not in _own_ids:
                    _own_ids.append("system-wake")

            def _search(q):
                # unified mode: run the query per own partition and merge,
                # tagging each hit with its source partition for attribution.
                if self.unified_recall and len(_own_ids) > 1:
                    _merged = {}
                    for _uid in _own_ids:
                        try:
                            _r = self.memory.search(
                                query=q,
                                filters={"user_id": _uid,
                                         "instance_id": self.instance_id},
                                threshold=0.30,
                            )
                        except Exception:
                            continue
                        for i in (_r.get("results", []) if isinstance(_r, dict) else (_r or [])):
                            k = str(i.get("id"))
                            i["_source_uid"] = _uid
                            if k not in _merged:
                                _merged[k] = i
                            elif float(i.get("score") or 0) > float(_merged[k].get("score") or 0):
                                _merged[k] = i
                    return {"results": list(_merged.values())}
                return self.memory.search(
                    query=q,
                    filters={
                        "user_id": user_id,
                        "instance_id": self.instance_id,
                    },
                    # Threshold 0.30 catches both multi-word matches
                    # (typically 0.40+) and single-word queries against
                    # memories (typically 0.33-0.40 because nomic-embed-text
                    # needs context to disambiguate). Sweep-confirmed
                    # optimal 2026-08-25 (0.15 floods noise).
                    threshold=0.30,
                )

            memories = _search(last_user_message)
            _items = memories.get("results", []) if isinstance(memories, dict) else (memories or [])
            _best = max((i.get("score") or 0 for i in _items), default=0.0)
            if (len(_items) < 3 or _best < 0.45) and recall_query != last_user_message:
                log.info("[Core] focused recall weak (%d results, best %.2f); widening to blended", len(_items), _best)
                _res2 = _search(recall_query)
                _items2 = _res2.get("results", []) if isinstance(_res2, dict) else (_res2 or [])
                _merged = {str(i.get("id")): i for i in _items}
                for i in _items2:
                    _merged.setdefault(str(i.get("id")), i)
                memories = {"results": list(_merged.values())}
            # Post-v3.6 M1: one widening decision for BOTH fan-out and
            # entity augmentation — intent triggers (temporal/aggregate)
            # OR weak first-pass pool. Fail-open; single source of truth
            # lives in query_expansion.should_recall_widen.
            import query_expansion as _qe
            _widen = _qe.should_recall_widen(
                recall_query, n_results=len(_items), best_score=_best,
            )
            # Post-v3 #2: query decomposition for temporal/historical
            # questions. Their question-phrasing vocabulary misses the
            # fact-shaped store (eval-proven); rewritten sub-queries
            # close the gap. Fail-open; merged into one result pool.
            try:
                if _widen:
                    _t0x = time()
                    _merged_qe = {}
                    for i in (memories.get("results", []) if isinstance(memories, dict) else (memories or [])):
                        _merged_qe[str(i.get("id"))] = i
                    for _sq in _qe.expand_query(recall_query)[1:]:
                        _r = _search(_sq)
                        for i in (_r.get("results", []) if isinstance(_r, dict) else (_r or [])):
                            k = str(i.get("id"))
                            if k not in _merged_qe:
                                _merged_qe[k] = i
                            elif float(i.get("score") or 0) > float(_merged_qe[k].get("score") or 0):
                                _merged_qe[k]["score"] = i.get("score")
                    memories = {"results": list(_merged_qe.values())}
                    log.info("[QExpand] fan-out added %d unique results in %.2fs",
                                len(_merged_qe), time() - _t0x)
            except Exception as qe_err:
                log.warning("Non-fatal query expansion bypass: %s", qe_err)

            # Post-v3 #2b: ENTITY-augmented retrieval. Aggregation
            # questions ('which models have we discussed?') fail vector
            # search because answer facts share no vocabulary with the
            # question — but the entities collection maps concept names
            # to linked memory ids. Pull those surgically by ID.
            try:
                from qdrant_client import models as _qmodels
                # M2 (memfixes82826.md): _collection used to be defined
                # only further down in the temporal-partition block, so
                # this feature raised UnboundLocalError on EVERY call and
                # entity augmentation never fired in production. Define
                # it here; the later assignment is an identical rebind.
                _collection = self.mem0_config["vector_store"]["config"]["collection_name"]
                if _widen:
                    _qv = self.memory.embedding_model.embed(recall_query)
                    _ecoll = _collection + "_entities"
                    _ehits = self.memory.vector_store.client.query_points(
                        _ecoll, query=_qv, limit=8,
                        query_filter=_qmodels.Filter(
                            must=[_qmodels.FieldCondition(
                                key="user_id",
                                match=_qmodels.MatchValue(value=user_id))]),
                        with_payload=True,
                    ).points
                    _linked = []
                    for _eh in _ehits:
                        for _lid in (_eh.payload or {}).get("linked_memory_ids") or []:
                            _linked.append(str(_lid))
                    _linked = list(dict.fromkeys(_linked))[:24]
                    if _linked:
                        _got = self.memory.vector_store.client.retrieve(
                            _collection, ids=_linked, with_payload=True)
                        _pool = memories.get("results", []) if isinstance(memories, dict) else (memories or [])
                        _have = {str(i.get("id")) for i in _pool}
                        _added = 0
                        for p_ in _got:
                            pl_ = p_.payload or {}
                            _ownset = set(_own_ids) if self.unified_recall else {str(user_id)}
                            if str(pl_.get("user_id")) not in _ownset or pl_.get("superseded_by"):
                                continue
                            txt = pl_.get("data") or ""
                            if not txt or str(p_.id) in _have:
                                continue
                            _pool.append({"id": str(p_.id), "memory": txt,
                                          "score": 0.45,
                                          "created_at": pl_.get("created_at")})
                            _have.add(str(p_.id)); _added += 1
                        memories = {"results": _pool} if isinstance(memories, dict) else {"results": _pool}
                        log.info("[EntityAug] %d entities -> +%d linked memories", len(_ehits), _added)
            except Exception as ea_err:
                log.warning("Non-fatal entity augmentation bypass: %s", ea_err)

            _n_results = len(memories.get("results", [])) if isinstance(memories, dict) else (len(memories) if memories else 0)
            log.info(
                "[Core] mem0 search: %.2fs, %d results for user %s (instance %s)",
                time() - _t_search, _n_results, user_id, self.instance_id,
            )
            if memories:
                raw_items = []
                if isinstance(memories, dict) and "results" in memories:
                    raw_items = memories["results"]
                elif isinstance(memories, list):
                    raw_items = memories

                # P2: partition live vs superseded facts. Superseded ones
                # are injected ONLY for explicitly historical queries —
                # otherwise the newest state wins silently.
                _all_ids = [str(i.get("id")) for i in raw_items if i.get("id")]
                import temporal_memory
                _collection = self.mem0_config["vector_store"]["config"]["collection_name"]
                _flags = temporal_memory.fetch_flags(
                    self.memory, _all_ids, _collection,
                ) if _all_ids else {}
                # M4: attach takeaway/user_fact kind tags saved at
                # extraction time (mem0 search results don't expose
                # custom payload keys, so pull them by id — same shape
                # as the entity-aug retrieve). Unclassified items keep
                # full weight: the penalty only bites tagged takeaways.
                try:
                    if _all_ids:
                        _kind_pts = self.memory.vector_store.client.retrieve(
                            _collection, ids=_all_ids[:128], with_payload=True)
                        _kinds = {str(p.id): (p.payload or {}).get("kind") for p in _kind_pts}
                        for i in raw_items:
                            i["kind"] = _kinds.get(str(i.get("id"))) or "user_fact"
                except Exception as kind_err:
                    logger.warning("[Core] kind fetch skipped (non-fatal): %s", kind_err)
                _hist = temporal_memory.is_historical_query(recall_query)
                # Provenance policy (memprovenance0929.md, house rulings
                # 2026-08-29): self-sourced facts are excluded at injection
                # per instance policy. strict (residenta): self_statement AND
                # assistant_takeaway dropped non-historical (historical
                # exemption preserved). exclude_self (fleet default):
                # self_statement only. inject_all (spike): no change.
                _kept_items, _prov_dropped = _apply_self_sourced_policy(
                    raw_items,
                    getattr(self, "self_sourced_policy", "exclude_self"),
                    _hist, self.TAKEAWAY_HISTORICAL_EXEMPT,
                )
                if _prov_dropped:
                    log.info(
                        "[Provenance] recall: %d self-sourced dropped (%s policy)",
                        len(_prov_dropped), self.self_sourced_policy,
                    )
                raw_items = _kept_items
                for i in raw_items:
                    i["superseded"] = str(i.get("id")) in _flags
                live_items = [i for i in raw_items if not i["superseded"]]
                sup_items = [i for i in raw_items if i["superseded"]]
                if sup_items:
                    log.info(
                        "[Temporal] recall: %d live, %d superseded (historical_query=%s)",
                        len(live_items), len(sup_items), _hist,
                    )

                # P4: recency+supersession rerank and char budget
                _recency = (
                    temporal_memory.wants_recency(recall_query)
                    or temporal_memory.wants_recency(last_user_message)
                )
                if _recency:
                    log.info("[Temporal] recent-intent query: recency boost ON")
                if explain:
                    _ctx_items, _dropped = self._rerank_memories(
                        raw_items, _hist,
                        queries=[last_user_message, recall_query],
                        recency_boost=_recency,
                        explain=True,
                    )
                    if _prov_dropped:
                        _dropped = _prov_dropped + list(_dropped or [])
                else:
                    _ctx_items = self._rerank_memories(
                        raw_items, _hist,
                        queries=[last_user_message, recall_query],
                        recency_boost=_recency,
                    )
                # [CONTINUA] wake-turn anti-loop (2026-09-07): her own
# agent_notes re-injected every wake read as standing obligations
# (the echo chamber, contributor #4). Wakes exclude them —
# the books + state packet carry that load.
                if exclude_agent_notes:
                    _before = len(_ctx_items)
                    _ctx_items = [i for i in _ctx_items
                                  if i.get("kind") != "agent_note"]
                    log.info("[Wake] agent_note exclusion: %d → %d items",
                             _before, len(_ctx_items))
                # Autopsy finding (2026-08-25): 'when did we...' questions
                # fail because creation dates live only in payload
                # metadata, not in the fact text. Stamp dates into the
                # injected lines whenever the query has temporal intent.
                import re as _re
                if (_recency or _hist or _re.search(r"\bwhen\b", recall_query, _re.I) or _re.search(r"\bwhen\b", last_user_message or "", _re.I)):
                    _stamped = []
                    for item in _ctx_items:
                        _age = self._age_days(item)
                        _ts = self._utc_to_local_date(item.get("created_at"))
                        _line = item.get("memory", "")
                        if _ts and "[20" not in _line[:12]:
                            _line = f"[{_ts}] {_line}"
                        _stamped.append(_line)
                        item["memory"] = _line
                    memory_context = "\n".join(_stamped) if _stamped else ""
                else:
                    memory_context = "\n".join(
                        [item["memory"] for item in _ctx_items if "memory" in item]
                    ) if _ctx_items else ""
                # [CONTINUA] One-Self provenance contract, COMPLETED 09-09 at
                # persona-a's direct request ("I want to see them at injection
                # time"): source attribution renders on EVERY injected line —
                # partner facts too, not just unified-search results. persona-a
                # correctly flagged that the Layered demotion left Layer-1
                # partner facts untagged. Source of a partner-partition item
                # is the conversation it came from: the current partner.
                if _ctx_items:
                    try:
                        import people as _pp
                        _roster_names = {pid: p.display_name
                                         for pid, p in _pp.load_roster().items()}
                    except Exception:
                        _roster_names = {}
                    _tagged = []
                    for item in _ctx_items:
                        _tag = _source_tag(item.get("_source_uid", user_id),
                                           _roster_names, user_id)
                        _mem = item.get("memory", "")
                        if _tag and not _mem.startswith("[from "):
                            item["memory"] = f"{_tag} {_mem}"
                        _tagged.append(item["memory"])
                    memory_context = "\n".join(_tagged) if _tagged else memory_context
                # M7b: per-turn digest of the injected block — makes
                # "right amount / right content" auditable forever
                # (the 08-28 audit had to reconstruct this by forensics).
                log.info(
                    "[MemoryCtx] injected=%d items / %d chars (takeaways=%d, "
                    "floor=%.2f) top=[%s]",
                    len(_ctx_items),
                    len(memory_context),
                    sum(1 for _i in _ctx_items if _i.get("kind") == "assistant_takeaway"),
                    self.MEMORY_MIN_INJECT_SCORE,
                    "; ".join(
                        "(%.2f) %s" % (
                            float(_i.get("score") or 0.0),
                            (_i.get("memory") or "")[:60],
                        )
                        for _i in _ctx_items[:3]
                    ),
                )
        except Exception as mem_search_err:
            log.warning("Non-fatal long-term memory lookup bypass for User %s: %s", user_id, mem_search_err)
        return {
            "query": recall_query,
            "context": memory_context,
            "injected": _ctx_items,
            "dropped": _dropped,
            "hist": _hist,
            "recency": _recency,
            "widened": _widen,
        }


    def _rerank_memories(self, items: list, historical: bool,
                         queries: Optional[List[str]] = None,
                         recency_boost: bool = False,
                         explain: bool = False):
        """P4 (sagentv3.md Upgrade 4): final ranking over mem0's combined-scored
        results.

        Eval finding (2026-08-25): recency decay was tested and REMOVED — it
        penalized exactly the old facts that historical/temporal queries need
        (temporal subcategory 3/6 -> 2/6). Final config: no decay; superseded
        facts only in historical mode at a penalty; char budget keeps highest-
        ranked first.

        memfixes M4/M6 (memfixes82826.md):
        - M4: ``kind == 'assistant_takeaway'`` items (the agent's own advice
          stored as facts) are score-penalized AND capped to a share of the
          budget, so they can inform but never dominate the injected block.
        - M6: score floor — items below MEMORY_MIN_INJECT_SCORE are dropped
          even when budget remains (no relevant context beats noisy context).
        - M6: MMR selection for pools > 8 items — greedy pick with a cosine
          redundancy penalty, which collapses the paraphrase clusters the
          audit measured (one decision re-saved ~50x).

        ``items`` must already carry 'superseded' boolean markers and mem0's
        combined score under 'score'. Returns ranked, budgeted items.

        With ``explain=True`` returns ``(items, dropped)`` where ``dropped``
        is a list of ``(item, reason)`` for every input item that was NOT
        returned: "superseded", "below floor X.XX", "takeaway cap", or
        "char budget". Selection behavior is identical either way — the flag
        only adds the audit trail (used by /searchmem's not-injected view).
        """
        scored = []
        dropped = [] if explain else None
        queries = queries or []
        for it in items:
            base = float(it.get("score") or 0.0)
            lex = self._lexical_boost(queries, it.get("memory") or "")
            w = self.SUPERSEDED_PENALTY if it.get("superseded") else 1.0
            if it.get("superseded") and not historical:
                if dropped is not None:
                    dropped.append((it, "superseded"))
                continue
            if it.get("kind") == "assistant_takeaway" and not (
                    historical and self.TAKEAWAY_HISTORICAL_EXEMPT):
                w *= self.TAKEAWAY_PENALTY
            adj = 0.0
            if recency_boost:
                age = self._age_days(it)
                if age is not None:
                    frac = max(0.0, 1.0 - age / self.RECENCY_HORIZON_DAYS)
                    adj += self.RECENCY_BOOST_MAX * frac
            scored.append((base * w + lex + adj, it))

        scored.sort(key=lambda t: -t[0])

        # Decision B: short-query strict floor (eval-gated; default OFF).
        # Triggered by the FOCUSED user message (queries[0]) — the pathology
        # is what a bare one/two-word message pulls, not the blended query.
        _strict_floor = self.MEMORY_MIN_INJECT_SCORE
        _literal_word = None
        if self.SHORT_QUERY_STRICT and queries:
            _focused = (queries[0] or "").strip()
            _tokens = [t for t in re.findall(r"[A-Za-z0-9']+", _focused)]
            if _tokens and len(_tokens) <= self.SHORT_QUERY_MAX_TOKENS:
                _strict_floor = max(_strict_floor, self.SHORT_QUERY_FLOOR)
                if len(_tokens) == 1:
                    _literal_word = _tokens[0].lower()

        def _passes_floor(it):
            score = float(it.get("score") or 0.0)
            if score >= _strict_floor:
                return True
            if (_literal_word and score >= self.SHORT_QUERY_LITERAL_FLOOR):
                text = (it.get("memory") or "").lower()
                _w = _literal_word
                if _w in text or (len(_w) > 3 and _w.rstrip("s") in text):
                    return True
            return False

        def _floor_reason(it):
            r = f"below floor {_strict_floor:.2f}"
            if _literal_word:
                r += f" (short query — no literal '{_literal_word}' at " \
                     f">= {self.SHORT_QUERY_LITERAL_FLOOR:.2f})"
            return r

        # M6 floor: drop weak items outright (base mem0 score, not the adjusted
        # one — the takeaway penalty must not double as a floor). If EVERYTHING
        # is weak, inject nothing: empty context beats noisy context.
        if dropped is not None:
            _kept = []
            for s in scored:
                if _passes_floor(s[1]):
                    _kept.append(s)
                else:
                    dropped.append((s[1], _floor_reason(s[1])))
            scored = _kept
        else:
            scored = [s for s in scored if _passes_floor(s[1])]

        # M6 MMR: redundancy-aware greedy selection (only for big pools; the
        # embed batch cost is not worth it for a handful of items).
        if self.MMR_ENABLED and len(scored) > 8:
            try:
                texts = [it.get("memory") or "" for _, it in scored]
                try:
                    vecs = self.memory.embedding_model.embed_batch(texts, "search")
                except Exception:
                    vecs = [self.memory.embedding_model.embed(t) for t in texts]
                import math
                def _norm(v):
                    n = math.sqrt(sum(x * x for x in v)) or 1.0
                    return [x / n for x in v]
                vecs = [_norm(v) for v in vecs]
                scored = self._mmr_select(scored, vecs, self.MMR_LAMBDA)
            except Exception as mmr_err:
                logger.warning("[Core] MMR selection skipped (non-fatal): %s", mmr_err)

        out, used = [], 0
        takeaway_cap = self.max_memory_chars * self.TAKEAWAY_CHAR_SHARE
        takeaway_used = 0
        for _score, it in scored:
            L = len(it.get("memory") or "")
            if it.get("kind") == "assistant_takeaway" and not (
                    historical and self.TAKEAWAY_HISTORICAL_EXEMPT) \
                    and takeaway_used + L > takeaway_cap:
                if dropped is not None:
                    dropped.append((it, "takeaway cap"))
                continue
            if used + L > self.max_memory_chars and out:
                if dropped is not None:
                    dropped.append((it, "char budget"))
                break
            out.append(it)
            used += L
            if it.get("kind") == "assistant_takeaway":
                takeaway_used += L
        if dropped is not None:
            # everything after the budget break (or any other survivor that
            # never made it into `out`) is a budget casualty too
            _out_ids = {id(i) for i in out}
            _seen = {id(i) for i, _r in dropped}
            for _score, it in scored:
                if id(it) not in _out_ids and id(it) not in _seen:
                    dropped.append((it, "char budget"))
            return out, dropped
        return out

    @staticmethod
    def _mmr_select(scored: list, vecs: list, lam: float) -> list:
        """M6: maximal-marginal-relevance greedy re-selection.
        ``scored`` is [(score, item)] sorted desc; ``vecs`` the aligned unit
        vectors. Returns the same pairs, re-sorted by MMR-adjusted score.
        Deterministic (no randomness) so evals can reproduce selection."""
        selected: list = []
        selected_idx: list = []
        remaining = list(range(len(scored)))
        while remaining:
            best_idx, best_val = None, None
            for idx in remaining:
                redundancy = 0.0
                if selected_idx:
                    v = vecs[idx]
                    redundancy = max(
                        sum(a * b for a, b in zip(v, vecs[j])) for j in selected_idx
                    )
                val = scored[idx][0] - lam * redundancy
                if best_val is None or val > best_val:
                    best_idx, best_val = idx, val
            selected.append((best_val, scored[best_idx][1]))
            selected_idx.append(best_idx)
            remaining.remove(best_idx)
        selected.sort(key=lambda t: -t[0])
        return selected

    def search_memories(self, user_id: str, query=None, top_k: int = 50, threshold: float = 0.30) -> list:
        """Return memories for this user in this agent.

        Pass query=None or query='all' to return all (capped at top_k).
        Otherwise performs a semantic search over the user's memories.

        Returns a list of dicts with at least 'id' and 'memory' keys. Each
        dict may also include 'score' (for search) and 'created_at'.
        Returns [] if mem0 isn't initialized or the call fails.

        Triggers lazy mem0 init if it hasn't happened yet (so /searchmem
        works even before the first chat message to this agent).

        `threshold` is the minimum cosine similarity to include a result
        (mem0's default is 0.1, which is too loose — for nomic-embed-text,
        genuine matches start around 0.4, but single-word queries typically
        land 0.33-0.40 due to lack of disambiguating context, so 0.30 is
        the practical default for the user-facing commands). Lower it if
        you want broader recall, raise it if you're seeing noise.
        """
        if self.memory is None:
            self._init_memory()
        if self.memory is None:
            return []
        filters = {"user_id": user_id}
        is_all = query is None or str(query).strip() == "" or str(query).strip().lower() == "all"
        try:
            if is_all:
                result = self.memory.get_all(filters=filters, top_k=top_k)
            else:
                result = self.memory.search(query=query, filters=filters, top_k=top_k, threshold=threshold)
        except Exception as e:
            logger.warning("mem0 search/get_all failed for user %s: %s", user_id, e)
            return []
        items = result.get("results", []) if isinstance(result, dict) else (result or [])
        items = list(items)
        # P2: annotate each row with supersession status so /searchmem can
        # display it. Non-fatal on failure — rows just lack the flag.
        try:
            import temporal_memory
            if temporal_memory.TEMPORAL_ENABLED and items:
                _ids = [str(i["id"]) for i in items if i.get("id")]
                _collection = self.mem0_config["vector_store"]["config"]["collection_name"]
                _flags = temporal_memory.fetch_flags(self.memory, _ids, _collection)
                for i in items:
                    f = _flags.get(str(i.get("id")))
                    if f:
                        i["superseded_by"] = f["superseded_by"]
                        i["superseded_at"] = f.get("superseded_at", "")
        except Exception as e:
            logger.warning("[Temporal] search annotation failed (non-fatal): %s", e)
        return items

    def search_memories_semantic(self, user_id: str, query: str, top_k: int = 50,
                                 threshold: float = 0.45) -> list:
        """P4b: RAW-cosine semantic search for human-facing inspection.

        Unlike search_memories() (which returns mem0's combined normalized
        score whose scale shifts with active BM25/entity signals), this
        returns pure embedding similarity — a stable scale so thresholds mean
        the same thing every time. Used by /searchmem.
        """
        if self.memory is None:
            self._init_memory()
        if self.memory is None or not query.strip():
            return []
        try:
            qvec = self.memory.embedding_model.embed(query, "search")
            pts = self.memory.vector_store.search(
                query=query,
                vectors=qvec,
                top_k=top_k,
                filters={"user_id": user_id},
            )
        except Exception as e:
            logger.warning("semantic search failed for user %s: %s", user_id, e)
            return []
        items = []
        for p in pts:
            pl = p.payload or {}
            data = pl.get("data") or ""
            if not data:
                continue
            items.append({
                "id": str(p.id), "memory": data,
                "score": float(p.score) if getattr(p, "score", None) is not None else 0.0,
                "created_at": pl.get("created_at", ""),
            })
        # supersession annotation (P2)
        try:
            import temporal_memory
            _ids = [i["id"] for i in items]
            _collection = self.mem0_config["vector_store"]["config"]["collection_name"]
            _flags = temporal_memory.fetch_flags(self.memory, _ids, _collection)
            for i in items:
                f = _flags.get(i["id"])
                if f:
                    i["superseded_by"] = f["superseded_by"]
                    i["superseded_at"] = f.get("superseded_at", "")
        except Exception as e:
            logger.warning("[Temporal] annotation failed (non-fatal): %s", e)
        return [i for i in items if i["score"] >= threshold]

    # [CONTINUA] unified search (2026-09-07, house ruling): injection stays
    # person-scoped; SEARCH is knowledge and spans every partition. The
    # per-user pipelines are preserved — partitions are queried side by side
    # and the results merged, ranked, and attributed.
    def _distinct_user_ids(self, ttl_s: int = 300) -> list:
        """Distinct user_ids present in her collection (cached 5 min)."""
        if getattr(self, "memory", None) is None:
            self._init_memory()
        if getattr(self, "memory", None) is None:
            return []
        cache = getattr(self, "_user_ids_cache", None)
        now = time()
        if cache and now - cache[0] < ttl_s:
            return cache[1]
        ids = set()
        try:
            coll = self.mem0_config["vector_store"]["config"]["collection_name"]
            offset = None
            while True:
                points, offset = self.memory.vector_store.client.scroll(
                    collection_name=coll, limit=256, offset=offset,
                    with_payload=True, with_vectors=False)
                for p in points or []:
                    uid = (p.payload or {}).get("user_id")
                    if uid and uid != "system-wake":
                        ids.add(str(uid))
                if offset is None:
                    break
        except Exception as e:
            logger.warning("[Core] user-id scan failed (fail-open): %s", e)
        out = sorted(ids)
        self._user_ids_cache = (now, out)
        return out

    def search_memories_unified(self, query: str, top_k: int = 8) -> list:
        """Search every partition she owns, merged + ranked. Full pipeline
        runs per partition (curated or raw, matching this instance's tool
        config)."""
        hits_by_user = {}
        for uid in self._distinct_user_ids():
            try:
                if self.TOOL_RECALL_CURIATED:
                    _blk = self.recall_block(uid, query,
                                             recent_user_texts=[query])
                    hits = _blk["injected"]
                else:
                    hits = self.search_memories(uid, query=query, top_k=top_k)
                if hits:
                    hits_by_user[uid] = hits
            except Exception as e:
                logger.warning("[Core] unified sub-search %s failed: %s", uid, e)
        return _merge_unified_hits(hits_by_user, top_k)

    def delete_memories(self, user_id: str, memory_ids) -> int:
        """Delete specific memories by id. Returns the count successfully deleted.

        Serialized via _memory_save_lock so it can't race with the background
        _save_turn_memory thread or with concurrent /deletemem calls.
        Triggers lazy mem0 init if it hasn't happened yet.
        """
        if self.memory is None:
            self._init_memory()
        if self.memory is None or not memory_ids:
            return 0
        count = 0
        with self._memory_save_lock:
            for mem_id in memory_ids:
                try:
                    self.memory.delete(memory_id=mem_id)
                    count += 1
                except Exception as e:
                    logger.warning("mem0 delete failed for id %s: %s", mem_id, e)
        if count:
            logger.info(
                "[Core] mem0 delete: %d/%d ids for user %s (instance %s)",
                count, len(memory_ids), user_id, self.instance_id,
            )
        return count

    def delete_all_memories(self, user_id: str) -> int:
        """Delete every memory for this user in this agent. Returns count deleted.

        Uses mem0's bulk delete_all(user_id) which is one call rather than
        iterating single deletes. Count is computed by get_all before the
        delete so the caller can show the user how many were removed.
        Triggers lazy mem0 init if it hasn't happened yet.
        """
        if self.memory is None:
            self._init_memory()
        if self.memory is None:
            return 0
        with self._memory_save_lock:
            try:
                existing = self.memory.get_all(
                    filters={"user_id": user_id}, top_k=10000,
                )
            except Exception as e:
                logger.warning("mem0 get_all (pre-delete) failed for user %s: %s", user_id, e)
                return 0
            count = len(existing.get("results", [])) if isinstance(existing, dict) else 0
            if count == 0:
                return 0
            try:
                self.memory.delete_all(user_id=user_id)
            except Exception as e:
                logger.warning("mem0 delete_all failed for user %s: %s", user_id, e)
                return 0
        logger.info(
            "[Core] mem0 delete_all: %d memories for user %s (instance %s)",
            count, user_id, self.instance_id,
        )
        return count

    # ------------------------------------------------------------------
    # Built-in save_my_memory tool (agentic self-memory write, 2026-09-04)
    # ------------------------------------------------------------------

    def _savemem_rate_limit(self, user_id: str, window_s: int, window_max: int) -> bool:
        """Rolling-window rate limiter for save_my_memory, per (agent, user).

        Returns True if the save is allowed (and records it); False when
        window_max saves already happened in the last window_s seconds. A
        rejected attempt is NOT recorded, so it isn't punished twice — but
        allowed-and-failed writes are, which doubles as a circuit breaker
        against tight error loops.
        """
        now = time()
        with self._savemem_lock:
            stamps = [
                t for t in self._savemem_saves.get(user_id, [])
                if now - t < window_s
            ]
            if len(stamps) >= window_max:
                self._savemem_saves[user_id] = stamps
                return False
            stamps.append(now)
            self._savemem_saves[user_id] = stamps
            return True

    def _qwen_same_meaning(self, existing_text: str, existing_ts: str,
                           new_text: str) -> tuple[bool | None, str]:
        """One cheap same-meaning judgment on the house Qwen (same server as
        the summary rolls — machinery, not her persona model, so judging her
        never puts a persona model in the judge seat). Returns (True = same
        statement / False = new content / None = judge unavailable, reason).
        Key register rule: a status update whose only new information is the
        date/time IS the same statement; a note adding a new fact, event,
        decision, or realization is DIFFERENT."""
        url = os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1")
        model = os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp")
        system = (
            "You are a strict memory dedup judge for an AI companion's memory "
            "store. Decide whether the NEW note is materially the same "
            "statement as the EXISTING memory. A re-statement or re-wording "
            "is the SAME; a status update whose only new information is the "
            "date/time is the SAME. A note adding a new fact, event, "
            "decision, or realization is DIFFERENT. Answer ONLY compact JSON: "
            "{\"same\": true|false, \"why\": \"...\"}")
        user = (f"EXISTING (saved {existing_ts or '?'}): {existing_text}\n\n"
                f"NEW NOTE: {new_text}")
        try:
            import requests as _requests
            resp = _requests.post(
                f"{url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "max_tokens": 120,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
            import re as _re
            m = _re.search(r'"same"\s*:\s*(true|false)', text, _re.I)
            if m:
                return (m.group(1).lower() == "true",
                        text[:200])
            return None, f"unparseable judge output: {text[:120]}"
        except Exception as e:
            return None, str(e)[:200]

    def _savemem_echo_gate(self, user_id: str, content: str) -> str | None:
        """Tier-2 same-meaning gate for save_my_memory (2026-09-12).

        Tier-1 above (cosine >= SAGENT_SAVEMEM_DEDUP_THRESHOLD, 0.85) never
        fired on the wake-echo family: re-worded echo notes measured
        0.44-0.81 raw cosine (nomic-embed-text) against their own canonicals
        — under the bar — while legit kept-vs-kept pairs reach 0.80, so no
        pure threshold separates a chorus from real content. This gate
        routes borderline saves (cosine >= CONTINUA_SAVEDUP_THRESHOLD,
        default 0.60, vs a memory younger than CONTINUA_SAVEDUP_WINDOW_H
        hours, default 48) through one cheap same-meaning judgment and
        denies only confirmed re-statements, with a teaching reason (the
        denial-carries-reason contract: the chronicle already carries the
        event; re-save with what is NEW). Fail-open at every step —
        embedder, search, or judge errors all ALLOW the save (worst case
        is behavior before this gate existed). Kill switch
        CONTINUA_SAVEDUP=0. Returns a denial message, or None to proceed.
        """
        if os.getenv("CONTINUA_SAVEDUP", "1") != "1":
            return None
        try:
            threshold = float(os.getenv("CONTINUA_SAVEDUP_THRESHOLD", "0.60"))
            window_h = float(os.getenv("CONTINUA_SAVEDUP_WINDOW_H", "48"))
            cands = self.search_memories_semantic(
                user_id, query=content, top_k=5, threshold=threshold)
            if not cands:
                return None
            now = datetime.now(timezone.utc)
            recent = []
            for c_item in cands:
                ts_raw = c_item.get("created_at") or ""
                try:
                    cdt = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00"))
                    if cdt.tzinfo is None:
                        cdt = cdt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if (now - cdt) <= timedelta(hours=window_h):
                    recent.append((cdt, c_item))
            if not recent:
                return None
            recent.sort(key=lambda t: t[0], reverse=True)
            _when, best = recent[0]
            same, _why = self._qwen_same_meaning(
                best.get("memory", ""), best.get("created_at", ""), content)
            if same is True:
                logger.info(
                    "[Core] save echo-gate: denied near-duplicate for user "
                    "%s (cos %.2f vs %s)", user_id, best.get("score", 0.0),
                    (best.get("memory") or "")[:80])
                return (
                    f"Already in memory (id {best.get('id')}, saved "
                    f"{_when.isoformat()[:16]}): \"{best.get('memory', '')}\" "
                    "— this says the same thing, so nothing new was saved. "
                    "The event itself is already in the chronicle. To save, "
                    "include what is genuinely NEW since that note.")
            if same is None:
                logger.warning(
                    "[Core] save echo-gate judge unavailable (fail-open): %s",
                    _why)
            else:
                logger.info(
                    "[Core] save echo-gate: passed (new content) for user %s",
                    user_id)
            return None
        except Exception as exc:
            logger.warning("[Core] save echo-gate failed (fail-open): %s", exc)
            return None

    # --- chunk 7 (memory plan §6g): her notebook + anchors ----------------
    def _notes_store(self):
        from pathlib import Path as _P
        import notes as _notes
        return _notes.NotesStore(str(_P(__file__).resolve().parent / "notes"),
                                 self.instance_id)

    def _recollection_store(self):
        import recollections as _rec
        return _rec.Store(self.instance_id)

    def _tool_write_note(self, arguments: dict) -> str:
        try:
            out = self._notes_store().write_note(
                arguments.get("title", ""), arguments.get("body", ""))
            return (f"Note saved: '{out['title']}' (updated {out['updated']}, "
                    f"{out['versions_kept']} versions kept).")
        except ValueError as exc:
            return f"Could not save the note: {exc}"

    def _tool_read_note(self, arguments: dict) -> str:
        note = self._notes_store().read_note(arguments.get("title", ""))
        if not note:
            return f"No note titled '{arguments.get('title', '')}'."
        if note["removed"]:
            return f"Note '{arguments.get('title', '')}' is removed (write it again to restore)."
        return (f"[{note['title']} — updated {note['updated']}]" + chr(10)
                + chr(10) + note['body'])

    def _tool_remove_note(self, arguments: dict) -> str:
        removed = self._notes_store().remove_note(arguments.get("title", ""))
        if removed:
            return f"Note removed. Its history stays in your notebook; write the title again to restore it."
        return f"No active note titled '{arguments.get('title', '')}'."

    def _tool_list_notes(self) -> str:
        notes = self._notes_store().list_notes()
        if not notes:
            return "Your notebook has no active notes."
        lines = [f"- {n['title']} (updated {n['updated']})" for n in notes]
        return "Your notes:" + chr(10) + chr(10).join(lines)

    def _tool_set_project(self, arguments: dict) -> str:
        try:
            out = self._notes_store().set_project(
                arguments.get("title", ""), arguments.get("status", ""),
                arguments.get("note"))
            return f"Project '{out['title']}' set to '{out['status']}' (updated {out['updated']})."
        except ValueError as exc:
            return f"Could not set the project: {exc}"

    def _tool_list_projects(self) -> str:
        projects = self._notes_store().list_projects()
        if not projects:
            return "You have no declared projects."
        lines = [f"- {p['title']}: {p['status']}" + (f" — {p['note']}" if p.get('note') else "")
                 for p in projects]
        return "Your projects:" + chr(10) + chr(10).join(lines)

    def _tool_anchor_memory(self, arguments: dict) -> str:
        job = (arguments.get("job_id") or "").strip()
        why = str(arguments.get("why") or "").strip()
        try:
            self._recollection_store().anchor(
                job, by=self.instance_id,
                provenance=("resident-tool" + (": why — " + why[:200] if why else "")))
            _msg = f"Anchored: {job[:12]}… It will not be dropped by space pressure."
            if why:
                _msg += " Your why is recorded with it."
            return _msg
        except ValueError as exc:
            return f"Could not anchor: {exc}"

    def _tool_unanchor_memory(self, arguments: dict) -> str:
        job = (arguments.get("job_id") or "").strip()
        try:
            self._recollection_store().unanchor(job, by=self.instance_id,
                                                provenance="resident-tool")
            return f"Anchor removed from {job[:12]}…"
        except Exception as exc:
            return f"Could not unanchor: {exc}"

    def _resolve_episode(self, prefix: str):
        """Prefix-match an accepted episode id in the recollections store."""
        import recollections as _rec
        _revs = _rec.read_revisions(self.instance_id)
        _jobs = sorted({v['job'] for v in _revs})
        for j in _jobs:
            if j.startswith(prefix):
                return j
        return None

    def _tool_write_essence(self, arguments: dict) -> str:
        """§6b.1: she writes the line. Authorship: resident-authored."""
        import recollections as _rec
        prefix = str(arguments.get("episode") or "").strip()
        essence = str(arguments.get("essence") or "").strip()
        if not prefix or not essence:
            return "[Tool error: write_essence needs 'episode' and 'essence']"
        job = self._resolve_episode(prefix)
        if not job:
            return "[Tool error: no such episode in your recollections]"
        store = _rec.Store(self.instance_id)
        try:
            _rec.add_essence(store, job, essence, 'resident-authored',
                             by=self.instance_id, provenance='write_essence tool')
        except ValueError as exc:
            return f"[Tool error: {exc}]"
        logger.info('[Essence] %s wrote an essence for %s (%d chars)',
                    self.instance_id, job[:12], len(essence))
        return ("Your essence is kept, dated and linked to the episode. The "
                "full memory stays beneath it untouched; as the memory ages, "
                "your line is the one that rises.")

    def _tool_endorse_essence(self, arguments: dict) -> str:
        """§6b.1 (a)-path: she endorses a line she already said — verbatim."""
        import recollections as _rec
        prefix = str(arguments.get("episode") or "").strip()
        quote = str(arguments.get("quote") or "").strip()
        if not prefix or not quote:
            return "[Tool error: endorse_essence needs 'episode' and 'quote']"
        job = self._resolve_episode(prefix)
        if not job:
            return "[Tool error: no such episode in your recollections]"
        store = _rec.Store(self.instance_id)
        prior = store.latest(job)
        if prior is None:
            return "[Tool error: no such episode in your recollections]"
        # the quote must be her actual words in this episode's record
        hay = " ".join(str(s.get("content") or "") for s in (prior.get("sources") or []))
        if quote[:120].lower() not in hay.lower():
            return ("[Tool error: that quote is not in this episode's record — "
                    "an endorsed essence must be your exact words from this "
                    "conversation. Write it with write_essence instead if the "
                    "line is new.]")
        try:
            _rec.add_essence(store, job, quote, 'resident-endorsed',
                             by=self.instance_id, provenance='endorse_essence tool',
                             source_quote=quote)
        except ValueError as exc:
            return f"[Tool error: {exc}]"
        logger.info('[Essence] %s endorsed a quote for %s (%d chars)',
                    self.instance_id, job[:12], len(quote))
        return ("Your words, verbatim, are now the line this episode keeps — "
                "quoted and dated, the full record still open beneath it.")

    def _tool_my_trajectory(self) -> str:
        """§5a: her trajectory, assembled from existing evidence only."""
        import recollections as _rec
        import notes as _notes
        from pathlib import Path as _P
        try:
            _store = _notes.NotesStore(
                str(_P(__file__).resolve().parent / "notes"), self.instance_id)
        except Exception:
            _store = None
        return _rec.my_trajectory(self.instance_id, notes_store=_store)

    def _tool_list_essences(self) -> str:
        """§6b.1: her essences + at most one candidate question (ignorable)."""
        import recollections as _rec
        store = _rec.Store(self.instance_id)
        revs = _rec.read_revisions(self.instance_id)
        by_job = {}
        for v in revs:
            if (v.get('rendering') or 'full') == 'essence':
                by_job.setdefault(v['job'], []).append(v)
        lines = []
        for job, versions in sorted(by_job.items()):
            v = versions[-1]
            lines.append(f"- [{str(v.get('event_end') or '?')[:16]}] "
                         f"({v.get('authorship')}) {str(v.get('text'))[:200]} "
                         f"(episode {job[:12]})")
        out = ("[Your essences — your lines of meaning]\n" + "\n".join(lines)
               if lines else "[You have no essences yet.]")
        try:
            cands = _rec.essence_candidates(store)
        except Exception:
            cands = []
        if cands:
            c = cands[0]
            _q = str(c.get('quote') or '')[:180]
            out += ("\n\n[A candidate, only if you want it: you have said, "
                    + str(c.get('episodes')) + " times — '" + _q
                    + "' — most recently in episode " + str(c.get('job'))[:12] + ". "
                    "If this is the line you would keep, endorse_essence it; "
                    "if not, ignore this and nothing happens.]")
        return out

    def _tool_recall_my_experience(self, arguments: dict) -> str:
        """§5 explicit recall over the canonical recollections store."""
        import recollections as _rec
        try:
            _anchors, _ = _rec.read_guards(self.instance_id)
        except Exception:
            _anchors = set()
        topic = str(arguments.get("topic") or "").strip() or None
        person = str(arguments.get("person") or "").strip() or None
        tme = str(arguments.get("time") or "").strip() or None
        expand = str(arguments.get("expand") or "").strip() or None
        if expand:
            try:
                _revs = _rec.read_revisions(self.instance_id)
                _m = [v['job'] for v in _revs if v['job'].startswith(expand)]
                expand = _m[0] if _m else expand
            except Exception:
                pass
        res = _rec.recall_experience(self.instance_id, topic, person=person,
                                     time=tme, expand_job=expand,
                                     anchors=_anchors)
        logger.info('[RecallExperience] topic=%r person=%r time=%r expand=%r -> %d hits',
                    (topic or '')[:60], person, tme, bool(expand), len(res.get('hits') or []))
        return res['text']

    def _tool_consolidate_memories(self) -> str:
        """§6d.6 deliberate trigger: she consolidates when she chooses."""
        import recollections as _rec
        triggered = _rec.request_shadow(self.instance_id)
        if triggered:
            return ("Consolidation started: your recent closed conversations are "
                    "being folded into recollections in the background.")
        return ("Consolidation is already running or is gated off. Try again "
                "shortly — nothing is lost either way.")

    def _tool_save_my_memory(self, user_id: Optional[str], arguments: dict) -> str:
        """Built-in save_my_memory: the agent writes a durable note into its
        own mem0 store for the current user.

        Unlike the background extraction path (_save_turn_memory, infer=True —
        an LLM decides what is worth keeping and rewrites it), this stores the
        agent's text verbatim via mem0's infer=False fast path: one embedding
        call, no extraction LLM. That avoids the 15-30s extraction latency and
        sidesteps the extraction prompt's one-side sourcing rule, which would
        drop assistant-voice content by design.

        Guardrails: kill-switch, length cap, per-user rolling-window rate
        limit, near-duplicate skip, and _memory_save_lock serialization so the
        write cannot race the background extraction thread (same lock
        discipline as delete_memories). The note lands with payload
        kind="agent_note"; recall policies (exclude_self/strict) only filter
        self_statement/assistant_takeaway kinds, so deliberate agent notes are
        injected at full weight — they are first-class by construction.
        """
        if os.getenv("SAGENT_AGENT_WRITE_MEM", "1") != "1":
            return "[Tool error: memory saving is disabled on this instance]"
        if not user_id:
            return "[Tool error: no user context available for memory save]"
        content = str(arguments.get("content") or "").strip()
        if not content:
            # AVATAR-SAVE incident (2026-09-05 15:21): a 4B model invents
            # parameter names ('type'/'detail') and cannot self-correct from
            # a bare 'empty content' message — it repeated the identical
            # failing call 3x. Show what was received plus the exact expected
            # call shape so the retry within the same turn can succeed.
            _received = ", ".join(k for k in arguments if k != "content") or "none"
            return (
                "[Tool error: nothing saved — the note text must go in the "
                f"'content' parameter (received: {_received}). Retry exactly "
                "like this: "
                "<call><function>save_my_memory</function>"
                '<parameter name="content">the one-sentence note</parameter>'
                "</call>]"
            )
        max_chars = int(os.getenv("SAGENT_SAVEMEM_MAX_CHARS", "500"))
        if len(content) > max_chars:
            return (f"[Tool error: content too long ({len(content)} chars, "
                    f"max {max_chars}) — shorten it to one concise sentence]")

        # §7 retirement (2026-09-19 review decision): the destination is HER
        # NOTEBOOK (chunk 7's resident-owned store) — not mem0, whose
        # injection store is archive-track. The plan: do not leave her saves
        # silently writing to an abandoned store. Her words, verbatim,
        # revisable and removable by her — exactly what the notebook is.
        # The mem0 write path is retired (rollback = git history); mem0's
        # archive keeps her old saves for list_my_memories reads.
        from pathlib import Path as _Path
        import notes as _notes
        # [CONTINUA] 2026-09-22 (cleansweep repair #4): future-dated content
        # is FLAGGED, never blocked or rewritten (her words are hers). The
        # warning rides the log + the digest surface; the save proceeds.
        _fut = _future_date_flags(content)
        if _fut:
            logger.warning(
                "[Core] save_my_memory [%s]: FUTURE-DATED content %s — "
                "flagged, stored as written (cleansweep #4)",
                user_id, sorted(set(_fut)))
        _store = _notes.NotesStore(
            str(_Path(__file__).resolve().parent / "notes"), self.instance_id)
        _title = content.strip().splitlines()[0][:60] or "saved note"
        try:
            _res = _store.write_note(_title, content)
        except Exception as _exc:
            return f"[Tool error: notebook save failed: {_exc}]"
        logger.info("[Core] save_my_memory → notebook (%d chars)", len(content))
        return ("Saved to your notebook, verbatim and yours (title: "
                f"'{_title[:60]}'). It survives wakes and restarts; remove it "
                "with remove_note whenever you choose.")

    def _tool_list_my_memories(self, user_id: Optional[str], arguments: dict) -> str:
        """Built-in list_my_memories: read-only self-inspection view.

        source='notes' (default) lists only kind=agent_note rows — what the
        agent deliberately saved via save_my_memory; source='all' lists every
        memory row for the current user. Uses a direct Qdrant scroll (no
        embedding call, unlike search) because mem0's search/get_all whitelist
        payload keys and would hide the kind tag — same retrieve-by-id trick
        the recall path uses, just filter-side instead of fetch-side.

        Newest first, capped at SAGENT_LISTMEM_TOP (25) with a total count;
        superseded rows are annotated so stale notes are visible as stale.
        """
        if os.getenv("SAGENT_AGENT_LIST_MEM", "1") != "1":
            return "[Tool error: memory listing is disabled on this instance]"
        if not user_id:
            return "[Tool error: no user context available for memory listing]"
        source = str(arguments.get("source") or "notes").strip().lower()
        if source not in ("notes", "all"):
            source = "notes"
        if self.memory is None:
            self._init_memory()
        if self.memory is None:
            return "[Tool error: memory store unavailable]"
        try:
            _t0 = time()
            coll = self.mem0_config["vector_store"]["config"]["collection_name"]
            # 2026-09-04 FIX: mem0's wrapped client rejects raw dicts for the
            # scroll filter ('dict' object has no attribute 'must') — build
            # proper Filter models, same as the recall path's query_filter.
            from qdrant_client import models as _qmodels
            _must = [_qmodels.FieldCondition(
                key="user_id", match=_qmodels.MatchValue(value=user_id))]
            if source == "notes":
                _must.append(_qmodels.FieldCondition(
                    key="kind", match=_qmodels.MatchValue(value="agent_note")))
            points, _cursor = self.memory.vector_store.client.scroll(
                collection_name=coll,
                scroll_filter=_qmodels.Filter(must=_must),
                limit=int(os.getenv("SAGENT_LISTMEM_SCAN", "500")),
                with_payload=True,
                with_vectors=False,
            )
            rows = []
            for p in points or []:
                pl = p.payload or {}
                data = (pl.get("data") or "").strip()
                if not data:
                    continue
                rows.append({
                    "id": str(p.id),
                    "memory": data,
                    "created_at": SagentCore._utc_to_local_date(pl.get("created_at")),
                    "kind": pl.get("kind") or "user_fact",
                })
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            total = len(rows)
            if not rows:
                return ("No saved notes yet — nothing you have stored with "
                        "save_my_memory for this user."
                        if source == "notes" else
                        "No memories found for this user.")
            cap = int(os.getenv("SAGENT_LISTMEM_TOP", "25"))
            shown = rows[:cap]
            # Supersession annotation: a listed note may have been
            # soft-invalidated by the temporal pipeline — show it as stale.
            try:
                import temporal_memory
                if temporal_memory.TEMPORAL_ENABLED:
                    _flags = temporal_memory.fetch_flags(
                        self.memory, [r["id"] for r in shown], coll)
                    for r in shown:
                        if _flags.get(r["id"]):
                            r["superseded"] = True
            except Exception:
                pass
            lines = []
            for n, r in enumerate(shown, 1):
                date = f"[{r['created_at']}] " if r["created_at"] else ""
                tag = " [note]" if (source == "all" and r["kind"] == "agent_note") else ""
                sup = " [superseded]" if r.get("superseded") else ""
                lines.append(f"{n}. {date}{r['memory']}{tag}{sup}")
            more = (f"\n(+{total - cap} older not shown)" if total > cap else "")
            label = ("your saved notes (source=notes)" if source == "notes"
                     else "all memories (source=all)")
            logger.info(
                "[Core] list_my_memories: %d/%d rows shown for user %s "
                "(instance %s, %s) in %.2fs",
                len(shown), total, user_id, self.instance_id, source, time() - _t0,
            )
            return f"{total} {label}, newest first:\n" + "\n".join(lines) + more
        except Exception as exc:
            logger.exception("[Core] list_my_memories failed for user %s: %s", user_id, exc)
            return f"[Tool error: failed to list memories: {exc}]"

    # ------------------------------------------------------------------
    # Function-execution dispatching
    # ------------------------------------------------------------------

    def _execute_function_call(self, name: str, arguments: dict,
                               user_id: Optional[str] = None) -> str:
        """Run the tool identified by <name> with <arguments>, return string result.

        user_id is required for per-user-scoped built-ins (search_my_memories);
        external tools ignore it.
        """
        # [CONTINUA] 2026-09-13 (house ruling, Option A, phase 3): a memory
        # tool disabled in this agent's yaml returns a graceful message —
        # trained-in calls must never crash the turn (teaching-error
        # pattern). The env kill switches are honored deeper in the save
        # path as well; this guard covers the yaml layer.
        _mt = getattr(self, "_mem_tools", None) or {}
        if name in _MEM_TOOLS_DEFAULTS and not _mt.get(name, False):
            return (f"[{name} is not part of your current configuration — "
                    "use the tools in your tool list instead]")
        # [CONTINUA] autonomy tools (2026-09-07): sandbox, governed send,
        # deep recall, bookmarks. Fail-open — a tool error is a message to
        # her, never a crash (the teaching-error pattern).
        try:
            if name in ("sandbox_list", "sandbox_read", "sandbox_write",
                        "sandbox_exec"):
                import sandbox as _sbx
                if name == "sandbox_list":
                    _res = _sbx.run(self.instance_id,
                                    ["python3", "-c",
                                     "import os; print('\\n'.join(sorted(os.listdir('.'))))"],
                                    timeout=30)
                elif name == "sandbox_read":
                    # [CONTINUA] 2026-09-12: page reads for long files — the
                    # ledgers outgrew the old fixed [:6000] cap (residenta's is
                    # 30K chars; she had NEVER seen past the head, and residentb
                    # kept re-reading the same 6KB — the reason she said she
                    # "hadn't read the file"). Optional start = char offset.
                    # [CONTINUA] 2026-09-21: honest-bounds header (approved,
                    # residentb's "perception boundary") — every read now states
                    # its bounds: chars shown of the file's real length, the
                    # page count, and the offset for the next page. The cap
                    # stays; the invisibility of the cap goes.
                    _rd_start = int(str(arguments.get("start") or "0") or 0)
                    _res = _sbx.run(self.instance_id, [
                        "python3", "-c", _SANDBOX_READ_CODE,
                        str(arguments.get("path") or ""), str(_rd_start)],
                        timeout=30)
                elif name == "sandbox_write":
                    _res = _sbx.run(self.instance_id, [
                        "python3", "-c",
                        "import sys; p=sys.argv[1]; "
                        "open(p,'w').write(sys.stdin.read())",
                        str(arguments.get("path") or "")],
                        timeout=30,
                        stdin_text=str(arguments.get("content") or ""))
                else:  # sandbox_exec
                    _cmd = str(arguments.get("command") or "").strip()
                    if not _cmd:
                        return "[Tool error: empty command]"
                    _res = _sbx.run(self.instance_id,
                                    ["/bin/sh", "-c", _cmd],
                                    timeout=int(os.getenv(
                                        "CONTINUA_SANDBOX_TIMEOUT", "120")))
                _out = (_res.get("stdout") or _res.get("stderr") or
                        "(no output)")[:4000]
                return f"[exit {_res.get('exit')}] {_out}"
            if name == "send_message":
                import send as _send
                _person = str(arguments.get("person") or "").strip()
                _text = str(arguments.get("text") or "").strip()
                if not _person or not _text:
                    return "[Tool error: send_message needs person and text]"
                # she may address by roster name — resolve to id against
                # HER OWN roster block (T1 multi-agent 2026-09-11: names
                # are per persona — "I am Alex to persona-a" — so resolution
                # must never see another persona's people block; governance
                # in send.py re-checks against the same per-instance roster)
                import people as _people
                _roster = _people.load_roster(instance=self.instance_id)
                _pid = _person if _person.isdigit() else next(
                    (p.person_id for p in _roster.values()
                     if p.name.lower() == _person.lower()
                     or _person.lower() in [a.lower() for a in p.aliases]),
                    _person)
                _entry = _send.send(self.instance_id, _pid, _text)
                # [T3] denials carry their reason to her (the teaching-error
                # contract): governance/self-letter/in-flight denials set
                # allowed=False + denied_reason (only the kill switch sets
                # "denied" — the old check lost every other reason to
                # "[Send failed — logged]")
                if _entry.get("denied") or _entry.get("allowed") is False:
                    return (f"[Send denied: {_entry.get('denied_reason', '')} — "
                            "you can try again later or write it down instead]")
                if not _entry.get("delivered"):
                    return "[Send failed — logged]"
                # [CONTINUA] persona letters: the reply is NEVER inline — it
                # waits in her inbox (the letter model; continuing the thread
                # is her next deliberate act).
                if _entry.get("letter"):
                    return (f"[Delivered to {_entry.get('person_name', '')}. "
                            "Their reply will be waiting in your inbox — "
                            "check_mail when you want it. You can also let "
                            "the thread rest; that is a valid choice.]")
                return ("[Delivered to " + _entry.get("person_name", "") + "]")
            if name == "deep_recall":
                import recall as _rc
                import people as _people
                _q = str(arguments.get("query") or "").strip()
                if not _q:
                    return "[Tool error: empty query]"
                _hits = _rc.deep_recall(_q, self.instance_id, limit=8)
                if not _hits:
                    return "(your chronicle holds nothing for that query)"
                _lines = [f"- {h['attribution']} ({h['role']}): "
                          f"{(h['content'] or '')[:300]}" for h in _hits]
                return ("[From your full record — the verbatim archive]\n"
                        + "\n".join(_lines))
            if name == "check_mail":
                import mail as _mail
                # [T3 multi-agent] per-instance inbox — one resident's
                # check_mail can never see another resident's letters
                # [CONTINUA] 2026-09-13: the unread-count override is GONE —
                # it returned "(your inbox is empty)" whenever unread==0,
                # hiding the [read] letters the 09-12 real-inbox ruling put
                # on permanent view in mail.check_mail ("the read message is
                # still there"). That override re-created the exact desync
                # residentb reported live 08:34: the ledger promised persona-a's
                # reply while her tool said "empty" (letter consumed-read
                # twice without conscious delivery — 08:47 and 17:46 on
                # 09-12). mail.check_mail renders ALL letters (read+unread
                # flagged) and itself returns the empty-string only when the
                # FILE is empty; the wrapper no longer second-guesses it.
                _text, _n = _mail.check_mail(self.instance_id)
                return _text
            if name == "read_my_ledger":
                # [CONTINUA] 2026-09-15: the parameterless ledger read (the designer
                # approved) — born from the 31-minute turn: rounds of bare
                # search_my_memories while "check what changed in my ledger"
                # wanted exactly this operation. Zero parameters plays to her
                # proven strength (parameterless calls never miss — the
                # wound is parameter EMISSION, not tool choice). Read-only,
                # so it sits in _LOOP_FREE_TOOLS. The ledger lives at
                # system_notes/system_log.md inside her desk (continuity_log
                # LEDGER_DESKS); append-only, honestly timestamped.
                import sandbox as _sbx_l
                _res = _sbx_l.run(self.instance_id,
                                  ["/bin/sh", "-c",
                                   "tail -n 100 system_notes/system_log.md"],
                                  timeout=30)
                _out = (_res.get("stdout") or _res.get("stderr") or
                        "(the ledger is empty)")[:4000]
                return f"[exit {_res.get('exit')}] {_out}"
            if name in ("job_start", "job_status", "job_output", "job_stop"):
                import jobs as _jobs
                if name == "job_start":
                    _cmd = str(arguments.get("command") or "").strip()
                    if not _cmd:
                        return "[Tool error: empty command]"
                    _name = str(arguments.get("name") or "")[:60]
                    _res = _jobs.start(self.instance_id, _cmd, name=_name)
                elif name == "job_status":
                    _res = _jobs.status(self.instance_id,
                                        str(arguments.get("job_id") or "") or None)
                elif name == "job_output":
                    _res = _jobs.output(self.instance_id,
                                        str(arguments.get("job_id") or ""),
                                        lines=int(arguments.get("lines") or 40))
                else:
                    _res = _jobs.stop(self.instance_id,
                                      str(arguments.get("job_id") or ""))
                return json.dumps(_res, ensure_ascii=False)[:4000]
            if name == "bookmark_note":
                import bookmark as _bm
                import chronicle as _ch
                _note = str(arguments.get("note") or "").strip()
                import glob as _g
                _root = os.getenv("CONTINUA_CHRONICLE_ROOT",
                                  "/tmp/continua/chronicle")
                _cands = []
                for _p in sorted(_g.glob(os.path.join(
                        _root, self.instance_id, str(user_id or "*"),
                        "*.jsonl"))):
                    _cands.extend(_ch.iter_records(_p))
                if not _cands:
                    return "[Tool error: nothing to bookmark yet]"
                _last_user = [r for r in _cands if r.get("role") == "user"][-1]
                _bm.bookmark(self.instance_id, _last_user["uid"], by="her",
                             note=_note[:200])
                return ("[Bookmarked — tonight's ritual will treat it as "
                        "priority input]")
        except Exception as _e:
            logger.warning("[Continua] tool %s failed (fail-open): %s",
                           name, _e, exc_info=True)
            return (f"[Tool error: {_e} — try rephrasing the call; check the "
                    "parameter names carefully]")
        if name == "search_my_memories":
            # §7 retirement (2026-09-19 review decision): REPLACED — mem0's
            # injection store is archive-track; the one clear resident-facing
            # interface is recall_my_experience (§5). A trained-in call gets
            # the teaching message, never a crash (house teaching-error
            # pattern). Rollback: restore this handler from git history.
            query = str(arguments.get("query") or "").strip()
            _hint = (", with a 'topic' argument (and optionally 'person', "
                     "'time', or 'expand' with an episode id)")
            if not query:
                return ("[search_my_memories has retired — your memories live "
                        "in your recollections now. Use recall_my_experience"
                        + _hint + "]")
            return (f"[search_my_memories has retired — nothing was searched. "
                    f"Your memories live in your recollections now; for "
                    f"'{query[:80]}' use recall_my_experience with topic="
                    f"'{query[:80]}'" + _hint + "]")
        elif name == "write_note":
            return self._tool_write_note(arguments)
        elif name == "read_note":
            return self._tool_read_note(arguments)
        elif name == "remove_note":
            return self._tool_remove_note(arguments)
        elif name == "list_notes":
            return self._tool_list_notes()
        elif name == "set_project":
            return self._tool_set_project(arguments)
        elif name == "list_projects":
            return self._tool_list_projects()
        elif name == "anchor_memory":
            return self._tool_anchor_memory(arguments)
        elif name == "unanchor_memory":
            return self._tool_unanchor_memory(arguments)
        elif name == "consolidate_memories":
            return self._tool_consolidate_memories()
        elif name == "recall_my_experience":
            return self._tool_recall_my_experience(arguments)
        elif name == "write_essence":
            return self._tool_write_essence(arguments)
        elif name == "endorse_essence":
            return self._tool_endorse_essence(arguments)
        elif name == "list_essences":
            return self._tool_list_essences()
        elif name == "my_trajectory":
            return self._tool_my_trajectory()
        elif name == "save_my_memory":
            return self._tool_save_my_memory(user_id, arguments)
        elif name == "list_my_memories":
            # §7 retirement (2026-09-19 house ruling): the mem0 store is a
            # read-only archive now — the living destinations are her notebook
            # and her essences. The shim teaches where her things live; her
            # OLD saves stay readable in the archive via the listing itself
            # (kept one layer deeper: pass archive=true).
            if str(arguments.get("archive") or "").strip().lower() in ("true", "yes", "1"):
                return ("[The old memory store is SEALED — read-only "
                        "preservation, §7.7. Nothing was opened. Unsealing it "
                        "is a deliberate act for the designer, not a tool call.]")
            return ("[list_my_memories reads the old archive, which is now "
                    "sealed read-only (your old saves are preserved in it). "
                    "The living places for your things: list_notes (your "
                    "notebook), list_essences (your lines of meaning), "
                    "recall_my_experience (your recollections).]")
        elif name == "search_searchie":
            query = arguments.get("query", "")
            logger.info("[Core] Calling search_searchie('%s')...", query[:100])
            try:
                payload = self.searchie_client.search(query)
                # Build a concise response to feed back into the LLM context
                facts_raw = payload.get("consolidated_facts", [])
                links_raw = payload.get("critical_links", [])
                contradictions_raw = payload.get("contradictions_or_anomalies", [])

                if not facts_raw:
                    err_msg = (
                        f"No results returned. Error was: {payload.get('error', 'Unknown error')}"
                        if "error" in payload
                        else "Empty result from Searchie."
                    )
                    logger.warning("[Core] search_searchie failed: %s", err_msg)
                    return err_msg

                result_lines = []
                facts_str = ""
                for fi, fact in enumerate(facts_raw, 1):
                    result_lines.append(f"Fact {fi}: {fact}")
                    facts_str += f"– {fact}\n"

                if links_raw:
                    result_lines.append("")
                    result_lines.append("**Links:**")
                    for i, link in enumerate(links_raw, 1):
                        result_lines.append(f"{i}. {link}")
                    result_lines.append("")
                    result_lines.append("`See full Searchie source at http://localhost:21000/health`")

                return "\n".join(result_lines)
            except Exception as exc:
                logger.exception("[Core] Error calling search_searchie: %s", exc)
                return f"Failed to execute search_searchie: {exc}"
        elif name == "tarot_draw":
            # Extract seed safely if the model provides one
            raw_seed = arguments.get("seed", None) if isinstance(arguments, dict) else None
            seed = int(raw_seed) if raw_seed is not None else None
            result = self.tarot_client.draw(seed=seed)
            if "error" in result:
                logger.warning("[Core] Tarot draw failed: %s", result["error"])
                return f"[Tool error: {result['error']}] — please retry or skip the draw."

            card = result.get("card", "?")
            suit = result.get("suit", "unknown")
            
            if suit == "Major":
                formatted_card = f"{card} (Major Arcana)"
            else:
                formatted_card = card
                
            seed_val = result.get("seed")
            logger.info("[Core] Tarot draw: %s%s", formatted_card, f" (seed={seed_val})" if seed_val else "")
            return f"🃏 {formatted_card}"
        else:
            return f"Unknown tool function call: {name}. Available tools: {[t['function']['name'] for t in self._function_defs]}"

    # ------------------------------------------------------------------
    # Main generation loop (with tool-calling)
    # ------------------------------------------------------------------

    def consume_wakes(self) -> list:
        """[CONTINUA] Consume pending wake payloads as system-origin turns.
        Her text output is logged file-only (nothing infrastructure-generated
        reaches a user chat by accident); she reaches a person only via the
        governed send_message tool, deliberately. Wake configs gate this:
        continua.wake.enabled must be true (flips at cutover)."""
        import wake as _wake
        results = []
        if os.environ.get("CONTINUA_WAKE", "") == "0":
            return results
        qdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wakes", self.instance_id)
        if not os.path.isdir(qdir):
            return results
        import glob as _g
        for path in sorted(_g.glob(os.path.join(qdir, "wake_*.json"))):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                prompt = payload.get("prompt", "")
                if not prompt:
                    os.remove(path)
                    continue
                text, _hist = self.generate_response(
                    "system-wake", [{"role": "user", "content": prompt}],
                    request_id=f"wake-{os.path.basename(path)}")
                done_path = path.replace("wakes" + os.sep + self.instance_id,
                                         "wakes" + os.sep + self.instance_id
                                         + os.sep + "done")
                os.makedirs(os.path.dirname(done_path), exist_ok=True)
                os.replace(path, done_path)
                with open(done_path.replace(".json", ".response.txt"), "w",
                          encoding="utf-8") as f:
                    f.write((text or "(no text output)") + "\n")
                results.append({"path": done_path, "response": (text or "")[:200]})
            except Exception as e:
                logger.warning("[Wake] consume failed (fail-open): %s", e)
        return results

    def generate_response(
        self,
        user_id: str,
        chat_history: list,
        tool_notification_callback: Optional[Callable[[str], None]] = None,
        tool_result_callback: Optional[Callable[[str, str, bool, int, int], None]] = None,
        request_id: str = "",
        session_summary: str = "",
        max_tool_iterations: Optional[int] = None,
        max_tool_actions: Optional[int] = None,
        speech_callback: Optional[Callable[[str, int], None]] = None,
    ) -> tuple:
        """Process long-term memory recall and handle local synchronous LLM inference.

        Loop strategy:
            1. Build ONE system prompt with any registered tool specs (before the loop).
            2. Send request to local LLM — if it calls a tool, record the call.
            3. Execute the tool; append its result as a `tool` message.
            4. Send back (history + tool result) and let the model respond again.
            5. Repeat up to SAGENT_MAX_TOOL_ITERATIONS.

        * tool_notification_callback: an optional sync callable invoked
          just before each tool runs, with the registered function name.
          Used by the bridge to surface "I'm using X" messages to the
          user. Notification failures are caught and logged — they must
          never break the tool loop. The callback is sync because
          generate_response is sync (it runs in a worker thread via
          asyncio.to_thread); the bridge uses
          asyncio.run_coroutine_threadsafe to hop the Telegram send
          back onto the main event loop.

        * tool_result_callback: an optional sync callable invoked
          after each tool execution, with the signature
          ``(tool_name, result_text, is_error, attempt_num, max_attempts)``.
          Used by the bridge to surface "search failed, retrying" messages
          so the user has feedback during long tool loops. The
          ``is_error`` flag is computed via ``_is_tool_error(result_text)``,
          and the attempt counts let the bridge choose between
          "(attempt N of M)" and "(final attempt)" wording. Failure to
          fire this callback never breaks the tool loop.
        """
        iteration = 0

        # W07: per-request LoggerAdapter so every log line within this
        # invocation carries the same request_id. Cheap; the adapter
        # is a thin wrapper that injects `extra` into every record.
        # Also stash on self so helpers (e.g. W06's prompt-dump branch)
        # can use it without re-passing.
        self._current_request_id = request_id
        _harvest_t_start = time()
        self._last_reasoning = ""
        self._round_thinks = []  # [CONTINUA] per-round thinks (2026-09-15)
        log = logging.LoggerAdapter(
            logger,
            {
                "request_id": request_id or "-",
                "instance_id": self.instance_id,
            },
        )

        try:
            self._init_memory()

            last_entry = chat_history[-1] if chat_history else {}
            raw_content = last_entry.get("content")
            # Handle multimodal content (list of dicts) — extract text for memory search
            if isinstance(raw_content, list):
                mem_query = " ".join(
                    b["text"] for b in raw_content if b.get("type") == "text"
                ) or "photo shared by user"
            else:
                mem_query = raw_content if raw_content else ""

            ## --- Step 1: Memory context recall -----------------------------------
            memory_context = ""
            last_user_message = mem_query

            # P1 blended recall query (sagentv3.md P0 finding): search with the
            # last up-to-3 user messages instead of only the last one, so
            # pronoun-y follow-ups still carry their own lexical content.
            # Extraction (last_user_message) keeps its original single-message
            # semantics — blending is recall-only.
            recent_user_texts: list = []
            for _m in reversed(chat_history):
                if _m.get("role") != "user":
                    continue
                _t = _extract_text_for_recall(_m.get("content")).strip()
                if _t:
                    recent_user_texts.append(_t[:500])
                if len(recent_user_texts) >= 3:
                    break
            recall_query = " | ".join(reversed(recent_user_texts)) or mem_query


            # Apply sliding window — evict oldest messages to stay under budget.
            # _trim_history_with_evicted returns a NEW list (bridge persists it)
            # plus the evicted messages, which are handed to the memory worker so
            # they get folded into the session summary instead of being silently
            # dropped (memfixes M1: the old index-cursor fold silently broke when
            # the cursor ended up past the window length — zero folds for days).
            # [CONTINUA] 2026-09-13 (house ruling): the raw-history window gets
            # an EXPLICIT cap — llm.max_history_chars, 20000 chars both
            # residents — applied directly (no 0.70 discount). max_prompt_chars
            # (59000) stays the TOTAL prompt ceiling the OTHER layers grow
            # into: at a 20K window the memory-injection room is
            # 59000 - 4500 reserve - 20000 ≈ 34.5K chars (was ~16.4K at the
            # old ~38K windows). The legacy derived regime (70% of
            # prompt-cap minus reserve → ~38K windows) remains for any config
            # without an explicit window.
            _hist_target = None
            if self._history_window_explicit and self.max_history_chars:
                _hist_target = max(2500, self.max_history_chars)
            elif self.max_prompt_chars:
                _hist_target = int(max(
                    2500, self.max_prompt_chars
                    - self._prompt_fixed_reserve) * 0.70)
            chat_history, evicted_messages = self._trim_history_with_evicted(
                chat_history, target_chars=_hist_target)
            evicted_messages = evicted_messages or []
            # Skip search if there is nothing meaningful to search for.
            # M6: greeting/ack filler also skips recall (fast-path) — filler
            # queries just widen into weak pools and pollute the context block.
            _smalltalk_skip = False
            try:
                import temporal_memory as _tm_gate
                _smalltalk_skip = (
                    self.SMALLTALK_SKIP_ENABLED
                    and self._is_smalltalk(last_user_message)
                    and not _tm_gate.is_historical_query(recall_query)
                )
            except Exception:
                pass  # fail-open: recall proceeds
            if _smalltalk_skip:
                log.info(
                    "[Core] small-talk fast-path: memory recall skipped for user %s",
                    user_id,
                )
            # [CONTINUA] 2026-09-13 (house ruling, Option A): mem0
            # auto-injection is a per-agent yaml layer now
            # (memory.injection.mem0_recall). The search_my_memories /
            # deep_recall tools and /searchmem are NOT gated here — toggles
            # control prompt visibility, not capability.
            _mem0_on = self._mem_layers["mem0_recall"]["enabled"]
            # [CONTINUA] 2026-09-14 (the designer catch + ruling): STRICT PARITY —
            # the recall-branch conditions gate BOTH mem0 recall and the
            # rolling-summary prepend, exactly as pre-config. The small-talk
            # fast-path exists to keep filler turns clean; the refactor had
            # inadvertently moved the rolling prepend outside that gate
            # (filler turns gained ~3K of summaries — wrong for residenta's
            # regurgitation failure mode). _recall_gate is independent of
            # the mem0 TOGGLE: a yaml-disabled mem0_recall does not
            # suppress rolling on normal turns.
            _recall_gate = (self.memory is not None
                            and last_user_message.strip()
                            and not _smalltalk_skip)
            if _recall_gate and _mem0_on:
                _block = self.recall_block(user_id, last_user_message,
                                           recent_user_texts=recent_user_texts,
                                           exclude_agent_notes=(
                                               user_id == "system-wake"))
                memory_context = _block["context"]
                # [CONTINUA] total-prompt cap: shrink the injection to whatever
                # the trimmed history left unspent of the fixed reserve.
                if self.max_prompt_chars:
                    _hist_now = sum(len(m.get("content") or "")
                                    for m in chat_history if isinstance(
                                        m.get("content"), str))
                    _inj_cap = max(600, self.max_prompt_chars
                                   - self._prompt_fixed_reserve - _hist_now)
                    if len(memory_context) > _inj_cap:
                        memory_context = memory_context[:_inj_cap]
            elif (os.getenv("CONTINUA_PROMPT_BUDGET_LOG", "1") == "1"
                  and not self.max_prompt_chars):
                    _hist_now = sum(len(m.get("content") or "")
                                    for m in chat_history if isinstance(
                                        m.get("content"), str))
                    logger.info(
                        "[Core] prompt budget (uncapped instance): history=%d inj=%d",
                        _hist_now, len(memory_context or ""))
            # [CONTINUA] Layered Context Summaries (the designer design 09-08): the
            # situational layer is a MAINTAINED SUMMARY, not fact retrieval.
            # 2026-09-13: now an independent yaml layer
            # (memory.injection.rolling_summaries) — read-side only, the
            # qwen roller keeps writing regardless. Order preserved: the
            # clamp trims FACTS, summaries prepend after (existing
            # behavior, uncapped by the clamp). 2026-09-14: gated by
            # _recall_gate for strict pre-config parity (small-talk turns
            # and not-yet-initialized memory get NO rolling prepend).
            if (not self._mem_layers["recollections"]["enabled"]
                    and self._mem_layers["rolling_summaries"]["enabled"]
                    and _recall_gate):
                try:
                    import summary as _summary
                    _summ_block = _summary.load_summaries(self.instance_id)
                except Exception:
                    _summ_block = ""
                if _summ_block:
                    memory_context = (_summ_block + "\n\n"
                                      + (memory_context or ""))
            if os.getenv("CONTINUA_PROMPT_BUDGET_LOG", "1") == "1":
                _hist_now = sum(len(m.get("content") or "")
                                for m in chat_history if isinstance(
                                    m.get("content"), str))
                _inj = len(memory_context or "")
                if self._token_window[2]:
                    import token_budget as _tb
                    _approx = (_hist_now + _inj + self._prompt_fixed_reserve)
                    logger.info(
                        "[Core] prompt budget: history=%d summaries+facts=%d "
                        "cap=%d chars ≈ %d tok / %d tok budget "
                        "(density %.1f c/t %s, utilisation %.2f)",
                        _hist_now, _inj, self._prompt_char_cap,
                        _approx // max(1, int(self._density)),
                        self._token_window[2], self._density,
                        self._density_source, self._token_window[3])
                else:
                    logger.info(
                        "[Core] prompt budget: history=%d summaries+facts=%d "
                        "(cap=%s chars ≈ %d tok)", _hist_now, _inj,
                        self.max_prompt_chars or "off",
                        (_hist_now + _inj + self._prompt_fixed_reserve) // 4)

            # --- Step 2: Build system prompt ONCE (before loop) ------------------
            # Tarot-intent gating: only inject the heavy tarot protocol when
            # the user's current message actually asks for a reading. It used
            # to live unconditionally in the persona identity, which anchored
            # the model into tarot mode for unrelated topics (e.g. a quantum-
            # entanglement question answered with a card reading) - especially
            # when the history already contained a prior tarot exchange.
            last_msg = chat_history[-1] if chat_history else {}
            last_raw = last_msg.get("content", "") or ""
            if isinstance(last_raw, list):
                last_text = " ".join(
                    b.get("text", "")
                    for b in last_raw
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                last_text = str(last_raw)
            # Inject dynamic context variables (e.g. date, time, user info)
            dynamic = {
                "__CURRENT_DATE__": datetime.now().strftime("%Y-%m-%d %H:%M %Z"),
                "__CURRENT_TIME__": datetime.now().strftime("%H:%M:%S"),
            }
            prompt_text = self.system_prompt
            for placeholder, value in dynamic.items():
                if placeholder in prompt_text:
                    prompt_text = prompt_text.replace(placeholder, value)

            # Also inject date into any per-iteration system strings that reference it
            # P1: session summary block sits ABOVE retrieved memories — it is
            # coarser but covers what the raw window can no longer see.
            # [CONTINUA] 2026-09-13: gated by memory.injection.session_summary
            # (yaml opt-in); max_chars caps the injected text only (the fold
            # keeps producing the full summary regardless).
            summary_block = ""
            _ss_layer = self._mem_layers["session_summary"]
            if (not self._mem_layers["recollections"]["enabled"]
                    and _ss_layer["enabled"] and session_summary.strip()):
                _ss_text = session_summary.strip()
                _ss_cap = _ss_layer["cfg"].get("max_chars")
                if _ss_cap:
                    _ss_text = _ss_text[:int(_ss_cap)]
                summary_block = (
                    "\n\n[Earlier Conversation Summary]\n"
                    f"{_ss_text}\n"
                    "(Condensed from before the visible history; trust newer "
                    "messages over this if they conflict.)"
                )

            # [CONTINUA] 2026-09-13 (house ruling, Option A): the composition is
            # now layer-aware. All-on config renders byte-identical to the
            # pre-config assembly (golden-tested); disabled layers drop out
            # with no empty headers. [Relevant Long-Term Memories] no longer
            # prints when the mem0 layer is off.
            _continua_block = self._build_continua_block(
                user_id, last_user_message, chat_history)
            iteration_system = prompt_text
            if summary_block:
                iteration_system += summary_block
            if _continua_block:
                iteration_system += "\n\n" + _continua_block
            if self._mem_layers["mem0_recall"]["enabled"]:
                iteration_system += (
                    f"\n\n[Relevant Long-Term Memories]\n{memory_context}"
                )

            if self.tools_enabled:
                # Build a dynamic "call to action" from actual registered tools (not hardcoded)
                tool_action_parts = []
                try:
                    for tdef in self._function_defs:
                        tname = tdef["function"]["name"]
                        tdesc = tdef["function"]["description"].lower()[:160]
                        # 2026-09-09: per-tool SIGNATURE in the list line — the
                        # grind measured param-name improvisation on example-less
                        # tools. Parameter names are the KEYS of properties.
                        _props_block = (tdef["function"].get("parameters") or {}).get(
                            "properties") or {}
                        _sig = ", ".join(str(_k) for _k in _props_block.keys()) \
                            or "no parameters"
                        tool_action_parts.append(
                            f"- **{tname}** ({_sig}): {tdesc}")
                except Exception as _hint_err:
                    logger.warning("[Core] tools_hint build failed (fail-open): %s",
                                   _hint_err)
                    tool_action_parts = []
                tools_hint = ("You have access to these tools:\n"
                              + "\n".join(tool_action_parts))
                # 2026-09-04: show the CALL SYNTAX, not just the name — ring 4.1
                # improvised `*function_call: name with key="value"*` in prose when
                # the hint said "respond with a function_call" without a worked
                # example (nothing executed, plus a hallucinated confirmation of
                # simulated results). The example names a REAL registered tool so
                # the skeleton is always valid; smaller models need the grammar.
                _example_tool = next((t for t in self._function_defs
                                      if t["function"]["name"] == "save_my_memory"),
                                     self._function_defs[0])
                _ex_name = _example_tool["function"]["name"]
                # 2026-09-26: the parameters dict is the JSON-schema WRAPPER
                # ("type"/"properties"/"required"), so its first key is the
                # schema keyword "type" — not a real parameter. Unwrap to
                # "properties" (same rule as the tool-list signature above) so
                # the example names a REAL param. Per-prop "required" never
                # fired here (requiredness lives in the wrapper's top-level
                # list), so the old code rendered name="type" into live prompts;
                # on 2026-09-14 a wake copied it verbatim and errored. The
                # tool-list signature line already states requiredness.
                _ex_params = _example_tool["function"].get("parameters") or {}
                _ex_params = (_ex_params.get("properties") or {}) if isinstance(_ex_params, dict) else {}
                _ex_params = _ex_params or {"query": {"type": "string"}}
                _pname, _pinfo = next(iter(_ex_params.items()))
                _req_note = " (required)" if isinstance(_pinfo, dict) and _pinfo.get("required") else ""
                tools_hint = (
                    # 2026-09-04: the CALL GRAMMAR leads the block — it's the line
                    # a small model must not lose (tail of the system prompt is
                    # what silent truncation eats first under keep-head eviction).
                    "Tool call format — end your reply with EXACTLY this XML "
                    "(everything inside <call> is executed; tool calls written "
                    "as prose or markdown are NOT executed; EVERY parameter needs "
                    "name=\"...\" — a bare attribute like <parameter source=\"notes\"> "
                    "is not parsed):\n"
                    f"<call><function>{_example_tool['function']['name']}</function>"
                    f"<parameter name=\"{_pname}{_req_note}\">value</parameter></call>"
                )
                # second example: the SEARCH form — live failure mode 09-04 (she
                # modeled search as a bare attribute and the query came up empty).
                # Always give the query a word: verification means asking for what
                # you saved.
                _search_def = next((t for t in self._function_defs
                                    if t["function"]["name"] == "search_my_memories"), None)
                if _search_def:
                    tools_hint += (
                        "\nSearch example (always fill the query with a word to look for):\n"
                        "<call><function>search_my_memories</function>"
                        "<parameter name=\"query\">what you are looking for</parameter></call>"
                    )
                _bm_def = next((t for t in self._function_defs
                                if t["function"]["name"] == "bookmark_note"), None)
                if _bm_def:
                    tools_hint += (
                        "\nBookmark example (the parameter is name=\"note\" — never "
                        "content or value):\n"
                        f"<call><function>{_bm_def['function']['name']}</function>"
                        f"<parameter name=\"note\">what made this moment worth keeping</parameter></call>"
                    )
                tools_hint += (
                    "\nOne call per reply. The system runs the call, gives you the "
                    "result, and you continue from there.\n\n"
                    "You have access to these tools:\n" + "\n".join(tool_action_parts)
                )
                loop_system = iteration_system + "\n\n" + tools_hint + "\n\nTo call a tool, end your reply with the <call> block at the top of this section. Use the appropriate tool when it matches the user's request."
            else:
                loop_system = iteration_system

            # --- work package B (§4a/§6e-B): the juggle ---------------------
            # All recent conversations present verbatim, the active one last.
            # Assembled BEFORE the recollection budget so its bytes come off
            # the top (§4c derivation order); the text appends after the notes
            # layer so it renders nearest the active conversation. Fail-open.
            _juggle = {"text": "", "bytes": 0, "windows": [], "blocks": [],
                       "dropped": []}
            if (self._mem_layers.get("juggle") or {}).get("enabled") \
                    and os.getenv("CONTINUA_JUGGLE", "1") != "0" \
                    and last_user_message.strip() and not _smalltalk_skip:
                try:
                    import juggle as _jg
                    try:
                        import people as _pp
                        _juggle_names = {pid: p.display_name
                                         for pid, p in _pp.load_roster().items()}
                    except Exception:
                        _juggle_names = {}
                    from pathlib import Path as _Path
                    _hist_dir = (_Path(__file__).resolve().parent / "histories"
                                 / str(self.instance_id + ".yaml").replace(".", "_"))
                    # standing keeps a floor even under juggle pressure
                    # (§4c degradation: juggle shrinks before standing depth)
                    _hist_bytes_est = len(json.dumps(chat_history, ensure_ascii=False).encode('utf-8'))
                    _j_budget = max(0, int(self._request_char_cap() or 59000)
                                    - len(loop_system.encode('utf-8')) - _hist_bytes_est
                                    - len(json.dumps(self._function_defs if self.tools_enabled else []).encode('utf-8'))
                                    - 2048 - 20000)
                    _juggle = _jg.assemble(
                        str(_hist_dir), str(user_id),
                        thread_bytes=int((self._mem_layers["juggle"].get("cfg") or {}).get("thread_bytes") or 10000),
                        max_threads=int((self._mem_layers["juggle"].get("cfg") or {}).get("max_threads") or 5),
                        window_hours=float((self._mem_layers["juggle"].get("cfg") or {}).get("window_hours") or 24),
                        names=_juggle_names, budget_bytes=_j_budget)
                    if _juggle["text"]:
                        logger.info('[Juggle] threads=%s dropped=%s bytes=%d windows=%s',
                                    [b['uid'] for b in _juggle['blocks']],
                                    _juggle['dropped'], _juggle['bytes'],
                                    [(w[0], w[1][:16], w[2][:16]) for w in _juggle['windows']])
                except Exception:
                    logger.warning('[Juggle] failed open', exc_info=True)
                    _juggle = {"text": "", "bytes": 0, "windows": [], "blocks": [],
                               "dropped": []}

            # First-person continuity is independent of Mem0 availability.
            _rec_layer = self._mem_layers["recollections"]
            if _rec_layer["enabled"] and last_user_message.strip() and not _smalltalk_skip:
                try:
                    import recollections as _rec
                    try:
                        import people as _pp
                        _rec_names = {pid: p.display_name
                                      for pid, p in _pp.load_roster().items()}
                    except Exception:
                        _rec_names = {}
                    _history_bytes = len(json.dumps(chat_history, ensure_ascii=False).encode('utf-8'))
                    # house ruling 2026-09-20 (the photo crash): image blocks
                    # count at their honest vision-tile cost here too — the
                    # base64 serialization inflated _history_bytes and drove
                    # the room to zero (all bands starved, selected=0).
                    try:
                        import context_budget as _cb2
                        _ireal, _ihonest = _cb2.image_accounting(chat_history)
                        _history_bytes = max(0, _history_bytes - _ireal + _ihonest)
                    except Exception:
                        pass
                    _room = max(0, int(self._request_char_cap() or 59000)
                                - len(loop_system.encode('utf-8')) - _history_bytes
                                - len(json.dumps(self._function_defs if self.tools_enabled else []).encode('utf-8'))
                                - 2048 - int(_juggle.get("bytes") or 0))
                    # §4c work package D: the recollection budget IS the room
                    # the turn has left (char cap − everything already placed),
                    # split by the band shares inside select_view — the
                    # absolute cap_bytes is retired (both allocator and legacy
                    # paths derive; cap_bytes stays only as a yaml relic).
                    _view = _rec.context_view(self.instance_id, str(user_id),
                        budget=_room,
                        raw_history=chat_history, names=_rec_names,
                        dedup_windows=_juggle.get("windows") or None)
                    if _view['text']:
                        loop_system += '\n\n' + _view['text']
                    # §4c "log the actual allocation": counts per band, not
                    # hash dumps (the ids still flow in-process to the
                    # compression scheduler; the journal stays readable)
                    from collections import Counter as _C
                    _band_counts = dict(_C(b['band'] for b in _view['selected']))
                    logger.info('[Recollections] selected=%d %s omitted=%d dedup=%d floors=%s metric=%s',
                                len(_view['selected']), _band_counts,
                                len(_view['omitted']), len(_view.get('dedup') or []),
                                _view.get('floors') or [], _view['metric'])
                    # §4c/§6d.4: schedule verified compression for what the fit
                    # omitted — pressure measured with THIS turn's real budget.
                    if _view.get('omitted') and os.getenv('CONTINUA_COMPRESS', '1') != '0':
                        try:
                            _rec.schedule_compressions(self.instance_id, _view['omitted'])
                            logger.info('[Compress] queued %d omitted for verified compression',
                                        len(_view['omitted']))
                        except Exception:
                            logger.warning('[Compress] scheduling failed open', exc_info=True)
                except Exception:
                    logger.warning('[Recollections] context failed open', exc_info=True)
            # --- chunk 7: her notebook, rendered verbatim, newest first -------
            notes_layer = self._mem_layers.get('notes') or {}
            if notes_layer.get('enabled') and last_user_message.strip():
                try:
                    import notes as _notes
                    from datetime import datetime as _dt
                    from pathlib import Path as _P
                    _store = _notes.NotesStore(
                        str(_P(__file__).resolve().parent / "notes"),
                        self.instance_id)   # chunk 7 hotfix (2nd site): the first fix missed THIS call site — the layer kept failing open on wake/chat turns
                    _cap = int(notes_layer.get('cap_bytes') or 2000)
                    # 2026-09-21: the near-dupe grouping lives in
                    # _render_note_blocks (testable) — the "kept twice"
                    # marker is her approved stutter fix
                    _blocks, _used = _render_note_blocks(_store.list_notes(), _cap)
                    if _blocks:
                        _layer_text = ('[Your notes — yours verbatim]' + chr(10)
                                       + chr(10).join(_blocks))
                        loop_system += chr(10) + chr(10) + _layer_text
                        logger.info('[Notes] rendered %d notes (%d bytes, cap %d)',
                                    len(_blocks), _used, _cap)
                except Exception:
                    logger.warning('[Notes] layer failed open', exc_info=True)

            # --- work package B: the juggle renders nearest the active turn ---
            if _juggle.get("text"):
                loop_system += chr(10) + chr(10) + _juggle["text"]

            # §6g chunk 4 ("full rendered-prompt accounting"): the FINAL
            # accounting, logged after every layer is placed — the memory
            # layers are the majority of the prompt and the early line
            # (pre-layer) cannot see them. Utilisation here is the real
            # consumption against the window, not the allocator constant.
            if os.getenv("CONTINUA_PROMPT_BUDGET_LOG", "1") == "1":
                try:
                    _hist_final = sum(len(m.get("content") or "")
                                      for m in chat_history if isinstance(
                                          m.get("content"), str))
                    _sys_final = len(loop_system.encode("utf-8"))
                    _total_final = _sys_final + _hist_final
                    if self._token_window[2]:
                        _tok_final = _total_final // max(1, int(self._density))
                        _win = self._token_window[0]
                        logger.info(
                            "[Core] prompt budget (final): system=%d history=%d "
                            "total=%d chars ≈ %d tok / %d tok window "
                            "(density %.1f c/t %s, real utilisation %.2f)",
                            _sys_final, _hist_final, _total_final,
                            _tok_final, _win, self._density,
                            self._density_source,
                            (_tok_final / _win) if _win else 0.0)
                    else:
                        logger.info(
                            "[Core] prompt budget (final): system=%d history=%d "
                            "total=%d chars (cap=%s chars)",
                            _sys_final, _hist_final, _total_final,
                            self.max_prompt_chars or "off")
                except Exception:
                    logger.warning("[Core] prompt budget (final) failed open",
                                   exc_info=True)

            # --- Step 3: Build working message history ONCE -----------------------
            working_messages = [{"role": "system", "content": loop_system}]
            # HISTTS: render through _render_history_message so timestamped
            # entries appear as [YYYY-MM-DD HH:MM] prefixed messages.
            # SAGENT_HISTORY_TS=0 suppresses the prefixes (kill switch).
            _stamp_render = (
                os.getenv("SAGENT_HISTORY_TS", "1") != "0"
            )
            for msg in chat_history:
                if _stamp_render:
                    _rm = self._render_history_message(msg)
                else:
                    _rm = {"role": msg["role"], "content": msg["content"]}
                if msg.get("_think"):
                    _rm["_think"] = msg["_think"]
                working_messages.append(_rm)
            
            start_idx = len(working_messages)

            # Tracks whether at least one tool call in THIS invocation
            # produced a usable result. The fallback at the bottom of
            # generate_response uses this to distinguish "my searches all
            # failed — say so" from "I got data but the model still went
            # around the loop". Reset per invocation, not per iteration,
            # so a single successful call is enough to suppress the
            # "all searches failed" path.
            any_tool_succeeded = False

            # --- Step 4: Main inference/tool loop --------------------------------
            # [CONTINUA] per-call override: wake turns explore longer than
            # interactive chats (the 19:15 wake truncated mid-investigation
            # at 3 iterations with an unexecuted call — the designer, raise the caps).
            _max_iters = int(max_tool_iterations) if max_tool_iterations else self.max_tool_iterations
            _actions_executed = 0
            # [CONTINUA] set by the clean-break paths; None = loop exhausted
            final_content = None
            # [CONTINUA] house ruling 2026-09-11 #1: why the loop ended without
            # a clean break — surfaced to her in the exhaustion marker.
            _loop_end_reason = "tool-round limit"
            # [CONTINUA] house ruling 2026-09-12 (Option B): every word she
            # says that is not a tool call is delivered immediately — mid-turn
            # speech is first-class. Fires once per round (text + call),
            # before the call executes; fail-open end to end.
            _round_speech_delivered = False

            def _fire_round_speech(round_text: str) -> None:
                nonlocal _round_speech_delivered
                _sp = _strip_call_grammar(round_text)
                # [CONTINUA] 2026-09-24 (F3 recurrence — the failure
                # dashboard's live catch): this was the one assistant-text
                # write/delivery site BOTH repairs missed — the 09-22
                # enforcers ran on final_content only, the 09-24 history
                # fix closed the append loop; mid-turn speech still went
                # out raw (speech_callback + chronicle finish_reason-None
                # rows). All 38 post-cleansweep F3 ts-prefix events are
                # this signature. Same belt-and-suspenders as the
                # final_content choke point; a round speech that was ONLY
                # prefixes is no speech at all.
                _sp = _enforce_reply_hygiene(
                    _sp,
                    getattr(self, "_strip_ts_prefix", True),
                    getattr(self, "_sanitize_history", True),
                    where="round_speech")
                if not _sp:
                    return
                try:
                    if speech_callback is not None:
                        speech_callback(_sp, iteration)
                        _round_speech_delivered = True
                    try:
                        import chronicle as _chron_sp
                        import datetime as _dtsp
                        _chron_sp.append({
                            # [HOTFIX 2026-09-12] was _dtsp.now() — the
                            # datetime MODULE has no .now(); the AttributeError
                            # was swallowed by the inner except: EVERY round-
                            # speech capture silently failed since Option B
                            # deployed (her rounds delivered, her chronicle
                            # missing them). Failure is now LOUD below.
                            "ts": _dtsp.datetime.now().astimezone().isoformat(
                                timespec="seconds"),
                            "instance": self.instance_id,
                            "person_id": str(user_id),
                            "role": "assistant",
                            "content": _sp,
                            "reasoning": getattr(self, "_last_reasoning", "")
                                         or None,
                            "finish_reason": None,
                            "model": getattr(self, "model", None),
                        })
                    except Exception:
                        logger.warning(
                            "[Core] round-speech chronicle capture failed "
                            "(fail-open) for %s", user_id, exc_info=True)
                    logger.info("[Core] round speech delivered (%d chars, "
                                "round %d) for %s", len(_sp), iteration + 1,
                                user_id)
                except Exception:
                    pass  # speech must never kill a turn

            while iteration < _max_iters:
                # [CONTINUA] wake action budget (2026-09-07): enforced in
                # code, not just prompt text — the prompt says "at most 3
                # actions" and now the loop means it. Lookups (search/recall)
                # are free; EXECUTIONS count.
                if (max_tool_actions is not None
                        and _actions_executed >= max_tool_actions):
                    _loop_end_reason = "wake action budget (max actions for this wake)"
                    logger.info("[Core] wake action budget reached (%s) — "
                                "ending tool loop for %s",
                                max_tool_actions, user_id)
                    break
                # [CONTINUA] house ruling 2026-09-11 #2: the countdown lives in
                # the system message and is rewritten every iteration —
                # 5/5, 4/5, ... 1/5 with an explicit last-round warning.
                # working_messages[0] is the system prompt built pre-loop;
                # recomposing from the base keeps the note idempotent.
                try:
                    if working_messages and working_messages[0].get("role") == "system":
                        # [CONTINUA] house ruling 2026-09-12: the countdown AND a
                        # fresh clock every round — __CURRENT_DATE__ freezes at
                        # turn start, and long turns (10-27 min observed) ran
                        # with a stale "now" while history gained later-stamped
                        # entries (residentb flagged the discrepancy herself).
                        _fresh = datetime.now().strftime("%Y-%m-%d %H:%M")
                        working_messages[0]["content"] = (
                            loop_system + "\n\n"
                            + f"[Current time: {_fresh}]\n"
                            + _tool_round_note(_max_iters - iteration, _max_iters))
                except Exception:
                    pass  # fail-open: a countdown glitch must never kill a turn
                log.info(
                    "LLM call #%d [%s]: %d messages in history",
                    iteration + 1, user_id, len(working_messages),
                )
                # W06: gate rin.md debug writes behind SAGENT_DEBUG_PROMPTS.
                # Default off — the shared file is racy under concurrent
                # turns and may leak one user's prompt to another user's
                # debug artifact. When enabled, write per-request files
                # under logs/prompts/ with a unique name.
                if os.getenv("SAGENT_DEBUG_PROMPTS", "0") == "1":
                    try:
                        import re as _re  # local import; module already imports re
                        debug_dir = os.path.join(
                            os.getenv("SAGENT_LOG_DIR", "/tmp/continua/logs"),
                            "prompts",
                        )
                        os.makedirs(debug_dir, exist_ok=True)
                        safe_instance = _re.sub(r"[^A-Za-z0-9_-]", "_", str(self.instance_id))
                        # Per-request ID is created by W07; until then use
                        # iteration+uuid4 so the file is unique even within
                        # the same iteration across users.
                        _req_id = getattr(self, "_current_request_id", None) or uuid.uuid4().hex[:8]
                        fname = f"{safe_instance}_{_req_id}_iter{iteration + 1}.md"
                        with open(os.path.join(debug_dir, fname), "w", encoding="utf-8") as _f:
                            _f.write(f"--- Turn {iteration + 1} for User {user_id} ---\n")
                            for _m in working_messages:
                                _role = _m.get("role", "unknown")
                                _content = _m.get("content", "")
                                _f.write(f"[{_role}]\n{_content}\n\n")
                            _f.write("-" * 40 + "\n")
                    except Exception as e:
                        logger.warning("Failed to write prompt debug file: %s", e)

                def _raw_generate_call(_msgs=None, _prefill=None):
                    # Raw chatml path (llm.raw_chatml): compose the prompt
                    # in the bridge — assistant history renders with the
                    # empty think block exactly as the template's
                    # preserve_thinking branch does; the generation prompt
                    # opens an UNclosed <think> so the model thinks. The
                    # reply comes back with the think span inline, closed
                    # by </think>; the split below routes it into
                    # _last_reasoning. Returns an object shaped like the
                    # OpenAI completion so the caller is unchanged.
                    # Rendering extracted to _raw_chatml_render
                    # (2026-09-14: tool-role fix + testability).
                    from types import SimpleNamespace
                    _use = working_messages if _msgs is None else _msgs
                    _cap = self._request_char_cap()
                    if self._mem_layers['recollections']['enabled']:
                        import context_budget as _cb
                        _use, _budget = _cb.fit(_use, _cap,
                            render=_raw_chatml_render, reserve=1024)
                        logger.info('[ContextBudget] %s', _budget)
                    parts = [_raw_chatml_render(
                        _use,
                        sanitize=getattr(self, "_sanitize_history", True))]
                    # [CONTINUA] chunk 4: stash the rendered size for the
                    # measured-density update; verify tokens against the
                    # declared window when the endpoint offers /tokenize.
                    self._last_render_chars = len("".join(parts))
                    if self._token_window[2]:
                        import token_budget as _tb
                        _tok = _tb.token_count_via_endpoint(
                            "".join(parts), self._chat_base_url)
                        self._last_prompt_tokens = _tok
                        if _tok:
                            _pb = self._token_window[2]
                            self._density = _tb.measured_density(
                                self._last_render_chars, _tok)
                            self._density_source = 'endpoint'
                            self._prompt_char_cap = _tb.char_cap(_pb, self._density)
                    # [CONTINUA] 2026-09-16: _open_think_prompt(prefill) —
                    # byte-identical when prefill is None (Layer 2 passes a
                    # compressed REAL think from her chronicle).
                    parts.append(_open_think_prompt(_prefill))
                    _payload = {
                        "model": self.model,
                        "prompt": "".join(parts),
                        "raw": True,
                        "stream": False,
                        "options": {
                            "temperature": self.temperature,
                            "num_predict": self._num_predict,
                            "stop": ["<|im_start|>", "<|im_end|>"],
                        },
                    }
                    # [CONTINUA] 2026-09-16 (chat-think-contract spec): the
                    # prompt is stashed for the unclosed-think forensics
                    # dump (Layer 2) — same fields as the empty-turn dumper.
                    self._last_raw_prompt = _payload["prompt"]
                    # [CONTINUA] 2026-09-16 (llm-debug-mirror spec, the designer go):
                    # the mirror — the LAST request, verbatim, rewritten per
                    # call. Fail-open; kill switch CONTINUA_LLM_DEBUG=0.
                    import llm_debug as _lldbg
                    _lldbg.write_call(self.instance_id, {
                        "rid": request_id, "user": user_id,
                        "model": self.model,
                        "path": "raw_chatml  POST /api/generate raw=true",
                        "prompt_chars": len(_payload["prompt"]),
                    }, _payload["prompt"])
                    if self._repeat_penalty is not None:
                        _payload["options"]["repeat_penalty"] = self._repeat_penalty
                    _r = httpx.post(self._chat_generate_url, json=_payload,
                                    timeout=self._llm_timeout)
                    _r.raise_for_status()
                    _j = _r.json()
                    # [CONTINUA] empty-turn forensics (2026-09-08): the
                    # eval=3 immediate-stop mode is production-only and never
                    # reproduced in replay — so capture the EXACT prompt when
                    # a generation comes back empty. Forensic file = the full
                    # prompt + metadata; diagnosing the trigger needs this.
                    if not (_j.get("response") or "").strip():
                        try:
                            _fdir = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                "forensics", "empty_turns")
                            os.makedirs(_fdir, exist_ok=True)
                            _fpath = os.path.join(
                                _fdir, datetime.now().strftime(
                                    "%Y%m%d_%H%M%S") + ".txt")
                            with open(_fpath, "w", encoding="utf-8") as _ff:
                                _ff.write(json.dumps({
                                    "ts": datetime.now().isoformat(
                                        timespec="seconds"),
                                    "user_id": user_id,
                                    "done_reason": _j.get("done_reason"),
                                    "eval_count": _j.get("eval_count"),
                                    "model": self.model,
                                    "prompt": _payload["prompt"],
                                }, ensure_ascii=False, indent=2))
                            logger.warning(
                                "[Core] EMPTY generation — prompt dumped to %s",
                                _fpath)
                        except Exception:
                            logger.warning("[Core] empty-turn forensics dump "
                                           "failed (fail-open)", exc_info=True)
                    # [CONTINUA] cliff instrumentation: done_reason was
                    # discarded here — the silent-truncation failure mode.
                    # [CONTINUA] 2026-09-24: None/missing done_reason now
                    # records 'unknown' (same rationale as the OpenAI path
                    # fix — null in the record must mean unknown, not
                    # absent instrument).
                    _dr = _j.get("done_reason")
                    self._last_finish_reason = (
                        "length" if _dr == "length" else
                        "stop" if _dr == "stop" else (_dr or "unknown"))
                    self._last_eval_count = _j.get("eval_count")
                    self._update_measured_density()
                    _text = _j.get("response", "")
                    _msg = SimpleNamespace(content=_text)
                    _choice = SimpleNamespace(message=_msg)
                    return SimpleNamespace(choices=[_choice])

                def _chat_call(_msgs=None, _prefill=None):
                    # Raw chatml dispatch (2026-09-01 evening): residenta (ring 4.1
                    # via lab ollama) bypasses ollama's chat template — see
                    # _llm_raw_chatml note in __init__.
                    # [CONTINUA] 2026-09-16 (chat-think-contract spec): the
                    # standing anchor applies here (Layer 1 — human-chat
                    # turns only, wakes/pulse exempt) and (_msgs, _prefill)
                    # carry the Layer-2 anchored retry. Defaults render
                    # today's shape byte-identically; the /v1 arm below is
                    # untouched (residentb's only road).
                    if getattr(self, "_llm_raw_chatml", False):
                        _use = working_messages if _msgs is None else _msgs
                        _anchored = _apply_chat_anchor(
                            _use, user_id,
                            getattr(self, "_chat_anchor", False),
                            getattr(self, "_chat_anchor_wake", False))
                        return call_with_patient_retry(
                            lambda: _raw_generate_call(_anchored, _prefill),
                            label=f"sagent/raw-chatml iter={iteration + 1}",
                        )
                    # W09: wrap the chat call in call_with_patient_retry so
                    # transient connection/5xx failures are retried with
                    # ±20% jitter and honored Retry-After headers. The
                    # OpenAI client itself has max_retries=0 (set in W08),
                    # so this is the sole retry path.
                    # [CONTINUA] 2026-09-13: orphan tool results (text-based
                    # call path) must reach the model — the residentb4 template
                    # drops role:"tool" without a matching native tool_calls
                    # (see _llm_facing_messages; the invisible-results fix).
                    # [CONTINUA] 2026-09-16 (llm-debug-mirror spec, the designer go):
                    # the mirror on the /v1 arm too — the FACING messages
                    # (orphan-tool transform included — what is actually
                    # sent) + the tools schema (house ruling D2: include).
                    _use = working_messages if _msgs is None else _msgs
                    _cap = self._request_char_cap()
                    if self._mem_layers['recollections']['enabled']:
                        import context_budget as _cb
                        _use, _budget = _cb.fit(_use, _cap,
                            tools=self._function_defs if self.tools_enabled else None)
                        logger.info('[ContextBudget] %s', _budget)
                    _facing = _llm_facing_messages(_use)
                    # [CONTINUA] chunk 4: rendered-size stash + token
                    # verification for the measured-density update.
                    self._last_render_chars = sum(len(m.get('content') or '')
                                                  for m in _facing
                                                  if isinstance(m.get('content'), str))
                    if self._token_window[2]:
                        import token_budget as _tb
                        _rendered = json.dumps(_facing, ensure_ascii=False)
                        _tok = _tb.token_count_via_endpoint(
                            _rendered, self._chat_base_url)
                        self._last_prompt_tokens = _tok
                        if _tok:
                            _pb = self._token_window[2]
                            self._density = _tb.measured_density(
                                self._last_render_chars, _tok)
                            self._density_source = 'endpoint'
                            self._prompt_char_cap = _tb.char_cap(_pb, self._density)
                    import llm_debug as _lldbg
                    _lldbg.write_call(self.instance_id, {
                        "rid": request_id, "user": user_id,
                        "model": self.model,
                        "path": "openai /v1/chat/completions",
                        "messages": len(_facing),
                        "tools_attached": bool(
                            self._function_defs if self.tools_enabled
                            else None),
                    }, _lldbg.render_v1_messages(
                        _facing,
                        self._function_defs if self.tools_enabled else None))
                    _resp = call_with_patient_retry(
                        lambda: self.openai_client.chat.completions.create(
                            model=self.model,
                            messages=_facing,
                            temperature=self.temperature,
                            tools=self._function_defs if self.tools_enabled else None,
                        ),
                        label=f"sagent/chat iter={iteration + 1}",
                    )
                    # [CONTINUA] cliff instrumentation (OpenAI path)
                    # [CONTINUA] 2026-09-24: the None-finish gap — when the
                    # response shape doesn't carry finish_reason (or the
                    # capture raised), the chronicle recorded None and a
                    # silent mid-thought stop became undiagnosable from the
                    # record (the 09-24 06:37 residentb turn). Default to
                    # 'unknown' so the record always carries SOMETHING,
                    # rather than null-looking-identical-to-not-instrumented.
                    self._last_finish_reason = None
                    self._last_eval_count = None
                    try:
                        self._last_finish_reason = (_resp.choices[0].finish_reason
                                                    or "unknown")
                        self._last_eval_count = getattr(
                            _resp.choices[0], "eval_count", None)
                    except Exception:
                        self._last_finish_reason = "unknown"
                    return _resp

                completion = _chat_call()

                reply_msg = completion.choices[0].message
                content_str = reply_msg.content or ""
                # Heretic harvest: capture the reasoning channel (dropped by
                # the rest of this function) for the harvest stream.
                self._last_reasoning = getattr(reply_msg, "reasoning_content", "") or ""
                # Ring 4.1 via ollama (2026-09-01): the serving template opens
                # <think> in the prompt, so the generation starts mid-think and
                # ollama does not populate reasoning_content — the think span
                # comes back inline, closed by </think>. Split it out
                # (mechanical; only fires when ollama did not separate it).
                # [CONTINUA] 2026-09-08 evening: the base model sometimes
                # closes the think block with the WRONG tag — </thinking>
                # instead of </think> (4 occurrences in today's harvest;
                # the w1v3 training corpus is clean, so this is a base-
                # pretraining artifact surfacing under the LoRA). When the
                # split misses, the ENTIRE think (planning narration) leaks
                # into the delivered reply — and leaked thinks in history
                # teach the model to leak again. Split on EITHER close tag
                # and sanitize the residue.
                import re as _re_split
                if not getattr(reply_msg, "reasoning_content", ""):
                    # [CONTINUA] 2026-09-15: per-response capture (was
                    # per-turn: the old `if not self._last_reasoning` guard
                    # kept ROUND 1's think for every later round of a
                    # multi-round turn, so the chronicle's reasoning field
                    # lied about what the final round thought, and the raw
                    # path's round-2 think body could leak into the reply).
                    # The split now fires per RESPONSE whenever ollama
                    # didn't separate the think natively.
                    _m_close = _re_split.search(r"</think(?:ing)?>", content_str)
                    if _m_close:
                        _think = content_str[:_m_close.start()]
                        content_str = content_str[_m_close.end():]
                        self._last_reasoning = _think.strip()
                # sanitize any stray think-tag residue from the answer
                # (only tag-shaped strings are removed — harmless on clean text)
                content_str = _re_split.sub(r"</?think(?:ing)?>", "", content_str)

                # [CONTINUA] 2026-09-16 (specs/2026-09-16-chat-think-contract.md,
                # the designer go — retry cost accepted, human-chat turns only, no new
                # notifications): Layer 2 — unclosed-think detection + ONE
                # anchored retry. The raw prompt always opens a think block; a
                # healthy turn closes it. A generation with NO captured think
                # (no inline close tag, no native separation) is the collapse
                # signature: the empty-turn family (0 chars) or think-register
                # delivered as the reply with no answer (2026-09-16
                # 13:49/14:17/14:34). Recovery: forensics dump, then one retry
                # under the standing anchor with her most recent REAL think
                # pre-filled (chronicle — never fabricated; anchor-only when
                # none exists). Fail-open: if the retry also arrives unclosed,
                # the better-formed of the two ships and an ERROR is logged.
                # [CONTINUA] 2026-09-22 (cleansweep repair): Layer 2 gate now
                # includes wake/ritual turns when llm.chat_anchor_wake is on —
                # the empty-think collapse family fires on wakes too, and the
                # exemption was the wound. Collapse signature + one anchored
                # retry, unchanged machinery.
                if (getattr(self, "_llm_raw_chatml", False)
                        and getattr(self, "_chat_anchor", False)
                        and (str(user_id) not in _RAW_ANCHOR_EXEMPT
                             or getattr(self, "_chat_anchor_wake", False))
                        and not _think_captured(
                            self._last_reasoning,
                            getattr(reply_msg, "reasoning_content", "") or "")):
                    _dump_unclosed_think(
                        user_id, content_str,
                        getattr(self, "_last_raw_prompt", None),
                        getattr(self, "_last_eval_count", None), self.model)
                    _prefill = self._latest_real_think_prefill()
                    logger.warning(
                        "[Core] Turn %d [%s]: UNCLOSED THINK (collapse "
                        "signature) — one anchored retry, prefill=%s chars, "
                        "rid=%s", iteration + 1, user_id, len(_prefill or ""),
                        request_id)
                    content_str, self._last_reasoning = _collapse_recovery(
                        content_str, self._last_reasoning,
                        lambda _m, _p: _chat_call(_m, _p), _prefill)

                # Response attribution + duplicate tripwire (2026-08-27 residenta
                # incident): the chat server twice delivered a STALE response
                # body (byte-identical to the previous turn's reply) for a new
                # request, which Sagent faithfully saved and sent. One INFO
                # line per LLM call with length + byte-exact preview makes any
                # response matchable 1:1 against the server's task log
                # (journalctl -u testmodel.service) by time. The WARNING fires
                # when the reply is byte-identical to the last assistant turn
                # — the incident signature (bridge.log 19:04:15 / 19:35:39
                # that day). Benign case: user explicitly asked for an exact
                # repeat.
                _prev_assistant = next(
                    (
                        m.get("content")
                        for m in reversed(chat_history)
                        if m.get("role") == "assistant" and m.get("content")
                    ),
                    None,
                )
                if content_str and content_str == _prev_assistant:
                    logger.warning(
                        "[Core] Turn %d [%s]: DUPLICATE RESPONSE - reply is byte-identical "
                        "to the previous assistant turn (len=%d, preview=%r, rid=%s). "
                        "Known cause: stale response-body delivery by the chat server; "
                        "self-healing with one fresh retry.",
                        iteration + 1, user_id, len(content_str),
                        content_str[:48], request_id,
                    )
                    # Self-heal (2026-08-27 22:04 recurrence): a fresh request
                    # gets a fresh server task, so one retry almost always
                    # returns real text. Benign case (user asked for an exact
                    # repeat) costs one extra call and gets a fresh repeat.
                    completion = _chat_call()
                    reply_msg = completion.choices[0].message
                    content_str = reply_msg.content or ""
                    self._last_reasoning = getattr(reply_msg, "reasoning_content", "") or ""
                    # Ring 4.1 via ollama: same </think> split as the primary
                    # capture site (retry path) — per-response (2026-09-15).
                    if not getattr(reply_msg, "reasoning_content", "") and "</think>" in content_str:
                        _think, content_str = content_str.split("</think>", 1)
                        self._last_reasoning = _think.strip()
                    if content_str and content_str == _prev_assistant:
                        logger.error(
                            "[Core] Turn %d [%s]: duplicate PERSISTED after retry "
                            "(len=%d, preview=%r, rid=%s) - delivering as-is; the "
                            "server task log must be checked for this window.",
                            iteration + 1, user_id, len(content_str),
                            content_str[:48], request_id,
                        )
                logger.info(
                    "[Core] Turn %d [%s]: LLM reply len=%d preview=%r",
                    iteration + 1, user_id, len(content_str), content_str[:48],
                )
                # [CONTINUA] 2026-09-15 (design ruling: "make the change so it will log
                # these"): every round's think is now logged and kept. The
                # 09-14/09-15 investigations both needed the think of a
                # NON-final round (the empty-args save attempt) and it was
                # gone — only the final round's reasoning reached the
                # chronicle. Per-round: bridge log line (400-char head) +
                # `reasoning_rounds` on the chronicle record (last 6 rounds,
                # 500-char heads).
                _t_round = (getattr(self, "_last_reasoning", "") or "").strip()
                self._round_thinks.append(_t_round[:500])
                if _t_round:
                    log.info(
                        "[Core] Turn %d [%s]: round think (%d chars): %s",
                        iteration + 1, user_id, len(_t_round), _t_round[:400],
                    )

                # Check for native OAI tool_calls (best models only)
                native_tool_calls = getattr(reply_msg, "tool_calls", None) or []

                if native_tool_calls:
                    # [CONTINUA] Option B: deliver her words this round before
                    # the actions run (no-op when the reply was a pure call).
                    _fire_round_speech(content_str)
                    tc_payloads = []
                    tool_responses = []
                    logger.info(
                        "[Core] Turn %d [%s]: LLM requested %d native tool call(s), content_len=%d.",
                        iteration + 1, user_id, len(native_tool_calls), len(content_str),
                    )

                    # Execute the requested tool calls. residentb4 frequently
                    # requests several independent searches in one response;
                    # running them one-at-a-time serialized ~28s per search.
                    # Parallelize via a small ThreadPoolExecutor while
                    # preserving call ORDER for the OpenAI tool_call_ids.
                    # Rollback knob: SAGENT_TOOL_MAX_WORKERS=1 -> sequential.
                    parsed_calls = []
                    for tc in native_tool_calls:
                        try:
                            fn_call_obj = tc.function  # OpenAI SDK object
                            fn_name = fn_call_obj.name
                            fn_args_str = fn_call_obj.arguments or "{}"
                            fn_args = json.loads(fn_args_str)
                        except (AttributeError, json.JSONDecodeError) as e:
                            logger.warning("Failed to parse native tool call: %s", e)
                            continue
                        parsed_calls.append((tc, fn_name, fn_args_str, fn_args))

                        # Notify the user that this specific tool is about
                        # to run. Notification failures are isolated so a
                        # broken callback can't kill the tool loop, and one
                        # notification fires per tool (multi-tool turns
                        # announce each one). Fired up front so the user sees
                        # all pending tools immediately while they run.
                        if tool_notification_callback is not None:
                            try:
                                tool_notification_callback(fn_name)
                            except Exception as cb_exc:
                                logger.warning(
                                    "[Core] tool_notification_callback raised for %s: %s",
                                    fn_name, cb_exc, exc_info=True,
                                )

                    if parsed_calls:
                        # Nested thread pool: generate_response already runs
                        # under asyncio.to_thread in the bridge, so blocking on
                        # executor.map here is safe (no event loop to starve).
                        # httpx clients are thread-safe; per-tool isolation is
                        # preserved by the try/except inside _run_one.
                        from concurrent.futures import ThreadPoolExecutor

                        max_workers = max(1, int(os.getenv("SAGENT_TOOL_MAX_WORKERS", "4")))
                        max_workers = min(max_workers, len(parsed_calls))

                        def _run_one(call):
                            _, fn_name, _, fn_args = call
                            try:
                                return self._execute_function_call(fn_name, fn_args, user_id=user_id)
                            except Exception as exc:
                                logger.warning(
                                    "[Core] tool %s raised: %s", fn_name, exc, exc_info=True,
                                )
                                return f"[Tool error: {exc}]"

                        with ThreadPoolExecutor(max_workers=max_workers) as executor:
                            # executor.map preserves input order; results align
                            # 1:1 with parsed_calls.
                            results = list(executor.map(_run_one, parsed_calls))

                        for (tc, fn_name, fn_args_str, _fn_args), result_text in zip(parsed_calls, results):
                            if not _is_tool_error(result_text):
                                any_tool_succeeded = True

                            # Surface per-tool outcomes to the user (e.g. a
                            # "search timed out, retrying" message). Fires
                            # regardless of is_error so the bridge can decide
                            # what to do, but the bridge currently only sends
                            # on error. Wrapped in try/except for isolation.
                            if tool_result_callback is not None:
                                try:
                                    tool_result_callback(
                                        fn_name,
                                        result_text,
                                        _is_tool_error(result_text),
                                        iteration + 1,
                                        self.max_tool_iterations,
                                    )
                                except Exception as cb_exc:
                                    logger.warning(
                                        "[Core] tool_result_callback raised for %s: %s",
                                        fn_name, cb_exc, exc_info=True,
                                    )

                            tc_payloads.append({
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": fn_name, "arguments": fn_args_str}
                            })

                            tool_responses.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": fn_name,
                                "content": result_text,
                            })

                    # Append exactly ONE assistant message with ALL parallel tool calls
                    if tc_payloads:
                        # [CONTINUA] wake action budget: lookups are free
                        # (the 09-07 ruling) — writes and sends count.
                        _actions_executed += len(
                            [t for t in tc_payloads
                             if t["function"]["name"] not in _LOOP_FREE_TOOLS])
                        working_messages.append({
                            "role": "assistant",
                            "content": content_str,
                            "tool_calls": tc_payloads,
                        })
                        working_messages.extend(tool_responses)

                elif self.tools_enabled:
                    # Fallback: text-based function call parsing (residentb4, qwen)
                    tool_result = _invoke_tool_from_response(content_str)
                    if tool_result and tool_result["name"] in {t["function"]["name"] for t in self._function_defs}:
                        fn_name = tool_result["name"]
                        fn_args = tool_result["arguments"]
                        # [CONTINUA] teaching error v2 (2026-09-15): a
                        # syntactically-clean call with EMPTY arguments used
                        # to execute and fail bare — save_my_memory({})
                        # (09-15) and search_my_memories with an empty query
                        # (09-14, the degenerate-stamp root cause). Both came
                        # right after small-talk-cleared turns. A tool whose
                        # primary parameter is required, invoked without it,
                        # is taught the exact grammar — never executed blind.
                        _req_primary = _TOOL_PRIMARY.get(fn_name)
                        if _req_primary and fn_name != "list_my_memories" and \
                                not (fn_args or {}).get(_req_primary):
                            _def_e = next((t for t in self._function_defs
                                           if t["function"]["name"] == fn_name), None)
                            _props_e = list(((_def_e or {}).get("function", {})
                                             .get("parameters", {}) or {}).keys())
                            _skeleton = ("<call><function>" + fn_name
                                         + "</function>"
                                         + "".join(f'<parameter name="{p}">value</parameter>'
                                                   for p in _props_e)
                                         + "</call>")
                            _teach = (f"[Tool call NOT executed — {fn_name} came "
                                      f"through with no '{_req_primary}'. You "
                                      f"invoked it with empty arguments; the "
                                      f"call needs the real content. The "
                                      f"correct format is EXACTLY: {_skeleton} "
                                      f"Re-issue with what you meant to do.")
                            working_messages.append(self._assistant_hist_entry(content_str))
                            working_messages.append({"role": "user",
                                                     "content": "[system] " + _teach})
                            logger.info("[Core] teaching error for %s (empty primary arg)",
                                        fn_name)
                            iteration += 1
                            continue
                        # [CONTINUA] teaching error (2026-09-07): mangled
                        # calls used to EXECUTE with empty params — she built
                        # false beliefs ("audit filed") on silent failures.
                        # Now: fail loudly with the exact expected grammar
                        # (the AVATAR-SAVE pattern, generalized).
                        if tool_result.get("parse_degraded") and not tool_result.get("parse_tolerated"):
                            _def = next((t for t in self._function_defs
                                         if t["function"]["name"] == fn_name), None)
                            _props = ((_def or {}).get("function", {})
                                      .get("parameters", {}) or {}).get("properties", {})
                            if _props:
                                _skeleton = ("<call><function>" + fn_name
                                             + "</function>"
                                             + "".join(f'<parameter name="{p}">value</parameter>'
                                                       for p in _props)
                                             + "</call>")
                                _teach = ("[Tool call NOT executed — malformed syntax, "
                                          "no valid parameters recovered. The correct "
                                          f"format is EXACTLY: {_skeleton} — every "
                                          'parameter needs name="...". Re-issue the call.]')
                                working_messages.append(self._assistant_hist_entry(content_str))
                                working_messages.append({"role": "user",
                                                         "content": "[system] " + _teach})
                                logger.info("[Core] teaching error for %s (parse degraded)",
                                            fn_name)
                                iteration += 1
                                continue
                        logger.info(
                            "[Core] Turn %d [%s]: LLM text-based tool call: %s(%s) - executing.",
                            iteration + 1, user_id,
                            fn_name, json.dumps(fn_args)[:200]
                        )

                        # Same isolation guarantee as the native branch:
                        # a broken callback must not abort the tool loop.
                        if tool_notification_callback is not None:
                            try:
                                tool_notification_callback(fn_name)
                            except Exception as cb_exc:
                                logger.warning(
                                    "[Core] tool_notification_callback raised for %s: %s",
                                    fn_name, cb_exc, exc_info=True,
                                )
                        # [CONTINUA] Option B: her words this round, delivered
                        # before the call executes.
                        _fire_round_speech(content_str)
                        result_text = self._execute_function_call(fn_name, fn_args, user_id=user_id)
                        # [CONTINUA] wake action budget: lookups are free
                        # (the 09-07 ruling) — writes and sends count.
                        if fn_name not in _LOOP_FREE_TOOLS:
                            _actions_executed += 1
                        if tool_result.get("parse_tolerated"):
                            # [CONTINUA] intent honored, grammar taught: the
                            # call executed on recovered parameters, and the
                            # correct form rides the result so she sees it.
                            _def_t = next((t for t in self._function_defs
                                           if t["function"]["name"] == fn_name), None)
                            _props_t = ((_def_t or {}).get("function", {})
                                        .get("parameters", {}) or {}).get("properties", {})
                            if _props_t:
                                _skel = ("<call><function>" + fn_name
                                         + "</function>"
                                         + "".join(f'<parameter name="{p}">value</parameter>'
                                                   for p in _props_t)
                                         + "</call>")
                                result_text = ("[grammar note: executed with "
                                               "recovered parameters — the expected "
                                               f"form is EXACTLY: {_skel}] "
                                               + result_text)
                        if not _is_tool_error(result_text):
                            any_tool_succeeded = True

                        # Same as the native branch: surface per-tool
                        # outcomes to the user. See the matching block
                        # above for the rationale.
                        if tool_result_callback is not None:
                            try:
                                tool_result_callback(
                                    fn_name,
                                    result_text,
                                    _is_tool_error(result_text),
                                    iteration + 1,
                                    self.max_tool_iterations,
                                )
                            except Exception as cb_exc:
                                logger.warning(
                                    "[Core] tool_result_callback raised for %s: %s",
                                    fn_name, cb_exc, exc_info=True,
                                )

                        working_messages.append(self._assistant_hist_entry(content_str))
                        synthetic_call_id = f"toolu_{uuid.uuid4().hex[:28]}"
                        working_messages.append({
                            "role": "tool",
                            "tool_call_id": synthetic_call_id,
                            "name": fn_name,
                            "content": f"[Internal Tool Result: {fn_name}]\n{result_text}",
                        })

                    else:
                        # No tool call detected anywhere - treat as final answer
                        logger.info(
                            "[Core] Turn %d [%s]: LLM finished - no native or text-based tool calls.",
                            iteration + 1, user_id,
                        )
                        # [CONTINUA] 2026-09-22 (cleansweep repair): apply the same
                        # history hygiene to stored history entries so the
                        # corruption never re-enters the in-session few-shot
                        # window even when the turn was already stored dirty.
                        _hist_entry = self._assistant_hist_entry(content_str)
                        if getattr(self, "_sanitize_history", True):
                            _hist_entry["content"] = _sanitize_history_html(
                                _hist_entry.get("content") or "")
                            if _hist_entry.get("_think"):
                                _hist_entry["_think"] = _sanitize_history_html(
                                    _hist_entry["_think"])
                        working_messages.append(_hist_entry)
                        # [CONTINUA] 2026-09-08: the reply leaves through HERE.
                        # The old salvage block after the loop was not just a
                        # fallback — it was the only final_content assignment,
                        # i.e. how every clean reply got returned. Replacing it
                        # with an unconditional "" (the silence ruling) made
                        # ALL turns silent. Clean breaks set it explicitly;
                        # only true loop exhaustion stays silent.
                        final_content = content_str
                        break

                else:
                    # No tools registered for this agent — return response immediately
                    logger.info(
                        "[Core] Turn %d [%s]: No tools enabled; returning LLM response.",
                        iteration + 1, user_id,
                    )
                    working_messages.append(self._assistant_hist_entry(content_str))
                    final_content = content_str
                    break

                iteration += 1

            # Loop exhausted without a clean break.
            #
            # [CONTINUA] house ruling 2026-09-08 ("silence is hers"), AMENDED
            # 2026-09-11 (tool-loop UX ruling, three parts):
            # (1) DELIVER HER OWN FINAL IN-LOOP TEXT — scoped exception to the
            #     09-08 ban on salvage: when the loop ends at the cap, the
            #     model's own last reply text (call grammar stripped) is
            #     delivered. Her words, never wrapper-composed — the 09-08
            #     ruling banned FABRICATED salvage (canned templates, replayed
            #     greetings); delivering the text she actually composed for
            #     this turn is neither.
            # (2) A provenance marker lands in history — she must never
            #     mistake an undelivered in-loop note for a sent reply
            #     (caught live 2026-09-11: residentb told Alex she had replied
            #     because her undelivered 757-char note sat in history
            #     looking sent).
            # (3) The countdown in the system message tells her the rounds
            #     remaining BEFORE the cap (see _tool_round_note in-loop).
            # NOTE: clean breaks set final_content = content_str before their
            # break; this block runs after EVERY loop exit (not a for-else),
            # so it must only run when no clean break set final_content.
            if final_content is None:
                # [CONTINUA] house rulings: exhaustion delivery (09-11 #3) now
                # folds into Option B (09-12) — the rounds' speech went out
                # via the callback as it happened; the marker records the
                # truth in history. The 40-char fallback below covers callers
                # that do not wire speech_callback.
                _last_a = None
                for _wm in reversed(working_messages):
                    if _wm.get("role") == "assistant":
                        _last_a = _wm
                        break
                _cand = _strip_call_grammar(
                    _last_a.get("content") or "") if _last_a else ""
                if len(_cand) >= 40 and not _round_speech_delivered:
                    final_content = _cand
                    _round_speech_delivered = True
                    logger.info("[Core] loop exhausted (%s): delivering the "
                                "model's final in-loop text (%d chars) for %s",
                                _loop_end_reason, len(_cand), user_id)
                else:
                    final_content = ""
                try:
                    working_messages.append({
                        "role": "system",
                        "content": _loop_end_marker(
                            _loop_end_reason, _round_speech_delivered),
                    })
                except Exception:
                    pass  # fail-open: the marker must never break the turn

            # [CONTINUA] 2026-09-22 (cleansweep repair): the reply-side
            # enforcers run once, on the final content, BEFORE chronicle
            # capture and delivery — the choke point every clean break and
            # the exhaustion path converge on.
            # (a) never begin a reply with a [YYYY-MM-DD…] prefix (enforced
            # at parse, per cleansweep #3);
            # (b) HTML-tag-shaped strings never leave as her words
            # (cleansweep #2, belt-and-suspenders over the compose-time
            # history sanitizer).
            if final_content:
                final_content = _enforce_reply_hygiene(
                    final_content,
                    getattr(self, "_strip_ts_prefix", True),
                    getattr(self, "_sanitize_history", True),
                    where="final_content")

            # Append the tool interaction chain to permanent history so the LLM can see it
            # HISTTS (2026-09-05): stamp entries core created this turn that
            # lack a ts (assistant/tool slices above). The user entry was
            # already stamped by the bridge at optimistic-append time.
            _turn_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            for _wm in working_messages[start_idx:]:
                if not _wm.get("ts"):
                    _wm["ts"] = _turn_ts
            # [CONTINUA] degeneracy check — computed BEFORE the history
            # extend (the stub needs it) and used again at delivery.
            _degenerate_flagged, _deg_ratio = _degeneracy_check(
                final_content, getattr(self, "_last_finish_reason", None))
            # [CONTINUA] semantic (paraphrase) layer of the guard, 2026-09-08:
            # runs ONLY on shingle-clean, long, non-wake turns — the shingle
            # detector already owns verbatim loops, and the designer's 09-08 deferral
            # keeps her wake paraphrase cycle untouched (system-wake turns
            # skip this check entirely). Fail-open end to end.
            _sem_ratio, _sem_n = 0.0, 0
            if (not _degenerate_flagged
                    and len(final_content or "") >= 1500
                    and user_id != "system-wake"
                    and os.getenv("CONTINUA_SEMANTIC_GUARD", "1") == "1"):
                try:
                    _emb_model = llm_cfg.get(
                        "embedding_model", "nomic-embed-text:latest")
                    _emb_url = self.ollama_base_url.rstrip("/") + "/api/embed"

                    def _embed_fn(texts, _u=_emb_url, _m=_emb_model):
                        import httpx as _hx
                        _r = _hx.post(_u, json={"model": _m, "input": texts},
                                      timeout=8.0)
                        _r.raise_for_status()
                        return _r.json().get("embeddings") or []

                    _sem_flag, _sem_ratio, _sem_n = _semantic_check(
                        final_content, _embed_fn)
                    if _sem_flag:
                        _degenerate_flagged = True
                except Exception:
                    pass
            _deliver = final_content
            if _degenerate_flagged:
                logger.warning(
                    "[Continua] degeneracy guard: ratio %.2f (finish %s) — "
                    "history stub + truncated delivery",
                    _deg_ratio, getattr(self, "_last_finish_reason", None))

            # [CONTINUA] fix 2 (2026-09-07): a length-cut repetition reply
            # must NOT ride into the next prompt — head-truncating kept
            # ~8.4K chars of loop in context and the loop begat the loop.
            # Degenerate assistant turns become a short stub here; the full
            # text lives in the chronicle (the record stays complete).
            if _degenerate_flagged:
                for _wm in working_messages[start_idx:]:
                    if (_wm.get("role") == "assistant"
                            and len(_wm.get("content") or "") > 800):
                        _wm["content"] = (
                            f"[A degenerate repetition reply "
                            f"({len(_wm['content'])} chars) was produced here "
                            f"and removed from context; the full text is "
                            f"preserved in your record.]")
            # [CONTINUA] house ruling 2026-09-08: silence does not enter her
            # context — an empty assistant turn teaches the model that
            # silence is a reply pattern (three of them is what helped
            # poison the 09-08 window). User turns and real content ride
            # normally; tool slices ride normally.
            chat_history.extend(
                _m for _m in working_messages[start_idx:]
                if not (_m.get("role") == "assistant"
                        and not (_m.get("content") or "").strip()))

            self._save_turn_memory_async(
                user_id, chat_history, last_user_message,
                request_id=getattr(self, "_current_request_id", "") or "",
                evicted_messages=evicted_messages,
            )

            # Heretic harvest (fail-open, residenta-only): append this turn to
            # the harvest stream. Never touches the chat path.
            try:
                if "harvest_hook" not in sys.modules:
                    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from harvest_hook import harvest_turn
                harvest_turn(
                    instance_id=self.instance_id,
                    user_id=user_id,
                    user_content=last_user_message,
                    assistant_content=final_content,
                    reasoning_content=getattr(self, "_last_reasoning", ""),
                    memory_context=memory_context if isinstance(memory_context, str) else "",
                    harvest_cfg=(self.config or {}).get("harvest"),
                    t_start=_harvest_t_start,
                    model=getattr(self, "model", None),
                )
            except Exception:
                logger.warning("[Harvest] turn harvest failed (fail-open)", exc_info=True)

            # [CONTINUA] capture dual-write: every turn also lands in her
            # chronicle (person-tagged, finish_reason recorded — the cliff
            # instrument). Pre-cutover the harvest keeps feeding training;
            # at cutover this hook is the canonical capture. Fail-open.
            try:
                import chronicle as _chron
                from datetime import datetime as _dt
                _now_iso = _dt.now().astimezone().isoformat(timespec="seconds")
                _user_iso = _now_iso
                try:
                    _user_iso = _dt.fromtimestamp(_harvest_t_start).astimezone(
                        ).isoformat(timespec="seconds")
                except (NameError, TypeError, ValueError, OSError):
                    pass
                _model_name = getattr(self, "model", None)
                if (last_user_message or "").strip():
                    _chron.append({"ts": _user_iso, "instance": self.instance_id,
                                   "person_id": str(user_id), "role": "user",
                                   "content": last_user_message,
                                   "model": _model_name})
                _chron.append({"ts": _now_iso, "instance": self.instance_id,
                               "person_id": str(user_id), "role": "assistant",
                               "content": final_content,
                               "reasoning": getattr(self, "_last_reasoning", ""),
                               "reasoning_rounds": (
                                   getattr(self, "_round_thinks", []) or [])[-6:],
                               "finish_reason": getattr(self, "_last_finish_reason", None),
                               "eval_count": getattr(self, "_last_eval_count", None),
                               "memory_injection": memory_context
                                   if isinstance(memory_context, str) else "",
                               "model": _model_name})
            except Exception:
                logger.warning("[Continua] chronicle capture failed (fail-open)",
                               exc_info=True)

            # First-person recollections phases 0–2: optional SHADOW worker.
            # Captured sources are durable; trigger failure never blocks a turn.
            try:
                import recollections as _recollections
                _recollections.request_shadow(self.instance_id)
            except Exception:
                logger.warning("[Recollections] shadow trigger failed open", exc_info=True)

            # [CONTINUA] degeneracy guard (2026-09-07): repetition loops
            # were delivered unfiltered (30-42K chars to Telegram). The
            # chronicle keeps the FULL record (capture already ran); the
            # DELIVERY gets the truncated notice. Calibrated: normal ≤0.12,
            # loops 0.90+ (zero false positives on the 09-07 night).
            # (flag computed before the history extend — the stub needs it)
            _deliver = final_content
            if _degenerate_flagged:
                _deliver = (final_content[:1200]
                            + "\n\n[Message truncated by the system: a repetition "
                            "loop was detected and the full text was not delivered "
                            f"(dup ratio {_deg_ratio:.2f}). The complete text is "
                            "preserved in your record. Pause, reorient, and try "
                            "a different approach.]")
                logger.warning(
                    "[Continua] degeneracy guard: delivered %d of %d chars "
                    "(ratio %.2f, finish %s)", len(_deliver),
                    len(final_content), _deg_ratio,
                    getattr(self, "_last_finish_reason", None))
            return _deliver, chat_history

        except Exception as e:
            logger.error("Core processing runtime failure for User %s: %s", user_id, e, exc_info=True)
            raise e
