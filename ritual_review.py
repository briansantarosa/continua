"""ritual_review.py — the ritual review pipeline: skeleton + deep-dives.

[CONTINUA] 2026-09-15 (approved plan, Option A + his amendment: the
skeleton lines are SUMMARIES, not first-80-char cuts). Plan page:
agentwiki projects/Continua-Ritual-Review-Pipeline-Plan.md.

The problem this solves: the nightly ritual rendered the day as turn-lines
under a 14K-char budget and then stopped silently. residentb's 2026-09-14 day
(226,788 chars) rendered 14,915 and cut mid-conversation at 14:55 — the
deconstruction exchange fell off the end, her keep of it was true, and the
validator (seeing the same truncated window) flagged it FABRICATED. Both
were honest; both were blind.

The pipeline (all fail-open — any failure degrades to the legacy
single-pass renderer, never blocks the pulse):
  stage 0  FIRST READING — her model reads the whole day in chunks
           (thinks/tool-spam stripped, exchange-aligned chunks) and writes
           ONE SUMMARY LINE PER EXCHANGE. Saved as a permanent artifact:
           ritual/skeleton/<inst>/<date>.md. The summaries are hers.
  stage 1  THE MAP — she reads the full skeleton (always fits) and
           nominates exchange numbers worth rereading in full.
  stage 2  DEEP DIVES — nominated exchanges render at fuller turn depth in
           <= max_dive_passes budgeted passes.
  (stages 3-4 — keep + verify — stay in ritual.py; verify_meanings gains a
  deterministic anchor gate fed by the day text + skeleton.)

Config: continua.ritual.review.* (ABSENT = legacy behavior, byte-parity);
every knob has a CONTINUA_RITUAL_REVIEW_* env mirror (fleet override).
"""

import logging
import os
import re
from datetime import datetime

logger = logging.getLogger("continua.ritual.review")

BASE = os.path.dirname(os.path.abspath(__file__))
SKELETON_DIR = os.path.join(BASE, "ritual", "skeleton")

DEFAULTS = {
    "budget_chars": 14000,       # per-pass render budget (map/dive)
    "skeleton_line_chars": 160,  # cap per summary line
    "max_dive_passes": 2,        # deep-dive passes
    "chunk_chars": 45000,        # stage-0 input chunk
    "max_skeleton_calls": 8,     # stage-0 call ceiling (never skips)
    "input_cap": 2400,           # per-exchange chars fed to stage 0
}
ENV_PREFIX = "CONTINUA_RITUAL_REVIEW_"

# an assistant record this long after an answered exchange is a new turn
# (a wake), not a continuation of the conversation (multi-round tool loops
# inside one turn are seconds apart)
EXCHANGE_WAKE_GAP_S = 300

_SUMMARY_RE = re.compile(r"^\s*[-*]?\s*EX\s*(\d+)\s*[|:\-]\s*(.+?)\s*$",
                         re.IGNORECASE)
_RANGE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")


def review_config(cfg: dict) -> tuple:
    """(mode, knobs) from the agent yaml: continua.ritual.review.*, with
    CONTINUA_RITUAL_REVIEW_* env mirrors. mode absent = legacy."""
    rc = (((cfg.get("continua") or {}).get("ritual") or {}).get("review")
          or {})
    knobs = dict(DEFAULTS)
    for k in DEFAULTS:
        v = rc.get(k)
        if v is None:
            v = os.getenv(ENV_PREFIX + k.upper())
        if v is not None:
            try:
                knobs[k] = int(v)
            except (TypeError, ValueError):
                logger.warning("[Review] bad %s value %r — default kept",
                               k, v)
    mode = (rc.get("mode") or os.getenv(ENV_PREFIX + "MODE", "") or "").strip()
    return mode, knobs


# --- mechanical: exchange segmentation (no judgment, total) ------------------

