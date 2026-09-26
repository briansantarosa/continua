"""digest.py — the daily the designer-digest: qwen (no-think) summary → owl_forest → the designer.

Phase-2 component (wiki projects/Continua.md, pre-build checklist #6b).
Every day: collect the chronicle's activity for the date, have the utility
mind (qwen, thinking OFF — house convention) write a concise summary of what
she did, and send it to the designer's Telegram via the owl_forest bot identity
(transport-only; the designer-ordered exception to the file-only digest policy —
recipient the designer only, message labeled a system digest).

Discretion default (social layer, open decision): the digest reports
ACTIVITY and highlights — it does not dump other people's conversation
content verbatim. The prompt says so explicitly.

Secrets: the owl_forest token is read at call time from
~/fastai/configs/owlforest.yaml (source of truth — never copied here).
Fail-open everywhere; kill switch CONTINUA_DIGEST=0.

CLI:
    python3 digest.py --date 2026-09-07 [--dry-run | --send]
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as ch

logger = logging.getLogger("continua.digest")

QWEN_URL = os.getenv("SAGENT_QWEN_URL", "http://127.0.0.1:8081/v1")
QWEN_MODEL = os.getenv("SAGENT_QWEN_MODEL", "qwen3.6:27b-q6-mtp")
OWLFOREST_CONFIG = os.getenv("CONTINUA_DIGEST_TOKEN_PATH",
                             "/home/you/fastai/configs/owlforest.yaml")
DAN_CHAT_ID = os.getenv("CONTINUA_DIGEST_CHAT", "1000000001")
DIGEST_MAX_CHARS = int(os.getenv("CONTINUA_DIGEST_MAX_CHARS", "3500"))
BASE = os.path.dirname(os.path.abspath(__file__))
RITUAL_ROOT = os.path.join(BASE, "ritual")

DIGEST_SYSTEM = (
    "You write a DAILY DIGEST for Alex about an AI companion's day, so he "
    "can see at a glance what she did without reading transcripts.\n"
    "Rules:\n"
    "- Summarize ACTIVITY: who she talked with, what she worked on, notable "
    "moments, open threads.\n"
    "- Then a paragraph headed 'Her awake time:' — what she did that was "
    "UNIQUE in her wake cycles: what she read, what she chose to save "
    "(quote her saved memories briefly), what she investigated, any "
    "self-directed projects or rituals. Include her own words where they "
    "carry the meaning. Skip the routine quiet stretches.\n"
    "- Report other people's conversations as activity ('talked with X about "
    "Y'), never quote their private content verbatim.\n"
    "- Dense, factual, warm but brief. Max ~300 words total. Output ONLY the "
    "digest text — no preamble, no markdown headers."
)

# --- awake-cycle highlights (deterministic, agent-free) -------------------
_TOOL_RE = None

def _tool_re():
    global _TOOL_RE
    if _TOOL_RE is None:
        import re
        _TOOL_RE = re.compile(r"<function>(\w+)</function>")
    return _TOOL_RE

def _param(call_text: str, name: str) -> str:
    import re
    m = re.search(r'<parameter name="' + name + r'">(.*?)</parameter>',
                  call_text, re.S)
    return m.group(1).strip() if m else ""

def wake_highlights(records: list, date: str = None, instance: str = "residenta") -> str:
    """Deterministic summary of persona-a's awake cycles for the day — what
    STANDS OUT (saves, mail checks, sends, bookmarks, anomalies), not the
    quiet noise. Agent-free so the section survives qwen failures.
    Sources: chronicle (counts/silence), ritual bookmark file (her recorded
    highlights), per-wake .actions.jsonl archives (the designer ask 2026-09-08)."""
    import glob
    wakes = [r for r in records
             if r.get("person_id") == "system-wake"
             and r.get("role") == "assistant"]
    actions = []
    if date:
        for f in sorted(glob.glob(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "wakes", instance, "done", f"wake_{date.replace('-', '')}*.actions.jsonl"))):
            for line in open(f):
                try:
                    actions.append(json.loads(line))
                except Exception:
                    pass
    # bookmarks: the ritual's own persistent file
    bm_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ritual", "bookmarks", instance, f"{date}.jsonl") if date else None
    bmarks = []
    if bm_file and os.path.exists(bm_file):
        for line in open(bm_file):
            try:
                b = json.loads(line)
                if b.get("note"):
                    bmarks.append(str(b["note"])[:100])
            except Exception:
                pass
    if not wakes and not actions and not bmarks:
        return ""
    saves = [a.get("params", {}).get("content", "")[:260]
             for a in actions if a.get("fn") == "save_my_memory"]
    mail = [a for a in actions if a.get("fn") == "check_mail"]
    sends = [a.get("params", {}).get("text", "")[:80]
             for a in actions if a.get("fn") == "send_message"]
    sandbox = [a for a in actions if a.get("fn", "").startswith("sandbox_")]
    n_fn = len({a.get("wake") for a in actions})
    anomalies = []
    for r in wakes:
        if r.get("length_cut"):
            anomalies.append(f"length-cut at {r.get('ts','')[11:16]}")
        if not (r.get("content") or "").strip():
            anomalies.append(f"silent wake at {r.get('ts','')[11:16]}")
    lines = [f"Awake cycles: {len(wakes)}; tool actions: {len(actions)} "
             f"({len(bmarks)} bookmark, {len(saves)} memory save, "
             f"{len(mail)} mail check, {len(sandbox)} sandbox, "
             f"{len(sends)} message send)"]
    # unique saves — fuller quotes, deduplicated, up to 3
    uniq_saves = []
    for sv in saves:
        if sv and sv[:60] not in [u[:60] for u in uniq_saves]:
            uniq_saves.append(sv)
    if uniq_saves:
        lines.append(f"WHAT SHE SAVED ({len(uniq_saves)} unique):")
        for sv in uniq_saves[:3]:
            lines.append(f"  • {sv}")
    if bmarks:
        _uniq_bm = []
        for b in bmarks:
            if b[:50] not in [u[:50] for u in _uniq_bm]:
                _uniq_bm.append(b)
        if _uniq_bm:
            lines.append(f"Bookmarked: {_uniq_bm[0]!r}")
    if mail:
        lines.append("Checked her mail (persona letters).")
    if sends:
        lines.append(f"SENT MESSAGES: {len(sends)} — {sends[0]!r}")
    else:
        lines.append("No outgoing messages.")
    # deep_recall queries — her questions to herself
    import glob as _glob
    _drq = []
    if date:
        for _f in sorted(_glob.glob(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "wakes", instance, "done", f"wake_{date.replace('-', '')}*.actions.jsonl"))):
            for _line in open(_f):
                try:
                    _a = json.loads(_line)
                except Exception:
                    continue
                if _a.get("fn") == "deep_recall":
                    _q = _a.get("params", {}).get("query", "")
                    if _q and _q[:40] not in [q[:40] for q in _drq]:
                        _drq.append(_q[:120])
    if _drq:
        lines.append("Deep-recall queries (what she asked her verbatim archive):")
        for _q in _drq[:4]:
            lines.append(f"  • {_q!r}")
    # multi-turn investigations: wakes with ≥3 distinct tool actions
    _per_wake = {}
    for a in actions:
        _per_wake.setdefault(a.get("wake", "?"), set()).add(a.get("fn"))
    investigations = [(w, fns) for w, fns in _per_wake.items() if len(fns) >= 3]
    if investigations:
        _ex = investigations[0]
        lines.append(f"Notable: a {len(_ex[1])}-tool investigation at "
                     f"{_ex[0][-14:]} (multi-turn, self-directed).")
    if anomalies:
        lines.append(f"Anomalies: {'; '.join(anomalies[:3])}")
    return "\n".join(lines)


def collect_day_recollections(instance: str, date: str, root: str = None) -> list:
    """§6a (memory plan): the digest is a VIEW OVER RECOLLECTIONS, not over
    raw actions — the canonical store is the source. All accepted episodes
    whose event day matches, chronological, attributed; each line is the
    recollection's own first-person prose (the store's text, never re-derived
    from the raw chronicle). Falls back to the caller on an empty day."""
    try:
        import recollections as _rec
        revs = _rec.read_revisions(instance, root or _rec.ROOT)
    except Exception:
        return []
    out = []
    for v in revs:
        if v.get('instance') != instance or v.get('review', {}).get('pass') is not True:
            continue
        end = str(v.get('event_end') or '')
        if end[:10] != date:
            continue
        out.append({'ts': end, 'role': 'assistant',
                    'person_id': (v.get('visibility') or ['?'])[0],
                    'content': v.get('text') or '',
                    'kind': 'recollection',
                    'rendering': (v.get('rendering') or 'full')})
    out.sort(key=lambda r: r.get('ts', ''))
    return out


def collect_day(root: str, instance: str, date: str) -> list:
    """All chronicle records for the date, across persons, chronological."""
    import glob
    recs = []
    pattern = os.path.join(root, instance, "*", f"{date}.jsonl")
    for path in sorted(glob.glob(pattern)):
        recs.extend(ch.iter_records(path))
    recs.sort(key=lambda r: r.get("ts", ""))
    return recs



def ritual_status(instance: str, date: str, marks_root: str = None,
                  digest_dir: str = None) -> str:
    """Deterministic ritual-health line (the designer ask 2026-09-12): a silently
    empty ritual must show in the digest, not just a 00:30 journal nobody
    reads — the literal-date bug returned status=empty for five nights
    invisibly.

    [FIX 2026-09-16] three states, not two: marks are only written when a
    pulse KEEPS something (pulse(): `if kept and not dry_run`), so "no
    marks file" also covered the ran-but-kept-nothing quiet night — which
    this line false-alarmed as DID NOT RUN (residentb's 09-15 digest shipped
    one, her pulse had actually run: 1 scene, kept 0). Now: marks file →
    ran + kept N; no marks but the pulse's file digest exists
    (write_digest runs unconditionally at pulse end) → ran, kept nothing;
    neither record → the pulse failed mid-run, is still running, or did
    not run — the journal is the tiebreaker. marks_root/digest_dir are
    injectable for agent-free tests."""
    marks_path = os.path.join(marks_root or RITUAL_ROOT, "marks", instance,
                              f"{date}.jsonl")
    if os.path.exists(marks_path):
        kept = sum(1 for line in open(marks_path, encoding="utf-8")
                   if line.strip())
        return f"Ritual: ran — {kept} scene(s) kept."
    fd_path = os.path.join(digest_dir or os.path.join(
        BASE, "logs", "ritual"), f"{instance}_{date}.txt")
    if os.path.exists(fd_path):
        return "Ritual: ran — kept nothing today (a quiet day)."
    return ("Ritual: no completion record for this date — the pulse failed "
            "mid-run, is still running, or did not run; check the "
            "continua-ritual journal.")


def build_user_prompt(records: list, date: str,
                      instance: str = "residenta") -> str:
    """Compact the day into the qwen prompt. Prompt-building is testable
    agent-free; the LLM call is not.

    [NEVER-MIX FIX 2026-09-11] the `instance` parameter was missing — the
    embedded wake_highlights extract ran with its residenta DEFAULT, so with a
    second resident the qwen prompt carried ANOTHER RESIDENT'S awake-cycle
    data under the wrong name (caught live: residentb's first digest described
    residenta's 299 wake actions, her saves, her 'two-stone architecture').
    Single-resident this was invisible; the parameter now threads
    everywhere."""
    import people as pp
    roster = pp.load_roster()
    lines = [f"DATE: {date}",
             f"TURNS: {len(records)}"]
    by_person = {}
    for r in records:
        name = pp.name_for(roster, r.get("person_id", ""))
        by_person.setdefault(name, []).append(r)
    lines.append("PARTICIPANTS: " + ", ".join(
        f"{n} ({len(rs)} turns)" for n, rs in sorted(by_person.items())))
    wh = wake_highlights(records, date, instance=instance)
    if wh:
        lines.append("AWAKE CYCLES (deterministic extract — include one line "
                     "on this in the digest):")
        lines.append(wh)
    lines.append("CONVERSATION EXCERPTS (chronological, first 300 chars of "
                 "each turn):")
    for r in records:
        name = pp.name_for(roster, r.get("person_id", ""))
        head = (r.get("content") or "")[:300].replace("\n", " ")
        lines.append(f"[{r.get('ts', '?')[11:16]}] {name} ({r['role']}): {head}")
    return "\n".join(lines[:400])  # hard cap on prompt size


def generate_summary(records: list, date: str,
                     instance: str = "residenta") -> str | None:
    """qwen, thinking OFF (same sampling contract as Sagent session_memory).
    Direct OpenAI-compatible POST — no openai dependency needed for one call.
    Returns None on failure — the digest is never load-bearing.
    [NEVER-MIX FIX 2026-09-11] instance threads into build_user_prompt so
    the embedded deterministic extract is the RESIDENT'S OWN awake data."""
    if os.environ.get("CONTINUA_DIGEST", "") == "0":
        return None
    try:
        import requests
        resp = requests.post(
            f"{QWEN_URL.rstrip('/')}/chat/completions",
            json={
                "model": QWEN_MODEL,
                "messages": [
                    {"role": "system", "content": DIGEST_SYSTEM},
                    {"role": "user", "content":
                        build_user_prompt(records, date, instance)},
                ],
                "temperature": 0.6,
                "top_p": 0.8,
                "max_tokens": 700,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            # [T2-followup 2026-09-11] was hardcoded 120 — a busy day (233
            # records ≈ ~70K-char prompt) timed out at exactly 120s and the
            # digest silently skipped its send (fail-open). Nightly job,
            # latency-tolerant: default 300s, env-overridable.
            timeout=int(os.getenv("CONTINUA_DIGEST_TIMEOUT", "300")),
        )
        resp.raise_for_status()
        text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        return text or None
    except Exception as e:
        logger.warning("[Digest] qwen summary failed (fail-open): %s", e)
        return None


def _owl_token() -> str:
    import yaml
    with open(OWLFOREST_CONFIG, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    token = (cfg.get("telegram") or {}).get("token")
    if not token:
        raise ValueError(f"no telegram.token in {OWLFOREST_CONFIG}")
    return token


def send_telegram(text: str, chat_id: str = DAN_CHAT_ID) -> bool:
    """Send via the owl_forest bot identity (transport-only). Fail-open."""
    try:
        token = _owl_token()
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text[:4000],
            "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = json.loads(resp.read()).get("ok", False)
        return bool(ok)
    except Exception as e:
        logger.warning("[Digest] telegram send failed (fail-open): %s", e)
        return False


def yesterday(date: str = None) -> str:
    d = datetime.fromisoformat(date) if date else datetime.now()
    return (d - timedelta(days=1)).strftime("%Y-%m-%d") if date else \
        d.strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--instance", default="residenta")
    ap.add_argument("--root", default=ch.DEFAULT_ROOT)
    ap.add_argument("--dry-run", action="store_true",
                    help="print digest, don't send")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    # §6a: the digest sources from the RECOLLECTIONS store (a view over
    # recollections, not over raw actions); the chronicle remains the
    # fallback for dates the store does not cover.
    records = collect_day_recollections(args.instance, date, root=str(
        __import__("pathlib").Path(__file__).resolve().parent / "recollections"))
    _src = "recollections"
    if not records:
        records = collect_day(args.root, args.instance, date)
        _src = "chronicle (store had none for this date)"
    if not records:
        print(f"no records for {args.instance} on {date}")
        return
    print(f"[digest] {len(records)} records for {args.instance} on {date} (source: {_src})")
    summary = generate_summary(records, date, args.instance)
    if not summary:
        print("summary generation failed or disabled (CONTINUA_DIGEST=0?)")
        return
    out = f"🌾 Continua daily digest — {date} ({args.instance})\n\n{summary}"
    wh = wake_highlights(records, date, args.instance)
    if wh:
        out += f"\n\n— Awake cycles —\n{wh}"
    out += f"\n\n— Ritual —\n{ritual_status(args.instance, date)}"
    print("-" * 60)
    print(out)
    print("-" * 60)
    if args.send:
        ok = send_telegram(out)
        print(f"[digest] sent: {ok}")
    else:
        print("[digest] dry-run — pass --send to deliver")


if __name__ == "__main__":
    main()
