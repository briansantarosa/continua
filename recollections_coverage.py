"""Read-only recollection coverage accounting (memory plan chunk 1).

No Store construction, enqueue, model calls, or prompt changes. SQLite is read
in one mode=ro transaction; source files are bounded to their initial size.
This is NOT an atomic snapshot across capture and SQLite: changing files are
reported and unmatched refs are not automatically called lost experience.

CLI: python recollections_coverage.py --instance all [--json] [--at ISO_TIME]
Redirect JSON to a private file outside git for a baseline. Reports contain
counts and source directory names, never conversation prose or source refs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
import json
from pathlib import Path
import re
import sqlite3

from recollections import CHRONICLE, ROOT, digest, now, plan_episodes, source_kind, source_record, stamp


POLICY = {
    "scanner": "recollections-v3 quality hold; prompt boundaries retained, single-row oversize exception; eligibility is not acceptance",
    "bucket_seconds": 1800,
    "close_margin_seconds": 300,
    "episode_cap_chars": 16000,
    "episode_gap_seconds": 1800,
    "units": "Unicode content characters, not bytes/tokens/experience",
    "duplicates": "same source_record ref; repeated text alone is NOT a duplicate",
    "accepted": "ref present in sources of accepted-job revision with matching resident and review.pass=true; not proof every detail survived",
    "partitions": "unique refs partition by planner disposition and separately by store state; diagnostic counters overlap",
    "length_cut": "excluded from enqueue eligibility (chunk 2); counted separately, never silently",
    "context_rows": "monologue prompt rows are machinery context, never enqueued",
    "version_note": "v2 replaced the v1 bucket model (chunk-1 baseline JSON preserved the v1 picture); chunk-2 comparison uses this policy",
    "consistency": "one SQLite read transaction; per-file bounded reads, not a global capture snapshot",
}


def measure(records):
    records = list(records)
    return {"refs": len(records), "chars": sum(len(s["content"]) for s in records)}


def store_snapshot(instance, root):
    """Never instantiate Store: even its status path creates/chmods a database."""
    path = Path(root) / instance / "shadow.sqlite3"
    result = {"state": "missing", "jobs": {}, "coverage_refs": 0,
              "accepted_evidence_refs": 0, "revision_rows": 0,
              "candidate_rows": 0, "candidate_error_rows": 0, "issues": []}
    coverage, accepted = {}, set()
    if not path.exists():
        return result, coverage, accepted
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            statuses = dict(db.execute("SELECT id,status FROM jobs"))
            result["jobs"] = dict(sorted(Counter(statuses.values()).items()))
            coverage = {ref: statuses.get(job, "orphan_coverage")
                        for ref, job in db.execute("SELECT ref,job FROM coverage")}
            for job, body in db.execute("SELECT job,body FROM revisions"):
                result["revision_rows"] += 1
                try:
                    value = json.loads(body)
                    if (statuses.get(job) == "accepted" and value["instance"] == instance
                            and value["job"] == job and value["review"]["pass"] is True):
                        refs = value["sources"]
                        if not isinstance(refs, list) or not refs or any(
                            s["instance"] != instance or not isinstance(s["ref"], str)
                            for s in refs
                        ):
                            raise ValueError("bad sources")
                        accepted.update(s["ref"] for s in refs)
                except (ValueError, TypeError, KeyError, AttributeError):
                    result["issues"].append("invalid_revision")
            for (body,) in db.execute("SELECT body FROM candidates"):
                result["candidate_rows"] += 1
                try:
                    value = json.loads(body)
                    if value.get("errors"):
                        result["candidate_error_rows"] += 1
                except (ValueError, TypeError, AttributeError):
                    result["issues"].append("invalid_candidate")
            result.update(state="readable", coverage_refs=len(coverage),
                          accepted_evidence_refs=len(accepted))
    except (sqlite3.Error, OSError):
        # No zero-valued success on unavailable storage; no private exception text.
        result.update(state="unavailable", issues=["database_read_failed"])
        coverage, accepted = {}, set()
    return result, coverage, accepted


def inspect_resident(instance, source_root=CHRONICLE, root=ROOT, at=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", instance):
        raise ValueError("invalid instance")
    at = stamp(at or now())
    db, coverage, accepted = store_snapshot(instance, root)
    directory = Path(source_root) / instance
    report = {"instance": instance, "at": at.isoformat(), "policy": POLICY,
              "store": db, "source_state": "readable", "sources": {}, "issues": []}
    all_refs = set()
    if not directory.is_dir():
        report["source_state"] = "missing"
    try:
        paths = sorted(directory.glob("*/*.jsonl"))
        # Include empty/unknown directories so they do not silently disappear.
        people = sorted(p.name for p in directory.iterdir() if p.is_dir()) if directory.is_dir() else []
    except OSError:
        paths, people = [], []
        report["source_state"] = "unavailable"
    for person in people:
        report["sources"][person] = {}
    by_person = defaultdict(list)
    for path in paths:
        by_person[path.parent.name].append(path)
    for person in sorted(set(people) | set(by_person)):
        counts = Counter()
        unique, cuts = {}, set()
        content_hashes = set()
        issues = []
        for path in by_person[person]:
            counts["files"] += 1
            try:
                before = path.stat()
                # Freeze read extent, not capture: do not lock or pause the resident.
                with path.open("rb") as stream:
                    data = stream.read(before.st_size)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino
                ):
                    counts["changed_during_read"] += 1
                for line in data.splitlines():
                    counts["rows"] += 1
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("not a record")
                        content = row.get("content")
                        if isinstance(content, str):
                            counts["captured_chars"] += len(content)
                            if row.get("length_cut") is True:
                                counts["length_cut_rows"] += 1
                                counts["length_cut_chars"] += len(content)
                            if row.get("role") == "assistant":
                                counts["assistant_chars"] += len(content)
                            elif row.get("role") == "user":
                                counts["user_chars"] += len(content)
                        if content is not None and not isinstance(content, str):
                            raise ValueError("non-text content")
                        s = source_record(path, row, instance, source_root)
                        if not s["content"].strip():
                            counts["blank_rows"] += 1
                            continue
                        counts["valid_nonblank_rows"] += 1
                        counts["valid_nonblank_chars"] += len(s["content"])
                        if row.get("length_cut") is True:
                            cuts.add(s["ref"])
                        if s["ref"] in unique:
                            counts["duplicate_ref_rows"] += 1
                            counts["duplicate_ref_chars"] += len(s["content"])
                        else:
                            # This overlap is a diagnostic, NOT a dedupe recommendation:
                            # same words at another time may be a distinct experience.
                            h = digest([s["role"], s["content"]])
                            if h in content_hashes:
                                counts["repeated_text_distinct_ref"] += 1
                            content_hashes.add(h)
                            unique[s["ref"]] = s
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        counts["malformed_rows"] += 1
            except OSError:
                counts["unreadable_files"] += 1
                issues.append("file_read_failed")
        # Chunk 2: disposition comes from the SAME planner scan() uses, so the
        # report and the scanner can never drift apart (policy recollections-v2).
        kind = source_kind(person)
        eligible_rows = [s for ref, s in unique.items() if ref not in cuts]
        if kind is None:
            disposition = {ref: "unknown_kind" for ref in unique}
            episodes, info = [], {"disposition": {}, "incomplete": 0,
                                  "oversize_single": 0}
        else:
            episodes, info = plan_episodes(
                eligible_rows, at.timestamp(),
                require_pairing=(kind == "dialogue"))
            disposition = dict(info["disposition"])
            for ref in cuts:
                disposition[ref] = "length_cut_excluded"
            if kind == "monologue":
                for ref, s in unique.items():
                    if s["role"] != "assistant" and ref not in cuts:
                        disposition[ref] = "context_only"
        counts["incomplete_exchanges"] = info["incomplete"]
        counts["oversize_single_exchanges"] = info["oversize_single"]
        scanner_groups, store_groups = defaultdict(list), defaultdict(list)
        for ref, s in unique.items():
            scanner_groups[disposition[ref]].append(s)
            if db["state"] == "unavailable":
                state = "unknown"
            elif ref in accepted:
                state = "accepted_evidence"
            else:
                state = coverage.get(ref, "not_enqueued")
                if state == "accepted":
                    state = "accepted_job_without_verified_evidence"
            store_groups[state].append(s)
        all_refs.update(unique)
        report["sources"][person] = {
            "kind": "human" if person.isdigit() else "wake" if person == "system-wake"
                    else "ritual" if person == "continua:ritual" else "other",
            "reachable_by_current_scan": kind is not None,
            "captured": dict(sorted(counts.items())),
            "unique": measure(unique.values()),
            "length_cut_unique": measure(unique[r] for r in cuts),
            "scanner": {k: measure(v) for k, v in sorted(scanner_groups.items())},
            "memory": {k: measure(v) for k, v in sorted(store_groups.items())},
            "issues": issues,
        }
    db["coverage_refs_not_in_valid_capture"] = len(set(coverage) - all_refs)
    db["accepted_refs_not_in_valid_capture"] = len(accepted - all_refs)
    # Counts here may reflect changed/deleted/unreadable sources or a concurrent
    # writer, not necessarily a defect. Do not assert that unmatched = lost.
    report["complete_read"] = (report["source_state"] == "readable"
        and db["state"] != "unavailable" and not db["issues"]
        and all(not s["issues"] and not s["captured"].get("changed_during_read")
                and not s["captured"].get("malformed_rows")
                for s in report["sources"].values()))
    return report


def render(report):
    lines = [f"{report['instance']} at {report['at']} — store={report['store']['state']} complete_read={report['complete_read']}",
             "source | unique refs/chars | scanner disposition (refs) | memory state (refs)"]
    for person, s in report["sources"].items():
        states = lambda key: ", ".join(f"{k}={v['refs']}" for k, v in s[key].items()) or "none"
        lines.append(f"{person} | {s['unique']['refs']}/{s['unique']['chars']} | {states('scanner')} | {states('memory')}")
    lines.append("Jobs: " + json.dumps(report["store"]["jobs"], sort_keys=True))
    lines.append("Counts are source evidence coverage, not proof of remembered meaning. See --json for exclusions, overlaps and read issues.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", choices=["residentb", "residenta", "all"], default="all")
    ap.add_argument("--source-root", type=Path, default=CHRONICLE)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--at", default=None, help="timezone-aware cutoff for reproducible bucket classification")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    at = args.at or now()
    reports = [inspect_resident(i, args.source_root, args.root, at) for i in
               (["residentb", "residenta"] if args.instance == "all" else [args.instance])]
    print(json.dumps(reports, indent=2, ensure_ascii=False) if args.json else
          "\n\n".join(render(r) for r in reports))
    return 0 if all(r["complete_read"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
