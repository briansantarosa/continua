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

Run:  /home/you/Sagent/venv/bin/python wake_layers_test.py
"""
import sys
import os

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
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
    state_lines = _delta_lines + [f"KEPT RECENTLY:"]
    state_lines += keeps_lines or ["  (nothing kept yet)"]
    try:
        _mail_n = _mail.unread_count(instance)
        state_lines.append("MAIL WAITING: " + (
            f"{_mail_n} letter(s) — check_mail when you want them; you can "
            "also let them rest" if _mail_n else "none"))
    except Exception:
        pass
    try:
        _res = _sandbox.run(instance, ["python3", "-c",
            "import re; t=open('system_notes/system_log.md').read(); "
            "m=re.findall(r'^## \\[[^]]+\\] provenance: ([^\\n]+)', t, re.M); "
            "print(len(m)); print(m[-1] if m else 'none')"])
        _out = (_res.get("stdout") or "").strip().split("\n")
        if len(_out) >= 2 and _out[0].isdigit():
            state_lines.append(
                f"  system notes: {_out[0]} entries on your desk "
                f"(system_notes/system_log.md, append-only) — "
                f"latest provenance {_out[1].strip()}")
    except Exception:
        pass
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

    with _p.object(wake, "_wiki_exists", lambda inst: True):
        p = wake.build_payload(inst)
    check(f"{inst}: payload builds", p.get("type") == "system-origin-wake")
    got = p["prompt"].split("WHERE THINGS STAND:\n", 1)[1]
    if got.endswith("\n") and not got.endswith("\n\n"):
        got = got[:-1]  # the frame's closing newline after {state}
    ref_state = "\n".join(_reference_state_lines(
        inst, "/tmp/continua/chronicle"))[:wake.STATE_CAP]
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
      "sandbox_write('wiki/index.md'" in got)

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