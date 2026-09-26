"""Bounded, alternating backfill driver. run_shadow owns the resident lock.

No acceptance bypass or quarantine resets. One process-level drain lock avoids
starting competing accelerated drivers; normal bridge workers retain their own
locks. Wall budget stops NEW passes, not a model call already in progress.
"""
import argparse
from collections import Counter
import contextlib
import fcntl
import json
from pathlib import Path
import time

import recollections as r


def drain(instances, max_jobs=1, wall=28800, *, run=None, clock=time.monotonic,
          sleep=time.sleep, emit=print):
    if not instances or any(i not in ('residentb', 'residenta') for i in instances):
        raise ValueError('instance must be residentb or residenta')
    if not 1 <= max_jobs <= 10 or not 1 <= wall <= 86400:
        raise ValueError('max_jobs must be 1..10; wall must be 1..86400 seconds')
    run = run or r.run_shadow
    started = clock()
    totals = {i: Counter() for i in instances}
    while clock() - started < wall:
        idle = set()
        for inst in instances:
            if clock() - started >= wall:
                break
            emit(json.dumps({'event': 'pass_start', 'instance': inst,
                             'max_jobs': max_jobs, 'elapsed_s': round(clock()-started)}))
            try:
                # Do NOT hold worker_lock here: run_shadow acquires it itself.
                result = run(inst, max_jobs=max_jobs)
            except Exception as exc:
                emit(json.dumps({'event': 'stop_error', 'instance': inst,
                                 'error_type': type(exc).__name__}))
                return 1
            status = result.get('status')
            if status == 'disabled':
                emit(json.dumps({'event': 'disabled', 'instance': inst}))
                return 1
            if status == 'busy':
                emit(json.dumps({'event': 'busy', 'instance': inst}))
                continue
            if status != 'shadow':
                emit(json.dumps({'event': 'unexpected_status', 'instance': inst,
                                 'status': status}))
                return 1
            outcomes = Counter(x.get('status', 'unknown') for x in result.get('results', []))
            totals[inst].update(outcomes)
            counts = result.get('counts', {})
            queued = result.get('collection', {}).get('queued', 0)
            emit(json.dumps({'event': 'pass_end', 'instance': inst,
                             'outcomes': dict(outcomes), 'counts': counts,
                             'queued': queued, 'total_outcomes': dict(totals[inst]),
                             'elapsed_s': round(clock()-started)}))
            # A zero pending count alone misses history not yet enqueued.
            # Require a scan that finds no fresh jobs as well. Quarantined
            # records stay visible and are NOT a successful coverage claim.
            if not counts.get('pending', 0) and not queued:
                idle.add(inst)
        if idle == set(instances):
            emit(json.dumps({'event': 'eligible_queue_drained',
                             'note': 'quarantine/exclusions still require review'}))
            return 0
        remaining = wall - (clock() - started)
        if remaining > 0:
            sleep(min(5, remaining))
    emit(json.dumps({'event': 'wall_budget_reached', 'totals': totals,
                     'note': 'incomplete; rerun to resume, no quarantine reset'}))
    return 2


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('instance', choices=['residentb', 'residenta', 'all'], nargs='?', default='all')
    ap.add_argument('max_jobs', type=int, nargs='?', default=1)
    ap.add_argument('wall', type=int, nargs='?', default=28800)
    args = ap.parse_args()
    instances = ['residentb', 'residenta'] if args.instance == 'all' else [args.instance]
    # Distinct from resident worker locks; never wrap run_shadow in those.
    lock_path = Path(r.ROOT) / 'accelerated-drain.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'event': 'another_accelerated_driver_running'}), flush=True)
            return 1
        return drain(instances, args.max_jobs, args.wall,
                     emit=lambda line: print(line, flush=True))


if __name__ == '__main__':
    raise SystemExit(main())
