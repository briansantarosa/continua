"""pause.py — the resident rest state (continua.paused).

memorybuild-era addition, 2026-09-29 evening (the designer's call): a resident
whose model cannot yet carry her declared window rests instead of
struggling. A pause is SLEEP, not absence:

- her store, document, ledger, desk are untouched (append-only always);
- arrivals during rest are RECORDED (machinery fact — she sees them
  when she wakes; the 09-08 ruling governs the reply, never the fact
  of an arrival);
- the chat router answers for the MACHINERY once, clearly marked, and
  never speaks for her;
- wake/ritual schedulers skip her;
- fail-open direction: any error reading the flag means she RUNS — a
  broken flag must never trap a resident asleep.

Precedence: env beats yaml (the §ITER convention).
  CONTINUA_PAUSE_<INSTANCE>=1  force paused
  CONTINUA_PAUSE_<INSTANCE>=0  force running
  yaml continua.paused: true   the declared rest state
Run: python3 pause_test.py
"""
import os


def _env_key(instance: str) -> str:
    return "CONTINUA_PAUSE_" + str(instance or "").upper().replace("-", "_")


def is_paused(instance: str, cfg: dict = None, root: str = None) -> bool:
    """True when the resident rests. Late-bound paths (the pollution
    rule): the config path resolves in-body, never at import."""
    env = os.environ.get(_env_key(instance), "")
    if env == "1":
        return True
    if env == "0":
        return False
    if cfg is None:
        try:
            import yaml
            base = root or os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "configs",
                                   f"{instance}.yaml"), encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            return False   # fail-open: she runs
    try:
        return bool(((cfg or {}).get("continua") or {}).get("paused"))
    except Exception:
        return False


def paused_reason(instance: str, cfg: dict = None, root: str = None) -> str:
    """The optional human-readable rest reason (continua.paused_reason)."""
    if cfg is None:
        try:
            import yaml
            base = root or os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "configs",
                                   f"{instance}.yaml"), encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            return ""
    try:
        return str(((cfg or {}).get("continua") or {}).get("paused_reason")
                   or "").strip()
    except Exception:
        return ""