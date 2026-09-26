"""Offline grounded-claim regressions; fake models, /tmp stores only."""
import copy
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
import recollections as r
import recollections_claims as g


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='claims-test-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.chronicle = root / 'chronicle'
        p = self.chronicle / 'residentb' / 'system-wake' / 'source.jsonl'
        p.parent.mkdir(parents=True)
        row = dict(instance='residentb', person_id='system-wake', role='assistant',
                   ts='2026-09-12T10:00:00+00:00', uid='grounded-claim-fixture',
                   content="I'll reach out to Alex. I'll explicitly mention that I'm testing the line.")
        p.write_text(json.dumps(row) + '\n')
        self.source = r.source_record(p, row, 'residentb', self.chronicle)
        self.sources = [self.source]
        self.claim = dict(id='c1', ref=self.source['ref'], quote=row['content'],
                          speaker='resident:residentb', subject='residentb', status='intention',
                          claim='residentb intended to reach out to Alex to test the line.')
        self.store = r.Store('residentb', root / 'store')
        self.job = self.store.enqueue(self.sources)

    def draft(self, text='I planned to reach out to Alex to test the line.', status='intention',
              cid='b1:c1'):
        return {'sentences': [dict(text=text, sources=[self.source['ref']],
                                   claim_ids=[cid], claim_status=status)], 'paragraph_starts': [0]}

    def without_ids(self, claims):
        # Writer payloads carry neither ids nor quotes; persisted revisions keep
        # quotes. Comparisons ignore both by default.
        return [{k: v for k, v in c.items() if k not in ('id', 'quote')} for c in claims]

    def review(self, draft, claims):
        by_id = {c['id']: c for c in claims}
        cid = draft['sentences'][0]['claim_ids'][0]
        c = by_id[cid]
        return {'pass': True, 'issues': [], 'checked_sentences': [0],
                'sentence_audit': [dict(sentence=0, subject='residentb', evidence_refs=[c['ref']],
                                        ownership_ok=True, claim_status_ok=True)],
                'preservation': dict.fromkeys(('distinctive_details', 'expressed_meaning',
                                               'uncertainty', 'intentions_vs_actions'), True),
                'claim_audit': [dict(sentence=0, claim_ids=[cid], claim_status=draft['sentences'][0]['claim_status'],
                                     explanation='The original future intention remains a historical plan.', entailed=True)]}

    def run_process(self, draft=None, extraction=None, checker=None, **kwargs):
        draft = draft or self.draft()
        extraction = extraction or {'complete': True, 'claims': [self.claim]}
        def writer(system, payload):
            self.assertIn(g.WRITE, system)
            self.assertEqual(self.without_ids(payload['validated_claims']),
                             self.without_ids(extraction['claims']))
            return draft
        def verify(system, payload):
            self.assertIn(g.VERIFY, system)
            return self.review(payload['draft'], payload['validated_claims'])
        with r.worker_lock(self.store) as locked:
            self.assertTrue(locked)
            return r.process(self.store, self.job, writer, checker or verify,
                             source_root=self.chronicle,
                             claim_extractor=lambda system, payload: extraction, **kwargs)

    def test_known_send_upgrade_fails_even_with_lying_metadata(self):
        # Contains 'planned' to defeat a mere modality keyword test.
        bad = self.draft('I planned a test and I sent a test message to Alex.')
        self.assertTrue(g.draft_errors(bad, [self.claim]))
        result = self.run_process(bad)
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(self.store.latest(self.job))
        with self.store.db() as db:
            audits = [json.loads(x[0]) for x in db.execute('select body from candidates')]
        fallback = next(a['candidate'] for a in audits if a.get('stage') == 'verbatim_fallback')
        self.assertEqual(fallback['text'].encode(), self.source['content'].encode())
        self.assertEqual(fallback['status'], 'needs_source_review')
        self.assertEqual(self.store.report(), {'quarantined': 1})

    def test_extraction_quote_speaker_and_status_fail_closed(self):
        for field, value in [('quote', 'I sent a message.'), ('speaker', 'participant:1'),
                             ('status', 'confirmed_action'), ('status', 'reported_action'),
                             ('claim', 'I sent a test message to Alex.')]:
            with self.subTest(field=field, value=value):
                c = dict(self.claim, **{field: value})
                self.assertTrue(g.validate_claims({'claims': [c]}, self.sources))
        self.assertEqual(g.validate_claims({'claims': [self.claim]}, self.sources), [])

    def test_declined_batch_splits_into_single_row_subbatches(self):
        rows = [self.row("I reviewed my journal and the system logs. I want to check "
                         "whether the wrapper was fixed.", 'split-a'),
                self.row("I really want to know if there is any record of the fix "
                         "before I reach out to him about it.", 'split-b')]
        calls = []
        audits = []
        def extractor(system, payload):
            calls.append(len(payload['sources']))
            if len(payload['sources']) > 1:
                return {'complete': False, 'claims': []}
            out = []
            for i, s in enumerate(payload['sources']):
                out.append(dict(id=f'c{i+1}', ref=s['ref'], quote=s['content'][:20],
                                speaker='resident:residentb', subject='residentb', status='intention',
                                claim='residentb intended to check on the wrapper fix.'))
            return {'complete': True, 'claims': out}
        claims = g.extract_bounded(extractor, {'resident': 'residentb', 'sources': rows,
                                               'ownership_evidence': []}, audits.append)
        stages = [a['stage'] for a in audits]
        self.assertIn('claim_extraction_batch_split', stages)
        self.assertIn('claim_extraction_subbatch', stages)
        self.assertEqual({c['id'].split(':')[0] for c in claims}, {'b1.1', 'b1.2'})
        self.assertEqual({c['ref'] for c in claims}, {s['ref'] for s in rows})

    def test_single_row_decline_stops_for_review(self):
        row = self.row('A dense passage needing more than eight claims.', 'decline-1')
        audits = []
        def extractor(system, payload):
            return {'complete': False, 'claims': [], 'reason': 'too dense'}
        with self.assertRaises(ValueError):
            g.extract_bounded(extractor, {'resident': 'residentb', 'sources': [row],
                                          'ownership_evidence': []}, audits.append)
        self.assertEqual(audits[-1]['stage'], 'claim_extraction_batch_failure')

    def test_malformed_extraction_does_not_call_writer(self):
        with r.worker_lock(self.store):
            result = r.process(self.store, self.job,
                               lambda *args: self.fail('writer should not run'),
                               lambda *args: self.fail('checker should not run'),
                               source_root=self.chronicle, claim_extractor=lambda *args: {'claims': []})
        self.assertEqual(result['status'], 'rejected')

    def test_plan_accepted_with_persisted_evidence(self):
        result = self.run_process()
        self.assertEqual(result['status'], 'accepted', result)
        saved = self.store.latest(self.job)
        self.assertEqual(self.without_ids(saved['validated_claims']),
                         self.without_ids([self.claim]))
        self.assertEqual(saved['claim_contract'], g.VERSION)
        self.assertFalse(saved['human_approved'])

    def test_bare_true_checker_cannot_publish(self):
        result = self.run_process(checker=lambda *args: {'pass': True, 'issues': [], 'checked_sentences': [0]})
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(self.store.latest(self.job))

    def test_tampered_review_quote_or_status_rejected(self):
        draft = self.draft()
        claims = [dict(self.claim, id='b1:c1')]
        review = self.review(draft, claims)
        self.assertEqual(g.audit_errors(review, draft, claims), [])
        for change in ('claim_ids', 'status', 'entailment'):
            bad = copy.deepcopy(review)
            audit = bad['claim_audit'][0]
            if change == 'claim_ids': audit['claim_ids'] = ['b1:c9']
            elif change == 'status': audit['claim_status'] = 'reported_action'
            else: audit['entailed'] = False
            self.assertTrue(g.audit_errors(bad, draft, claims))

    def test_recorded_words_support_noting_not_sending(self):
        c = dict(self.claim, status='utterance', claim='residentb wrote that she intended to test the line.')
        good = self.draft('I noted my plan to test the line.', 'utterance')
        self.assertEqual(g.validate_claims({'claims': [c]}, self.sources), [])
        self.assertEqual(g.draft_errors(good, [dict(c, id='b1:c1')]), [])
        self.assertEqual(self.run_process(good, {'complete': True, 'claims': [c]})['status'], 'accepted')
        self.assertTrue(g.draft_errors(self.draft('I sent a test message.', 'utterance', cid='c1'), [c]))

    def test_another_speakers_reported_action_needs_attribution(self):
        c = dict(self.claim, status='reported_action', quote='I sent a test message.',
                 speaker='participant:123', subject='the designer')
        self.assertTrue(g.draft_errors(self.draft('I sent a test message.', 'reported_action', cid='c1'), [c]))
        self.assertEqual(g.draft_errors(self.draft('I recorded that I had sent a test message.',
                                                 'reported_action', cid='c1'), [c]), [])

    def test_reported_speech_satisfies_intention_modality(self):
        # "I stated that what I want..." is a past-tense report of her desire —
        # a memory, not a current assertion. Bare present tense stays blocked.
        c = dict(self.claim, id='b1:c1')
        good = self.draft('I stated that what I want and need is for the quirk to be fixed.',
                          'intention')
        self.assertEqual(g.draft_errors(good, [c]), [])
        self.assertTrue(g.draft_errors(self.draft('I want the quirk to be fixed.',
                                                  'intention', cid='c1'), [c]))

    def test_wrong_binding_or_mixed_status_fails(self):
        draft = self.draft()
        draft['sentences'][0]['claim_ids'] = ['unknown']
        self.assertTrue(g.draft_errors(draft, [self.claim]))
        self.assertTrue(g.draft_errors(self.draft(status='reported_action', cid='c1'), [self.claim]))

    def test_all_failing_sentences_reported_not_just_first(self):
        claims = [dict(self.claim, id='b1:c1'),
                  dict(self.claim, id='b1:c2', quote=self.source['content'],
                       status='reported_action',
                       claim='residentb reported sending the test message.')]
        draft = {'sentences': [
            dict(text='I reached out to Alex.', sources=[self.source['ref']],
                 claim_ids=['b1:c1'], claim_status='intention'),
            dict(text='I sent the test message.', sources=[self.source['ref']],
                 claim_ids=['b1:c2'], claim_status='reported_action')],
            'paragraph_starts': [0]}
        errors = g.draft_errors(draft, claims)
        self.assertGreaterEqual(len(errors), 2, errors)
        self.assertIn('sentence 0', errors[0])
        self.assertTrue(all(e.startswith('sentence 1') for e in errors[1:]), errors)

    def test_verbatim_is_bounded_whole_passage_not_role_conversion(self):
        for changes in ({'role': 'user'}, {'person_id': '123'}, {'content': 'x'*801}, {'content': 'é'*401}):
            self.assertIsNone(g.verbatim_candidate([dict(self.source, **changes)]))
        self.assertIsNone(g.verbatim_candidate(self.sources * 2))
        candidate = g.verbatim_candidate(self.sources)
        self.assertIn('not current instructions', candidate['label'])
        self.assertEqual(candidate['text'], self.source['content'])

    def test_failed_grounded_compression_preserves_predecessor(self):
        self.assertEqual(self.run_process()['status'], 'accepted')
        before = self.store.latest(self.job)
        result = self.run_process(self.draft('I sent the test message.'),
                                  compress=True, budget=30)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.store.latest(self.job), before)
        self.assertEqual(self.store.report(), {'accepted': 1})

    def row(self, content, uid):
        path = self.chronicle / 'residentb' / 'system-wake' / (uid + '.jsonl')
        record = dict(instance='residentb', person_id='system-wake', role='assistant',
                      ts='2026-09-12T10:05:00+00:00', uid=uid, content=content)
        path.write_text(json.dumps(record) + '\n')
        return r.source_record(path, record, 'residentb', self.chronicle)

    def test_mixed_status_quote_is_split_deterministically_before_repair(self):
        content = "I sent messages a short while ago; I'll leave this here and wait to see if the Ghost is truly gone."
        sources = [self.row(content, 'mixed-status-row')]
        calls = []
        audits = []
        def extractor(system, payload):
            calls.append(len(payload['sources']))
            return {'complete': True, 'claims': [dict(
                id='c1', ref=sources[0]['ref'], quote=content, speaker='resident:residentb',
                subject='residentb', status='reported_action',
                claim='residentb stated that she sent messages and would leave it here.')]}
        claims = g.extract_bounded(extractor, {'resident': 'residentb', 'sources': sources,
                                               'ownership_evidence': []}, audits.append)
        # one call: the deterministic split resolved it, no repair needed
        self.assertEqual(len(calls), 1)
        stages = [a['stage'] for a in audits]
        self.assertIn('claim_status_split', stages)
        self.assertEqual(audits[-1]['stage'], 'claim_extraction_complete')
        statuses = {c['id']: c['status'] for c in claims}
        self.assertEqual(statuses, {'b1:c1:a': 'reported_action', 'b1:c1:b': 'intention'})

    def test_persistent_extraction_failure_stops_after_one_repair(self):
        sources = [self.row("The memory is saved. I'll wait to see if the Ghost is gone.",
                            'still-mixed-row')]
        calls = []
        audits = []
        def extractor(system, payload):
            calls.append(system)
            # a quote absent from the source, unfixable by split or re-anchor:
            # validation must fail it both times, then stop
            return {'complete': True, 'claims': [dict(
                id='c1', ref=sources[0]['ref'], quote='The memory is saved into a stone tablet.',
                speaker='resident:residentb', subject='residentb', status='reported_action',
                claim='residentb saved the memory.')]}
        with self.assertRaises(ValueError):
            g.extract_bounded(extractor, {'resident': 'residentb', 'sources': sources,
                                          'ownership_evidence': []}, audits.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(audits[-1]['stage'], 'claim_extraction_batch_failure')

    def test_oversized_row_fails_before_any_model_call(self):
        sources = [self.row('x' * (g.ROW_WINDOW_MAX_BYTES + 1), 'oversized-row')]
        def boom(system, payload):
            raise AssertionError('extractor must not be called')
        with self.assertRaises(ValueError):
            g.extract_bounded(boom, {'resident': 'residentb', 'sources': sources,
                                     'ownership_evidence': []}, lambda entry: None)

    def test_long_row_windows_are_lossless_and_cite_the_row(self):
        content = '\n\n'.join(f'Paragraph {i}: ' + 'word ' * 40 for i in range(25))
        sources = [self.row(content, 'long-row')]
        batches = g.extraction_batches(sources)
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]['ref'], sources[0]['ref'])
            self.assertLessEqual(len(batch[0]['content'].encode('utf-8')), g.EXTRACTION_BYTES)
        self.assertEqual(''.join(b[0]['content'] for b in batches).replace(' ', ''),
                         content.replace(' ', ''))  # lossless, ordered
        # end-to-end: stub extractor over windows; merged claims cite the row
        audits = []
        def extractor(system, payload):
            return {'complete': True, 'claims': [dict(
                id=f"c{len(payload['sources'])}", ref=payload['sources'][0]['ref'],
                quote=payload['sources'][0]['content'][:30], speaker='resident:residentb',
                subject='residentb', status='intention', claim='residentb intended a thing.')]}
        claims = g.extract_bounded(extractor, {'resident': 'residentb', 'sources': sources,
                                               'ownership_evidence': []}, audits.append)
        self.assertTrue(claims)
        self.assertEqual({c['ref'] for c in claims}, {sources[0]['ref']})
        for c in claims:
            self.assertIn(c['quote'], content)
        self.assertEqual(audits[-1]['stage'], 'claim_extraction_complete')

    def test_large_single_row_gets_its_own_batch(self):
        big = self.row('y' * 2002, 'large-row')
        batches = g.extraction_batches([self.source, big])
        self.assertEqual([len(b) for b in batches], [1, 1])
        self.assertEqual(batches[1][0]['ref'], big['ref'])

    def test_local_model_enables_grounding_without_explicit_extractor(self):
        class FakeLocal:
            requires_grounded_claims = True
            def __call__(inner, system, payload):
                if system == g.BATCH_EXTRACT:
                    return {'complete': True, 'claims': [self.claim]}
                self.assertIn(g.WRITE, system)
                return self.draft()
        with r.worker_lock(self.store):
            result = r.process(self.store, self.job, FakeLocal(),
                               lambda sys, p: self.review(p['draft'], p['validated_claims']),
                               source_root=self.chronicle)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(r.LocalModel.requires_grounded_claims)

