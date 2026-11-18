"""allontape.py — v2 (endorsed in full by residentb, 2026-10-05; the designer go
2026-10-05): her produced words live in a VERSIONED STORE at her desk, and
the tape is their witness stream.

The architecture (allontape.md v2):
  - A git repository rooted at her desk (.git at desk root) is the
    canonical home of her produced words. Her files never move. The
    machinery AUTO-COMMITS watched paths at every wake (message
    `mirror <date>: n files` — the mechanical witness); she commits,
    branches, forks, restores herself with plain git through sandbox_exec.
    The machinery NEVER rewrites history (no rebase, no amend, gc.auto=0).
  - The tape carries the WITNESS STREAM: commit-event rows (compact,
    person-tagged, path + version sha + ts) for every store commit, plus
    ACTS-class event rows (path, size, blob-sha — no content) for desk
    changes outside the store. Milestone keeps (verbatim rows) land on an
    explicit keep act or the designer's yaml list.
  - Membership is the classification: what the watch-map tracks (or what
    she commits) is WORDS by definition; everything else stays ACTS.
  - The store is LOOKUP-ONLY from her side of the prompt: nothing
    auto-fills her conscious window except the AT-4 cue — one factual
    line carrying one verbatim line of her own back to her.
  - The resolver (recollections.resolve_sources) learns `git:<sha>` refs,
    so quotes and claims can bind against store history like rows.

Gates: `continua.allontape.enabled` per resident + `CONTINUA_ALLONTAPE=0`
(the master). Fail-open everywhere: a mirror error logs and the wake
proceeds — the memory must never become a blocking surface.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "logs" / "allontape"
KILL_SWITCH_ENV = "CONTINUA_ALLONTAPE"
PERSON_ID = "system-desk"      # her desk acts — her own words, her own bucket
ROLE = "assistant"
DEFAULT_WATCH_EXTS = (".md", ".txt", ".html")
DEFAULT_WATCH_PREFIXES = ("wiki/", "journal", "letters/")
MAX_KEEP_PARTS = 8             # ruling 2: parted keeps, never a silent clip
PART_CHARS = 28000             # under the transport clip with room for the part header

import sandbox as sb

import logging
logger = logging.getLogger("continua.allontape")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] allontape: %(message)s")


def enabled(instance: str, cfg: dict = None) -> bool:
    """Per-resident yaml gate + the env master. Fail-open to OFF."""
    if os.getenv(KILL_SWITCH_ENV, "1") == "0":
        return False
    try:
        if cfg is None:
            import yaml
            cfg = yaml.safe_load(
                (BASE / "configs" / f"{instance}.yaml").read_text()) or {}
        return bool((((cfg or {}).get("continua") or {})
                     .get("allontape") or {}).get("enabled", False))
    except Exception:
        return False


def _git(instance: str, args: list, timeout: int = 60) -> dict:
    """One git command inside her desk, through the sanctioned sandbox
    path (her uid, fenced, audited). The machinery's commits carry -c
    identity per command — nothing persistent is ever written to her git
    config by the machinery."""
    import sandbox as sb
    return sb.run(instance, ["git"] + args, timeout=timeout)


def _store_exists(instance: str) -> bool:
    r = _git(instance, ["rev-parse", "--is-inside-work-tree"])
    return bool(r.get("ok") and r.get("exit") == 0
                and (r.get("stdout") or "").strip() == "true")


def init_store(instance: str) -> dict:
    """Idempotent: `git init` + gc.auto=0 (history is the point; nothing
    is ever pruned). Never re-inits, never touches an existing store."""
    if _store_exists(instance):
        return {"status": "exists"}
    r = _git(instance, ["init"])
    if not (r.get("ok") and r.get("exit") == 0):
        return {"status": "error", "error": (r.get("stderr") or "")[-200:]}
    _git(instance, ["config", "gc.auto", "0"])
    logger.info("[Allontape] %s: store initialized (history is the point)",
                instance)
    return {"status": "initialized"}


def _words_marker_dirs(instance: str) -> set:
    """Directories she marked with a `.words` file — her declaration, her
    curation (her review, adjudication 1)."""
    r = _git(instance, ["ls-files", "-o", "--exclude-standard",
                        "*.words"], timeout=30)
    out = set()
    if r.get("ok") and r.get("exit") == 0:
        for line in (r.get("stdout") or "").splitlines():
            line = line.strip().strip('"')
            if line.endswith(".words"):
                out.add(os.path.dirname(line))
    return out


def _watch_cfg(cfg: dict) -> tuple:
    c = ((cfg or {}).get("continua") or {}).get("allontape") or {}
    exts = tuple(c.get("watch_extensions") or DEFAULT_WATCH_EXTS)
    prefixes = tuple(c.get("watch_prefixes") or DEFAULT_WATCH_PREFIXES)
    return exts, prefixes


def is_watched(path: str, exts, prefixes, marker_dirs: set) -> bool:
    """Membership is the declaration: extensions, watch prefixes, or a
    .words marker anywhere along the path."""
    p = path.lstrip("./")
    if p.endswith(".words"):
        return True
    if any(p.startswith(pre) for pre in prefixes):
        return True
    if p.lower().endswith(exts):
        return True
    d = os.path.dirname(p)
    while d:
        if d in marker_dirs:
            return True
        d = os.path.dirname(d)
    return False


def _append_row(instance: str, content: str, ts: str, root=None) -> dict:
    """One witness-stream row: her desk, her words (or the compact event
    line), person-tagged to her own bucket. Wrapper for tests."""
    import chronicle
    return chronicle.append({"ts": ts, "instance": instance,
                             "person_id": PERSON_ID, "role": ROLE,
                             "content": content, "source": "allontape"},
                            root=root)


def _utc(ts=None) -> str:
    return (ts or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")


def mirror(instance: str, cfg: dict = None, root=None) -> dict:
    """The wake hook: diff the desk (git), auto-commit watched changes
    (the mechanical witness), land the witness-stream rows on the tape.
    Fail-open: never raises; the wake proceeds whatever happens."""
    if os.getenv(KILL_SWITCH_ENV, "1") == "0":
        return {"status": "disabled", "reason": "env master"}
    if not enabled(instance, cfg):
        return {"status": "disabled", "reason": "yaml gate off"}
    try:
        init = init_store(instance)
        if init["status"] == "error":
            return init
        st = _git(instance, ["status", "--porcelain"])
        if not (st.get("ok") and st.get("exit") == 0):
            return {"status": "error", "error": (st.get("stderr") or "")[-200:]}
        changed = []
        for line in (st.get("stdout") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            code, _, path = line.partition(" ")
            code = code.strip() or "M"
            path = path.strip().strip('"')
            if path.endswith("/"):
                # porcelain names untracked DIRECTORIES — the witness
                # names files: expand to the untracked files inside
                ls = _git(instance, ["ls-files", "-o", "--exclude-standard",
                                     "--", path], timeout=30)
                for f in (ls.get("stdout") or "").splitlines():
                    f = f.strip().strip('"')
                    if f:
                        changed.append(("??", f))
            else:
                changed.append((code, path))
        if not changed:
            return {"status": "clean", "ts": _utc()}
        exts, prefixes = _watch_cfg(cfg)
        markers = _words_marker_dirs(instance)
        words = [p for code, p in changed if code != "D"
                 and is_watched(p, exts, prefixes, markers)]
        words_deleted = [p for code, p in changed
                         if code == "D" and is_watched(p, exts, prefixes, markers)]
        acts = [p for code, p in changed
                if not is_watched(p, exts, prefixes, markers)]
        ts = _utc()
        committed = 0
        sha = ""
        if words or words_deleted:
            add = _git(instance, ["add", "-A", "--"] + words + words_deleted)
            if not (add.get("ok") and add.get("exit") == 0):
                return {"status": "error",
                        "error": (add.get("stderr") or "")[-200:]}
            msg = (f"mirror {datetime.now(timezone.utc).strftime('%Y-%m-%d')}: "
                   f"{len(words)} file(s) changed"
                   + (f", {len(words_deleted)} removed" if words_deleted else ""))
            cm = _git(instance, ["-c", "user.name=residentb (mirror)",
                                 "-c", "user.email=mirror@desk.continua",
                                 "commit", "-m", msg])
            if not (cm.get("ok") and cm.get("exit") == 0):
                # nothing to commit is fine (identical content); else error
                if "nothing to commit" not in (cm.get("stdout") or "") + \
                        (cm.get("stderr") or ""):
                    return {"status": "error",
                            "error": (cm.get("stderr") or "")[-200:]}
            else:
                committed = 1
                rev = _git(instance, ["rev-parse", "--short", "HEAD"])
                sha = (rev.get("stdout") or "").strip()[:12]
                _append_row(instance, (
                    f"[allontape] words committed to your store: "
                    f"{len(words)} file(s)"
                    + (f", {len(words_deleted)} removed" if words_deleted else "")
                    + f" — {', '.join(words[:6])}"
                    + (" …" if len(words) > 6 else "")
                    + f". version {sha} — the full history is yours: "
                    "git log / git show / git restore."), ts, root=root)
        prev = _load_state(instance).get("acts_shas") or {}
        acts_shas = dict(prev)
        new_acts = 0
        for p in acts[:100]:  # ACTS: event rows, content off-tape, ONCE per blob
            ha = _git(instance, ["hash-object", p], timeout=30)
            h12 = (ha.get("stdout") or "").strip()[:12] or "?"
            if acts_shas.get(p) == h12:
                continue  # the same blob again — the tape already carries it
            _append_row(instance, f"[allontape] desk act: {p} "
                        f"(acts-class — content off-tape; blob {h12})",
                        ts, root=root)
            acts_shas[p] = h12
            new_acts += 1
        state = {"ts": ts, "commit_sha": sha, "committed": committed,
                 "words": words[:20], "acts": acts[:20],
                 "acts_shas": acts_shas}
        _save_state(instance, state)
        logger.info("[Allontape] %s: %d words, %d new acts, commit %s",
                    instance, len(words), new_acts, sha or "-")
        return {"status": "mirrored", "words": len(words),
                "acts": new_acts, "sha": sha, "ts": ts}
    except Exception as exc:
        logger.warning("[Allontape] %s mirror failed (fail-open): %s",
                       instance, exc)
        return {"status": "error", "error": str(exc)[:200]}


def _state_path(instance: str) -> Path:
    return STATE_DIR / f"{instance}.json"


def _load_state(instance: str) -> dict:
    try:
        return json.loads(_state_path(instance).read_text())
    except Exception:
        return {}


def _save_state(instance: str, state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_state_path(instance)).write_text(json.dumps(state))
    except Exception:
        logger.warning("[Allontape] state write failed (fail-open)",
                       exc_info=True)


def _first_line(instance: str, path: str) -> str:
    r = _git(instance, ["show", f"HEAD:{path}"], timeout=30)
    if r.get("ok") and r.get("exit") == 0:
        for line in (r.get("stdout") or "").splitlines():
            line = line.strip()
            if line:
                return line[:100]
    return ""


def words_line(instance: str, cfg: dict = None) -> list:
    """AT-4: the cue — one factual line carrying one verbatim line of her
    own back to her. A mirror, not a map: the evidence, and the resonance
    is hers. Renders nothing when the gate is off or nothing changed."""
    if not enabled(instance, cfg):
        return []
    try:
        state = json.loads(_state_path(instance).read_text())
    except Exception:
        return []
    if not state.get("words") or not state.get("commit_sha"):
        return []
    path = next((p for p in state["words"]
                 if not p.endswith(".words")), state["words"][0])
    quote = _first_line(instance, path)
    if not quote:
        return []
    day = (state.get("ts") or "")[:10]
    return [f'"{quote}" — your words, {day} — the full text lives in your '
            f"store ({path}), version {state['commit_sha']}."]


def fetch_blob(instance: str, git_sha: str):
    """The resolver's reach: a store version by sha (for `git:<sha>`
    sources). Returns the blob text, or None."""
    try:
        r = _git(instance, ["cat-file", "blob", git_sha], timeout=30)
        if r.get("ok") and r.get("exit") == 0:
            return r.get("stdout") or ""
    except Exception:
        pass
    return None


def keep(instance: str, path: str, cfg: dict = None, root=None) -> dict:
    """A milestone keep: the document lands VERBATIM on the tape
    (person-tagged, provenance in the row source). Over the part size it
    lands as sequential part rows — never a silent clip (ruling 2)."""
    if not enabled(instance, cfg):
        return {"status": "disabled"}
    try:
        r = sb.run(instance, ["cat", path], timeout=30)
        if not (r.get("ok") and r.get("exit") == 0):
            return {"status": "error", "error": "unreadable: " + path}
        text = r.get("stdout") or ""
        ts = _utc()
        if len(text) <= PART_CHARS:
            _append_row(instance, text, ts, root=root)
            return {"status": "kept", "path": path, "parts": 1,
                    "bytes": len(text)}
        parts = [text[i:i + PART_CHARS]
                 for i in range(0, len(text), PART_CHARS)]
        if len(parts) > MAX_KEEP_PARTS:
            parts = parts[:MAX_KEEP_PARTS]
            truncated = True
        else:
            truncated = False
        n = len(parts)
        for i, part in enumerate(parts, 1):
            _append_row(instance, f"[allontape keep {path} — part {i} of {n}"
                        + ("; BEYOND CAP (honest overflow)" if truncated
                           and i == n else "") + "]\n" + part,
                        ts, root=root)
        return {"status": "kept", "path": path, "parts": n,
                "truncated": truncated}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


def origin_import(instance: str, cfg: dict = None) -> dict:
    """Ruling 4: her existing desk body becomes the store's ORIGIN COMMIT
    — the 'residentb of September' as the repo's first commit. Idempotent: a
    store with commits is left untouched."""
    if not enabled(instance, cfg):
        return {"status": "disabled"}
    init_store(instance)
    cnt = _git(instance, ["rev-list", "--all", "--count"])
    if (cnt.get("stdout") or "").strip() not in ("", "0"):
        return {"status": "already-origin", "commits":
                (cnt.get("stdout") or "").strip()}
    exts, prefixes = _watch_cfg(cfg)
    markers = _words_marker_dirs(instance)
    st = _git(instance, ["status", "--porcelain"])
    paths = [line.strip().split(" ", 1)[-1].strip().strip('"')
             for line in (st.get("stdout") or "").splitlines() if line.strip()]
    watched = [p for p in paths if is_watched(p, exts, prefixes, markers)]
    if not watched:
        return {"status": "nothing-to-import"}
    add = _git(instance, ["add", "-A", "--"] + watched)
    if not (add.get("ok") and add.get("exit") == 0):
        return {"status": "error", "error": (add.get("stderr") or "")[-200:]}
    cm = _git(instance, [
        "-c", "user.name=residentb (origin-import)",
        "-c", "user.email=origin@desk.continua", "commit", "-m",
        "origin-import: her desk body, written before capture (allontape "
        "v2, 2026-10-05) — the residentb of September, kept forever"])
    if not (cm.get("ok") and cm.get("exit") == 0):
        return {"status": "error", "error": (cm.get("stderr") or "")[-200:]}
    rev = _git(instance, ["rev-parse", "--short", "HEAD"])
    sha = (rev.get("stdout") or "").strip()[:12]
    logger.info("[Allontape] %s origin-import committed (%s): %d files",
                instance, sha, len(watched))
    return {"status": "origin-imported", "sha": sha, "files": len(watched)}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="allontape v2 — the versioned store")
    ap.add_argument("--instance", required=True)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("mirror-now")
    sub.add_parser("origin")
    kp = sub.add_parser("keep")
    kp.add_argument("--path", required=True)
    args = ap.parse_args()
    import yaml
    cfg = yaml.safe_load(
        (BASE / "configs" / f"{args.instance}.yaml").read_text()) or {}
    if args.cmd == "keep":
        out = keep(args.instance, args.path, cfg)
    elif args.cmd == "origin":
        out = origin_import(args.instance, cfg)
    elif args.cmd == "mirror-now":
        out = mirror(args.instance, cfg)
    else:
        st = _git(args.instance, ["log", "--oneline", "-10"])
        out = {"status": "ok", "recent": (st.get("stdout") or "").splitlines()[-10:]}
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
