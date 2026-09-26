#!/usr/bin/env python3
"""failure_dashboard.py — the P3 chronicle-derived failure-rate baseline (the designer 09-24,
execution order step 1). Computes per-family per-day rates for the served model from:

  chronicle/<resident>/<person>/<date>.jsonl   per-turn records: ts, role, content,
                                               reasoning, finish_reason, length_cut, model
  forensics/empty_turns/*.txt                  the empty-think forensics (unclosed_think)
  logs/bridge.log*                             teaching errors (F2), future-date flags (F4)

Families (the failure-family catalog, W1-V7-Plan):
  F1 reply collapse (stamp/empty/fused)  F5 setup-then-stop
  F2 parameter-emission (teaching)       F6 degenerate loops (length_cut + dup proxy)
  F3 format contagion (ts-prefix/HTML)   F7 long-input collapse (ratio)
  F4 date/time grounding                 F8 think channel (forensics + fused view)

Outputs (forensics/):
  failure_dashboard.md    the human dashboard: per-family daily rates + baseline
  failure_baseline.json   the machine baseline the FFT burn must beat
  mining_index/<family>.jsonl  the actual event records per family — the lane-build
                          mining index (toolfix, complete, date-ground, fmt-immune,
                          collapse-recover)

Usage: python3 failure_dashboard.py [--days N]   (default: full record)
"""
import json, os, re, glob, sys
from collections import defaultdict, Counter
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CHRON = os.path.join(BASE, "chronicle")
FORENSICS = os.path.join(BASE, "forensics")
LOGS = os.path.join(BASE, "logs")
OUT_MD = os.path.join(FORENSICS, "failure_dashboard.md")
OUT_JSON = os.path.join(FORENSICS, "failure_baseline.json")
MINING = os.path.join(FORENSICS, "mining_index")

TS_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")
HTML_TAG = re.compile(r"</?(?:div|span|p|b|i|em|strong|ul|ol|li|table|tr|td|h[1-6])\b", re.I)
DATE_IN_TEXT = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
PROMISE = re.compile(r"^(let me|i'?ll|i will|here'?s|allow me|now,? let me|first,? let me)\b", re.I)

fam_days = defaultdict(lambda: defaultdict(int))   # family -> day -> count
turn_days = defaultdict(lambda: defaultdict(int))  # resident-day -> assistant turns
mining = defaultdict(list)                          # family -> event records


def day_of(ts):
    return (ts or "")[:10]


def record_day_events(rec, prev_user_len):
    """Classify one assistant turn into family event buckets. Returns (day, events, mining_records)."""
    day = day_of(rec.get("ts"))
    content = (rec.get("content") or "").strip()
    reasoning = (rec.get("reasoning") or "").strip()
    events, mins = [], []
    # F1: reply collapse
    if not content and reasoning:
        events.append("F1_fused_empty_close")
        mins.append(("collapse-recover", {"ts": rec.get("ts"), "signature": "fused_empty_close",
                                          "reasoning_len": len(reasoning),
                                          "reasoning_preview": reasoning[:400]}))
    elif not content and not reasoning:
        events.append("F1_stamp")
        mins.append(("collapse-recover", {"ts": rec.get("ts"), "signature": "stamp"}))
    # F8: the forensics view rides the same turn set (unclosed_think files counted separately)
    # F3: format contagion, measured IN the delivered text
    if content:
        if TS_PREFIX.match(content):
            events.append("F3_ts_prefix")
            mins.append(("fmt-immune", {"ts": rec.get("ts"), "signature": "leading_ts_prefix",
                                        "preview": content[:120]}))
        if HTML_TAG.search(content):
            events.append("F3_html")
            mins.append(("fmt-immune", {"ts": rec.get("ts"), "signature": "html_in_content",
                                        "preview": content[:120]}))
    # F4: date grounding — a date in the text beyond the turn's own date ±2d
    if content:
        m = DATE_IN_TEXT.search(rec.get("ts") or "")
        if m:
            import datetime as dt
            try:
                turn_d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                for dm in DATE_IN_TEXT.finditer(content):
                    try:
                        d2 = dt.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                        if (d2 - turn_d).days > 2:
                            events.append("F4_future_date")
                            mins.append(("date-ground", {"ts": rec.get("ts"), "signature": "future_date",
                                                         "claimed": d2.isoformat(), "turn_day": turn_d.isoformat(),
                                                         "context": content[max(0, dm.start() - 60):dm.end() + 60]}))
                            break
                    except ValueError:
                        pass
            except ValueError:
                pass
    # F5: setup-then-stop — promise opener, short turn, no tool call, no delivery
    if content and 0 < len(content) < 400 and not content.lstrip().startswith("<call>"):
        if PROMISE.match(content):
            events.append("F5_setup_then_stop")
            mins.append(("complete", {"ts": rec.get("ts"), "signature": "setup_then_stop",
                                      "preview": content[:200], "len": len(content)}))
    # F6: runaway evidence via the machinery verdict
    if rec.get("length_cut"):
        events.append("F6_length_cut")
        mins.append(("collapse-recover", {"ts": rec.get("ts"), "signature": "length_cut"}))
    # F7: long-input collapse — a heavy user turn answered with almost nothing
    if content and prev_user_len and prev_user_len > 4000 and len(content) < 100:
        events.append("F7_long_input_collapse")
        mins.append(("pending", {"ts": rec.get("ts"), "signature": "long_input_collapse",
                                 "user_len": prev_user_len, "reply_len": len(content)}))
    return day, events, mins