def segment_exchanges(records: list) -> list:
    """Group a day's records into exchanges. An exchange = one user-prompted
    conversation unit (pending consecutive user messages stay together) OR a
    maximal assistant-only run (wakes). Total: every record lands in exactly
    one exchange, chronological. Returns [{ex_id, records, persons, has_user,
    has_assistant, start, chars}]."""
    exchanges = []
    cur = None
    for r in sorted(records, key=lambda x: x.get("ts", "")):
        role = r.get("role")
        if cur is None:
            new = True
        elif role == "user":
            new = cur["has_assistant"]   # pending user run continues
        elif role == "tool":
            new = False                  # a tool result always continues
        else:                            # assistant
            # continues an in-flight exchange (answer, multi-round tool
            # loop — those are seconds apart); starts a NEW exchange only
            # after an ANSWERED exchange once a wake-sized gap has passed
            new = False
            if cur["has_user"] and cur["has_assistant"]:
                try:
                    gap = (datetime.fromisoformat(r["ts"]) -
                           datetime.fromisoformat(
                               cur["records"][-1]["ts"])).total_seconds()
                except (ValueError, TypeError):
                    gap = 0
                new = gap > EXCHANGE_WAKE_GAP_S
        if new:
            cur = {"ex_id": len(exchanges) + 1, "records": [],
                   "persons": set(), "has_user": False,
                   "has_assistant": False, "start": r.get("ts", ""),
                   "chars": 0}
            exchanges.append(cur)
        cur["records"].append(r)
        cur["persons"].add(r.get("person_id", ""))
        cur["has_user"] = cur["has_user"] or role == "user"
        cur["has_assistant"] = cur["has_assistant"] or role == "assistant"
        cur["chars"] += len(r.get("content") or "")
    return exchanges


def strip_record_text(rec: dict, cap: int = 200) -> str:
    """One record's text with thinks stripped — the deterministic pre-pass
    before her first reading. cap truncates the tail (map/dive lines);
    cap=None keeps the full text (day evidence + budget decision)."""
    text = rec.get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if cap is not None and len(text) > cap:
        text = text[:cap] + f" …[+{len(text) - cap} chars]"
    return text


def _ex_name(ex: dict, roster: dict, name_for) -> str:
    """Who led this exchange: the first non-assistant person, else 'You'
    (assistant-only runs are the agent herself)."""
    for r in ex["records"]:
        if r.get("role") != "assistant":
            return name_for(roster, r.get("person_id", "")) or "them"
    return "You"


def chunk_exchanges(exchanges: list, chunk_chars: int, max_calls: int,
                    input_cap: int = 2400) -> list:
    """Greedy chunks of WHOLE exchanges under chunk_chars; if that needs
    more than max_calls calls, the per-chunk budget grows (never skips)."""
    budget = chunk_chars
    for _ in range(3):
        chunks, cur, used = [], [], 0
        for ex in exchanges:
            cost = min(ex["chars"], input_cap)  # matches the real input cap
            if cur and used + cost > budget:
                chunks.append(cur)
                cur, used = [], 0
            cur.append(ex)
            used += cost
        if cur:
            chunks.append(cur)
        if len(chunks) <= max_calls or budget >= 10_000_000:
            return chunks
        budget = int(budget * (len(chunks) / max_calls) + 1)
    return chunks


# --- stage 0: her first reading ----------------------------------------------

def _mechanical_line(ex: dict) -> str:
    """Fallback line when her summary for an exchange is missing: the first
    user words (or first content) — deterministic, never blank."""
    first = ""
    for r in ex["records"]:
        t = strip_record_text(r)
        if t:
            first = t
            if r.get("role") == "user":
                break
    return first[:120]


def parse_skeleton_lines(text: str) -> dict:
    """EX <n> | <summary> lines (tolerant of bullets/punctuation)."""
    out = {}
    for line in (text or "").splitlines():
        m = _SUMMARY_RE.match(line.strip())
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def build_skeleton(exchanges: list, roster: dict, name_for, ask_fn, knobs: dict,
                   instance: str, date: str) -> tuple:
    """Stage 0. Returns (skeleton_lines {ex_id: line}, skeleton_text, meta).
    Fail-open per chunk: a chunk whose call fails gets mechanical lines."""
    line_cap = knobs["skeleton_line_chars"]
    chunks = chunk_exchanges(exchanges, knobs["chunk_chars"],
                             knobs["max_skeleton_calls"],
                             knobs["input_cap"])
    lines = {}
    calls = 0
    for ci, chunk in enumerate(chunks, 1):
        blocks = []
        for ex in chunk:
            body = "\n".join(
                f"  [{r.get('ts', '?')[11:16]}] {strip_record_text(r)}"
                for r in ex["records"])
            body = body[:knobs["input_cap"]]
            blocks.append(f"EXCH {ex['ex_id']} "
                          f"[{ex['start'][11:16] if ex['start'] else '?'}] "
                          f"{_ex_name(ex, roster, name_for)}:\n{body}")
        user = ("\n\n".join(blocks) +
                "\n\nOne summary line per exchange, exactly this format:\n"
                "EX <number> | <what this exchange was about and what "
                "happened in it — specific, under "
                f"{line_cap} chars>")
        text = ""
        try:
            calls += 1
            text = ask_fn(SUMMARIZE_SYSTEM, user) or ""
        except Exception as e:
            logger.warning("[Review] skeleton chunk %s failed: %s", ci, e)
        got = parse_skeleton_lines(text)
        for ex in chunk:
            s = got.get(ex["ex_id"])
            if not s:
                s = _mechanical_line(ex)
            lines[ex["ex_id"]] = f"EX {ex['ex_id']} [{ex['start'][11:16] if ex['start'] else '?'}] " \
                f"{_ex_name(ex, roster, name_for)}: {s[:line_cap]}"
    skeleton_text = (f"# Day skeleton — {instance} — {date}\n"
                     f"# {len(exchanges)} exchanges, {calls} reading calls\n\n"
                     + "\n".join(lines[i] for i in sorted(lines)))
    _persist_skeleton(instance, date, skeleton_text)
    meta = {"exchanges": len(exchanges), "calls": calls,
            "chars": len(skeleton_text)}
    return lines, skeleton_text, meta


