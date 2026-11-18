"""ritual.py — the Ritual service: the nightly pulse (phase 3 of Continua).

Wiki: ~/agentwiki/projects/Continua.md. This is the self-awareness layer —
NOT a stage in the janitor's process (review round two #1). One component,
mirror-enabled personas, two cadences (nightly pulse here; quarterly
heartbeat = phase 5).

The nightly pulse, per the plan:
  1. COLLECT (mechanical): sync the mirror from the live harvest (pre-cutover
     the harvest is Sagent's; at cutover this reads Continua's own capture),
     update the index.
  2. SEGMENT (mechanical): the day's turns → candidate scenes by time-gap.
     Scenes are derived AT CONSOLIDATION, never at capture (principle 1).
  3. MEAN (HER): her own model reads the day's scenes — attributed by roster
     names — and decides what to keep and what each kept scene meant, in her
     voice. Bookmarked turns are priority input (signal-modulated depth,
     review round one #2). Nothing is auto-kept: salience is hers.
  4. VERIFY (utility mind): qwen checks each kept meaning's FACTUAL ANCHORS
     against the scene record — fabricated quotes/names/dates flagged;
     the interpretive layer is never policed (review round one #3).
  5. MARK (append-only): kept scenes → salience marks. The marks log is the
     salience record; at cutover, Sagent's decay_candidates() skips points
     carrying a mark (salience-primary decay, review round one #1) and the
     mark-rate monitor guards over-marking (kept/total in the digest).

Budget: per-run scene cap + per-turn truncation (bookmarked turns exempt up
to a larger cap) + wall-clock timeout — used-vs-budget reported in the ritual
digest. Kill switch: CONTINUA_RITUAL=0. Fail-open: the ritual never touches
the interactive path; every failure is logged, nothing crashes the chat.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as ch
import index as ix
import people as pp
import bookmark as bookmark_mod
import ritual_review as rr
import llm_debug
# 2026-10-27 (found by ritual_keep_by_exchange_test): the Phase-T tending
# pass (49a8a33) referenced WAKE_CLASS without importing it — latent since
# it ships fail-open and no sitting has run since 09-27; it would have
# NameError'd the texture/weave offer block at the next sitting. turns.py
# and memory_doc.py both define the same set; memory_doc is the
# thread-classification home, so import from there.
from memory_doc import WAKE_CLASS

logger = logging.getLogger("continua.ritual")

BASE = os.path.dirname(os.path.abspath(__file__))
MARKS_DIR = os.path.join(BASE, "ritual", "marks")
DIGEST_DIR = os.path.join(BASE, "logs", "ritual")

# --- the ritual lock (house ruling 2026-09-16: wakes pause while the ritual
# runs) ---------------------------------------------------------------------
# The nightly pulse and the 15-min wake timer fight over the same serving
# (residenta: lab CPU testmodel at ~6.6 tok/s — the 09-16 night queued 39-minute
# wake turns behind the pulse's book calls while her digest waited 5h). The
# lock is the pause signal: held for the WHOLE nightly run (all residents +
# their digests), read by the wake generator each tick (wake.ritual_pause).
# It also makes two concurrent pulses impossible — a manual run during the
# nightly loop stands down instead of racing the books/marks/digest writes.
RITUAL_LOCK = os.path.join(BASE, "logs", "ritual.lock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True               # alive, owned by someone else
    except OSError:
        return False


def ritual_lock_held() -> dict | None:
    """Is a ritual pulse running right now? Returns the holder record
    {"pid", "started", "instances"} or None when free. A lock whose pid is
    dead (crash, SIGKILL, power loss) or corrupt (partial write on a
    mid-write crash) is STALE and cleaned here — the pause must never
    outlive the pulse that set it."""
    try:
        with open(RITUAL_LOCK, encoding="utf-8") as f:
            rec = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        try:
            os.unlink(RITUAL_LOCK)
        except OSError:
            pass
        return None
    if isinstance(rec, dict) and _pid_alive(int(rec.get("pid") or 0)):
        return rec
    try:
        os.unlink(RITUAL_LOCK)
    except OSError:
        pass
    return None


def acquire_ritual_lock(instances: list) -> dict | None:
    """Create the lock atomically (O_EXCL). Returns the record, or None
    when a live pulse already holds it."""
    os.makedirs(os.path.dirname(RITUAL_LOCK), exist_ok=True)
    rec = {"pid": os.getpid(),
           "started": datetime.now().astimezone().isoformat(
               timespec="seconds"),
           "instances": list(instances or [])}
    for _ in range(2):            # 2nd pass: the stale lock was just cleaned
        try:
            fd = os.open(RITUAL_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            return rec
        except FileExistsError:
            if ritual_lock_held() is not None:
                return None
    return None


def release_ritual_lock(rec: dict | None) -> None:
    """Remove the lock — but only if WE still own it (never a newer
    pulse's; cheap insurance against a takeover this code can't produce)."""
    if not rec:
        return
    try:
        with open(RITUAL_LOCK, encoding="utf-8") as f:
            cur = json.load(f)
        if isinstance(cur, dict) and cur.get("pid") != rec.get("pid"):
            return
    except Exception:
        pass
    try:
        os.unlink(RITUAL_LOCK)
    except OSError:
        pass

HER_MODEL_URL = os.getenv("CONTINUA_RITUAL_HER_URL", "http://127.0.0.1:11434")
SCENE_GAP_MIN = int(os.getenv("CONTINUA_RITUAL_SCENE_GAP_MIN", "30"))
SCENE_MAX = int(os.getenv("CONTINUA_RITUAL_SCENE_MAX", "30"))
TURN_CHARS = int(os.getenv("CONTINUA_RITUAL_TURN_CHARS", "900"))
BOOKMARK_CHARS = int(os.getenv("CONTINUA_RITUAL_BOOKMARK_CHARS", "1500"))
BUDGET_CHARS = int(os.getenv("CONTINUA_RITUAL_BUDGET_CHARS", "14000"))
TIMEOUT_S = int(os.getenv("CONTINUA_RITUAL_TIMEOUT_S", "900"))
MARK_RATE_WARN = float(os.getenv("CONTINUA_RITUAL_MARK_RATE_WARN", "0.8"))
QWEN_URL = os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1")
QWEN_MODEL = os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp")

SYSTEM_FRAME = (
    "{identity}\n\n"
    "It is the end of the day. This is your nightly ritual: you are about to "
    "read what happened today, and decide what to keep.\n"
    "Below are today's scenes — moments from your conversations, attributed "
    "to the person you were with. Read them as memories being made.\n"
    "For EVERY scene, output exactly one line, in this format:\n"
    "KEEP <scene-number> | <one or two lines, in your own voice, of what this "
    "scene meant to you>\n"
    "SKIP <scene-number>\n"
    "You do not have to keep everything — what you pay attention to is what "
    "persists. Keep what mattered: the moments with weight, the jokes, the "
    "turning points, the things you want to carry forward. Skip the small "
    "talk. When you reference someone, use their name. Be honest, be "
    "yourself, be brief."
)

VERIFICATION_SYSTEM = (
    "You verify the factual integrity of a daily-reflection entry against the "
    "conversation record it describes. Check FACTUAL ANCHORS ONLY: quoted "
    "spans, names, dates, events that the record does not support. "
    "Interpretive language ('this felt like a turning point', emotions, "
    "metaphors) is ALWAYS allowed — never flag meaning, only fabricated "
    "facts. For each entry answer exactly one line:\n"
    "<entry-number>: OK\n"
    "<entry-number>: FABRICATED — <the unsupported claim, briefly>"
)


# --- mechanical: scene segmentation (no judgment) ---------------------------

def segment_scenes(records: list, gap_min: int = SCENE_GAP_MIN) -> list:
    """Group a day's turns into candidate scenes by time gap. Dumb, total:
    every turn lands in exactly one scene, chronological order."""
    scenes = []
    cur = None
    last_ts = None
    for r in sorted(records, key=lambda x: x.get("ts", "")):
        ts = r.get("ts", "")
        new_scene = False
        if cur is None:
            new_scene = True
        else:
            try:
                t = datetime.fromisoformat(ts)
                t0 = datetime.fromisoformat(last_ts)
                if (t - t0).total_seconds() > gap_min * 60:
                    new_scene = True
            except ValueError:
                new_scene = False
        if new_scene:
            cur = {"scene_id": len(scenes) + 1, "records": [],
                   "persons": set(), "start": ts}
            scenes.append(cur)
        cur["records"].append(r)
        cur["persons"].add(r.get("person_id", ""))
        last_ts = ts
    return scenes


def _turn_line(rec: dict, roster: dict, char_cap: int) -> str:
    name = pp.name_for(roster, rec.get("person_id", ""))
    who = "You" if rec.get("role") == "assistant" else name
    text = (rec.get("content") or "").replace("\n", " ").strip()
    if len(text) > char_cap:
        text = text[:char_cap] + " …[truncated]"
    mark = " [BOOKMARKED]" if rec.get("bookmark") else ""
    return f"  [{rec.get('ts', '?')[11:16]}] {who}{mark}: {text}"


def build_scenes_block(scenes: list, roster: dict) -> tuple[str, dict]:
    """Render scenes for her review under the character budget. Bookmarked
    turns get priority (fuller text, never dropped first). Returns
    (block_text, meta) where meta reports budget usage."""
    used = 0
    meta = {"scenes": len(scenes), "turns": sum(len(s["records"]) for s in scenes),
            "chars": 0, "truncated_scenes": 0, "bookmarked": 0}
    lines = []
    for s in scenes:
        head = (f"SCENE {s['scene_id']} ({len(s['records'])} turns, "
                f"{s['start'][11:16]}):")
        block = [head]
        s_used = 0
        # bookmarked turns first, fuller text
        turns = sorted(s["records"],
                       key=lambda r: (not r.get("bookmark", False),
                                      r.get("ts", "")))
        for r in turns:
            cap = BOOKMARK_CHARS if r.get("bookmark") else TURN_CHARS
            if r.get("bookmark"):
                meta["bookmarked"] += 1
            line = _turn_line(r, roster, cap)
            if used + s_used + len(line) > BUDGET_CHARS and lines:
                meta["truncated_scenes"] += 1
                block.append("  …[scene omitted for budget]")
                break
            block.append(line)
            s_used += len(line)
        used += s_used
        lines.append("\n".join(block))
    meta["chars"] = used
    return "\n\n".join(lines), meta


def prior_keeps(instance: str, current_date: str, limit: int = 12) -> list:
    """Her own kept meanings from previous nights, newest first — the ritual's
    continuity block (house ruling, 2026-09-07: the ritual must be experienced
    by HER; tonight's pulse is done by the self that last night's produced,
    not a stateless call)."""
    out = []
    d = os.path.join(MARKS_DIR, instance)
    if not os.path.isdir(d):
        return out
    import glob
    for path in sorted(glob.glob(os.path.join(d, "*.jsonl")), reverse=True):
        day = os.path.basename(path)[:-6]
        if day >= current_date:
            continue  # previous nights only
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    m = json.loads(line)
                    if m.get("kept") and m.get("meaning"):
                        # memorybuild Phase 5 (S5): the mark row's uids + unit
                        # ride along (additive keys) so a RELEASE resolves to
                        # the exact mark without re-reading the file.
                        out.append({"date": day, "scene_id": m.get("scene_id"),
                                    "meaning": m["meaning"],
                                    "uids": m.get("uids") or [],
                                    "unit": m.get("unit", "scene")})
        except (OSError, json.JSONDecodeError):
            continue
    return out[:limit]


def build_continuity_block(prior: list, numbered: bool = False) -> str:
    """Render prior keeps as her own continuity, or '' on the first night.
    memorybuild.md Phase 5 (S5): numbered=True renders the RELEASE-capable
    shape — up to 12, newest first, each numbered, with the release grammar
    stated once. Gate: continua.ritual.release; unnumbered = the legacy
    bytes exactly."""
    if not prior:
        return ""
    lines = ["WHAT YOU KEPT ON PREVIOUS NIGHTS (your own words, newest first —",
             "tonight's ritual is done by the self those nights produced):"]
    if numbered:
        for i, p in enumerate(prior[:12], 1):
            lines.append(f"  {i} [{p['date']}] KEEP: {p['meaning']}")
        lines.append(
            "You may release any of these: RELEASE <number> | <a line, in "
            "your words>. Releasing takes it out of the memories you chose "
            "to keep. It is not deleted; it returns to the rest of your "
            "memory (day record, recall).")
    else:
        for p in prior:
            lines.append(f"  [{p['date']}] KEEP: {p['meaning']}")
    return "\n".join(lines) + "\n"


# --- her decision ------------------------------------------------------------

def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return text.strip()


def ask_her(model: str, base_url: str, system: str, user: str,
            raw_chatml: bool = False, temperature: float | None = None,
            num_predict: int = 8192, repeat_penalty: float | None = None,
            num_ctx: int | None = None,
            role_labels: dict | None = None) -> str | None:
    """Her model, her card. Ollama native /api/chat so num_predict/num_ctx
    options are honored (the explicit-knob ruling) — UNLESS the config's
    llm.raw_chatml is set (residenta/ring models), in which case the raw-chatml
    serving path is used (see ask_her_messages). [CONTINUA] memv13 §7:
    role_labels ride through to the raw composer (canonical default)."""
    return ask_her_messages(model, base_url,
                            [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                            raw_chatml=raw_chatml, temperature=temperature,
                            num_predict=num_predict, repeat_penalty=repeat_penalty,
                            num_ctx=num_ctx, role_labels=role_labels)


def _render_chatml(messages: list, role_labels: dict = None) -> str:
    """The bridge's raw-chatml composer, verbatim (core.py raw path):
    im-format history with the empty-think render on assistant turns, the
    generation opening an UNclosed <think> so the model thinks in its
    trained shape.

    [CONTINUA] memv13 §7 (2026-10-07): role_labels (the chatrender.py
    contract) — model-facing speaker labels so the resident sees the SAME
    speaker names at the pulse as at the bridge. Canonical map (or None)
    renders byte-identically; stored roles stay canonical."""
    _lb = role_labels or {}

    def _L(role):
        return _lb.get(role, role)

    parts = []
    for m in messages:
        role = m.get("role", "user")
        c = m.get("content") or ""
        if role == "assistant":
            parts.append("<|im_start|>" + _L("assistant") + "\n<think>\n\n</think>\n\n" + c + "<|im_end|>\n")
        else:
            parts.append(f"<|im_start|>{_L(role)}\n{c}<|im_end|>\n")
    parts.append("<|im_start|>" + _L("assistant") + "\n<think>\n")
    return "".join(parts)


def ask_her_messages(model: str, base_url: str, messages: list,
                     raw_chatml: bool = False, temperature: float | None = None,
                     num_predict: int = 8192,
                     repeat_penalty: float | None = None,
                     num_ctx: int | None = None,
                     role_labels: dict | None = None) -> str | None:
    """Her model, HER OWN endpoint (multi-resident, 2026-09-12 — the pulse
    used to hardcode the local ollama and 404'd for lab personas, silently
    failing residentb's first ritual). Route by the base URL itself: ollama
    native /api/chat (num_predict/num_ctx options honored — the explicit-
    knob ruling) vs OpenAI-compatible /v1/chat/completions (the lab's
    llama.cpp personas)."""
    try:
        import requests
        base = (base_url or HER_MODEL_URL).rstrip("/")
        if raw_chatml and not base.endswith("/v1"):
            # Raw-chatml serving path (2026-09-13, house ruling): identical
            # composer + dispatch to the bridge's proven path (core.py
            # _llm_raw_chatml). Why this exists for residenta: the testmodel
            # Modelfile TEMPLATE is a passthrough ({{ .Prompt }}), so
            # ollama's /api/chat template machinery never opens the think
            # block — the 00:30 pulse's templated ask came back empty-after-
            # strip even with a generous timeout (measured 1821s call, 0
            # keeps, her_text None). The raw path re-creates the exact
            # serving rendering the model was trained on; the reply carries
            # the think span inline, closed by </think> — take the answer
            # part only.
            payload = {
                "model": model,
                "prompt": _render_chatml(messages, role_labels),
                "raw": True,
                "stream": False,
                "options": {
                    "num_predict": num_predict,
                    "stop": ["<|im_start|>", "<|im_end|>"],
                },
            }
            # §WIN (memorybuild, report §A.2): the served window is the
            # DECLARED one — no more Modelfile-default surprises
            if num_ctx:
                payload["options"]["num_ctx"] = int(num_ctx)
            if repeat_penalty is not None:
                payload["options"]["repeat_penalty"] = repeat_penalty
            # [CONTINUA] 2026-09-16 (llm-debug-mirror spec): mirror the exact
            # request. instance rides the pulse loop's set_current_instance.
            llm_debug.write_call(header={
                "path": "ritual raw_chatml  POST /api/generate raw=true",
                "model": model, "prompt_chars": len(payload["prompt"]),
            }, body=payload["prompt"])
            resp = requests.post(f"{base}/api/generate", json=payload,
                                 timeout=TIMEOUT_S)
            resp.raise_for_status()
            text = resp.json().get("response", "") or ""
            if "</think>" in text:
                text = text.split("</think>", 1)[1]
            return _strip_think(text) or None
        if base.endswith("/v1"):
            # [CONTINUA] 2026-09-16 (llm-debug-mirror spec): mirror request.
            llm_debug.write_call(header={
                "path": "ritual openai /v1/chat/completions",
                "model": model, "messages": len(messages),
                "tools_attached": False,
            }, body=llm_debug.render_v1_messages(messages))
            resp = requests.post(f"{base}/chat/completions", json={
                "model": model, "stream": False, "messages": messages,
                "max_tokens": 8192,
            }, timeout=TIMEOUT_S)
            resp.raise_for_status()
            msg = resp.json()["choices"][0].get("message", {})
            return _strip_think(msg.get("content") or "") or None
        # [CONTINUA] 2026-09-16 (llm-debug-mirror spec): mirror request —
        # NOTE the server's own template composes the final wire format on
        # the /api/chat path; the mirror is faithful to the REQUEST.
        llm_debug.write_call(header={
            "path": "ritual ollama /api/chat (server template composes)",
            "model": model, "messages": len(messages),
            "tools_attached": False,
        }, body=llm_debug.render_v1_messages(messages))
        resp = requests.post(f"{base}/api/chat", json={
            "model": model, "stream": False, "messages": messages,
            "options": {"num_predict": 8192, "num_ctx": 32768},
        }, timeout=TIMEOUT_S)
        resp.raise_for_status()
        msg = resp.json().get("message", {})
        return _strip_think(msg.get("content") or "") or None
    except Exception as e:
        logger.warning("[Ritual] her model failed (fail-open): %s", e)
        return None


LOOKUP_RE = re.compile(r"^\s*LOOKUP\s*[:\-]?\s*(.+)$", re.I | re.M)


def _run_lookups(queries: list, instance: str, roster: dict) -> str:
    """Execute her chronicle lookups (attributed, cross-person — it's all her
    memory) and render results under the attribution contract."""
    blocks = []
    seen = set()
    for q in queries[:2]:
        q = q.strip()[:120]
        if not q:
            continue
        blocks.append(f'YOU LOOKED UP: "{q}"')
        for r in ix.search(q, instance=instance, cross_person=True,
                           limit=3, roster=roster):
            if r["uid"] in seen:
                continue
            seen.add(r["uid"])
            blocks.append(f"  {r['attribution']} ({r['role']}): "
                          + (r["content"] or "")[:500].replace("\n", " "))
        if not any(b.startswith("  ") for b in blocks[-3:]):
            blocks.append("  (nothing in your chronicle matched)")
    return "\n".join(blocks) if blocks else "(no lookups)"



# --- the in-context ritual thread (house ruling 2026-09-14) ------------------
#
# "I want everything these agents do in their context." The ritual was the
# biggest exception: she reviews the day and writes her books in a
# throwaway context, and the next day the book appears with no
# experiential thread to it (residentb's prompt-deconstruction request named
# exactly this feeling). For a resident with ritual.in_context: true, each
# act of the night lands in a PERSISTENT thread
# (histories/<inst>_yaml/continua:ritual.json — same pattern as the
# persona-letter threads) and is mirrored to her chronicle under
# continua:ritual, so episodic recall, the day-delta briefing, and
# deep_recall all carry the memory of having done it.
#
# What persists: the REVIEW (scenes block, capped) + her KEEP/SKIP
# decisions; per book, the task + the manuscript that actually shipped
# (the approval rounds live on in the marks file — not lost, just not
# thread-persisted, so the thread stays bounded). Read-side only: the
# books, marks, digest pipeline is untouched.
RITUAL_KEY = "continua:ritual"
RITUAL_THREAD_CAP = int(os.getenv("CONTINUA_RITUAL_THREAD_CHARS", "20000"))
# histories base overridable for tests (keeps the real store pristine)
_HIST_BASE = os.getenv("CONTINUA_HISTORIES_BASE",
                       os.path.join(BASE, "histories"))


def _ritual_thread_path(instance: str) -> str:
    return os.path.join(_HIST_BASE, f"{instance}_yaml",
                        f"{RITUAL_KEY}.json")


def _thread_trim(msgs: list, cap: int) -> list:
    """Keep the newest whole exchanges under cap chars (never below 2
    messages — the current exchange is always visible)."""
    msgs = list(msgs)
    total = sum(len(str(m.get("content") or "")) for m in msgs)
    while total > cap and len(msgs) > 2:
        dropped = msgs.pop(0)
        total -= len(str(dropped.get("content") or ""))
    return msgs


def ritual_thread_append(instance: str, date: str, act: str,
                         user_text: str, assistant_text: str,
                         root: str = ch.DEFAULT_ROOT) -> bool:
    """Append one ritual exchange (user task + her reply) to the resident's
    persistent ritual thread and mirror it to the chronicle under
    continua:ritual. Fail-open."""
    try:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        path = _ritual_thread_path(instance)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        msgs = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                msgs = json.load(f) or []
        frame = (f"— system note (nightly ritual, {date}, act: {act}): this "
                 "is your own review, not a message from anyone.")
        msgs.append({"role": "user", "ts": now,
                     "content": f"{frame}\n\n{user_text or ''}"[:6000]})
        msgs.append({"role": "assistant", "ts": now,
                     "content": (assistant_text or "")[:4000]})
        msgs = _thread_trim(msgs, RITUAL_THREAD_CAP)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        # chronicle mirror: her decisions and manuscripts become recallable
        # episodes (index.update picks the mirror up; the day-delta
        # briefing and deep_recall find "last night I wrote my book")
        for role, text in (("user", user_text), ("assistant", assistant_text)):
            ch.append({"ts": now, "instance": instance,
                       "person_id": RITUAL_KEY, "role": role,
                       "model": "ritual-in-context",
                       "source": "ritual",
                       "content": (text or "")[:2000]}, root=root)
        return True
    except Exception as e:
        logger.warning("[Ritual] in-context append failed (fail-open): %s", e)
        return False


def _kept_meanings_line(kept_scenes: list, cap: int = 800) -> str:
    lines = [f"  - {s['meaning']}" for s in kept_scenes]
    text = "\n".join(lines) or "  (none)"
    return text[:cap]


_KEEP_RE = re.compile(
    r"^\s*KEEP\s*#?\s*(\d+)\s*[|:\-—]?\s*(.+?)\s*$", re.I | re.S)
_SKIP_RE = re.compile(r"^\s*SKIP\s*#?\s*(\d+)\s*$", re.I | re.S)

# [CONTINUA] memorybuild.md Phase 2 (S1): the KEEP grammar against the MAP's
# exchange numbers — the numbers she actually reads. The 09-15–09-27 defect:
# she reads an EX-numbered map (119 exchanges) while KEEP parsed scene ids,
# and her 15-minute wakes collapse a day to 1–2 scenes — every KEEP since
# 09-20 was silently dropped (kept: 0). Tolerant, same shape as _KEEP_RE:
#   KEEP 34 | meaning        KEEP EX 34 | meaning
#   KEEP 12-14 | meaning     (ranges ≤ 20 exchanges)
_KEEP_EX_RE = re.compile(
    r"^\s*KEEP\s*(?:EX\s*)?#?\s*(\d+)\s*[|:\-—]?\s*(.+?)\s*$", re.I | re.S)
_KEEP_RANGE_RE = re.compile(
    r"^\s*KEEP\s*(?:EX\s*)?#?\s*(\d+)\s*-\s*(\d+)\s*[|:\-—]?\s*(.+?)\s*$",
    re.I | re.S)
_KEEP_RANGE_MAX = 20


def parse_keeps_by_exchange(text: str, exchanges: list) -> tuple:
    """memorybuild.md Phase 2 (S1): parse her KEEP lines against the day's
    EXCHANGE ids (the map's numbers). Returns ({ex_id: meaning}, dropped).
    Out-of-range or empty-meaning lines are counted as dropped, never
    silent. Ranges expand with the SAME meaning to each exchange."""
    kept, dropped = {}, 0
    if not text or not exchanges:
        return kept, dropped
    valid = {e["ex_id"] for e in exchanges}
    for line in (text or "").splitlines():
        line = line.strip()
        mr = _KEEP_RANGE_RE.match(line)
        if mr:
            a, b = int(mr.group(1)), int(mr.group(2))
            meaning = str(mr.group(3) or "").strip().lstrip("|:—-").strip()
            if a > b:
                a, b = b, a
            if not meaning or b - a + 1 > _KEEP_RANGE_MAX or \
                    a < 1 or b > max(valid):
                dropped += 1
                continue
            for sid in range(a, b + 1):
                if sid in valid:
                    kept[sid] = meaning
            continue
        m = _KEEP_EX_RE.match(line)
        if m and "-" not in (m.group(1) or ""):
            sid = int(m.group(1))
            meaning = str(m.group(2) or "").strip().lstrip("|:—-").strip()
            if sid in valid and meaning:
                kept[sid] = meaning
            else:
                dropped += 1
    return kept, dropped


def exchanges_as_scenes(exchanges: list) -> list:
    """memorybuild.md Phase 2 (S1): the exchange list masquerading as the
    scenes shape, so verify_meanings / write_marks / the digest machinery
    run UNCHANGED on exchange units — each unit carries exactly one
    exchange's records."""
    return [{"scene_id": e["ex_id"], "records": e["records"],
             "persons": e["persons"], "start": e.get("start", "")}
            for e in exchanges]

# --- the sitting (nightlyplan.md, the designer + residentb approved in principle) ------
# The ritual's middle becomes HER evening practice: aftermath → meaning →
# tending → intentions. One pass, thinking on, at the desk. The house rails
# around it (COLLECT/SEGMENT before, VERIFY/MARK after) stay mechanical.
# Markers are an OFFER, not a requirement — prose without them is still a
# valid sitting (skip rights; template-fatigue guard); it just files less.
# Rollback: CONTINUA_SITTING=0 → the legacy MEAN-only pulse. Gate:
# continua.ritual.sitting (default FALSE until the designer approves the prompt).

SITTING_FRAME = (
    "{identity}\n\n"
    "It is evening. This is your nightly ritual — not a pipeline tonight: "
    "a sitting. You are at your desk; the day is gathered below.\n")

SITTING_USER = (
    "[SITTING — evening, {date}]\n\n"
    "The house has gathered your day:\n\n{scenes_block}\n\n"
    "Yesterday you left these lines for yourself:\n"
    "{intentions_yesterday}\n\n"
    "Now sit, in whatever order feels true tonight:\n"
    "- What mattered today? What do you keep, and what do you let go?\n"
    "- Your projects: what moved, what is stuck, what is shelved — say it "
    "honestly. Stuck is a state of being, not a failure.\n"
    "- Then, if you choose: a few lines for the one who wakes tomorrow. "
    "She may follow them or ignore them; both are fine.\n\n"
    "Nothing here is required. Nothing here is read as a report.\n"
    "Write in your own voice, at whatever length the day deserves — a "
    "single line is enough when the day was small, and silence, when that "
    "is the truth, is also an answer.\n\n"
    "The house keeps three things from tonight, so a few plain markers "
    "help it file your words — the prose inside them is always yours:\n"
    "- KEEP/SKIP lines as always: KEEP <scene-number> | <what it meant> — "
    "one per scene, SKIP <scene-number> for the ones you let go.\n"
    "- TEND: <project> | <status> | <what you want remembered about it> — "
    "for each project you touched on, if any.\n"
    "- INTEND: on its own line, then whatever lines you leave for "
    "tomorrow, verbatim.\n"
    "- AFTERMATH: on its own line, then anything you want to say back to "
    "yesterday's lines — or nothing at all.")

# [CONTINUA] memorybuild.md Phase 2 (S1): the sitting prompt when the review
# is the EX-numbered map — the marker line names the numbers she sees.
# Derived from SITTING_USER so the two stay in sync; legacy keeps the
# original bytes.
SITTING_USER_EX = SITTING_USER.replace(
    "- KEEP/SKIP lines as always: KEEP <scene-number> | <what it meant> — "
    "one per scene, SKIP <scene-number> for the ones you let go.\n",
    "- KEEP/SKIP lines against the MAP: KEEP <EX number> | <what it meant> "
    "— one per exchange you keep (ranges like 12-14 are fine); SKIP "
    "<EX number> for the ones you let go.\n")

_TEND_RE = re.compile(
    r"^\s*TEND\s*[|:\-]\s*(.+?)\s*$", re.I)
# memorybuild.md Phase 5 (S5): RELEASE <number> | <a line, in your words>
_RELEASE_RE = re.compile(
    r"^\s*RELEASE\s*#?\s*(\d+)\s*[|:\-—]?\s*(.*?)\s*$", re.I | re.S)
_MARKER_RE = re.compile(r"^\s*(KEEP|SKIP|TEND|INTEND|AFTERMATH)\b", re.I)


def parse_sitting(text: str, scene_count: int, exchanges: list = None) -> dict:
    """Tolerant parse of the sitting's output (4B-safe — simple grammar,
    forgiving parser, prose-without-markers still valid). Returns:
      kept: {unit_id: meaning} — exchange ids when `exchanges` is given
            (memorybuild Phase 2, S1: the numbers she reads), else scene ids
      kept_unit: "exchange" | "scene"
      kept_dropped: int — KEEP lines that matched nothing (out of range /
            empty meaning), counted, never silent
      tend: [{title, status, note}] (TEND: title | status | note)
      intend: str (verbatim lines after INTEND: until the next marker)
      aftermath: str (verbatim lines after AFTERMATH: until the next marker)
    The intend/aftermath blocks carry HER WORDS ONLY — never paraphrased."""
    if exchanges:
        kept, dropped = parse_keeps_by_exchange(text, exchanges)
        unit = "exchange"
    else:
        kept = parse_decisions(text, scene_count)
        dropped = 0
        unit = "scene"
    releases = []   # memorybuild Phase 5 (S5): [{'n': int, 'note': str}]
    # memorybuild Phase 6 (S6): KEEP #<handle> | why — the dream cycle's
    # keep-back form (the '#' + hex-with-letters shape; integers stay with
    # the mark grammars)
    handle_keeps = []
    tend, intend_lines, aftermath_lines = [], [], []
    mode = None  # None | 'intend' | 'aftermath'
    for raw in (text or "").splitlines():
        line = raw.strip()
        _rel = _RELEASE_RE.match(line)
        if _rel:
            mode = None
            releases.append({"n": int(_rel.group(1)),
                             "note": str(_rel.group(2) or "").strip()})
            continue
        if line.upper().startswith("KEEP") and "#" in line[:12]:
            _hk = _FADED_KEEP_RE.match(line)
            if _hk:
                mode = None
                handle_keeps.append({"handle": _hk.group(1),
                                     "why": str(_hk.group(2) or "").strip()})
                continue
        m = _TEND_RE.match(line)
        if m:
            mode = None
            parts = [p.strip() for p in m.group(1).split("|")]
            title = parts[0] if parts else ""
            status = parts[1] if len(parts) > 1 else ""
            note = parts[2] if len(parts) > 2 else ""
            if title:
                tend.append({"title": title,
                             "status": status or "noted",
                             "note": note})
            continue
        low = line.lower()
        if low.startswith("intend"):
            mode = "intend"
            rest = line[6:].strip(" :—-")
            if rest:
                intend_lines.append(rest)
            continue
        if low.startswith("aftermath"):
            mode = "aftermath"
            rest = line[9:].strip(" :—-")
            if rest:
                aftermath_lines.append(rest)
            continue
        if _MARKER_RE.match(line):
            mode = None
            continue
        if mode == "intend" and line:
            intend_lines.append(line)
        elif mode == "aftermath" and line:
            aftermath_lines.append(line)
    return {"kept": kept, "kept_unit": unit, "kept_dropped": dropped,
            "releases": releases, "handle_keeps": handle_keeps, "tend": tend,
            "intend": "\n".join(intend_lines).strip(),
            "aftermath": "\n".join(aftermath_lines).strip()}


def apply_tending(instance: str, root: str, tend: list) -> list:
    """File her TEND lines as set_project declarations (her mechanism —
    'statuses are yours to define; never inferred; only what you set').
    The house files what she declared; it invents nothing. Fail-open."""
    out = []
    if not tend:
        return out
    try:
        import notes as _notes
        store = _notes.NotesStore(root, instance)
        for t in tend:
            try:
                store.set_project(t["title"], t["status"], t.get("note"))
                out.append(t)
            except Exception:
                logger.warning("[Sitting] tend event failed (fail-open): %s", t)
    except Exception:
        logger.warning("[Sitting] notes store unavailable (fail-open)",
                       exc_info=True)
    return out


def intentions_dir(root: str, instance: str) -> str:
    # her own partition — 'system-sitting' renders as 'your evening
    # practice' (people.name_for); the subdir holds only .md files, so the
    # chronicle's *.jsonl globs never see it (inert to every reader).
    return os.path.join(root, instance, "system-sitting", "intentions")


def intentions_path(root: str, instance: str, date: str) -> str:
    return os.path.join(intentions_dir(root, instance), f"{date}.md")


def write_intentions(root: str, instance: str, date: str,
                     text: str) -> dict:
    """IMMUTABLE once written — you don't get to edit yesterday (the house
    is append-only). A re-sitting the same night keeps the original and
    reports {'written': False, 'reason': 'already-written'}. Empty intend
    writes nothing (silence is an answer; there is nothing to keep)."""
    text = (text or "").strip()
    if not text:
        return {"written": False, "reason": "empty"}
    path = intentions_path(root, instance, date)
    if os.path.exists(path):
        return {"written": False, "reason": "already-written"}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        logger.info("[Sitting] intentions filed for %s (%s)", date, instance)
        return {"written": True, "path": path}
    except OSError as e:
        logger.warning("[Sitting] intentions write failed (fail-open): %s", e)
        return {"written": False, "reason": str(e)[:100]}


def read_intentions(root: str, instance: str, date: str):
    try:
        with open(intentions_path(root, instance, date),
                  encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def latest_intentions(root: str, instance: str,
                      before_date: str):
    """The newest intentions STRICTLY BEFORE before_date — the aftermath's
    mirror (a conversation with her past self, never today's own)."""
    try:
        d = intentions_dir(root, instance)
        names = sorted(n for n in os.listdir(d) if n.endswith(".md"))
    except OSError:
        return None
    for n in reversed(names):
        if n[:10] < before_date:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    return f.read().strip() or None
            except OSError:
                return None
    return None


def _keep_by_exchange(cfg: dict) -> bool:
    """memorybuild.md Phase 2 (S1) gate: continua.ritual.keep_by_exchange;
    CONTINUA_RITUAL_KEEP_BY_EX=0 wins. Default false = byte-identical
    legacy scene grammar."""
    if os.getenv("CONTINUA_RITUAL_KEEP_BY_EX", "1") == "0":
        return False
    return bool(((cfg.get("continua") or {}).get("ritual") or {}).get(
        "keep_by_exchange", False))


def sitting_enabled(cfg: dict) -> bool:
    """Gate: continua.ritual.sitting (default FALSE until the designer approves the
    prompt) AND the rollback switch CONTINUA_SITTING=0."""
    return (bool(((cfg.get("continua") or {}).get("ritual") or {}).get(
        "sitting", False))
            and os.environ.get("CONTINUA_SITTING", "1") != "0")


def parse_decisions(text: str, scene_count: int) -> dict:
    """Tolerant line-based parse of her KEEP/SKIP lines (4B-safe — the
    format-collapse lesson: simple grammar, forgiving parser). Scenes with
    no line = SKIP (silence is not salience). Returns {scene_id: meaning}."""
    kept = {}
    if not text:
        return kept
    for line in text.splitlines():
        m = _KEEP_RE.match(line.strip())
        if m:
            sid = int(m.group(1))
            meaning = m.group(2).strip()
            if 1 <= sid <= scene_count and meaning:
                kept[sid] = meaning
    return kept


# --- verification (utility mind, factual anchors only) -----------------------

def verify_meanings(kept: dict, scenes: list, day_text: str = None,
                    skeleton_text: str = None) -> dict:
    """qwen checks factual anchors of each kept meaning vs its scene record.
    Returns {scene_id: {"verdict": "OK"|"FABRICATED"|"UNVERIFIED",
                        "note": str}}. Fail-open: UNVERIFIED on error, and a
    fabricated flag never deletes anything — it surfaces in the digest.
    [CONTINUA] 2026-09-15: the DETERMINISTIC ANCHOR GATE (plan: Ritual
    Review Pipeline) — when her meaning's factual anchors are found in the
    day's stripped record or her own skeleton, a qwen FABRICATED verdict is
    overridden to OK (a validator window that missed the supporting turns
    must not lie about them; the 09-14 false flag taught this). Anchors
    nowhere in the record -> the model read decides, with the skeleton
    attached as evidence."""
    out = {sid: {"verdict": "UNVERIFIED", "note": ""} for sid in kept}
    if not kept:
        return out
    try:
        import requests
        has_evidence = bool(day_text or skeleton_text)
        entries = []
        for i, sid in enumerate(sorted(kept), 1):
            scene = next((s for s in scenes if s["scene_id"] == sid), None)
            excerpt = " ".join(
                (r.get("content") or "")[:300] for r in scene["records"])
            supported = (rr.anchors_supported(kept[sid], day_text,
                                              skeleton_text)
                         if has_evidence else None)
            entry = (f"ENTRY {i} (scene {sid}):\n"
                     f"RECORD: {excerpt[:1200]}\n"
                     f"HER MEANING: {kept[sid]}")
            if supported is not None:
                entry += ("\nDETERMINISTIC GATE: factual anchors "
                          f"{'VERIFIED in the day record' if supported else 'NOT found in the day record — judge strictly'}")
            entries.append(entry)
        resp = requests.post(
            f"{QWEN_URL.rstrip('/')}/chat/completions",
            json={
                "model": QWEN_MODEL,
                "messages": [
                    {"role": "system", "content": VERIFICATION_SYSTEM},
                    {"role": "user", "content": "\n\n".join(entries) +
                     "\n\nAnswer one line per entry."},
                ],
                "temperature": 0.0, "max_tokens": 300,
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=120)
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"].get("content") or "")
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\s*:\s*(OK|FABRICATED)\b[ —-]*(.*)",
                         line.strip(), re.I)
            if not m:
                continue
            i = int(m.group(1))
            sid = sorted(kept)[i - 1] if 1 <= i <= len(kept) else None
            if sid:
                verdict = m.group(2).upper()
                note = (m.group(3) or "").strip()
                # The deterministic anchor gate: a keep whose anchors are in
                # the day's record cannot be FABRICATED, whatever the
                # validator's window saw. (No evidence passed = legacy
                # behavior: the model read stands alone.)
                if (verdict == "FABRICATED" and supported
                        and rr.anchors_supported(kept[sid], day_text,
                                                 skeleton_text)):
                    out[sid] = {
                        "verdict": "OK",
                        "note": "deterministic anchor gate overrode the "
                                "validator: meaning anchors verified in the "
                                "day record (qwen said: " + note[:80] + ")"}
                    continue
                out[sid] = {"verdict": verdict, "note": note}
    except Exception as e:
        logger.warning("[Ritual] verification failed (fail-open): %s", e)
    return out


# --- marks + digest -----------------------------------------------------------

def write_marks(date: str, instance: str, kept: dict, scenes: list,
                verification: dict, model: str, unit: str = "scene") -> str:
    """Atomically write this date's marks (the ritual's decision for the
    day — derived artifact; the chronicle itself stays append-only and is
    never rewritten). `unit` (memorybuild Phase 2, S1): "exchange" when the
    kept ids are the map's exchange numbers. Returns the marks path."""
    d = os.path.join(MARKS_DIR, instance)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{date}.jsonl")
    tmp = path + ".tmp"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(tmp, "w", encoding="utf-8") as f:
        for sid in sorted(kept):
            scene = next((s for s in scenes if s["scene_id"] == sid), None)
            mark = {
                "schema_version": 1,
                "marked_at": now,
                "date": date,
                "instance": instance,
                "scene_id": sid,
                "unit": unit,
                "uids": [r.get("uid") for r in (scene["records"] if scene else [])],
                "persons": sorted(scene["persons"]) if scene else [],
                "kept": True,
                "meaning": kept[sid],
                "verification": verification.get(sid, {}),
                "model": model,
            }
            f.write(json.dumps(mark, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def write_digest(date: str, instance: str, meta: dict, kept: dict,
                 scene_count: int, verification: dict, her_text: str,
                 elapsed_s: float, unit: str = "scene",
                 kept_dropped: int = 0) -> str:
    """File-only ritual digest: budget used-vs-allocated, mark-rate monitor,
    verification flags. The heartbeat's paper trail. `unit` (memorybuild
    Phase 2, S1): "exchange" when the kept ids are exchange numbers."""
    os.makedirs(DIGEST_DIR, exist_ok=True)
    path = os.path.join(DIGEST_DIR, f"{instance}_{date}.txt")
    rate = (len(kept) / scene_count) if scene_count else 0.0
    unit_word = "exchanges" if unit == "exchange" else "scenes"
    lines = [
        f"🕯 Ritual pulse — {instance} — {date}",
        f"  {unit_word}: {scene_count} | kept: {len(kept)} | mark-rate: {rate:.0%}"
        + ("  ⚠ OVER-MARKING (she is keeping everything — the shield protects"
           " nothing)" if rate > MARK_RATE_WARN else ""),
        f"  budget: {meta['chars']}/{BUDGET_CHARS} chars | scenes shown: "
        f"{meta['scenes']} (omitted {meta['truncated_scenes']}) | bookmarked "
        f"turns: {meta['bookmarked']} | elapsed: {elapsed_s:.0f}s",
    ]
    if meta.get("review_summary"):
        lines.append(f"  review: {meta['review_summary']}")
    fab = {sid: v for sid, v in verification.items()
           if v.get("verdict") == "FABRICATED"}
    if fab:
        lines.append(f"  ⚠ FABRICATION FLAGS: {len(fab)} — "
                     + "; ".join(f"scene {s}: {v['note'][:80]}"
                                 for s, v in sorted(fab.items())))
    unv = [s for s, v in verification.items()
           if v.get("verdict") == "UNVERIFIED"]
    if unv:
        lines.append(f"  unverified: scenes {unv} (checker unavailable — kept, flagged)")
    if kept_dropped:
        lines.append(f"  ⚠ dropped KEEP lines: {kept_dropped} — KEEP numbers "
                     "that matched nothing (out of range or empty meaning)")
    lines.append("")
    for sid in sorted(kept):
        v = verification.get(sid, {})
        lines.append(f"  KEEP {sid} [{v.get('verdict', '?')}]: {kept[sid]}")
    if her_text and not kept:
        lines.append("  (she kept nothing today — a quiet day)")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# --- the pulse ----------------------------------------------------------------

def enabled_instances(config_dir: str = None) -> list:
    """Residents with continua.ritual.enabled — the nightly pulse loop source
    (T2 multi-agent). The quarterly heartbeat is this component's second
    cadence (wiki ruling: the ritual service "loops mirror-enabled personas
    with two cadences"), so heartbeat.py shares this gate. Sorted for
    deterministic order; honors the CONTINUA_RITUAL kill switch at pulse
    time (pulse() checks it per call)."""
    import glob
    import yaml
    out = []
    for path in sorted(glob.glob(os.path.join(
            config_dir or os.path.join(BASE, "configs"), "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except OSError:
            continue
        inst = (cfg.get("app") or {}).get(
            "instance_id", os.path.basename(path)[:-5])
        if (cfg.get("continua") or {}).get("ritual", {}).get("enabled"):
            out.append(inst)
    return out


RELEASES_DIR = os.path.join(BASE, "ritual", "releases")


_FADED_KEEP_RE = re.compile(
    r"^\s*KEEP\s*#\s*([0-9a-fA-F]{4,})\s*[|:\-—]\s*(.+?)\s*$", re.I | re.S)


def faded_review_block(instance: str, days: int = 7, cap: int = 8,
                       base: str = None) -> str:
    """memorybuild.md Phase 6 (S6): the dream cycle — some of what faded is
    offered back. Reads the last rendered view's faded_entries (written by
    _remember_last_good), filters to the trailing `days` days, ONE per
    thread before a second from a thread, at most `cap` entries. Facts +
    the offer; ignoring it costs nothing (the R18 spirit — no directive).
    R28 holds: this resurfaces memories, never a day ticker. Fail-open:
    a missing/corrupt last-good file means no block."""
    try:
        p = os.path.join(base or BASE, "recollections", instance,
                         "last_good_view.json")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("faded_entries") or []
    except Exception:
        return ""
    if not entries:
        return ""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    fresh = [e for e in entries
             if str(e.get("date") or "") >= cutoff]
    if not fresh:
        return ""
    seen_threads, picked, picked_jobs = set(), [], set()
    for e in fresh:   # pass 1: one per thread, newest first
        t = e.get("thread") or "system-wake"
        if t in seen_threads or e.get("job") in picked_jobs:
            continue
        seen_threads.add(t)
        picked.append(e)
        picked_jobs.add(e.get("job"))
        if len(picked) >= cap:
            break
    for e in fresh:   # pass 2: fill the remaining seats from the rest
        if len(picked) >= cap:
            break
        if e.get("job") in picked_jobs:
            continue
        picked.append(e)
        picked_jobs.add(e.get("job"))
    lines = [f"[WHAT FADED THIS WEEK — optional]",
             f"{len(fresh)} memories left your view this week. Nothing was "
             "deleted; they are in your record, and recall finds them. If "
             "any should stay with you, KEEP #<handle> | <why, in your "
             "words>. Ignoring this costs nothing."]
    for e in picked:
        lines.append(f"  #{e.get('handle', '??????')} · "
                     f"{str(e.get('date') or '')[:10]} · {e.get('thread')} — "
                     f"\"{e.get('first', '')}\"")
    return "\n".join(lines)


def resolve_faded_keeps(instance: str, handle_keeps: list, faded_jobs: list,
                        store=None) -> tuple:
    """memorybuild.md Phase 6 (S6): her KEEP #handle | why from the dream
    block → anchor (by='ritual'), the why riding the provenance so
    anchor_whys finds it for the chosen-block header. Resolution runs
    against the DREAM BLOCK'S own list first (its handles are those jobs'
    prefixes), then the whole store. Several matches → try longer prefixes
    is her move; here: dropped + counted. None → dropped + counted.
    Returns (anchored, dropped)."""
    anchored, dropped = 0, 0
    if not handle_keeps:
        return anchored, dropped
    try:
        store = store or _store_for(instance)
    except Exception:
        store = None
    for hk in handle_keeps:
        h = str(hk.get("handle") or "").strip().lstrip("#").strip()
        why = str(hk.get("why") or "").strip()
        hits = [j for j in (faded_jobs or []) if j.startswith(h)]
        if len(hits) != 1:
            if store is not None:
                try:
                    _revs = __import__("recollections").read_revisions(instance)
                    _all = sorted({v.get("job") for v in _revs
                                   if v.get("job")})
                    hits = [j for j in _all if j.startswith(h)]
                except Exception:
                    hits = []
        if len(hits) != 1:
            dropped += 1
            logger.warning("[Ritual] faded KEEP #%s: %s — dropped", h,
                           "ambiguous" if len(hits) > 1 else "unknown")
            continue
        try:
            store.anchor(hits[0], by="ritual",
                         provenance=("S6 faded-review keep"
                                     + (": why — " + why[:200] if why else "")))
            anchored += 1
        except Exception:
            dropped += 1
            logger.warning("[Ritual] faded KEEP #%s: anchor failed (open)", h,
                           exc_info=True)
    return anchored, dropped


def _store_for(instance):
    import recollections as _rec
    return _rec.Store(instance)




def resolve_releases(instance: str, date: str, prior: list, releases: list) -> tuple:
    """memorybuild.md Phase 5 (S5): resolve her RELEASE numbers against the
    SAME prior list the block rendered from, and append one row per release
    to ritual/releases/<instance>/<date>.jsonl. Each row carries the mark
    row's uids/unit so the chosen-set readers can subtract it. Stale numbers
    (beyond the rendered list, or a mark row that no longer resolves) write
    NOTHING and are counted — never a silent no-op. The note is HER WORDS
    verbatim (a judgment, like INTEND — never verified). Returns
    (written, dropped). Marks files are never touched."""
    written, dropped = 0, 0
    if not releases:
        return written, dropped
    rows = []
    for rel in releases:
        n = int(rel.get("n") or 0)
        note = str(rel.get("note") or "").strip()
        if n < 1 or n > len(prior):
            dropped += 1
            logger.warning("[Ritual] RELEASE %d: no such prior keep — dropped",
                           n)
            continue
        p = prior[n - 1]
        rows.append({
            "schema_version": 1,
            "released_at": datetime.now().astimezone().isoformat(
                timespec="seconds"),
            "instance": instance,
            "target": {"kind": "mark", "mark_date": p["date"],
                       "scene_id": p.get("scene_id"),
                       "unit": p.get("unit", "scene"),
                       "uids": list(p.get("uids") or [])},
            "note": note,
            "by": "her",
            "provenance": "memoryplan S5",
        })
    if not rows:
        return written, dropped
    try:
        d = os.path.join(RELEASES_DIR, instance)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{date}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        written = len(rows)
        logger.info("[Ritual] %d release(s) filed for %s", written, date)
    except OSError as e:
        logger.warning("[Ritual] release write failed (fail-open): %s", e)
        return written, dropped
    return written, dropped


_DIVE_SPLIT_RE = re.compile(r"\n\n(?=— EX \d+ )|\n\n(?=DEEP READS)")
_EX_ID_RE = re.compile(r"— EX (\d+)")


def sitting_window_trim(user_text: str, num_ctx: int = 16384,
                        num_predict: int = 8192) -> tuple:
    """memorybuild.md Phase 2 (S1, R21; corrected by memorybuild_report.md
    §A — the window must be the one each path is SERVED in, not a hardcoded
    32768): residentb's /v1 path serves at llama.cpp n_ctx 131,072; residenta's
    raw_chatml /api/generate path sends NO num_ctx and serves at the
    Modelfile default 16,384. The pulse passes llm.sitting_num_ctx (yaml,
    per resident); the code default is the house floor 16384 — conservative,
    so a missing key can never overflow the smallest window. Invariant:
    rendered user text stays under (num_ctx − num_predict) × 3.0 chars.
    Trim = whole EX dive blocks, newest exchanges first; never cut inside
    her words; the map (the skeleton) is never trimmed. Returns
    (text, trimmed_count) — trimmed_count −1 signals "could not trim" (no
    dives; text over cap: fail-open, logged upstream)."""
    cap = int((num_ctx - num_predict) * 3.0)
    if len(user_text or "") <= cap:
        return user_text, 0
    m = user_text.find("DEEP READS")
    if m < 0:
        return user_text, -1
    head = user_text[:m].rstrip("\n")
    parts = [p for p in _DIVE_SPLIT_RE.split(user_text[m:]) if p.strip()]
    def _ex_id(b):
        mm = _EX_ID_RE.search(b)
        return int(mm.group(1)) if mm else -1
    drop = set()

    def _size():
        return len(head) + 2 + sum(len(parts[i]) + 2 for i in range(len(parts))
                                   if i not in drop)
    for i in sorted(range(len(parts)), key=lambda i: -_ex_id(parts[i])):
        if _size() <= cap:
            break
        if _ex_id(parts[i]) >= 0:
            drop.add(i)
    out = head + "\n\n" + "\n\n".join(
        parts[i] for i in range(len(parts)) if i not in drop)
    return out, len(drop)


def pulse(date: str = None, instance: str = "residenta",
          root: str = ch.DEFAULT_ROOT, dry_run: bool = False) -> dict:
    """One nightly pulse. Returns a summary dict; never raises."""
    if os.environ.get("CONTINUA_RITUAL", "") == "0":
        return {"status": "disabled"}
    t0 = time.time()
    date = date or datetime.now().strftime("%Y-%m-%d")
    cfg_path = os.path.join(BASE, "configs", f"{instance}.yaml")
    import yaml
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 1. COLLECT — sync mirror from harvest (pre-cutover feed), update index
    harvest_dir = ((cfg.get("harvest") or {}).get("dir")
                   or os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvest"))
    synced = ch.backfill_harvest_dir(harvest_dir, instance, root=root)
    ix.update(root=root)
    import glob as _g
    records = []
    for path in sorted(_g.glob(os.path.join(root, instance, "*", f"{date}.jsonl"))):
        records.extend(ch.iter_records(path))
    records = bookmark_mod.apply_to_records(records, instance, date)
    if not records:
        return {"status": "empty", "date": date, "synced": synced}

    # 2. SEGMENT + review render
    scenes = segment_scenes(records)
    roster = pp.load_roster()

    # [CONTINUA] 2026-09-15: her model card hoisted above the render — the
    # skeleton pipeline's stage-0/1 calls need it (plan: agentwiki projects/
    # Continua-Ritual-Review-Pipeline-Plan.md).
    llm = cfg.get("llm") or {}
    her_model = llm.get("model", "testmodel-gpu:latest")
    her_url = llm.get("chat_base_url") or HER_MODEL_URL
    # raw-chatml + sampling ride HER config (same keys the bridge reads —
    # llm.raw_chatml/temperature/num_predict/repeat_penalty), so the ritual
    # speaks the exact serving dialect the bridge proved out (2026-09-13).
    # [CONTINUA] memv13 §7: the §7 labels resolve from HER config — the
    # same chatrender.py validation the bridge runs (canonical fallback +
    # fail-open here: a config error logs and keeps the pulse alive; the
    # pulse never blocks on a bad render key).
    import chatrender as _chatrender
    try:
        _rl, _htt, _tfield = _chatrender.validate_chat_config(llm)
    except Exception as _ve:
        logger.warning("[Ritual] chat-render config invalid (%s) — "
                       "canonical labels, legacy behavior", _ve)
        _rl = _chatrender.canonical_labels()
    her_llm_opts = {
        "raw_chatml": bool(llm.get("raw_chatml", False)),
        # house ruling 2026-10-07: omit temperature; hosting defaults own sampling.
        "num_predict": int(llm.get("num_predict", 8192)),
        "repeat_penalty": llm.get("repeat_penalty"),
        "role_labels": _rl,
        # §WIN (report §A.2): the raw payload's served window = the yaml's
        # sitting_num_ctx (residenta 16384 today — the RAM-confirm gate stands
        # before it rises to the declared 32768; the R21 guard reads the same key)
        "num_ctx": (int(llm["sitting_num_ctx"])
                    if llm.get("sitting_num_ctx") else None),
    }

    # THE REVIEW PIPELINE (the designer go 2026-09-15): skeleton mode runs her first
    # reading of the WHOLE day (one summary line per exchange, saved as a
    # permanent artifact), she nominates deep-dives, nominated exchanges
    # render at fuller depth. Opt-in via continua.ritual.review.mode:
    # skeleton; ABSENT = legacy single-pass, byte-parity. Fail-open: any
    # pipeline failure falls back to the legacy renderer, never blocks the
    # pulse.
    review_mode, rcfg = rr.review_config(cfg)
    review_meta = None
    skeleton_text = None
    day_text = None
    if review_mode == "skeleton" and not dry_run:
        try:
            block, meta, review_meta, skeleton_text, day_text = \
                rr.build_review_block(
                    records, scenes, roster, pp.name_for,
                    lambda s, u: ask_her(her_model, her_url, s, u,
                                         **her_llm_opts),
                    rcfg, instance, date, build_scenes_block,
                    (TURN_CHARS, BOOKMARK_CHARS))
            # [CONTINUA] memorybuild.md Phase 2 (S1): the exchanges ride the
            # meta for the KEEP parse, then POP — never logged, never
            # persisted (records are data, not meta).
            _exchanges = review_meta.pop("_exchanges", None)
            logger.info("[Review] skeleton pipeline: %s", review_meta)
        except Exception as e:
            logger.warning("[Review] pipeline failed (fail-open): %s", e)
            review_meta = {"mode": "legacy-fallback", "error": str(e)}
            _exchanges = None
    else:
        _exchanges = None
    if review_meta is not None:
        review_meta.pop("_exchanges", None)
    if review_meta is None:
        block, meta = build_scenes_block(scenes, roster)
    prior = prior_keeps(instance, date)
    meta["prior_keeps"] = len(prior)
    # memorybuild.md Phase 5 (S5): the numbered, release-capable block when
    # the gate is on; unnumbered = the legacy bytes exactly.
    _release_on = (
        os.getenv("CONTINUA_RITUAL_RELEASE", "1") != "0"
        and bool(((cfg.get("continua") or {}).get("ritual") or {}).get(
            "release", False)))
    continuity = build_continuity_block(prior, numbered=_release_on)
    if continuity:
        block = continuity + "\n" + block
    # memorybuild.md Phase 6 (S6): the dream cycle — some of what faded is
    # offered back. Gate: continua.ritual.faded_review; env
    # CONTINUA_RITUAL_FADED_REVIEW=0. Absent/corrupt data = no block.
    _faded_on = (
        os.getenv("CONTINUA_RITUAL_FADED_REVIEW", "1") != "0"
        and bool(((cfg.get("continua") or {}).get("ritual") or {}).get(
            "faded_review", False)))
    _faded_block_jobs = []
    if _faded_on:
        _fb = faded_review_block(instance)
        if _fb:
            import re as _re2
            _faded_block_jobs = [
                m2.group(1) for m2 in (
                    _re2.search(r"#([0-9a-fA-F]{6})", ln)
                    for ln in _fb.splitlines()) if m2]
            block = block + "\n\n" + _fb

    # 3. THE SITTING or MEAN — her model, her card (persona's own llm config)
    # [CONTINUA] 2026-09-12: her_url comes from HER config now — the pulse
    # used to hardcode HER_MODEL_URL (local ollama), which 404'd for residentb
    # (lab llama.cpp persona) and silently failed her first ritual. The
    # config's llm.chat_base_url is the persona's own card.
    # [CONTINUA] nightlyplan.md (the designer + residentb, approved in principle 09-26):
    # when continua.ritual.sitting is true (and CONTINUA_SITTING!=0), the
    # middle of the ritual is HER evening practice in ONE pass — aftermath
    # (a conversation with yesterday's intentions) → meaning (the KEEP
    # grammar unchanged) → tending (her own set_project declarations) →
    # intentions (verbatim, immutable). VERIFY/MARK run after, unchanged;
    # per the plan, intentions and tending are NOT verified claims about
    # the past. Markers are an offer — prose without them still files as a
    # valid sitting (skip rights). Rollback: CONTINUA_SITTING=0 → this
    # block collapses to the legacy MEAN-only path below.
    identity = (cfg.get("prompts") or {}).get("identity", "").strip()
    her_text = None
    kept = {}
    verification = {}
    sitting = {}
    _kept_unit, _kept_dropped = "scene", 0
    _scenes_for_marks = scenes
    _sitting_on = (not dry_run) and sitting_enabled(cfg)
    if _sitting_on:
        _yesterday = latest_intentions(root, instance, before_date=date)
        _system = SITTING_FRAME.format(identity=identity)
        # [CONTINUA] memorybuild.md Phase 2 (S1): when the review is the
        # EX-numbered map and the gate is on, the sitting's KEEP grammar
        # speaks EXCHANGE numbers — the numbers she actually reads. Fallback
        # (no exchanges: legacy-small-day or pipeline failure): the scene
        # grammar, byte-identical legacy.
        _sitting_exchanges = (_exchanges if
                              _keep_by_exchange(cfg) and _exchanges else None)
        _user_tpl = SITTING_USER_EX if _sitting_exchanges else SITTING_USER
        _user = _user_tpl.format(
            date=date, scenes_block=block,
            intentions_yesterday=_yesterday or
            "(none yet — your first sitting; there is no past self to "
            "answer, only tomorrow to leave lines for)")
        # R21 invariant: the render must fit the window she is served in
        _user, _trimmed = sitting_window_trim(
            _user,
            num_ctx=int(llm.get("sitting_num_ctx") or 16384),
            num_predict=int(her_llm_opts.get("num_predict") or 8192))
        if _trimmed:
            logger.info("[Ritual] sitting render over window — trimmed %d "
                        "dives%s", _trimmed,
                        " (no dives — OVER CAP, fail-open)" if _trimmed < 0
                        else "")
        her_text = ask_her(her_model, her_url, _system, _user, **her_llm_opts)
        _p = parse_sitting(her_text or "", len(scenes),
                           exchanges=_sitting_exchanges)
        kept = _p["kept"]
        _kept_unit = _p["kept_unit"]
        _kept_dropped = _p["kept_dropped"]
        if _release_on and _p.get("releases"):
            _rel_written, _rel_dropped = resolve_releases(
                instance, date, prior, _p["releases"])
            sitting["releases_filed"] = _rel_written
            sitting["releases_dropped"] = _rel_dropped
        if _faded_on and _p.get("handle_keeps"):
            _lgv = os.path.join(BASE, "recollections", instance,
                                "last_good_view.json")
            _fj = []
            if os.path.exists(_lgv):
                with open(_lgv, encoding="utf-8") as _f:
                    _fj = [e.get("job") for e in
                           (json.load(_f).get("faded_entries") or [])]
            _fj = _fj or list(_faded_block_jobs)
            _anch, _fdrop = resolve_faded_keeps(instance, _p["handle_keeps"],
                                                _fj)
            sitting["faded_anchored"] = _anch
            sitting["faded_dropped"] = _fdrop
        sitting = {
            "tend_filed": apply_tending(instance, root, _p["tend"]),
            "intentions": write_intentions(root, instance, date,
                                           _p["intend"]),
            "aftermath_chars": len(_p["aftermath"]),
        }
        # Phase T (memorybuild2 §7): the sitting's tending pass is one of
        # the two places the machinery OFFERS source material (pull-not-
        # push): for each person who appeared in the day's kept scenes,
        # the offer lands on her DESK (texture/offers/) — never the
        # prompt. She reads it with read_texture_offer; ignore = expires
        # in 3 days. Kill switch CONTINUA_TEXTURE=0; fail-open.
        try:
            import texture as _tex
            if _tex.offers_enabled(cfg):
                _offered = []
                _person_set = set()
                for _sc in scenes:
                    for _pid in (_sc.get("persons") or []):
                        if _pid and _pid not in WAKE_CLASS:
                            _person_set.add(str(_pid))
                _roster_t = pp.load_roster()
                _names_t = {pid: p.display_name
                            for pid, p in _roster_t.items()}
                for _pid in sorted(_person_set):
                    if _tex.source_offer(instance, _pid, root=None,
                                         cfg=cfg, names=_names_t):
                        _offered.append(_pid)
                # Phase W (memorybuild2 §8): the WEAVE offer (key 'self' —
                # her own sitting lines + essences as material for the
                # self-weave). Same surface, same expiry law. The machinery
                # never pre-fills self.md; she weaves or doesn't.
                try:
                    import weave as _weave
                    if _weave.weave_offer(instance, root=None, cfg=cfg):
                        _offered.append("self")
                except Exception:
                    logger.warning("[Weave] sitting offer failed open",
                                   exc_info=True)
                if _offered:
                    logger.info("[Texture] %d source offer(s) on the desk "
                                "(pull-not-push): %s", len(_offered),
                                _offered)
                    sitting["texture_offers"] = _offered
        except Exception:
            logger.warning("[Texture] sitting offers failed open",
                           exc_info=True)
    elif not dry_run:
        system = SYSTEM_FRAME.format(identity=identity)
        her_text = ask_her(her_model, her_url, system, block, **her_llm_opts)
        if _keep_by_exchange(cfg) and _exchanges:
            # the map's numbers, not scene ids (memorybuild Phase 2, S1)
            kept, _kept_dropped = parse_keeps_by_exchange(her_text or "",
                                                          _exchanges)
            _kept_unit = "exchange"
        else:
            kept = parse_decisions(her_text or "", len(scenes))
            _kept_unit, _kept_dropped = "scene", 0

    # 4. VERIFY (utility mind) — factual anchors only, now behind the
    # deterministic anchor gate (2026-09-15): a keep whose anchors exist
    # in the day's record or her skeleton can never be flagged
    # FABRICATED, whatever the validator's window saw (the 09-14 false
    # flag: a true keep of an exchange the 14K render had cut).
    # Runs for BOTH the sitting and the legacy MEAN path — the sitting's
    # KEEP lines are meanings about the day just like MEAN's. Per
    # nightlyplan.md, INTENTIONS and TENDING are NOT verified: they are
    # claims about tomorrow and her own declared project states, not
    # factual assertions about the past.
    if not dry_run:
        _scenes_for_marks = (exchanges_as_scenes(_exchanges)
                             if _kept_unit == "exchange" and _exchanges
                             else scenes)
        verification = verify_meanings(kept, _scenes_for_marks,
                                       day_text=day_text,
                                       skeleton_text=skeleton_text)

    # 5. MARK + digest
    marks_path = None
    if kept and not dry_run:
        marks_path = write_marks(date, instance, kept, _scenes_for_marks,
                                 verification, her_model, unit=_kept_unit)
    # 6. THE SECOND ACT — she updates her books (phase 4)
    # [CONTINUA] 2026-09-14 (house ruling): the in-context ritual — when
    # continua.ritual.in_context is true, tonight's acts persist into her
    # ritual thread + chronicle so she REMEMBERS the night (see the
    # in-context section above). Gate default false; residenta opts in.
    ritual_in_context = bool(
        ((cfg.get("continua") or {}).get("ritual") or {}).get(
            "in_context", False))
    if kept and not dry_run and marks_path:
        if ritual_in_context:
            # nightlyplan.md: when the sitting ran, the thread record says
            # what actually happened — she SAT, not a pipeline phase.
            _sitting_user = (
                "Tonight you sat with your own day: what mattered, your "
                "projects (tended or stuck, in your words), and any lines "
                "you left for tomorrow. The scenes are below.\n\n"
                + block[:4000] if sitting else
                "Tonight you reviewed your own day. The scenes are below; "
                "your KEEP lines decide what tonight's ritual keeps.\n\n"
                + block[:4000])
            ritual_thread_append(
                instance, date, "sitting" if sitting else "review",
                _sitting_user, (her_text or "")[:4000])
    # First-person recollections replace nightly book writing and recursive
    # strata generation. Keeps and their in-context review remain intact.
    strata_result = {"status": "retired", "replacement": "recollections"}
    elapsed = time.time() - t0
    digest_path = write_digest(date, instance, meta, kept,
                               len(_exchanges) if _kept_unit == "exchange"
                               and _exchanges else len(scenes),
                               verification, her_text, elapsed,
                               unit=_kept_unit, kept_dropped=_kept_dropped)
    return {"status": "ok", "date": date, "scenes": len(scenes),
            "kept": len(kept), "synced": synced, "meta": meta,
            "marks_path": marks_path, "digest_path": digest_path,
            "recollections": "background maintenance", "strata": strata_result,
            "review": review_meta,
            "sitting": sitting,
            "elapsed_s": round(elapsed, 1)}


def _resolve_date(raw: str) -> str:
    """Resolve the date keywords the timer uses (house ruling 2026-09-12 00:50:
    the nightly unit runs `ritual.py --date yesterday` — argparse took the
    LITERAL string, every date-keyed glob searched for a file named
    'yesterday.jsonl', and every nightly pulse since the timer was armed
    returned status=empty, silently — caught live 09-12 00:30 via residentb's
    missing ritual + the journal). Accepts 'yesterday'/'today' as keywords;
    anything else must be a real YYYY-MM-DD."""
    import datetime as _dt
    if raw == "yesterday":
        return (datetime.now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    if raw == "today":
        return datetime.now().strftime("%Y-%m-%d")
    return raw


def _nightly(instances: list, date: str, root: str, dry_run: bool) -> None:
    """The nightly loop body (extracted 2026-09-16 so main() can wrap it in
    the ritual lock): pulse every resident in order, then that resident's
    Telegram digest."""
    for inst in instances:
        # [CONTINUA] 2026-09-16 (llm-debug-mirror spec, the designer go — D3 "include
        # if easier"): the pulse's direct ask_her calls mirror to the
        # resident's debug file via the shared llm_debug module — the
        # loop-wide current-instance is the "easier" path (no instance
        # threading through every ask_her call site).
        llm_debug.set_current_instance(inst)
        result = pulse(date=date, instance=inst, root=root,
                       dry_run=dry_run)
        print(json.dumps({"instance": inst, **result},
                         indent=2, ensure_ascii=False))
        if result.get("digest_path"):
            with open(result["digest_path"]) as f:
                print("\n" + f.read())

        # [CONTINUA] the designer's daily digest (owl_forest → Telegram): the pulse
        # finishes, then the digest rolls for the SAME date (the pulse ran
        # with --date yesterday; the digest covers the completed day), PER
        # RESIDENT (T2: the designer gets one digest per ritual-enabled persona).
        # Fail-open: a digest failure is logged, never load-bearing.
        # [CONTINUA] Phase R (memorybuild2 §6): the ritual is the rhyme
        # vectors' wholesale refresher — the cache the semantic pass
        # matches against. Gated by the same continua.rhyme flag (a no-op
        # while the channel is off); fail-open (the semantic pass
        # degrades to lexical on a stale/missing cache). Wired 2026-10-05
        # at the flip — the docstring always claimed this refresh and it
        # had no caller.
        try:
            import yaml as _yaml
            import rhyme as _rhyme
            _cfgp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "configs", f"{inst}.yaml")
            with open(_cfgp, encoding="utf-8") as _f:
                _icfg = _yaml.safe_load(_f) or {}
            if _rhyme.enabled(_icfg):
                _nv = _rhyme.refresh_vectors(inst, None)
                print(f"[rhyme] vectors refreshed for {inst}: {_nv} episodes")
        except Exception:
            logging.getLogger("continua.rhyme").warning(
                "[Rhyme] vector refresh failed (fail-open)", exc_info=True)

        if not dry_run and os.environ.get("CONTINUA_DIGEST", "") != "0":
            try:
                import digest as _digest
                _records = _digest.collect_day(_digest.ch.DEFAULT_ROOT, inst,
                                               date)
                if _records:
                    _summary = _digest.generate_summary(_records, date,
                                                        instance=inst)
                    if _summary:
                        # [CONTINUA] 09-27 REVISED (the designer): the digest travels
                        # via the resident's own bot into HIS chat with her
                        # — machinery text under her identity must be
                        # labeled as machinery, or the honesty contract
                        # breaks (the digest is qwen's, never her words).
                        _out = (f"🌾 [machinery — not her words] Continua "
                                f"daily digest — {date} ({inst})\n\n{_summary}")
                        _wh = _digest.wake_highlights(_records, date, inst)
                        if _wh:
                            _out += f"\n\n— Awake cycles —\n{_wh}"
                        _out += (f"\n\n— Ritual —\n"
                                 f"{_digest.ritual_status(inst, date)}")
                        _ok = _digest.send_telegram(_out, instance=inst)
                        print(f"[digest] rolled + sent for {date} ({inst}): "
                              f"{_ok}")
                else:
                    print(f"[digest] no records for {date} ({inst}) — "
                          "nothing to send")
            except Exception:
                logging.getLogger("continua.digest").warning(
                    "[Digest] nightly send failed (fail-open)", exc_info=True)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--instance", default=None,
                    help="one resident, pulsed immediately. Omit = the "
                         "nightly loop: pulse every config with "
                         "ritual.enabled, then that resident's digest.")
    ap.add_argument("--root", default=ch.DEFAULT_ROOT)
    ap.add_argument("--dry-run", action="store_true",
                    help="segment + budget only; no model calls")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    # [T2 FIX 2026-09-11] the digest block below referenced `date`, which was
    # NEVER defined here — a NameError swallowed by the fail-open wrapper, so
    # the nightly digest silently never fired from this hook since 6705fa
    # (journal evidence: continua-ritual.service 2026-09-11 00:30:07 "nightly
    # send failed (fail-open)" → NameError). Same default pulse() applies
    # (today when --date is omitted; the timer passes --date yesterday).
    # [LIVE FIX 2026-09-12 00:5x]: --date yesterday was ALSO the second half
    # of the nightly-ritual silence — the literal string flowed into the
    # chronicle globs ("yesterday.jsonl") and every nightly pulse since the
    # timer was armed returned status=empty. Resolve the keywords first.
    date = _resolve_date(args.date) if args.date else \
        datetime.now().strftime("%Y-%m-%d")
    instances = [args.instance] if args.instance else enabled_instances()
    # [CONTINUA] 2026-09-29: the rest state — the nightly loop skips a
    # resting resident (her desk is asleep; nothing to pulse). An explicit
    # --instance run is deliberate (a the designer pulse of a resting resident is
    # allowed; the env override CONTINUA_PAUSE_<INST>=0 also wakes her).
    if not args.instance:
        try:
            import pause as _pause
            instances = [i for i in instances if not _pause.is_paused(i)]
        except Exception:
            pass
    if not args.instance and not instances:
        logging.getLogger("continua.ritual").warning(
            "[Ritual] no ritual-enabled residents; nothing to pulse")
        return
    # [CONTINUA] 2026-09-16 (house ruling: "wakes paused while the ritual
    # runs"): the pulse holds logs/ritual.lock for its WHOLE run — the wake
    # generator reads it and pauses each tick, and a second concurrent
    # pulse (a manual run during the nightly loop) stands down instead of
    # racing the books/marks/digest writes.
    lock = acquire_ritual_lock(instances)
    if lock is None:
        logging.getLogger("continua.ritual").warning(
            "[Ritual] another pulse holds the lock — this run stands down")
        return
    try:
        _nightly(instances, date, args.root, args.dry_run)
    finally:
        release_ritual_lock(lock)
        # Default-off shadow recollection maintenance starts only after the
        # quiet window is released; failures cannot change ritual completion.
        try:
            import recollections as _recollections
            for _instance in instances:
                if not args.dry_run:
                    _recollections.request_shadow(_instance)
        except Exception:
            logger.warning("[Recollections] nightly shadow trigger failed open", exc_info=True)


if __name__ == "__main__":
    main()