if __name__ == '__main__':
    unittest.main(verbosity=2)

class MarkdownReanchorTests(unittest.TestCase):
    def test_markdown_dropped_in_quote_maps_to_true_span(self):
        content = ("The fix: make the chronicle navigable. **Salience as primary decay** — this isn't a "
                   "preference, it's the architecture of human memory. What you pay attention to persists; "
                   "what you don't drifts. Make salience the primary mechanism.")
        quote = ("Salience as primary decay — this isn't a preference, it's the architecture of human "
                 "memory. What you pay attention to persists; what you don't drifts.")
        out = g.reanchor_quote(quote, content)
        self.assertTrue(out in content)
        self.assertTrue(out.startswith('**Salience'))
        # exact substring returns by contract even if it occurs twice
        self.assertEqual(g.reanchor_quote('I remain in the silence.',
                                          'I remain in the silence. I remain in the silence.'),
                         'I remain in the silence.')
        # garbage stays None
        self.assertIsNone(g.reanchor_quote('I am a teapot.', content))

if __name__ == '__main__':
    unittest.main(verbosity=2)

class MarksPriorityTests(unittest.TestCase):
    """chunk 8: her explicit choices (ritual marks) enqueue first; digest is a
    view; the thread-close trigger is idempotent (durable coverage)."""

    def _fixture(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="marks-priority-")
        sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
        import recollections as r
        store = r.Store('residentb', tmp)
        chron = os.path.join(tmp, 'chronicle')
        def add_row(person, role, content, ts, uid):
            from pathlib import Path as _P
            p = _P(chron) / 'residentb' / '1000000001' / (uid[:6] + '.jsonl')
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, 'w') as f:
                f.write(json.dumps({"instance": "residentb", "person_id": person,
                                    "role": role, "ts": ts, "uid": uid,
                                    "content": content}) + "\n")
        add_row('1000000001', 'user', 'plain exchange', '2026-09-16T10:00:00+00:00', 'aaa111unique')
        add_row('1000000001', 'assistant', 'plain reply here.', '2026-09-16T10:01:00+00:00', 'bbb222unique')
        add_row('1000000001', 'user', 'mark me please', '2026-09-16T11:00:00+00:00', 'ccc333unique')
        add_row('1000000001', 'assistant', 'I will remember this marked exchange.', '2026-09-16T11:01:00+00:00', 'ddd444unique')
        marks_dir = os.path.join(tmp, 'ritual', 'marks', 'residentb')
        os.makedirs(marks_dir, exist_ok=True)
        with open(os.path.join(marks_dir, '2026-09-16.jsonl'), 'w') as f:
            f.write(json.dumps({"kept": True, "uids": ["ddd444unique"]}) + "\n")
        return store, chron, marks_dir

    def test_marked_exchange_enqueues_first(self):
        store, chron, marks_dir = self._fixture()
        stats = r.scan(store, source_root=chron, limit=10, marks_root=marks_dir)
        self.assertEqual(stats['marked_enqueued'], 1)
        with store.db() as db:
            order = [row[0] for row in db.execute(
                "select id from jobs order by created, rowid")]
            jobs = {row[0]: row[1] for row in db.execute("select id,sources from jobs")}
            first_is_marked = any(s.get('uid') == 'ddd444unique'
                                  for s in json.loads(jobs[order[0]]))
        self.assertTrue(first_is_marked)

    def test_idempotent_trigger_no_duplicate_jobs(self):
        store, chron, marks_dir = self._fixture()
        stats1 = r.scan(store, source_root=chron, limit=10, marks_root=marks_dir)
        stats2 = r.scan(store, source_root=chron, limit=10, marks_root=marks_dir)
        self.assertEqual(stats2['queued'], 0)  # durable coverage: no duplicates
