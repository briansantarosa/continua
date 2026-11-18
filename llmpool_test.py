"""llmpool_test.py — the shared-pool discipline (2026-09-29, the designer: "do b").
Chats take the pool immediately (queueing behind chats); wakes defer
while a chat is active or WAITING; a starved wake proceeds after the max
defer; kill switch; fail-open.
Run: python3 llmpool_test.py
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llmpool        # noqa: E402


class PoolSemanticsTests(unittest.TestCase):
    def test_chat_excludes_chat(self):
        p = llmpool._Pool()
        self.assertTrue(p.acquire("chat"))
        self.assertFalse(p.acquire("chat", timeout=0.05))
        p.release("chat")
        self.assertTrue(p.acquire("chat", timeout=1.0))

    def test_chat_waits_for_wake(self):
        """The 2026-09-30 correction: chats wait for wakes too — at most
        ONE sequence in flight, ever. No chat pre-emption mid-wake."""
        p = llmpool._Pool()
        self.assertTrue(p.acquire("wake"))
        self.assertFalse(p.acquire("chat", timeout=0.1))   # must wait
        p.release("wake")
        self.assertTrue(p.acquire("chat", timeout=1.0))
        p.release("chat")

    def test_chat_enters_before_wake_when_pool_frees(self):
        """Priority = queue order only: wake holds; chat AND wake wait;
        on release the waiting CHAT enters first."""
        p = llmpool._Pool()
        got_c, got_w = [], []
        self.assertTrue(p.acquire("wake"))

        def _chat_b():
            if p.acquire("chat", timeout=3.0):
                got_c.append(True)
                p.release("chat")               # hold it only briefly
            else:
                got_c.append(False)

        t_c = threading.Thread(target=_chat_b)
        t_c.start(); time.sleep(0.1)
        t_w = threading.Thread(target=lambda: got_w.append(
            p.acquire("wake", timeout=3.0)))
        t_w.start(); time.sleep(0.1)
        p.release("wake")
        t_c.join(3); t_w.join(3)
        self.assertEqual(got_c, [True])          # chat entered first
        self.assertEqual(got_w, [True])          # then the wake
        p.release("wake")

    def test_wake_defers_while_chat_active(self):
        p = llmpool._Pool()
        p._wake_max_defer_s = 0.2
        self.assertTrue(p.acquire("chat"))
        self.assertFalse(p.acquire("wake"))   # defers → max-defer expiry
        p.release("chat")
        self.assertTrue(p.acquire("wake", timeout=1.0))
        p.release("wake")

    def test_wake_defers_while_chat_WAITING(self):
        """No queue-jumping: a wake stays behind a chat that is itself
        still waiting for another chat. The wake runs on a thread so the
        main thread can release chat A while it defers."""
        p = llmpool._Pool()
        p._wake_max_defer_s = 0.3
        got_b, got_w = [], []
        self.assertTrue(p.acquire("chat"))          # chat A holds
        t_b = threading.Thread(target=lambda: got_b.append(
            p.acquire("chat", timeout=2.0)))        # chat B waits
        t_b.start()
        time.sleep(0.1)
        t_w = threading.Thread(target=lambda: got_w.append(
            p.acquire("wake")))                     # the wake defers
        t_w.start()
        time.sleep(0.1)
        p.release("chat")                           # A releases → B enters
        t_b.join(3)
        self.assertEqual(got_b, [True])             # B got in before the wake
        time.sleep(0.4)                             # the wake's defer expires
        self.assertEqual(got_w, [False])            # it never jumped the queue
        p.release("chat")                           # B releases
        self.assertTrue(p.acquire("wake", timeout=1.0))
        p.release("wake")

    def test_wakes_serialize(self):
        p = llmpool._Pool()
        self.assertTrue(p.acquire("wake"))
        self.assertFalse(p.acquire("wake", timeout=0.05))
        p.release("wake")

    def test_starved_wake_proceeds(self):
        """A wake held off past its max defer PROCEEDS (the gate contract:
        the pool is skipped, not the wake). Hold the chat in the SAME pool
        object the gate resolves for that base_url."""
        pool = llmpool.pool_for("http://test-shared")
        pool._wake_max_defer_s = 0.15
        self.assertTrue(pool.acquire("chat"))
        time.sleep(0.2)
        g = llmpool.gate("http://test-shared", "wake")
        self.assertIs(type(g).__name__, "_Null")   # expiry → proceeds unsafely
        pool.release("chat")

    def test_per_server_pools(self):
        a = llmpool.pool_for("http://127.0.0.1:8080/v1")
        b = llmpool.pool_for("http://127.0.0.1:11434/v1")
        self.assertIsNot(a, b)
        self.assertIs(a, llmpool.pool_for("http://127.0.0.1:8080/v1"))

    def test_kill_switch(self):
        with mock.patch.dict(
                os.environ, {"CONTINUA_LLMPOOL": "0"}):
            g = llmpool.gate("http://x", "chat")
            self.assertIs(type(g).__name__, "_Null")


if __name__ == "__main__":
    unittest.main()