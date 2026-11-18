"""autoresident.py — drop a YAML, the resident comes online (autoresident.md).

2026-10-08, the designer's spec: activation is AUTOMATIC. There is no interactive
dry-run gate. The plan is computed silently, provisioned idempotently, bound
through the bridge's own bind path, verified passively, and REPORTED — the
manifest (logs/autoresident/) is the dry-run's replacement: the plan made
permanent, with outcomes attached.

Pipeline (autoresident.md "The onboarding pipeline"):
  1 watch/stage → 2 validate → 3 plan silently → 4 provision
  → 5 activate (hot-add) → 6 verify passively → 7 report manifest

House rules honored here:
  - Kill switch CONTINUA_AUTORESIDENT=0 — the watcher is not even armed.
  - Nothing human-facing on drop: no test Telegram messages, no
    resident-authored commands as smoke tests. Failures surface in the
    bridge log + the manifest only.
  - Provisioning NEVER deletes and NEVER touches another resident's files.
    chown/chmod run only on the dirs this module just created (the desk is
    skipped entirely if it already exists — residentc's desk contract:
    owner continua, mode 700).
  - A changed config of a LIVE resident is recorded, not hot-reloaded (v1
    scope); restart applies it through the existing discipline.
  - Rollback is by construction: nothing is unbound, nothing is deleted;
    a failed onboarding leaves the pre-drop state intact.
  - Isolation: every function takes explicit base_dir/roots, so tests run
    against a temp root and never touch production stores.

Run: the watcher is armed by bridge.py main(); this module is import-safe.
Tests: python3 autoresident_test.py
"""
import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

import httpx
import yaml

logger = logging.getLogger(__name__)

BASE_DIR = os.getenv("SAGENT_BASE") or os.path.dirname(os.path.abspath(__file__))
INTERVAL_S = float(os.getenv("CONTINUA_AUTORESIDENT_INTERVAL_S", "10"))
SETTLE_HASHES = 2   # identical content hashes on consecutive polls before staging
MIN_AGE_S = 3.0     # ...and the file must be at least this old
INSTANCE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")

# The fleet-level kill switches that can override a candidate's YAML — the
# plan records their ACTUAL values so the manifest reflects what would run.
_ENV_SWITCHES = (
    "CONTINUA_CAPTURE", "CONTINUA_SANDBOX", "CONTINUA_SITTING",
    "CONTINUA_AUTORESIDENT", "CONTINUA_MUTE", "CONTINUA_ALLONTAPE",
    "CONTINUA_DESK_SNAPSHOTS", "CONTINUA_TEXTURE", "CONTINUA_NEWSIE",
    "CONTINUA_MYTOOLS", "CONTINUA_TOOLREVIEW",
)


def kill_switch_off() -> bool:
    return os.getenv("CONTINUA_AUTORESIDENT", "1") == "0"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _desk_root(base_dir: str = BASE_DIR) -> str:
    override = os.getenv("CONTINUA_SANDBOX_HOME")
    return override or os.path.join(base_dir, "sandbox", "home")


def _manifest_dir(base_dir: str = BASE_DIR) -> str:
    return os.path.join(base_dir, "logs", "autoresident")


def _sudo_run(args: List[str]) -> subprocess.CompletedProcess:
    """The house privileged-op convention (sandbox.py / jobs.py style):
    fixed argv, no shell, `sudo -n`, narrow use."""
    return subprocess.run(["sudo", "-n", *args],
                          capture_output=True, timeout=30)


# --- stage 1: watch and stage ------------------------------------------------

def _scan_configs(config_dir: str) -> Dict[str, str]:
    """filename → sha256 for the current top-level configs/*.yaml."""
    out: Dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
        name = os.path.basename(path)
        if name.startswith(".") or not os.path.isfile(path):
            continue
        try:
            out[name] = _hash_file(path)
        except OSError:
            continue
    return out


# --- stage 2: validate (the planner's checks) --------------------------------

