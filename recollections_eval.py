"""Explicit local-model prose trial. NEVER imported by the agent-free test gate.
Writes private review artifacts; no injection and no resident desktop writes.
Run: python recollections_eval.py --run --root /path/to/private/review
"""
import argparse
import json
from pathlib import Path
import time

import recollections as r

# Three genuine episodes, covering rich interpretation, unresolved decisions,
# and repeated attempts to get an answer. IDs come from the original chronicle.
SAMPLES = [
    ('residentb', '2026-09-16', 'architecture-pointer',
     ['0ca6f3d09778', '21136a4d08eb', 'fb05e3a99161', '1fdb80532bf0']),
    ('residenta', '2026-09-15', 'prompt-ownership',
     ['66d732314e52', 'b84d5c1c4e5f', '9a23a552d6ec', '23eaad72c12a']),
    ('residenta', '2026-09-16', 'unanswered-wants',
     ['5cb39ab2ab3e', '1642e0299574', '893a21c56f0f', '98b7c757a099',
      'e8301fa579c6', '4e2e2b41f1aa']),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run', action='store_true', required=True)
    ap.add_argument('--root', required=True, type=Path)
    args = ap.parse_args()
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    model = r.LocalModel()
    results = []
    for instance, day, label, uids in SAMPLES:
        path = r.CHRONICLE / instance / '1000000001' / (day + '.jsonl')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        by_uid = {row['uid']: row for row in rows}
        sources = [r.source_record(path, by_uid[uid], instance) for uid in uids]
        # Separate stores keep selected samples from the automatic backfill trial.
        store = r.Store(instance, args.root / label)
        job = store.enqueue(sources)
        if job is None:
            job = r.digest([instance, [s['ref'] for s in sorted(sources, key=lambda s: (r.stamp(s['ts']), s['ref']))]])
        started = time.monotonic()
        with r.worker_lock(store) as locked:
            if not locked:
                raise RuntimeError('sample locked')
            full = store.latest(job)
            first = {'status': 'reused', 'text': full['text']} if full else r.process(store, job, model, model, budget=2400)
            full = store.latest(job)
            compressed = r.process(store, job, model, model, budget=int(r.token_bound(full['text']) * .62), compress=True) if full else None
        result = {'sample': label, 'instance': instance, 'job': job,
                  'seconds': round(time.monotonic() - started, 2), 'full': first,
                  'compression': compressed, 'human_review': 'pending'}
        results.append(result)
        (args.root / (label + '.json')).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        text = '# ' + label + '\n\nSHADOW ONLY — generated recollection; the designer review pending.\n\n## Original passages\n\n'
        for source in sources:
            text += f"### {source['ts']} {source['role']} ({source['uid']})\n\n{source['content'] or '(empty delivered reply)'}\n\n"
        text += '## Full draft result\n\n' + first.get('text', json.dumps(first))
        text += '\n\n## Smaller version result\n\n' + (compressed.get('text', json.dumps(compressed)) if compressed else 'No accepted full version.')
        text += '\n\nBudget metric: UTF-8 bytes, conservative proxy, not measured model tokens.\n'
        (args.root / (label + '.md')).write_text(text)
        print(json.dumps({k: v for k, v in result.items() if k not in ('full', 'compression')}), flush=True)
    (args.root / 'results.json').write_text(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
