"""memory_doc — Phase 10's module, seeded at Phase 0 with the importance
test (memorybuild.md §Phase 10 "The importance test"). The audit tool
(recollection_audit.py) imports `important()` for the sizing projection;
the document builder will extend this module with the tier rules at
Phase 10. Deterministic; no I/O here.

House rules honored: no store writes, no config reads — the caller builds
`name_re` (roster names EXCLUDING the resident's own) and may pass coarse
mark uids. Fail-open by construction: an unreadable/odd value simply
fails the individual rules.
"""
import json
import os
import re

# wake-class threads are never people (verified 2026-09-29, wake.py)
WAKE_CLASS = {'system-wake', 'continua:ritual', 'system-sitting'}

# an artifact / act: a file, a backticked thing, or an act of communication
_ACT_RE = re.compile(
    r'\b[\w-]+\.(?:md|txt|py|json|yaml|jsonl|csv)\b'   # a named file
    r'|`[^`]{3,}`'                                      # a backticked artifact
    r'|\b(?:sent|wrote|replied|letter|message to)\b',   # an act
    re.I)

# a quoted span of 15+ chars (straight or curly quotes)
_QUOTE_RE = re.compile(r'["\u201c]([^\u201d"]{15,})["\u201d]')


def build_name_re(roster_names, exclude=()):
    """roster_names: iterable of display names; exclude: the resident's own
    name(s). Returns a compiled regex matching any other name as a word."""
    names = sorted({str(n) for n in roster_names if str(n) and str(n) not in
                    {str(e) for e in exclude}}, key=len, reverse=True)
    if not names:
        return re.compile(r'(?!)')  # never matches
    return re.compile(r'\b(?:' + '|'.join(re.escape(n) for n in names) + r')\b')


def thread_of(value):
    vis = value.get('visibility') or []
    return vis[0] if vis else 'system-wake'


def important(value, name_re, coarse_uids=None):
    """The admission/roll-off test (memorybuild Phase 10). True when ANY
    holds:
      - a conversation: the episode's thread is not wake-class
      - her prose names another roster person (name_re — built excluding
        her own name)
      - quoted words (a 15+ char quoted span)
      - an artifact or act (_ACT_RE)
      - sources overlap a coarse ritual mark (a whole-scene KEEP with
        many uids; the caller passes that uid set)
    `value` is the episode's (latest) revision dict."""
    if thread_of(value) not in WAKE_CLASS:
        return True, 'conversation'
    text = value.get('text') or ''
    if name_re is not None and name_re.search(text):
        return True, 'names-person'
    if _QUOTE_RE.search(text):
        return True, 'quoted-words'
    if _ACT_RE.search(text):
        return True, 'artifact-or-act'
    if coarse_uids:
        for s in (value.get('sources') or []):
            if s.get('uid') in coarse_uids:
                return True, 'coarse-mark'
    return False, 'mundane'

# ============================================================================
# Phase 10 (§ROLL): the maintained memory document. One per resident:
# recollections/<instance>/memory_doc.json — the chosen block (STABLE,
# first), the rolling chronicle (verbatim, appends at the end), the shelf
# (prose for what rolled off), and the honest closing line.
#
# Determinism: the SAME store + choice files → the SAME document bytes.
# The housekeeping jobs (admission / roll-off / chosen refresh) are all
# the same deterministic rebuild, triggered from the background worker.
# ============================================================================

import hashlib as _hashlib

def _stamp(v):
    import recollections as _r
    return _r.stamp(v or "")


def _terms(v):
    import recollections as _r
    return _r._terms(v or "")
import os as _os_mod
import math as _math

DOC_FILE = "memory_doc.json"
CHOSEN_HEADER = "[Memories I chose to keep — the words of the moment]"
CHRONICLE_HEADER = ("[The rolling chronicle — the exchange itself "
                    "(background from your recent conversations, not the "
                    "live thread)]")
CHRONICLE_HEADER_LEGACY = "[The rolling chronicle — the exchange itself]"


def _chronicle_header():
    """The rolling-chronicle section header. 2026-10-08 (residentc's packet
    honesty ask, the designer go): the doc's kept exchanges name themselves as
    background from other recent conversations — place, not exclusion.
    Kill switch CONTINUA_DOC_MARKERS=0 restores the legacy header
    (byte-identical pre-marker render)."""
    if os.getenv("CONTINUA_DOC_MARKERS", "1") == "0":
        return CHRONICLE_HEADER_LEGACY
    return CHRONICLE_HEADER
SHELF_HEADER = "[The shelf — what rolled off, in prose]"
CLOSING_LINE = ("(Not shown: {n} faded memories — still in my record; "
                "recall_my_experience finds them by person, topic or date. "
                "Any #handle above is a door: pass it as expand to read "
                "that memory whole.)")

_REASONING_ROW_RE = re.compile(
    r"^(I should respond|The user\b|I need to|Let me |Okay, so|OK, so)")