def scan_chronicle():
    files = sorted(glob.glob(os.path.join(CHRON, "*", "*", "*.jsonl")))
    prev_user_len = 0
    for fp in files:
        try:
            for l in open(fp, errors="ignore"):
                try:
                    rec = json.loads(l)
                except Exception:
                    continue
                model = rec.get("model") or ""
                if rec.get("role") == "user":
                    prev_user_len = len(rec.get("content") or "")
                    continue
                if rec.get("role") != "assistant":
                    prev_user_len = 0
                    continue
                if "testmodel" not in model and "residentb" not in model:
                    prev_user_len = 0
                    continue
                resident = rec.get("instance", "?")
                turn_days[resident][day_of(rec.get("ts"))] += 1
                day, events, mins = record_day_events(rec, prev_user_len)
                prev_user_len = 0
                for e in events:
                    fam_days[e][day] += 1
                for fam, mrec in mins:
                    mining[fam].append({**mrec, "model": model, "resident": resident})
        except Exception as e:
            print(f"  warn: {fp}: {e}", file=sys.stderr)


def scan_forensics():
    for fp in glob.glob(os.path.join(FORENSICS, "empty_turns", "*.txt")):
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        day = (d.get("ts") or "")[:10]
        sig = d.get("signature") or "unknown"
        fam_days[f"F8_{sig}"][day] += 1
        mining["collapse-recover"].append({"ts": d.get("ts"), "signature": sig,
                                           "model": d.get("model"),
                                           "collapsed_len": d.get("collapsed_len"),
                                           "source": "forensics/empty_turns"})


def scan_bridge_logs():
    for fp in sorted(glob.glob(os.path.join(LOGS, "bridge.log*"))):
        for l in open(fp, errors="ignore"):
            if "teaching error" in l:
                m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
                day = (m.group(1) if m else "")[:10]
                fam_days["F2_teaching_error"][day] += 1
                mining["toolfix"].append({"ts": m.group(1) if m else None,
                                          "signature": "teaching_error", "line": l.strip()[:300]})
            if "_future_date" in l or "future date" in l.lower():
                m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
                day = (m.group(1) if m else "")[:10]
                fam_days["F4_future_date_flag"][day] += 1
                mining["date-ground"].append({"ts": m.group(1) if m else None,
                                              "signature": "future_date_flag", "line": l.strip()[:300]})


