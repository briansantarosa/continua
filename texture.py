"""texture.py — Phase T Part 1 (memorybuild2 §7): relationship books as
a DESK PRACTICE, not a pipeline.

The 09-16 ruling retired the pipeline-authored book (no compulsory
autobiography). This re-homes the retired books.py contract on her DESK
— only she writes; the machinery's ONE role is pull-not-push source
offers (the newsie house law): when she is on a person's page (or in
the sitting's tending pass), the machinery OFFERS that person's recent
kept episodes / kept lines / anchors as source material. Nothing
compels, nothing is injected, nothing is written for her.

Layout on the desk (uid `continua`, mode 700 — all writes via
sandbox.run()):
  books/<key>.md              her current manuscript (key: 'self' or a
                              person_id — the retired contract's keys)
  books/.versions/<key>/      dated snapshots (7-day retention, the
                              the designer 2026-09-07 ruling; origin kept forever)

API:
  source_offer(instance, person_id, root=None)  — the machinery's offer:
      {"text": "...", "expires": ts} written to
      texture/offers/<person_id>.json (ONE per person; a newer offer
      replaces it — self-extinguishing)
  offers_for(instance, person_ids)              — read offers (her tools
      / the sitting's tending pass surface them; NEVER the prompt)
  save_manuscript(instance, key, text)          — her write (her shell
      at her desk); archives the previous as a dated version + prunes
      7-day-old versions (origin kept)
  read_manuscript / versions                    — her reads

NEGATIVE GUARANTEE (the test proves it): no function here touches
core's prompt assembly; no offer or manuscript reaches the prompt.
The whole layer is desk-side.

Env: CONTINUA_TEXTURE=0 disables the OFFER machinery only (her pages
are hers; a rollback never touches her words). Per-part yaml:
memory.texture.offers (default true).
Run: python3 texture_test.py
"""
import glob
import json
import os
import re
from datetime import datetime, timedelta

RETENTION_DAYS = int(os.environ.get("CONTINUA_BOOK_RETENTION_DAYS", "7"))
_OFFER_TTL_H = 72          # an offer self-extinguishes in 3 days
_OFFER_MAX = 2400          # the offer's own cap (source material, not a book)


def _desk(instance, root=None):
    """Her desk root — sandbox.home/<instance> (the same base sandbox.run
    uses; the caller passes root for tests)."""
    if root:
        return root
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "sandbox", "home", instance)


def _books_dir(instance, root=None):
    return os.path.join(_desk(instance, root), "books")


def _versions_dir(instance, key, root=None):
    return os.path.join(_books_dir(instance, root), ".versions", key)


def _offers_dir(instance, root=None):
    return os.path.join(_desk(instance, root), "texture", "offers")


def offers_enabled(cfg=None):
    """The offer machinery's gate: CONTINUA_TEXTURE=0 wins; per-part yaml
    memory.texture.offers (default true)."""
    if os.environ.get("CONTINUA_TEXTURE", "") == "0":
        return False
    try:
        return bool((((cfg or {}).get("memory") or {}).get("texture")
                     or {}).get("offers", True))
    except Exception:
        return False


def _kept_material(instance, person_id, root=None, limit=6):
    """That person's recent kept episodes + kept lines + anchors, from
    material v10 already builds: the ritual marks (kept meanings) and
    the store's anchors. Read-only; empty on any failure."""
    out = {"kept": [], "anchors": []}
    try:
        base = root or os.path.dirname(os.path.abspath(__file__))
        marks_dir = os.path.join(base, "ritual", "marks", instance)
        for path in sorted(glob.glob(os.path.join(marks_dir, "*.jsonl")),
                           reverse=True)[:14]:
            for line in open(path, encoding="utf-8"):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("kept") is not True or not row.get("meaning"):
                    continue
                # the marks carry `persons` (a list of person ids) + uids —
                # not a scalar person_id (verified against the live files)
                persons = [str(x) for x in (row.get("persons") or [])]
                if person_id and persons and str(person_id) not in persons:
                    continue
                out["kept"].append({"ts": row.get("ts"),
                                    "meaning": str(row["meaning"])[:160]})
                if len(out["kept"]) >= limit:
                    break
            if len(out["kept"]) >= limit:
                break
    except Exception:
        pass
    try:
        import recollections as _rec
        anchors, _ = _rec.read_guards(instance, root=root)
        out["anchors"] = sorted(anchors)[:limit]
    except Exception:
        pass
    return out


