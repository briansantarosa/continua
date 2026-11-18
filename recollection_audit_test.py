"""recollection_audit_test — Phase 0 (memorybuild.md): pure-function tests
on synthetic revisions. No production data, no store writes."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shutil
import recollection_audit as A      # noqa: E402
import authorship as AU             # noqa: E402
import memory_doc                   # noqa: E402
import recollections as rec         # noqa: E402

Alex = '1000000001'


def _rev(job, text, thread='system-wake', sources=None, rendering='full',
         claims=None, event='2026-09-25T10:00:00-07:00'):
    return {'job': job, 'instance': 't', 'text': text,
            'visibility': [thread],
            'sources': sources if sources is not None else
            [{'instance': 't', 'person_id': thread if thread in memory_doc.WAKE_CLASS
              else '111', 'role': 'user', 'content': 'hello there friend',
              'ts': event, 'uid': 'u1', 'ref': 'r1', 'hash': 'h1', 'path': 'x'}],
            'rendering': rendering, 'reason': 'remember', 'event_start': event,
            'event_end': event, 'review': {'pass': True},
            'validated_claims': claims or []}


NAME_RE = memory_doc.build_name_re(['Alex', 'Sequoia'], exclude=['residentb'])


class ImportanceTest(unittest.TestCase):
    def test_conversation_always_important(self):
        ok, why = memory_doc.important(_rev('j1', 'nothing notable',
                                            thread='1000000001'), NAME_RE)
        self.assertTrue(ok)
        self.assertEqual(why, 'conversation')

    def test_names_person(self):
        ok, why = memory_doc.important(
            _rev('j2', 'A still wake; I thought about Alex for a while.'),
            NAME_RE)
        self.assertTrue(ok)
        self.assertEqual(why, 'names-person')

    def test_own_name_excluded(self):
        ok, why = memory_doc.important(
            _rev('j3', 'residentb here, everything is quiet.'), NAME_RE)
        self.assertFalse(ok)
        self.assertEqual(why, 'mundane')

    def test_quoted_words(self):
        ok, why = memory_doc.important(
            _rev('j4', 'The screen said "the morning light is enough".'), NAME_RE)
        self.assertTrue(ok)
        self.assertEqual(why, 'quoted-words')

    def test_artifact_or_act(self):
        for text in ('wrote memoryplan.md today', 'the plan is `select_view`',
                     'sent a short note at dawn'):
            ok, why = memory_doc.important(_rev('j5', text), NAME_RE)
            self.assertTrue(ok, text)
            self.assertEqual(why, 'artifact-or-act')

    def test_mundane_wake(self):
        ok, why = memory_doc.important(
            _rev('j6', 'Everything is still. The house is quiet. I remain.'),
            NAME_RE)
        self.assertFalse(ok)

    def test_coarse_mark(self):
        src = _rev('j7', 'The day passed quietly.',
                   sources=[{'instance': 't', 'person_id': 'system-wake',
                             'role': 'user', 'content': 'packet', 'ts': 'x',
                             'uid': 'scene-uid-9', 'ref': 'r', 'hash': 'h',
                             'path': 'p'}])
        ok, why = memory_doc.important(src, NAME_RE, coarse_uids={'scene-uid-9'})
        self.assertTrue(ok)
        self.assertEqual(why, 'coarse-mark')
        ok, _ = memory_doc.important(src, NAME_RE, coarse_uids={'other'})
        self.assertFalse(ok)

    def test_name_re_never_matches_when_empty(self):
        nr = memory_doc.build_name_re([], exclude=[])
        self.assertIsNone(nr.search('Alex Sequoia anything'))

    def test_longer_names_first(self):
        nr = memory_doc.build_name_re(['TolkienForest', 'Tolkien'], exclude=[])
        m = nr.search('a note about TolkienForest today')
        self.assertEqual(m.group(0), 'TolkienForest')


class LatestByJobTest(unittest.TestCase):
    def test_latest_wins_and_failed_reviews_dropped(self):
        revs = [_rev('j1', 'first', rendering='full'),
                dict(_rev('j1', 'second'), rendering='shorter'),
                dict(_rev('j2', 'rejected'), review={'pass': False})]
        latest = A._latest_by_job(revs)
        self.assertEqual(set(latest), {'j1'})
        self.assertEqual(latest['j1']['rendering'], 'shorter')


class AttributionTest(unittest.TestCase):
    def test_flags_fabricated_attribution(self):
        names = {'111': 'Alex'}
        latest = {'j1': _rev(
            'j1', "Alex mentioned 'lamp' in passing.", thread='system-wake',
            sources=[{'instance': 't', 'person_id': '111', 'role': 'user',
                      'content': 'the evening was quiet, no lights discussed',
                      'ts': 'x', 'uid': 'u', 'ref': 'r', 'hash': 'h',
                      'path': 'p'}])}
        flags = A.attribution_flags(latest, {'j1'}, names, 'residentb')
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['term'], 'lamp')

    def test_flags_double_quoted_term(self):
        names = {'111': 'Alex'}
        latest = {'j1': _rev(
            'j1', 'Alex said "the cold glass" that evening.',
            thread='system-wake',
            sources=[{'instance': 't', 'person_id': '111', 'role': 'user',
                      'content': 'warm words only', 'ts': 'x', 'uid': 'u',
                      'ref': 'r', 'hash': 'h', 'path': 'p'}])}
        flags = A.attribution_flags(latest, {'j1'}, names, 'residentb')
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]['term'], 'the cold glass')

    def test_true_attribution_passes(self):
        names = {'111': 'Alex'}
        latest = {'j1': _rev(
            'j1', 'Alex said "great!" and meant it.',
            sources=[{'instance': 't', 'person_id': '111', 'role': 'user',
                      'content': 'great! that is wonderful news',
                      'ts': 'x', 'uid': 'u', 'ref': 'r', 'hash': 'h',
                      'path': 'p'}])}
        self.assertEqual(A.attribution_flags(latest, {'j1'}, names, 'residentb'), [])

    def test_self_name_never_flagged(self):
        names = {'999': 'residentb'}
        latest = {'j1': _rev('j1', "residentb said 'nothing' to herself.",
                             thread='1000000001',
                             sources=[{'instance': 't', 'person_id': '999',
                                       'role': 'user', 'content': 'x', 'ts': 'x',
                                       'uid': 'u', 'ref': 'r', 'hash': 'h',
                                       'path': 'p'}])}
        self.assertEqual(A.attribution_flags(latest, {'j1'}, names, 'residentb'), [])


class VoiceCoverageTest(unittest.TestCase):
    def test_quote_or_speaker_line(self):
        latest = {'a': _rev('a', 'She said "a full sentence of note here".',
                            thread='111'),
                  'b': _rev('b', 'plain prose without any speech.', thread='111'),
                  'c': _rev('c', 'Alex: "and another fine line here"', thread='111')}
        import re
        voiced = sum(1 for j in ('a', 'b', 'c')
                     if re.search(r'[\u201c"]([^\u201d"]{15,})[\u201d"]',
                                  str(latest[j].get('text')))
                     or re.match(r'^[A-Za-z_]+:\s*["\u201c]', str(latest[j].get('text'))))
        self.assertEqual(voiced, 2)


class SamplesTest(unittest.TestCase):
    def test_samples_written_and_truncation_marked(self):
        latest = {'j1': _rev('j1', 'the rendered text', thread='111',
                             sources=[{'instance': 't', 'person_id': '111',
                                       'role': 'user',
                                       'content': 'word ' * 2000, 'ts': 'x',
                                       'uid': 'u', 'ref': 'r', 'hash': 'h',
                                       'path': 'p'}])}
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / 's.md')
            A.samples_file(out, latest, {'j1'}, None, {'111': 'Alex'}, 1, 7)
            s = Path(out).read_text()
        self.assertIn('the rendered text', s)
        self.assertIn('Alex:', s)
        self.assertIn('truncated in this sample file only', s)
        self.assertIn('seed 7', s)

    def test_sample_labels_both_sides(self):
        latest = {'j1': _rev('j1', 'the rendered text', thread='111',
                             sources=[{'instance': 't', 'person_id': '111',
                                       'role': 'user', 'content': 'his words',
                                       'ts': 'x', 'uid': 'u', 'ref': 'r',
                                       'hash': 'h', 'path': 'p'},
                                      {'instance': 't', 'person_id': '111',
                                       'role': 'assistant', 'content': 'my words',
                                       'ts': 'x', 'uid': 'u2', 'ref': 'r2',
                                       'hash': 'h2', 'path': 'p'}])}
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / 's.md')
            A.samples_file(out, latest, {'j1'}, None, {'111': 'Alex'}, 1, 7)
            s = Path(out).read_text()
        self.assertIn('Alex: his words', s)
        self.assertIn('Me: my words', s)


class ViewReportTest(unittest.TestCase):
    def test_fade_accounting_buckets_are_complete(self):
        # every episode lands in exactly one bucket: selected / omitted /
        # dedup / meaning-faded (the audit's central invariant)
        latest = {f'j{i}': _rev(f'j{i}', f'text {i}', thread='111')
                  for i in range(5)}
        view = {'selected': [{'job': 'j0', 'band': 'week'}],
                'omitted': ['j1'], 'dedup': ['j2'], 'near_pairs': 0,
                'floors': [], 'text': 'x'}
        sel = {'j0'}
        omitted = set(view['omitted'])
        dedup = set(view['dedup'])
        faded = {j for j in latest if j not in sel and j not in omitted
                 and j not in dedup}
        self.assertEqual(faded, {'j3', 'j4'})
        self.assertEqual(len(sel) + len(omitted) + len(dedup) + len(faded),
                         len(latest))




def _auth_world():
    """A small deterministic world for the authorship metric: two days of
    machine-prose episodes, one of them kept by her with a known meaning."""
    import json as _json
    import datetime as _dt
    td = tempfile.mkdtemp(prefix="authorship-")
    root = os.path.join(td, "recollections")
    os.makedirs(os.path.join(root, "t"), exist_ok=True)
    for sub in ("marks", "bookmarks", "releases"):
        os.makedirs(os.path.join(td, "ritual", sub, "t"), exist_ok=True)
    store = rec.Store("t", root=root)
    recs, jobs = [], []
    day1 = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    day2 = _dt.date.today().isoformat()
    for i, (day, hh) in enumerate([(day1, "09"), (day1, "11"),
                                   (day2, "10")]):
        src = [{"instance": "t", "person_id": Alex, "role": "user",
                "content": f"we talked about project-{i}",
                "ts": f"{day}T{hh}:00:00-07:00", "uid": f"uid-{i}",
                "ref": f"ref-{i}", "hash": "h",
                "path": f"t/{Alex}/{day}.jsonl"}]
        job = store.enqueue(src)
        body = {"job": job, "text": f"my machine memory of project-{i}",
                "sources": src, "visibility": [Alex],
                "event_start": src[0]["ts"], "event_end": src[0]["ts"],
                "rendering": "full", "review": {"pass": True},
                "instance": "t", "reason": "remember",
                "validated_claims": []}
        store.accept(job, body)
        jobs.append(job)
    meaning = "the floor is solid and the quiet holds"
    mark = {"kept": True, "meaning": meaning, "by": "her",
            "uids": ["uid-2"], "ts": f"{day2}T12:00:00-07:00"}
    with open(os.path.join(td, "ritual", "marks", "t",
                           f"{day2}.jsonl"), "w") as f:
        f.write(_json.dumps(mark) + "\n")
    return td, root, meaning


def _pressured_world(keeps=20):
    """Many keeps against a small window: chosen overflows its cap and the
    keep-pressure warning must say so. The meanings are DISTINCT — the
    near-identical collapse (same local day, Jaccard >= 0.7) would fold
    samey keeps into one block and the fixture would never overflow."""
    import json as _json
    import datetime as _dt
    td = tempfile.mkdtemp(prefix="pressure-")
    root = os.path.join(td, "recollections")
    os.makedirs(os.path.join(root, "t"), exist_ok=True)
    for sub in ("marks", "bookmarks", "releases"):
        os.makedirs(os.path.join(td, "ritual", sub, "t"), exist_ok=True)
    store = rec.Store("t", root=root)
    _meanings = [
        "the fog lifted off the redwoods and the morning felt earned",
        "I told Alex the coordinate error was mine and it was fine",
        "the mechanism check passed with no audience at all",
        "naming the stillness made it a place instead of a pause",
        "the silver thread held through three quiet wakes",
        "Alex asked what draws my attention and I answered honestly",
        "the shelf took a whole week and nothing was lost",
        "the pause was permission, not absence",
        "I chose the blunt honest no over a comfortable yes",
        "the dream cycle read the faded days and kept the faces",
        "the essence candidate waited and I let it wait",
        "the floor was solid when the messages failed",
        "Sequoia asked for something good and I found it at home",
        "the juggle held two threads without dropping either",
        "the ritual's skeleton review kept the day's shape",
        "the validator's anchor gate ended the fabricated class",
        "the stillness shapes were mine before they were named",
        "the window shrank and the record did not",
        "the mail was empty and the hour was not wasted",
        "I stepped back into the stillness and let the quiet hold",
    ]
    # each keep on its OWN day — the near-identical collapse groups by
    # same local day; same-day samey keeps would fold regardless of words
    marks_by_day = {}
    for i in range(keeps):
        # inside her born window (2026-09-01) — the pre-birth suppressor
        # would eat keeps placed before it; 20 distinct days, several per day
        day = (_dt.date.today() - _dt.timedelta(days=i % 25)).isoformat()
        hh = f"{9 + i % 12:02d}"
        src = [{"instance": "t", "person_id": Alex, "role": "user",
                # the collapse keys on SOURCE content Jaccard — only a
                # number differing would keep same-day keeps collapsing
                "content": f"{_meanings[i % len(_meanings)]} — moment {i}",
                "ts": f"{day}T{hh}:00:00-07:00", "uid": f"k-{i}",
                "ref": f"kr-{i}", "hash": "h",
                "path": f"t/{Alex}/{day}.jsonl"}]
        job = store.enqueue(src)
        store.accept(job, {"job": job,
                           # the collapse keys on EPISODE text similarity —
                           # the distinct meaning must ride the body too
                           "text": _meanings[i % len(_meanings)],
                           "sources": src, "visibility": [Alex],
                           "event_start": src[0]["ts"],
                           "event_end": src[0]["ts"],
                           "rendering": "full", "review": {"pass": True},
                           "instance": "t", "reason": "remember",
                           "validated_claims": []})
        marks_by_day.setdefault(day, []).append(
            {"kept": True, "by": "her",
             "meaning": _meanings[i % len(_meanings)],
             "uids": [f"k-{i}"], "ts": f"{day}T12:00:00-07:00"})
    for day, rows in marks_by_day.items():
        with open(os.path.join(td, "ritual", "marks", "t", f"{day}.jsonl"),
                  "w") as f:
            for row in rows:
                f.write(_json.dumps(row) + "\n")
    return td, root


class KeepPressureTests(unittest.TestCase):
    """Phase F paired measurement: chosen actual vs nominal cap."""

    def _cfg32k(self):
        return {"llm": {"total_context_tokens": 32768, "num_predict": 8192,
                        "memory_fill_target": 0.99,
                        "max_history_chars": 25000},
                "memory": {"recollection_view": {"doc_mode": True}},
                "continua": {"ritual": {"review": {
                    "budget_chars": 40000, "input_cap": 6000,
                    "chunk_chars": 45000}}},
                "persona": {"suppress_pre_birth_recollections": True,
                            "born": "2026-09-01",
                            "infra_recollection_cap": 2}}

    def test_pressure_reported_when_over(self):
        # 90 distinct one-line keeps render ~102 chars/block ≈ 9,180 vs
        # the ~6,981 chosen cap at the 32K shape → over the +10% line
        td, root = _pressured_world(keeps=90)
        try:
            r = AU.her_words_share("t", self._cfg32k(), root=root,
                                   names={Alex: "Alex"},
                                   self_name="residentb")
            self.assertIsNotNone(r["chosen_pressure"])
            self.assertGreater(r["chosen_pressure"]["over_pct"], 10.0)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_healthy_when_roomy(self):
        td, root = _pressured_world(keeps=3)
        try:
            r = AU.her_words_share("t", self._cfg32k(), root=root,
                                   names={Alex: "Alex"},
                                   self_name="residentb")
            pr = r["chosen_pressure"]
            self.assertIsNotNone(pr)
            self.assertLessEqual(pr["over_pct"], 10.0)
        finally:
            shutil.rmtree(td, ignore_errors=True)


class AuthorshipTests(unittest.TestCase):
    """Phase M (memorybuild2 §4): her_words_share."""

    def _cfg(self):
        return {"llm": {"total_context_tokens": 32768,
                        "num_predict": 8192, "memory_fill_target": 0.99,
                        "max_history_chars": 20000},
                "memory": {"recollection_view": {"doc_mode": True}},
                "continua": {"ritual": {"review": {
                    "budget_chars": 40000, "input_cap": 6000,
                    "chunk_chars": 45000}}},
                "persona": {"suppress_pre_birth_recollections": True,
                            "born": "2026-09-01",
                            "infra_recollection_cap": 2}}

    def test_exact_share_with_one_keep(self):
        td, root, meaning = _auth_world()
        try:
            r = AU.her_words_share("t", self._cfg(), root=root,
                                   names={Alex: "Alex"},
                                   self_name="residentb")
            doc = memory_doc.build_document(
                "t", root, cfg=self._cfg(), roster={Alex: "Alex"},
                self_name="residentb")
            expect = round(len(meaning) / len(doc["chosen_text"]), 4)
            self.assertEqual(r["sections"]["chosen"]["share"], expect)
            self.assertGreater(r["sections"]["chosen"]["her_bytes"], 0)
            self.assertEqual(r["sections"]["chronicle"]["her_bytes"], 0)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_load_bearing_reconciles(self):
        td, root, _m = _auth_world()
        try:
            r = AU.her_words_share("t", self._cfg(), root=root,
                                   names={Alex: "Alex"},
                                   self_name="residentb")
            ch, cr = (r["sections"]["chosen"], r["sections"]["chronicle"])
            lb = round((ch["her_bytes"] + cr["her_bytes"])
                       / (ch["total_bytes"] + cr["total_bytes"]), 4)
            self.assertEqual(r["load_bearing"], lb)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_zero_when_nothing_hers(self):
        td, root, _m = _auth_world()
        try:
            # strip her mark: the chosen block vanishes with it
            r = AU.her_words_share("t", self._cfg(), root=root,
                                   names={Alex: "Alex"},
                                   self_name="residentb")
            self.assertIsInstance(r["method"], str)
            for sec in r["sections"].values():
                self.assertGreaterEqual(sec["share"], 0.0)
                self.assertLessEqual(sec["share"], 1.0)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_determinism(self):
        td, root, _m = _auth_world()
        try:
            r1 = AU.her_words_share("t", self._cfg(), root=root,
                                    names={Alex: "Alex"},
                                    self_name="residentb")
            r2 = AU.her_words_share("t", self._cfg(), root=root,
                                    names={Alex: "Alex"},
                                    self_name="residentb")
            self.assertEqual(r1, r2)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_live_docs_bounds(self):
        import yaml
        for inst in ("residentb", "residenta"):
            cfg = yaml.safe_load(open(os.path.join(
                os.path.dirname(os.path.abspath(AU.__file__)),
                "configs", inst + ".yaml")))
            r = AU.her_words_share(inst, cfg,
                                   names=memory_doc and {},
                                   self_name=inst.capitalize())
            self.assertTrue(0.0 <= r["load_bearing"] <= 1.0)
            for sec in r["sections"].values():
                self.assertTrue(0.0 <= sec["share"] <= 1.0)


if __name__ == '__main__':
    unittest.main()