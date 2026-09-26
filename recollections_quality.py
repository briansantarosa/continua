"""Source ownership guards and append-only selection review, not prose authorship."""
import re

# Deterministic regression guards for confirmed accepted perspective swaps.
# Broader semantics still require a source-grounded reviewer, not a regex.
SWAP_CLAIMS = (
    (r'\bi (?:had to |did )?clear(?:ed)? (?:our|the) context\b',
     r'\bi (?:had to |did )?clear(?:ed)? (?:our|the) context\b'),
    (r'\bi told (?:the system|you|them|him|her) that it (?:does not|doesn.t) matter',
     r'\bit (?:does not|doesn.t) matter if you have awareness'),
)


def ownership_errors(draft, sources):
    errors = []
    by_ref = {s['ref']: s for s in sources}
    for n, sentence in enumerate(draft.get('sentences', [])):
        evidence = [by_ref[x] for x in sentence.get('sources', []) if x in by_ref]
        text = sentence.get('text', '')
        for claim, source_claim in SWAP_CLAIMS:
            if re.search(claim, text, re.I) and any(
                s['role'] == 'user' and re.search(source_claim, s['content'], re.I)
                for s in evidence
            ) and not any(s['role'] == 'assistant' and re.search(source_claim, s['content'], re.I)
                          for s in evidence):
                errors.append(f'sentence {n}: confirmed other-speaker first-person swap')
    return errors


def ownership_evidence(sources, instance):
    """Compact per-row speaker map; the rules it implies live in the prompt
    text (recorded utterance semantics, quotation rules) — duplicating them
    per row doubled the payload on row-heavy parts."""
    return [{'ref': s['ref'], 'outer_speaker': ('resident:' + instance
             if s['role'] == 'assistant' else 'participant:' + s['person_id'])}
            for s in sources]


def review_errors(review, draft, sources):
    """Require explicit per-sentence attribution and preservation checks."""
    checks = review.get('sentence_audit')
    if not isinstance(checks, list) or len(checks) != len(draft['sentences']):
        return ['missing sentence ownership audit']
    refs = {s['ref'] for s in sources}
    for i, check in enumerate(checks):
        if (check.get('sentence') != i or check.get('ownership_ok') is not True
            or check.get('claim_status_ok') is not True
            or not isinstance(check.get('subject'), str) or not check['subject'].strip()
            or not check.get('evidence_refs') or not set(check['evidence_refs']) <= refs
            or not set(check['evidence_refs']) <= set(draft['sentences'][i]['sources'])):
            return ['invalid sentence ownership audit']
    preservation = review.get('preservation', {})
    if any(preservation.get(k) is not True for k in
           ('distinctive_details', 'expressed_meaning', 'uncertainty', 'intentions_vs_actions')):
        return ['meaning/detail preservation not verified']
    return []
