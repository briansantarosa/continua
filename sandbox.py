"""sandbox.py — her desk: uid confinement + the network fence interface.

Phase: autonomy track A (wiki projects/Continua.md, pre-build checklist #1).

Rulings encoded:
  - Identity: everything she executes runs as the dedicated unprivileged
    user `continua` — never as bob. Her processes cannot read the designer's files,
    the Sagent stores, mem0 DBs, or bob's systemd units even when a prompt
    tricks her into trying.
  - Filesystem: her desk (/tmp/continua/sandbox/home/<instance>/) is
    the only persistent writable location (perms + TMPDIR redirected there —
    her temp files land on the data drive, never the root drive). The desk
    persists across restarts: her projects survive; that is continuity.
  - Network (iptables fence, Continua-sandbox-net.service): internet
    HTTP/HTTPS + DNS only; EVERYTHING else denied for uid continua including
    all local/LAN (localhost, the LLM endpoints, control endpoint, Qdrant,
    RFC1918, CGNAT/tailscale, link-local). The fence matches by uid, so it
    holds for every process she runs, however launched.
  - Audit surface: every executed command + exit + duration logged
    append-only (harvest-consistent; sandbox sessions are episodic material).
  - Fail-closed: if confinement cannot be established, nothing runs.

CONFIDEMENT NOTE (honest): bwrap mount-isolation is deferred — Ubuntu 24.04's
apparmor_restrict_unprivileged_userns=1 blocks unprivileged userns, and the
alternatives (system-wide sysctl relaxation; root-run bwrap with mapped uid,
which defeats the uid-matched fence) are worse trades. The identity + fence +
desk guarantees do not depend on bwrap. An apparmor profile for bwrap is the
named hardening path.

Kill switch: CONTINUA_SANDBOX=0 refuses to run anything (fail-closed).
"""

import json
import logging
import os
import subprocess
import time

logger = logging.getLogger("continua.sandbox")

