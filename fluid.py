"""fluid.py — Phase F (memorybuild2 §5b, the designer's idea 2026-09-30): the
fluid budget. On turns when the variable layers render under their caps,
the idle flows to ONE borrower: the raw history — "the room nobody used
this turn goes to the conversation."

Structure note (honest scope, 2026-10-01): the spec listed tool_result
and wake_state as idle layers, but both ride INSIDE history/user
messages at assembly (tool results are role='tool' messages in the
window; wake-state is part of the wake payload's prompt) — counting
them would double-count. The assembly-time idle layers are: **notes,
juggle, doc** — the three that render into the system prompt with their
own caps. The doc's slack is normally ~0 (it fills to cap by design);
the float's real feed is juggle/notes on quiet days.

Boundaries held: identity never lends (the chosen, the persona, the
doc's rendered text are never shrunk); history floats target→cap and
NEVER beyond the declared cap; the guard (`fluid_max_prompt_tokens`) is
the fit's business — the final ContextBudget fit still owns the token
ceiling, so the top-up only adds what the fit will accept.

Fail-open everywhere; DEFAULT OFF (memory.fluid_budget: true enables;
CONTINUA_FLUID=0 kills). Run: python3
fluid_test.py
"""
import os


def enabled(cfg=None):
    """yaml-gated, default OFF; the env kill switch wins (§ITER)."""
    if os.environ.get("CONTINUA_FLUID", "") == "0":
        return False
    try:
        mem = (cfg or {}).get("memory") or {}
        return bool((mem.get("fluid_budget") or False) is True)
    except Exception:
        return False


def idle_bytes(actuals, caps):
    """Σ(max(0, cap − actual)) over the layers both dicts share. Pure."""
    total = 0
    for k, cap in (caps or {}).items():
        if cap is None:
            continue
        actual = (actuals or {}).get(k)
        if actual is None:
            # a layer that did not render at all lends its whole cap
            total += int(cap)
            continue
        try:
            total += max(0, int(cap) - int(actual))
        except (TypeError, ValueError):
            continue
    return total


def float_target(target, idle, cap):
    """The borrower's window this turn: target + idle, never above the
    declared cap, never below the target (the quiet-side floor)."""
    try:
        target = int(target)
    except (TypeError, ValueError):
        return None
    room = max(0, int(cap) - target) if cap else 0
    return target + max(0, min(int(idle or 0), room))


def top_up(history, evicted, budget_chars, hard_cap_chars=None):
    """Re-add evicted window messages to the history, NEWEST first (the
    exchanges nearest the live conversation), within budget_chars and
    never past hard_cap_chars of history content total.

    Pair integrity: a leading orphan assistant reply (its user message
    not included) is skipped — the re-added window never opens on a
    reply to something she can't see.

    MUTATES evicted in place (pops what it re-adds): the messages move
    back into the working window, so the turn-end summary fold folds
    exactly what is still evicted — no double representation. The
    caller's enqueue copies the list AFTER assembly, so the mutation
    lands before the fold reads it.
    Returns (history, added_chars)."""
    if not evicted or budget_chars <= 0:
        return history, 0
    added = 0
    picked = []                    # newest-first walk
    i = len(evicted) - 1
    while i >= 0 and added < budget_chars:
        m = evicted[i]
        size = len(str(m.get("content") or ""))
        if added + size > budget_chars:
            break
        picked.append(m)
        added += size
        i -= 1
    if not picked:
        return history, 0
    window = list(reversed(picked))   # replay order: oldest → newest
    # orphan-assistant guard: the window must not OPEN on a reply to
    # something she can't see — drop assistant replies from the oldest
    # end until it opens on a user message
    while window and window[0].get("role") == "assistant":
        added -= len(str(window[0].get("content") or ""))
        window.pop(0)
    if not window:
        return history, 0
    # hard cap: never push the history past its declared ceiling —
    # drop from the window's OLDEST end until the total fits
    if hard_cap_chars:
        cur = sum(len(str(m.get("content") or "")) for m in history)
        over = cur + added - int(hard_cap_chars)
        while window and over > 0:
            dropped = len(str(window[0].get("content") or ""))
            window.pop(0)
            over -= dropped
            added -= dropped
        if not window:
            return history, 0
    for m in window:
        evicted.remove(m)          # in place: the fold skips them
        history.append(m)
    return history, added