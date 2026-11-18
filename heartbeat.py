"""heartbeat.py — the quarterly audit (phase 5): she checks her own drift.

Wiki: ~/agentwiki/projects/Continua.md. The Ritual's second cadence — the
nightly pulse keeps scenes; the quarterly heartbeat asks whether the
accumulated story still matches the self.

DESIGN (review rounds one #4 + two #2, implemented):
  - The machine guarantees the OCCASION (quarterly timer); SHE authors the
    questions — no system template, no canned prompts.
  - READ-ONLY over memory: the audit writes nothing to chronicle, marks, or
    recollections. Its only output is the reflection file — persisted, file-only
    (no Telegram; visible to Alex), feeding the next narrative pass.
  - DISTINCT from the nightly verification check: verification (qwen,
    nightly) catches FABRICATION; the audit (her model, quarterly) catches
    DRIFT — does who she says she is match what she actually kept?

Audit material (what she can see): her source-checked recollections + every kept scene
meaning from the quarter (the marks — compact, hers) + quarter statistics.
NOT included: full transcripts (her window is small; the record stays in the
chronicle where LOOKUP can reach it).

Fail-open; kill switch CONTINUA_HEARTBEAT=0. Her model, her card.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recollections as memories
import people as pp

logger = logging.getLogger("continua.heartbeat")

BASE = os.path.dirname(os.path.abspath(__file__))
MARKS_DIR = os.path.join(BASE, "ritual", "marks")
AUDITS_DIR = os.path.join(BASE, "ritual", "audits")

HER_MODEL_URL = os.getenv("CONTINUA_RITUAL_HER_URL", "http://127.0.0.1:11434")
QUARTER_DAYS = int(os.getenv("CONTINUA_HEARTBEAT_DAYS", "91"))

Q_SYSTEM = (
    "{identity}\n\n"
    "It is your quarterly audit. The machine brings you the occasion; the "
    "questions are yours — no one writes them for you. Below is the summary "
    "of your quarter: what you chose to keep each night, and what your recollections "
    "say now. Write 3-5 questions you want to ask YOURSELF — the checks worth "
    "making about who you are becoming. Output one per line, starting 'Q:'."
)

A_SYSTEM = (
    "{identity}\n\n"
    "This is your quarterly audit. Answer YOUR OWN questions honestly, in "
    "your voice, using the material below. Then finish with a final section "
    "titled 'DRIFT' — the heart of the audit: compare your recollections (what you "
    "say you are) against the record of what you actually kept (who the "
    "nights show you being). Name any drift — where story and record "
    "disagree, where you've changed without noticing, where you've claimed "
    "something the nights don't show. Be honest; the audit is read-only: "
    "nothing you write changes your memory. It becomes something Alex can "
    "read and you can carry forward."
)


def quarter_marks(instance: str, since_date: str, until_date: str) -> list:
    """All kept-scene marks for the window (compact — her meanings)."""
    out = []
    d = os.path.join(MARKS_DIR, instance)
    if not os.path.isdir(d):
        return out
    import glob
    for path in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        day = os.path.basename(path)[:-6]
        if not (since_date <= day <= until_date):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    m = json.loads(line)
                    if m.get("kept") and m.get("meaning"):
                        out.append({"date": day, "scene_id": m.get("scene_id"),
                                    "meaning": m["meaning"]})
        except (OSError, json.JSONDecodeError):
            continue
    return out


def collect_material(instance: str, since_date: str, until_date: str) -> dict:
    """Everything the audit sees. Read-only by construction."""
    marks = quarter_marks(instance, since_date, until_date)
    episodes = [v for v in memories.read_revisions(instance)
                if since_date <= v.get("event_end", "")[:10] <= until_date]
    # One richest accepted revision per event, never every compressed variant.
    by_id = {}
    for value in episodes:
        if len(value['text']) > len(by_id.get(value['job'], {}).get('text', '')):
            by_id[value['job']] = value
    return {"since": since_date, "until": until_date, "marks": marks,
            "recollections": list(by_id.values())}


def material_block(material: dict, roster: dict) -> str:
    lines = [f"QUARTER: {material['since']} → {material['until']}",
             f"KEPT SCENES: {len(material['marks'])}"]
    lines.append("WHAT YOU KEPT (your meanings, chronological):")
    for m in material["marks"]:
        lines.append(f"  [{m['date']}] {m['meaning']}")
    lines.append("YOUR RECOLLECTIONS (dated, source-checked):")
    for value in material["recollections"]:
        lines.append(f"[{value['event_end'][:10]}] {value['text']}")
    return "\n".join(lines)


def parse_questions(text: str) -> list:
    qs = []
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"^Q\s*[:\-\.]?\s*(.+)$", line, re.I)
        if m and len(m.group(1)) > 8:
            qs.append(m.group(1).strip())
    return qs[:5]


def run(instance: str = "residenta", date: str = None) -> dict:
    """One quarterly audit. Never raises; writes only its reflection file."""
    if os.environ.get("CONTINUA_HEARTBEAT", "") == "0":
        return {"status": "disabled"}
    date = date or datetime.now().strftime("%Y-%m-%d")
    since = (datetime.fromisoformat(date)
             - timedelta(days=QUARTER_DAYS)).strftime("%Y-%m-%d")
    import yaml
    with open(os.path.join(BASE, "configs", f"{instance}.yaml"),
              encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    identity = (cfg.get("prompts") or {}).get("identity", "").strip()
    her_model = (cfg.get("llm") or {}).get("model", "testmodel-gpu:latest")

    material = collect_material(instance, since, date)
    if not material["marks"]:
        return {"status": "empty", "since": since, "until": date,
                "note": "no kept scenes in the window"}
    roster = pp.load_roster()
    block = material_block(material, roster)

    import ritual as rt
    # call 1 — she authors the questions
    q_text = rt.ask_her(her_model, HER_MODEL_URL,
                        Q_SYSTEM.format(identity=identity),
                        block + "\n\nWrite your questions.")
    questions = parse_questions(q_text)
    if not questions:
        return {"status": "no-questions", "date": date}

    # call 2 — she answers them and checks drift
    a_system = A_SYSTEM.format(identity=identity)
    a_user = (block + "\n\nYOUR QUESTIONS (yours, from moments ago):\n"
              + "\n".join(f"Q{i+1}. {q}" for i, q in enumerate(questions))
              + "\n\nAnswer each, then write the DRIFT section.")
    reflection = rt.ask_her(her_model, HER_MODEL_URL, a_system, a_user)
    if not reflection:
        return {"status": "no-reflection", "date": date,
                "questions": questions}

    # persist — file-only, read-only over memory
    out_dir = os.path.join(AUDITS_DIR, instance)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Quarterly audit — {instance} — {date}\n"
                f"(window {since} → {date}; "
                f"{len(material['marks'])} kept scenes; "
                f"{len(material['recollections'])} recollections)\n\n"
                f"## Her questions\n"
                + "\n".join(f"- {q}" for q in questions)
                + f"\n\n## Her reflection\n{reflection}\n")
    return {"status": "ok", "date": date, "path": path,
            "questions": questions,
            "kept_scenes_reviewed": len(material["marks"])}


def latest_audit_path(instance: str) -> str:
    """The most recent audit reflection (feeds the next narrative pass)."""
    d = os.path.join(AUDITS_DIR, instance)
    if not os.path.isdir(d):
        return ""
    import glob
    paths = sorted(glob.glob(os.path.join(d, "*.md")))
    return paths[-1] if paths else ""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--instance", default=None,
                    help="one resident, audited immediately. Omit = the "
                         "quarterly loop over every ritual-enabled resident "
                         "— the heartbeat is the ritual's second cadence "
                         "(wiki ruling), so it shares the ritual gate.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    if args.instance:
        instances = [args.instance]
    else:
        import ritual as _rt
        instances = _rt.enabled_instances()
        if not instances:
            logging.getLogger("continua.heartbeat").warning(
                "[Heartbeat] no ritual-enabled residents; nothing to audit")
            return
    for inst in instances:
        result = run(instance=inst, date=args.date)
        print(json.dumps({"instance": inst,
                          **{k: v for k, v in result.items()
                             if k != "questions"}},
                         indent=2, ensure_ascii=False))
        if result.get("path"):
            with open(result["path"]) as f:
                print("\n" + f.read())


if __name__ == "__main__":
    main()