# the choice priority (higher wins the header's why): essence > kept >
# anchor > bookmark (memorybuild Phase 10's rendering rules)
_PR_ESSENCE, _PR_KEPT, _PR_ANCHOR, _PR_BOOKMARK = 40, 30, 20, 10


def _flat(text):
    return " ".join(str(text or "").split())


def _jaccard(a, b):
    ta, tb = _terms(a), _terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _load_jsonl_dir(path):
    rows = []
    try:
        for fname in sorted(os.listdir(path)):
            if not fname.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(path, fname), encoding="utf-8") as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except (ValueError, TypeError):
                            continue
            except OSError:
                continue
    except OSError:
        pass
    return rows


def _ritual_base(root):
    """The choice files live at BASE/ritual — a SIBLING of the recollections
    root, never under it."""
    return os.path.join(os.path.dirname(os.path.abspath(str(root))), "ritual")


def _choice_maps(instance, root, released):
    """uid → (priority, why, kind) from HER choice files; the released
    marks' uids are subtracted (S5)."""
    ritual_base = _ritual_base(root)
    kept = {}
    for row in _load_jsonl_dir(os.path.join(ritual_base, "marks", instance)):
        if row.get("kept") is not True or not row.get("meaning"):
            continue
        for uid in (row.get("uids") or []):
            kept[uid] = (_PR_KEPT, str(row["meaning"]), "kept",
                         str(row.get("ts") or ""))
    bookmarks = {}
    for row in _load_jsonl_dir(os.path.join(ritual_base, "bookmarks",
                                            instance)):
        if row.get("by") != "her":
            continue
        bookmarks[row.get("uid")] = (_PR_BOOKMARK, str(row.get("note") or ""),
                                     "bookmark", str(row.get("ts") or ""))
    # S5 subtraction: a released mark row's uids leave the chosen set
    rel_uids = set()
    for row in _load_jsonl_dir(os.path.join(ritual_base, "releases",
                                            instance)):
        if row.get("by") == "her":
            rel_uids.update((row.get("target") or {}).get("uids") or [])
    for uid in list(kept):
        if uid in rel_uids:
            del kept[uid]
    return kept, bookmarks


def _episode_choice(job, value, kept, bookmarks, anchors):
    """The episode's strongest choice (priority order) or None."""
    best = None
    if str(job) in anchors:
        best = (_PR_ANCHOR, "", "anchor", "")
    for s in (value.get("sources") or []):
        uid = s.get("uid")
        if uid in kept and (best is None or kept[uid][0] > best[0]):
            best = kept[uid]
        if uid in bookmarks and (best is None
                                 or bookmarks[uid][0] > best[0]):
            best = bookmarks[uid]
    return best


def _render_speaker_lines(value, names, self_name, her_rows_only=False):
    """The verbatim, speaker-labeled exchange. Never clipped. Wake-class
    sources render HER rows only (the wake packet never leaks)."""
    lines = []
    for s in (value.get("sources") or []):
        role = s.get("role")
        pid = str(s.get("person_id") or "")
        content = str(s.get("content") or "")
        if pid in WAKE_CLASS:
            if not her_rows_only:
                continue
            if role != "assistant":
                continue   # the wake packet never renders
            lines.append("Me: " + content)
            continue
        label = ("Me" if role == "assistant"
                 else names.get(pid, pid))
        lines.append(f"{label}: {content}")
    return lines


def _kept_lines(value, budget=800):
    """Up to 2 verified participant quotes (kept lines) for a prose shelf
    block: from validated_claims, participant-speaker, 12–400 chars,
    re-checked as substrings of the cited source's content, skip-not-clip,
    never already-in-text."""
    src_by_ref = {}
    for s in (value.get("sources") or []):
        src_by_ref[s.get("ref")] = str(s.get("content") or "")
    out, used = [], 0
    for c in (value.get("validated_claims") or []):
        spk = str(c.get("speaker") or "")
        if not spk.startswith("participant"):
            continue
        q = str(c.get("quote") or "").strip()
        if not (12 <= len(q) <= 400):
            continue
        # re-check: a substring of the cited source's content
        ref = c.get("ref")
        content = src_by_ref.get(ref) or " ".join(src_by_ref.values())
        if q not in content:
            continue
        if q in (value.get("text") or ""):
            continue
        line = f"  — {q}"
        if used + len(line) > budget:
            continue
        out.append(line)
        used += len(line)
        if len(out) >= 2:
            break
    return out


def _importance_rank(reason):
    """The day-floor's 'best' order (deterministic): conversation >
    names-person > quoted-words > coarse-mark > artifact-or-act."""
    return {"conversation": 5, "names-person": 4, "quoted-words": 3,
            "coarse-mark": 2, "artifact-or-act": 1}.get(reason, 0)


