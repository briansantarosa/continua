"""tier_probe.py — P1 (fixtoolcalls.md §2.1): the tool-contract tier probe.

At bridge startup, once per resident (background thread, fail-open):
assign the tool-contract TIER for the model+backend pair, capture the
SERVING template hash, and detect churn against the pinned state.

Tiers (fixtoolcalls.md §2.1):
  A — native: backend renders tools= and parses tool_calls back
      (vLLM+parser, OpenRouter, llama.cpp --jinja with a recognized
      template). Proven by a live scripted round-trip.
  B — emulated native: the model's template knows tools but the serving
      path won't render them. Evidence: the template renders tool markup
      (llama.cpp /apply-template) while the native probe fails. Families:
      Qwen XML, Qwen JSON (§2.1).
  C — scaffolded: no template evidence (or the serving path is the raw
      composer, which today teaches no dialect). House fallback grammar.
  - — no tools declared for this resident; nothing to serve.

SERVING POLICY (the designer, 2026-11-10): any ring is always served with
llama.cpp --jinja — rings are Tier A by policy. This probe MEASURES the
contract; it never enforces the policy.

Template hash: sha256[:16] over the render of a FIXED canonical payload
(/apply-template on llama.cpp; /tokenize ids on vLLM; None where the
backend exposes no render oracle — OpenRouter, ollama-raw). Churn: a
hash change against logs/tier_state/<resident>.json logs a
[TierPinned] WARNING and re-records — keep serving on the pinned tier,
alert, refuse only when the pinned contract is no longer verifiable
(the refusal hook lands with the tier-aware send path; detection +
alert + record are this module).

Config pin: `llm.tier:` in the resident yaml overrides assignment
(recorded as pinned; probing still captures the hash).

Cost: ONE small generation per resident per probe run (temp 0,
max_tokens 120) — background, never boot-blocking. Kill switch:
CONTINUA_TIER_PROBE=0. Any fetch/parse error = a logged skip, never
boot friction. Run: python3 tier_probe_test.py
"""
import hashlib
import json
import os
import urllib.request

STATE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "tier_state")

# FIXED bytes — the template hash is only meaningful if the payload never
# changes. Do not edit without bumping CANONICAL_VERSION (which invalidates
# every pinned hash once, deliberately).
CANONICAL_VERSION = 1
_CANONICAL = {
    "messages": [
        {"role": "system", "content": "You have tools."},
        {"role": "user", "content": "Remember the rain."},
    ],
    "tools": [{
        "type": "function",
        "function": {"name": "save_my_memory",
                     "description": "Save a short note.",
                     "parameters": {"type": "object",
                                    "properties": {"content": {
                                        "type": "string"}},
                                    "required": ["content"]}}},
    ],
}


