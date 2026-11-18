"""her_words_share — Phase M (memorybuild2 §4, ratified with R39–R43 on
2026-09-30): the authorship metric. Measures — from the document and the
store's provenance, never guessed from text shape — the byte share of
each load-bearing section that is HER verbatim words: kept lines,
bookmark notes, anchor whys, essences. The episode prose beneath stays
machine (R39: admission is a machinery stopgap; the metric doubles as
its retirement condition).

v1 accepts an honest approximation; the method is documented in the
output. Direction, not precision: this is a ramp to watch, not a target.
Deterministic: the same store + choice files produce the same shares.
"""
import json
import os

import memory_doc as _MD
import recollections as _R


def _her_strings(instance, root=None, revs=None):
    """Her verbatim source strings, from HER choice surfaces only:
    kept marks' meanings, her bookmark notes, anchor whys, essences."""
    out = set()
    base = _MD._ritual_base(root)
    for sub, want in (("marks", True), ("bookmarks", "her")):
        for row in _MD._load_jsonl_dir(
                os.path.join(base, sub, instance)):
            if want is True and row.get("kept") is not True:
                continue
            if want == "her" and row.get("by") != "her":
                continue
            text = str(row.get("meaning") or row.get("note") or "").strip()
            if len(text) >= 8:
                out.add(text)
    for why in (_R.anchor_whys(instance, root=root) or {}).values():
        why = str(why or "").strip()
        if len(why) >= 8:
            out.add(why)
    revs = revs if revs is not None else _R.read_revisions(
        instance, root=root)
    for v in revs:
        if (v.get("instance") == instance
                and v.get("review", {}).get("pass") is True
                and v.get("reason") == "essence"):
            t = str(v.get("text") or "").strip()
            if len(t) >= 8:
                out.add(t)
    return out


_PREFIX_LADDER = (120, 100, 80, 60, 48, 40, 32, 24)


def _count_bytes(section_text, her_strings):
    """Rendered occurrences of her strings inside one section's text.
    The renders TRUNCATE her lines (kept-by-me heads slice the meaning),
    so the full string often never appears verbatim: try the whole
    string first, then the longest truncated prefix that the document
    actually carries. Counts the bytes of HER words that render.
    0 when the section is empty."""
    if not section_text:
        return 0
    total = 0
    for s in her_strings:
        for k in [len(s)] + [n for n in _PREFIX_LADDER if n < len(s)]:
            prefix = s[:k]
            start = 0
            found = 0
            while True:
                i = section_text.find(prefix, start)
                if i < 0:
                    break
                found += 1
                start = i + len(prefix)
            if found:
                total += found * len(prefix)
                break
    return total


def her_words_share(instance, cfg, root=None, names=None, self_name=None,
                    revs=None):
    """The shares, per section and for the load-bearing tiers
    (chosen + chronicle). Returns {'sections': {...}, 'load_bearing':
    share, 'method': str}. Fail-open: an unreadable store → a zero
    report, never a crash."""
    method = ("her bytes = rendered occurrences of her choice-file "
              "strings (kept meanings, her bookmark notes, anchor whys, "
              "essences) located in the document's section texts, "
              "longest-prefix matched (the renders truncate her lines); "
              "totals = section byte lengths (headers included); v1 "
              "approximation — participant quotes inside chronicle "
              "prose are not attributed; direction, not precision")
    try:
        if root is None:
            root = str(_R.ROOT)
        doc = _MD.build_document(instance, root, cfg=cfg or {},
                                 roster=names or {},
                                 self_name=self_name or "")
    except Exception:
        return {"sections": {}, "load_bearing": 0.0, "method": method}
    sections = {"chosen": doc.get("chosen_text") or "",
                "chronicle": doc.get("chronicle_text") or "",
                "shelf": doc.get("shelf_text") or ""}
    her = _her_strings(instance, root=root, revs=revs)
    out, lb_her, lb_all = {}, 0, 0
    for name, text in sections.items():
        h = _count_bytes(text, her)
        t = len(text)
        out[name] = {"her_bytes": h, "total_bytes": t,
                     "share": round(h / t, 4) if t else 0.0}
        if name in ("chosen", "chronicle"):
            lb_her += h
            lb_all += t
    counts = doc.get("counts") or {}
    chosen_actual = len(sections.get("chosen") or "")
    chosen_cap = counts.get("chosen_cap")
    pressure = None
    if chosen_cap:
        over = (chosen_actual - chosen_cap) / chosen_cap * 100.0
        pressure = {"actual": chosen_actual, "cap": int(chosen_cap),
                    "over_pct": round(over, 1)}
    return {"sections": out,
            "load_bearing": round(lb_her / lb_all, 4) if lb_all else 0.0,
            "chosen_pressure": pressure,
            "method": method}


def print_authorship(instance, cfg, names=None, self_name=None):
    """The audit tool's authorship block. Returns the report for callers
    (the baseline recorder)."""
    r = her_words_share(instance, cfg, names=names, self_name=self_name)
    print('\n== AUTHORSHIP (her_words_share, Phase M) ==')
    print('  method:', r['method'])
    for name, s in r['sections'].items():
        print(f"  {name:>10}: {s['share']*100:.2f}%  "
              f"({s['her_bytes']}/{s['total_bytes']} bytes)")
    print(f"  LOAD-BEARING (chosen+chronicle): "
          f"{r['load_bearing']*100:.2f}%")
    pr = r.get("chosen_pressure")
    if pr and pr["over_pct"] > 10.0:
        # Phase F's paired measurement (memorybuild2 §5b): chosen-never-
        # trimmed silently starves the chronicle at small windows — say it
        # in numbers instead of letting the inversion stay invisible.
        print(f"  KEEP-PRESSURE: chosen renders {pr['actual']} chars vs "
              f"cap {pr['cap']} (+{pr['over_pct']}%) — the chronicle is "
              f"starving at this window")
    elif pr:
        print(f"  keep-pressure: chosen {pr['actual']}/{pr['cap']} chars "
              f"({pr['over_pct']:+.1f}%) — healthy")
    return r