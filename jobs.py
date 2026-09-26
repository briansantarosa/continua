"""jobs.py — supervised background jobs: she can run things that outlive her
turn (autonomy phase, 2026-09-07; systemd-run redesign after the incident).

The wake rhythm becomes: start things, tend them across wakes, collect
results. Pattern:
  job_start(command)    — launches as uid continua in a transient systemd
                          unit (cgroup-capped: CPU weight + memory.max)
  job_status(job_id)    — alive? deadline? command?
  job_output(job_id)    — tail of the log
  job_stop(job_id)      — systemctl stop (precise, cgroup-scoped)

INCIDENT NOTE (2026-09-07 20:59, wiki: skills/System-wide-SIGTERM-from-
Continua-Job-Kill-2026-09-07): the previous implementation killed jobs with
`sudo kill -TERM -<pgid>`; procps kill parses a bare -<number> as a signal
spec and degraded to kill(-1, SIGTERM) — every process on the host except
PID 1 was signaled (sshd + 5 SSH sessions + ~20 daemons died). This redesign
removes signal-based kills entirely: stop = `systemctl stop <unit>`, which
kills exactly the unit's cgroup. Fail-safe: an unknown/missing unit is
refused, never guessed.

Safety per the house rulings:
  - uid confinement: jobs run as `continua` (fence-matched: internet
    80/443/DNS only; desk-only writes; Bob's files invisible).
  - cgroup caps via systemd properties (CPUWeight, MemoryMax) — a runaway
    job cannot starve the box or the model cards.
  - Jobs survive turn ends and bridge restarts (transient units are
    systemd-managed); a deadline bounds lifetime (default 24h).
  - Every transition logged append-only to logs/jobs/<instance>/.
"""

import json
import logging
import os
import pwd
import re
import subprocess
import time
from datetime import datetime, timedelta

logger = logging.getLogger("continua.jobs")

BASE = os.path.dirname(os.path.abspath(__file__))
SBX_USER = os.getenv("CONTINUA_SANDBOX_USER", "continua")
DESK_ROOT = os.getenv("CONTINUA_SANDBOX_HOME", "/tmp/continua/sandbox/home")
JOBS_DIR = os.path.join(BASE, "jobs")
LOG_DIR = os.path.join(BASE, "logs", "jobs")
JOB_MAX_HOURS = float(os.getenv("CONTINUA_JOB_MAX_HOURS", "24"))
JOB_MAX_CONCURRENT = int(os.getenv("CONTINUA_JOB_MAX_CONCURRENT", "3"))
CPU_WEIGHT = int(os.getenv("CONTINUA_CGROUP_CPU", "50"))
MEM_MAX = os.getenv("CONTINUA_CGROUP_MEM", "2G")


def _desk(instance: str) -> str:
    return os.path.join(DESK_ROOT, instance)


def _state_path(instance: str) -> str:
    return os.path.join(JOBS_DIR, f"{instance}.state.json")


def _load_state(instance: str) -> dict:
    try:
        with open(_state_path(instance), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}}


def _save_state(instance: str, state: dict) -> None:
    os.makedirs(JOBS_DIR, exist_ok=True)
    tmp = _state_path(instance) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, _state_path(instance))


def _audit(instance: str, entry: dict) -> None:
    """Append-only job audit — the watch surface for truncations/cut-offs."""
    try:
        d = os.path.join(LOG_DIR, instance)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{datetime.now():%Y-%m-%d}.jsonl")
        entry = dict(entry, schema_version=1, instance=instance)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[Jobs] audit write failed", exc_info=True)


def _uid_gid():
    p = pwd.getpwnam(SBX_USER)
    return p.pw_uid, p.pw_gid


