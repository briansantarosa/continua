"""summary.py — the rolling situational summaries (Layered Context Summaries).

the designer's design 2026-09-08: the cross-context layer of persona-a's memory is
MAINTAINED SUMMARIES, not fact retrieval. Two blocks, regenerated from the
chronicle (pure functions of the record — never incrementally edited):

  rolling_24h.md — the last 24 hours, in depth. Rolls at every wake
                   consumption (and manually via CLI).
  rolling_2d.md  — the last ~48 hours, brief. Rolls daily (the ritual).

Register rule (critical): neutral event lines. NEVER third-person narration
about "the user" — that register in her context primes the loops (see the
Ring-6.1 plan). Fail-open everywhere; kill switch CONTINUA_SUMMARY=0.

CLI:
    python3 summary.py --instance residenta [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as ch

logger = logging.getLogger("continua.summary")

QWEN_URL = os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1")
QWEN_MODEL = os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp")
SUMMARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "summaries", "{instance}")
# register rule: this prompt is where the analyst-voice war is won or lost
# {persona} placeholder (2026-09-12): the prompt hardcoded "persona-a" and every
# resident's roll attributed her own actions to persona-a — residentb's summary said
# "persona-a searched logs..." in residentb's context block. Name must follow the
# instance being rolled.
SYSTEM = (
    "You maintain a rolling situational summary for an AI companion "
    "({persona}) "
    "so she knows what has been happening in her life.\n"
    "Rules:\n"
    "- Neutral factual EVENT LINES. Name people plainly (Alex, Maggie...).\n"
    "- NEVER write 'The user asked...' or narrate about 'the user' in third "
    "person. Write events as they happened: 'Talked with Alex about X.', "
    "'Ran a continuity check at noon.', 'Letters from Maggie unread.'\n"
    "- ATTRIBUTION IS MANDATORY: every line states who said or did what. "
    "Speech and positions belong to their speaker ('Alex apologized', "
    "'persona-a declined') — never blend two people's parts into one line.\n"
    "- ONE event per line, each line starting with its timestamp "
    "([YYYY-MM-DD HH:MM] ...) mirroring the event lines given. Never merge "
    "separate exchanges into one line; keep different interactions "
    "distinct.\n"
    "- Most important and most recent first. Cover conversations (who, about "
    "what), her wake activities, letters, saved memories, anything notable.\n"
    "- No analysis, no feelings-attribution, no advice. Facts only.\n"
    "- Output ONLY the summary text — no preamble, no headers."
)


def _collect_window(instance: str, hours: int) -> list:
    """Chronicle records for the last `hours` hours, across persons,
    deduplicated (dual-write parity), chronological. Includes wake turns."""
    import glob
    cut = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    recs = []
    seen = set()
    pattern = os.path.join(ch.DEFAULT_ROOT, instance, "*", "*.jsonl")
    for f in glob.glob(pattern):
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("ts", "")
            if ts < cut:
                continue
            k = (ts, r.get("role"), (r.get("content") or "")[:60])
            if k in seen:
                continue
            seen.add(k)
            recs.append(r)
    recs.sort(key=lambda r: r.get("ts", ""))
    return recs


def _render_events(records: list, cap: int = 24000) -> str:
    """Compact event lines for the summarizer (user + assistant + wakes).

    NEWEST FIRST (approved fix 2026-09-12): _collect_window returns
    records oldest-first, and the old 9000-char cap kept the OLDEST slice —
    a staleness ratchet (residentb's 24h summary froze at 00:16-00:36 of a
    407-record day; everything later never reached the summarizer). The
    cap must keep the RECENT end. Per-record preview 280 -> 160 chars so
    ~2x more events fit under the cap; the summarizer's output register
    ('most important and most recent first') already matches this order."""
    import people as pp
    try:
        roster = pp.load_roster()
    except Exception:
        roster = {}
    lines = []
    for r in reversed(records):
        name = r.get("person_id", "?")
        try:
            name = roster[name].display_name
        except Exception:
            if name == "system-wake":
                name = "her wake"
        c = (r.get("content") or "")[:160].replace("\n", " ")
        lines.append(f"[{r.get('ts','?')[5:16]}] ({name}/{r.get('role')}) {c}")
    return "\n".join(lines)[:cap]


def _persona_name(instance: str) -> str:
    """Persona display name from her config identity ("Your name is X").
    Fallback: the instance id itself."""
    try:
        import yaml as _yaml
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "configs", f"{instance}.yaml"),
                  encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
        import re as _re
        m = _re.search(r"Your name is (\w+)",
                       (cfg.get("prompts") or {}).get("identity") or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return instance


def _qwen_summarize(events: str, scope: str, max_tokens: int,
                    keep_lines: str = "(none)",
                    persona: str = "the companion") -> str | None:
    if os.environ.get("CONTINUA_SUMMARY", "") == "0":
        return None
    try:
        import requests
        resp = requests.post(
            f"{QWEN_URL.rstrip('/')}/chat/completions",
            json={
                "model": QWEN_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM.format(
                        persona=persona)},
                    {"role": "user", "content":
                        f"SCOPE: {scope}\n\n"
                        f"SCENES persona-a MARKED AS SIGNIFICANT (priority — these "
                        f"must be represented if their events fall in scope):\n"
                        f"{keep_lines}\n\n"
                        f"EVENT LINES:\n{events}"},
                ],
                "temperature": 0.6,
                "top_p": 0.8,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=120,
        )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        return text or None
    except Exception as e:
        logger.warning("[Summary] qwen roll failed (fail-open): %s", e)
        return None


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def roll(instance: str = "residenta") -> dict:
    """Roll both summaries. Fail-open: on qwen failure the previous file
    stands (a summary is a view; the chronicle is the record)."""
    # her keeps (ritual salience marks) steer the curation — the meaning-maker
    # decides what matters; the summary carries it (salience answer 09-08).
    try:
        import heartbeat as _hb
        _keeps = _hb.quarter_marks(instance, "2000-01-01", "2999-12-31")[-8:]
        _keep_lines = "\n".join(f"- [{k['date']}] {k['meaning']}"
                                for k in _keeps) or "(none)"
    except Exception:
        _keep_lines = "(none)"
    results = {}
    for hours, fname, scope, mtok in (
            (24, "rolling_24h.md", "the last 24 hours, in depth", 600),
            (48, "rolling_2d.md", "the last ~48 hours, briefly (older events "
             "compressed harder; skip anything already covered in detail)", 350)):
        recs = _collect_window(instance, hours)
        if not recs:
            results[fname] = "no-records"
            continue
        events = _render_events(recs)
        text = _qwen_summarize(events, scope, mtok, keep_lines=_keep_lines,
                                persona=_persona_name(instance))
        path = str(SUMMARY_DIR).format(instance=instance)
        path = os.path.join(path, fname)
        if text:
            _write_atomic(path, text)
            results[fname] = f"rolled ({len(text)} chars, {len(recs)} records)"
        else:
            results[fname] = "kept previous (qwen failed or disabled)"
    return results


def load_summaries(instance: str = "residenta") -> str:
    """The injection block: the two summaries, layered. Returns "" when
    nothing is on disk (fail-open; the prompt just omits the layer)."""
    d = str(SUMMARY_DIR).format(instance=instance)
    parts = []
    p24 = os.path.join(d, "rolling_24h.md")
    p2d = os.path.join(d, "rolling_2d.md")
    if os.path.exists(p24):
        t = open(p24, encoding="utf-8").read().strip()
        if t:
            parts.append(f"THE LAST 24 HOURS:\n{t}")
    if os.path.exists(p2d):
        t = open(p2d, encoding="utf-8").read().strip()
        if t:
            parts.append(f"BEFORE THAT (recent days, briefly):\n{t}")
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="residenta")
    ap.add_argument("--dry-run", action="store_true",
                    help="print summaries, don't roll")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    if args.dry_run:
        block = load_summaries(args.instance)
        print(block if block else "(no summaries on disk)")
        return
    results = roll(args.instance)
    for k, v in results.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
