"""chronicle.py — the Continua mirror: schema v1, append-only capture, backfill.

Phase-1 component of Continua (wiki: ~/agentwiki/projects/Continua.md).
The chronicle is HER memory: a per-(instance, person, day) append-only JSONL
mirror of every conversation turn, person-tagged, with the num_predict-cliff
fix baked in (finish_reason recorded from day one — the silent-truncation
ruling, 2026-09-07).

Invariants (house discipline):
  - APPEND-ONLY. Nothing here ever rewrites or deletes a line. No retrospective
    revocation (review round one #5).
  - FAIL-OPEN. Capture must never break the caller: every failure is swallowed
    and logged, same contract as harvest_hook.py.
  - CAPTURE STAYS DUMB AND TOTAL (governing principle 1). No judgment here —
    no salience, no meaning, no scene-segmentation. Those belong to the Ritual.
    The only computation allowed at capture is mechanical: uid derivation and
    the length_cut flag (a data point about the generation, not an opinion).
  - PERSON-TAGGED from birth (social layer): every line carries person_id.
    Cross-person unification happens at recall, never by erasing attribution.

Storage layout:
    <root>/<instance>/<person_id>/<YYYY-MM-DD>.jsonl

One JSON object per line, schema_version 1:
    schema_version   1
    uid              stable 12-hex id — same derivation as harvest
                     (sha1(ts|role|content[:160])[:12]) so backfilled lines
                     keep their harvest identity and dedup/idempotency work
    ts               ISO timestamp (from harvest; local tz)
    instance         persona instance id ("residenta")
    person_id        telegram chat id of the human (roster key; names live in
                     the people: YAML block, never denormalized here)
    role             "user" | "assistant"
    content          message text
    reasoning        assistant-only chain-of-thought (nullable)
    latency_s        assistant-only generation seconds (nullable)
    finish_reason    assistant-only: "stop" | "length" | null (null = legacy
                     backfill, pre-instrumentation)
    eval_count       assistant-only ollama eval token count (nullable)
    length_cut       bool — the silent-truncation flag. finish_reason=="length"
                     for new lines; mid-sentence heuristic for legacy backfill.
                     length_cut rows are NEVER RFT candidates (extends the
                     TERMINATES ruling) — enforced downstream, recorded here.
    memory_injection what memory context was injected that turn (nullable)
    model            model/ring that produced the turn (provenance)
    bookmark         bool — dumb capture-level "remember this" stamp (data,
                     not judgment; the Ritual reads it as priority input)
    salience         null — RESERVED for the Ritual's meaning-maker marks
                     (nightly pulse). Capture never sets it.
    source           "live" | "harvest-backfill"
    harvest_path     backfill-only: source file basename (provenance)

Kill switch: CONTINUA_CAPTURE=0 disables append (no-op, never raises).
"""

import hashlib
import json
import logging
import os
import re

logger = logging.getLogger("continua.chronicle")

SCHEMA_VERSION = 1
DEFAULT_ROOT = "/tmp/continua/chronicle"

_TERMINATORS = ".!?…。！？"
# Closing wrappers that may legally follow a terminator (quotes, brackets,
# markdown emphasis, emoji sign-offs — emoji are part of her voice: "The dance
# continues. 🌲✨" is a COMPLETE sentence, measured in the 2026-09-07 backfill).
_CLOSERS = "\"'”’)）]】*`》»"
_EMOJI_TAIL_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u2BFF\uFE0F\u200d\\s]+$")
# Structurally-complete XML endings (tool-call grammar): parseable structure,
# not mid-prose. Real cuts of these turns are caught by finish_reason going
# forward; the heuristic never claims them.
_STRUCTURAL_ENDINGS = ("</call>", "</think>", "</function>", "</parameter>",
                       "</tool_call>")

_HARVEST_FILENAME_RE = re.compile(
    r"^(?P<instance>[a-z0-9_]+?)_(?P<person>\d+)_(?P<day>\d{4}-\d{2}-\d{2})\.jsonl$"
)


def _uid(ts: str, role: str, content: str) -> str:
    """Stable line id — byte-compatible with harvest_hook.py's derivation so
    backfilled lines keep their harvest identity (dedup/idempotency key)."""
    src = (ts or "") + "|" + (role or "") + "|" + (content or "")[:160]
    return hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]