BASE = os.path.dirname(os.path.abspath(__file__))
SBX_USER = os.getenv("CONTINUA_SANDBOX_USER", "continua")
DESK_ROOT = os.getenv("CONTINUA_SANDBOX_HOME", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox", "home"))
LOG_DIR = os.path.join(BASE, "logs", "sandbox")
DEFAULT_TIMEOUT = int(os.getenv("CONTINUA_SANDBOX_TIMEOUT", "120"))


def desk(instance: str) -> str:
    return os.path.join(DESK_ROOT, instance)


def build_argv(instance: str, argv: list) -> list:
    """The confinement for one command: bwrap as her uid (apparmor profile
    continua-bwrap grants the userns; host-uid stays 1001 so the iptables
    fence matches), desk-only writes, private /proc//dev//tmp, DNS via
    public resolvers. Fails closed if the desk is missing."""
    d = desk(instance)
    tmp = os.path.join(d, "tmp")
    for p in (d, tmp):
        probe = subprocess.run(["sudo", "-n", "test", "-d", p],
                               capture_output=True)
        if probe.returncode != 0:
            raise RuntimeError(f"sandbox desk/tmp missing for {instance}: {p}")
    resolv = os.path.join(BASE, "sandbox", "resolv.conf")
    hosts = os.path.join(BASE, "sandbox", "hosts")
    nsswitch = os.path.join(BASE, "sandbox", "etc", "nsswitch.conf")
    return [
        "sudo", "-n", "-u", SBX_USER,
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs",
        "--ro-bind", resolv, "/etc/resolv.conf",
        "--ro-bind", hosts, "/etc/hosts",
        "--ro-bind", nsswitch, "/etc/nsswitch.conf",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", d, "/home/continua",
        "--chdir", "/home/continua",
        "--unshare-pid",
        "--unshare-ipc",
        "--die-with-parent",
        "--setenv", "HOME", "/home/continua",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--",
    ] + argv


# [CONTINUA] 2026-09-29 (memoryplan.md §PAGE, v7 R31, the designer go): the
# the read page is a PERCENT OF THE AGENT'S DECLARED CONTEXT WINDOW,
# not a fleet constant. The fixed 20,000-char page was sized in the
# repetition-crisis era (73K-token prompt budgets); under the 99%-fill plan
# it is ~5% of residentb's budget and turns big-file reads (the ~170K ledger)
# into 9-page expeditions.
#   page = 11% of (declared window tokens x 3.0 chars/token floor density),
#          floored at 20,000 (today's proven size — nobody shrinks) and
#          capped at 40,000.
#   residentb (131,072 tok) -> 40,000; residenta (no declared window yet) -> 20,000
#   — byte-identical to today until her §WIN window lands (and at 32K the
#   floor still holds: 32,768 x 3.0 x 0.11 = 10.8K < 20K).
# The honest-bounds header (residentb's wish #1) is untouched — only the page
# GRANULARITY grows. Kill switch: CONTINUA_SANDBOX_READ_PAGE_CHARS env
# override forces one size for every instance (20000 = exact today).
_READ_PAGE_CACHE: dict = {}


def _page_from_window(total_context_tokens, cfg=None) -> int:
    """§PAGE's pure math: 11% of the declared window expressed in chars at
    the 3.0 chars/token floor density, floored at 20,000 (today's proven
    size — nobody shrinks), capped at 40,000 — §SCALE (R34): the ceiling
    RISES with C (max(40K, 3% C)) for huge windows. None/0 -> 20,000."""
    try:
        total = int(total_context_tokens or 0)
    except (TypeError, ValueError):
        return 20000
    if total <= 0:
        return 20000
    ceiling = 40000
    try:
        import context_scale as _cs
        C = _cs.C_for(cfg or {})
        if C:
            ceiling = max(40000, int(0.03 * C))
    except Exception:
        ceiling = 40000
    return max(20000, min(ceiling, int(total * 3.0 * 0.11)))


def read_page_chars(instance: str) -> int:
    """The read-page size for one instance (memoryplan §PAGE) — the house
    readers' page and the transport headroom (lesstools LT-2/LT-3: the
    named reader retired; the size survives for the house's own readers).
    Env override wins globally; declared window drives the percent;
    floor 20000 / ceiling 40000; unknown window -> 20000. Cached per
    process (a yaml change takes the restart it deserves)."""
    _env = os.getenv("CONTINUA_SANDBOX_READ_PAGE_CHARS", "")
    if _env:
        try:
            return max(1, int(_env))
        except ValueError:
            pass  # garbage env falls through to derivation
    if instance in _READ_PAGE_CACHE:
        return _READ_PAGE_CACHE[instance]
    page = 20000
    try:
        import yaml
        _cfg_path = os.path.join(BASE, 'configs', instance + '.yaml')
        cfg = yaml.safe_load(open(_cfg_path).read())
        total = int((cfg.get('llm') or {}).get('total_context_tokens') or 0)
        if total > 0:
            page = _page_from_window(total, cfg)
    except Exception:
        page = 20000  # fail-open to today's size
    _READ_PAGE_CACHE.clear()
    _READ_PAGE_CACHE[instance] = page
    return page


def run(instance: str, argv: list, timeout: int = DEFAULT_TIMEOUT,
        stdin_text: str = None) -> dict:
    """Run one command inside her sandbox. Fails closed; append-only audit
    log. Returns {ok, exit, stdout, stderr, duration_s, argv}."""
    if os.environ.get("CONTINUA_SANDBOX", "") == "0":
        return {"ok": False, "exit": None, "stdout": "", "stderr":
                "sandbox disabled (CONTINUA_SANDBOX=0)",
                "duration_s": 0, "argv": argv, "refused": True}
    try:
        argv_full = build_argv(instance, argv)
    except Exception as e:
        logger.warning("[Sandbox] refused (fail-closed): %s", e)
        return {"ok": False, "exit": None, "stdout": "",
                "stderr": f"refused: {e}", "duration_s": 0, "argv": argv,
                "refused": True}
    t0 = time.time()
    try:
        proc = subprocess.run(argv_full, capture_output=True, text=True,
                              timeout=timeout,
                              input=stdin_text if stdin_text is not None
                              else None)
        # [CONTINUA] lesstools LT-2 (the designer go 2026-10-05): the clip teaches
        # paging — HEAD-keep with the honest numbers and the trained-pattern
        # hint (sed -n / wc -l / head / tail). The old tail-keep was the
        # Ghost-Message era's guard for the honest-bounds header (that tool
        # that tool retires with LT-3, and head-keep + hint is the trained
        # shape (lesstools.md §7: bash is the canonical tool interface in
        # these models' training data). The cap still follows the
        # window-derived page (§PAGE, v7 R31); the env override wins.
        _transport = int(os.getenv("CONTINUA_SANDBOX_TRANSPORT_CHARS", "") or
                         max(30256, read_page_chars(instance) + 2048))
        _total = len(proc.stdout)
        _out = proc.stdout
        if _total > _transport:
            _out = (proc.stdout[:_transport] +
                    f"\n\n[showing first {_transport} of {_total} chars — "
                    "page with `sed -n '1,400p' <file>`, `tail -c +<byte> "
                    "<file>`, `wc -c`, `head`, `tail`]")
        result = {"ok": proc.returncode == 0, "exit": proc.returncode,
                  "stdout": _out, "stderr":
                  proc.stderr[-8000:], "duration_s": round(time.time() - t0, 2),
                  "argv": argv}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "exit": None, "stdout": "", "stderr":
                  f"timeout after {timeout}s", "duration_s": timeout,
                  "argv": argv, "timeout": True}
    except Exception as e:
        result = {"ok": False, "exit": None, "stdout": "",
                  "stderr": f"sandbox error: {e}", "duration_s":
                  round(time.time() - t0, 2), "argv": argv}
    _audit(instance, result)
    return result


def _audit(instance: str, result: dict) -> None:
    """Append-only audit log — every sandbox execution is on the record."""
    try:
        d = os.path.join(LOG_DIR, instance)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{time.strftime('%Y-%m-%d')}.jsonl")
        entry = {"schema_version": 1,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "instance": instance, "argv": result.get("argv"),
                 "exit": result.get("exit"), "ok": result.get("ok"),
                 "duration_s": result.get("duration_s"),
                 "timeout": result.get("timeout", False)}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[Sandbox] audit write failed", exc_info=True)
