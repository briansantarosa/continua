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


def prior_keeps(instance: str, current_date: str, limit: int = 6) -> list:
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
                        out.append({"date": day, "scene_id": m.get("scene_id"),
                                    "meaning": m["meaning"]})
        except (OSError, json.JSONDecodeError):
            continue
    return out[:limit]


def build_continuity_block(prior: list) -> str:
    """Render prior keeps as her own continuity, or '' on the first night."""
    if not prior:
        return ""
    lines = ["WHAT YOU KEPT ON PREVIOUS NIGHTS (your own words, newest first —",
             "tonight's ritual is done by the self those nights produced):"]
    for p in prior:
        lines.append(f"  [{p['date']}] KEEP: {p['meaning']}")
    return "\n".join(lines) + "\n"


# --- her decision ------------------------------------------------------------

def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return text.strip()


def ask_her(model: str, base_url: str, system: str, user: str,
            raw_chatml: bool = False, temperature: float = 0.7,
            num_predict: int = 8192, repeat_penalty: float | None = None) -> str | None:
    """Her model, her card. Ollama native /api/chat so num_predict/num_ctx
    options are honored (the explicit-knob ruling) — UNLESS the config's
    llm.raw_chatml is set (residenta/ring models), in which case the raw-chatml
    serving path is used (see ask_her_messages)."""
    return ask_her_messages(model, base_url,
                            [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                            raw_chatml=raw_chatml, temperature=temperature,
                            num_predict=num_predict, repeat_penalty=repeat_penalty)


def _render_chatml(messages: list) -> str:
    """The bridge's raw-chatml composer, verbatim (core.py raw path):
    im-format history with the empty-think render on assistant turns, the
    generation opening an UNclosed <think> so the model thinks in its
    trained shape."""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        c = m.get("content") or ""
        if role == "assistant":
            parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n" + c + "<|im_end|>\n")
        else:
            parts.append(f"<|im_start|>{role}\n{c}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n<think>\n")
    return "".join(parts)


def ask_her_messages(model: str, base_url: str, messages: list,
                     raw_chatml: bool = False, temperature: float = 0.7,
                     num_predict: int = 8192,
                     repeat_penalty: float | None = None) -> str | None:
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
                "prompt": _render_chatml(messages),
                "raw": True,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": num_predict,
                    "stop": ["<|im_start|>", "<|im_end|>"],
                },
            }
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
                "max_tokens": 8192, "temperature": 0.7,
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
                verification: dict, model: str) -> str:
    """Atomically write this date's marks (the ritual's decision for the
    day — derived artifact; the chronicle itself stays append-only and is
    never rewritten). Returns the marks path."""
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
                 elapsed_s: float) -> str:
    """File-only ritual digest: budget used-vs-allocated, mark-rate monitor,
    verification flags. The heartbeat's paper trail."""
    os.makedirs(DIGEST_DIR, exist_ok=True)
    path = os.path.join(DIGEST_DIR, f"{instance}_{date}.txt")
    rate = (len(kept) / scene_count) if scene_count else 0.0
    lines = [
        f"🕯 Ritual pulse — {instance} — {date}",
        f"  scenes: {scene_count} | kept: {len(kept)} | mark-rate: {rate:.0%}"
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
                   or "/home/you/heretic/harvest")
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
    her_llm_opts = {
        "raw_chatml": bool(llm.get("raw_chatml", False)),
        "temperature": float(llm.get("temperature", 0.7)),
        "num_predict": int(llm.get("num_predict", 8192)),
        "repeat_penalty": llm.get("repeat_penalty"),
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
            logger.info("[Review] skeleton pipeline: %s", review_meta)
        except Exception as e:
            logger.warning("[Review] pipeline failed (fail-open): %s", e)
            review_meta = {"mode": "legacy-fallback", "error": str(e)}
    if review_meta is None:
        block, meta = build_scenes_block(scenes, roster)
    prior = prior_keeps(instance, date)
    meta["prior_keeps"] = len(prior)
    continuity = build_continuity_block(prior)
    if continuity:
        block = continuity + "\n" + block

    # 3. MEAN — her model, her card (persona's own llm config)
    # [CONTINUA] 2026-09-12: her_url comes from HER config now — the pulse
    # used to hardcode HER_MODEL_URL (local ollama), which 404'd for residentb
    # (lab llama.cpp persona) and silently failed her first ritual. The
    # config's llm.chat_base_url is the persona's own card.
    identity = (cfg.get("prompts") or {}).get("identity", "").strip()
    system = SYSTEM_FRAME.format(identity=identity)
    her_text = None
    kept = {}
    verification = {}
    if not dry_run:
        her_text = ask_her(her_model, her_url, system, block, **her_llm_opts)
        kept = parse_decisions(her_text or "", len(scenes))
        # 4. VERIFY (utility mind) — factual anchors only, now behind the
        # deterministic anchor gate (2026-09-15): a keep whose anchors exist
        # in the day's record or her skeleton can never be flagged
        # FABRICATED, whatever the validator's window saw (the 09-14 false
        # flag: a true keep of an exchange the 14K render had cut).
        verification = verify_meanings(kept, scenes, day_text=day_text,
                                       skeleton_text=skeleton_text)

    # 5. MARK + digest
    marks_path = None
    if kept and not dry_run:
        marks_path = write_marks(date, instance, kept, scenes,
                                 verification, her_model)
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
            ritual_thread_append(
                instance, date, "review",
                "Tonight you reviewed your own day. The scenes are below; "
                "your KEEP lines decide what tonight's ritual keeps.\n\n"
                + block[:4000],
                (her_text or "")[:4000])
    # First-person recollections replace nightly book writing and recursive
    # strata generation. Keeps and their in-context review remain intact.
    strata_result = {"status": "retired", "replacement": "recollections"}
    elapsed = time.time() - t0
    digest_path = write_digest(date, instance, meta, kept, len(scenes),
                               verification, her_text, elapsed)
    return {"status": "ok", "date": date, "scenes": len(scenes),
            "kept": len(kept), "synced": synced, "meta": meta,
            "marks_path": marks_path, "digest_path": digest_path,
            "recollections": "background maintenance", "strata": strata_result,
            "review": review_meta,
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
        if not dry_run and os.environ.get("CONTINUA_DIGEST", "") != "0":
            try:
                import digest as _digest
                _records = _digest.collect_day(_digest.ch.DEFAULT_ROOT, inst,
                                               date)
                if _records:
                    _summary = _digest.generate_summary(_records, date,
                                                        instance=inst)
                    if _summary:
                        _out = (f"🌾 Continua daily digest — {date} "
                                f"({inst})\n\n{_summary}")
                        _wh = _digest.wake_highlights(_records, date, inst)
                        if _wh:
                            _out += f"\n\n— Awake cycles —\n{_wh}"
                        _out += (f"\n\n— Ritual —\n"
                                 f"{_digest.ritual_status(inst, date)}")
                        _ok = _digest.send_telegram(_out)
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
