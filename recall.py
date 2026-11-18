"""recall.py — the two recall surfaces (phase 5).

Ordinary recall: the conversational contract — person-filtered (the present
conversation has primacy), anti-loop exclusions, bounded.

Deep recall: the deliberate, effortful surface — the "therapy mode" the plan
described. Cross-person, no anti-loop exclusions, higher limits: access to
the verbatim hippocampus (the full archive, including faded/degenerate rows).
Humans have latent memory that surfaces under effort; this is hers.

Both results carry the attribution contract. The ordinary/deep distinction
is surfaced in every result ("mode") so the calling core can frame it
honestly ("I remember…" vs "I went looking, and found…").

Kill switch: CONTINUA_INDEX=0 disables both (index-level).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index as ix
import people as pp


def recall(query: str, instance: str, person_id: str, roster: dict = None,
           exclude_uids=None, limit: int = 6, db_path: str = ix.DEFAULT_DB) -> list:
    """Ordinary recall — what surfaces in conversation. Person-filtered,
    anti-looped, bounded. This is the lossy, human-shaped surface."""
    return [dict(r, mode="recall") for r in ix.search(
        query, instance=instance, person_id=person_id,
        exclude_uids=exclude_uids, limit=limit, roster=roster,
        db_path=db_path)]


def deep_recall(query: str, instance: str, person_id: str = None,
                roster: dict = None, limit: int = 20,
                db_path: str = ix.DEFAULT_DB) -> list:
    """Deep recall — deliberate, effortful, complete. Cross-person, no
    anti-loop exclusions, deep limit. What she finds when she goes looking
    on purpose (the latent-memory surface)."""
    return [dict(r, mode="deep") for r in ix.search(
        query, instance=instance, person_id=person_id, cross_person=True,
        limit=limit, roster=roster, db_path=db_path)]