def source_offer(instance, person_id, root=None, cfg=None, names=None):
    """The machinery's ONE role: prepare and OFFER source material for a
    person's book — never inject, never write, never push. Writes ONE
    offer file per person (a newer offer replaces it — self-extinguishing
    after _OFFER_TTL_H). Returns the offer dict, or None when gated."""
    if not offers_enabled(cfg):
        return None
    try:
        material = _kept_material(instance, person_id, root=root)
        if not material["kept"] and not material["anchors"]:
            return None
        name = (names or {}).get(person_id, person_id)
        lines = [f"Source material for a possible {name} page — offered "
                 f"to your desk, not injected into anything.",
                 f"(nothing compels; ignore it and it expires)"]
        if material["kept"]:
            lines.append("Your kept lines about them, newest first:")
            for k in material["kept"][:6]:
                lines.append(f'  - [{(k.get("ts") or "")[:10]}] '
                             f'"{k["meaning"]}"')
        if material["anchors"]:
            lines.append(f"Anchored episodes: {len(material['anchors'])} "
                         f"(recall_my_experience finds them)")
        text = "\n".join(lines)
        if len(text) > _OFFER_MAX:
            text = text[:_OFFER_MAX] + "\n  [offered material trimmed]"
        offer = {"person_id": str(person_id), "name": name,
                 "ts": datetime.now().astimezone().isoformat(
                     timespec="seconds"),
                 "expires": (datetime.now().astimezone()
                             + timedelta(hours=_OFFER_TTL_H))
                 .isoformat(timespec="seconds"),
                 "text": text}
        d = _offers_dir(instance, root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{person_id}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(offer, f, ensure_ascii=False, indent=1)
        return offer
    except Exception:
        return None


def offers_for(instance, person_ids, root=None):
    """The offers SHE could read (her tools / the tending pass surface
    them) — live ones only; expired ones are deleted on sight."""
    out = {}
    d = _offers_dir(instance, root)
    now = datetime.now().astimezone()
    for pid in person_ids:
        p = os.path.join(d, f"{pid}.json")
        try:
            with open(p, encoding="utf-8") as f:
                offer = json.load(f)
            if datetime.fromisoformat(offer.get("expires")) < now:
                os.remove(p)
                continue
            out[pid] = offer
        except Exception:
            continue
    return out


def dismiss_offer(instance, person_id, root=None):
    """She declines: the offer file is removed (her hand, her call)."""
    try:
        os.remove(os.path.join(_offers_dir(instance, root),
                               f"{person_id}.json"))
        return True
    except Exception:
        return False


# --- the manuscript: her writes only ---------------------------------------

def read_manuscript(instance, key, root=None):
    p = os.path.join(_books_dir(instance, root), f"{key}.md")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def save_manuscript(instance, key, text, root=None):
    """HER write (her shell, at her desk — lesstools LT-3). Archives the previous manuscript as
    a dated version; prunes versions older than RETENTION_DAYS; the ORIGIN
    version is kept forever (the the designer 2026-09-07 ruling). Returns the path."""
    if not str(text or "").strip():
        raise ValueError("an empty manuscript is not a save — use delete "
                         "if you mean it (deletes are deliberate)")
    bdir = _books_dir(instance, root)
    os.makedirs(bdir, exist_ok=True)
    p = os.path.join(bdir, f"{key}.md")
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            prior = f.read()
        if prior != text:
            vdir = _versions_dir(instance, key, root)
            os.makedirs(vdir, exist_ok=True)
            with open(os.path.join(vdir, f"{today}.md"), "w",
                      encoding="utf-8") as f:
                f.write(prior)
    with open(p, "w", encoding="utf-8") as f:
        f.write(str(text))
    _prune_versions(instance, key, root=root)
    return p


def _prune_versions(instance, key, root=None):
    """the designer's retention ruling: versions older than RETENTION_DAYS are gone
    (she had 7 nights to reclaim them); the ORIGIN version (the oldest)
    is kept forever."""
    vdir = _versions_dir(instance, key, root)
    if not os.path.isdir(vdir):
        return
    dates = sorted(os.path.basename(p)[:-3]
                   for p in glob.glob(os.path.join(vdir, "*.md")))
    if not dates:
        return
    origin = dates[0]
    cutoff = (datetime.now().astimezone()
              - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for d in dates[1:]:
        if d < cutoff and d != origin:
            try:
                os.remove(os.path.join(vdir, f"{d}.md"))
            except Exception:
                continue


def versions(instance, key, root=None):
    vdir = _versions_dir(instance, key, root)
    return sorted(os.path.basename(p)[:-3]
                  for p in glob.glob(os.path.join(vdir, "*.md")))


def version_asof(instance, key, date, root=None):
    """The retired contract's read: the manuscript as it stood on `date`.
    Versions are snapshots of the PRIOR text at save time, so the state on
    `date` = the newest version saved strictly before-or-on the NEXT save
    after `date`; past the last save, the state is the current manuscript.
    The ORIGIN (the first version) covers dates before the first snapshot."""
    vs = versions(instance, key, root=root)
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    if date >= today:
        return read_manuscript(instance, key, root=root)
    # the state on `date` = the version saved ON the last save date <= date
    # (the snapshot taken that day carries the pre-save text — the state
    # during that day was the PRIOR text, i.e. the snapshot of that day)
    candidates = [d for d in vs if d <= date]
    if not candidates:
        return None
    vdir = _versions_dir(instance, key, root)
    with open(os.path.join(vdir, f"{candidates[-1]}.md"),
              encoding="utf-8") as f:
        return f.read()