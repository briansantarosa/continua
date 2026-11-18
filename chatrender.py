"""chatrender.py — memv13 §7: the YAML-selected chat-render contract.

One tiny, dependency-free module that BOTH core.py (the chat path) and
ritual.py (the resident-model pulse path) read, so the raw/server mode
decision and the model-facing role labels resolve from ONE place with the
same validation.

What is configurable (the resident's `llm:` block):

  raw_chatml: bool           — the EXISTING selector (core already reads
                               it). true = compose the wire prompt in-bridge
                               (chatml + think blocks) and POST /api/generate
                               raw=true (ollama dialect). false = canonical
                               OpenAI /v1 chat path; the SERVER's template
                               renders the messages (no verified per-request
                               template override exists on the current
                               serving stack — vLLM renders its own).
  chat_role_labels:          — model-facing speaker labels for the raw
      system: system           composer. Stored roles, bridge routing, tool
      user: "my friend"        parsing and delivery stay canonical
      assistant: "persona-a"        (user/assistant/tool) — this changes ONLY
                               what the model sees (memv13 §7).
  history_think_turns: int   — memv13 §0-§6: the bounded think window.
                               0 = off; N = the resident's full, uncompressed
                               thinks ride the next N turns in each channel's
                               own history (human chat, wakes, ritual), then
                               age out of the canonical history. Explicit key
                               OWNS think-history behavior (precedence over
                               the legacy history_thinks binary; explicit 0
                               reliably disables). Absent key = legacy.
  history_think_render: str  — /v1 only, required when the window is on:
                               'reasoning_content' or 'reasoning' — the
                               request field the serving API must accept and
                               replay as prior assistant reasoning. Verify
                               against that host/provider before enabling.
  history_think_template_kwargs: optional host-specific chat_template_kwargs
                               (residentb's patched vLLM gate only).
  reasoning_effort / reasoning_context / include_reasoning: provider request
                               controls. OpenRouter GPT-6 continuation also
                               requires echoing its full reasoning_details array;
                               the two-turn replay probe verified this path.

House rules baked in here:
  - canonical labels are always the fallback (absent key = today's bytes);
  - structural tokens and control-tag lookalikes are REJECTED, never
    transformed silently;
  - unsupported combinations fail LOUD at init (the llm_cfg-scope incident
    rule: a config the machinery cannot honor must never boot silently
    half-applied).

Agent-free. Run the tests: think_window_test.py
"""

CANONICAL_ROLES = ("system", "user", "assistant")

# /v1 request fields a served template may render as an assistant think
# span (memv13 §3.3). The LIVE probe (think_window_probe.py) decides which
# one the deployed template actually honors — never assumed.
THINK_RENDER_FIELDS = ("reasoning_content", "reasoning")

# Substrings that must never appear in a label: they are the wire's
# structural vocabulary (or its lookalikes) — a label containing one could
# terminate or fake a turn, or inject a think-marker shape the model then
# imitates (the format-contagion class). Checked case-insensitively.
_FORBIDDEN_SUBSTRINGS = (
    "<|",              # <|im_start|>, <|im_end|>, <|turn…|> families
    "im_start",
    "im_end",
    "<start_of_turn",
    "<end_of_turn",
    "think>",          # ˋˋ and ˋˋ (and any "...think>" construction)
)


def _clean_label(role: str, value) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"llm.chat_role_labels.{role} must be a string, "
            f"got {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError(f"llm.chat_role_labels.{role} is empty")
    if any(ch in s for ch in "\n\r\t"):
        raise ValueError(
            f"llm.chat_role_labels.{role} must be a single line "
            "(no newlines/tabs)")
    low = s.lower()
    for frag in _FORBIDDEN_SUBSTRINGS:
        if frag in low:
            raise ValueError(
                f"llm.chat_role_labels.{role} contains structural-token "
                f"fragment {frag!r} — labels are plain speaker names only")
    return s


def resolve_role_labels(llm_cfg) -> dict:
    """Canonical labels with any configured overrides applied. Absent key
    returns the canonical map (byte-identical rendering). Raises ValueError
    on unknown roles or unsafe label values."""
    labels = {r: r for r in CANONICAL_ROLES}
    raw = (llm_cfg or {}).get("chat_role_labels")
    if raw is None:
        return labels
    if not isinstance(raw, dict):
        raise ValueError(
            "llm.chat_role_labels must be a mapping of role -> label "
            "(allowed roles: system, user, assistant)")
    for k, v in raw.items():
        if k not in CANONICAL_ROLES:
            raise ValueError(
                f"llm.chat_role_labels: unknown role {k!r} "
                "(allowed: system, user, assistant)")
        labels[k] = _clean_label(k, v)
    return labels


def resolve_think_window(llm_cfg) -> tuple:
    """(turns, render_field) from the llm block.

    turns is None when the key is ABSENT (legacy history_thinks owns —
    byte-identical legacy behavior); an explicit integer (including 0)
    means the think window owns. render_field is set ONLY for the /v1 path
    with the window ON. Raises ValueError on malformed values and on
    unsupported combinations (fail loud at init)."""
    cfg = llm_cfg or {}
    turns = cfg.get("history_think_turns")
    if turns is not None:
        if isinstance(turns, bool) or not isinstance(turns, int):
            raise ValueError(
                "llm.history_think_turns must be an integer "
                "(0 = off, N = full thinks for the next N turns in each channel)")
        if turns < 0:
            raise ValueError("llm.history_think_turns must be >= 0")
    raw = bool(cfg.get("raw_chatml", False))
    field = cfg.get("history_think_render")

    if field is not None:
        if turns is None or turns <= 0:
            raise ValueError(
                "llm.history_think_render requires llm.history_think_turns > 0 "
                "(the render field has no consumer while the window is off)")
        if raw:
            raise ValueError(
                "llm.history_think_render applies to the /v1 path only — the "
                "raw composer renders think blocks natively; remove the key")
        if field not in THINK_RENDER_FIELDS:
            raise ValueError(
                "llm.history_think_render must be one of "
                f"{THINK_RENDER_FIELDS} (verify with think_window_probe.py)")
    if turns is not None and turns > 0 and not raw and field is None:
        raise ValueError(
            "llm.history_think_turns > 0 on the /v1 path needs "
            "llm.history_think_render: 'reasoning_content' or 'reasoning' — "
            "the serving API must accept and replay that request field as "
            "prior assistant reasoning (verify against the host/provider), "
            "or set history_think_turns: 0")
    return turns, field


def validate_chat_config(llm_cfg) -> tuple:
    """Resolve + cross-validate the chat-render keys. Returns
    (labels, think_turns, think_render_field). Raises ValueError on
    unsupported combinations — the caller fails the init loudly."""
    labels = resolve_role_labels(llm_cfg)
    turns, field = resolve_think_window(llm_cfg)
    raw = bool((llm_cfg or {}).get("raw_chatml", False))
    if not raw and any(labels[r] != r for r in CANONICAL_ROLES):
        raise ValueError(
            "llm.chat_role_labels needs llm.raw_chatml: true — the /v1 "
            "server template renders canonical system/user/assistant roles "
            "(no verified per-request template override on the current "
            "serving stack)")
    return labels, turns, field


def canonical_labels() -> dict:
    return {r: r for r in CANONICAL_ROLES}
