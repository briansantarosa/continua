"""llmpool.py — the shared-pool discipline (2026-09-29 evening, the designer's call).

The lab server runs 4 slots inside ONE shared KV cache of n_ctx tokens
(verified: /props total_slots=4, n_ctx=131072). Since the fill went to
99%, any two of our in-flight prompts sum past the pool (~90K + ~90K >
131K) and the server 500s — sometimes cleanly ("Context size has been
exceeded"), sometimes as a speculative-decoding assertion. The
concurrency was always there (per-user turn pipelines + the 15-min wake
cadence); the arithmetic only turned lethal when the windows filled.

The house's answer (design ruling: "do b", corrected 2026-09-30 after the
09:41/09:53/15:24/15:36 overflows) — ONE MIND, ONE ATTENTION, and the
priority is QUEUE ORDER ONLY, never concurrency:

  AT MOST ONE of our sequences is in flight, PERIOD. Chats wait for
  wakes too (the first build let chats pre-empt a wake mid-generation —
  that WAS the overflow: both in flight, ~176K > 131K).
  When the pool frees, a waiting CHAT enters before a waiting WAKE;
  wakes never jump waiting chats and serialize among themselves.
  A wake will not defer forever: after wake_max_defer_s (default 600)
  it proceeds anyway — the overflow it risks is survivable (retries,
  the shed, the arrival capture), and a starved wake is its own failure.

Fail-open: any error deciding = no gating (she runs). Kill switch:
CONTINUA_LLMPOOL=0.

Scope note: this gates THIS process's calls (chats + wakes). The nightly
ritual runs in its own service and already holds the quiet-window lock
against wakes; a chat colliding with the ritual remains possible (rare,
retryable) — a file-based pool would close it if it ever matters.
Run: python3 llmpool_test.py
"""
import os
import threading
import time


class _Pool:
    """One shared-pool gate per server base_url."""

    def __init__(self, wake_max_defer_s: float = 600.0):
        self._cv = threading.Condition()
        self._chat_active = 0
        self._chat_waiters = 0
        self._wake_active = 0
        self._wake_max_defer_s = float(wake_max_defer_s)

    def acquire(self, priority: str, timeout: float = None):
        """priority 'chat' or 'wake'. Returns True when acquired, False
        only when a wake exceeded its max defer (timeout path)."""
        deadline = time.monotonic() + self._wake_max_defer_s
        with self._cv:
            if priority == "chat":
                self._chat_waiters += 1
                try:
                    chat_deadline = (time.monotonic() + timeout
                                     if timeout else None)
                    # ONE SEQUENCE AT A TIME: a chat waits for wakes too.
                    # Priority governs QUEUE ORDER (who enters first when
                    # the pool frees), never concurrency.
                    while self._chat_active > 0 or self._wake_active > 0:
                        if chat_deadline is None:
                            self._cv.wait(timeout=0.1)
                            continue
                        remaining = chat_deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        self._cv.wait(timeout=min(remaining, 0.1))
                finally:
                    self._chat_waiters -= 1
                self._chat_active += 1
                return True
            # wake: defer while a chat is active or WAITING (never jump
            # the queue), and serialize among wakes. deadline = the earlier
            # of the max-defer horizon and the caller's own timeout.
            if timeout is not None:
                deadline = min(deadline, time.monotonic() + timeout)
            while (self._chat_active > 0 or self._chat_waiters > 0
                   or self._wake_active > 0):
                if time.monotonic() >= deadline:
                    return False
                # wait only UNTIL the deadline — the expiry must fire on
                # time, not whenever the last 5s sleep happens to end
                self._cv.wait(timeout=min(
                    5.0, max(0.05, deadline - time.monotonic())))
            self._wake_active += 1
            return True

    def release(self, priority: str):
        with self._cv:
            if priority == "chat":
                self._chat_active = max(0, self._chat_active - 1)
            else:
                self._wake_active = max(0, self._wake_active - 1)
            self._cv.notify_all()


_pools: dict = {}
_pools_lock = threading.Lock()


def pool_for(base_url: str = "", wake_max_defer_s: float = 600.0) -> _Pool:
    key = (base_url or "").split("//")[-1].split("/")[0]
    with _pools_lock:
        p = _pools.get(key)
        if p is None:
            p = _Pool(wake_max_defer_s=wake_max_defer_s)
            _pools[key] = p
        return p


def enabled() -> bool:
    return os.environ.get("CONTINUA_LLMPOOL", "1") != "0"


def gate(base_url: str, priority: str, user_id: str = None,
         timeout: float = None):
    """The context manager core uses. FAIL-OPEN: any error deciding, the
    kill switch, or a disabled pool → a null gate (she runs)."""
    try:
        if not enabled():
            return _Null()
        p = pool_for(base_url)
        got = p.acquire(priority, timeout=timeout)
        if not got:
            return _Null()   # the wake's max-defer expiry: proceed unsafely
        return _Held(p, priority)
    except Exception:
        return _Null()


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Held:
    def __init__(self, pool: _Pool, priority: str):
        self._pool = pool
        self._priority = priority

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._pool.release(self._priority)
        return False