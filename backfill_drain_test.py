"""Agent-free scheduling tests. Stub passes; no database or model calls."""
import unittest
from backfill_drain import drain

class DrainTests(unittest.TestCase):
    def run_case(self, responses, wall=30):
        t=[0]; calls=[]; events=[]
        def run(inst, **kw):
            calls.append(inst); t[0]+=1
            return responses.pop(0)
        code=drain(['residentb','residenta'],1,wall,run=run,clock=lambda:t[0],
                   sleep=lambda n:t.__setitem__(0,t[0]+n),emit=events.append)
        return code,calls,events
    def test_alternates_and_does_not_stop_before_scan(self):
        more={'status':'shadow','counts':{'pending':0},'collection':{'queued':2},'results':[]}
        done={'status':'shadow','counts':{'pending':0,'quarantined':3},'collection':{'queued':0},'results':[]}
        code,calls,events=self.run_case([more,more,done,done])
        self.assertEqual(calls,['residentb','residenta','residentb','residenta'])
        self.assertEqual(code,0)
        self.assertIn('quarantine/exclusions',events[-1])
    def test_busy_does_not_starve_other_resident(self):
        code,calls,events=self.run_case([{'status':'busy'},{'status':'busy'}],wall=2)
        self.assertEqual(calls,['residentb','residenta'])
        self.assertEqual(code,2)
    def test_disabled_stops(self):
        code,calls,_=self.run_case([{'status':'disabled'}])
        self.assertEqual(code,1)
        self.assertEqual(calls,['residentb'])
    def test_exception_stops(self):
        def fail(*a,**kw): raise RuntimeError('test')
        self.assertEqual(drain(['residentb'],run=fail,emit=lambda x:None),1)
    def test_invalid_arguments(self):
        for instances,jobs,wall in [(['bad'],1,30),(['residentb'],0,30),(['residentb'],1,0)]:
            with self.assertRaises(ValueError): drain(instances,jobs,wall)

if __name__=='__main__': unittest.main(verbosity=2)
