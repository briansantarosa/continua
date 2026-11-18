"""context_scale — memorybuild.md Phase 9 (§SCALE, v8 R32–R35): one percent
law, one helper.

THE LAW (the binding laws #10 + #9):
  - every byte-shaped surface = max(today's floor, percent × C), where
    C = P × 3.0 (the floor density) and P is the same prompt budget the
    allocator derives (the fill mode's (W − G − M) × target);
  - floors = TODAY'S PROVEN VALUES (R33: nothing shrinks — read from the
    resident's own yaml where the floor is per-resident);
  - ceilings only where natural (R34) — and percent-derived there;
  - counts and serving knobs NEVER scale (R35) — they are not here;
  - env overrides win where they exist today;
  - an undeclared window → {} — every caller keeps today's constants
    (fail-open to today = fail-open to the floor).

Consumers adopt surface by surface (the build file's steps 1–8), each with
a no-regression test: every surface ≥ today for BOTH residents.
"""
import logging
import os

logger = logging.getLogger("continua.context_scale")

# the percents (of C) and their per-resident floors, in one table:
#   surface:        (percent, ceiling-or-None, floor-key-in-llm-or-fixed)
TABLE = {
    "history_chars":   (8.0,  None,      "max_history_chars"),
    "juggle_total":    (15.0, None,      15000),
    "juggle_thread":   (3.0,  None,      10000),
    "notes_cap":       (1.0,  ("max", 0.5, 8000), 2000),
    "tool_result_cap": (0.5,  ("max", 0.5, 16000), 4000),
    "wake_state":      (3.0,  ("min", None, 24000), 12000),
    "read_page_ceiling": (3.0, None,      40000),
    "writer_budget":   (1.0,  None,      2400),
    "ritual_budget":   (30.0, None,      14000),
    "ritual_input":    (2.0,  None,      2400),
    "ritual_chunk":    (20.0, None,      45000),
    "ritual_turn":     (2.0,  None,      900),
}


def C_for(cfg):
    """C = P × 3.0 (the floor density) — 0 when the window is undeclared."""
    import token_budget as _tb
    _w = _tb.window_from_config(cfg or {})
    if not _w or not _w[2]:
        return 0
    return int(_w[2] * 3.0)


def scale_for(cfg: dict) -> dict:
    """The per-agent byte budgets. {} when the window is undeclared —
    callers keep today's constants (fail-open to the floor)."""
    cfg = cfg or {}
    llm = cfg.get("llm") or {}
    import token_budget as _tb
    _w = _tb.window_from_config(cfg)
    if not _w or not _w[2]:
        return {}
    total, _G, P, _F = _w
    C = P * 3.0
    out = {"margin": _tb.margin_for(total), "C": C, "P": P}
    # the per-resident floors (R33): the ritual knobs' floors are the
    # RESIDENT'S OWN yaml values (today's proven numbers — e.g. residenta's
    # ritual budget is 40000 after S1, not the legacy 14000)
    _rv = (((cfg.get("continua") or {}).get("ritual") or {}).get("review")
           or {})
    _yaml_floors = {
        "ritual_budget": int(_rv.get("budget_chars") or 14000),
        "ritual_input": int(_rv.get("input_cap") or 2400),
        "ritual_chunk": int(_rv.get("chunk_chars") or 45000),
    }
    for name, (percent, ceiling, floor) in TABLE.items():
        if floor == "max_history_chars":
            floor = int(llm.get("max_history_chars") or 25000)
        if name in _yaml_floors:
            floor = _yaml_floors[name]
        v = max(int(floor), int(percent * C / 100.0))
        if ceiling is not None:
            kind, cpercent, cval = ceiling
            if kind == "max":   # the ceiling RISES with C (percent-derived)
                cl = max(int(cval), int((cpercent or 0) * C / 100.0))
            else:               # ("min"): a hard ceiling
                cl = int(cval)
            v = min(v, cl)
        out[name] = v
    # A5 (memorybuild2 §5; R43 ratified 2026-09-30): the history TARGET
    # is first-class — the yaml value if present (residenta 12K at 32K,
    # deliberately), else the history cap itself; never above the cap.
    _ht = llm.get("history_target_chars")
    _hist = out.get("history_chars")
    out["history_target"] = min(int(_ht), _hist) if _ht else _hist
    return out


def env_capped(name, value):
    """The env override wins where one exists today (the value override is
    the rollback); otherwise the computed value."""
    env_keys = {
        "notes_cap": "CONTINUA_NOTES_CAP_CHARS",
        "tool_result_cap": "CONTINUA_SANDBOX_RESULT_CHARS",
        "wake_state": "CONTINUA_WAKE_STATE_CHARS",
    }
    key = env_keys.get(name)
    if key and os.getenv(key):
        try:
            return int(os.getenv(key))
        except ValueError:
            pass
    return value