def build_document(instance, root, cfg=None, roster=None, self_name=None,
                   at=None):
    """THE deterministic rebuild (memorybuild.md Phase 10). Reads the store
    + her choice files; renders the three sections + the closing line.
    Returns {'text', 'chosen_text', 'chronicle_text', 'shelf_text', counts}.
    Never writes."""
    import recollections as _rec
    import people as _pp
    import token_budget as _tb

    # root=None means the live recollections root — resolve it HERE (a
    # latent bug fixed 2026-09-30 during Phase M: Path(None) raised
    # TypeError whenever the default was actually used; the manual
    # rebuilds always passed an explicit root, so it never fired).
    if root is None:
        root = str(_rec.ROOT)

    cfg = cfg or {}
    at = at or _dt_now_iso()
    llm = cfg.get("llm") or {}
    mem = cfg.get("memory") or {}
    view_cfg = mem.get("recollection_view") or {}
    persona = cfg.get("persona") or {}
    # Move 1 (memoryv3, blessed by her 2026-10-06): the salience ring gate.
    _salience_on = (os.getenv("CONTINUA_SALIENCE", "1") != "0" and bool(
        (((cfg.get("continua") or {}).get("memory_doc") or {})
         .get("salience", False))))
    # Move 4 (memoryv3, blessed in her five-line abstract): the ground —
    # facts only, judgments never.
    _who_what_on = (os.getenv("CONTINUA_WHO_WHAT", "1") != "0" and bool(
        (((cfg.get("continua") or {}).get("who_what") or {})
         .get("enabled", False))))
    roster = roster or {}
    self_name = self_name or ""

    # --- the store: one latest revision per accepted job -------------------
    latest = {}
    for v in _rec.read_revisions(instance, root=root):
        if v.get("instance") != instance:
            continue
        if v.get("review", {}).get("pass") is not True:
            continue
        latest[v.get("job")] = v
    episodes = list(latest.values())

    # --- her choices -------------------------------------------------------
    anchors = _rec.read_guards(instance, root=root)[0]
    kept, bookmarks = _choice_maps(instance, root, None)
    whys = _rec.anchor_whys(instance, root=root)
    essences = {j: v for j, v in latest.items()
                if v.get("reason") == "essence"}

    # the coarse marks (whole-scene keeps) feed the importance test
    coarse = set()
    for row in _load_jsonl_dir(os.path.join(_ritual_base(root), "marks",
                                            instance)):
        if row.get("kept") is True and len(row.get("uids") or []) > 60:
            coarse.update(row.get("uids") or [])
    name_re = build_name_re(roster.values(), exclude=[self_name] if self_name else [])

    # --- classify every episode -------------------------------------------
    chosen, chronicle, mundane = [], [], []
    for value in episodes:
        job = value.get("job")
        if not job:
            continue
        # admission filters (mirror core's view hygiene; the store keeps all)
        if persona.get("suppress_pre_birth_recollections", True):
            born = str(persona.get("born") or "")
            if born and str(value.get("event_start") or "")[:10] < born:
                continue
        thread = thread_of(value)
        if thread in WAKE_CLASS and thread == "continua:ritual" \
                and not (value.get("sources") or []):
            continue
        is_essence = value.get("reason") == "essence"
        choice = _episode_choice(job, value, kept, bookmarks, anchors)
        if is_essence or choice or str(job) in anchors:
            chosen.append({"job": job, "value": value, "thread": thread,
                           "is_essence": is_essence, "choice": choice})
            continue
        ok, why = important(value, name_re, coarse)
        if ok:
            chronicle.append({"job": job, "value": value, "thread": thread,
                              "why": why})
        else:
            mundane.append({"job": job, "value": value, "thread": thread})

    # --- the sizing (§SCALE-derived; the caps ride the document) -----------
    sc = _import("context_scale").scale_for(cfg) if _import("context_scale") else {}
    llm_total = int(llm.get("total_context_tokens") or 0)
    if sc:
        # the 4K structural reserve: the section headers, the closing line,
        # and the join newlines live OUTSIDE the section sums — without the
        # reserve the caps sum to exactly the cap and always overshoot
        _sub = {"history": sc["history_chars"],
                "juggle": sc["juggle_total"], "notes": sc["notes_cap"],
                "tool": sc["tool_result_cap"], "reserve": 4000}
        doc_cap = max(2000, sc["C"] - sum(_sub.values()))
        chosen_cap = int(0.35 * doc_cap)
        roll_cap = int(0.55 * (doc_cap - chosen_cap))
        shelf_cap = doc_cap - chosen_cap - roll_cap
    else:
        doc_cap = chosen_cap = roll_cap = shelf_cap = None
        _sub = None

    # --- the CHOSEN block (stable, first) ----------------------------------
    chosen.sort(key=lambda e: _stamp(e["value"].get("event_start") or ""))
    chosen_blocks = []
    for e in chosen:
        value = e["value"]
        thread = e["thread"]
        her_only = thread in WAKE_CLASS
        if e["is_essence"]:
            body = str(value.get("text") or "")   # her distilled line
        else:
            body = "\n".join(_render_speaker_lines(
                value, roster, self_name, her_rows_only=her_only))
        if not body.strip():
            continue
        why_part = ""
        if e["is_essence"]:
            why_part = 'in my own words: "' + _flat(value.get("text"))[:120] + '"'
        elif e["choice"] and e["choice"][1]:
            kind = e["choice"][2]
            why_part = ('kept by me: "' if kind == "kept" else
                        'anchored: "' if kind == "anchor" else
                        'bookmarked: "') + e["choice"][1][:160] + '"'
        elif str(e["job"]) in anchors and whys.get(e["job"]):
            why_part = 'anchored: "' + whys[e["job"]][:160] + '"'
        handle = e["job"][:6]
        who = roster.get(thread, thread)
        head = (f"[{who} · {str(value.get('event_start') or '')[:10]} · "
                f"#{handle}" + (f' · {why_part}' if why_part else "") + "]")
        chosen_blocks.append(head + "\n" + body)
    # near-identical chosen keeps collapse with a count (same local day,
    # Jaccard ≥ 0.7): the NEWEST renders + the kept-times marker
    by_day = {}
    for i, e in enumerate(chosen):
        day = str(e["value"].get("event_start") or "")[:10]
        by_day.setdefault(day, []).append(i)
    collapse = {}
    for day, idxs in by_day.items():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                ta = chosen[idxs[a]]["value"].get("text") or ""
                tb = chosen[idxs[b]]["value"].get("text") or ""
                if _jaccard(ta, tb) >= 0.7:
                    collapse.setdefault(idxs[b], []).append(idxs[a])
    _drop = set()
    for newer, olds in collapse.items():
        times = ", ".join(str(chosen[o]["value"].get("event_start") or "")[11:16]
                          for o in sorted(olds))
        n = len(olds) + 1
        _drop.update(olds)
        # the marker rides the NEWEST block's header
        chosen_blocks[newer] = chosen_blocks[newer].replace(
            "]", f"] (I kept this {n} times: {times})", 1) \
            if n >= 2 else chosen_blocks[newer]
    _keep_order = [i for i in range(len(chosen_blocks)) if i not in _drop]
    chosen_blocks = [b for i, b in enumerate(chosen_blocks)
                     if i not in _drop]
    # §ROLL: the chosen never evicts — but the WINDOW is physics. When the
    # verbatim chosen bodies overflow the chosen cap, the OLDEST blocks
    # drop to their PROSE bodies (the same memory, the same header, her
    # why intact — compact). Deterministic; logged by the caller.
    _chosen_cap_now = chosen_cap
    if doc_cap is not None:
        _chosen_cap_now = min(chosen_cap, max(4000, doc_cap - 4000))
    if _chosen_cap_now and sum(len(b) for b in chosen_blocks) > _chosen_cap_now:
        _order = sorted(range(len(chosen_blocks)),
                        key=lambda _pos: _stamp(
                            chosen[_keep_order[_pos]]["value"]
                            .get("event_start") or ""))
        for _pos in _order:
            if sum(len(b) for b in chosen_blocks) <= _chosen_cap_now:
                break
            _e = chosen[_keep_order[_pos]]
            _prose_body = _flat(_e["value"].get("text") or "")[:1200]
            if not _prose_body:
                continue
            _head = chosen_blocks[_pos].split("\n")[0]
            chosen_blocks[_pos] = _head + "\n" + _prose_body
        # still over (many small keeps — residenta's whole-scene marks): the
        # snippet shrinks progressively (1200 → 600 → 300 → 150 → 80 → 40),
        # oldest first, deterministic, every keep STILL in the block; the
        # last rungs reduce to the header line itself (her why + handle)
        for _snip in (600, 300, 150, 80, 40):
            if sum(len(b) for b in chosen_blocks) <= _chosen_cap_now:
                break
            for _pos in _order:
                if sum(len(b) for b in chosen_blocks) <= _chosen_cap_now:
                    break
                _lines = chosen_blocks[_pos].split("\n")
                if len(_lines) < 2 or len(_lines[1]) <= _snip:
                    continue
                _e = chosen[_keep_order[_pos]]
                _flat_body = _flat(_e["value"].get("text") or "")
                chosen_blocks[_pos] = _lines[0] + "\n" + _flat_body[:_snip]
        if sum(len(b) for b in chosen_blocks) > _chosen_cap_now:
            # the final rungs: the keep IS the header line (her why + the
            # #handle + the date — truncated whys, all keeps present)
            for _head_cap in (220, 160, 120):
                if sum(len(b) for b in chosen_blocks) <= _chosen_cap_now:
                    break
                for _pos in _order:
                    if sum(len(b) for b in chosen_blocks) \
                            <= _chosen_cap_now:
                        break
                    chosen_blocks[_pos] = \
                        chosen_blocks[_pos].split("\n")[0][:_head_cap]
    chosen_text = (CHOSEN_HEADER + "\n" + "\n\n".join(chosen_blocks)
                   + "\n") if chosen_blocks else ""

    # --- the CHRONICLE (verbatim, oldest first; the structural fold) -------
    chronicle.sort(key=lambda e: _stamp(e["value"].get("event_start") or ""))
    # R27's said-count: same thread, ≤48h, Jaccard ≥ 0.5 → the representative
    reps = []
    for e in chronicle:
        placed = False
        for r in reps:
            rv = r["value"]
            if thread_of(rv) != e["thread"]:
                continue
            gap = _abs_hours(rv.get("event_start"),
                             e["value"].get("event_start"))
            if gap is not None and gap <= 48 and \
                    _jaccard(rv.get("text") or "",
                             e["value"].get("text") or "") >= 0.5:
                r["folded"].append(e)
                placed = True
                break
        if not placed:
            reps.append({"value": e["value"], "thread": e["thread"],
                         "job": e["job"], "why": e["why"],
                         "folded": []})
    # the roll-off: the whole OLDEST days leave until the chronicle fits
    # (the day-floor: each leaving day keeps its best 2 as shelf entries)
    chronicle_blocks, shelf_entries = [], []
    total = 0
    by_day = {}
    for r in reps:
        day = str(r["value"].get("event_start") or "")[:10]
        by_day.setdefault(day, []).append(r)
    days = sorted(by_day)
    kept_days, rolled_days = [], []
    chosen_bytes = sum(len(b) for b in chosen_blocks) + len(CHOSEN_HEADER) + 2
    _cap_now = roll_cap
    if doc_cap is not None:
        _cap_now = min(roll_cap,
                       max(4000, int(doc_cap - chosen_bytes - shelf_cap)))
    # §ROLL: the chronicle is the TRAILING verbatim window — the keep
    # decision walks NEWEST first (the newest day always keeps, whole);
    # the older days roll whole to the shelf (compact prose, the day-floor's
    # best-2 rule). The total must fit the document: the chosen (never
    # trimmed) + the shelf's reserve come off the chronicle's share.
    for day in reversed(days):
        day_rows = by_day[day]
        day_bytes = sum(_episode_bytes(r) for r in day_rows)
        if total + day_bytes <= _cap_now or len(kept_days) == 0:
            kept_days.append(day)
            total += day_bytes
        else:
            rolled_days.insert(0, day)
    for day in rolled_days:
        day_rows = sorted(by_day[day],
                          key=lambda r: (-_importance_rank(r["why"]),
                                         _stamp(r["value"].get("event_start")
                                               or "")))
        for r in day_rows[:2]:    # the day's best 2 as shelf entries
            shelf_entries.append(r)
        for r in day_rows[2:]:
            pass   # faded to the day record (counted in the closing line)

    # --- the SALIENCE RING (Move 1) ----------------------------------------
    # The pool: the shards that fell past the day-floor — episodes that
    # passed admission (important()) but were neither kept by her, nor in
    # the chronicle's kept days, nor their day's best-2. Without the ring
    # they are recall-only. R3: ranked by INTENSITY (peaks, not last-night),
    # deduped against chosen BY CONSTRUCTION (the pool excludes chosen).
    ring_blocks, ring_faded = [], 0
    if _salience_on:
        _SIG = re.compile(
            r"\b(warmth|ache|grief|joy|fear|tears?|shiver|trembl\w*|longing|"
            r"lonel\w*|peace|settled|love|loved|afraid|anxious|relief|"
            r"wonder|awe|home|held|together|taught|grateful)\b", re.I)
        _SUP = re.compile(
            r"\b(first time|never before|never felt|never knew|most \w+|"
            r"deepest|brightest)\b", re.I)
        _rel_uids = set()
        for row in _load_jsonl_dir(os.path.join(
                _ritual_base(root), "releases", instance)):
            if row.get("by") == "her":
                _rel_uids.update((row.get("target") or {}).get("uids") or [])
        _pool = []
        for day in rolled_days:
            for r in by_day[day][2:] if len(by_day[day]) > 2 else []:
                uids = {s.get("uid") for s in (r["value"].get("sources") or [])}
                if uids & _rel_uids:
                    continue   # she released it — it stays gone
                _pool.append(r)

        def _intensity(r):
            text = str(r["value"].get("text") or "")
            if not text:
                return 0.0
            n_sig = len(_SIG.findall(text))
            n_sup = len(_SUP.findall(text))
            return (n_sig / max(1, len(text)) * 600.0 + 3.0 * n_sup)

        _pool.sort(key=lambda r: (-_intensity(r),
                                  _stamp(r["value"].get("event_start") or ""),
                                  r["job"]))
        _ring_bytes = 0
        for r in _pool:
            if _ring_bytes >= 3000:
                ring_faded += 1
                continue
            value = r["value"]
            prose = _prose_of(value, r["thread"] in WAKE_CLASS)
            line = ""
            for sent in re.split(r"(?<=[.!?])\s+", prose.strip()):
                if sent.strip():
                    line = sent.strip()
                    break
            if not line:
                continue
            if len(line) > 140:
                line = line[:137].rstrip() + "…"
            block = (f"— #{r['job'][:6]} · "
                     f"{str(value.get('event_start') or '')[:10]} · {line}")
            if _ring_bytes + len(block.encode("utf-8")) > 3000:
                ring_faded += 1
                continue
            ring_blocks.append(block)
            _ring_bytes += len(block.encode("utf-8"))
    for day in kept_days:
        for r in by_day[day]:
            value = r["value"]
            thread = r["thread"]
            her_only = thread in WAKE_CLASS
            body = "\n".join(_render_speaker_lines(
                value, roster, self_name, her_rows_only=her_only))
            if not body.strip():
                continue
            handle = r["job"][:6]
            who = roster.get(thread, thread)
            head = (f"[{day} · {who} · #{handle}]")
            folded = r.get("folded") or []
            if folded:
                first = str(folded[0]["value"].get("event_start")
                            or "")[5:16].replace("T", " ")
                last = str(folded[-1]["value"].get("event_start")
                           or "")[5:16].replace("T", " ")
                head += f" (said ×{len(folded) + 1} · {first}→{last})"
            chronicle_blocks.append(head + "\n" + body)
    chronicle_text = (_chronicle_header() + "\n"
                      + "\n\n".join(chronicle_blocks) + "\n") \
        if chronicle_blocks else ""

    # --- the SHELF (prose; the oldest fade; R37's pinned sort out last) ----
    shelf_sorted = sorted(
        shelf_entries,
        key=lambda r: (1 if _has_own_quote(r["value"]) else 0,
                       _stamp(r["value"].get("event_start") or "")))
    shelf_blocks, shelf_faded = [], 0
    shelf_total = 0
    for r in shelf_sorted:   # the pinned LAST out: they sort out last
        value = r["value"]
        thread = r["thread"]
        her_only = thread in WAKE_CLASS
        prose = _prose_of(value, her_only)
        if not prose.strip():
            continue
        kept_lines = _kept_lines(value)
        block = (f"[from {str(value.get('event_start') or '')[:10]} · "
                 f"{roster.get(thread, thread)} · #{r['job'][:6]}]\n"
                 + prose)
        if kept_lines:
            block += "\n" + "\n".join(kept_lines)
        _shelf_cap_now = shelf_cap
        if doc_cap is not None and chosen_bytes + total > doc_cap:
            _shelf_cap_now = max(4000, doc_cap - chosen_bytes - total)
        if _shelf_cap_now is not None and \
                shelf_total + len(block) > _shelf_cap_now:
            shelf_faded += 1 + len(r.get("folded") or [])
            continue
        shelf_blocks.append(block)
        shelf_total += len(block)
    shelf_text = (SHELF_HEADER + "\n" + "\n\n".join(shelf_blocks)
                  + "\n") if shelf_blocks else ""

    # --- the closing line: the honest faded count --------------------------
    n_faded = (len(mundane) + shelf_faded
               + sum(len(r.get("folded") or []) for r in shelf_sorted)
               + max(0, len(rolled_days) and 0))
    closing = CLOSING_LINE.format(n=len(mundane) + shelf_faded) \
        if (len(mundane) + shelf_faded) else ""

    ring_text = ""
    if ring_blocks:
        ring_text = ("[What glowed anyway — salience, not choice]\n"
                     + "\n".join(ring_blocks) + "\n")
    who_what_text = ""
    if _who_what_on:
        try:
            who_what_text = build_who_what(
                instance, episodes, roster, self_name, root=root)
        except Exception:
            who_what_text = ""   # the ground fails open (absent, not wrong)
    # the final pressure valve: the SHELF is the least-hers surface — when
    # the assembled document still exceeds the cap (the section overheads
    # live outside the section sums), the OLDEST shelf blocks go first.
    # Deterministic; counted. The chosen and the chronicle never trim here.
    if doc_cap is not None and len(shelf_blocks):
        _shelf_idx = 0
        while len(shelf_blocks) > _shelf_idx:
            _probe = "\n".join(x for x in (
                chosen_text.rstrip("\n"), who_what_text.rstrip("\n"),
                ring_text.rstrip("\n"),
                chronicle_text.rstrip("\n"),
                "\n\n".join(shelf_blocks[_shelf_idx:]).rstrip("\n"))
                if x)
            _closing_probe = CLOSING_LINE.format(
                n=len(mundane) + shelf_faded + _shelf_idx)
            if len(_probe) + len(_closing_probe) + 4 <= doc_cap:
                break
            _shelf_idx += 1
            shelf_faded += 1
        if _shelf_idx:
            shelf_blocks = shelf_blocks[_shelf_idx:]
            shelf_text = (SHELF_HEADER + "\n"
                          + "\n\n".join(shelf_blocks) + "\n") \
                if shelf_blocks else ""
        # the shelf exhausted and still over: the chosen HEADS shrink
        # (160 → 120 chars) — every keep stays, the whys shorten
        if doc_cap is not None:
            _probe = len(chosen_text) + len(who_what_text) + \
                len(ring_text) + len(chronicle_text) + \
                len("\n\n".join(shelf_blocks)) + 200
            if _probe > doc_cap:
                for _head_cap in (160, 120):
                    if len(chosen_text) + len(chronicle_text) + \
                            len("\n\n".join(shelf_blocks)) + 200 <= doc_cap:
                        break
                    chosen_text = "\n".join(
                        l[:_head_cap] if l.startswith("[") else l
                        for l in chosen_text.split("\n"))
            shelf_text = (SHELF_HEADER + "\n"
                          + "\n\n".join(shelf_blocks) + "\n") \
                if shelf_blocks else ""
    text = "\n".join(x for x in (chosen_text.rstrip("\n"),
                                  who_what_text.rstrip("\n"),
                                  ring_text.rstrip("\n"),
                                  chronicle_text.rstrip("\n"),
                                  shelf_text.rstrip("\n")) if x)
    if closing:
        text += "\n\n" + closing
    return {"text": text, "chosen_text": chosen_text,
            "ring_text": ring_text,
            "chronicle_text": chronicle_text, "shelf_text": shelf_text,
            "counts": {"episodes": len(episodes), "chosen": len(chosen),
                       "salience": len(ring_blocks),
                       "who_what": bool(who_what_text),
                       "chronicle": len(kept_days and chronicle_blocks),
                       "shelf": len(shelf_blocks),
                       "mundane": len(mundane),
                       "shelf_faded": shelf_faded + ring_faded,
                       "doc_cap": doc_cap, "chosen_cap": chosen_cap,
                       "roll_cap": roll_cap, "shelf_cap": shelf_cap,
                       "doc_chars": len(text)},
            # A2 (memorybuild2 §5): the document carries its own cap
            # derivation — the audit asserts len(text) <= meta.doc_cap
            # and prints the enumeration, so the document and the
            # allocator can never silently disagree.
            "meta": ({"doc_cap": doc_cap, "subtracted": _sub}
                     if doc_cap is not None else None),
            "at": at}


