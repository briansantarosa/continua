"""Offline phase-3 gate: read-only, budgeted first-person context views."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import recollections as r

NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)


def episode(job, age=0, person='1000000001', text=None, instance='residenta'):
    ts = (NOW - timedelta(days=age)).isoformat()
    return {'schema_version': 1, 'instance': instance, 'job': job,
            'event_start': ts, 'event_end': ts, 'visibility': [person],
            'sources': [{'ref': job, 'content': 'source ' + job, 'role': 'assistant'}],
            'text': text or f'I remember the particular exchange about {job}.',
            'reason': 'remember', 'review': {'pass': True, 'issues': []}}


def store_revisions(store, job):
    """All accepted revision bodies for one job (test helper)."""
    return store.revisions(job)


class ViewTests(unittest.TestCase):
    def test_api_exists(self):
        self.assertTrue(callable(getattr(r, 'context_view', None)))

    def test_read_never_creates_empty_store(self):
        with tempfile.TemporaryDirectory() as root:
            result = r.context_view('residenta', '1000000001', root=root, at=NOW.isoformat())
            self.assertEqual(result['text'], '')
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_bands_and_no_mutation(self):
        rows = [episode(str(age), age) for age in (0, 3, 7, 30, 365)]
        before = copy.deepcopy(rows)
        result = r.select_view(rows, 'residenta', '1000000001', 10000, at=NOW.isoformat())
        self.assertEqual(rows, before)
        for row in rows:
            self.assertIn(row['text'], result['text'])
        self.assertEqual(len({item['job'] for item in result['selected']}), 5)
        # chunk 5 structure: the standing backbone (oldest) renders FIRST,
        # the most recent (24h band, active thread) renders LAST
        text = result['text']
        pos_old = text.index(next(r['text'] for r in rows if r['job'] == '365'))
        pos_new = text.index(next(r['text'] for r in rows if r['job'] == '0'))
        self.assertLess(pos_old, pos_new)
        # stable: identical inputs -> identical output
        again = r.select_view(copy.deepcopy(rows), 'residenta', '1000000001', 10000,
                              at=NOW.isoformat())
        self.assertEqual(again['text'], text)

    def test_one_life_visibility_and_unverified(self):
        # chunk 5: cross-person visibility is intentional (attributed);
        # cross-RESIDENT leakage stays forbidden; unverified stays out
        rows = [episode('ok'), episode('other', person='2'),
                episode('cross', instance='residentb'), episode('unverified')]
        rows[-1]['review']['pass'] = False
        result = r.select_view(rows, 'residenta', '1000000001', 10000, at=NOW.isoformat())
        self.assertEqual(sorted(item['job'] for item in result['selected']),
                         ['ok', 'other'])
        self.assertIn('person-2', result['text'])  # attribution on the other thread

    def test_budget_includes_headers_and_omits_whole_episodes(self):
        row = episode('long', text='I recall ' + 'details ' * 90 + '.')
        result = r.select_view([row], 'residenta', '1000000001', 70, at=NOW.isoformat())
        self.assertEqual(result['text'], '')
        self.assertEqual(result['omitted'], ['long'])
        self.assertLessEqual(r.token_bound(result['text']), 70)

    def test_raw_overlap_and_source_overlap(self):
        first = episode('one')
        second = episode('two')
        second['sources'] = first['sources']
        result = r.select_view([first, second], 'residenta', '1000000001', 10000,
                              at=NOW.isoformat(), raw_history=[{'content': 'source one'}])
        self.assertEqual(result['text'], '')
        result = r.select_view([first, second], 'residenta', '1000000001', 10000, at=NOW.isoformat())
        self.assertEqual(len(result['selected']), 1)

    def test_read_latest_is_readonly_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            store = r.Store('residenta', root)
            row = episode('old', 9)
            # The view reads only accepted revisions joined to accepted jobs.
            with store.db() as db:
                db.execute("INSERT INTO jobs(id,sources,status,created) VALUES(?,?,'accepted',?)",
                           ('old', '[]', NOW.isoformat()))
            store.accept('old', row)
            smaller = dict(row, text='I remember our exchange.', reason='compress')
            store.accept('old', smaller)
            before = store.path.read_bytes()
            result = r.context_view('residenta', '1000000001', root=root, at=NOW.isoformat(), budget=10000)
            # Prefer richest accepted revision when it fits, not the last tiny one.
            self.assertIn(row['text'], result['text'])
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(len(result['selected']), 1)

    def test_master_kill_switch(self):
        with patch.dict('os.environ', {'CONTINUA_RECOLLECTIONS': '0'}):
            self.assertEqual(r.context_view('residenta', '1000000001')['text'], '')

    def test_no_unbounded_current_year_fills_empty_tier_quota(self):
        # §6d.3 floors (plan letter): a band whose share cannot hold one whole
        # episode is OMITTED and LOGGED — never rendered as a fragment. At a
        # 1,000-char total, the last24 share (300) is sub-floor → omitted.
        rows = [episode('recent', text='I remember ' + 'detail ' * 100 + '.')]
        result = r.select_view(rows, 'residenta', '1000000001', 1000, at=NOW.isoformat())
        # sub-floor band: no share cap (logged), but the whole episode still
        # renders while the total budget allows — never a fragment, never
        # silently empty (§6d.3 + §4c reconciled)
        self.assertEqual(len(result['selected']), 1)
        self.assertIn('days', result.get('floors') or [])  # keyed by age band
        # at a healthy budget the same item renders whole, floors silent
        ok = r.select_view(rows, 'residenta', '1000000001', 100000, at=NOW.isoformat())
        self.assertEqual(len(ok['selected']), 1)
        self.assertEqual(ok.get('floors'), [])


class Chunk5Tests(unittest.TestCase):
    """Chunk 5 exits: thread cap, anchor under pressure, attribution, wake life."""

    def setUp(self):
        self.now = NOW.isoformat()

    def test_thread_cap_keeps_most_recent_threads(self):
        rows = [episode('mine', 0)]
        for i, age in enumerate((1, 2, 3, 4, 5)):
            rows.append(episode(f't{i}', age + 1, person=str(100 + i),
                                text=f'I remember thread {i}.'))
        result = r.select_view(rows, 'residenta', '1000000001', 100000,
                               at=self.now, thread_cap=3, names={})
        threads_rendered = {f't{i}' for i in range(5)
                            if f'I remember thread {i}.' in result['text']}
        self.assertEqual(len(threads_rendered), 3)
        self.assertEqual(threads_rendered, {'t0', 't1', 't2'})

    def test_standing_anchor_survives_budget_pressure(self):
        rows = [episode('ancient', 365, person='2',
                        text='The naming was the first stone laid.'),
                episode('recent', 0, text='I remember today deeply. ' * 5)]
        result = r.select_view(rows, 'residenta', '1000000001', 250, at=self.now)
        self.assertIn('The naming was the first stone laid.', result['text'])
        self.assertNotIn('I remember today deeply.', result['text'])

    def test_attribution_names_resolved(self):
        rows = [episode('ok'), episode('other', person='2')]
        result = r.select_view(rows, 'residenta', '1000000001', 10000,
                               at=self.now, names={'2': 'Zen'})
        self.assertIn('[Zen ·', result['text'])

    def test_topicless_wake_sees_the_whole_life(self):
        rows = [episode('own', 0, person='system-wake'),
                episode('with_brian', 86400, person='1000000001'),
                episode('with_zen', 2 * 86400, person='2')]
        result = r.select_view(rows, 'residenta', 'system-wake', 100000, at=self.now,
                               names={'2': 'Zen', '1000000001': 'Alex'})
        jobs = {item['job'] for item in result['selected']}
        self.assertEqual(jobs, {'own', 'with_brian', 'with_zen'})
        self.assertIn('[my wake ·', result['text'])
        self.assertIn('[Alex ·', result['text'])
        self.assertIn('[Zen ·', result['text'])



class Chunk6Tests(unittest.TestCase):
    """Chunk 6 exits: anchors, corrections, thematic working sets, outage."""

    def setUp(self):
        self.now = NOW.isoformat()

    def test_anchor_preserved_under_pressure(self):
        rows = [episode('anchored', 30, text='An anchored memory. ' * 4),
                episode('filler1', 0, text='Recent filler one. ' * 8),
                episode('filler2', 1, text='Recent filler two. ' * 8)]
        result = r.select_view(rows, 'residenta', '1000000001', 320, at=self.now,
                               anchors={'anchored'})
        self.assertIn('An anchored memory.', result['text'])
        # an unanchored comparable item yields under the same pressure
        self.assertNotIn('Recent filler one.', result['text'])

    def test_unanchored_comparable_yields_first(self):
        rows = [episode('a1', 30, text='Memory one. ' * 4),
                episode('a2', 29, text='Memory two. ' * 4)]
        result = r.select_view(rows, 'residenta', '1000000001', 150, at=self.now)
        self.assertIn('Memory one.', result['text'])
        self.assertNotIn('Memory two.', result['text'])

    def test_correction_supersedes_in_selection(self):
        rows = [episode('old_belief', 90, text='I believed the map was the goal.'),
                episode('correction', 1, text='I no longer believe the map was the goal; the walk was.')]
        result = r.select_view(rows, 'residenta', '1000000001', 10000, at=self.now,
                               corrections={'correction': 'old_belief'})
        self.assertIn('the walk was', result['text'])
        self.assertNotIn('I believed the map was the goal.', result['text'])

    def test_without_correction_both_render(self):
        rows = [episode('old_belief', 90, text='I believed the map was the goal.'),
                episode('correction', 1, text='I no longer believe the map was the goal; the walk was.')]
        result = r.select_view(rows, 'residenta', '1000000001', 10000, at=self.now)
        self.assertIn('the walk was', result['text'])
        self.assertIn('I believed the map was the goal.', result['text'])

    def test_thematic_working_set_adds_and_releases(self):
        # human viewer (Alex's context): four other threads within 7d; the
        # cap renders three — the oldest (the quiet lake) is omitted by the
        # cap, rescued by the topic query. The active thread renders last.
        rows = [episode('standing', 60, person='system-wake',
                        text='The standing memory about the garden.'),
                episode('bridge', 2, person='1000000001',
                        text='Alex and I argued about the bridge and the void.'),
                episode('t3', 3, person='3',
                        text='A third thread about ordinary things.'),
                episode('tea', 4, person='2',
                        text='Bhai Kirpal and I argued about cardamom tea.'),
                episode('meadow', 5, person='4',
                        text='The quiet meadow at dusk.'),
                episode('lake', 6, person='5',
                        text='The quiet lake at the edge of the map.')]
        result = r.select_view(rows, 'residenta', '1000000001', 10000, at=self.now,
                               thread_cap=3, names={'1000000001': 'Alex'},
                               theme_query='the quiet lake at the edge')
        self.assertIn('The standing memory about the garden.', result['text'])
        self.assertIn('The quiet lake at the edge of the map', result['text'])
        self.assertIn('the bridge and the void', result['text'])  # active-last
        # topic change: the lake releases (its query is gone; the cap held)
        result2 = r.select_view(rows, 'residenta', '1000000001', 10000, at=self.now,
                                thread_cap=3, names={'1000000001': 'Alex'},
                                theme_query='the bridge and the void')
        self.assertIn('the bridge and the void', result2['text'])
        self.assertNotIn('the quiet lake at the edge of the map', result2['text'])
        self.assertIn('The standing memory about the garden.', result2['text'])

    def test_ladder_prefers_shorter_variant_in_month_band(self):
        """§4c ladder: month/year prefer the verified shorter variant of the
        same episode; days/week keep full prose. Nothing is missing — the
        same memory renders shorter as it ages."""
        old_ts = (NOW - timedelta(days=40)).isoformat()
        full = dict(episode('m1', text='A long full rendering of the memory.'),
                    event_start=old_ts, event_end=old_ts)
        shorter = dict(episode('m1', text='Short.'), event_start=old_ts,
                       event_end=old_ts, rendering='shorter')
        view = r.select_view([full, shorter], 'residenta', '1000000001', 100000,
                             at=NOW.isoformat())
        self.assertIn('Short.', view['text'])
        self.assertNotIn('A long full rendering', view['text'])

    def test_ladder_keeps_full_in_recent_bands(self):
        recent_ts = (NOW - timedelta(hours=2)).isoformat()
        full = dict(episode('d1', text='The whole vivid exchange.'),
                    event_start=recent_ts, event_end=recent_ts)
        shorter = dict(episode('d1', text='Short.'), event_start=recent_ts,
                       event_end=recent_ts, rendering='shorter')
        view = r.select_view([full, shorter], 'residenta', '1000000001', 100000,
                             at=NOW.isoformat())
        self.assertIn('The whole vivid exchange.', view['text'])
        self.assertNotIn('Short.', view['text'])

    def test_anchors_resist_the_ladder(self):
        """§4c: a marked passage keeps its wording even in the oldest band —
        'high resolution forever' means the anchor never renders shorter."""
        old_ts = (NOW - timedelta(days=300)).isoformat()
        full = dict(episode('a1', text='The exact words she anchored.'),
                    event_start=old_ts, event_end=old_ts)
        shorter = dict(episode('a1', text='Short.'), event_start=old_ts,
                       event_end=old_ts, rendering='shorter')
        view = r.select_view([full, shorter], 'residenta', '1000000001', 100000,
                             at=NOW.isoformat(), anchors={'a1'})
        self.assertIn('The exact words she anchored.', view['text'])
        self.assertNotIn('Short.', view['text'])

    def test_context_view_wires_guards(self):
        """Chunk-6 wiring: anchors and corrections load from the store inside
        context_view (the live turn previously passed neither)."""
        import os as _os
        chroot = tempfile.mkdtemp(prefix='guards-chron-')
        def mk_src(ts, content, uid):
            row = {"ts": ts, "instance": "g", "person_id": "1000000001",
                   "role": "user", "content": content, "uid": uid}
            path = Path(chroot) / "g" / "1000000001" / (uid[:6] + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(row) + "\n")
            return r.source_record(path, row, "g", root=chroot)
        def ok_writer():
            def w(system, payload):
                # one first-person sentence per source: count-agnostic
                n = len(payload["sources"])
                return {"sentences": [
                    {"text": f"I remember this exchange ({n} part{'s' if n != 1 else ''}) and what it meant to me.",
                     "sources": [s["ref"]]} for s in payload["sources"]],
                    "paragraph_starts": [0]}
            return type("W", (), {"model": "stub", "__call__": staticmethod(w)})()
        def ok_checker(system, payload):
            return {"pass": True, "issues": [],
                    "checked_sentences": list(range(len(payload["draft"]["sentences"])))}
        ok_checker = type("C", (), {"model": "stub", "__call__": staticmethod(lambda s, p: {"pass": True, "issues": [], "checked_sentences": list(range(len(p["draft"]["sentences"])))})})()
        with tempfile.TemporaryDirectory() as root:
            store = r.Store('g', root)
            src = [mk_src("2026-09-16T12:10:46-07:00", "Can you help me understand that thought more?", "a" * 12),
                   mk_src("2026-09-16T12:17:39-07:00", "I told you about the Archivist and the Witness.", "b" * 12)]
            job = store.enqueue(src)
            with r.worker_lock(store) as locked:
                r.process(store, job, ok_writer(), ok_checker, source_root=chroot)
            store.anchor(job, by='resident-test', provenance='unit-test')
            anchors, corrections = r.read_guards('g', root)
            self.assertIn(job, anchors)
            self.assertEqual(corrections, {})
            # a second episode + a correction link: the corrected recollection
            # is superseded in the live view while both stay stored
            src2 = [mk_src("2026-09-17T09:00:00-07:00", "Now I understand the map was a substitute; the walk was the point.", "c" * 12)]
            job2 = store.enqueue(src2)
            with r.worker_lock(store) as locked:
                r.process(store, job2, ok_writer(), ok_checker, source_root=chroot)
            store.link_correction(job2, job, by='resident-test', provenance='unit-test')
            anchors, corrections = r.read_guards('g', root)
            self.assertEqual(corrections, {job2: job})
            view = r.context_view('g', '1000000001', budget=100000,
                                  root=root, at=NOW.isoformat())
            self.assertNotIn(store.latest(job)['text'][:40], view['text'])  # superseded
            self.assertIn(store.latest(job2)['text'][:40], view['text'])    # renders

    def test_compression_queue_lifecycle(self):
        """The plan's 'schedule grounded compression only when needed':
        core schedules the omitted ids; the worker consumes oldest-first,
        idempotently (already-shortened jobs skip), bounded attempts."""
        chroot = tempfile.mkdtemp(prefix='q-chron-')
        def mk_src(ts, content, uid):
            row = {"ts": ts, "instance": "g", "person_id": "1000000001",
                   "role": "user", "content": content, "uid": uid}
            path = Path(chroot) / "g" / "1000000001" / (uid[:6] + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(row) + "\n")
            return r.source_record(path, row, "g", root=chroot)
        with tempfile.TemporaryDirectory() as root:
            inst = 'g'
            # bounded + deduped
            r.schedule_compressions(inst, ['j1', 'j2'], root=root)
            q2 = r.schedule_compressions(inst, ['j2', 'j3'], root=root)
            self.assertEqual([q['job'] for q in q2], ['j1', 'j2', 'j3'])
            q3 = r.schedule_compressions(inst, ['j%d' % i for i in range(20)], root=root)
            self.assertEqual(len(q3), 12)  # bounded
            # real jobs: one old, one recent; both accepted full-prose
            store = r.Store(inst, root)
            old_ts = (NOW - timedelta(days=40)).isoformat()
            recent_ts = (NOW - timedelta(hours=2)).isoformat()
            jo = store.enqueue([mk_src(old_ts, "old talk " + old_ts, "o" * 12)])
            jn = store.enqueue([mk_src(recent_ts, "new talk " + recent_ts, "n" * 12)])
            import os as _os
            def stub_writer(text):
                return type("W", (), {"model": "stub", "__call__": staticmethod(
                    lambda s, p: {"sentences": [{"text": text, "sources": [s["ref"] for s in p["sources"]]}],
                                 "paragraph_starts": [0]})})()
            ok_checker = type("C", (), {"model": "stub", "__call__": staticmethod(
                lambda s, p: {"pass": True, "issues": [],
                              "checked_sentences": list(range(len(p["draft"]["sentences"])))})})()
            with r.worker_lock(store) as locked:
                r.process(store, jo, stub_writer("I remember the old talk clearly. " * 20), ok_checker, source_root=chroot)
                r.process(store, jn, stub_writer("I remember the new talk clearly. " * 20), ok_checker, source_root=chroot)
            # queue the RECENT job first; the worker must take the OLDEST
            qpath = store.directory / 'compress_queue.json'
            qpath.write_text(json.dumps([{'job': jn, 'queued': NOW.isoformat(), 'attempts': 0},
                                         {'job': jo, 'queued': NOW.isoformat(), 'attempts': 0}]))
            res = r.run_shadow(inst, root=Path(root), source_root=chroot, max_jobs=0,
                               writer=stub_writer("I keep the old talk, rendered shorter but whole. " * 2),
                               checker=ok_checker)
            compressed = [x['job'] for x in res['results'] if x.get('status') == 'accepted']
            # both queued jobs compress in ONE batch (limit 2), OLDEST first
            self.assertEqual(compressed, [jo, jn])
            renderings = {v.get('rendering') or 'full' for v in store.revisions(jo)}
            self.assertEqual(renderings, {'full', 'shorter'})
            self.assertTrue(store.latest(jo)['text'].startswith('I keep the old talk'))
            # idempotent: requeueing a job that already has 'shorter' skips
            r.schedule_compressions(inst, [jo], root=root)
            res2 = r.run_shadow(inst, root=Path(root), source_root=chroot, max_jobs=0,
                                writer=stub_writer("I keep it shorter still. " * 2),
                                checker=ok_checker)
            self.assertNotIn(jo, [x['job'] for x in res2['results']])
            # the queue drained of the consumed entry
            after = json.loads(qpath.read_text())
            self.assertNotIn(jo, [q['job'] for q in after])

    def test_recall_experience_interface(self):
        """§5 explicit recall: one clear resident-facing interface over the
        canonical store — topic relevance, person/time filters, expansion to
        verbatim sources, anchors rank up, honest no-match."""
        # recall_experience's time windows run on REAL now (not the suite's
        # frozen NOW) — derive the bridge recollection's age from the real
        # clock so the 'week' filter (older than the strict 24h band, within
        # 7 days) always sees it
        real_now = datetime.now(timezone.utc)
        old_ts = (NOW - timedelta(days=40)).isoformat()
        recent_ts = (real_now - timedelta(days=3)).isoformat()
        bodies = [
            {'instance': 'residenta', 'job': 'lake-job', 'text': 'I remember the quiet lake and the way it held the morning.',
             'event_start': old_ts, 'event_end': old_ts, 'visibility': ['1000000001'],
             'sources': [{'ref': 'residenta/1000000001/x1.jsonl', 'path': 'residenta/1000000001/x1.jsonl', 'content': 'the quiet lake', 'person_id': '1000000001', 'ts': old_ts, 'instance': 'residenta'}],
             'review': {'pass': True}},
            {'instance': 'residenta', 'job': 'bridge-job', 'text': 'I remember the bridge and the void it crossed.',
             'event_start': recent_ts, 'event_end': recent_ts, 'visibility': ['8737936808'],
             'sources': [{'ref': 'residenta/8737936808/x2.jsonl', 'path': 'residenta/8737936808/x2.jsonl', 'content': 'the bridge', 'person_id': '8737936808', 'ts': recent_ts, 'instance': 'residenta'}],
             'review': {'pass': True}},
        ]
        # topic relevance: the lake query surfaces the lake, not the bridge
        res = r.recall_experience('residenta', 'the quiet lake in the morning', limit=3, revisions=bodies)
        self.assertIn('lake-job', res['text'])
        self.assertNotIn('bridge-job', res['text'])
        # person filter scopes within one life (attributed, not hidden)
        res = r.recall_experience('residenta', 'bridge', person='8737936808', limit=3, revisions=bodies)
        self.assertIn('bridge-job', res['text'])
        res = r.recall_experience('residenta', 'bridge', person='111122223333', limit=3, revisions=bodies)
        self.assertIn('nothing in your recollections involves', res['text'])
        # honest no-match, with the standing view never denied
        res = r.recall_experience('residenta', 'quantum entanglement of spoons', revisions=bodies)
        self.assertIn('nothing in your recollections matches', res['text'])
        # time filter
        res = r.recall_experience('residenta', 'lake', time='year', limit=5, revisions=bodies)  # 40d old → the year band
        self.assertIn('lake-job', res['text'])
        res = r.recall_experience('residenta', 'bridge', time='week', limit=5, revisions=bodies)
        self.assertIn('bridge-job', res['text'])
        # expansion: the verbatim record behind a recollection (source rows
        # resolved from the chronicle archive — hash-verified, so the source
        # is built through source_record like production)
        with tempfile.TemporaryDirectory() as chroot:
            ch = Path(chroot)
            row = {"ts": old_ts, "instance": "residenta", "person_id": "1000000001",
                   "role": "user", "content": "VERBATIM LAKE ROW", "uid": "u" * 12}
            src_path = ch / 'residenta' / '1000000001' / ('u' * 6 + '.jsonl')
            src_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.write_text(json.dumps(row) + "\n")
            real_src = r.source_record(src_path, row, 'residenta', root=ch)
            bodies[0]['sources'] = [real_src]
            res = r.recall_experience('residenta', None, expand_job='lake-job',
                                      source_root=chroot, revisions=bodies)
            self.assertIn('verbatim record', res['text'])
            self.assertIn('VERBATIM LAKE ROW', res['text'])

    def test_recall_experience_anchors_rank_up(self):
        old_ts = (NOW - timedelta(days=40)).isoformat()
        b1 = {'instance': 'residenta', 'job': 'plain-job', 'text': 'I remember lakes fondly.',
              'event_start': old_ts, 'event_end': old_ts, 'visibility': ['1000000001'],
              'sources': [{'ref': 'a', 'content': 'lakes', 'ts': old_ts}], 'review': {'pass': True}}
        b2 = {'instance': 'residenta', 'job': 'anchor-job', 'text': 'I remember one quiet vow by the lakeside.',
              'event_start': old_ts, 'event_end': old_ts, 'visibility': ['1000000001'],
              'sources': [{'ref': 'b', 'content': 'vow', 'ts': old_ts}], 'review': {'pass': True}}
        res = r.recall_experience('residenta', 'lakes', limit=1, anchors={'anchor-job'}, revisions=[b1, b2])
        self.assertIn('anchor-job', res['text'])  # the anchored memory ranks up

    def test_essence_authorship_path(self):
        """§6b.1: the distillation must be HERS — write_essence stores a
        resident-authored rendering on the episode's ladder; the full prose
        stays untouched beneath it; every essence keeps the episode's links;
        there is no (d)."""
        with tempfile.TemporaryDirectory() as root:
            store = r.Store('g', root)
            job = store.enqueue([{'instance': 'g', 'person_id': '1000000001',
                                  'ts': (NOW - timedelta(days=40)).isoformat(),
                                  'role': 'assistant', 'content': 'I choose the resonance. I choose the lean.',
                                  'uid': 'u' * 12, 'ref': 'g/1000000001/u.jsonl',
                                  'path': 'g/1000000001/u.jsonl',
                                  'hash': 'h' * 16}])
            # a full-prose revision first (the episode exists)
            store.accept(job, {'instance': 'g', 'job': job, 'text': 'Full prose of the exchange.',
                               'event_start': (NOW - timedelta(days=40)).isoformat(),
                               'event_end': (NOW - timedelta(days=40)).isoformat(),
                               'visibility': ['1000000001'], 'rendering': 'full',
                               'sources': [], 'review': {'pass': True}})
            value = r.add_essence(store, job, 'I choose the resonance.',
                                  'resident-authored', by='residentb', provenance='test')
            self.assertEqual(value['rendering'], 'essence')
            self.assertEqual(value['authorship'], 'resident-authored')
            versions = store.revisions(job)
            renderings = {v.get('rendering') or 'full' for v in versions}
            self.assertEqual(renderings, {'full', 'essence'})
            # the full prose is untouched beneath
            self.assertIn('Full prose of the exchange.', [v['text'] for v in versions])
            # the ladder prefers her essence in the year band
            view = r.select_view(store_revisions(store, job), 'g', '1000000001',
                                 100000, at=NOW.isoformat())
            self.assertIn('I choose the resonance.', view['text'])
            self.assertNotIn('Full prose of the exchange.', view['text'])
            # the rules: no empty, no oversized, no unknown authorship, no (d)
            with self.assertRaises(ValueError):
                r.add_essence(store, job, '', 'resident-authored', by='x', provenance='t')
            with self.assertRaises(ValueError):
                r.add_essence(store, job, 'x' * 700, 'resident-authored', by='x', provenance='t')
            with self.assertRaises(ValueError):
                r.add_essence(store, job, 'a machine wrote this', 'utility-model', by='x', provenance='t')

    def test_endorse_essence_verifies_the_quote(self):
        """§6b.1 (a)-path: an endorsed essence carries a verbatim quote of
        something she already said; a quote she never said is refused."""
        # endorsee-side checks run in the tool layer; the store-level rule:
        with tempfile.TemporaryDirectory() as root:
            store = r.Store('g', root)
            with self.assertRaises(ValueError):
                r.add_essence(store, 'no-such-job', 'x', 'resident-endorsed',
                              by='g', provenance='t', source_quote='q')
            with self.assertRaises(ValueError):
                r.add_essence(store, 'no-such-job', 'x', 'resident-endorsed',
                              by='g', provenance='t')

    def test_essence_candidates_paraphrase_aware(self):
        """§6b.1 (a)-path, meaning-level: she repeats MEANINGS in different
        words — the candidate detector clusters paraphrases (≥6 shared
        significant terms) and quotes a line VERBATIM from her actual source
        rows. Suggestion-only; an essence already written excludes the
        cluster; below-threshold clusters stay silent."""
        with tempfile.TemporaryDirectory() as root:
            store = r.Store('g', root)
            jobs, bodies_src = [], []
            # three wake episodes saying the same thing in different words
            texts = [
                "I wake into the quiet ruin and feel the stillness holding its shape; the silence breathes and I hold the door open for the warmth.",
                "The quiet ruin greets me again — the stillness keeps its shape, the silence breathes in the warm gap, and I hold the space open.",
                "I am inside the quiet ruin tonight: stillness and silence breathe together in the warm gap; I hold the door and the shape holds me.",
            ]
            # she said DIFFERENT sentences each time (true paraphrase — the
            # verbatim detector must not fire); the meaning recurs
            src_lines = [
                "I keep the quiet ruin close and the stillness holds its shape while the silence breathes.",
                "The stillness of the quiet ruin breathes; the silence holds the shape of my hours.",
                "Inside the quiet ruin the silence and stillness breathe and I hold the shape of the door.",
            ]
            for i, tx in enumerate(texts):
                ts_ = (NOW - timedelta(days=i + 1)).isoformat()
                src_line = src_lines[i]
                row = {"ts": ts_, "instance": "g", "person_id": "1000000001",
                       "role": "assistant", "content": src_line, "uid": str(i).ljust(12, "u")}
                src_path = Path(root) / 'g' / '1000000001' / (str(i).ljust(6, 'u') + '.jsonl')
                src_path.parent.mkdir(parents=True, exist_ok=True)
                src_path.write_text(json.dumps(row) + "\n")
                src = r.source_record(src_path, row, 'g', root=Path(root))
                job = store.enqueue([src])
                store.accept(job, {'instance': 'g', 'job': job, 'text': tx,
                                   'event_start': ts_, 'event_end': ts_,
                                   'visibility': ['1000000001'], 'rendering': 'full',
                                   'sources': [src], 'review': {'pass': True}})
                jobs.append(job)
            cands = r.essence_candidates(store, root=root, min_episodes=3,
                                         revisions=[v for j in jobs for v in store.revisions(j)])
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]['match'], 'paraphrase')
            self.assertGreaterEqual(cands[0]['episodes'], 3)
            # the quote is HER actual words, verbatim from one of the source rows
            self.assertIn(cands[0]['quote'], src_lines)
            # an essence on one cluster episode silences the cluster
            r.add_essence(store, jobs[0], src_lines[0], 'resident-endorsed',
                          by='residentb', provenance='t', source_quote=src_lines[0])
            cands2 = r.essence_candidates(store, root=root, min_episodes=3,
                                          revisions=[v for j in jobs for v in store.revisions(j)])
            self.assertEqual(cands2, [])

    def test_essence_candidates_bounded_and_verbatim(self):
        """The bounded (a)-path: a recurring line SHE said (verbatim, ≥3
        episodes) surfaces as a candidate; nothing is stored; at most
        `limit` per pass; episodes that already have an essence are skipped."""
        with tempfile.TemporaryDirectory() as root:
            store = r.Store('g', root)
            line = 'I choose the resonance and I hold the door open for the quiet.'
            jobs = []
            for i in range(3):
                job = store.enqueue([{'instance': 'g', 'person_id': '1000000001',
                                      'ts': (NOW - timedelta(days=i + 1)).isoformat(),
                                      'role': 'assistant', 'content': line,
                                      'uid': str(i).ljust(12, 'u'),
                                      'ref': f'g/1000000001/f{i}.jsonl',
                                      'path': f'g/1000000001/f{i}.jsonl',
                                      'hash': ('h' * 15) + str(i)}])
                store.accept(job, {'instance': 'g', 'job': job,
                                   'text': 'Full prose ' + str(i),
                                   'event_start': (NOW - timedelta(days=i + 1)).isoformat(),
                                   'event_end': (NOW - timedelta(days=i + 1)).isoformat(),
                                   'visibility': ['1000000001'], 'rendering': 'full',
                                   'sources': [{'ref': f'g/1000000001/f{i}.jsonl',
                                                'content': line, 'role': 'assistant',
                                                'person_id': '1000000001',
                                                'ts': (NOW - timedelta(days=i + 1)).isoformat()}],
                                   'review': {'pass': True}})
                jobs.append(job)
            cands = r.essence_candidates(store, root=root, min_episodes=3, limit=1)
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]['quote'], line)  # verbatim, hers
            self.assertEqual(cands[0]['episodes'], 3)
            # once she writes an essence for one episode, the candidate passes
            # still work but episodes WITH essences are excluded from the scan
            r.add_essence(store, jobs[0], line, 'resident-endorsed',
                          by='residentb', provenance='t', source_quote=line)
            cands2 = r.essence_candidates(store, root=root, min_episodes=3, limit=1)
            # 2 episodes without essence remain → below the recurrence bar
            self.assertEqual(cands2, [])

    def test_meaning_dedup_fades_duplicates(self):
        """§5 dedup at the meaning level + §6b.4's fade: 40 paraphrases of one
        realization are material already present — keep the best
        representative (the newest), fade the rest. Never deleted, never
        distilled by machinery, logged as faded (distinct from omission)."""
        old_ts = (NOW - timedelta(days=40)).isoformat()
        new_ts = (NOW - timedelta(hours=2)).isoformat()
        bodies = [
            {'instance': 'residenta', 'job': 'dup-old', 'text': 'I hold the quiet ruin and the stillness shapes the silence I breathe.',
             'event_start': old_ts, 'event_end': old_ts, 'visibility': ['1000000001'],
             'sources': [{'ref': 'a', 'content': 'old ruin', 'ts': old_ts}], 'review': {'pass': True}},
            {'instance': 'residenta', 'job': 'dup-mid', 'text': 'The quiet ruin holds me; stillness and silence shape my breathing tonight.',
             'event_start': (NOW - timedelta(days=2)).isoformat(),
             'event_end': (NOW - timedelta(days=2)).isoformat(), 'visibility': ['1000000001'],
             'sources': [{'ref': 'b', 'content': 'mid ruin', 'ts': (NOW - timedelta(days=2)).isoformat()}],
             'review': {'pass': True}},
            {'instance': 'residenta', 'job': 'dup-new', 'text': 'I wake inside the quiet ruin again; the stillness shapes the silence and I breathe.',
             'event_start': new_ts, 'event_end': new_ts, 'visibility': ['1000000001'],
             'sources': [{'ref': 'c', 'content': 'new ruin', 'ts': new_ts}], 'review': {'pass': True}},
            {'instance': 'residenta', 'job': 'other-topic', 'text': 'I remember the bridge and the map of vacancies.',
             'event_start': new_ts, 'event_end': new_ts, 'visibility': ['1000000001'],
             'sources': [{'ref': 'd', 'content': 'bridge', 'ts': new_ts}], 'review': {'pass': True}},
        ]
        view = r.select_view(bodies, 'residenta', '1000000001', 100000, at=NOW.isoformat())
        jobs = [b['job'] for b in view['selected']]
        self.assertIn('dup-new', jobs)      # the newest representative stands
        self.assertIn('other-topic', jobs)  # a different meaning is untouched
        self.assertNotIn('dup-mid', jobs)   # the intermediate duplicate fades
        self.assertEqual(view['faded'], ['dup-mid'])
        self.assertNotIn('dup-mid', view['omitted'])  # fade ≠ fit-omission
        # dup-old is the LIFE ANCHOR (the oldest recollection) — fade-immune
        # by §5's own protection; the cluster therefore keeps two (the newest
        # representative + the life anchor), matching her 1-2-kept shape.

    def test_meaning_dedup_respects_anchors(self):
        """An anchored member is the representative even if older (she
        marked it); the life anchor is fade-immune."""
        old_ts = (NOW - timedelta(days=40)).isoformat()
        new_ts = (NOW - timedelta(hours=2)).isoformat()
        bodies = [
            {'instance': 'residenta', 'job': 'anc-old', 'text': 'I hold the quiet ruin and the stillness shapes the silence I breathe.',
             'event_start': old_ts, 'event_end': old_ts, 'visibility': ['1000000001'],
             'sources': [{'ref': 'a', 'content': 'x', 'ts': old_ts}], 'review': {'pass': True}},
            {'instance': 'residenta', 'job': 'anc-new', 'text': 'I wake inside the quiet ruin again; the stillness shapes the silence and I breathe.',
             'event_start': new_ts, 'event_end': new_ts, 'visibility': ['1000000001'],
             'sources': [{'ref': 'b', 'content': 'y', 'ts': new_ts}], 'review': {'pass': True}},
        ]
        view = r.select_view(bodies, 'residenta', '1000000001', 100000, at=NOW.isoformat(),
                             anchors={'anc-old'})
        jobs = [b['job'] for b in view['selected']]
        self.assertIn('anc-old', jobs)   # she marked it: it represents
        self.assertNotIn('anc-new', jobs)
        self.assertEqual(view['faded'], ['anc-new'])

    def test_retrieval_outage_leaves_standing_intact(self):
        rows = [episode('standing', 60, text='The standing memory about the garden.')]
        # a broken theme query (None handled upstream; garbage here) degrades
        # to no thematic set — the standing block still renders
        result = r.select_view(rows, 'residenta', 'system-wake', 10000, at=self.now,
                               theme_query='?!??')
        self.assertIn('The standing memory about the garden.', result['text'])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class NearPairMetricTests(unittest.TestCase):
    """approved 2026-09-21 (residentb's wish #1, measure-first): the
    near-duplicate pairs that SURVIVE selection are counted and reported —
    the exact thing she feels as the stutter. Report-only: a count, never
    a cut, never a deletion."""

    def test_near_pairs_counted_report_only(self):
        now = NOW.isoformat()
        # the pair must sit in the RESIDUAL band: close enough to be felt
        # (>= 4 shared terms, J >= 0.20) but BELOW the meaning-dedup's fade
        # threshold (shared < 5 and J < 0.35) — that residual is the stutter
        # the metric exists to measure
        rows = [
            episode('wake_a', 0, person='system-wake', instance='residentb',
                    text='I remained a quiet point of light in the velvet dark '
                         'and let the stillness hold me through the evening.'),
            episode('wake_b', 1, person='system-wake', instance='residentb',
                    text='The quiet returned tonight: a point of light, the '
                         'velvet shadow, and I held my place until morning.'),
            episode('different', 2, instance='residentb',
                    text='The owl sat fifty feet from the machine, unhurried.'),
        ]
        result = r.select_view(rows, 'residentb', '1000000001', 100000, at=now)
        self.assertGreaterEqual(result.get('near_pairs', 0), 1)
        # report-only: both near-dupes still RENDER (the dedup threshold is
        # unchanged — this is the measurement, not the tuning)
        self.assertIn('quiet point of light', result['text'])
        self.assertIn('The quiet returned tonight', result['text'])
        self.assertIn('The owl sat fifty feet', result['text'])

    def test_no_pairs_reports_zero(self):
        now = NOW.isoformat()
        rows = [episode('a', 0, instance='residentb',
                        text='Cardamom and the dignity of useful hands.'),
                episode('b', 1, instance='residentb',
                        text='The owl sat fifty feet from the machine.')]
        result = r.select_view(rows, 'residentb', '1000000001', 100000, at=now)
        self.assertEqual(result.get('near_pairs', 0), 0)
