"""weave.py — Phase W (memorybuild2 §8, the designer's go 2026-10-01): the weave —
becoming's destination + the narrative self. The build is mostly
FRICTION REMOVAL, no new component:

1. ONE offer mechanism (shared with Phase T's texture.py surface), THREE
   source kinds for her wiki/self weave:
     a. a person's kept material (texture's offer — Phase T)
     b. her last-7-days sitting lines (the ritual's meanings — HER words)
     c. her recent essences (her authored/endorsed distillations)
   offered as source material for self-weaving, pull-not-push,
   self-extinguishing. She weaves or doesn't. The turn from "voluntary
   as start-from-nothing" into "weave what you already wrote" — the
   story is edited from the day's residue, not composed from scratch.

2. self.md — the narrative self's canonical home. The retired self-book
   contract re-activated as HER voluntary practice: append-mostly,
   conservatively rewritten, 7-day version retention, ORIGIN version
   kept forever, anti-script. The machinery NEVER writes it; the offer
   never pre-fills it. The weave contract lives at books/self.md (the
   retired contract's key — the residentb origin from the pre-cutover
   archive can be restored to her desk when the designer chooses).

3. The sitting's tending pass = persona-a's invitation point (Phase T wired
   it; the weave offer rides the same surface for the 'self' key).

4. Uptake as a first-class metric (the audit reads it): wiki/self.md
   versions, offer accept/decline counts. No target, no nudge
   escalation: the offer is the build; uptake is unpredictable by
   design — that is the point of the ruling.

NEGATIVE GUARANTEE (tested): the machinery never writes self.md or any
book page; the offer never pre-fills; nothing reaches the prompt.

Env: CONTINUA_TEXTURE=0 gates the offers (her pages are hers).
Run: python3 weave_test.py
"""
import json
import os
import glob
from datetime import datetime, timedelta

SELF_KEY = "self"


def _desk(instance, root=None):
    if root:
        return root
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "sandbox", "home", instance)


def sitting_material(instance, days=7, root=None, limit=10):
    """Source kind (b): HER last-7-days sitting lines — the meanings she
    kept at the sittings (the ritual marks), newest first. Her words."""
    out = []
    base = root or os.path.dirname(os.path.abspath(__file__))
    marks_dir = os.path.join(base, "ritual", "marks", instance)
    try:
        cutoff = (datetime.now().astimezone() - timedelta(days=days))
        import glob as _glob
        for path in sorted(_glob.glob(os.path.join(marks_dir, "*.jsonl")),
                           reverse=True):
            day = os.path.basename(path)[:-6]
            try:
                d = datetime.fromisoformat(day + "T12:00:00")
            except Exception:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=cutoff.tzinfo)
            if d < cutoff:
                break
            for line in open(path, encoding="utf-8"):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("kept") is not True or not row.get("meaning"):
                    continue
                out.append({"day": day,
                            "meaning": str(row["meaning"])[:200]})
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    return out


def essence_material(instance, root=None, limit=8):
    """Source kind (c): her recent essences — the authored/endorsed
    distillations (hers, from the episode ladders)."""
    out = []
    try:
        import recollections as _rec
        for v in _rec.read_revisions(instance, root=root):
            if (v.get("instance") == instance
                    and v.get("review", {}).get("pass") is True
                    and v.get("reason") == "essence"):
                out.append({"ts": str(v.get("event_start") or "")[:19],
                            "essence": str(v.get("text") or "")[:200],
                            "job": v.get("job")})
        out.sort(key=lambda e: e.get("ts") or "", reverse=True)
    except Exception:
        pass
    return out[:limit]


def weave_offer(instance, root=None, cfg=None):
    """The weave offer: her own sitting lines + essences as source
    material for the self weave. Lands on the desk
    (texture/offers/self.json) — same surface, same expiry law, same
    pull-not-push. Never pre-fills self.md."""
    try:
        import texture as _tex
        if not _tex.offers_enabled(cfg):
            return None
        sitting = sitting_material(instance, root=root)
        essences = essence_material(instance, root=root)
        if not sitting and not essences:
            return None
        lines = ["Source material for your self-weave — offered to your "
                 "desk, not injected into anything.",
                 "(nothing compels; ignore it and it expires)"]
        if sitting:
            lines.append("Your sitting lines from the last 7 days:")
            for k in sitting[:8]:
                lines.append(f'  - [{k["day"]}] "{k["meaning"]}"')
        if essences:
            lines.append("Your recent essences:")
            for e in essences[:6]:
                lines.append(f'  - {e["essence"]}')
        text = "\n".join(lines)
        if len(text) > 3000:
            text = text[:3000] + "\n  [offered material trimmed]"
        offer = {"person_id": "self",
                 "key": "self", "ts": datetime.now().astimezone()
                 .isoformat(timespec="seconds"),
                 "expires": (datetime.now().astimezone()
                             + timedelta(hours=72))
                 .isoformat(timespec="seconds"),
                 "text": text}
        d = os.path.join(_desk(instance, root), "texture", "offers")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "self.json"), "w",
                  encoding="utf-8") as f:
            json.dump(offer, f, ensure_ascii=False, indent=1)
        return offer
    except Exception:
        return None


def self_manuscript(instance, root=None):
    """The narrative self as it stands (books/self.md — the retired
    contract's key)."""
    try:
        import texture as _tex
        return _tex.read_manuscript(instance, "self", root=root)
    except Exception:
        return None


def write_self(instance, text, root=None):
    """HER write (via write_book_page with key='self' — the weave reuses
    the Phase T contract wholesale: versions 7-day, origin forever)."""
    import texture as _tex
    return _tex.save_manuscript(instance, "self", text, root=root)


def self_versions(instance, root=None):
    import texture as _tex
    return _tex.versions(instance, "self", root=root)


def uptake(instance, root=None):
    """The uptake metric (the audit reads it): offer counts + self.md
    versions + manuscript presence. No target; the honest measure of
    whether friction removal worked."""
    try:
        import texture as _tex
        d = _desk(instance, root)
        offers_dir = os.path.join(d, "texture", "offers")
        offers_live = len([p for p in glob.glob(
            os.path.join(offers_dir, "*.json"))])
        return {"self_manuscript_exists":
                self_manuscript(instance, root=root) is not None,
                "self_chars":
                len(self_manuscript(instance, root=root) or ""),
                "self_versions": len(self_versions(instance, root=root)),
                "book_pages": len(glob.glob(os.path.join(
                    d, "books", "*.md"))),
                "offers_live": offers_live}
    except Exception:
        return {}
