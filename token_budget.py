"""Measured token budgeting and the window allocator (memory plan §6g chunk 4;
memorybuild.md Phase 7 §WIN + Phase 9 §SCALE).

Two modes:
  - LEGACY (llm.prompt_utilisation): capacity, not a demand to fill —
    the pre-§WIN behavior, byte-preserved.
  - FILL (llm.memory_fill_target): the window is filled to the target with
    the reply space (num_predict) reserved FIRST (the voice yields last).
    The margin is §SCALE's M = max(1024, 1% of the window). A fill target
    on a window under MIN_FILL_WINDOW is REFUSED (ERROR log, legacy path
    runs) — the 32K minimum (R12).

Enforcement stays character-based (the existing ordinal eviction is
unchanged); the allocator converts the token budget to a char cap using
the MEASURED density from real turns' prompt token counts, with a
conservative fallback when tokenization is unavailable. llama.cpp servers
expose POST /tokenize (verified on the lab); ollama does not — the raw
path's prompt_eval_count feeds the measured density (§WIN step 0: the
GENERATED count never derives density).
"""
import json
import logging

logger = logging.getLogger("continua.token_budget")

FALLBACK_DENSITY = 3.7          # chars/token, the measured prompt-mix figure
DENSITY_FLOOR = 3.0             # conservative floor: never overshoot the window
MARGIN_TOKENS = 1024            # legacy margin (template/serving variance)
MIN_UTILISATION = 0.10
MAX_UTILISATION = 1.0
MAX_FILL_TARGET = 0.99          # the fill ceiling (R26: 99% is the sizing rule)
MIN_PROMPT_TOKENS = 2048
MIN_FILL_WINDOW = 32768         # R12: the declared-window minimum for fill mode


def margin_for(total_tokens):
    """§SCALE (R32): M = max(1024, int(0.01 × W))."""
    return max(1024, int(0.01 * int(total_tokens or 0)))


def window_from_config(cfg, instance_id=""):
    """(total_tokens, generation_reserve, prompt_budget_tokens, utilisation)
    from llm config; (None, None, None, None) when unconfigured.

    [CONTINUA] 2026-11-14 evening (the tool-round reserve, the designer go: the
    wake vetoes' root cause): a 0.99 fill leaves ~1% slack (~700 chars at
    32K) — less than ONE tool result. Every wake that filled its window
    and then called a tool vetoed its own round 2 (the graceful paths
    ship what was said, but the tool work silently never happens). The
    fill math now reserves tool-round headroom: min(max_tool_iterations,
    4) × 1024 tok (~13K chars at 3.2 c/t — the observed result sizes with
    margin). `llm.tool_round_reserve_tokens` overrides explicitly; 0
    disables. The voice still yields last; the TOOLS are her hands — they
    get room before the fill does."""
    llm = (cfg or {}).get("llm") or {}
    total = llm.get("total_context_tokens")
    if not total:
        return None, None, None, None
    total = int(total)
    reserve = int(llm.get("num_predict") or 8192)
    fill = llm.get("memory_fill_target")
    if fill is not None:
        if total < MIN_FILL_WINDOW:
            logger.error(
                "[Window] %s: memory_fill_target on a %d-token window is "
                "REFUSED (minimum %d, R12) — the legacy allocator runs",
                instance_id or "?", total, MIN_FILL_WINDOW)
        else:
            utilisation = min(MAX_FILL_TARGET, float(fill))
            margin = margin_for(total)
            _tool_rounds = int(llm.get("max_tool_iterations") or 0)
            _tool_reserve = int(llm.get(
                "tool_round_reserve_tokens",
                min(max(_tool_rounds, 0), 4) * 1024))
            usable = max(0, total - reserve - margin - _tool_reserve)
            prompt_budget = max(MIN_PROMPT_TOKENS,
                                int(usable * utilisation))
            return total, reserve, prompt_budget, utilisation
    # legacy path (no fill target, or the fill refused): byte-preserved
    margin = int(llm.get("context_margin_tokens") or MARGIN_TOKENS)
    utilisation = float(llm.get("prompt_utilisation") or 0.60)
    utilisation = max(MIN_UTILISATION, min(MAX_UTILISATION, utilisation))
    usable = max(0, total - reserve - margin)
    prompt_budget = max(MIN_PROMPT_TOKENS, int(usable * utilisation))
    return total, reserve, prompt_budget, utilisation


def token_count_via_endpoint(text, chat_base_url, timeout=10):
    """Measured token count via a llama.cpp POST /tokenize; None when the
    endpoint is absent/unreachable (ollama) — the caller falls back."""
    if not text:
        return 0
    try:
        import requests
        base = chat_base_url.rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        response = requests.post(base.rstrip('/') + '/tokenize',
                                 json={"content": text}, timeout=timeout,
                                 allow_redirects=False)
        if response.status_code != 200:
            return None
        tokens = response.json().get("tokens")
        return len(tokens) if isinstance(tokens, list) else None
    except Exception:
        return None


def measured_density(chars, tokens, fallback=FALLBACK_DENSITY):
    """chars/token from the last real turn's PROMPT token count; the
    conservative floor applies. A LOWER density means MORE tokens per char
    — the floor protects the window by shrinking the char cap. (§WIN step
    0: the GENERATED count never derives density — that was the bug.)"""
    if not tokens or not chars:
        return fallback
    density = chars / tokens
    return max(DENSITY_FLOOR, density)


def char_cap(prompt_budget_tokens, density):
    """The char ceiling the existing character-based layers enforce."""
    return int(prompt_budget_tokens * density)


def report(prompt_chars, prompt_tokens, budget_tokens, density, source,
           utilisation):
    """The per-turn accounting line's numbers, all real or honestly labeled."""
    over = bool(prompt_tokens and prompt_tokens > budget_tokens)
    return {
        "chars": prompt_chars,
        "tokens_estimated": prompt_tokens,
        "budget_tokens": budget_tokens,
        "density": round(density, 2),
        "density_source": source,   # 'endpoint' | 'measured' | 'fallback'
        "utilisation": utilisation,
        "over_budget": over,
    }