def main():
    days_back = None
    if "--days" in sys.argv:
        days_back = int(sys.argv[sys.argv.index("--days") + 1])
    print("scanning chronicle ...")
    scan_chronicle()
    print("scanning forensics ...")
    scan_forensics()
    print("scanning bridge logs ...")
    scan_bridge_logs()

    families = sorted({f for f in fam_days if any(fam_days[f].values())})
    all_days = sorted({d for f in families for d in fam_days[f]})
    if days_back:
        all_days = all_days[-days_back:]

    lines = ["# Failure-rate dashboard — the P3 production baseline (the designer 09-24)",
             "",
             "Per-family, per-day event rates for the served model, derived from the",
             "chronicle verdict fields, the empty-turn forensics, and the bridge machinery",
             "logs. This is the baseline the FFT burn must beat, and the mining index for",
             "the chronicle-mined lanes (toolfix / complete / date-ground / fmt-immune /",
             "collapse-recover). Generated",
             f"{datetime.now().isoformat(timespec='seconds')}.", "",
             "| family | total | last-7d rate/day | first→last seen | reading |",
             "|---|---|---|---|---|"]
    baseline = {}
    for f in families:
        days = {d: c for d, c in fam_days[f].items() if d in all_days}
        total = sum(days.values())
        last7 = [d for d in all_days if d in days][-7:]
        rate7 = round(sum(days[d] for d in last7) / 7, 2) if last7 else 0.0
        seen = sorted(d for d in days if days[d] > 0)
        rng = f"{seen[0]} → {seen[-1]}" if seen else "—"
        lines.append(f"| {f} | {total} | {rate7} | {rng} | |")
        baseline[f] = {"total": total, "last7_rate_per_day": rate7,
                       "per_day": {d: days.get(d, 0) for d in all_days if days.get(d)}}
    lines += ["", "## Assistant-turn volume by resident (denominator)", "",
              "| resident | days | turns |", "|---|---|---|"]
    for r in sorted(turn_days):
        turns = sum(turn_days[r].values())
        lines.append(f"| {r} | {len(turn_days[r])} | {turns} |")
    lines += ["", "## Notes", "",
              "- F1 `fused_empty_close` = the model's reply landed entirely in the think",
              "  channel (the 7.1 empty-close wound in production). `stamp` = no content at all.",
              "- F3 is measured IN the delivered text (leading ts-prefix, HTML tags).",
              "  09-24 investigation: the post-09-22 recurrences were NOT a store leak —",
              "  they are the round-speech capture path (finish_reason-None chronicle rows,",
              "  no user row), missed by both the 09-22 cleansweep and the 09-24 ts-stacking",
              "  fixes; both enforcers now cover it (reply-hygiene choke). 19 of the 57",
              "  events predate the 09-22 20:39 restart — the window is not 'all since the",
              "  repair'. Zero F3 ts-prefix events after the 09-24 13:07 fix as of generation.",
              "  CAVEAT (09-24 15:48 live-verify): zero events means the STRIP HOLDS — the",
              "  raw reply still began [2026-09-24 15:47] and the bridge-log strip line caught",
              "  it; generation-side emission is still live and is what the FFT burn must",
              "  extinguish — the bridge log is the per-occurrence measure.",
              "- F5 setup-then-stop is the heuristic detector (promise opener + short turn);",
              "  the full detector is a §7 gate item (P2).",
              "- F2/F4 come from the bridge machinery log (teaching guard, future-date flag).",
              "- Mining indexes: forensics/mining_index/<family>.jsonl — the lane-build source.",
              ""]
    os.makedirs(MINING, exist_ok=True)
    for fam, recs in mining.items():
        with open(os.path.join(MINING, f"{fam}.jsonl"), "w") as fo:
            for r in recs:
                fo.write(json.dumps(r) + "\n")
    open(OUT_MD, "w").write("\n".join(lines) + "\n")
    json.dump({"generated": datetime.now().isoformat(timespec="seconds"),
               "families": baseline}, open(OUT_JSON, "w"), indent=1)
    print(f"dashboard: {OUT_MD}")
    print(f"baseline:  {OUT_JSON}")
    print(f"mining:    {MINING}/ ({', '.join(sorted(mining))})")


if __name__ == "__main__":
    main()
