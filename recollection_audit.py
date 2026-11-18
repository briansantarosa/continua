#!/usr/bin/env python3
"""recollection_audit — Phase 0 of memorybuild.md (report-only, read-only).

Measures what the memory system actually shows vs stores, for the designer's eyes:

  store      episodes/revisions, chars, wake/chat split, per-band counts,
             rendering distribution, essences, claims (participant vs
             resident), anchors/corrections, her marks & bookmarks,
             compress queue
  view       the standing view at --at with --budget (no raw_history, so
             juggle-dedup reads lower than a live turn): selected/omitted/
             dedup/meaning-faded per band, distinct local days visible,
             per-thread visibility, near_pairs, voice coverage
  attribution  report-only flag scan (Phase 11's D2, measured early)
  sizing     the importance-test projection (memory_doc.important) that
             Phase 10's admission will use

Read-only everywhere: read_revisions uses SQLite mode=ro; the ritual dirs
are read with open() only. Never writes except --samples' report file
(under logs/, for the designer). Run with the venv python:

  python3 recollection_audit.py residentb \
      [--person 1000000001] [--at ISO] [--budget N] [--samples N] [--seed 7]
"""
import argparse
import collections
import datetime as _dt
import json
import random
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recollections as R          # noqa: E402
import memory_doc                  # noqa: E402
import people                      # noqa: E402

import yaml                        # noqa: E402

BASE = Path(__file__).resolve().parent
SELF_NAMES = {'residentb': 'residentb', 'residenta': 'persona-a', 'residentc': 'residentc'}
BANDS = ['days', 'week', 'month', 'year', 'older']

_SENT_RE = re.compile(r'(?<=[.!?])\s+')
# report-only attribution scan (Phase 11's attribution_flags, measured early)
_ATTR_VERBS = (r'mentioned|said|told me|told us|showed me|showed|asked|'
               r'suggested|called|shared|wrote|sent|named|referred to')
_ATTR_RE = re.compile(
    r'(?P<name>[A-Z][A-Za-z_]+(?: [A-Z][A-Za-z_]+)?)\s+(?:' + _ATTR_VERBS + r')\b'
    r'(?P<mid>[^."\u201c\u201d\']*)'
    r'["\u201c\u201d\'](?P<term>[^\u201d"\'\n]{2,})["\u201c\u201d\']', re.M)


def _latest_by_job(revs):
    """read_revisions returns every revision ordered by (job, revision);
    the episode's latest revision is the one the view renders."""
    latest = {}
    for r in revs:
        if r.get('instance') and r.get('review', {}).get('pass') is not True:
            continue
        latest[r['job']] = r
    return latest


def _cfg(instance):
    p = BASE / 'configs' / f'{instance}.yaml'
    try:
        return yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    except Exception:
        return {}


def _roster():
    try:
        return {pid: p.display_name for pid, p in people.load_roster().items()}
    except Exception:
        return {}


def _jsonl_rows(root):
    rows = []
    try:
        for path in sorted(Path(root).glob('*.jsonl')):
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    rows.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return rows


def store_report(revs, instance):
    latest = _latest_by_job(revs)
    jobs = list(latest.values())
    chars = sum(len(str(j.get('text') or '')) for j in jobs)
    src_chars = sum(len(str(s.get('content') or ''))
                    for j in jobs for s in (j.get('sources') or []))
    per_band = collections.Counter(memory_doc.thread_of(j) for j in jobs)
    wake = sum(v for k, v in per_band.items() if k in memory_doc.WAKE_CLASS)
    chat = len(jobs) - wake
    bands = collections.Counter(R.age_band(j.get('event_end') or
                                           j.get('event_start'), R.now())
                                for j in jobs)
    render = collections.Counter(str(j.get('rendering')) for j in jobs)
    reasons = collections.Counter(str(j.get('reason')) for j in jobs)
    claims = [c for j in jobs for c in (j.get('validated_claims') or [])]
    part = [c for c in claims if str(c.get('speaker', '')).startswith('participant')]
    quotes = [c.get('quote') for c in claims if c.get('quote')]
    anchors, corrections = R.read_guards(instance)
    marks = _jsonl_rows(BASE / 'ritual' / 'marks' / instance)
    bookmarks = _jsonl_rows(BASE / 'ritual' / 'bookmarks' / instance)
    cq = 0
    try:
        cq = len(json.loads((BASE / 'recollections' / instance /
                             'compress_queue.json').read_text(encoding='utf-8')))
    except Exception:
        pass
    days = sorted({str(j.get('event_start'))[:10] for j in jobs})
    return {
        'episodes': len(jobs), 'revisions': len(revs), 'text_chars': chars,
        'source_chars': src_chars, 'wake_episodes': wake, 'chat_episodes': chat,
        'per_thread': dict(per_band), 'bands': dict(bands),
        'rendering': dict(render), 'reasons': dict(reasons),
        'claims': len(claims), 'claims_participant': len(part),
        'quotes': len(quotes),
        'quotes_long': sum(1 for q in quotes if len(str(q)) >= 15),
        'anchors': len(anchors), 'corrections': len(corrections),
        'marks_rows': len(marks),
        'marks_kept_rows': sum(1 for m in marks if m.get('kept') is True),
        'marks_kept_uids': len(R.marked_uids(instance)),
        'bookmarks_rows': len(bookmarks),
        'bookmarks_her': sum(1 for b in bookmarks if b.get('by') == 'her'),
        'compress_queue': cq,
        'first_day': days[0] if days else None, 'last_day': days[-1] if days else None,
    }, latest


