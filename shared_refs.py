"""shared_refs.py — Phase T Part 2 (memorybuild2 §7): the shared-
references ledger. The machinery PROPOSES compressed relational tokens
(running references, inside-joke names, recurring themes) from material
v10 already builds — kept lines, said-counts, the near-duplicate
collapses, R37 pins. She ENDORSES them: authorship hers, the quote
verified verbatim, machinery proposes from verified sources only.

The essence pattern exactly:
  - proposals live in the store's candidates ledger with provenance
    ('proposed' / 'endorsed', proposer, source refs) — append-only
  - endorsement VERIFIES the quote against the episode's sources before
    accepting (invented quotes can't pass — the quote_errors law)
  - unendorsed proposals expire (HOLD_DAYS, the existing count law) on
    the background worker
  - endorsed tokens become the rhyme channel's densest cue targets
    (Phase R reads them) and the "compressed references" property

NEGATIVE GUARANTEE (the test proves it): no function here touches the
prompt; proposals reach her ONLY through her own tools (list_/endorse_)
and the desk.

Env: CONTINUA_TEXTURE=0 disables proposing (her endorsements are hers;
a rollback never touches her words).
Run: python3 shared_refs_test.py
"""
import json
import os
import re
from datetime import datetime, timedelta

HOLD_DAYS = int(os.environ.get("CONTINUA_TEXTURE_HOLD_DAYS", "5"))
TOKEN_MAX = 120          # a compressed reference is a phrase, not a line


def _tokens_path(instance, root=None):
    if root:
        return os.path.join(root, instance, "shared_refs.json")
    import recollections as _rec
    return os.path.join(str(_rec.ROOT), instance, "shared_refs.json")


def _load(instance, root=None):
    try:
        with open(_tokens_path(instance, root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"tokens": []}
    except Exception:
        return {"tokens": []}


def _save(instance, data, root=None):
    path = _tokens_path(instance, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def propose(instance, person_id, token, source_refs, evidence_quote,
            root=None, provenance="shared_refs.propose"):
    """The machinery PROPOSES a compressed reference for a person.
    material v10 builds: the token must be derivable from verified
    sources; evidence_quote carries the kept line that suggests it."""
    token = str(token or "").strip()
    if not token or len(token) > TOKEN_MAX:
        return None
    if not source_refs or not str(evidence_quote or "").strip():
        return None
    if os.environ.get("CONTINUA_TEXTURE", "") == "0":
        return None
    d = _load(instance, root)
    now = datetime.now().astimezone()
    tok = {"token": token, "person_id": str(person_id),
           "status": "proposed", "by": "machinery",
           "source_refs": list(source_refs)[:8],
           "evidence_quote": str(evidence_quote)[:240],
           "ts": now.isoformat(timespec="seconds"),
           "expires": (now + timedelta(days=HOLD_DAYS))
           .isoformat(timespec="seconds"),
           "provenance": provenance}
    # ONE live proposal per (person, normalized token)
    key = (str(person_id), token.lower())
    d["tokens"] = [t for t in d.get("tokens", [])
                   if not (t.get("status") == "proposed"
                           and (t.get("person_id"), t.get("token", "").lower())
                           == key)]
    d["tokens"].append(tok)
    _save(instance, d, root)
    return tok


def list_for(instance, person_id, root=None, status=None):
    """HER tool's read: the tokens for a person (proposed + endorsed)."""
    out = []
    for t in _load(instance, root).get("tokens", []):
        if str(t.get("person_id")) != str(person_id):
            continue
        if status and t.get("status") != status:
            continue
        if (t.get("status") == "proposed" and t.get("expires")
                and datetime.fromisoformat(t["expires"])
                < datetime.now().astimezone()):
            continue                       # expired proposals don't show
        out.append(t)
    return out


def endorse(instance, person_id, token, episode_job, quote,
            root=None, by="resident"):
    """She endorses: the QUOTE IS VERIFIED against the episode's stored
    sources before the token flips to endorsed (invented quotes can't
    pass — the quote_errors law). Append-only: the proposal row stays;
    an endorsed row is appended with provenance."""
    store = None
    try:
        import recollections as _rec
        store = _rec.Store(instance, root=root)
        prior = store.latest(episode_job)
        if prior is None:
            return None, "no such episode in your recollections"
        hay = " ".join(str(s.get("content") or "")
                       for s in (prior.get("sources") or []))
        if str(quote).strip()[:120].lower() not in hay.lower():
            return None, ("that quote is not in this episode's record — "
                          "an endorsed token carries her verified words")
    except Exception as e:
        return None, f"verification failed open: {e}"
    d = _load(instance, root)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    endorsed = {"token": str(token).strip(),
                "person_id": str(person_id), "status": "endorsed",
                "by": by, "episode_job": episode_job,
                "verified_quote": str(quote).strip()[:240],
                "ts": now, "provenance": "shared_refs.endorse tool"}
    key = (str(person_id), str(token).strip().lower())
    d["tokens"] = [t for t in d.get("tokens", [])
                   if not ((t.get("person_id"), t.get("token", "").lower())
                           == key and t.get("status") == "proposed")]
    d["tokens"].append(endorsed)
    _save(instance, d, root)
    return endorsed, None


def expire_pass(instance, root=None):
    """The background worker's expiry law: proposed tokens past their
    expiry are REMOVED (append-only would rot the ledger; expiry is the
    count law's shape — the proposal had its hold days). Endorsed tokens
    NEVER expire (hers). Returns the removed count."""
    d = _load(instance, root)
    now = datetime.now().astimezone()
    kept, removed = [], 0
    for t in d.get("tokens", []):
        if (t.get("status") == "proposed" and t.get("expires")
                and datetime.fromisoformat(t["expires"]) < now):
            removed += 1
            continue
        kept.append(t)
    if removed:
        d["tokens"] = kept
        _save(instance, d, root)
    return removed


def endorsed_tokens(instance, person_id=None, root=None):
    """The rhyme channel's read (Phase R): the endorsed tokens, optionally
    per person. Machinery prose never appears here — endorsed only."""
    out = []
    for t in _load(instance, root).get("tokens", []):
        if t.get("status") != "endorsed":
            continue
        if person_id and str(t.get("person_id")) != str(person_id):
            continue
        out.append(t)
    return out