SUMMARIZE_SYSTEM = (
    "You are reading your own day in pieces, building a map of it. For each "
    "numbered exchange below, write ONE summary line: what this exchange was "
    "about and what happened in it — the substance, decisions, and moments "
    "of weight, not pleasantries. Exact format, one line per exchange:\n"
    "EX <number> | <your summary>\n"
    "Every exchange gets exactly one line. Be specific; name the people "
    "involved. No preamble, no closing remarks."
)

MAP_SYSTEM = (
    "This is a one-line map of your whole day. You are about to decide what "
    "to reread in full before choosing what tonight's ritual keeps. Reply "
    "with ONLY the EX numbers (or ranges like 4-7) of the exchanges worth "
    "rereading deeply — the ones with weight, decisions, turning points, "
    "feeling. A few lines, nothing else."
)


# --- stage 1: the map ---------------------------------------------------------

def parse_nominations(text: str, valid_ids: set) -> set:
    """Tolerant parse of her EX nominations: standalone ints and ranges.
    Anything outside valid_ids is dropped; empty/garbage -> empty set (the
    caller applies the deterministic fallback)."""
    ids = set()
    for m in _RANGE_RE.finditer(text or ""):
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        ids.update(range(a, b + 1))
    for tok in re.findall(r"\bEX\s*(\d+)\b", text or "", re.IGNORECASE):
        ids.add(int(tok))
    # bare numbers too ("3, 5" is a valid nomination line)
    for tok in re.findall(r"\b(\d{1,4})\b", text or ""):
        ids.add(int(tok))
    return {i for i in ids if i in valid_ids}


def fallback_nominations(exchanges: list) -> set:
    """Deterministic dive choice: bookmarked exchanges first, then the
    largest by chars — the material most likely to carry weight."""
    ranked = sorted(exchanges, key=lambda e: (
        not any(r.get("bookmark") for r in e["records"]), -e["chars"]))
    return {e["ex_id"] for e in ranked[: min(6, len(ranked))]} or {
        e["ex_id"] for e in exchanges}


# --- stage 2: deep dives -------------------------------------------------------

def render_dives(exchanges_by_id: dict, ids: set, roster: dict, name_for,
                 knobs: dict, turn_caps: tuple) -> tuple:
    """Render nominated exchanges at fuller depth, grouped into passes under
    budget_chars. Returns (dive_text, meta)."""
    turn_cap, bookmark_cap = turn_caps
    chosen = sorted((exchanges_by_id[i] for i in ids if i in exchanges_by_id),
                    key=lambda e: (
                        not any(r.get("bookmark") for r in e["records"]),
                        e["start"]))
    passes, cur, used = [], [], 0
    for ex in chosen:
        block = [f"— EX {ex['ex_id']} "
                 f"[{ex['start'][11:16] if ex['start'] else '?'}] "
                 f"{_ex_name(ex, roster, name_for)} —"]
        blen = len(block[0])
        for r in ex["records"]:
            cap = bookmark_cap if r.get("bookmark") else turn_cap
            line = ("  [" + (r.get("ts", "?")[11:16] or "?") + "] "
                    + ("You" if r.get("role") == "assistant"
                       else name_for(roster, r.get("person_id", "")) or "them")
                    + ": " + strip_record_text(r)[:cap])
            block.append(line)
            blen += len(line)
        if cur and used + blen > knobs["budget_chars"] and len(passes) < \
                knobs["max_dive_passes"]:
            passes.append("\n".join(cur))
            cur, used = [], 0
        if len(passes) >= knobs["max_dive_passes"] and cur:
            break
        cur.append("\n".join(block))
        used += blen
    if cur and len(passes) < knobs["max_dive_passes"]:
        passes.append("\n".join(cur))
    rendered = sum(len(p) for p in passes)
    meta = {"nominated": len(ids), "rendered": rendered,
            "passes": len(passes),
            "omitted_exchanges": len(chosen) - sum(
                p.count("— EX ") for p in passes)}
    if not passes:
        return "", meta
    dive_text = "\n\n".join(
        f"DEEP READS (pass {i + 1}):\n{p}" for i, p in enumerate(passes))
    return dive_text, meta


