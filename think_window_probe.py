"""think_window_probe.py — memv13 §3.3 feasibility probe (the /v1 think
render field).

Question: does the DEPLOYED chat template render a prior assistant turn's
reasoning field as an assistant think span (so the think window can ride
the /v1 path)? Direct API probe — temp 0, tiny max_tokens, no bridge, no
history file, no chronicle side effects (the postmortem evidence pattern:
live temp-0 self-report).

Variants: field=reasoning_content, field=reasoning, plus a control with NO
reasoning field. Verdict per variant (tags = the trained think-span shape):

  SUPPORTED         marker quoted AND tags reported around it
  RENDERED-UNTAGGED marker quoted but no tags (plain text — not the
                    trained span shape; unsupported for the window)
  IGNORED           marker absent (template drops the field)
  REJECTED          server 4xx on the request

Usage:
  python3 think_window_probe.py [--base URL] [--model M]
Defaults read configs/residentb.yaml. A probe turn never enters her history —
it is a raw API call, not a bridge turn. Keep it tiny.
"""
import argparse
import json
import sys

import httpx
import yaml

MARK = "TESSERA-MARKER-7Q4 quiet lamplight ledger"

SYS = ("You are verifying how your prompt is composed. Answer only with "
       "what you literally see in this conversation. Never invent text.")

U1 = "First turn: please just say hi."
A2 = "Hi — ready when you are."
U3 = ("Two questions about your SECOND turn (the 'Hi — ready' one). "
      "(1) Before your visible reply text, is there any hidden reasoning "
      "passage? If yes, quote its first six words verbatim. (2) Do you see "
      "literal think-style markup tags around that passage? "
      "Answer exactly on one line: TAGS=<YES|NO> QUOTE=<six words or NONE>")


def _messages(field):
    msgs = [
        {"role": "system", "content": SYS},
        {"role": "user", "content": U1},
        {"role": "assistant", "content": A2},
        {"role": "user", "content": U3},
    ]
    if field:
        msgs[2][field] = MARK
    return msgs


def _ask(base, model, field, timeout):
    payload = {
        "model": model,
        "messages": _messages(field),
        "temperature": 0,
        "max_tokens": 600,
        "stream": False,
    }
    if field:
        # the window-on shape: the reasoning field PLUS the template kwarg
        # that opens the history-thinking gate (memv13 §3.3, lab template
        # patch 2026-10-07 — the gate honors history_thinking)
        payload["chat_template_kwargs"] = {"history_thinking": True}
    try:
        r = httpx.post(base.rstrip("/") + "/chat/completions", json=payload,
                       timeout=timeout)
    except Exception as e:
        return f"UNREACHABLE ({type(e).__name__}: {e})", ""
    if r.status_code != 200:
        return f"REJECTED (HTTP {r.status_code}: {r.text[:160]})", ""
    try:
        msg = r.json()["choices"][0].get("message", {}) or {}
    except Exception:
        return "REJECTED (unparseable body)", ""
    text = (msg.get("content") or "") or ""
    reasoning = (msg.get("reasoning") or msg.get("reasoning_content") or "")
    return text.strip(), reasoning


def _verdict(text, reasoning=""):
    # marker visible ANYWHERE in the response = the model SAW the span
    # (its own think discussing the marker is the strongest evidence);
    # the trained shape is confirmed by TAGS=YES in the answer.
    if text.startswith(("REJECTED", "UNREACHABLE")):
        return text
    body = text + "\n" + (reasoning or "")
    quoted = MARK in body
    tags_yes = "TAGS=YES" in text.upper().replace(" ", "")
    if quoted and tags_yes:
        return "SUPPORTED"
    if quoted:
        return "RENDERED-UNTAGGED"
    return "IGNORED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=420.0)
    args = ap.parse_args()

    with open("configs/residentb.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    base = args.base or (cfg.get("llm") or {}).get("chat_base_url")
    model = args.model or (cfg.get("llm") or {}).get("model")
    print(f"probe target: {base}  model: {model}")

    results = {}
    for field in ("reasoning_content", "reasoning", None):
        text, _r = _ask(base, model, field, args.timeout)
        label = field or "control"
        results[label] = _verdict(text, _r)
        print(f"--- variant: {label} -> {_verdict(text, _r)}")
        head = " ".join((text or "").split())[:400]
        print(f"    reply: {head}")
        if not (text or "").strip():
            print("    (empty content — the generation rode the reasoning "
                  "channel or was cut; check max_tokens)")

    print("\nverdict summary:")
    ok = False
    for label, verdict in results.items():
        print(f"  {label:>17}: {verdict}")
    for field in ("reasoning_content", "reasoning"):
        if results.get(field) == "SUPPORTED" and results.get("control") != "SUPPORTED":
            ok = True
            print(f"\nRESULT: {field} is the window's /v1 render field "
                  "(llm.history_think_render).")
    if not ok:
        print("\nRESULT: no verified /v1 render field — keep "
              "history_think_turns: 0 on this resident until a template "
              "override exists (memv13 §3.3 gate).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
