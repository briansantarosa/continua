"""rhyme.py — Phase R (memorybuild2 §6, the designer's go 2026-10-01): the rhyme
channel, "cued, not queried". Nothing surfaces involuntarily because
something in the present rhymes — until now.

Two passes, both on existing infrastructure:
  - LEXICAL: index.search (FTS5 MATCH) over the episodic index — terms
    from the cue text, the OR-for-associative fallback shape.
  - SEMANTIC: one nomic-embed-text embedding of the cue (the same
    /api/embed the degeneracy guard uses) against a cached embedding set
    of recent accepted episodes (cached at recollections/
    <inst>/rhyme_vectors.json, refreshed by the ritual/background;
    revisions immutable so the cache never goes stale mid-cycle).
    Cosine threshold HIGH (default 0.62 — nomic-embed-text's same-topic
    pairs measure 0.44–0.81 raw; only strong rhymes pass).

Cue text: the last user message + HER last utterance (the second cue
channel). Contentless turns (one-word replies, pure acknowledgments)
are skipped by a deterministic filter.

Output shape — a NUDGE, not an injection: at most max_refs (default 2)
episode references, returned to the caller as ranked (job, score, pass)
tuples; the caller feeds them into the EXISTING recollection render
path (cue-weighted ranking ahead of the band shares) — natural memory,
no new render surface, no raw marks.

Anti-loop: the caller passes the thread's already-surfaced jobs; a
rhyme never re-surfaces in the same thread.

Discipline: continua.rhyme yaml {enabled: false DEFAULT OFF until
the designer's flip, threshold: 0.62, recency_days: 14, max_refs: 2}; kill
switch CONTINUA_RHYME=0; every call logs
[Rhyme] cue->ref pass=lexical|semantic score — auditable. Fail-open
everywhere: an outage degrades to no rhyme while the standing backbone
is intact.
Run: python3 rhyme_test.py
"""
import json
import math
import os
import re
import time
from datetime import datetime, timedelta


def enabled(cfg=None):
    """yaml-gated, DEFAULT OFF; the env kill switch wins (§ITER)."""
    if os.environ.get("CONTINUA_RHYME", "") == "0":
        return False
    try:
        return bool((((cfg or {}).get("continua") or {}).get("rhyme")
                     or {}).get("enabled", False))
    except Exception:
        return False


def _cfg_of(cfg):
    r = (((cfg or {}).get("continua") or {}).get("rhyme") or {})
    return {"threshold": float(r.get("threshold", 0.62)),
            "recency_days": int(r.get("recency_days", 14)),
            "max_refs": int(r.get("max_refs", 2))}


_CONTENTLESS = re.compile(
    r"^[\W_]{0,4}$"                       # pure punctuation
    r"|^(ok|okay|yes|no|yeah|yep|nope|thanks|thank you|sure|cool|nice|"
    r"good|great|lol|hm+|hmm+|mhm|wow|ooh|\.\.\.+)[\s.!?,;:\-~]*$",
    re.I | re.ASCII)


def contentless(text):
    """The deterministic filter: one-word replies / pure acknowledgments
    carry no cue worth humming on."""
    t = str(text or "").strip()
    if len(t) < 4:
        return True
    return bool(_CONTENTLESS.match(t.strip()))


def _terms(text):
    stop = {'the', 'and', 'was', 'were', 'that', 'this', 'with', 'have',
            'had', 'for', 'not', 'but', 'her', 'his', 'she', 'him', 'are',
            'our', 'out', 'about', 'into', 'over', 'then', 'them', 'what',
            'when', 'you', 'your', 'yourself', 'myself'}
    return [w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}",
                                          text or "")
            if w.lower() not in stop]


def _lexical(cue, instance, person_id, recency_days, limit):
    """FTS5 pass: the index's OR-for-associative shape, recency-bounded.
    Returns [(job, score)] — score = hits (bm25 rank inverted crudely:
    position). index.search keys on uid/ts/instance — jobs come from the
    caller's episode map via the sources' path ts... SIMPLER: the index
    carries the chronicle rows; the caller maps (instance, ts, content)
    -> job. Here we return the raw rows and let rhyme() match them."""
    try:
        import index as _idx
        after = (datetime.now().astimezone()
                 - timedelta(days=recency_days)).isoformat(
                     timespec="seconds")
        rows = _idx.search(cue, instance=instance, person_id=person_id,
                           after=after, limit=limit * 2)
        return rows
    except Exception:
        return []