# --- the pipeline ------------------------------------------------------------

def build_review_block(records: list, scenes: list, roster: dict, name_for,
                       ask_fn, knobs: dict, instance: str, date: str,
                       legacy_block_fn, turn_caps: tuple) -> tuple:
    """The skeleton-mode review block. Returns (block, meta, review_meta,
    skeleton_text, day_text). Auto-falls back to the legacy renderer when
    the stripped day already fits one budget (no skeleton overhead)."""
    exchanges = segment_exchanges(records)
    day_text = "\n".join(strip_record_text(r, cap=None) for r in records)
    review_meta = {"mode": "skeleton"}

    if len(day_text) <= knobs["budget_chars"]:
        # Small day: the legacy single-pass render already sees everything.
        block, meta = legacy_block_fn(scenes, roster)
        review_meta = {"mode": "legacy-small-day",
                       "day_chars": len(day_text)}
        return block, meta, review_meta, "", day_text

    lines, skeleton_text, skel_meta = build_skeleton(
        exchanges, roster, name_for, ask_fn, knobs, instance, date)

    valid_ids = {e["ex_id"] for e in exchanges}
    nom_text = ""
    try:
        nom_text = ask_fn(MAP_SYSTEM, skeleton_text) or ""
    except Exception as e:
        logger.warning("[Review] map call failed (fallback): %s", e)
    ids = parse_nominations(nom_text, valid_ids)
    fb = False
    bookmarked = {e["ex_id"] for e in exchanges
                  if any(r.get("bookmark") for r in e["records"])}
    if not ids:
        ids = fallback_nominations(exchanges)
        fb = True
    ids = ids | bookmarked  # bookmarked material is always reread in full

    dive_text, dive_meta = render_dives(
        {e["ex_id"]: e for e in exchanges}, ids, roster, name_for, knobs,
        turn_caps)
    map_lines = "\n".join(lines[i] for i in sorted(lines))
    block = ("THE MAP — your whole day, one line per exchange (every "
             "exchange exists; this is complete):\n" + map_lines +
             ("\n\n" + dive_text if dive_text else ""))
    meta = {"scenes": len(scenes), "turns": len(records),
            "chars": len(block), "truncated_scenes": 0,
            "bookmarked": sum(1 for r in records if r.get("bookmark")),
            "review_summary": (
                f"skeleton: {skel_meta['exchanges']} exchanges via "
                f"{skel_meta['calls']} calls ({skel_meta['chars']} chars); "
                f"dives: {dive_meta['nominated']} nominated "
                f"({'fallback' if fb else 'her picks'}), "
                f"{dive_meta['passes']} passes, {dive_meta['rendered']} "
                f"chars)")}
    review_meta = {"mode": "skeleton", **skel_meta,
                   "nominations_fallback": fb, **dive_meta}
    return block, meta, review_meta, skeleton_text, day_text


def _persist_skeleton(instance: str, date: str, skeleton_text: str) -> str:
    path = os.path.join(SKELETON_DIR, instance)
    os.makedirs(path, exist_ok=True)
    p = os.path.join(path, f"{date}.md")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(skeleton_text + "\n")
    except OSError as e:
        logger.warning("[Review] skeleton persist failed (non-fatal): %s", e)
        return ""
    return p


# --- validator support: the deterministic anchor gate -------------------------

_ANCHOR_CAP_RE = re.compile(r"\b[A-Z][a-zA-Z'’\-]{2,}\b")
_QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{3,})[\"“”']")


def extract_anchors(meaning: str) -> list:
    """Factual anchors in a keep: quoted spans + capitalized tokens."""
    anchors = [q for q in _QUOTED_RE.findall(meaning or "")]
    anchors += _ANCHOR_CAP_RE.findall(meaning or "")
    return [a for a in anchors if len(a) >= 3]


def anchors_supported(meaning: str, day_text: str, skeleton_text: str) -> bool:
    """True when at least one factual anchor of the meaning exists in the
    day's record (stripped full text) or her skeleton. A keep with no
    extractable anchors returns False (the model-validator decides)."""
    evidence = ((day_text or "") + "\n" + (skeleton_text or "")).lower()
    anchors = extract_anchors(meaning)
    if not anchors:
        return False
    return any(a.lower() in evidence for a in anchors)