def mid_sentence_tail(text: str) -> bool:
    """Legacy heuristic: does this text end mid-sentence?

    Used ONLY for backfilled lines that predate finish_reason capture —
    the heuristic that caught the num_predict cliff (turn 6, 2026-09-06:
    18,652 chars ending 'Wait, let'). Conservative: only flags when the last
    character (after stripping emoji decoration, closing wrappers) is not a
    terminator. Tuned on the 2026-09-07 backfill: emoji sign-offs and XML
    structural endings are complete, not cuts.
    """
    if not text:
        return False
    t = text.rstrip()
    if t.endswith(_STRUCTURAL_ENDINGS):
        return False
    # strip emoji/variation-selector decoration (her sign-off voice)
    t = _EMOJI_TAIL_RE.sub("", t).rstrip()
    while t and t[-1] in _CLOSERS:
        t = t[:-1].rstrip()
        t = _EMOJI_TAIL_RE.sub("", t).rstrip()
    if not t:
        return False
    return t[-1] not in _TERMINATORS


def compute_length_cut(role: str, content: str, finish_reason) -> bool:
    """The silent-truncation flag.

    New lines: finish_reason == "length" (authoritative). Legacy backfill
    (finish_reason None): mid-sentence heuristic, assistant lines only —
    a user message is never a generation cut.
    """
    if role != "assistant":
        return False
    if finish_reason == "length":
        return True
    if finish_reason is None:
        return mid_sentence_tail(content or "")
    return False


def day_path(root: str, instance: str, person_id: str, day: str) -> str:
    """<root>/<instance>/<person_id>/<day>.jsonl"""
    return os.path.join(root, instance, str(person_id), f"{day}.jsonl")


def latest_reasoning(instance: str, root: str = None) -> str:
    """[CONTINUA] 2026-09-16 (specs/2026-09-16-chat-think-contract.md): the
    most recent captured REAL reasoning for an instance — any channel,
    today's day-files first, then yesterday's. The Layer-2 prefill source
    (her own last planning register, never fabricated). Returns "" when
    nothing exists. Skips underscore-prefixed dirs (_test etc.).
    Fail-open: any error returns "". root=None resolves DEFAULT_ROOT at
    CALL time (default-arg binding made the root unmonkeypatchable in
    tests — caught by chat_contract_test 2026-09-16)."""
    try:
        base = os.path.join(root or DEFAULT_ROOT, instance)
        if not os.path.isdir(base):
            return ""
        best_ts, best_think = "", ""
        for person in os.listdir(base):
            pdir = os.path.join(base, person)
            if person.startswith("_") or not os.path.isdir(pdir):
                continue
            days = sorted((d for d in os.listdir(pdir)
                           if d.endswith(".jsonl")), reverse=True)
            for day in days[:2]:
                rec = _latest_reasoning_in_file(os.path.join(pdir, day))
                if rec and rec[0] > best_ts:
                    best_ts, best_think = rec
        return best_think
    except Exception:
        return ""


def _latest_reasoning_in_file(path: str):
    """(ts, think) of the LAST record in the file carrying a non-empty
    reasoning_round — files are appended chronologically, so a reverse scan
    with an early break finds that day's latest. None when absent."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rr = r.get("reasoning_rounds") or []
            for th in reversed(rr):
                if (th or "").strip():
                    return (r.get("ts") or "", th.strip())
        return None
    except Exception:
        return None


def _base_record(ts, instance, person_id, role, content, model=None,
                 memory_injection=None, uid=None, bookmark=False,
                 reasoning_rounds=None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "uid": uid or _uid(ts, role, content),
        "ts": ts,
        "instance": instance,
        "person_id": str(person_id),
        "role": role,
        "content": content or "",
        "reasoning": None,
        "reasoning_rounds": reasoning_rounds,  # [CONTINUA] per-round thinks (2026-09-15)
        "latency_s": None,
        "finish_reason": None,
        "eval_count": None,
        "length_cut": False,
        "memory_injection": memory_injection,
        "model": model,
        "bookmark": bool(bookmark),
        "salience": None,  # RESERVED for the Ritual — capture never sets it
        "source": "live",
        "harvest_path": None,
    }


def append(record: dict, root: str = DEFAULT_ROOT,
           _kill_switch_env: str = "CONTINUA_CAPTURE"):
    """Append one turn to the chronicle. Fail-open, kill-switchable.

    `record` needs: ts, instance, person_id, role, content. Optional:
    reasoning, latency_s, finish_reason, eval_count, memory_injection,
    model, bookmark, uid. length_cut is always computed here (mechanical,
    from finish_reason/content — never trusted from the caller).
    Returns the written record dict, or None on kill-switch/failure.
    """
    try:
        if os.environ.get(_kill_switch_env, "") == "0":
            return None
        for field in ("ts", "instance", "person_id", "role", "content"):
            if field not in record:
                raise ValueError(f"chronicle.append missing field: {field}")
        rec = _base_record(
            ts=record["ts"], instance=record["instance"],
            person_id=record["person_id"], role=record["role"],
            content=record["content"], model=record.get("model"),
            memory_injection=record.get("memory_injection"),
            uid=record.get("uid"), bookmark=bool(record.get("bookmark", False)),
            reasoning_rounds=record.get("reasoning_rounds"),
        )
        rec["source"] = record.get("source", "live")
        if rec["role"] == "assistant":
            rec["reasoning"] = record.get("reasoning")
            rec["reasoning_rounds"] = record.get("reasoning_rounds")
            rec["latency_s"] = record.get("latency_s")
            rec["finish_reason"] = record.get("finish_reason")
            rec["eval_count"] = record.get("eval_count")
        rec["length_cut"] = compute_length_cut(
            rec["role"], rec["content"], rec["finish_reason"])
        path = day_path(root, rec["instance"], rec["person_id"],
                        rec["ts"][:10])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception:
        logger.warning("[Chronicle] append failed (fail-open)", exc_info=True)
        return None


def iter_records(path: str):
    """Yield records from a chronicle file, skipping corrupt lines."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[Chronicle] corrupt line skipped in %s", path)
    except FileNotFoundError:
        return


