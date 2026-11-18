"""history_curate.py — compose-time history curation (helpnany.md Phase 3,
2026-09-28). Selection, never rewriting: stored history is untouched; these
transforms shape only what the PROMPT shows, and every rule is flag-gated
and fail-open.

Three rules, each attacking a measured wound in the 09-27 dump:

  collapse_failed_tool_rounds — consecutive identical teaching-error
      rounds (persona-a fired the same empty-arg search 3× on 09-15) render as
      ONE summary line + the LAST round verbatim, so the (now-correct)
      grammar lesson survives without 3 full failure cycles as few-shot.

  stub_giant_pastes — a user turn over paste_stub_chars renders as
      head + honest char count + pointer-to-record + tail. The 09-22
      9,183-char plan was 68% of Alex's words and 25% of the whole
      prompt; the full text stays in the store, the distilled note, and
      deep_recall — the prompt points at them instead of carrying them.

  mark_unanswered — a user turn followed by another user turn with no
      assistant between renders a one-line gap marker (Phase 1b's
      render-side sibling for the archive's pre-existing silences).

Pure functions, no I/O, no store access. Run the tests:
  python3 history_curate_test.py
"""
import re

# [CONTINUA] the marker texts — same provenance voice as core.py's markers.
COLLAPSED_ROUNDS_MARKER = (
    "[system] {n} consecutive failed tool rounds collapsed here "
    "({fn} — {why}); the last round follows verbatim.]")
UNANSWERED_MARKER = "[no reply was composed that day — wrapper failure]"

_HEAD_TAIL = 240          # chars kept at each end of a stubbed paste
_HEAD_LINES = 2           # lines kept verbatim at the head


def collapse_failed_tool_rounds(messages, enabled=True):
    """Collapse runs of >=2 consecutive failed tool rounds into one summary
    + the last round verbatim. A 'failed tool round' is the teaching-error
    user turn the dispatcher emits: content starting '[system] [Tool call
    NOT executed'. Never touches non-teaching turns; never collapses across
    other roles; the most recent failure in the run stays verbatim.
    Returns (messages, collapsed_count)."""
    if not enabled or not messages:
        return messages, 0
    out = list(messages)
    i = 0
    collapsed = 0
    while i < len(out):
        m = out[i]
        if (m.get("role") == "user"
                and str(m.get("content", "")).startswith(
                    "[system] [Tool call NOT executed")):
            j = i
            while (j + 1 < len(out)
                   and out[j + 1].get("role") == "user"
                   and str(out[j + 1].get("content", "")).startswith(
                       "[system] [Tool call NOT executed")):
                j += 1
            run_len = j - i + 1
            if run_len >= 2:
                last = out[j]
                fn_m = re.search(r"NOT executed — (\w+)", str(last.get("content", "")))
                fn_name = fn_m.group(1) if fn_m else "tool call"
                why_m = re.search(r"no '([^']+)'", str(last.get("content", "")))
                why = f"missing '{why_m.group(1)}'" if why_m else "bad grammar"
                summary = {
                    "role": "user",
                    "content": COLLAPSED_ROUNDS_MARKER.format(
                        n=run_len, fn=fn_name, why=why),
                    "ts": last.get("ts"),
                }
                out[i:j + 1] = [summary, last]
                collapsed += run_len - 1
                i = i + 2
                continue
        i += 1
    return out, collapsed


def _paste_stub_content(content, cap):
    """Build the stub body: verbatim head lines, honest count, pointer,
    verbatim tail. The pointer names where the full text lives — the store,
    the distilled notes, deep_recall — never a void."""
    head = content[:_HEAD_TAIL]
    tail = content[-_HEAD_TAIL:]
    return (
        f"{head}"
        f"[… Alex pasted a document ({len(content)} chars) — full text in "
        f"the record; your notes carry the distilled version. "
        f"your shell (sed -n / tail -c) / deep_recall to see the original.]\n"
        f"{tail}"
    )


def stub_giant_pastes(messages, enabled=True, paste_stub_chars=2000):
    """Render user turns over paste_stub_chars as a stub. The stored
    history is untouched (this runs on prompt-facing copies). Conversation
    is never 2k+ chars; his pastes are. Returns (messages, stubbed_count)."""
    if not enabled or not messages:
        return messages, 0
    out = list(messages)
    stubbed = 0
    for i, m in enumerate(out):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str) or len(c) < paste_stub_chars:
            continue
        if c.startswith("[system]"):   # teaching lines are never pastes
            continue
        if "[Alex pasted a document" in c:   # already stubbed (idempotent)
            continue
        out[i] = dict(m)
        out[i]["content"] = _paste_stub_content(c, paste_stub_chars)
        stubbed += 1
    return out, stubbed


def mark_unanswered(messages):
    """Insert a one-line gap marker at the START of every consecutive-user
    RUN (an assistant turn absent = no reply was composed that day). One
    marker per run — three unanswered greetings render one marker, not
    three. Render-side only — the store is untouched. Returns
    (messages, marked_count)."""
    if not messages:
        return messages, 0
    out = []
    marked = 0
    for i, m in enumerate(messages):
        prev = messages[i - 1] if i else None
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if (m.get("role") == "user" and nxt is not None
                and nxt.get("role") == "user"
                and (prev is None or prev.get("role") != "user")
                and UNANSWERED_MARKER not in str(m.get("content", ""))):
            out.append({"role": "system", "content": UNANSWERED_MARKER})
            marked += 1
        out.append(m)
    return out, marked


def curate(messages, collapse=True, stub=True, paste_stub_chars=2000,
           unanswered=True):
    """The Phase-3 composition, in order: collapse → stub → unanswered.
    Returns (messages, stats-dict). Fail-open by construction (pure ops on
    copies of list slots, originals untouched)."""
    stats = {"collapsed_rounds": 0, "stubbed_pastes": 0, "unanswered": 0}
    if not messages:
        return messages, stats
    messages, stats["collapsed_rounds"] = collapse_failed_tool_rounds(
        messages, enabled=collapse)
    messages, stats["stubbed_pastes"] = stub_giant_pastes(
        messages, enabled=stub, paste_stub_chars=paste_stub_chars)
    if unanswered:
        messages, stats["unanswered"] = mark_unanswered(messages)
    return messages, stats
