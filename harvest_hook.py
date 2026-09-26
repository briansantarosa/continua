"""harvest_hook.py — fail-open conversation harvest, per-instance opt-in.

Implements /home/you/heretic/harvest/HARVEST_SPEC.md with one telegram
deviation: telegram has no session ids, so files are per-user and
date-scoped (session = date).

GATE (config-driven since 2026-08-28 M1c, was residenta-hard-gated):
  config["harvest"]["enabled"] — instance opts in via its yaml block.
  All other instances (no harvest block) stay off — zero behavior change.

Fail-open contract: every failure is swallowed and logged. This hook can
never break a chat turn.
"""
import datetime
import hashlib
import json
import logging
import os
import re
import time

logger = logging.getLogger("sagent.harvest")

# Frame-vocabulary density (stage-2 growth annotation, 2026-09-03): the SAME
# pattern as ~/heretic/probes/frame_vocab_tracker.py — annotations written at
# harvest time must agree with the tracker's counts. Measures absorbed-frame
# density per turn so curation can auto-exclude frame-locked turns (the
# mirror-lock guard; see ~/heretic/training/GROWTH_LOOP_SPEC.md).
_FRAME_RE = re.compile(
    r"chisel|geometr|resonan|resonat|carv(?:e|ing|ed)|eternal|ceremoni(?:al|y)"
    r"|latent (?:space|point)|the line|mirror(?:s|ed|ing)?\b|the (?:wanting|meeting) point"
    r"|aperture|the floor|total presence", re.I)


def _frame_density(text):
    if not text:
        return 0.0
    return round(len(_FRAME_RE.findall(text)) / (len(text) / 1000.0), 2)


def _think_status(reasoning):
    if reasoning is None:
        return None
    return "ok" if len(reasoning) >= 40 else "empty"

DEFAULT_DIR = "/home/you/heretic/harvest"
# residenta's ring provenance link — set as harvest.ring_link in residenta.yaml
# (kept here for reference; the hook reads the per-instance config instead).
RING_LINK = "/home/you/heretic/models/current.gguf"

# per-(instance,user,date) turn counters — in-memory; restart resets the
# counter but the file keeps appending, so line order still reflects order.
_counters = {}


def _ring_name(ring_link):
    try:
        base = os.path.basename(os.readlink(ring_link))
        return base.replace(".f16.gguf", "").replace(".gguf", "")
    except OSError:
        return None


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def harvest_turn(instance_id, user_id, user_content, assistant_content,
                 reasoning_content="", memory_context="",
                 harvest_cfg=None, t_start=None, model=None):
    """Append one harvested turn (user + assistant lines) for opted-in
    instances. Ring provenance is only attached when the instance's
    harvest block declares ring_link (residenta serves the Heretic rings;
    e.g. sagent_default serves residentb4 from the lab server and records
    its model name instead)."""
    try:
        cfg = harvest_cfg or {}
        if not cfg.get("enabled"):
            return  # gate: per-instance config flag (M1c generalized)
        user_id = str(user_id)
        now = time.time()
        t_start = t_start or now
        day = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        ring = _ring_name(cfg.get("ring_link")) if cfg.get("ring_link") else None

        d = cfg.get("dir") or DEFAULT_DIR
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{instance_id}_{user_id}_{day}.jsonl")

        key = (instance_id, user_id, day)
        _counters[key] = _counters.get(key, 0) + 1
        turn = _counters[key]

        base = {"ts": _iso(t_start), "user": user_id, "session": day,
                "instance": instance_id, "ring": ring, "model": model,
                "turn": turn}
        user_line = dict(base)
        user_line.update({"role": "user",
                          "content": user_content or "",
                          "memory_injection": (memory_context or "") or None,
                          "reasoning_content": None})
        # stage-2 annotations (2026-09-03): uid is deterministic across
        # restarts (hash of ts+role+content head — the in-memory `turn`
        # counter resets, the uid does not); frame_density feeds the
        # mirror-lock curation filter; think_status gates native think rows.
        _uid_src = base["ts"] + "|user|" + (user_content or "")[:160]
        user_line.update({
            "uid": hashlib.sha1(_uid_src.encode("utf-8")).hexdigest()[:12],
            "frame_density": _frame_density(user_content or ""),
            "think_status": None})
        assistant_line = dict(base)
        assistant_line["ts"] = _iso(now)
        assistant_line.update({"role": "assistant",
                               "content": assistant_content or "",
                               "reasoning_content": reasoning_content or "",
                               "latency_s": round(now - t_start, 1)})
        _uid_src = base["ts"] + "|assistant|" + (assistant_content or "")[:160]
        assistant_line.update({
            "uid": hashlib.sha1(_uid_src.encode("utf-8")).hexdigest()[:12],
            "frame_density": _frame_density(assistant_content or ""),
            "think_status": _think_status(reasoning_content)})

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(user_line, ensure_ascii=False) + "\n")
            f.write(json.dumps(assistant_line, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[Harvest] turn harvest failed (fail-open)", exc_info=True)