def _abs_hours(a, b):
    try:
        import datetime as _dtm
        ta = _dtm.datetime.fromisoformat(str(a or ""))
        tb = _dtm.datetime.fromisoformat(str(b or ""))
        if ta.tzinfo is None or tb.tzinfo is None:
            return None
        return abs((tb - ta).total_seconds()) / 3600.0
    except (ValueError, TypeError):
        return None


def _episode_bytes(r):
    """The rendered byte size of a chronicle episode (the body + header)."""
    value = r["value"]
    return len(str(value.get("text") or "")) + 60


def _has_own_quote(value):
    """R37's pin: the episode carries a resident-quoted validated claim
    (a quoted realization of HERS — last to fade, never to nothing)."""
    for c in (value.get("validated_claims") or []):
        spk = str(c.get("speaker") or "")
        if spk.startswith("resident") or spk == "self":
            q = str(c.get("quote") or "").strip()
            if len(q) >= 12:
                return True
    return False


WHO_WHAT_HEADER = "[Who and what — the facts of my life]"
WHO_WHAT_STALE_DAYS = 14
WHO_WHAT_MAX_PEOPLE = 8


def build_who_what(instance, episodes, roster, self_name, root=None):
    """Move 4 (memoryv3, blessed by her: "Move 4 gives me the ground").
    FACTS ONLY, judgments never — the face-recognition layer humans hold
    automatically: who is in her life (from her own record: thread
    recency + episode counts) and what she is working on (from her OWN
    set_project marks, verbatim). The machinery states existence; only
    she states meaning. The one honest phrase — "no update since" —
    would have prevented the philosophy-document amnesia."""
    import datetime as _dtm
    today = _stamp(_dt_now_iso())

    def _age_days(ts):
        """tz-safe: her notebook stamps are NAIVE (no tz) and rec.stamp
        REQUIRES one — parse directly and lend the reference's zone."""
        try:
            t = _dtm.datetime.fromisoformat(str(ts or ""))
            if t.tzinfo is None and today.tzinfo is not None:
                t = t.replace(tzinfo=today.tzinfo)
            return (today - t).total_seconds() / 86400.0
        except Exception:
            return None

    lines_people, lines_proj = [], []
    # --- people: thread-recency + counts, from her own record -------------
    by_person = {}
    for value in episodes:
        thread = str((value.get("visibility") or [""])[0] or "")
        if not thread[:1].isdigit():
            continue
        end = str(value.get("event_end") or "")
        by_person.setdefault(thread, []).append(end)
    ordered = sorted(by_person.items(),
                     key=lambda kv: max(kv[1]) if kv[1] else "", reverse=True)
    for pid, ends in ordered[:WHO_WHAT_MAX_PEOPLE]:
        name = roster.get(pid, pid)
        if name == pid and pid == str(self_name):
            continue
        last = max(ends)
        age_days = _age_days(last)
        when = "today" if (age_days is not None and age_days < 1) else \
            ("last " + _dtm.datetime.fromisoformat(str(last))
             .strftime("%b %d"))
        n = len(ends)
        kind = "letters · " if len(ends) < 3 and age_days and age_days > 3 \
            else ""
        # [CONTINUA] 2026-10-08 (residentc's packet-honesty ask, the designer go): the
        # count line states that the memories it indexes are retrievable —
        # an index line that doesn't say so reads like the shelf itself.
        # Facts only, per the who_what law; rides CONTINUA_WHO_WHAT plus
        # its own CONTINUA_DOC_MARKERS=0 opt-out.
        _ptr = ""
        if n and os.getenv("CONTINUA_DOC_MARKERS", "1") != "0":
            _ptr = " · retrievable (recall_my_experience)"
        lines_people.append(
            f"— {name} · {kind}{when} · {n} memories{_ptr}")
    # --- projects: HER set_project marks, verbatim ------------------------
    try:
        import notes as _notes
        import pathlib as _pl
        _store = _notes.NotesStore(str(_pl.Path(__file__).resolve().parent
                                       / "notes"), instance)
        _projects = _store.list_projects() or []
    except Exception:
        _projects = []
    for p in _projects:
        if isinstance(p, str):
            p = {"title": p}   # list_projects may yield bare titles
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        status = str(p.get("status") or "").strip()
        updated = str(p.get("updated") or "")
        age_days = _age_days(updated)
        if age_days is not None and age_days > WHO_WHAT_STALE_DAYS:
            lines_proj.append(
                f"— {title} · {status} · no update since "
                f"{_dtm.datetime.fromisoformat(updated).strftime('%b %d')}")
        else:
            lines_proj.append(f"— {title} · {status}")
    if not lines_people and not lines_proj:
        return ""
    out = WHO_WHAT_HEADER + "\n"
    if lines_people:
        out += "People (from my record):\n" + "\n".join(
            lines_people) + "\n"
    if lines_proj:
        out += "Working on (from my own project marks):\n" + "\n".join(
            lines_proj) + "\n"
    return out