def load_others(config_dir: str, exclude_filename: str) -> List[Tuple[str, dict]]:
    """Every OTHER resident's parsed config — the uniqueness baseline."""
    others: List[Tuple[str, dict]] = []
    for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
        name = os.path.basename(path)
        if name == exclude_filename:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            continue
        if isinstance(cfg, dict):
            others.append((name, cfg))
    return others


def validate_candidate(cfg, filename: str,
                       others: List[Tuple[str, dict]]) -> Tuple[List[str], List[str]]:
    """Stage 2 — errors block onboarding, warnings ride the manifest."""
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(cfg, dict):
        return [f"yaml does not parse to a mapping ({type(cfg).__name__})"], warnings

    app_cfg = cfg.get("app") or {}
    inst = app_cfg.get("instance_id")
    if not inst or not isinstance(inst, str):
        errors.append("app.instance_id missing")
    elif not INSTANCE_RE.match(inst):
        errors.append(f"app.instance_id {inst!r} does not match {INSTANCE_RE.pattern}")
    elif filename != f"{inst}.yaml":
        errors.append(f"filename {filename!r} does not agree with instance_id "
                      f"{inst!r} (expected {inst}.yaml)")

    coll = app_cfg.get("collection_name")
    if not coll or not isinstance(coll, str):
        errors.append("app.collection_name missing")

    tg = cfg.get("telegram") or {}
    tok = tg.get("token")
    if not tok or not isinstance(tok, str):
        errors.append("telegram.token missing")
    elif "YOUR_TELEGRAM" in tok:
        errors.append("telegram.token is the placeholder")
    elif not TOKEN_RE.match(tok.strip()):
        errors.append("telegram.token does not look like a bot token (<bot_id>:<secret>)")

    llm = cfg.get("llm") or {}
    if not llm.get("model"):
        errors.append("llm.model missing")
    if not (llm.get("base_url") or llm.get("chat_base_url")):
        errors.append("llm.base_url / llm.chat_base_url both missing")

    tools = cfg.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            errors.append("tools must be a list")
        else:
            for t in tools:
                if not isinstance(t, dict) or not t.get("name"):
                    errors.append(f"tool entry missing name: {t!r}")
                    break

    if not (cfg.get("prompts") or {}).get("identity"):
        warnings.append("prompts.identity missing — she would run with no identity anchor")

    # Uniqueness vs the residents already on the grid (autoresident.md: the
    # qdrant-lock / duplicate-token collision class fails here, loudly).
    for other_name, other_cfg in others:
        o_app = other_cfg.get("app") or {}
        if inst and o_app.get("instance_id") == inst:
            errors.append(f"instance_id {inst!r} already used by {other_name}")
        if coll and o_app.get("collection_name") == coll:
            errors.append(f"collection_name {coll!r} already used by {other_name}")
        o_tok = ((other_cfg.get("telegram") or {}).get("token") or "").strip()
        if tok and o_tok and o_tok == tok.strip():
            errors.append(f"telegram token already used by {other_name}")

    return errors, warnings


# --- stage 3: plan silently ---------------------------------------------------

