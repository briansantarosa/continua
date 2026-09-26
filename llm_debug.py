"""llm_debug.py — the LLM debug mirror (specs/2026-09-16-llm-debug-mirror.md,
the designer go 2026-09-16: root location, tools JSON included, ritual included via
this shared module, tail -F semantics).

One file per resident — residenta-debug.md / residentb-debug.md — holding the LAST
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
    "residenta": "residenta-debug.md",
    "residentb": "residentb-debug.md",
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
    content is shown exactly as sent."""
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
        parts.append(f"### [{role}] ({len(body)} chars)\n{body}\n")
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
        for k in ("rid", "user", "model", "path", "prompt_chars",
                  "messages", "tools_attached", "instance_channel"):
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