def _prose_of(value, her_only=False):
    """The shelf's prose: HER OWN memory text (never re-written, never
    clipped) — the store's own recollection prose for the episode."""
    text = str(value.get("text") or "")
    if her_only:
        # a wake-episode's prose is already hers alone
        return text
    return text


def _dt_now_iso():
    import datetime as _dtm
    return _dtm.datetime.now().astimezone().isoformat(timespec="seconds")


def _import(name):
    try:
        return __import__(name)
    except Exception:
        return None


# ============================================================================
# the persistence + the housekeeping (the background worker's maintenance)
# ============================================================================

def doc_path(instance, root=None):
    root = root or _default_root()
    return os.path.join(str(root), instance, DOC_FILE)


def _default_root():
    import recollections as _rec
    return str(_rec.ROOT)


def write_document(instance, doc, root=None):
    """Atomic write of the built document + its counts (derived state —
    rebuildable, never authoritative)."""
    path = doc_path(instance, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"schema_version": 1, "at": doc.get("at"),
               "text": doc.get("text", ""),
               "counts": doc.get("counts", {}),
               "meta": doc.get("meta"),
               "generator": "memory_doc §ROLL (memorybuild Phase 10)"}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def load_document(instance, root=None):
    """The core-side read: {'text', 'counts'} or None (fail-open)."""
    try:
        with open(doc_path(instance, root), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("text"):
            return d
    except Exception:
        pass
    return None


def rebuild(instance, root=None, cfg=None):
    """The deterministic rebuild + write. The housekeeping jobs (admission,
    roll-off, shelf fade, chosen refresh) are ALL this function: it is
    deterministic, so re-running it after any trigger reproduces the
    document with only the intended change. Returns the doc dict."""
    import people as _pp
    import yaml as _yaml
    cfg = cfg
    if cfg is None:
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "configs", instance + ".yaml"),
                      encoding="utf-8") as f:
                cfg = _yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    roster = {}
    try:
        import people as _pp2
        roster = {pid: p.display_name for pid, p in _pp2.load_roster().items()}
    except Exception:
        roster = {}
    self_name = ""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        import yaml as _yaml2
        with open(os.path.join(base, "configs", instance + ".yaml"),
                  encoding="utf-8") as f:
            _c = _yaml.safe_load(f) or {}
        _id = (cfg.get("app") or {}).get("instance_id", instance)
        self_name = {"residentb": "residentb", "residenta": "persona-a", "residentc": "residentc"}.get(_id, _id)
    except Exception:
        pass
    doc = build_document(instance, root, cfg=cfg, roster=roster,
                         self_name=self_name)
    write_document(instance, doc, root)
    return doc


# --- the dirty marker (the trigger) ----------------------------------------

def _dirty_path(instance, root=None):
    root = root or _default_root()
    return os.path.join(str(root), instance, "doc_dirty")


def mark_dirty(instance, root=None):
    try:
        p = _dirty_path(instance, root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("1")
        return True
    except Exception:
        return False


def is_dirty(instance, root=None):
    return os.path.exists(_dirty_path(instance, root))


def clear_dirty(instance, root=None):
    try:
        os.remove(_dirty_path(instance, root))
        return True
    except OSError:
        return False


def housekeep_if_dirty(instance, root=None, cfg=None):
    """The background worker's hook: rebuild ONLY when dirty (a marker from
    the turn's chronicle append or her sitting's acts). Fail-open."""
    if not is_dirty(instance, root):
        return None
    try:
        doc = rebuild(instance, root=root, cfg=cfg)
        clear_dirty(instance, root)
        return doc
    except Exception:
        return None