def _unit_active(unit: str) -> bool:
    """'activating' counts as alive — a unit is real the moment systemd
    accepts it (the r4-race in the concurrency test proved this)."""
    if not unit:
        return False
    r = subprocess.run(["systemctl", "is-active", unit],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() in ("active", "activating")


def reap(instance: str) -> list:
    """Enforce the deadline: jobs past JOB_MAX_HOURS are stopped (logged).
    Also clears finished entries older than a day. Called on interactions."""
    state = _load_state(instance)
    now = datetime.now()
    stopped = []
    for jid, j in list(state["jobs"].items()):
        try:
            if datetime.fromisoformat(j["deadline_dt"]) <= now:
                unit = j.get("unit")
                if unit:
                    subprocess.run(["sudo", "-n",
                                    "/usr/local/bin/continua-unit-kill", unit],
                                   capture_output=True, timeout=15)
                _audit(instance, {"event": "reaped", "job_id": jid,
                                  "reason": "deadline reached (24h)"})
                state["jobs"].pop(jid, None)
                stopped.append(jid)
        except (ValueError, TypeError, KeyError):
            pass
    if stopped:
        _save_state(instance, state)
    return stopped


def start(instance: str, command: str, name: str = "") -> dict:
    """Launch a supervised background job as a transient systemd unit.
    Fails closed."""
    if os.environ.get("CONTINUA_SANDBOX", "") == "0":
        return {"ok": False, "error": "sandbox disabled (CONTINUA_SANDBOX=0)"}
    command = (command or "").strip()
    if not command or re.search(r"\bsudo\b", command):
        return {"ok": False, "error": "empty command or sudo not allowed in jobs"}
    reap(instance)
    state = _load_state(instance)
    alive = [jid for jid, j in state["jobs"].items()
             if _unit_active(j.get("unit", ""))]
    if len(alive) >= JOB_MAX_CONCURRENT:
        return {"ok": False,
                "error": (f"max {JOB_MAX_CONCURRENT} concurrent jobs — "
                          f"running: {len(alive)}. job_stop one first.")}
    desk = _desk(instance)
    uid, gid = _uid_gid()
    stamp = time.time_ns()
    job_id = f"job_{stamp}"
    unit = f"continua-job-{stamp}.service"
    log_rel = os.path.join("job_logs", f"{job_id}.log")
    log_abs = os.path.join(desk, log_rel)
    # the desk is 700 continua — the log setup goes through sudo (her space)
    setup = subprocess.run(
        ["sudo", "-n", "sh", "-c",
         f"mkdir -p '{os.path.dirname(log_abs)}' && "
         f"touch '{log_abs}' && chown {uid}:{gid} '{log_abs}'"],
        capture_output=True, timeout=10)
    if setup.returncode != 0:
        return {"ok": False,
                "error": f"log setup failed: {setup.stderr.decode()[:150]}"}
    argv = [
        "sudo", "-n", "systemd-run",
        f"--uid={SBX_USER}", f"--gid={gid}",
        f"--unit={unit}",
        f"--property=CPUWeight={CPU_WEIGHT}",
        f"--property=MemoryMax={MEM_MAX}",
        f"--property=WorkingDirectory={desk}",
        "/bin/sh", "-c",
        f"{command} >> '{log_abs}' 2>&1",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {"ok": False,
                    "error": f"systemd-run failed: {proc.stderr[:200]}"}
    except Exception as e:
        _audit(instance, {"event": "start_failed", "command": command[:200],
                          "error": str(e)[:200]})
        return {"ok": False, "error": str(e)[:300]}
    entry = {
        "job_id": job_id, "unit": unit, "name": name or command[:60],
        "command": command,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "deadline": (datetime.now() + timedelta(hours=JOB_MAX_HOURS)
                     ).strftime("%Y-%m-%dT%H:%M:%S"),
        "log_rel": log_rel,
    }
    state["jobs"][job_id] = entry
    _save_state(instance, state)
    _audit(instance, {"event": "started", "job_id": job_id, "unit": unit,
                      "command": command[:200], "log": log_rel,
                      "deadline": entry["deadline"],
                      "caps": {"cpu_weight": CPU_WEIGHT, "mem_max": MEM_MAX}})
    return {"ok": True, "job_id": job_id, "unit": unit, "log": log_rel,
            "deadline": entry["deadline"],
            "note": (f"running as a systemd transient unit (max {JOB_MAX_HOURS}h, "
                     "CPU/mem capped). job_status / job_output to tend it.")}


def status(instance: str, job_id: str = None) -> dict:
    reap(instance)
    state = _load_state(instance)
    jobs = state["jobs"]
    if job_id:
        jobs = {k: v for k, v in jobs.items() if k == job_id}
        if not jobs:
            return {"ok": False, "error": f"unknown job {job_id}"}
    out = {}
    for jid, j in jobs.items():
        unit = j.get("unit", "")
        out[jid] = {"job_id": jid, "name": j.get("name"),
                    "alive": _unit_active(unit), "unit": unit,
                    "started": j.get("started"), "deadline": j.get("deadline"),
                    "log": j.get("log_rel"), "command": j.get("command", "")[:120]}
    return {"ok": True, "jobs": out}


def output(instance: str, job_id: str, lines: int = 40) -> dict:
    state = _load_state(instance)
    j = state["jobs"].get(job_id)
    if not j:
        return {"ok": False, "error": f"unknown job {job_id}"}
    log_abs = os.path.join(_desk(instance), j["log_rel"])
    # her desk is 700 — the log is read through sudo (her space is hers)
    r = subprocess.run(["sudo", "-n", "tail", f"-n {lines}", log_abs],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"ok": True, "job_id": job_id, "lines": [],
                "note": "no output yet"}
    return {"ok": True, "job_id": job_id,
            "alive": _unit_active(j.get("unit", "")),
            "lines": [l for l in r.stdout.splitlines()]}


def stop(instance: str, job_id: str) -> dict:
    state = _load_state(instance)
    j = state["jobs"].get(job_id)
    if not j:
        return {"ok": False, "error": f"unknown job {job_id}"}
    unit = j.get("unit", "")
    if not unit:
        return {"ok": False, "error": "job has no unit recorded"}
    r = subprocess.run(["sudo", "-n", "/usr/local/bin/continua-unit-kill",
                        unit], capture_output=True, text=True, timeout=20)
    ok = r.returncode == 0
    _audit(instance, {"event": "stopped" if ok else "stop_failed",
                      "job_id": job_id, "unit": unit,
                      "error": None if ok else r.stderr[:150]})
    if ok:
        state["jobs"].pop(job_id, None)
        _save_state(instance, state)
    return {"ok": ok}
