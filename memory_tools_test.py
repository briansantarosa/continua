"""memory_tools_test.py — characterization test for the 2026-09-13 house ruling
(Option A), phase 3: built-in memory tools become per-agent yaml config.

Proves:
  1. Injection: _get_function_definition injects the three built-ins ONLY
     when mem_tools enables them; yaml-defined same-name tools always win
     (no duplicates); env kill switches remain fleet overrides.
  2. Absent memory.tools section = no built-ins injected (Option A).
  3. Dispatch guard: a disabled tool invoked by a trained-in call returns
     the graceful configuration message, not a crash.

Run:  /home/you/Sagent/venv/bin/python memory_tools_test.py
"""
import sys
import os

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402
import core  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def _names(defs):
    return [d["function"]["name"] for d in defs]


# ---- 1. injection semantics --------------------------------------------------
defs = core._get_function_definition([], {"search_my_memories": True,
                                          "save_my_memory": True,
                                          "list_my_memories": True})
check("all-on injects the three built-ins",
      set(_names(defs)) == {"search_my_memories", "save_my_memory",
                            "list_my_memories"}, str(_names(defs)))

defs = core._get_function_definition([], None)
check("absent mem_tools = none injected", _names(defs) == [])

defs = core._get_function_definition([], {"save_my_memory": True})
check("partial config injects only the enabled one",
      _names(defs) == ["save_my_memory"])

defs = core._get_function_definition(
    [{"name": "search_my_memories", "description": "custom", "parameters": {}}],
    {"search_my_memories": True, "save_my_memory": True})
check("yaml-defined same-name tool wins, no duplicate",
      _names(defs) == ["search_my_memories", "save_my_memory"])

os.environ["SAGENT_AGENT_WRITE_MEM"] = "0"
defs = core._get_function_definition([], {"save_my_memory": True})
check("env kill switch overrides yaml true (fleet level)",
      "save_my_memory" not in _names(defs))
os.environ["SAGENT_AGENT_WRITE_MEM"] = "1"

# ---- 2. real configs ----------------------------------------------------------
# flag → the function defs its enablement must inject (the flags and the
# tool names diverged as the memory tools grew; the mapping is the contract)
_FLAG_TO_DEFS = {
    "search_my_memories": ["search_my_memories"],
    "save_my_memory": ["save_my_memory"],
    "list_my_memories": ["list_my_memories"],
    "notes": ["write_note", "read_note", "remove_note", "list_notes",
              "set_project", "list_projects"],
    "anchors": ["anchor_memory", "unanchor_memory"],
    "recall_my_experience": ["recall_my_experience"],
    "essence": ["write_essence", "endorse_essence", "list_essences"],
    "trajectory": ["my_trajectory"],
}
for inst in ("residentb", "residenta"):
    cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
    _mt = core._normalize_memory_tools(cfg.get("memory") or {})
    check(f"{inst}: all three memory tools enabled",
          all(_mt.values()), str(_mt))
    defs = core._get_function_definition(cfg.get("tools") or [], _mt)
    for flag, expected in _FLAG_TO_DEFS.items():
        if _mt.get(flag):
            check(f"{inst}: {flag} -> function defs present",
                  all(n in _names(defs) for n in expected),
                  str(sorted(set(expected) - set(_names(defs)))))

# ---- 3. dispatch guard ----------------------------------------------------------
c = core.SagentCore.__new__(core.SagentCore)
c.instance_id = "residentb"
c._mem_tools = {"search_my_memories": True, "save_my_memory": False,
                "list_my_memories": True}
r = c._execute_function_call("save_my_memory", {"content": "test note"},
                             user_id="1000000001")
check("disabled save returns graceful config message",
      r.startswith("[save_my_memory is not part of your current configuration"),
      r)
r2 = core.SagentCore.__new__(core.SagentCore)
r2.instance_id = "residentb"
r2._mem_tools = {k: True for k in core._MEM_TOOLS_DEFAULTS}
check("enabled tool passes the guard (proceeds to real path)",
      r2._execute_function_call("save_my_memory", {"content": ""},
                               user_id="1000000001")
      .startswith("[Tool error:"))  # empty-content guard = reached real path

# ---- 4. normalization ------------------------------------------------------------
n = core._normalize_memory_tools({"tools": {"save_my_memory": True}})
check("absent tool keys = OFF",
      n["save_my_memory"] and not n["search_my_memories"]
      and not n["list_my_memories"])
n2 = core._normalize_memory_tools({"tools": {"list_my_memories": {"enabled": True}}})
check("map form works", n2["list_my_memories"] and not n2["save_my_memory"])
n3 = core._normalize_memory_tools({})
check("absent section = all OFF", not any(n3.values()))

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("ALL CHECKS PASSED")