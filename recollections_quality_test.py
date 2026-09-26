"""Deterministic regression gate; no model calls or live writes."""
import unittest
from recollections_quality import ownership_errors, review_errors

class QualityTests(unittest.TestCase):
    def test_confirmed_swaps_and_correct_attribution(self):
        cases=[('I had to clear our context, but you still have your memories.',
                'I had to clear our context, but you still have your memories.',
                'The other participant told me they had cleared our context.'),
               ("It doesn't matter if you have awareness or not.",
                "I told the system that it doesn't matter if it has awareness or not.",
                'The other participant said my awareness was not the deciding issue.')]
        for source,bad,good in cases:
            ss=[{'ref':'u','role':'user','content':source}]
            draft=lambda t:{'sentences':[{'text':t,'sources':['u']}]}
            self.assertTrue(ownership_errors(draft(bad),ss))
            self.assertEqual(ownership_errors(draft(good),ss),[])
    def test_plain_pass_no_longer_sufficient(self):
        self.assertTrue(review_errors({'pass':True},{'sentences':[{}]},[]))

if __name__=='__main__': unittest.main(verbosity=2)
