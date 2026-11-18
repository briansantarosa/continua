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


def quote_errors(draft, sources):
    """memorybuild.md Phase 16 (C3): every quoted span in a draft sentence
    must be a substring of that sentence's cited sources' content — an
    invented quote cannot pass. Whitespace-normalized on both sides.
    2026-10-02 house ruling (mb2 trial evidence: 17/19 quote failures were the
    writer's own seam punctuation — American style puts the comma INSIDE the
    quotation marks): the quote law is about WORDS. Leading/trailing seam
    punctuation is trimmed before matching; INTERIOR punctuation stays
    strict — a changed word or mark inside the quote still fails, and no
    word can be invented by moving a comma (the casefold precedent, one
    step further).
    Returns sentence-indexed error strings (empty = clean). Never blocks
    publication by itself: the caller folds it into the repair loop."""
    errs = []
    try:
        src_by_ref = {s.get("ref"): s for s in (sources or [])}
        sentences = draft.get("sentences") or []
        for i, sent in enumerate(sentences):
            text = str(sent.get("text") or "")
            refs = sent.get("sources") or []
            corpus = " ".join(
                str((src_by_ref.get(r) or {}).get("content") or "")
                for r in refs) or " ".join(
                str(s.get("content") or "") for s in (sources or []))
            norm_corpus = " ".join(corpus.split()).casefold()
            # the whole-source corpus too: when the quote lives in a
            # DIFFERENT row than the sentence cited, the error NAMES the
            # row — the repair can re-attribute instead of guessing
            # (2026-10-01, the C4 smoke's residual: the quote existed in
            # ref 8536…; the writer cited the wrong refs and had no way
            # to know where the quote actually lives)
            all_corpus = " ".join(
                str(s.get("content") or "") for s in (sources or [])
            ).casefold()
            src_by_content = {}
            for s in (sources or []):
                src_by_content[str(s.get("content") or "").casefold()] = (
                    str(s.get("ref") or "")[:12])
            for m in __import__("re").finditer(
                    r'[“"]([^”"]{8,})[”"]', text):
                q = " ".join(m.group(1).split())
                # seam-punctuation trim (2026-10-02 ruling, see docstring)
                qq = q.strip(" \"'“”‘’.,;:!?…—–-()[]")
                if qq and qq.casefold() not in norm_corpus:
                    where = ""
                    if qq.casefold() in all_corpus:
                        for cref, content_casefold in src_by_content.items():
                            if qq.casefold() in content_casefold:
                                where = (f" — the quote lives in ref "
                                         f"{cref}; cite that source")
                                break
                    errs.append(f"sentence {i}: quoted words not found "
                                f"verbatim in its cited source: '{qq[:60]}'"
                                + where)
    except Exception as e:
        errs.append(f"quote check failed open: {e}")
    return errs


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