def view_report(instance, latest, person, budget, at, names, suppress, infra_cap):
    view = R.context_view(instance, str(person), budget=budget, at=at,
                          raw_history=None, names=names,
                          suppress_before=suppress, infra_cap=infra_cap)
    sel = {b['job'].split(':', 1)[1] if str(b['job']).startswith('thematic:')
           else b['job'] for b in view.get('selected') or []}
    omitted = set(view.get('omitted') or [])
    dedup = set(view.get('dedup') or [])
    faded = {j for j in latest if j not in sel and j not in omitted and j not in dedup}
    bands_sel = collections.Counter(str(b.get('band')) for b in
                                    view.get('selected') or [])
    sel_text_chars = sum(len(str(latest[j].get('text') or '')) for j in sel if j in latest)
    days_sel = sorted({str(latest[j].get('event_start'))[:10] for j in sel if j in latest})
    days_all = sorted({str(j.get('event_start'))[:10] for j in latest.values()})
    threads_sel = collections.Counter(memory_doc.thread_of(latest[j])
                                      for j in sel if j in latest)
    # voice coverage over the SELECTED episodes (participant quote or a
    # labeled speaker line in the rendered text)
    def _has_voice(t):
        return bool(re.search(r'[\u201c"]([^\u201d"]{15,})[\u201d"]', t)
                    or re.match(r'^[A-Za-z_]+:\s*["\u201c]', t))
    voiced = sum(1 for j in sel if j in latest and _has_voice(str(latest[j].get('text') or '')))
    per_band_faded = collections.Counter(
        R.age_band(latest[j].get('event_end') or latest[j].get('event_start'), at)
        for j in faded)
    return view, {
        'budget_chars': budget, 'at': str(at),
        'selected': len(sel), 'selected_chars': sel_text_chars,
        'selected_bands': dict(bands_sel), 'omitted': len(omitted),
        'dedup': len(dedup), 'meaning_faded': len(faded),
        'faded_bands': dict(per_band_faded),
        'near_pairs': view.get('near_pairs'), 'floors': view.get('floors'),
        'days_visible': len(days_sel), 'days_in_store': len(days_all),
        'days_missing': len(set(days_all) - set(days_sel)),
        'threads_selected': dict(threads_sel),
        'voice_coverage': f'{voiced}/{len(sel)}',
    }, sel, faded


def attribution_flags(latest, sel, names, self_name):
    """Report-only scan: sentences attributing a quoted term to a person
    whose stored sources never said it. Never blocks, never alters."""
    by_name = {v: str(k) for k, v in names.items()}
    flags = []
    for j in sorted(sel):
        rev = latest.get(j)
        if rev is None:
            continue
        text = str(rev.get('text') or '')
        sources = rev.get('sources') or []
        for m in _ATTR_RE.finditer(text):
            name = m.group('name')
            pid = by_name.get(name)
            if not pid or name == self_name:
                continue
            term = m.group('term').strip()
            said = [str(s.get('content') or '') for s in sources
                    if str(s.get('person_id')) == pid and s.get('role') == 'user']
            if said and not any(term.lower() in c.lower() for c in said):
                flags.append({'job': j, 'name': name, 'term': term,
                              'sentence': m.group(0)[:160]})
    return flags


def sizing_projection(latest, names, self_name, marks_dir):
    """Phase 10's admission rules, measured early (report-only)."""
    coarse = set()
    for row in _jsonl_rows(marks_dir):
        if row.get('kept') is True and len(row.get('uids') or []) > 60:
            coarse.update(row.get('uids') or [])
    name_re = memory_doc.build_name_re(names.values(), exclude=[self_name])
    reasons = collections.Counter()
    imp = 0
    for j, rev in latest.items():
        ok, why = memory_doc.important(rev, name_re, coarse)
        if ok:
            imp += 1
        reasons[why] += 1
    return {'important': imp, 'mundane': len(latest) - imp,
            'by_rule': dict(reasons)}


