"""tools_runtime.py — house execution for her promoted tools (toolplan.md,
stage 5). the designer approved in principle 2026-09-26.

The fence made this design: exploratory code runs in her sandbox (fenced,
uid-confined); PROMOTED code runs as house code — the designer's review IS the
trust boundary that replaces the sandbox. Everything here is behind three
switches: CONTINUA_MYTOOLS=0 (the whole namespace),
CONTINUA_TOOLREVIEW=0 (the gate — nothing new gets in),
CONTINUA_TOOL_<NAME>=0 (per tool, declared in her manifest).

Contract for a promoted tool (v1 — files only, stdlib only):
    def run(args, house):
        ...return a string (or anything str()-able)...
`house` is the injected helper namespace: read/write confined to HER desk
(via sandbox.run — the sanctioned path; the tool runs as house but its
FILE reach stays her desk, v1 = 'files: desk' capability), plus newsie
v1.1 later through the same shape. Errors fail-open: an exception returns
an honest error message to her — never crashes the chat path.
"""

import json
import logging
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
MYTOOLS_DIR = os.path.join(BASE, "mytools")
AUDIT_LOG = os.path.join(MYTOOLS_DIR, "audit.log")
KILL_SWITCH_ENV = "CONTINUA_MYTOOLS"
DEFAULT_TIMEOUT = 30
OUTPUT_CAP = 3000

logger = logging.getLogger("continua.tools_runtime")


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "1") != "0"


def _manifests() -> dict:
    """{name: manifest} from mytools/*.yaml — read at call time (a newly
    promoted tool appears without a bridge restart; the loader is the
    config_tools injector in core, this is the runtime's fresh view)."""
    import glob
    import yaml
    out = {}
    for path in sorted(glob.glob(os.path.join(MYTOOLS_DIR, "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as f:
                m = yaml.safe_load(f) or {}
            if m.get("name"):
                out[m["name"]] = m
        except Exception as e:
            logger.warning("[MyTools] manifest %s unreadable (fail-open): %s",
                           path, e)
    return out


def load_tool_defs() -> list:
    """OpenAI-style tool definitions for every promoted tool (the loader
    half; core appends these to config_tools)."""
    defs = []
    for name, m in _manifests().items():
        params = {}
        for pname, pspec in (m.get("args") or {}).items():
            if not isinstance(pspec, dict):
                pspec = {"type": "string"}
            pd = {"type": str(pspec.get("type", "string")),
                  "description": str(pspec.get("desc")
                                     or pspec.get("description")
                                     or pname)}
            if pspec.get("required"):
                pd["required"] = True
            params[pname] = pd
        defs.append({
            "name": name,
            "description": (str(m.get("what") or name))[:300],
            "parameters": params,
            "my_tool": True,  # marker so the config_tools consumer knows
        })
    return defs


def _audit(entry: dict) -> None:
    """Append-only audit log (plan: 'everything auditable'). Fail-open."""
    try:
        os.makedirs(MYTOOLS_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("[MyTools] audit write failed (fail-open): %s", e)


class HouseHelpers:
    """Injected into run(args, house). v1 = desk-confined file access via
    sandbox.run (the sanctioned path; her tool's file reach stays her desk
    even though the tool runs as house code). v1.1 = house_fetch — ONE
    reviewed network door: the manifest declares the domains at review,
    the helper enforces them at runtime (allowlisted egress, auditable —
    the fence's spirit without bwrap for house-run code)."""

    def __init__(self, instance: str, manifest: dict = None):
        self.instance = instance
        m = manifest or {}
        caps = m.get("capabilities") or {}
        # exact-host allowlist (scheme-agnostic); subdomains NOT implied
        self.allowed_hosts = {h.lower().strip()
                              for h in (caps.get("network") or [])
                              if isinstance(h, str) and h.strip()}

    def read(self, path: str) -> str:
        import sandbox as sb
        r = sb.run(self.instance, ["/bin/sh", "-c",
                                   f"cat '{path}'"], timeout=15)
        if not (r.get("ok") and r.get("exit") == 0):
            raise OSError(f"unreadable: {path} "
                          f"({(r.get('stderr') or '')[:100]})")
        return r.get("stdout") or ""

    def write(self, path: str, text: str) -> bool:
        import sandbox as sb
        r = sb.run(self.instance, ["/bin/sh", "-c",
                                   f"mkdir -p \"$(dirname '{path}')\" && "
                                   f"cat > '{path}'"],
                   timeout=15, stdin_text=text)
        return bool(r.get("ok") and r.get("exit") == 0)

    def fetch(self, url: str, timeout: float = 15.0) -> str:
        """The one network door (v1.1): GET text from a manifest-approved
        domain. No raw sockets, ever; the allowlist is enforced HERE at
        runtime, not trusted to the tool's own code."""
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host not in self.allowed_hosts:
            raise PermissionError(
                f"'{host}' is not on the reviewed domain allowlist "
                f"{sorted(self.allowed_hosts)} — ask the designer to approve it "
                f"in the manifest")
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=True) as s:
            r = s.get(url, headers={"User-Agent": "continua-mytool/1.0"})
        r.raise_for_status()
        return r.text


def _load_module(name: str):
    """Import mytools/<name>.py as a module. Reviewed house code — the
    import itself is the trust boundary made real."""
    import importlib.util
    path = os.path.join(MYTOOLS_DIR, name + ".py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(f"mytool_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_tool(instance: str, name: str, arguments: dict) -> str:
    """The dispatch half. Kill switches (master + per-tool), timeout, cap,
    audit, fail-open errors — the plan's stage-5 mechanics."""
    if not enabled():
        return "[Tool error: the my_ tools namespace is switched off " \
               "(CONTINUA_MYTOOLS=0)]"
    m = _manifests().get(name)
    if not m:
        return f"[Tool error: {name} is not an installed tool]"
    ks = m.get("kill_switch") or ""
    if ks and os.environ.get(ks, "") == "0":
        return f"[Tool error: {name} is switched off ({ks}=0)]"
    args = arguments if isinstance(arguments, dict) else {}
    house = HouseHelpers(instance, m)
    result_box = {}
    t0 = time.time()

    def _target():
        try:
            mod = _load_module(name)
            if mod is None or not hasattr(mod, "run"):
                result_box["error"] = (f"{name} has no run(args, house) — "
                                       "the house contract")
                return
            result_box["out"] = mod.run(args, house)
        except Exception as e:
            result_box["error"] = f"{type(e).__name__}: {e}"

    # A thread bounds WALL-CLOCK politely (a hung tool returns an honest
    # timeout instead of eating her turn; the runaway thread dies with the
    # process — acceptable for reviewed stdlib-only code, v1).
    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout=int(m.get("timeout", DEFAULT_TIMEOUT)))
    elapsed_ms = int((time.time() - t0) * 1000)
    if th.is_alive():
        _audit({"ts": _now(), "instance": instance, "tool": name,
                "outcome": "timeout", "ms": elapsed_ms})
        return (f"[Tool error: {name} ran past its {m.get('timeout', DEFAULT_TIMEOUT)}s "
                "budget — the house stopped waiting; the tool may still be "
                "finishing in the background]")
    _audit({"ts": _now(), "instance": instance, "tool": name,
            "outcome": "error" if "error" in result_box else "ok",
            "ms": elapsed_ms})
    if "error" in result_box:
        # fail-open: an honest error message to her, never a crash
        logger.warning("[MyTools] %s failed: %s", name, result_box["error"])
        return f"[Your tool {name} failed: {result_box['error']}]"
    out = str(result_box.get("out") or "(no output)")
    return out[:OUTPUT_CAP]


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")