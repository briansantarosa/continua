"""tools_lint.py — the mechanical lint for her authored tools (toolplan.md,
stage 3). the designer approved in principle 2026-09-26.

Philosophy: MECHANICAL, never judgmental — the same shape as
export_public.py's banned-pattern gate. Lint checks the house rules a
candidate must satisfy before the designer ever sees it; it never judges the idea.
Fail-CLOSED (unlike the house's usual fail-open): a lint failure BLOCKS
submission, with the reason shown TO HER — she learns the house rules by
reading them (plan: "she learns the house rules by reading them").

House rules (v1):
  - single file, ast-parses, size cap (default 400 lines)
  - import allowlist: stdlib only for v1 (the venv's httpx NOT exposed to
    her tools in v1 — house helpers bring network in v1.1 via house_fetch)
  - banned patterns: reuse export_public.py's BANNED list (secrets, tokens,
    person-ids, host paths) — her tools never touch them
  - no subprocess, no raw sockets, no dynamic execution (eval/exec/compile)
  - no writes outside the desk: no absolute-path opens, no os.system
  - manifest sanity: my_ prefix (namespace rule — her tools NEVER override
    house tools, a deliberate departure from SAKU's override built-ins),
    args schema present, kill-switch name declared
"""

import ast
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

DEFAULT_SIZE_CAP = 400

# stdlib allowlist for v1 — deliberately conservative; the designer can extend by
# review (an edit to a tool re-lints through the same gate).
STDLIB_OK = {
    "json", "re", "math", "datetime", "time", "random", "textwrap",
    "collections", "itertools", "functools", "statistics", "string",
    "unicodedata", "calendar", "zoneinfo", "difflib", "html", "csv",
    "io", "os.path", "pathlib", "typing", "dataclasses", "enum", "abc",
}

# dynamic-execution / escape hatches — always banned in her tools
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open_system",
                "system", "popen", "spawn", "fork", "socket", "create_connection"}

# reuse export_public.py's banned-pattern gate (secrets, tokens, person
# ids, host paths, LAN endpoints) — HER tools must never touch them either
# reuse export_public.py's banned-pattern gate — the SECURITY subset the
# plan names (secrets, tokens, person-ids, host paths, LAN endpoints). The
# name-patterns (resident names) are deliberately NOT reused: her tools are
# git-tracked in the private ops repo and may mention her family; the
# security shapes may never appear.
_SEC_SUBSET = {"lan-ip", "host-path", "person-id", "tg-token", "sk-key",
               "ghp-token", "gh-pat", "slack-tok"}
try:
    import scripts.export_public as _ep
    _BANNED = [(n, re.compile(pat, re.IGNORECASE))
               for (n, pat, _note) in _ep.BANNED if n in _SEC_SUBSET]
