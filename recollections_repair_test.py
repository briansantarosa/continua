"""Quality-hold repair regressions, offline and temporary only."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import recollections as r

class RepairTests(unittest.TestCase):
    def rows(self,n=200):
        start=datetime(2026,9,10,tzinfo=timezone.utc)
        out=[]
        for i in range(n):
            ts=(start+timedelta(minutes=15*i)).isoformat()
            for role,text in [('user','BOILERPLATE'),('assistant','I checked my journal. '*50)]:
                out.append(dict(instance='residentb',person_id='system-wake',role=role,
                                content=text,ts=ts,ref=f'{i}-{role}'))
        return out
    def test_cadence_old_history_not_held_by_open_tail(self):
        rows=self.rows();at=datetime.fromisoformat(rows[-1]['ts']).timestamp()
        eps,info=r.plan_episodes(rows,at,require_pairing=False)
        refs=[s['ref'] for e in eps for s in e]
        self.assertGreater(len(refs),190)
        self.assertEqual(len(refs),len(set(refs)))
        self.assertTrue(all(sum(len(s['content']) for s in e)<=16000 for e in eps))
        self.assertTrue(all(s['role']=='assistant' for e in eps for s in e))
        self.assertEqual(info['disposition'][rows[-1]['ref']],'not_closed')
    def test_only_single_row_can_exceed_cap(self):
        rows=self.rows(5)
        rows[3]['content']='I '+ 'x'*41000
        eps,info=r.plan_episodes(rows,1e12,require_pairing=False)
        large=[e for e in eps if sum(len(s['content']) for s in e)>16000]
        self.assertEqual(len(large),1);self.assertEqual(len(large[0]),1)
    def test_oversize_dialogue_splits_without_loss(self):
        rows=self.rows(1)
        rows[0]['content']='x'*9000;rows[1]['content']='y'*9000
        eps,_=r.plan_episodes(rows,1e12,require_pairing=True)
        self.assertEqual([len(e) for e in eps],[1,1])
        self.assertEqual(sum(len(s['content']) for e in eps for s in e),18000)
    def test_split_parts_carry_shared_link(self):
        rows=self.rows(1)
        rows[0]['content']='x'*9000;rows[1]['content']='y'*9000
        eps,info=r.plan_episodes(rows,1e12,require_pairing=True)
        chains={e[0]['episode_link']['chain'] for e in eps}
        self.assertEqual(len(chains),1)
        self.assertEqual(sorted(e[0]['episode_link']['part'] for e in eps),[1,2])
        for e in eps:
            L=e[0]['episode_link']
            self.assertEqual(L['source_count'],2)
            self.assertEqual(L['source_chars'],18000)
            self.assertNotIn('source_refs',L)  # link must not duplicate payload
    def test_review_hold_retains_bytes_and_blocks_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=r.Store('residentb',tmp);source=self.rows(1)[1]
            job=store.enqueue([source])
            value=dict(instance='residentb',job=job,review={'pass':True},sources=[source],text='I remember.')
            store.accept(job,value)
            before=store.latest(job)
            store.hold_for_review(job,'confirmed attribution failure','test')
            self.assertEqual(store.latest(job),before)
            self.assertEqual(r.read_revisions('residentb',tmp),[])
            with self.assertRaises(ValueError):store.accept(job,value)
            with store.db() as db:self.assertEqual(db.execute('select count(*) from coverage').fetchone()[0],1)

if __name__=='__main__': unittest.main(verbosity=2)