def _vectors_path(instance, root=None):
    if root:
        return os.path.join(root, instance, "rhyme_vectors.json")
    import recollections as _rec
    return os.path.join(str(_rec.ROOT), instance, "rhyme_vectors.json")


def load_vectors(instance, root=None):
    try:
        with open(_vectors_path(instance, root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("episodes") else {}
    except Exception:
        return {}


def refresh_vectors(instance, store, root=None, embed_fn=None, limit=200):
    """The ritual/background's refresh: embed the newest accepted
    episodes' texts (revisions immutable — the cache never goes stale
    mid-cycle; rebuilt wholesale). Disposable state."""
    try:
        import recollections as _rec
        latest = {}
        # root=None means "the house default" — read_revisions' own
        # signature resolves it; an explicit None crashes (Path(None),
        # found live at the flip, 2026-10-05)
        revisions = (_rec.read_revisions(instance) if root is None
                     else _rec.read_revisions(instance, root=root))
        for v in revisions:
            if (v.get("instance") == instance
                    and (v.get("review") or {}).get("pass") is True):
                latest[v.get("job")] = v
        eps = sorted(latest.values(),
                     key=lambda v: stamp(v.get("event_start")),
                     reverse=True)[:limit]
        if not eps:
            return 0
        embed_fn = embed_fn or _default_embed_fn()
        if embed_fn is None:
            return 0
        texts = [str(v.get("text") or "")[:1200] for v in eps]
        # chunked: the embedder serves ~0.2s/vector — 200 texts in ONE
        # request overruns the timeout (found live at the flip, 2026-10-05);
        # 32/call keeps every request inside it
        vecs = []
        for i in range(0, len(texts), 32):
            part = embed_fn(texts[i:i + 32])
            if not part or len(part) != len(texts[i:i + 32]):
                return 0
            vecs.extend(part)
        if len(vecs) != len(eps):
            return 0
        out = {"built": datetime.now().astimezone().isoformat(
                   timespec="seconds"),
               "model": "nomic-embed-text",
               "episodes": {}}
        for v, vec in zip(eps, vecs):
            out["episodes"][v["job"]] = {"vec": vec,
                                         "text": str(v.get("text") or "")[:160],
                                         "ts": str(v.get("event_start")
                                                   or "")[:19]}
        tmp = _vectors_path(instance, root) + ".tmp"
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, _vectors_path(instance, root))
        return len(eps)
    except Exception:
        return 0


def _default_embed_fn():
    """The SAME /api/embed call the chat path already uses (the embedder
    host, not the chat host)."""
    url = os.environ.get("CONTINUA_EMBED_URL", "")
    if not url:
        try:
            import yaml
            base = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "configs", "residentb.yaml"),
                      encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            url = str(((cfg.get("llm") or {}).get("base_url")
                       or "")).replace("/v1", "").rstrip("/")
        except Exception:
            return None
    if not url:
        return None

    def _embed(texts):
        import httpx
        r = httpx.post(url.rstrip("/") + "/api/embed",
                       json={"model": os.environ.get(
                           "CONTINUA_EMBED_MODEL", "nomic-embed-text:latest"),
                           "input": list(texts)}, timeout=8.0)
        r.raise_for_status()
        return r.json().get("embeddings") or []
    return _embed


