# [CONTINUA FORK — from ~/Sagent @ e276913, 2026-09-07]
# -*- coding: utf-8 -*-
"""Process-wide Sagent LLM admission limiter.

W10: optional safety valve for cross-bridge fairness against the
shared 2-slot llama.cpp backend. Default disabled
(``SAGENT_LLM_ADMISSION_LIMIT=0``). When ``N>0``, an
``asyncio.Semaphore(N)`` is created lazily on first use and shared
across all 13 SagentCore instances in this process.

CRITICAL: ``asyncio.Semaphore(0)`` in CPython 3.10+ never acquires
and would deadlock every chat call. The implementation explicitly
branches on ``n <= 0`` and returns a no-op context manager, rather
than ever passing 0 to ``asyncio.Semaphore``.

See plan/v2/W10-llm-admission-limiter.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _no_op_acquire():
    """No-op async context manager used when admission is disabled."""
    yield


def _resolve_limit() -> int:
    """Read ``SAGENT_LLM_ADMISSION_LIMIT`` from env. Invalid → 0 (logged)."""
    raw = os.getenv("SAGENT_LLM_ADMISSION_LIMIT", "0")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid SAGENT_LLM_ADMISSION_LIMIT=%r; defaulting to 0 (no limiter).",
            raw,
        )
        return 0
    return max(0, n)


def _get_admission_semaphore() -> asyncio.Semaphore:
    """Lazily build the singleton admission semaphore (called only when limit > 0)."""
    sem = globals().get("_admission_semaphore")
    if sem is None:
        n = _resolve_limit()
        if n <= 0:
            raise RuntimeError(
                "_get_admission_semaphore called with n<=0; use make_admission() instead."
            )
        sem = asyncio.Semaphore(n)
        globals()["_admission_semaphore"] = sem
    return sem


def make_admission() -> "contextlib.AbstractAsyncContextManager":
    """Return an async context manager that bounds concurrent LLM calls.

    Behavior:
      * ``SAGENT_LLM_ADMISSION_LIMIT=0`` (default) → no-op (no waiting).
      * ``SAGENT_LLM_ADMISSION_LIMIT=N>0`` → ``asyncio.Semaphore(N)``.

    Per Design A in W10, the bridge holds the token for the entire
    turn (including tool calls and retry sleeps). Acceptable for the
    safety-valve use case: when LIMIT=1, this forces Sagent-wide
    serial chat, which is the rollback lever for cross-bridge
    fairness.
    """
    if _resolve_limit() <= 0:
        return _no_op_acquire()
    return _get_admission_semaphore()
