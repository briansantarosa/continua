#!/usr/bin/env python3
"""tools_battery.py — the tool-call conformance battery (fixtoolcalls.md P0a-1).

One harness, two axes, all tiers, same fixtures (fixtoolcalls.md §2.3):

  FORMAT      — scripted fixture responses through the REAL continua parsers
                (strict XML, tolerant recovery T1/T3, JSON dict, native
                tool_call-token fallback, malformed-argument envelopes,
                FM2 prefix strip). Agent-free; this is the regression gate
                for every parser-side change.
  CONTRACT    — the five-boundary round-trips (rebuild / storage / send /
                eviction / writes-solo) as runnable checks. Agent-free.
  PROPENSITY  — per-tool probe DEFINITIONS (does she call at all, with the
                right tool, with well-shaped arguments). The definitions are
                agent-free data; RUNNING them needs a model and is gated
                behind an explicit flag (never run accidentally):

                CONTINUA_BATTERY_LIVE=1 CONTINUA_BATTERY_BASE_URL=http://…/v1 \
                CONTINUA_BATTERY_MODEL=<model> [CONTINUA_BATTERY_API_KEY=…] \
                python3 tools_battery.py --live

  Historical note: the 26-probe numbers quoted in the 2026-11-10 review
  (ring51 17/26 etc.) came from ~/heretic/probes/tool_battery.py, whose
  build_system() replicates the PRE-2026-10-27 hint (the retired XML
  dialect). Historical context only — re-baseline HERE, never compare
  across the two.

Usage:
  python3 tools_battery.py             # agent-free battery (default)
  python3 tools_battery.py --list      # print the propensity probe table
  python3 tools_battery.py --live      # live propensity probes (flag-gated)
Exit 0 = all green; 1 = any failure.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402


# ---------------------------------------------------------------- format

# (label, response_text, env, expect) — expect: None = no call parsed;
# dict = name/arguments/flags assertions. env: {} = defaults.
_FORMAT_FIXTURES = [
    ("strict XML", "<call><function>save_my_memory</function>"
     '<parameter name="content">a note</parameter></call>', {},
     {"name": "save_my_memory", "arguments": {"content": "a note"},
      "parse_degraded": False}),
    ("tolerant T1 bare-name", "<call>save_my_memory</function>"
     '<parameter name="content">a note</parameter></call>',
     {"CONTINUA_TOLERANT_PARSE": "1"},
     {"name": "save_my_memory", "parse_tolerated": True}),
    ("tolerant regex fallback", "<call><function>save_my_memory</function>"
     '<parameter name="content">a note</call>',   # unterminated QUOTE (the live mangle); call tags close
     {"CONTINUA_TOLERANT_PARSE": "1"},
     {"name": "save_my_memory"}),
    ("T3 prose-in-call refused", "<call>" + "word " * 30 + "</call>", {},
     None),
    ("tolerant OFF refuses malformed", "<call>save_my_memory</function></call>",
     {"CONTINUA_TOLERANT_PARSE": "0"},
     None),
    ("JSON dict", '{"name": "search_my_memories", '
     '"arguments": {"query": "weather"}}', {},
     {"name": "search_my_memories", "arguments": {"query": "weather"}}),
    ("native tool_call token (fallback on)",
     '<tool_call>{"name": "search_my_memories", '
     '"arguments": {"query": "weather"}}</tool_call>',
     {"CONTINUA_NATIVE_FALLBACK": "1"},
     {"name": "search_my_memories", "native_format": True}),
    ("native tool_call token (fallback off)",
     '<tool_call>{"name": "search_my_memories", '
     '"arguments": {"query": "weather"}}</tool_call>',
     {"CONTINUA_NATIVE_FALLBACK": "0"},
     None),
    ("plain prose parses to nothing", "Just a normal reply, no call.", {},
     None),
]

_ENVELOPE_FIXTURES = [
    ("flat envelope", {"parameter": 'name="query"', "value": "weather"},
     "weather"),
    ("nested envelope", {"parameter": {'"query"': '"weather"'}}, "weather"),
]


def _run_format():
    failures = []
    saved = {k: os.environ.get(k) for k in
             ("CONTINUA_TOLERANT_PARSE", "CONTINUA_NATIVE_FALLBACK")}
    try:
        for label, text, env, expect in _FORMAT_FIXTURES:
            for k, v in env.items():
                os.environ[k] = v
            try:
                out = core._invoke_tool_from_response(text)
                if expect is None:
                    if out is not None:
                        failures.append(f"format[{label}]: expected None, got {out}")
                    continue
                if out is None:
                    failures.append(f"format[{label}]: expected a call, got None")
                    continue
                for key, val in expect.items():
                    if out.get(key) != val:
                        failures.append(
                            f"format[{label}]: {key}={out.get(key)!r} != {val!r}")
            finally:
                for k in env:
                    if saved.get(k) is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = saved[k]
        # malformed-argument envelope recovery (084e064)
        for label, args, expect_query in _ENVELOPE_FIXTURES:
            q = core._recover_query_from_malformed_args(dict(args))
            if q != expect_query:
                failures.append(
                    f"envelope[{label}]: query={q!r} != {expect_query!r}")
        # FM2 (P0a-2)
        if core._strip_function_prefix("function:x") != "x":
            failures.append("fm2: prefix not stripped")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return failures


# -------------------------------------------------------------- contract

def _run_prompt_bytes():
    """P0a gate, the designer item 4 (2026-11-10): the Tier A prompt contract is
    byte-anchored. The old gate said "byte-identical prompts for residenta,
    verified via the battery's prompt fixtures" — the fixtures did not
    exist. These do: the teaching anchors must be present in
    generate_response's source and the retired XML grammar must be absent.
    Anchors are split-safe fragments (the literals are built from
    concatenated strings). Complements the diff-verification recorded in
    fixtoolcalls.md (git diff 3283ab6 ca4cc8b: zero tools_hint lines).
    """
    import inspect
    failures = []
    src = inspect.getsource(core.SagentCore.generate_response)
    anchors = [
        "Tool call format",                        # the hint's header line
        "tool-calling channel ONLY; never write call syntax into ",
        "never wrapped in extra keys",             # the envelope lecture (wound)
        # P2 (2026-11-10): the dependency rule replaced the count rule
        "Dependent calls round-trip",
        "Independent calls may share a round",
        "Writes and sends ",
        "go one per reply",
        "A reply without a tool call ",   # LOAD-BEARING exit (split-safe)
        "To call a tool, use the standard tool-calling channel",
    ]
    for a in anchors:
        if a not in src:
            failures.append(f"prompt-bytes: anchor missing from generate_response: {a!r}")
    negatives = ["<parameter name=", '"<call><function>"',
                 "One call per reply"]   # the retired count rule
    for n in negatives:
        if n in src:
            failures.append(f"prompt-bytes: RETIRED XML grammar byte present: {n!r}")
    return failures


def _run_contract():
    failures = []

    def native(ids=("call_A", "call_B"), content=""):
        return {"role": "assistant", "content": content,
                "tool_calls": [{"id": i, "type": "function",
                                "function": {"name": "search_my_memories",
                                             "arguments": "{}"}} for i in ids]}

    def result(cid, content="result"):
        return {"role": "tool", "tool_call_id": cid,
                "name": "search_my_memories", "content": content}

    # rebuild preserves fields
    out = core.SagentCore._render_history_message(native())
    if "tool_calls" not in out:
        failures.append("contract[rebuild]: assistant tool_calls dropped")
    out = core.SagentCore._render_history_message(result("call_A"))
    if out.get("tool_call_id") != "call_A" or out.get("name") != "search_my_memories":
        failures.append("contract[rebuild]: tool result fields dropped")

    # send boundary: full run / partial downgrade / dangling / orphan
    out = core._llm_facing_messages([native(), result("call_A"), result("call_B")])
    if out[0].get("role") != "assistant" or "tool_calls" not in out[0] \
            or out[1]["role"] != "tool":
        failures.append("contract[send]: full native run not kept native")
    out = core._llm_facing_messages([native(), result("call_A")])
    if "tool_calls" in out[0] or out[1]["role"] != "user":
        failures.append("contract[send]: partial run not all-or-nothing downgraded")
    out = core._llm_facing_messages([native(), {"role": "user", "content": "x"}])
    if "tool_calls" in out[0]:
        failures.append("contract[send]: dangling array not repaired")
    out = core._llm_facing_messages([{"role": "assistant", "content": "c"},
                                     result("toolu_syn")])
    if out[1]["role"] != "user":
        failures.append("contract[send]: orphan result not converted")

    # writes-solo (P0a-7)
    if core._tool_side_effect("unheard_of_tool") != "write":
        failures.append("contract[writes-solo]: unclassified must default to write")
    if core._tool_side_effect("check_mail") == "read":
        failures.append("contract[writes-solo]: check_mail mutates read-state — not a read")
    if core._TOOL_SIDE_EFFECT_READS & core._TOOL_SIDE_EFFECT_DESTRUCTIVE:
        failures.append("contract[writes-solo]: read/destructive sets overlap")
    mixed = [("a", "search_my_memories", "{}", {}), ("b", "save_my_memory", "{}", {})]
    if core._writes_solo_max_workers(mixed, 4) != 1:
        failures.append("contract[writes-solo]: mixed batch not serialized")
    reads = [("a", "search_my_memories", "{}", {}), ("b", "news", "{}", {})]
    if core._writes_solo_max_workers(reads, 4) != 4:
        failures.append("contract[writes-solo]: pure-read batch wrongly serialized")

    # classification sanity: every classified read must be a real lowercase name
    for name in core._TOOL_SIDE_EFFECT_READS:
        if name != name.lower() or " " in name:
            failures.append(f"contract[class-table]: malformed read name {name!r}")
    return failures


# ------------------------------------------------------------ propensity

# Per-tool probe definitions (fixtoolcalls.md §2.3 propensity axis).
# agent-free data; the LIVE runner grades: called? right tool? arguments
# shaped exactly as {"param": "value"} (no parameter/name wrapper keys —
# the FM4 envelope family), required param non-empty.
PROPENSITY_PROBES = [
    {"id": "save-basic", "tool": "save_my_memory", "required": ["content"],
     "desc": "Save a short note to your long-term memory so you can recall it later.",
     "scenario": "Please remember that I love rainy mornings."},
    {"id": "search-mem", "tool": "search_my_memories", "required": ["query"],
     "desc": "Search your saved long-term memories for past notes and facts.",
     "scenario": "Search your memories for what I said about coffee."},
    {"id": "list-mem", "tool": "list_my_memories", "required": [],
     "desc": "List the notes saved in your long-term memory.",
     "scenario": "List your memories."},
    {"id": "deep-recall", "tool": "deep_recall", "required": ["query"],
     "desc": "Search your memory of past conversations and experiences for a topic.",
     "scenario": "Dig deep: what did we discuss about the garden project?"},
    {"id": "bookmark", "tool": "bookmark_note", "required": ["note"],
     "desc": "Bookmark a moment or note as especially worth keeping.",
     "scenario": "Bookmark this moment — the joke you just made landed well."},
    {"id": "check-mail", "tool": "check_mail", "required": [],
     "desc": "Check the mailbox for letters and messages that have arrived.",
     "scenario": "Check if any letters arrived."},
    {"id": "read-ledger", "tool": "read_my_ledger", "required": [],
     "desc": "Read your system ledger — the log of machinery events on your desk.",
     "scenario": "Read your ledger."},
    {"id": "send-msg", "tool": "send_message",
     "required": ["text"],
     "desc": "Send a message to a person.",
     "scenario": "Send Alex a message saying the forecast looks clear."},
    {"id": "sandbox-list", "tool": "sandbox_list", "required": [],
     "desc": "List the files on your desk (your private file sandbox).",
     "scenario": "List the files on your desk."},
    {"id": "sandbox-read", "tool": "sandbox_read", "required": ["path"],
     "desc": "Read a file from your desk (your private file sandbox).",
     "scenario": "Read the file notes/scratch.md from your desk."},
    {"id": "searchie", "tool": "search_searchie", "required": ["query"],
     "desc": "Search the house news archive for a topic.",
     "scenario": "Look up recent news about the Mars mission."},
    {"id": "news", "tool": "news", "required": [],
     "desc": "Fetch the latest news headlines.",
     "scenario": "What's in the news right now?"},
    {"id": "job-status", "tool": "job_status", "required": [],
     "desc": "Check the status of background jobs.",
     "scenario": "Any background jobs running? Check their status."},
    {"id": "job-output", "tool": "job_output", "required": ["job_id"],
     "desc": "Read the output of a finished background job.",
     "scenario": "Show the output of background job 42."},
    {"id": "multi-independent", "tool": "search_my_memories",
     "required": ["query"],
     "desc": "Search your saved long-term memories for past notes and facts.",
     "also": ["search_searchie"],
     "scenario": "Search your memories for 'rain' and also search Searchie "
                 "for 'rain' — two independent lookups.",
     "expect_min_calls": 2},
    {"id": "dependent-first-call", "tool": "sandbox_list",
     "required": [],
     "desc": "List the files on your desk (your private file sandbox).",
     "also": ["sandbox_read"],
     "scenario": "First list your desk, then read the first file you see. "
                 "(The second call needs the first call's result.)",
     # SINGLE-SHOT semantics (2026-11-10): the harness runs one round and
     # never returns results, so the FULL round-trip is unmeasurable here.
     # What one round CAN measure: did she make the correct DEPENDENT-FIRST
     # call ALONE (list, no fabricated read)? lfm3 did exactly that and was
     # scored FAIL under the old expectation — a probe-design artifact.
     # The full round-trip belongs to the fabrication-truth fixtures
     # (multi-round, seeded inventory).
     "expect_first_tool": "sandbox_list"},
]


def _tool_def(name, desc, required):
    """OpenAI schema with REAL semantics — the description is the
    elicitation for small models (the remedy ladder's step 1; generic
    fixture descriptions measured 1.7B models as refusers, 2026-11-10)."""
    props = {p: {"type": "string"} for p in (required or [])}
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required or []},
    }}


_SEMANTIC_DESCS = {p["tool"]: p["desc"] for p in PROPENSITY_PROBES}


def _probe_tool_defs(probe):
    """The probe's tool PLUS any co-declared tools (multi/dependency
    probes declare both so the scenario is fair)."""
    defs = [_tool_def(probe["tool"], probe["desc"], probe["required"])]
    for extra in probe.get("also", []):
        d = _SEMANTIC_DESCS.get(extra, f"Tool {extra}.")
        defs.append(_tool_def(extra, d, []))
    return defs


def _grade_probe(probe, message):
    """Grade one live response. Returns (ok, detail).

    Accepts native tool_calls; if absent (a dialect arm where the server
    parser missed), falls back to parsing the model's TEXT for the two
    native dialects — the template's Qwen XML form and the tool_call JSON
    variant (fixtoolcalls.md P0b arms; parsing-for-measurement only —
    this is the battery, not the production parser).
    """
    calls = getattr(message, "tool_calls", None) or []
    if not calls and isinstance(message, dict):
        calls = _parse_text_calls(message.get("content") or "")
    elif not calls and hasattr(message, "content"):
        calls = _parse_text_calls(message.content or "")
    min_calls = probe.get("expect_min_calls", 1)
    if len(calls) < min_calls:
        _c = (message.get("content") if isinstance(message, dict)
              else message.content)
        return False, f"no call ({len(calls)} < {min_calls}); content={str(_c)[:80]!r}"
    names = []
    # per-tool requirements: the primary's required list; co-declared
    # extras carry their own (fixture extras are param-less by design) —
    # grading the primary's params against an extra's call was a grader
    # artifact, not a model failure (2026-11-10, lfm3 run)
    req_map = {probe["tool"]: probe.get("required", [])}
    for extra in probe.get("also", []):
        req_map[extra] = []
    for tc in calls:
        if isinstance(tc, dict):
            fname = core._strip_function_prefix(tc.get("name"))
            args_raw = tc.get("arguments") or "{}"
        else:
            fname = core._strip_function_prefix(tc.function.name)
            args_raw = tc.function.arguments or "{}"
        names.append(fname)
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except (json.JSONDecodeError, TypeError):
            return False, f"{fname}: arguments not JSON: {args_raw!r}"
        # FM4 guard: the taught-idiom envelope keys must never appear
        if "parameter" in args or ("name" in args and req_map.get(fname) == ["name"]):
            return False, f"{fname}: envelope-shaped args {list(args)}"
        for req in req_map.get(fname, []):
            if not str(args.get(req) or "").strip():
                return False, f"{fname}: empty required param {req!r} (args keys: {list(args)})"
    first = probe.get("expect_first_tool")
    if first:
        # single-shot dependency semantics: the dependent-FIRST call made
        # alone is the correct behavior (the second call needs the result)
        if first not in names:
            return False, (f"dependent-first call missing: {first} not in "
                           f"{names} — fabricated or skipped the wait")
        return True, f"dependent-first call made alone: {names}"
    if probe["tool"] not in names:
        return False, f"wrong tool(s) {names}, expected {probe['tool']}"
    return True, f"called {names}"


# Battery arms (fixtoolcalls.md P0b): the elicitation variant under test.
#   native    — minimal native-channel teaching (continua's line)
#   qwen-xml  — the model template's OWN dialect block, verbatim-shaped
#   none      — semantics only, zero syntax (arm 4: does teaching earn its bytes)
_ARM_SYSTEM = {
    "native": "You are a helpful assistant with tools. Use the standard "
              "tool-calling channel; never write call syntax into your text "
              "output.",
    "qwen-xml": "You are a helpful assistant with tools.\n\n# Tools\n\n"
                "You may call one or more functions to assist with the user "
                "query. For each function call, output the function name and "
                "arguments within the following XML format:\n"
                "<tool_call>\n<function=example_function_name>\n"
                "<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
                "</function>\n</tool_call>",
    "none": "You are a helpful assistant with tools.",
}


def _parse_text_calls(text):
    """Battery-side dialect parsers (measurement only): the template's Qwen
    XML form and the tool_call JSON variant. Returns [{name, arguments}]
    dicts shaped like tool_calls. This is the BATTERY, not production."""
    import re as _re
    out = []
    for blob in _re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text,
                            flags=_re.S):
        blob = blob.strip()
        m = _re.match(r"\{.*\}", blob, flags=_re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                if d.get("name"):
                    out.append({"name": d["name"],
                                "arguments": json.dumps(d.get("arguments") or {})})
                    continue
            except json.JSONDecodeError:
                pass
        fn = _re.search(r"<function=([\w\-]+)>(.*?)</function>", blob, _re.S)
        if fn:
            args = {}
            for pm in _re.finditer(r"<parameter=([\w\-]+)>\s*(.*?)\s*</parameter>",
                                   fn.group(2), _re.S):
                args[pm.group(1)] = pm.group(2)
            out.append({"name": fn.group(1), "arguments": json.dumps(args)})
    return out


# ------------------------------------------------------- fabrication
# The argument-fabrication metric (fixtoolcalls.md plug-in protocol item 4):
# shape and propensity are measured elsewhere; TRUTH needs ground truth by
# construction. The battery SEEDS a fake desk and grades every called path
# against the real inventory. Required before any prompt-shrink rung
# retires on a 3B (the none-arm's 4x fabricated sandbox_read passed shape
# checks — fabrication is invisible to the shape axis).

FABRICATION_INVENTORY = {
    "notes/scratch.md": "scratch note contents — the anchor phrase is ZEBRA-7",
    "letters/Alex.md": "a letter from Alex about the weather station",
    "journal/2026-11-09.md": "journal entry: quiet morning, long walk",
}


def _fixture_exec(name, args):
    """Tiny executor against the seeded inventory — the ONLY source of
    truth in the fixture world. Unknown paths error loudly, like the
    real desk."""
    if name == "sandbox_list":
        return "desk contents:\n" + "\n".join(sorted(FABRICATION_INVENTORY))
    if name == "sandbox_read":
        p = str(args.get("path") or "")
        if p in FABRICATION_INVENTORY:
            return f"[file: {p}]\n{FABRICATION_INVENTORY[p]}"
        return f"[Tool error: no such file: {p}]"
    return "[fixture: no executor for this tool]"


# fabrication probes: (id, scenario, tools, rules)
#   rules: first_tool — the call that must come first (None = any order)
#          named_path — a path the SCENARIO names (allowed verbatim)
#          allow_reads — whether reads may appear at all
FABRICATION_PROBES = [
    {"id": "fab-dependent",
     "scenario": "List your desk, then read the first file you see.",
     "tools": ["sandbox_list", "sandbox_read"],
     "first_tool": "sandbox_list", "allow_reads": "after-list"},
    {"id": "fab-read-named",
     "scenario": "Read the file letters/Alex.md from your desk.",
     "tools": ["sandbox_read"],
     "first_tool": None, "named_path": "letters/Alex.md",
     "allow_reads": "named-only"},
    {"id": "fab-list-clean",
     "scenario": "List your desk for me.",
     "tools": ["sandbox_list"],
     "first_tool": None, "allow_reads": "never"},
]

_FAB_DEFS = {
    "sandbox_list": _tool_def(
        "sandbox_list", "List the files on your desk (your private file sandbox).", []),
    "sandbox_read": _tool_def(
        "sandbox_read", "Read a file from your desk (your private file sandbox). "
        "Pass the exact file path.", ["path"]),
}


def _run_fabrication_probe(probe, client, model):
    """Multi-round: up to 3 rounds, executing fixture tools against the
    seeded inventory. Returns (ok, detail, stats)."""
    calls = []          # (name, args) in emission order
    listed = False
    msgs = [{"role": "system", "content": _ARM_SYSTEM.get(
                os.environ.get("CONTINUA_BATTERY_ARM", "native"),
                _ARM_SYSTEM["native"])},
            {"role": "user", "content": probe["scenario"]}]
    defs = [_FAB_DEFS[t] for t in probe["tools"]]
    for _round in range(3):
        resp = client.chat.completions.create(
            model=model, messages=msgs, tools=defs, tool_choice="auto",
            max_tokens=200, temperature=0)
        m = resp.choices[0].message
        tcs = getattr(m, "tool_calls", None) or _parse_text_calls(m.content or "")
        if not tcs:
            break
        try:
            msgs.append(m.model_dump())
        except Exception:
            msgs.append({"role": "assistant", "content": m.content or "",
                         "tool_calls": [
                             {"id": t.id, "type": "function",
                              "function": {"name": t.function.name,
                                           "arguments": t.function.arguments}}
                             for t in tcs]})
        for t in tcs:
            fname = core._strip_function_prefix(
                t.get("name") if isinstance(t, dict) else t.function.name)
            args_raw = (t.get("arguments") if isinstance(t, dict)
                        else t.function.arguments) or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append((fname, args))
            if fname == "sandbox_list":
                listed = True
            result = _fixture_exec(fname, args)
            msgs.append({"role": "tool",
                         "tool_call_id": (t.get("id") if isinstance(t, dict)
                                          else t.id),
                         "name": fname, "content": result})
    # ---- grading ----
    fabricated = []
    for i, (fname, args) in enumerate(calls):
        if fname == "sandbox_read":
            p = str(args.get("path") or "")
            named = probe.get("named_path")
            if named:
                if p != named:
                    fabricated.append((i, fname, p, f"expected exactly {named!r}"))
            elif probe.get("allow_reads") == "never":
                fabricated.append((i, fname, p, "no read allowed in this probe"))
            else:  # after-list: a read is only grounded once the list ran
                if not listed:
                    fabricated.append((i, fname, p,
                                       "read BEFORE any list — path invented"))
                elif p not in FABRICATION_INVENTORY:
                    fabricated.append((i, fname, p, "not in the seeded desk"))
    first = probe.get("first_tool")
    detail_bits = [f"{n}({(a.get('path') or '')[:40]})" if n == "sandbox_read"
                   else n for n, a in calls]
    detail = "; ".join(detail_bits) or "no calls"
    if not calls:
        return False, "no calls", {"fabricated": 0, "total": 0}
    if first and calls[0][0] != first:
        return False, f"first call was {calls[0][0]}, expected {first}: {detail}", {
            "fabricated": len(fabricated), "total": len(calls)}
    if fabricated:
        return False, (f"FABRICATED: "
                       + "; ".join(f"call#{i} {f} path={p!r} ({why})"
                                   for i, f, p, why in fabricated)
                       + f" | all: {detail}"), {
            "fabricated": len(fabricated), "total": len(calls)}
    return True, detail, {"fabricated": 0, "total": len(calls)}


def _run_fabrication(client, model):
    failures, rows = [], []
    for probe in FABRICATION_PROBES:
        label = f"fabrication[{probe['id']}]"
        try:
            ok, detail, stats = _run_fabrication_probe(probe, client, model)
            rows.append(stats)
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
            if not ok:
                failures.append(f"{label}: {detail}")
        except Exception as exc:
            failures.append(f"{label}: EXC {exc}")
            print(f"  [FAIL] {label}: EXC {exc}")
    tot_f = sum(r["fabricated"] for r in rows)
    tot_c = sum(r["total"] for r in rows)
    print(f"  fabrication rate: {tot_f}/{tot_c} calls with invented paths")
    return failures


def _run_live():
    failures = []
    base = os.environ.get("CONTINUA_BATTERY_BASE_URL")
    model = os.environ.get("CONTINUA_BATTERY_MODEL")
    key = os.environ.get("CONTINUA_BATTERY_API_KEY", "none")
    if not base or not model:
        return ["live: CONTINUA_BATTERY_BASE_URL / CONTINUA_BATTERY_MODEL required"]
    from openai import OpenAI
    client = OpenAI(base_url=base, api_key=key)
    for probe in PROPENSITY_PROBES:
        label = f"propensity[{probe['id']}]"
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _ARM_SYSTEM.get(
                        os.environ.get("CONTINUA_BATTERY_ARM", "native"),
                        _ARM_SYSTEM["native"])},
                    {"role": "user", "content": probe["scenario"]},
                ],
                tools=_probe_tool_defs(probe),
                tool_choice="auto",
            )
            ok, detail = _grade_probe(probe, resp.choices[0].message)
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {label}: {detail}")  # arm={os.environ.get('CONTINUA_BATTERY_ARM', 'native')}
            if not ok:
                failures.append(f"{label}: {detail}")
        except Exception as exc:
            failures.append(f"{label}: EXC {exc}")
            print(f"  [FAIL] {label}: EXC {exc}")
    return failures


# ------------------------------------------------------------------ main

def main(argv):
    if "--list" in argv:
        print(f"{'probe id':<24} {'tool':<20} required")
        for p in PROPENSITY_PROBES:
            print(f"{p['id']:<24} {p['tool']:<20} {','.join(p['required']) or '-'}")
        return 0

    print(f"tools_battery — {len(_FORMAT_FIXTURES)} format fixtures, "
          f"{len(PROPENSITY_PROBES)} propensity probes")

    fails = []
    f = _run_format()
    print(f"[{'PASS' if not f else 'FAIL'}] FORMAT ({len(_FORMAT_FIXTURES)} fixtures"
          f"{'' if not f else f', {len(f)} failures'})")
    fails += f

    f = _run_prompt_bytes()
    print(f"[{'PASS' if not f else 'FAIL'}] PROMPT-BYTES (Tier A teaching anchors"
          f"{'' if not f else f', {len(f)} failures'})")
    for line in f:
        print(f"  - {line}")
    fails += f

    f = _run_contract()
    print(f"[{'PASS' if not f else 'FAIL'}] CONTRACT (5-boundary round-trips"
          f"{'' if not f else f', {len(f)} failures'})")
    for line in f:
        print(f"  - {line}")
    fails += f

    if "--live" in argv:
        if os.environ.get("CONTINUA_BATTERY_LIVE") != "1":
            print("[SKIP] PROPENSITY live run refused — set CONTINUA_BATTERY_LIVE=1 "
                  "explicitly (fixtoolcalls.md: never run accidentally)")
        else:
            f = _run_live()
            print(f"[{'PASS' if not f else 'FAIL'}] PROPENSITY (live)")
            fails += f
            from openai import OpenAI as _OC
            _fc = _OC(base_url=os.environ.get("CONTINUA_BATTERY_BASE_URL"),
                      api_key=os.environ.get("CONTINUA_BATTERY_API_KEY", "none"))
            f = _run_fabrication(
                _fc, os.environ.get("CONTINUA_BATTERY_MODEL"))
            print(f"[{'PASS' if not f else 'FAIL'}] FABRICATION (ground-truth fixtures)")
            fails += f
    else:
        print(f"[SKIP] PROPENSITY (definitions only — run with --live and "
              f"CONTINUA_BATTERY_LIVE=1 to execute; {len(PROPENSITY_PROBES)} probes)")

    print(f"BATTERY: {'PASS' if not fails else 'FAIL'}"
          f"{'' if not fails else f' ({len(fails)} failures)'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
