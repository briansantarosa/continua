"""the designer's 2026-10-07 sampling ruling: agent turns inherit hosted defaults.

The direct ritual caller is exercised on each serving dialect; explicitly
passing the old compatibility argument must not serialize temperature.
"""
import pathlib
import unittest
from unittest.mock import patch

import ritual


class _Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class HostSamplingTests(unittest.TestCase):
    def test_ritual_agent_payloads_omit_temperature(self):
        responses = [
            _Response({"response": "</think>raw answer"}),
            _Response({"choices": [{"message": {"content": "v1 answer"}}]}),
            _Response({"message": {"content": "ollama answer"}}),
        ]
        with patch("requests.post", side_effect=responses) as post, \
             patch("llm_debug.write_call"):
            ritual.ask_her_messages(
                "model", "http://localhost:11434",
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "u"}],
                raw_chatml=True, temperature=0.7)
            ritual.ask_her_messages(
                "model", "https://openrouter.ai/api/v1",
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "u"}],
                temperature=0.7)
            ritual.ask_her_messages(
                "model", "http://localhost:11434",
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "u"}],
                temperature=0.7)

        raw_payload = post.call_args_list[0].kwargs["json"]
        v1_payload = post.call_args_list[1].kwargs["json"]
        ollama_payload = post.call_args_list[2].kwargs["json"]
        self.assertNotIn("temperature", raw_payload["options"])
        self.assertNotIn("temperature", v1_payload)
        self.assertNotIn("temperature", ollama_payload["options"])

    def test_core_agent_paths_do_not_serialize_temperature(self):
        src = pathlib.Path("core.py").read_text(encoding="utf-8")
        self.assertIn("self.temperature = None", src)
        self.assertNotIn('"temperature": self.temperature', src)
        self.assertNotIn("temperature=self.temperature", src)


if __name__ == "__main__":
    unittest.main()
