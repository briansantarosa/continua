"""llm_debug.py — the LLM debug mirror (specs/2026-09-16-llm-debug-mirror.md,
the designer go 2026-09-16: root location, tools JSON included, ritual included via
this shared module, tail -F semantics).

One file per resident — <instance>debug.md — holding the LAST
conversational LLM request, atomically rewritten on EVERY call. Reader
guidance: `tail -F <file>` (os.replace swaps the inode; -F follows the
name, -f would keep following the dead one).

House contract: fail-open (a mirror problem never touches a turn), one
kill switch (CONTINUA_LLM_DEBUG=0), no new model calls, no fabrication —
the body is the verbatim request.
"""

import json
import os
import tempfile
from datetime import datetime

# persona-name mapping (house ruling): the files are named for the PERSON,
# like the desk ledgers. Unknown instances fall back to <instance>debug.md.
MIRROR_NAMES = {
}

# set by the nightly pulse loop (ritual.py) so its direct ask_her calls
# mirror to the right resident's file without threading an instance param
# through every call site. The bridge paths pass the instance explicitly.
_CURRENT = {"instance": None}


def set_current_instance(instance: str) -> None:
    _CURRENT["instance"] = instance


def mirror_path(instance: str, root: str = None) -> str:
    base = root or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(
        base, MIRROR_NAMES.get(instance, f"{instance}debug.md"))


def render_v1_messages(messages: list, tools=None) -> str:
    """Render the /v1 request body: one '### [role] (n chars)' block per
    message, verbatim content. Text parts of multimodal blocks render
    inline; image blocks are noted (not inlined). Tools JSON appended when
    attached (house ruling D2: include). Pure — no I/O, no fabrication:
    content is shown exactly as sent.

    [CONTINUA] memv13 §3.4 (2026-10-07): reasoning fields (the think
    window's render — reasoning_content/reasoning) render too, headed
    BEFORE the content, mirroring the served think-span position. OpenRouter's
    structured `reasoning_details` continuation blocks are also shown exactly
    as sent (they may be encrypted/opaque). The private `_think` and
    `_think_details` keys are never rendered — the facing adapter strips them
    before the request (memv13 §3.3); their absence here would not hide a
    leak, but the adapter is the only lawful path."""
    parts = []
    for m in (messages or []):
        role = m.get("role", "unknown")
        c = m.get("content")
        if isinstance(c, str):
            body = c or ""
        elif isinstance(c, list):
            chunks = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    chunks.append(blk.get("text") or "")
                elif isinstance(blk, dict):
                    chunks.append(f"[{blk.get('type', 'block')} block — "
                                  "not inlined]")
                else:
                    chunks.append(str(blk))
            body = "\n".join(chunks)
        else:
            body = "" if c is None else str(c)
        _think = ""
        for _k in ("reasoning_content", "reasoning"):
            _v = m.get(_k)
            if isinstance(_v, str) and _v:
                _think = _v
                break
        _details = m.get("reasoning_details")
        _detail_text = (json.dumps(_details, ensure_ascii=False, indent=2)
                        if _details else "")
        _suffix = ""
        if _details:
            _suffix = ("<<< reasoning_details — provider continuation state >>>\n"
                       + _detail_text + "\n<<< end reasoning_details >>>\n")
        if _think:
            parts.append(
                f"### [{role}] ({len(body)} chars + think {len(_think)} chars)\n"
                f"<<< think — rides as the assistant reasoning field >>>\n"
                f"{_think}\n<<< end think >>>\n{_suffix}{body}\n")
        else:
            parts.append(f"### [{role}] ({len(body)} chars)\n{_suffix}{body}\n")
    if tools:
        parts.append("## TOOLS ATTACHED (schema sent with this call)\n"
                     + json.dumps(tools, ensure_ascii=False, indent=2) + "\n")
    return "\n".join(parts)


def write_call(instance: str = None, header: dict = None, body: str = "",
               root: str = None) -> str:
    """Rewrite the resident's mirror with THIS call's request. Atomic
    (temp + os.replace). Kill switch CONTINUA_LLM_DEBUG=0 disables.
    Fail-open: returns the path written, or '' on any failure — never
    raises into a turn."""
    try:
        if os.environ.get("CONTINUA_LLM_DEBUG", "") == "0":
            return ""
        inst = instance or _CURRENT["instance"]
        if not inst:
            return ""
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        hdr = header or {}
        lines = [f"# LLM debug — {inst} (last call, rewritten every call)"]
        lines.append(f"- ts: {stamp}")
        # [CONTINUA] 2026-09-28 (helpnany.md Phase 5): the context-health
        # metrics — one glance at the header shows the repetition/curriculum
        # regressions that used to need a human reading 400+ lines.
        #   visible_reply_chars — her own delivered words in the window
        #   think_chars         — in-history think text (few-shot voice)
        #   empty_render_markers — non-delivery turns in the window
        #   failed_tool_rounds  — teaching-error rounds in the window
        #   paste_chars         — bytes of giant pastes in the window
        #   near_dup_score      — newest assistant turn vs any prior (0-1)
        #   recollection_chars  — [My recollections] block size, if present
        for k in ("rid", "user", "model", "path", "prompt_chars",
                  "messages", "tools_attached", "instance_channel",
                  "visible_reply_chars", "think_chars",
                  "empty_render_markers", "failed_tool_rounds",
                  "paste_chars", "near_dup_score", "recollection_chars"):
            if k in hdr and hdr[k] is not None:
                lines.append(f"- {k}: {hdr[k]}")
        text = "\n".join(lines) + "\n\n" + (body or "")
        path = mirror_path(inst, root)
        tmp = None
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".llmdebug_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return path
    except Exception:
        try:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        return ""