def existing_uids(path: str) -> set:
    return {r.get("uid") for r in iter_records(path) if r.get("uid")}


def _map_harvest_line(hline: dict, instance: str, person_id: str,
                      harvest_basename: str) -> dict:
    """Map one harvest line to a mirror record (backfill path)."""
    role = hline.get("role", "user")
    rec = _base_record(
        ts=hline.get("ts", ""), instance=instance, person_id=person_id,
        role=role, content=hline.get("content", ""),
        model=hline.get("model"), memory_injection=hline.get("memory_injection"),
        uid=hline.get("uid"), bookmark=False,
    )
    rec["source"] = "harvest-backfill"
    rec["harvest_path"] = harvest_basename
    if role == "assistant":
        rec["reasoning"] = hline.get("reasoning_content")
        rec["latency_s"] = hline.get("latency_s")
        # Legacy lines predate finish_reason capture → None; the heuristic
        # decides length_cut (the instrument that caught the cliff).
        rec["finish_reason"] = None
        rec["eval_count"] = None
    rec["length_cut"] = compute_length_cut(role, rec["content"], rec["finish_reason"])
    return rec


def backfill_harvest_file(harvest_path: str, root: str = DEFAULT_ROOT,
                          instance: str = None, person_id: str = None,
                          day: str = None) -> int:
    """Backfill one harvest JSONL file into the mirror. Idempotent by uid.

    Filename contract: {instance}_{person}_{day}.jsonl (harvest_hook.py).
    Explicit instance/person/day args override the parsed filename.
    Returns the number of lines written (0 if all already present).
    """
    try:
        base = os.path.basename(harvest_path)
        m = _HARVEST_FILENAME_RE.match(base)
        if not m:
            logger.warning("[Chronicle] unrecognized harvest filename: %s", base)
            return 0
        instance = instance or m.group("instance")
        person_id = person_id or m.group("person")
        day = day or m.group("day")

        out_path = day_path(root, instance, person_id, day)
        seen = existing_uids(out_path) if os.path.exists(out_path) else set()
        written = 0
        with open(harvest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    hline = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[Chronicle] corrupt harvest line in %s", base)
                    continue
                rec = _map_harvest_line(hline, instance, person_id, base)
                if rec["uid"] in seen:
                    continue
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                seen.add(rec["uid"])
                written += 1
        if written:
            logger.info("[Chronicle] backfilled %d lines -> %s", written, out_path)
        return written
    except Exception:
        logger.warning("[Chronicle] backfill failed for %s (fail-open)",
                       harvest_path, exc_info=True)
        return 0


def backfill_harvest_dir(harvest_dir: str, instance: str,
                         root: str = DEFAULT_ROOT) -> int:
    """Backfill every {instance}_*.jsonl in a harvest directory."""
    import glob
    total = 0
    for path in sorted(glob.glob(os.path.join(harvest_dir, f"{instance}_*.jsonl"))):
        total += backfill_harvest_file(path, root=root)
    return total
