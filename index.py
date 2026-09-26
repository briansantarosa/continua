"""index.py — the episodic index: SQLite FTS5 over the chronicle mirror.

Phase-2 component of Continua (wiki projects/Continua.md). The chronicle is
the store; this is its search surface — the thing that did not exist before
("full conversation saved AND SEARCHABLE").

Design:
  - One SQLite db (default /tmp/continua/index/chronicle.db), two tables:
      `seen`  (uid TEXT PRIMARY KEY)          — dedup gate, makes updates cheap
      `episodes` (FTS5)                        — the searchable text + metadata
    FTS5 columns: content, reasoning searchable; everything else UNINDEXED
    metadata carried on the row (uid, ts, instance, person_id, role, model).
  - Incremental: `update()` walks the mirror, indexes only records whose uid
    is not yet present; `rebuild()` wipes and reindexes (schema bumps).
  - RECALL MODES (social layer):
      default        — person-filtered (the present conversation has primacy)
      cross_person   — explicit flag; results from OTHER people, never silently
                       mixed: callers render them in the distinct attribution
                       block ("Things you remember from other conversations")
  - ATTRIBUTION CONTRACT: every result carries name+date via people.py —
    `[memory · Alex · 2026-09-06]`. Names come from the roster (her names),
    never invented.
  - ANTI-LOOP GUARD: `exclude_uids` lets the caller suppress fragments already
    surfaced recently (the injection-repetition lesson; core tracks the set).

Kill switch: CONTINUA_INDEX=0 makes search/update no-ops (fail-open).
"""

import logging
import os
import sqlite3

logger = logging.getLogger("continua.index")

SCHEMA_VERSION = 1
DEFAULT_DB = "/tmp/continua/index/chronicle.db"
DEFAULT_ROOT = "/tmp/continua/chronicle"

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes USING fts5(
    uid UNINDEXED, ts UNINDEXED, instance UNINDEXED, person_id UNINDEXED,
    role UNINDEXED, model UNINDEXED,
    content, reasoning
);
"""


def _enabled():
    return os.environ.get("CONTINUA_INDEX", "") != "0"


def _connect(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_DDL)
    return conn


def rebuild(root: str = DEFAULT_ROOT, db_path: str = DEFAULT_DB) -> int:
    """Wipe and reindex everything under the mirror root. Returns count."""
    if not _enabled():
        return 0
    import glob
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()
        total = 0
        for path in sorted(glob.glob(os.path.join(root, "*", "*", "*.jsonl"))):
            total += _index_file(conn, path)
        return total
    finally:
        conn.close()


def update(root: str = DEFAULT_ROOT, db_path: str = DEFAULT_DB) -> int:
    """Incrementally index new mirror records. Cheap to call often."""
    if not _enabled():
        return 0
    import glob
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                           ).fetchone()
        if not row or row[0] != str(SCHEMA_VERSION):
            logger.warning("[Index] schema bump — rebuilding")
            conn.close()
            return rebuild(root, db_path)
        total = 0
        for path in sorted(glob.glob(os.path.join(root, "*", "*", "*.jsonl"))):
            total += _index_file(conn, path)
        return total
    finally:
        conn.close()


def _index_file(conn, path: str) -> int:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import chronicle as ch
    n = 0
    for rec in ch.iter_records(path):
        if conn.execute("SELECT 1 FROM seen WHERE uid=?",
                        (rec.get("uid", ""),)).fetchone():
            continue
        conn.execute("INSERT INTO seen VALUES (?)", (rec.get("uid", ""),))
        conn.execute(
            "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?)",
            (rec.get("uid", ""), rec.get("ts", ""), rec.get("instance", ""),
             rec.get("person_id", ""), rec.get("role", ""),
             rec.get("model") or "", rec.get("content", ""),
             rec.get("reasoning") or ""))
        n += 1
    conn.commit()
    return n


def _match_expr(query: str, mode: str) -> str:
    """Build an FTS5 MATCH string. Terms are quoted (no operator injection);
    AND for precision, OR for associative fallback."""
    terms = [f'"{t.replace(chr(34), "")}"' for t in query.split() if t.strip()]
    if not terms:
        return ""
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(terms)


def search(query: str, instance: str = None, person_id: str = None,
           cross_person: bool = False, exclude_uids=None, limit: int = 10,
           after: str = None, before: str = None, db_path: str = DEFAULT_DB,
           roster=None) -> list:
    """Search the chronicle. Returns attributed results, best-first.

    Default mode filters to person_id (the current conversation);
    cross_person=True EXCLUDES that person — results from other people only
    ("what did others say about this"), which the caller MUST render in the
    distinct cross-person attribution block.
    """
    if not _enabled() or not query.strip():
        return []
    conn = _connect(db_path)
    out = []
    try:
        for mode in ("and", "or"):
            expr = _match_expr(query, mode)
            if not expr:
                continue
            sql = ("SELECT uid, ts, instance, person_id, role, model, content,"
                   " bm25(episodes) AS rank FROM episodes WHERE episodes MATCH ?")
            args = [expr]
            if instance:
                sql += " AND instance = ?"
                args.append(instance)
            if person_id and not cross_person:
                sql += " AND person_id = ?"
                args.append(str(person_id))
            elif person_id and cross_person:
                sql += " AND person_id != ?"
                args.append(str(person_id))
            if after:
                sql += " AND ts >= ?"
                args.append(after)
            if before:
                sql += " AND ts <= ?"
                args.append(before)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit * 3)  # overfetch, then filter + trim
            try:
                rows = conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                logger.warning("[Index] query failed: %s", e)
                return []
            exclude = set(exclude_uids or ())
            for uid, ts, inst, pid, role, model, content, rank in rows:
                if uid in exclude:
                    continue
                out.append(_attributed(uid, ts, inst, pid, role, model,
                                       content, rank, roster))
                if len(out) >= limit:
                    break
            if out:
                break  # AND hit — no OR fallback needed
    finally:
        conn.close()
    return out


def _attributed(uid, ts, instance, person_id, role, model, content, rank,
                roster) -> dict:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import people as pp
    name = pp.name_for(roster or {}, person_id)
    return {
        "uid": uid,
        "ts": ts,
        "date": ts[:10] if ts else "",
        "instance": instance,
        "person_id": person_id,
        "person_name": name,
        "role": role,
        "model": model,
        "content": content,
        "rank": round(rank, 3),
        # THE ATTRIBUTION CONTRACT — every result carries this string:
        "attribution": f"[memory · {name} · {ts[:10] if ts else '?'}]",
    }


def render_attribution(rec: dict) -> str:
    return rec["attribution"]


def stats(db_path: str = DEFAULT_DB) -> dict:
    if not os.path.exists(db_path):
        return {"indexed": 0}
    conn = _connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        per = conn.execute("SELECT instance, COUNT(*) FROM episodes "
                           "GROUP BY instance").fetchall()
        return {"indexed": n,
                "by_instance": {k: v for k, v in per}}
    finally:
        conn.close()