def stamp(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return datetime.min.replace(tzinfo=None)


def _cosine(a, b):
    try:
        num = sum(x * y for x, y in zip(a, b))
        da = math.sqrt(sum(x * x for x in a))
        db = math.sqrt(sum(x * x for x in b))
        return num / (da * db) if da and db else 0.0
    except Exception:
        return 0.0


def _semantic(cue, vectors, threshold, recency_days, limit):
    """Cosine of the cue's vector against the cached episode vectors.
    Returns [(job, score)] above the threshold, best first."""
    try:
        import recollections as _rec
        eps = vectors.get("episodes") or {}
        if not eps:
            return []
        embed_fn = _default_embed_fn()
        if embed_fn is None:
            return []
        vecs = embed_fn([cue])
        if not vecs:
            return []
        cue_vec = vecs[0]
        cutoff = datetime.now().astimezone() - timedelta(days=recency_days)
        scored = []
        for job, rec in eps.items():
            ts = stamp(rec.get("ts"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
            if ts < cutoff:
                continue
            score = _cosine(cue_vec, rec.get("vec") or [])
            if score >= threshold:
                scored.append((job, score))
        scored.sort(key=lambda p: -p[1])
        return scored[:limit]
    except Exception:
        return []


def rhyme(cue_text, instance, cfg=None, person_id=None, surfaced=None,
          episodes=None, root=None, log=None):
    """THE entry: cue -> [(job, score, pass)] at most max_refs, anti-loop
    filtered, determinstic per (cue, store). episodes: {job: value} —
    the accepted-episode map (for lexical matching of job texts)."""
    if not enabled(cfg):
        return []
    r = _cfg_of(cfg)
    surfaced = {str(j) for j in (surfaced or [])}
    cue = str(cue_text or "").strip()
    if contentless(cue):
        _log(log, instance, "-", "skip-contentless", 0.0)
        return []
    out, seen = [], set()
    # LEXICAL: the FTS index rows; match their content to the caller's
    # episode texts (the index keys the chronicle; the store keys jobs)
    try:
        rows = _lexical(cue, instance, person_id, r["recency_days"],
                        r["max_refs"] * 4)
        import recollections as _rec
        for row in rows:
            content = str(row.get("content") or "")
            for job, value in (episodes or {}).items():
                if job in seen:
                    continue
                srcs = " ".join(str(s.get("content") or "")
                                for s in (value.get("sources") or []))
                text = str(value.get("text") or "")
                # the row rhymes with the episode when the episode's own
                # words contain the row's distinctive content (a wake's
                # reply is part of the episode's sources)
                if content[:120] and content[:120].lower() in (
                        srcs.lower() + text.lower()):
                    seen.add(job)
                    out.append({"job": job, "score": 0.5,
                                "pass": "lexical"})
                    break
    except Exception:
        pass
    # SEMANTIC: the cached vectors
    try:
        vectors = load_vectors(instance, root=root)
        if not vectors and (episodes or {}):
            # bounded: a cold cache at a TURN must not stall the turn —
            # the wholesale refresh belongs to the ritual (wired into
            # ritual._nightly 2026-10-05)
            refresh_vectors(instance, None, root=root, limit=24)
            vectors = load_vectors(instance, root=root)
        for job, score in _semantic(cue, vectors, r["threshold"],
                                    r["recency_days"], r["max_refs"] * 2):
            if job in seen or job in surfaced:
                continue
            seen.add(job)
            out.append({"job": job, "score": round(score, 4),
                        "pass": "semantic"})
    except Exception:
        pass
    # anti-loop + cap, best first
    out = [x for x in out if x["job"] not in surfaced]
    out.sort(key=lambda x: (-x["score"], x["job"]))
    out = out[:r["max_refs"]]
    for x in out:
        _log(log, instance, x["job"], x["pass"], x["score"])
    return out


def _log(log, instance, job, kind, score):
    try:
        if log:
            log.info("[Rhyme] cue→%s pass=%s score=%.3f", job[:12], kind,
                     score)
    except Exception:
        pass


def _surfaced_path(instance, person_id, root=None):
    if root:
        return os.path.join(root, instance, "rhyme_surfaced.json")
    import recollections as _rec
    return os.path.join(str(_rec.ROOT), instance,
                        f"rhyme_surfaced_{person_id}.json")


def load_surfaced(instance, person_id, root=None, max_age_h=48):
    """The thread's recently surfaced rhyme jobs (the anti-loop ledger;
    48h window — older surfaces may hum again)."""
    try:
        with open(_surfaced_path(instance, person_id, root),
                  encoding="utf-8") as f:
            d = json.load(f)
        cutoff = time.time() - max_age_h * 3600
        return {j for j, ts in (d.get("jobs") or {}).items()
                if float(ts) >= cutoff}
    except Exception:
        return set()


def mark_surfaced(instance, person_id, jobs, root=None):
    """Record that these rhyme jobs surfaced in this thread (the caller
    does it when the nudge renders)."""
    if not jobs:
        return
    try:
        p = _surfaced_path(instance, person_id, root)
        d = {}
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            pass
        jobs_d = d.get("jobs") or {}
        now = time.time()
        for j in jobs:
            jobs_d[str(j)] = now
        # prune the >48h tail
        cutoff = now - 48 * 3600
        jobs_d = {j: t for j, t in jobs_d.items() if t >= cutoff}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jobs": jobs_d}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def render_nudge(refs, episodes, max_chars=600):
    """The nudge's TEXT: the reference rides INTO the existing
    recollection render path (the caller adds these blocks to the view's
    candidate set, cue-weighted ahead of the band shares). A nudge, not
    an injection: the block is the episode's own stored text."""
    out = []
    for x in (refs or []):
        value = (episodes or {}).get(x["job"])
        if not value:
            continue
        text = " ".join(str(value.get("text") or "").split())[:max_chars]
        out.append((x["job"], text, x.get("pass"), x.get("score")))
    return out