"""Final-facing request budget: explicit estimate, whole-turn eviction only.

Does not mutate persistent history. Model-token calibration is not claimed:
character ceilings are the existing deployment policy, schema included.
"""
import copy
import json
import os


class ContextBudgetExceeded(ValueError):
    pass


# house ruling 2026-09-20 (the photo crash): image blocks are billed by the
# API as vision tiles (~1.2K tok floor measured on the 8080 /tokenize
# equivalent; real photos 1.5–3K tok), NOT as their base64 serialized
# length. Counting the data URI as text made a ~240K-char photo blow the
# request cap and crash her turn before any LLM call (the silent drop).
# The honest constant ≈2,500 tok × ~3.4 c/t. Env-tunable for tuning.
_IMAGE_BLOCK_COST_CHARS = int(os.getenv('CONTINUA_IMAGE_BLOCK_COST_CHARS', '8500'))
_IMAGE_BLOCK_SKELETON_CHARS = 48


def image_accounting(messages):
    """(real_chars, honest_chars) over image_url blocks in message content
    lists — what the base64 serialization adds vs what the API bills."""
    real = honest = 0
    for m in messages:
        c = m.get('content') if isinstance(m, dict) else None
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get('type') == 'image_url':
                    real += len(json.dumps(b, ensure_ascii=False))
                    honest += _IMAGE_BLOCK_COST_CHARS + _IMAGE_BLOCK_SKELETON_CHARS
    return real, honest


def fit(messages, ceiling, tools=None, render=None, reserve=512):
    facing = copy.deepcopy(messages)
    schema = json.dumps(tools, ensure_ascii=False) if tools else ''
    def size():
        base = len(render(facing) if render else json.dumps(facing, ensure_ascii=False))
        _real, _honest = image_accounting(facing)
        return base - _real + _honest + len(schema) + reserve
    dropped = 0
    if not ceiling:
        _real, _honest = image_accounting(facing)
        return facing, {'chars': size(), 'dropped': dropped,
                        'image_blocks_honest': _honest - _real,
                        'metric': 'characters_estimate'}
    # Protect current user and all of its tool rounds. Remove older complete
    # exchanges, never isolated native tool results from the current exchange.
    while size() > ceiling:
        users = [i for i, m in enumerate(facing) if m.get('role') == 'user']
        if len(users) < 2:
            raise ContextBudgetExceeded('current exchange + fixed context exceeds request budget')
        start, end = users[0], users[1]
        del facing[start:end]
        dropped += end - start
    _real, _honest = image_accounting(facing)
    return facing, {'chars': size(), 'dropped': dropped,
                    'image_blocks_honest': _honest - _real,
                    'metric': 'characters_estimate'}