def samples_file(path, latest, sel, view, names, n, seed):
    rng = random.Random(seed)
    chosen = sorted(sel)
    rng.shuffle(chosen)
    picks = chosen[:max(0, int(n))]
    lines = [f'# recollection samples — {len(picks)} of {len(sel)} visible '
             f'episodes (seed {seed})', '',
             'Rendered text, then the source rows behind it. Truncation marks '
             'apply to THIS FILE ONLY — the view never clips.', '']
    for j in picks:
        rev = latest.get(j)
        if rev is None:
            continue
        lines.append(f'--- episode {j} · {rev.get("event_start")} · '
                     f'thread {memory_doc.thread_of(rev)} ---')
        lines.append(str(rev.get('text') or ''))
        used = 0
        for s in (rev.get('sources') or []):
            label = 'Me' if s.get('role') == 'assistant' else \
                names.get(str(s.get('person_id')), str(s.get('person_id')))
            body = str(s.get('content') or '')
            room = 3000 - used
            if room <= 0:
                lines.append('  [more source rows omitted from this sample]')
                break
            cut = body if len(body) <= room else body[:room] + ' …[truncated in this sample file only]'
            used += len(cut)
            lines.append(f'  {label}: {cut}')
        lines.append('')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text('\n'.join(lines), encoding='utf-8')
    return str(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('instance')
    ap.add_argument('--person', default='1000000001')
    ap.add_argument('--at', default=None)
    ap.add_argument('--budget', type=int, default=200000,
                    help='view budget in chars (default 200000 = the live '
                         'room measured on 09-29; the budget is NOT binding '
                         'today — the meaning-fade is)')
    ap.add_argument('--samples', type=int, default=0)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    cfg = _cfg(a.instance)
    persona = cfg.get('persona') or {}
    suppress = None
    if persona.get('suppress_pre_birth_recollections', True):
        suppress = str(persona.get('born') or '')
    infra_cap = persona.get('infra_recollection_cap', 2)
    names = _roster()
    self_name = SELF_NAMES.get(a.instance, a.instance)
    at = a.at or R.now()

    revs = R.read_revisions(a.instance)
    store, latest = store_report(revs, a.instance)
    print('== STORE ==')
    for k, v in store.items():
        print(f'  {k}: {v}')

    view, vr, sel, faded = view_report(a.instance, latest, a.person, a.budget,
                                       at, names, suppress, infra_cap)
    print('\n== VIEW (no raw_history — a live turn dedups MORE via juggle) ==')
    for k, v in vr.items():
        print(f'  {k}: {v}')

    print('\n== ATTRIBUTION FLAGS (report-only) ==')
    flags = attribution_flags(latest, sel, names, self_name)
    for f in flags[:20]:
        print(f"  {f['job']} · {f['name']}: '{f['term']}' — "
              f"{f['sentence'][:110]}")
    print(f'  total: {len(flags)}')

    print('\n== SIZING PROJECTION (Phase 10 admission rules) ==')
    sz = sizing_projection(latest, names, self_name, BASE / 'ritual' / 'marks' / a.instance)
    for k, v in sz.items():
        print(f'  {k}: {v}')

    # Phase M (memorybuild2 §4, 2026-09-30): the authorship metric —
    # her words' byte share of the load-bearing tiers. Fail-open.
    try:
        import authorship as _au
        _au.print_authorship(a.instance, cfg, names=names,
                             self_name=self_name)
    except Exception:
        print('\n== AUTHORSHIP == (unavailable, fail-open)')

    # Phase W (memorybuild2 §8, 2026-10-01): the UPTAKE metric — offer
    # counts + self-weave state. No target, no nudge escalation: the
    # offer is the build; uptake is unpredictable by design.
    try:
        import weave as _wv
        _u = _wv.uptake(a.instance)
        print('\n== UPTAKE (Phase W) ==')
        for _k, _v in _u.items():
            print(f'  {_k}: {_v}')
        if not _u.get("self_manuscript_exists"):
            _origin = os.path.join(
                os.path.dirname(os.path.abspath(_wv.__file__)),
                "recollections", "pre-cutover-20260917-073949", "books",
                a.instance, "self.md")
            if os.path.exists(_origin):
                note = ("  note: her pre-cutover self.md sits in the "
                        "archive (origin kept) — restoring it to her "
                        "desk is the designer's choice; the weave offer works "
                        "with or without it")
                print(note)
    except Exception:
        print('\n== UPTAKE == (unavailable, fail-open)')

    if a.samples:
        out = a.out or str(BASE / 'logs' / ('recollection_samples_'
                           + _dt.datetime.now().strftime('%Y%m%d-%H%M') + '.md'))
        print('\nsamples →', samples_file(out, latest, sel, view, names,
                                          a.samples, a.seed))


if __name__ == '__main__':
    main()