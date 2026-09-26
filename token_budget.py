"""Measured token budgeting and the window allocator (memory plan §6g chunk 4).

The declared window (configs: llm.total_context_tokens) minus generation
reserve and margin is the prompt budget; utilisation (default 0.60) is
capacity, not a demand to fill. Enforcement stays character-based (the
existing ordinal eviction is unchanged); the allocator converts the token
budget to a char cap using the MEASURED density from real turns'
usage.prompt_eval_count, with a conservative fallback when tokenization is
unavailable. llama.cpp servers expose POST /tokenize (verified on the lab);
ollama does not.
"""
import json

FALLBACK_DENSITY = 3.7          # chars/token, the measured prompt-mix figure
DENSITY_FLOOR = 3.0             # conservative floor: never overshoot the window
MARGIN_TOKENS = 1024            # template/serving variance headroom
MIN_UTILISATION = 0.10
MAX_UTILISATION = 1.0
MIN_PROMPT_TOKENS = 2048


def window_from_config(cfg, instance_id=""):
    """(total_tokens, generation_reserve, prompt_budget_tokens, utilisation)
    from llm config; (None, None, None, None) when unconfigured."""
    llm = (cfg or {}).get("llm") or {}
    total = llm.get("total_context_tokens")
    if not total:
        return None, None, None, None
    total = int(total)
    reserve = int(llm.get("num_predict") or 8192)
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
    """chars/token from the last real turn; conservative floor applies.
    A LOWER density means MORE tokens per char — the floor protects the
    window by shrinking the char cap."""
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
