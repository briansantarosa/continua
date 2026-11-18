"""Pure bounded episode planning. Prompt rows are boundaries, not wake evidence."""
from datetime import datetime
import hashlib
import json


def plan(records, at_ts, cap=16000, gap_s=1800, close_margin_s=300,
         require_pairing=True):
    if cap < 1:
        raise ValueError('positive cap required')
    stamp = lambda s: datetime.fromisoformat(s['ts']).timestamp()
    ordered = sorted({s['ref']: s for s in records}.values(),
                     key=lambda s: (stamp(s), s['role'] != 'user', s['ref']))
    if len({(s['instance'], s['person_id']) for s in ordered}) > 1:
        raise ValueError('cross-thread planner input')
    info = {'disposition': {}, 'incomplete': 0, 'oversize_single': 0}
    closed = lambda row: at_ts - (int(stamp(row)) // 1800 + 1) * 1800 >= close_margin_s
    groups, group = [], []
    for row in ordered:
        # User prompts delimit wakes even though their content is excluded.
        if group and (row['role'] == 'user' and group[-1]['role'] == 'assistant'
                      or stamp(row) - stamp(group[-1]) >= gap_s
                      and (not require_pairing or row['role'] == group[-1]['role'])):
            groups.append(group); group = []
        group.append(row)
    if group:
        groups.append(group)
    episodes = []
    for group in groups:
        if not require_pairing:
            for s in group:
                if s['role'] == 'user': info['disposition'][s['ref']] = 'context_only'
            evidence = [s for s in group if s['role'] == 'assistant']
        else:
            evidence = group
            if {s['role'] for s in group} != {'user', 'assistant'}:
                state = ('not_closed' if not closed(group[-1]) else
                         'incomplete_unanswered' if group[0]['role']=='user' else 'incomplete_orphan')
                info['disposition'].update((s['ref'], state) for s in group)
                info['incomplete'] += state.startswith('incomplete')
                continue
        # Whole rows are indivisible. Only a single oversized row may exceed
        # the cap. Large exchanges split into source-linked consecutive parts;
        # repeated context is not re-enqueued as new experience.
        group_start = len(episodes)
        batch, size = [], 0
        for s in evidence:
            if not closed(s):
                if batch: episodes.append(batch); batch=[]; size=0
                info['disposition'][s['ref']] = 'not_closed'
                continue
            if batch and size + len(s['content']) > cap:
                episodes.append(batch); batch=[]; size=0
            batch.append(s); size += len(s['content'])
            if len(s['content']) > cap:
                episodes.append(batch); batch=[]; size=0
        if batch: episodes.append(batch)
        parts = episodes[group_start:]
        if len(parts) > 1:
            link = hashlib.sha256(json.dumps([s['ref'] for s in evidence]).encode()).hexdigest()
            for index, part in enumerate(parts):
                episodes[group_start + index] = [dict(s, episode_link={
                    'chain': link, 'part': index + 1, 'parts': len(parts),
                    'source_count': len(evidence),
                    'source_chars': sum(len(row['content']) for row in evidence)})
                    for s in part]
    for episode in episodes:
        oversized = sum(len(s['content']) for s in episode) > cap
        assert not oversized or len(episode) == 1
        info['oversize_single'] += oversized
        info['disposition'].update((s['ref'], 'eligible_oversize_single' if oversized else 'eligible')
                                   for s in episode)
    return episodes, info
