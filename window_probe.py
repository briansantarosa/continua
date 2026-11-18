"""window_probe.py — Phase A1 (memorybuild2 §5): the served-window drift
probe. At bridge startup, once per resident: fetch the SERVED window from
the chat server (llama.cpp /props; ollama /api/show), compare it against
every declared window (total_context_tokens, sitting_num_ctx), and WARN
on mismatch — a log line, never a block (fail-open). Turns v10's
"consistency bonus" into a checkable assertion so a future window change
cannot desync paths silently.

Comparison rules (the 96K flip is the reason for rule 2's direction):
  1. llama.cpp: served n_ctx MUST equal sitting_num_ctx (the ritual's
     window is really served). total_context_tokens may be BELOW served
     (the declared budget; headroom by design) but never ABOVE it.
  2. ollama: the model's context_length must fit BOTH declared values
     (the request would be truncated or refused otherwise; ollama serves
     per-request num_ctx, so the model metadata is the ceiling).

Kill switch: CONTINUA_WINDOW_PROBE=0. Any fetch/parse error = a logged
skip, never boot friction.
Run: python3 window_probe_test.py
"""
import json
import os
import urllib.request


def _fetch(url, payload=None, timeout=5.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _server_root(base_url):
    """The probe endpoints live at the SERVER ROOT, not under /v1 —
    http://host:8080/v1 → http://host:8080/props (404 otherwise,
    observed live 2026-09-30)."""
    b = str(base_url or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


def _served_llamacpp(base_url, fetch):
    d = fetch(_server_root(base_url) + "/props")
    n = int(((d.get("default_generation_settings") or {}).get("n_ctx")) or 0)
    return {"served_n_ctx": n, "total_slots": int(d.get("total_slots") or 0)}


def _served_ollama(base_url, model, fetch):
    d = fetch(_server_root(base_url) + "/api/show",
              payload={"model": model})
    info = d.get("model_info") or {}
    n = 0
    for k, v in info.items():
        if k.endswith("context_length"):
            n = max(n, int(v))
    return {"model_context_length": n}


def _served_vllm(base_url, fetch, model=""):
    d = fetch(_server_root(base_url) + "/v1/models")
    items = d.get("data") or []
    n = 0
    for it in items:
        if model and it.get("id") != model:
            continue
        n = max(n, int(it.get("max_model_len") or 0))
    return {"max_model_len": n}


def probe_instance(cfg, fetch=_fetch):
    """Returns {'ok': bool, 'warnings': [str], 'detail': str}. Pure with
    an injected fetch — tests mock the server."""
    llm = cfg.get("llm") or {}
    base = str(llm.get("chat_base_url") or llm.get("base_url") or "")
    total = int(llm.get("total_context_tokens") or 0)
    sitting = int(llm.get("sitting_num_ctx") or 0)
    model = str(llm.get("model") or "")
    warnings = []
    if not base or not total:
        return {"ok": True, "warnings": [],
                "detail": "undeclared window — nothing to compare"}
    try:
        if ":8090" in base or "vllm" in base:
            served = _served_vllm(base, fetch, model)
            n = served.get("max_model_len") or 0
            if sitting and n and n != sitting:
                warnings.append(
                    f"served max_model_len {n} != sitting_num_ctx {sitting}")
            if n and total > n:
                warnings.append(
                    f"total_context_tokens {total} > served max_model_len {n}")
            return {"ok": not warnings, "warnings": warnings,
                    "detail": f"vllm served max_model_len={n} "
                              f"declared total={total} sitting={sitting}"}
        if ":8080" in base or "llamacpp" in base:
            served = _served_llamacpp(base, fetch)
            n = served.get("served_n_ctx") or 0
            if sitting and n and n != sitting:
                warnings.append(
                    f"served n_ctx {n} != sitting_num_ctx {sitting}")
            if n and total > n:
                warnings.append(
                    f"total_context_tokens {total} > served n_ctx {n}")
            return {"ok": not warnings, "warnings": warnings,
                    "detail": f"llama.cpp served n_ctx={n} "
                              f"slots={served.get('total_slots')} "
                              f"declared total={total} sitting={sitting}"}
        try:
            served = _served_ollama(base, model, fetch)
        except Exception:
            # 2026-10-11 (residenta ring 8.1 cutover): a llama-server driven
            # through ollama_bridge.py serves no /api/show — that 404
            # used to fail the whole probe open. Try the llama.cpp
            # probe (the bridge proxies /props upstream, so the served
            # window is still the truth) before giving up.
            served = _served_llamacpp(base, fetch)
            n = served.get("served_n_ctx") or 0
            if sitting and n and n != sitting:
                warnings.append(
                    f"served n_ctx {n} != sitting_num_ctx {sitting}")
            if n and total > n:
                warnings.append(
                    f"total_context_tokens {total} > served n_ctx {n}")
            return {"ok": not warnings, "warnings": warnings,
                    "detail": f"llama.cpp (via bridge) served n_ctx={n} "
                              f"slots={served.get('total_slots')} "
                              f"declared total={total} sitting={sitting}"}
        n = served.get("model_context_length") or 0
        for label, decl in (("total_context_tokens", total),
                            ("sitting_num_ctx", sitting)):
            if n and decl > n:
                warnings.append(
                    f"{label} {decl} > model context_length {n}")
        return {"ok": not warnings, "warnings": warnings,
                "detail": f"ollama model context_length={n} "
                          f"declared total={total} sitting={sitting}"}
    except Exception as e:
        return {"ok": True, "warnings": [],
                "detail": f"probe skipped (fail-open): {e}"}


def enabled():
    return os.environ.get("CONTINUA_WINDOW_PROBE", "1") != "0"


def probe_all(configs, log=None):
    """One pass over every enabled resident at startup. Never raises."""
    import logging
    log = log or logging.getLogger("window_probe")
    if not enabled():
        log.info("[WindowProbe] disabled (CONTINUA_WINDOW_PROBE=0)")
        return
    for name, cfg in (configs or {}).items():
        r = probe_instance(cfg or {})
        log.log(logging.WARNING if r["warnings"] else logging.INFO,
                "[WindowProbe] %s: %s%s", name, r["detail"],
                (" | WARN: " + "; ".join(r["warnings"]))
                if r["warnings"] else "")