def _effective_env(instance: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for k in _ENV_SWITCHES:
        v = os.getenv(k)
        env[k] = v if v is not None else "(unset)"
    pk = f"CONTINUA_PAUSE_{instance.upper()}"
    pv = os.getenv(pk)
    env[pk] = pv if pv is not None else "(unset)"
    return env


def build_plan(cfg: dict, filename: str, config_hash: str,
               base_dir: str = BASE_DIR) -> dict:
    """Stage 3 — the effective plan, held for the manifest. No human gate."""
    app_cfg = cfg.get("app") or {}
    cont = cfg.get("continua") or {}
    llm = cfg.get("llm") or {}
    wake = cont.get("wake") or {}
    inst = app_cfg.get("instance_id") or ""
    droot = _desk_root(base_dir)
    return {
        "schema": "autoresident-plan/1",
        "instance": inst,
        "collection": app_cfg.get("collection_name"),
        "config_file": filename,
        "config_hash": config_hash,
        "base_dir": base_dir,
        "provider": {
            "model": llm.get("model"),
            "base_url": llm.get("base_url"),
            "chat_base_url": llm.get("chat_base_url"),
        },
        "desk": {"root": droot, "path": os.path.join(droot, inst), "planned": True},
        "stores": [
            os.path.join(base_dir, "agents", inst),
            os.path.join(base_dir, "wakes", inst),
            os.path.join(base_dir, "wakes", inst, "done"),
            os.path.join(base_dir, "histories", filename.replace(".", "_").replace(":", "_")),
        ],
        "wake": {"enabled": bool(wake.get("enabled")),
                 "interval_min": wake.get("interval_min")},
        "ritual_enabled": bool((cont.get("ritual") or {}).get("enabled")),
        "paused": bool(cont.get("paused")),
        "paused_reason": cont.get("paused_reason"),
        "env": _effective_env(inst),
    }


# --- stage 4: provision idempotently ------------------------------------------

def provision_desk(instance: str, base_dir: str = BASE_DIR) -> dict:
    """The residentc desk contract, mechanized: owner `continua`, mode 700,
    plus tmp/ and job_logs/ (755). Idempotent; NEVER chowns or modifies an
    existing desk — existing state is skipped, not touched."""
    created: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []
    if not INSTANCE_RE.match(instance or ""):
        return {"created": created, "skipped": skipped,
                "errors": [f"invalid instance name: {instance!r}"], "ok": False}
    desk = os.path.join(_desk_root(base_dir), instance)
    tmp = os.path.join(desk, "tmp")
    job_logs = os.path.join(desk, "job_logs")
    try:
        if _sudo_run(["test", "-d", desk]).returncode == 0:
            skipped.append(desk)
            return {"created": created, "skipped": skipped,
                    "errors": errors, "ok": not errors}
        for d in (desk, tmp, job_logs):
            if _sudo_run(["mkdir", "-p", d]).returncode != 0:
                errors.append(f"mkdir failed: {d}")
        if not errors:
            # Owner+mode ONLY on what we just created — never -R over
            # existing data (autoresident.md: no deletion, no rewriting).
            for d, mode in ((desk, "700"), (tmp, "755"), (job_logs, "755")):
                if _sudo_run(["chown", f"continua:continua", d]).returncode != 0:
                    errors.append(f"chown failed: {d}")
                if _sudo_run(["chmod", mode, d]).returncode != 0:
                    errors.append(f"chmod failed: {d}")
            if _sudo_run(["test", "-d", tmp]).returncode != 0:
                errors.append(f"post-check failed: {tmp} missing")
        if not errors:
            created.append(desk)
    except Exception as e:
        errors.append(f"desk provision error: {e}")
    return {"created": created, "skipped": skipped,
            "errors": errors, "ok": not errors}


def provision_stores(instance: str, filename: str,
                     base_dir: str = BASE_DIR) -> dict:
    """The bob-side stores the bridge would lazily create anyway — pre-created
    so 'what did onboarding create' is answerable. Plain makedirs; idempotent."""
    created: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []
    targets = [
        os.path.join(base_dir, "agents", instance),
        os.path.join(base_dir, "wakes", instance),
        os.path.join(base_dir, "wakes", instance, "done"),
        os.path.join(base_dir, "histories",
                     filename.replace(".", "_").replace(":", "_")),
    ]
    for d in targets:
        try:
            if os.path.isdir(d):
                skipped.append(d)
            else:
                os.makedirs(d, exist_ok=True)
                created.append(d)
        except Exception as e:
            errors.append(f"{d}: {e}")
    return {"created": created, "skipped": skipped, "errors": errors}


def provision(plan: dict, base_dir: str = BASE_DIR) -> dict:
    inst = plan.get("instance") or ""
    desk_res = provision_desk(inst, base_dir)
    store_res = provision_stores(inst, plan.get("config_file") or "", base_dir)
    return {
        "desk": desk_res,
        "stores": store_res,
        "errors": desk_res["errors"] + store_res["errors"],
        "ok": not (desk_res["errors"] or store_res["errors"]),
    }


# --- stage 5: activate without interrupting turns ------------------------------

def warm_new_agent(config_path: str, filename: str) -> None:
    """Thread target: the boot warmup convention for ONE hot-added config.
    Deliberately does NOT touch logs/mem0_warm.done (that marker belongs to
    the boot warmup restart.sh waits for)."""
    try:
        import bridge  # lazy — bridge imports this module from main()
        agent = bridge.agent_manager.get_agent(config_path)
        if hasattr(agent, "_init_memory") and getattr(agent, "_mem0_producer", True):
            t0 = time.time()
            agent._init_memory()
            logger.info("[AutoResident] %s mem0 engine warm (%.1fs)",
                        filename, time.time() - t0)
        else:
            logger.info("[AutoResident] %s mem0 engine COLD (producer off / retired injection)",
                        filename)
    except Exception:
        logger.warning("[AutoResident] %s warmup failed (fail-open)",
                       filename, exc_info=True)


async def activate(filename: str, plan: dict, bind_fn=None,
                   base_dir: str = BASE_DIR, warm_fn=None) -> dict:
    """Hot-add through the bridge's own bind path (same arming retries, same
    logs), then arm her wake consumer and start her mem0 warmup. A bind
    failure is REPORTED (raise_on_fail=False) — it can never kill the bridge.
    warm_fn is injectable so tests never spawn real warmup threads."""
    import bridge  # lazy; see warm_new_agent
    if bind_fn is None:
        bind_fn = bridge._bind_config_with_retry
    if warm_fn is None:
        warm_fn = warm_new_agent
    res = {"bound": False, "bot_task": False, "wake_armed": False,
           "warmup": "skipped"}
    config_path = os.path.join(base_dir, "configs", filename)
    task = await bind_fn(config_path, filename, raise_on_fail=False)
    if task is None:
        return res
    res["bound"] = True
    try:
        res["bot_task"] = not task.done()
    except Exception:
        res["bot_task"] = True
    bridge._register_dynamic_task(task)
    if (plan.get("wake") or {}).get("enabled"):
        res["wake_armed"] = bridge._arm_wake_consumer(plan.get("instance")) is not None
    try:
        threading.Thread(target=warm_fn, args=(config_path, filename),
                         daemon=True,
                         name=f"autoresident-warmup:{plan.get('instance')}").start()
        res["warmup"] = "started"
    except Exception:
        res["warmup"] = "failed"
    return res


# --- stage 6: verify passively -------------------------------------------------

def verify(instance: str, filename: str, plan: dict, activate_result: dict,
           base_dir: str = BASE_DIR) -> dict:
    """Passive checks ONLY — no test Telegram messages, no resident-authored
    commands (autoresident.md stage 6). Provider unreachability is warn-only:
    the lab engine may be down without that being an onboarding failure."""
    checks: Dict[str, object] = {}
    dpath = (plan.get("desk") or {}).get("path")
    if dpath:
        if _sudo_run(["test", "-d", dpath]).returncode == 0:
            checks["desk"] = "present"
            st = _sudo_run(["stat", "-c", "%a %U", dpath])
            if st.returncode == 0:
                out = st.stdout.decode() if isinstance(st.stdout, bytes) else str(st.stdout)
                checks["desk_mode_owner"] = out.strip()
        else:
            checks["desk"] = "MISSING (her sandbox tools fail closed until it exists)"
    provider = plan.get("provider") or {}
    url = provider.get("chat_base_url") or provider.get("base_url")
    if url:
        try:
            r = httpx.get(url.rstrip("/") + "/models", timeout=5.0)
            checks["provider"] = f"reachable (http {r.status_code})"
        except Exception as e:
            checks["provider"] = f"unreachable (warn-only: {type(e).__name__})"
    checks["memory_engine"] = (activate_result or {}).get("warmup", "unknown")
    checks["bot_task_alive"] = bool((activate_result or {}).get("bot_task"))
    wake_enabled = bool((plan.get("wake") or {}).get("enabled"))
    if (activate_result or {}).get("wake_armed"):
        checks["wake_consumer"] = "armed"
    elif not wake_enabled:
        checks["wake_consumer"] = "off (wake disabled in her yaml)"
    else:
        checks["wake_consumer"] = "NOT ARMED"
    # Wakes and ritual re-glob configs/*.yaml at fire time (wake.py ~186,
    # ritual.py ~1056) — a bound resident is on the grid with no extra work.
    checks["ritual_timer"] = "system unit (continua-ritual.timer) reads configs at fire time"
    return checks


# --- stage 7: report ------------------------------------------------------------

def _write_manifest(kind: str, name: str, payload: dict,
                    base_dir: str = BASE_DIR) -> Optional[str]:
    try:
        d = _manifest_dir(base_dir)
        os.makedirs(d, exist_ok=True)
        stem = name or "unknown"
        if stem.endswith(".yaml"):
            stem = stem[:-len(".yaml")]
        stem = re.sub(r"[^a-z0-9_-]", "_", stem.lower())
        path = os.path.join(
            d, f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{stem}-{kind}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        logger.warning("[AutoResident] manifest write failed", exc_info=True)
        return None


def _record_changed(name: str, old_hash: str, new_hash: str,
                    base_dir: str = BASE_DIR) -> None:
    """A changed config of a LIVE resident: recorded, never hot-reloaded (v1)."""
    payload = {
        "schema_version": 1, "kind": "changed", "ts": _now(),
        "config_file": name, "old_hash": old_hash, "new_hash": new_hash,
        "note": ("changed config of a live resident is recorded, not "
                 "hot-reloaded (autoresident.md v1 scope) — restart applies it"),
    }
    _write_manifest("changed", name, payload, base_dir)
    logger.warning("[AutoResident] %s CHANGED while live — recorded, not "
                   "hot-reloaded (restart applies it)", name)


# --- the pipeline for one candidate ---------------------------------------------

async def handle_new_config(filename: str, config_hash: str,
                            base_dir: str = BASE_DIR, bind_fn=None,
                            others: Optional[List[Tuple[str, dict]]] = None) -> dict:
    """Stages 2–7 for one settled candidate. Never raises: every failure is a
    failure manifest; the bridge keeps running; nothing is half-bound."""
    t0 = time.time()
    manifest = {
        "schema_version": 1, "kind": "activation", "ts": _now(),
        "config_file": filename, "config_hash": config_hash,
        "base_dir": base_dir, "stages": {},
    }
    path = os.path.join(base_dir, "configs", filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        manifest.update(kind="failure", stage="validate", outcome="failed",
                        errors=[f"yaml parse: {e}"],
                        duration_s=round(time.time() - t0, 2))
        _write_manifest("failure", filename, manifest, base_dir)
        logger.warning("[AutoResident] %s FAILED at validate: yaml parse: %s",
                       filename, e)
        return manifest

    if others is None:
        others = load_others(os.path.join(base_dir, "configs"), filename)
    errors, warnings = validate_candidate(cfg, filename, others)
    manifest["stages"]["validate"] = {"errors": errors, "warnings": warnings}
    if errors:
        manifest.update(kind="failure", stage="validate", outcome="failed",
                        errors=errors, duration_s=round(time.time() - t0, 2))
        _write_manifest("failure", filename, manifest, base_dir)
        logger.warning("[AutoResident] %s FAILED at validate: %s",
                       filename, "; ".join(errors))
        return manifest

    plan = build_plan(cfg, filename, config_hash, base_dir)
    manifest["plan"] = plan
    if plan.get("paused"):
        logger.info("[AutoResident] %s onboards paused:%s — she binds but sleeps",
                    filename, f" ({plan['paused_reason']})" if plan.get("paused_reason") else "")

    prov = provision(plan, base_dir)
    manifest["stages"]["provision"] = prov
    if not prov["ok"]:
        manifest.update(kind="failure", stage="provision", outcome="failed",
                        errors=prov["errors"], duration_s=round(time.time() - t0, 2))
        _write_manifest("failure", filename, manifest, base_dir)
        logger.warning("[AutoResident] %s FAILED at provision: %s",
                       filename, "; ".join(prov["errors"]))
        return manifest

    act = await activate(filename, plan, bind_fn=bind_fn, base_dir=base_dir)
    manifest["stages"]["activate"] = act
    if not act["bound"]:
        manifest.update(kind="failure", stage="activate", outcome="failed",
                        errors=["bind returned no bot task (see bridge log for "
                                "the arming attempts)"],
                        duration_s=round(time.time() - t0, 2))
        _write_manifest("failure", filename, manifest, base_dir)
        logger.warning("[AutoResident] %s FAILED at activate — bind produced no "
                       "bot task (arming retries logged by the bridge)", filename)
        return manifest

    checks = verify(plan.get("instance") or "", filename, plan, act, base_dir)
    manifest["stages"]["verify"] = checks
    manifest["outcome"] = "activated"
    manifest["duration_s"] = round(time.time() - t0, 2)
    _write_manifest("activation", filename, manifest, base_dir)
    logger.info("[AutoResident] %s ACTIVATED in %.1fs — desk:%s wake:%s "
                "provider:%s bot:%s", filename, manifest["duration_s"],
                checks.get("desk"), checks.get("wake_consumer"),
                checks.get("provider"), checks.get("bot_task_alive"))
    return manifest


# --- the watcher loop ------------------------------------------------------------

async def watch_loop(bound_names: List[str], *, interval_s: float = None,
                     bind_fn=None, base_dir: str = BASE_DIR,
                     min_age_s: float = None) -> None:
    """Stage 1 — poll configs/, settle new files, run the pipeline. Seeded
    with the filenames the bridge bound at boot, so boot-time configs are
    never re-processed and only files that appear AFTER boot are candidates."""
    interval = float(interval_s if interval_s is not None else INTERVAL_S)
    min_age = float(min_age_s if min_age_s is not None else MIN_AGE_S)
    cdir = os.path.join(base_dir, "configs")
    known: Dict[str, str] = {}
    for name in bound_names:
        p = os.path.join(cdir, name)
        if os.path.isfile(p):
            try:
                known[name] = _hash_file(p)
            except OSError:
                pass
    logger.info("[AutoResident] watching %s (bound at boot: %s)", cdir,
                ", ".join(sorted(known)) or "none")
    pending: Dict[str, List[str]] = {}   # filename → recent hashes (settle gate)
    failed: Set[Tuple[str, str]] = set() # (filename, hash) already reported failed
    while True:
        try:
            current = _scan_configs(cdir)
            for name, h in sorted(current.items()):
                if name in known:
                    if known[name] == h:
                        pending.pop(name, None)
                        continue
                    # known file, new content → live-resident change
                    old = known[name]
                    known[name] = h
                    pending.pop(name, None)
                    _record_changed(name, old, h, base_dir)
                    continue
                # candidate: settle gate (identical hashes on consecutive
                # polls + file older than min_age) before anything runs
                hist = pending.setdefault(name, [])
                hist.append(h)
                if len(hist) > SETTLE_HASHES:
                    del hist[0]
                try:
                    age_ok = (time.time() - os.path.getmtime(
                        os.path.join(cdir, name))) >= min_age
                except OSError:
                    age_ok = False
                settled = (len(hist) >= SETTLE_HASHES
                           and len(set(hist[-SETTLE_HASHES:])) == 1 and age_ok)
                if not settled:
                    continue
                pending.pop(name, None)
                if (name, h) in failed:
                    continue  # already reported; a NEW hash retries
                manifest = await handle_new_config(
                    name, h, base_dir=base_dir, bind_fn=bind_fn)
                if manifest.get("outcome") == "activated":
                    known[name] = h
                else:
                    failed.add((name, h))
        except Exception:
            logger.warning("[AutoResident] watcher loop error (fail-open)",
                           exc_info=True)
        await asyncio.sleep(interval)