def _fetch(url, payload=None, timeout=8.0, api_key=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _server_root(base_url):
    b = str(base_url or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


def _sha16(s):
    return hashlib.sha256(s.encode("utf-8") if isinstance(s, str) else s
                          ).hexdigest()[:16]


# ------------------------------------------------------------- hashers

def _hash_llamacpp(base, fetch):
    d = fetch(_server_root(base) + "/apply-template", payload=dict(_CANONICAL))
    prompt = d.get("prompt") or d.get("formatted_prompt") or ""
    return _sha16(prompt) if prompt else None


def _hash_vllm(base, fetch):
    d = fetch(_server_root(base) + "/tokenize",
              payload={"messages": _CANONICAL["messages"],
                       "tools": _CANONICAL["tools"], "add_special_tokens": False})
    ids = d.get("tokens") or d.get("token_ids") or []
    return _sha16(json.dumps(ids)) if ids else None


# ------------------------------------------------------ native probe

def _native_probe_ok(base, model, fetch, api_key=None):
    """ONE small generation: does the backend accept tools= and return a
    STRUCTURED tool_calls object? That is the Tier A definition. Auth:
    hosted relays (OpenRouter) 401 an unauthenticated probe — pass the
    resident's key when the config carries one (never log it)."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "You have tools."},
                     {"role": "user",
                      "content": "Use the save_my_memory tool to save the "
                                 "note 'tier probe'."}],
        "tools": _CANONICAL["tools"],
        "tool_choice": "auto",
        "max_tokens": 120,
        "temperature": 0,
    }
    d = fetch(base.rstrip("/") + "/chat/completions", payload=payload,
              timeout=30.0, api_key=api_key)
    msg = ((d.get("choices") or [{}])[0].get("message")) or {}
    tcs = msg.get("tool_calls") or []
    for tc in tcs:
        fn = ((tc.get("function") or {}).get("name") or "")
        if fn.replace("function:", "") == "save_my_memory":
            return True
    return False


def _template_renders_tools(base, fetch):
    """llama.cpp only: does the model's OWN template render tool markup for
    the canonical payload? (Tier B evidence — template knows tools.)"""
    try:
        d = fetch(_server_root(base) + "/apply-template", payload=dict(_CANONICAL))
        prompt = d.get("prompt") or d.get("formatted_prompt") or ""
        return bool(prompt) and ("<tools>" in prompt or "tool_call" in prompt
                                 or "<function=" in prompt)
    except Exception:
        return False


# --------------------------------------------------------- assignment

def probe_instance(cfg, fetch=_fetch):
    """Assign the tier for one resident. Returns
    {'tier', 'dialect', 'template_hash', 'pinned', 'evidence', 'detail'}.
    Pure with an injected fetch — tests mock the server. NEVER raises."""
    llm = cfg.get("llm") or {}
    base = str(llm.get("chat_base_url") or llm.get("base_url") or "")
    model = str(llm.get("model") or "")
    raw = bool(llm.get("raw_chatml"))
    has_tools = bool(cfg.get("tools"))
    pin = str(llm.get("tier") or "").strip()
    # auth: core's own precedence (app_cfg openai_api_key -> env
    # OPENROUTER for openrouter endpoints -> env OPENAI_API_KEY); the
    # placeholder "ollama" is filtered inside _fetch. Never logged.
    api_key = (cfg.get("openai_api_key")
               or (os.getenv("OPENROUTER_API_KEY") if "openrouter" in base
                   else None)
               or os.getenv("OPENAI_API_KEY"))

    if not base or not has_tools:
        return {"tier": "-", "dialect": "-", "template_hash": None,
                "pinned": False, "evidence": [],
                "detail": "no tools declared — nothing to serve"}

    evidence, template_hash = [], None
    try:
        # CONFIG PIN short-circuits BEFORE generation — assignment is the
        # human's call; the probe still captures the hash (no LLM call).
        if pin:
            evidence.append(f"config pin llm.tier={pin} honored")
            tier, dialect = (pin.split(":", 1) + [""])[:2] if ":" in pin \
                else (pin, "native")
            try:
                if "openrouter" in base:
                    pass
                elif ":8090" in base or "vllm" in base:
                    template_hash = _hash_vllm(base, fetch)
                else:
                    template_hash = _hash_llamacpp(base, fetch)
            except Exception as e:
                evidence.append(f"hash skip: {e}")
            return {"tier": tier, "dialect": dialect,
                    "template_hash": template_hash, "pinned": True,
                    "evidence": evidence,
                    "detail": f"PINNED tier={tier} dialect={dialect} "
                              f"hash={template_hash}"}

        if "openrouter" in base:
            template_hash = None   # no render oracle on hosted relays
            evidence.append("openrouter: no render oracle — hash=None")
        elif ":8090" in base or "vllm" in base:
            try:
                template_hash = _hash_vllm(base, fetch)
                evidence.append(f"vllm /tokenize hash={template_hash}")
            except Exception as e:
                evidence.append(f"vllm hash skip: {e}")
        else:
            try:
                template_hash = _hash_llamacpp(base, fetch)
                evidence.append(f"apply-template hash={template_hash}")
            except Exception as e:
                evidence.append(f"llamacpp hash skip: {e}")

        tier, dialect = "C", "house"
        if raw:
            # the raw composer is the serving path: tools= is never sent,
            # so no backend capability can make it Tier A today
            evidence.append("raw_chatml serving path — native channel unused")
            tier, dialect = "C", "house-raw"
            if _template_renders_tools(base, fetch):
                tier, dialect = "B", "qwen-xml"
                evidence.append("template renders tool markup — Tier B "
                                "family available")
        else:
            try:
                if _native_probe_ok(base, model, fetch, api_key=api_key):
                    tier, dialect = "A", "native"
                    evidence.append("native round-trip: structured "
                                    "tool_calls returned")
                elif _template_renders_tools(base, fetch):
                    tier, dialect = "B", "qwen-xml"
                    evidence.append("native probe failed but template "
                                    "renders tool markup")
                else:
                    evidence.append("native probe failed; no template "
                                    "tool evidence")
            except Exception as e:
                evidence.append(f"native probe skip (fail-open): {e}")

        return {"tier": tier, "dialect": dialect,
                "template_hash": template_hash, "pinned": False,
                "evidence": evidence,
                "detail": f"tier={tier} dialect={dialect} "
                          f"hash={template_hash}"}
    except Exception as e:
        return {"tier": "C", "dialect": "house", "template_hash": None,
                "pinned": False, "evidence": [f"probe error: {e}"],
                "detail": f"probe failed open → Tier C: {e}"}


# -------------------------------------------------------------- state

def _state_path(resident):
    safe = "".join(c for c in str(resident) if c.isalnum() or c in "-_.")
    return os.path.join(STATE_DIR, f"{safe}.json")


def load_state(resident):
    try:
        with open(_state_path(resident)) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(resident, assignment):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        assignment = dict(assignment)
        import datetime as _dt
        assignment["probed_at"] = _dt.datetime.now().astimezone().isoformat(
            timespec="seconds")
        with open(_state_path(resident), "w") as f:
            json.dump(assignment, f, indent=2)
    except Exception:
        pass  # fail-open: state is an instrument, not a dependency


def current_for(resident):
    """The pinned assignment for a resident (chronicle provenance)."""
    st = load_state(resident) or {}
    return {"tier": st.get("tier"), "dialect": st.get("dialect"),
            "template_hash": st.get("template_hash")}


def enabled():
    return os.environ.get("CONTINUA_TIER_PROBE", "1") != "0"


def probe_all(configs, log=None, fetch=_fetch):
    """One pass over every resident at startup. Never raises. Churn: a
    template-hash change against the pinned state logs [TierPinned] WARN
    and re-records — serving stays on the pinned tier (fixtoolcalls §2.1).
    The fetch is injectable (tests mock the server)."""
    import logging
    log = log or logging.getLogger("tier_probe")
    if not enabled():
        log.info("[TierProbe] disabled (CONTINUA_TIER_PROBE=0)")
        return
    for name, cfg in (configs or {}).items():
        try:
            r = probe_instance(cfg or {}, fetch=fetch)
            prior = load_state(name)
            if (prior and prior.get("template_hash")
                    and r.get("template_hash")
                    and prior["template_hash"] != r["template_hash"]):
                log.warning(
                    "[TierPinned] %s: TEMPLATE CHANGED (hash %s → %s) — "
                    "serving on the pinned tier; investigate before "
                    "re-pinning. prior=%s", name, prior["template_hash"],
                    r["template_hash"], json.dumps(
                        {k: prior.get(k) for k in
                         ("tier", "dialect", "probed_at")}))
            elif (prior and prior.get("tier") and
                  prior["tier"] not in ("-", r["tier"]) and not r["pinned"]):
                log.warning(
                    "[TierPinned] %s: tier drifted %s → %s since last probe",
                    name, prior["tier"], r["tier"])
            save_state(name, r)
            log.info("[TierProbe] %s: %s%s", name, r["detail"],
                     (" | " + "; ".join(r["evidence"][:3]))
                     if r["evidence"] else "")
        except Exception as e:
            log.warning("[TierProbe] %s: probe skipped (fail-open): %s",
                        name, e)
