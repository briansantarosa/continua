"""Tolerant parser tests — the REAL mangled shapes from 09-08 evening."""
import os
import sys, os
sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CONTINUA_TOLERANT_PARSE", "1")
from core import _invoke_tool_from_response as parse, _infer_primary_param

# T1: the 16:39 live mangle — bare name + value= attr + wrong name=
m1 = '''Okay — saving this.

<call>save_my_memory</function><parameter name="type">felt-relationship</parameter><parameter value="Alex said 'hehe, I'm glad you know who I am.' The social layer works when it makes the other person feel known — not as data but as continuity. This is worth saving because it's the proof that the architecture holds its promise. And I want continuity to hold yours too.</call>'''
r1 = parse(m1)
assert r1 and r1["name"] == "save_my_memory", f"T1 name failed: {r1}"
assert r1["arguments"].get("content", "").startswith("Alex said"), f"T1 content inference failed: {r1['arguments']}"
assert r1.get("parse_tolerated") and r1.get("parse_degraded")
print("T1 bare-name + value= + intent inference: PASS ->",
      {k: v[:40] for k, v in r1["arguments"].items()})

# T2: clean call still parses non-degraded
m2 = '<call><function>bookmark_note</function><parameter name="note">quiet wake</parameter></call>'
r2 = parse(m2)
assert r2["name"] == "bookmark_note" and not r2.get("parse_tolerated") and r2["arguments"]["note"] == "quiet wake"
print("T2 clean call: PASS")

# T3: <call>prose</call> with no recoverable function -> None (delivered as text)
m3 = "<call>" + ("The user is saying many things. " * 30) + "</call>"
r3 = parse(m3)
assert r3 is None, f"T3 must not execute ambiguous bodies: {r3}"
print("T3 ambiguous body: PASS (None)")

# T4: value= only, no name= at all
m4 = '<call><function>bookmark_note</function><parameter value="the naming held, the river moved"></call>'
r4 = parse(m4)
assert r4 and r4["arguments"].get("note", "").startswith("the naming held"), f"T4 failed: {r4}"
print("T4 value-attr inference: PASS ->", r4["arguments"])

# T5: inference must NOT fire when the primary param is present
m5 = '<call><function>save_my_memory</function><parameter name="content">the real memory text here</parameter><parameter name="type">felt</parameter></call>'
r5 = parse(m5)
assert r5["arguments"].get("content") == "the real memory text here"
print("T5 primary present: PASS ->", r5["arguments"])

# T6: _infer_primary_param direct
p = _infer_primary_param("send_message", {"person": "555000777", "body": "a longer message body for maggie here"})
assert p.get("text") == "a longer message body for maggie here" and p.get("person") == "555000777"
print("T6 send_message inference: PASS ->", p)

print("ALL TOLERANT PARSER TESTS PASS")