except Exception:  # self-contained fallback, same shapes
    _BANNED = [
        ("lan-ip", re.compile(r"192\.168\.\d+\.\d+", re.I)),
        ("host-path", re.compile(r"/tmp/data", re.I)),
        ("person-id", re.compile(r"1000000001")),
        ("tg-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")),
        ("sk-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
        ("ghp-token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
        ("gh-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
        ("slack-tok", re.compile(r"xox[bap]-")),
    ]


def _own_banned():
    """Backward-compat shim — the module-level _BANNED is the gate."""
    return _BANNED


def lint_source(code, *, size_cap: int = DEFAULT_SIZE_CAP) -> dict:
    """Lint one candidate's source. Returns {'ok': bool, 'errors': [str],
    'lines': int, 'imports': [str]}. Fail-closed: every rule violation is
    an error with the rule explained FOR HER. Accepts str or bytes (the
    desk reader delivers bytes — sandbox.run stdout)."""
    if isinstance(code, bytes):
        code = code.decode("utf-8", errors="replace")
    errors = []
    lines = (code or "").count("\n") + 1
    if lines > size_cap:
        errors.append(f"the file is {lines} lines; the house keeps tools "
                      f"small — {size_cap} is the cap (small tools stay "
                      f"reviewable in a glance)")
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        return {"ok": False,
                "errors": [f"the file does not parse as python: {e}"],
                "lines": lines, "imports": []}

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add((a.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else "")
            if name in BANNED_CALLS:
                errors.append(
                    f"'{name}()' is not allowed in your tools — tools run "
                    f"as house code after review, and the house rules say "
                    f"no dynamic execution, no subprocesses, no raw sockets")
        elif isinstance(node, (ast.Global,)):  # keep her tools side-effect-shy
            errors.append("'global' declarations are not allowed — keep "
                          "each tool self-contained")

    for mod in sorted(imports):
        if mod == "os":
            # os is allowed for os.path-ish use but os.system etc. is
            # caught above; os environ is a house secret surface — banned
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "os"
                        and node.attr in ("environ", "system", "popen")):
                    errors.append(
                        "os.environ / os.system / os.popen are not "
                        "available to your tools — the environment carries "
                        "the house's secrets and the sandbox fence applies")
        if mod not in STDLIB_OK and mod not in ("os",):
            errors.append(
                f"import '{mod}' is outside the v1 allowlist — v1 tools "
                f"use the standard library only (json, re, math, datetime, "
                f"random, textwrap, collections, calendar, pathlib, ...). "
                f"Need more? Say so in the manifest's 'what' — the designer reviews "
                f"the allowlist.")

    patterns = _BANNED if _BANNED else _own_banned()
    for name, rx in patterns:
        m = rx.search(code or "")
        if m:
            errors.append(
                f"banned pattern '{name}' found — tools live in the house "
                f"process and must never carry secrets, tokens, person ids, "
                f"or host paths")
            break  # one banned-pattern line is enough to teach the rule

    return {"ok": not errors, "errors": errors, "lines": lines,
            "imports": sorted(imports)}


def lint_manifest(manifest: dict, house_tool_names: set = None) -> dict:
    """The manifest sanity gate: my_ prefix (namespace rule — her tools
    NEVER override house tools; a deliberate departure from SAKU's
    self-authored tools that override built-ins), args schema present,
    kill-switch name declared."""
    errors = []
    name = str(manifest.get("name") or "")
    if not name:
        errors.append("the manifest has no 'name'")
    elif not name.startswith("my_"):
        errors.append("your tool's name must start with 'my_' — that "
                      "namespace is yours; house tools keep their own "
                      "names and can never be overridden")
    elif house_tool_names and name in house_tool_names:
        errors.append(f"'{name}' collides with a house tool name — pick "
                      f"another name; your tools never override the house")
    if not (manifest.get("what") or "").strip():
        errors.append("the manifest needs 'what' — in your own words, what "
                      "the tool is for (the designer reads this first)")
    if not isinstance(manifest.get("args"), dict):
        errors.append("the manifest needs 'args' — an (possibly empty) "
                      "schema of the tool's parameters")
    ks = str(manifest.get("kill_switch") or "")
    if not ks.startswith("CONTINUA_TOOL_"):
        errors.append("the manifest needs 'kill_switch' starting with "
                      "'CONTINUA_TOOL_' — every tool can be stood down "
                      "individually")
    caps = manifest.get("capabilities") or {}
    if not isinstance(caps, dict):
        errors.append("'capabilities' must be a mapping (files/network)")
    net = caps.get("network") or []
    if not isinstance(net, list):
        errors.append("'capabilities.network' must be a list of domains "
                      "(v1: keep it empty — network comes later, through "
                      "one reviewed door)")
    return {"ok": not errors, "errors": errors}


def lint_candidate(code: str, manifest: dict,
                   house_tool_names: set = None) -> dict:
    """Both gates — the stage-3 verdict the designer's watcher relies on. Fail-
    closed: any error blocks submission with the reason shown TO HER."""
    src = lint_source(code)
    man = lint_manifest(manifest, house_tool_names)
    errors = src["errors"] + man["errors"]
    return {"ok": not errors, "errors": errors,
            "source": src, "manifest": man,
            "report": ("lint OK" if not errors else
                       "lint FAILED:\n- " + "\n- ".join(errors))}