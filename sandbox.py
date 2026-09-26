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
DESK_ROOT = os.getenv("CONTINUA_SANDBOX_HOME", "/tmp/continua/sandbox/home")
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
        result = {"ok": proc.returncode == 0, "exit": proc.returncode,
                  "stdout": proc.stdout[-20000:], "stderr":
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
