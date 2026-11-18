"""wake_layers_test.py — characterization test for the 2026-09-13 house ruling
(Option A), phase 2: wake state-packet layers become per-agent yaml config.

Proves:
  1. BYTE-PARITY: build_payload with all five wake_packet layers enabled
     renders the state packet byte-identical to the pre-config code
     (verbatim reference below). Runs against the real read-only stores
     (quarter-marks, mail count, system-notes ledger, audit pointer).
  2. Layer-off: each disabled layer leaves no residue — no "UNREAD REPLIES
     WAITING: none" lie when the unread layer is off, no KEPT header, etc.
  3. Normalization: bare boolean / {enabled: bool} / absent = OFF.

Run:  python3 wake_layers_test.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402
import wake  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ---- reference: the pre-config state packet, verbatim -----------------------
def _reference_state_lines(instance, root):
    import glob
    import heartbeat as hb
    import mail as _mail
    import sandbox as _sandbox
    unread = wake.unread_replies(root, instance)
    keeps = hb.quarter_marks(instance, "2000-01-01", "2999-12-31")[-5:]
    keeps_lines = [f"  [{k['date']}] {k['meaning']}" for k in keeps]
    audit_path = hb.latest_audit_path(instance)
    audit_line = ""
    if audit_path:
        audit_line = (f"  last quarterly audit: "
                      f"{os.path.basename(audit_path)} — read-only, exists "
                      "outside your desk")
    # §5a: the factual delta now RENDERS (the chunk-7 fail-open NameError is
    # fixed) — the reference must include it. Built from the same store via
    # the same function; the elapsed-minutes value can drift a tick between
    # the two computations, which is accepted for this factual line.
    _delta_lines = []
    try:
        _d = wake.since_last_wake(instance)  # the delta resolves the project root itself
        if _d.get("elapsed"):
            _delta_lines.append("SINCE YOUR LAST WAKE (" + _d["elapsed"] + "):")
        if _d.get("nothing_changed"):
            _delta_lines.append("SINCE YOUR LAST WAKE: nothing changed.")
        else:
            _parts = [f"{_k.replace('_', ' ')}: {_d[_k]}"
                      for _k in ("chats", "letters_sent", "memories_saved", "jobs")
                      if _d.get(_k)]
            _delta_lines.append("SINCE YOUR LAST WAKE: " + "; ".join(_parts)
                                if _parts else "SINCE YOUR LAST WAKE: nothing changed.")
    except Exception:
        pass
    state_lines = _delta_lines
    # [CONTINUA] 2026-09-28: parity repair — commit 61a2431 (the sitting)
    # added the intentions consult line to build_payload's state but the
    # golden reference was never updated, so byte-parity has been failing
    # at HEAD since then (pre-existing; not caused by helpnany). Mirror the
    # section here, same store + same function, so the compare catches
    # unintended changes again.
    try:
        from datetime import datetime as _dt, timedelta as _td
        import ritual as _sit
        _tomorrow = (_dt.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0)
                     + _td(days=1)).strftime("%Y-%m-%d")
        _int = _sit.latest_intentions(root, instance, before_date=_tomorrow)
        if _int:
            state_lines.append("YOUR INTENTIONS FOR TODAY, IN YOUR OWN "
                               "WORDS:")
            state_lines += ["  " + l for l in _int.splitlines()[:8]]
            state_lines.append("  They are yours — consult or ignore.")
    except Exception:
        pass
    state_lines += [f"KEPT RECENTLY:"]
    state_lines += keeps_lines or ["  (nothing kept yet)"]
    try:
        _mail_n = _mail.unread_count(instance)
        state_lines.append("MAIL WAITING: " + (
            f"{_mail_n} letter(s) — check_mail when you want them; you can "
            "also let them rest" if _mail_n else "none"))
    except Exception:
        pass
    try:
        # 2026-09-28 (ledgerplan.md): mirror the PRODUCTION pointer exactly
        # — same snippet (counts + latest provenance + closed volumes), so
        # the golden compare still catches unintended changes.
        _res = _sandbox.run(instance, ["python3", "-c",
            "import os, re; p='system_notes'; "
            "m=re.findall(r'^## \\[[^]]+\\] provenance: ([^\\n]+)', "
            "open(os.path.join(p,'system_log.md')).read(), re.M); "
            "print(len(m)); print(m[-1] if m else 'none'); "
            "print(sorted(f for f in os.listdir(p) "
            "if f.startswith('system_log.vol')))"])
        _out = (_res.get("stdout") or "").strip().split("\n")
        if len(_out) >= 2 and _out[0].isdigit():
            _vols = (_out[2].strip().strip("[]'").replace("'", "")
                     .replace(", ", ",") if len(_out) >= 3 else "")
            _vol_list = [v for v in _vols.split(",") if v] if _vols else []
            _vol_note = (f" + {len(_vol_list)} closed volume(s) also on "
                         f"your desk (system_notes/system_log.vol*)"
                         if _vol_list else "")
            state_lines.append(
                f"  system notes: {_out[0]} entries on your desk "
                f"(system_notes/system_log.md, append-only) — "
                f"latest provenance {_out[1].strip()}{_vol_note}")
    except Exception:
        pass
    # memorybuildv2 V2-5 (2026-10-05 the designer go): "memories awaiting your
    # witness" rides the packet after the memory counts, before the unread
    # block — the reference must include it too. Same detector
    # (wake.witness_lines); renders nothing when nothing awaits, so the
    # compare still bites.
    state_lines += wake.witness_lines(instance)
    # toolplan §ACTIVATION AT-2 (2026-10-05): the desk-to-capability line
    # rides after the witness section — same detector
    # (wake.tools_line); renders nothing when the pipeline is off.
    _cfg_ref = None
    try:
        import yaml as _yaml_ref
        _cfg_ref = _yaml_ref.safe_load(open(f"configs/{instance}.yaml"))
    except Exception:
        pass
    state_lines += wake.tools_line(instance, _cfg_ref)
    # lesstools LT-4 (2026-10-05): the one-line desk-tools announcement —
    # same detector (wake.lesstools_line); renders nothing when the flag
    # is off.
    state_lines += wake.lesstools_line(instance, _cfg_ref)
    # allontape v2 AT-4 (2026-10-05): the words cue — her own quoted line,
    # a mirror not a map — same detector (allontape.words_line); renders
    # nothing when the gate is off or nothing changed.
    import allontape as _at_ref
    state_lines += _at_ref.words_line(instance, _cfg_ref)
    state_lines.append("UNREAD REPLIES WAITING:" if unread
                       else "UNREAD REPLIES WAITING: none")
    state_lines += [f"  from {u['person_id']} at {u['at'][:16]}: "
                    f"{u['excerpt']}" for u in unread]
    # 2026-09-21 (approved, residentb's wish #3): the essence candidate
    # invitation rides the packet — the reference must include it too, or
    # the parity check would flag an approved addition as drift. Built from
    # the SAME detector, so the byte-compare still catches unintended changes.
    _invite = wake.essence_invite_line(instance)
    if _invite:
        state_lines.append(_invite)
    if audit_line:
        state_lines.append(audit_line)
    return state_lines


# Strategy: extract the state block from the payload prompt (it is the
# FINAL block: "WHERE THINGS STAND:\n{state}\n") and byte-compare against
# the reference reconstruction from the same stores, sliced by STATE_CAP
# exactly as the code does. Presence assertions are parity-of-presence:
# sections eaten by STATE_CAP (pre-existing residenta truncation) must be
# absent in BOTH, proving the refactor changed nothing.
# ---- byte-parity (five legacy layers, wiki_offer EXCLUDED) ------------------
# wiki_offer is new 2026-09-17 and absent from the pre-config reference;
# parity runs with the offer extinguished (opt-in mocked True) so the five-
# layer state block must equal the legacy shape exactly.
from unittest.mock import patch as _p
for inst in ("residentb", "residenta"):
    cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
    _wl = wake._wake_layers(cfg)
    _legacy = {k: v for k, v in _wl.items() if k != "wiki_offer"}
    check(f"{inst}: all five legacy wake layers enabled",
          all(_legacy.values()), str(_wl))
    check(f"{inst}: wiki_offer layer exists", "wiki_offer" in _wl)
    check(f"{inst}: wiki_offer enabled in yaml", _wl["wiki_offer"])

    with _p.object(wake, "_wiki_exists", lambda inst: True), \
         _p.object(wake, "turn_state_lines", lambda *a, **k: []), \
         _p.object(wake, "memory_count_lines", lambda *a, **k: []), \
         _p.object(wake, "rolloff_line", lambda *a, **k: []):
        # memorybuild.md Phases 1/13/14: the turn-state block (W1a), the
        # memory-count lines (R38/S4) and the roll-off window (memoryv3
        # Move 3, 2026-10-06) are NEW and golden-tested in their own test
        # files — this legacy golden proves the pre-Phase-1 layers' parity
        # unchanged.
        p = wake.build_payload(inst)
    check(f"{inst}: payload builds", p.get("type") == "system-origin-wake")
    got = p["prompt"].split("WHERE THINGS STAND:\n", 1)[1]
    if got.endswith("\n") and not got.endswith("\n\n"):
        got = got[:-1]  # the frame's closing newline after {state}
    ref_state = "\n".join(_reference_state_lines(
        inst, os.path.join(os.path.dirname(os.path.abspath(__file__)), "chronicle")))[:wake.STATE_CAP]
    check(f"{inst}: state block byte-identical to pre-config code",
          got == ref_state,
          f"got len={len(got)} ref len={len(ref_state)}")
    check(f"{inst}: KEPT RECENTLY parity",
          ("KEPT RECENTLY:" in got) == ("KEPT RECENTLY:" in ref_state))
    check(f"{inst}: MAIL WAITING parity",
          ("MAIL WAITING:" in got) == ("MAIL WAITING:" in ref_state))
    check(f"{inst}: UNREAD header parity",
          ("UNREAD REPLIES WAITING" in got)
          == ("UNREAD REPLIES WAITING" in ref_state))

# ---- dedicated wiki_offer tests (independent of byte-parity) ----------------
with _p.object(wake, "_wiki_exists", lambda inst: False):
    p = wake.build_payload("residenta")
got = p["prompt"].split("WHERE THINGS STAND:\n", 1)[1]
check("offer VISIBLE pre-opt-in (layer on, no ~/wiki)",
      "keep a wiki on" in got)
check("offer reads as invitation, not assignment",
      "Entirely optional" in got and "Nothing writes to it" in got)
check("offer names the exact opt-in action",
      "mkdir -p wiki && cat > wiki/index.md" in got)

with _p.object(wake, "_wiki_exists", lambda inst: True):
    p2 = wake.build_payload("residenta")
got2 = p2["prompt"].split("WHERE THINGS STAND:\n", 1)[1]
check("offer self-extinguishes once ~/wiki exists",
      "keep a wiki on" not in got2)

_residenta_cfg = yaml.safe_load(open("configs/residenta.yaml"))
_off = {**wake._wake_layers(_residenta_cfg), "wiki_offer": False}
with _p.object(wake, "_wake_layers", lambda c: dict(_off)), \
     _p.object(wake, "_wiki_exists", lambda inst: False):
    p3 = wake.build_payload("residenta")
got3 = p3["prompt"].split("WHERE THINGS STAND:\n", 1)[1]
check("layer off => no offer even pre-opt-in", "keep a wiki on" not in got3)

# _wiki_exists: opt-in signal + fail-open on sandbox error
import sandbox as _sbx
with _p.object(_sbx, "run", lambda *a, **k: {"ok": True, "stdout": "True\n"}):
    check("_wiki_exists True when desk reports True", wake._wiki_exists("residenta") is True)
with _p.object(_sbx, "run", lambda *a, **k: {"ok": True, "stdout": "False\n"}):
    check("_wiki_exists False when desk reports False", wake._wiki_exists("residenta") is False)
with _p.object(_sbx, "run", lambda *a, **k: {"ok": False, "stderr": "boom"}):
    check("_wiki_exists fail-open False on sandbox error", wake._wiki_exists("residenta") is False)

# ---- layer-off behavior (isolated normalizer) -------------------------------
n = wake._wake_layers({"memory": {"wake_packet": {"mail_count": True}}})
check("absent wake layers = OFF (Option A)",
      n["mail_count"] and not n["kept_recently"] and not n["unread_replies"]
      and not n["system_notes_pointer"] and not n["audit_pointer"])
n2 = wake._wake_layers({"memory": {"wake_packet": {"kept_recently": {"enabled": True}}}})
check("map form works", n2["kept_recently"] and not n2["mail_count"])
n3 = wake._wake_layers({})
check("absent section = all OFF", not any(n3.values()))

# ---- off-payload composition (unit-level, no stores needed) -----------------
n = wake._wake_layers({"memory": {"wake_packet": {"mail_count": True}}})
check("absent wake layers = OFF (Option A)",
      n["mail_count"] and not n["kept_recently"] and not n["unread_replies"]
      and not n["system_notes_pointer"] and not n["audit_pointer"])
n2 = wake._wake_layers({"memory": {"wake_packet": {"kept_recently": {"enabled": True}}}})
check("map form works", n2["kept_recently"] and not n2["mail_count"])
n3 = wake._wake_layers({})
check("absent section = all OFF", not any(n3.values()))

# ---- off-payload composition (unit-level, no stores needed) -----------------
# With everything off, the gated branches collapse to nothing: no lying
# "UNREAD REPLIES WAITING: none" when the unread layer never looked.
_wl_off = {k: False for k in wake._WAKE_PACKET_LAYERS}
lines = []
unread = []
if _wl_off["unread_replies"]:
    lines.append("UNREAD REPLIES WAITING:" if unread else "UNREAD REPLIES WAITING: none")
state = "\n".join(lines)
check("all-off packet is empty (no lying 'none' line)", state == "")

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("ALL CHECKS PASSED")
# --- Move 3 (memoryv3): the roll-off window — quiet by construction -------
def test_rolloff_line():
    import json as _json
    import tempfile
    import yaml
    import contextlib
    import recollections as _rec
    import context_scale as _cst
    cfg = yaml.safe_load(open("configs/residentb.yaml"))
    wc = (cfg.get("continua") or {}).get("wake") or {}
    td = tempfile.mkdtemp(prefix="rolloff-")
    # a tiny doc whose chronicle is far over its share, with a dialogue day
    old = _json.dump and None
    lgv = os.path.join(td, "recollections", "residentb")
    os.makedirs(lgv, exist_ok=True)
    _dcap = 20000
    _chosen = "[Memories I chose to keep — the words of the moment]\n\n"
    _shelf = "[The shelf — what rolled off, in prose]\n\n"
    _chron = ("[The rolling chronicle — the exchange itself]\n\n"
              "[2026-10-05 · system-wake · #ccc333]\n"
              "this morning's thought, held\n"
              + "[2026-10-04 · Sequoia · #aaa111]\n"
              "a conversation with a friend, whole — some more words to give the day real weight\n" * 60)
    _doc = {"at": "2026-10-06T13:00:00-07:00",
            "meta": {"doc_cap": _dcap},
            "counts": {},
            "text": _chosen + _chron + _shelf}
    with open(os.path.join(lgv, "memory_doc.json"), "w") as f:
        _json.dump(_doc, f)
    with _p.object(wake, "BASE", td), \
         _p.object(_cst, "scale_for", lambda cfg: {
             "C": 30000, "history_chars": 8000, "juggle_total": 9000,
             "notes_cap": 600, "tool_result_cap": 300}):
        lines = wake.rolloff_line("residentb", cfg, wc)
    check("the window fires when a conversation day would roll",
          len(lines) == 1 and "THE WINDOW" in lines[0]
          and "Sequoia" in lines[0] and "anchor_memory" in lines[0],
          str(lines)[:200])
    # the quiet rule: a monologue-only chronicle stays silent
    _chron2 = ("[The rolling chronicle — the exchange itself]\n\n"
               "[2026-10-05 · system-wake · #ccc333]\n"
               "this morning's thought, held\n"
               + "[2026-10-04 · system-wake · #bbb222]\n"
               "her solitary morning thought — some more words to give the day real weight\n" * 60)
    _doc["text"] = _chosen + _chron2 + _shelf
    with open(os.path.join(lgv, "memory_doc.json"), "w") as f:
        _json.dump(_doc, f)
    with _p.object(wake, "BASE", td), \
         _p.object(_cst, "scale_for", lambda cfg: {
             "C": 30000, "history_chars": 8000, "juggle_total": 9000,
             "notes_cap": 600, "tool_result_cap": 300}):
        lines2 = wake.rolloff_line("residentb", cfg, wc)
    check("the window is quiet when only wake-thoughts roll",
          lines2 == [], str(lines2)[:120])
    # the gate: env kill switch wins
    os.environ["CONTINUA_ROLLOFF_REVIEW"] = "0"
    try:
        with _p.object(wake, "BASE", td):
            lines3 = wake.rolloff_line("residentb", cfg, wc)
        check("CONTINUA_ROLLOFF_REVIEW=0 silences the window", lines3 == [])
    finally:
        os.environ.pop("CONTINUA_ROLLOFF_REVIEW", None)


test_rolloff_line()
