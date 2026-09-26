"""Offline layer contract after book retirement. No live-store dependencies."""
import unittest
from unittest.mock import patch
import core


class LayerTests(unittest.TestCase):
    def test_absent_and_unknown_off(self):
        for value in ({}, None, {'injection': {'bogus': True}}):
            layers = core._normalize_memory_layers(value)
            self.assertFalse(any(v['enabled'] for v in layers.values()))
            self.assertNotIn('bogus', layers)

    def test_bool_and_map(self):
        layers = core._normalize_memory_layers({'injection': {'roster': True,
            'recollections': {'enabled': True, 'cap_bytes': 999}}})
        self.assertTrue(layers['roster']['enabled'])
        self.assertEqual(layers['recollections']['cfg']['cap_bytes'], 999)
        self.assertNotIn('self_book', layers)
        self.assertNotIn('person_book', layers)

    def test_roster_survives_retirement(self):
        c = core.SagentCore.__new__(core.SagentCore)
        c.instance_id = 'test'
        c._mem_layers = core._normalize_memory_layers({'injection': {'roster': True, 'talking_with_line': True}})
        with patch('people.load_roster', return_value={}):
            block = c._build_continua_block('123', 'hello', [])
        self.assertIn('You are talking with', block)
        self.assertIn('PEOPLE YOU KNOW', block)
        self.assertNotIn('WHO YOU ARE', block)

    def test_all_off_no_headers(self):
        c = core.SagentCore.__new__(core.SagentCore)
        c.instance_id = 'test'
        c._mem_layers = core._normalize_memory_layers({})
        self.assertEqual(c._build_continua_block('123', 'hello', []), '')

    def test_episodic_cap_and_attribution(self):
        c = core.SagentCore.__new__(core.SagentCore)
        c.instance_id = 'test'
        c._mem_layers = core._normalize_memory_layers({'injection': {
            'episodic_recall': {'enabled': True, 'cap_chars': 8}}})
        with patch('recall.recall', return_value=[{'attribution': 'Alex yesterday',
                'role': 'user', 'content': 'abcdefghTHIS MUST BE CUT'}]):
            block = c._build_continua_block('123', 'hello', [])
        self.assertIn('Alex yesterday (user): abcdefgh', block)
        self.assertNotIn('THIS MUST BE CUT', block)


if __name__ == '__main__':
    unittest.main()
