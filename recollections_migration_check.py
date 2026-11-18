"""Explicit live LOCAL migration probe; not part of the offline test glob.
Uses real chronicle, live recollection DB and YAML. Calls utility LLM only;
assembles but does NOT send the resident-facing prompt or restart the bridge.
Run with --run to authorize model calls and accepted revision writes.
"""
import argparse
import json
from pathlib import Path
import sys

import yaml
import recollections as r


def check(label, condition, detail=''):
    print(('PASS' if condition else 'FAIL') + ': ' + label +
          (' — ' + detail if detail else ''), flush=True)
    if not condition:
        raise AssertionError(label)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run', action='store_true', required=True)
    ap.parse_args()
    instance, person = 'residenta', '1000000001'
    cfg = yaml.safe_load((r.BASE / 'configs/residenta.yaml').read_text())
    path = r.CHRONICLE / instance / person / '2026-09-15.jsonl'
    wanted = ['66d732314e52', 'b84d5c1c4e5f']
    rows = {row['uid']: row for row in map(json.loads, path.read_text().splitlines())}
    sources = [r.source_record(path, rows[uid], instance) for uid in wanted]
    check('original greeting attribution',
          sources[0]['role'] == 'user' and sources[0]['content'] == 'Hi persona-a'
          and sources[1]['role'] == 'assistant' and 'Hi Alex' in sources[1]['content'])
    before_sources = [s['hash'] for s in sources]
    model = r.LocalModel()
    store = r.Store(instance)
    with r.worker_lock(store) as locked:
        check('production-store worker lock', locked)
        job = store.enqueue(sources) or r.digest([instance, [s['ref'] for s in sorted(
            sources, key=lambda s: (r.stamp(s['ts']), s['ref']))]])
        check('job exists in actual store', store.latest(job) is not None or
              job in [row[0] for row in _jobs(store)])
        value = store.latest(job)
        if value is None:
            full = r.process(store, job, model, model, budget=2400)
            check('source → accepted rich recollection', full['status'] == 'accepted',
                  json.dumps(full.get('errors', [])))
        versions = store.revisions(job)
        fullest = max(versions, key=lambda v: r.token_bound(v['text']))
        check('rich version is more than snippets', len(fullest['text'].split()) >= 100)
        # Independent targeted source-grounded assertion. No regex pretending
        # to establish semantics; require evidence in the checker's answer.
        def greeting(value):
            verdict = model('''Audit only greeting attribution against the supplied sources.
The source role user is Alex; role assistant is persona-a. Alex's 'Hi persona-a'
is a greeting FROM Alex TO persona-a. persona-a's 'Hi Alex' is the reverse greeting.
Return JSON {"pass":true/false,"reason":"explanation"}. Pass only if the
recollection mentions Alex greeting persona-a (or I greeted Alex in response)
without reversing the source roles. Do not invent a reversal.''',
                {'sources': sources, 'recollection': value['text']})
            check('greeting NOT inverted', verdict.get('pass') is True, verdict.get('reason', ''))
        greeting(fullest)
        target = int(r.token_bound(fullest['text']) * .82)
        candidates = [v for v in versions if r.token_bound(v['text']) <= target]
        if not candidates:
            smaller = r.process(store, job, model, model, budget=target, compress=True)
            check('accepted smaller revision', smaller['status'] == 'accepted',
                  json.dumps(smaller.get('errors', [])))
            candidates = [store.latest(job)]
        compact = max(candidates, key=lambda v: r.token_bound(v['text']))
        greeting(compact)
        check('strictly smaller without replacing original',
              r.token_bound(compact['text']) < r.token_bound(fullest['text'])
              and any(v['text'] == fullest['text'] for v in store.revisions(job)),
              f"{r.token_bound(fullest['text'])} → {r.token_bound(compact['text'])} UTF-8 bytes (NOT model tokens)")
        r.resolve_sources(sources, instance)
        check('original source evidence unchanged', before_sources == [s['hash'] for s in sources])

    # Exercise the actual read/selection and core renderer, not hand-authored
    # injected prose. Network resident generation and Mem0 are deliberately
    # not invoked: this test ends at the exact request-facing prompt format.
    import core
    import context_budget
    c = core.SagentCore.__new__(core.SagentCore)
    c.instance_id = instance
    c._mem_layers = core._normalize_memory_layers(cfg['memory'])
    check('actual config enables recollections', c._mem_layers['recollections']['enabled'])
    view = r.context_view(instance, person, budget=16000)
    check('accepted episode selected from actual store', any(s['job'] == job for s in view['selected']))
    compact_view = r.select_view([compact], instance, person, 16000)
    check('smaller revision reaches prompt view unchanged', compact['text'] in compact_view['text'])
    social = c._build_continua_block(person, 'What do you remember about our prompt discussion?', [])
    identity = cfg.get('prompts', {}).get('identity', '')
    messages = [{'role': 'system', 'content': identity + '\n\n' + social + '\n\n' + compact_view['text']},
                {'role': 'user', 'content': 'What do you remember about our prompt discussion?'}]
    facing, measured = context_budget.fit(messages, cfg['max_prompt_chars'], render=core._raw_chatml_render,
                                          reserve=1024)
    prompt = core._raw_chatml_render(facing) + core._open_think_prompt()
    check('actual raw composer preserves smaller first-person recollection', compact['text'] in prompt)
    check('request within configured character budget', len(prompt) < cfg['max_prompt_chars'], str(measured))
    check('no legacy book or narrator blocks', all(marker not in prompt for marker in
        ('[WHO YOU ARE', '[WHO YOU\'RE WITH', '[Earlier Conversation Summary]', 'THE LAST 24 HOURS:')))
    output = r.ROOT / 'migration-check-residenta.json'
    output.write_text(json.dumps({'job': job, 'full': fullest['text'], 'smaller': compact['text'],
                                  'prompt': prompt, 'budget': measured,
                                  'scope': 'real sources/model/store/config/read-view/core-renderer; no resident generation'},
                                 indent=2, ensure_ascii=False))
    print('PASS: migration probe; evidence: ' + str(output), flush=True)


def _jobs(store):
    with store.db() as db:
        return db.execute('SELECT id FROM jobs').fetchall()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('FAIL: migration probe stopped: ' + type(exc).__name__ + ': ' + str(exc), flush=True)
        sys.exit(1)
