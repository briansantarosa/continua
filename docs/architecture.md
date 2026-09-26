# Continua's memory architecture

Distilled from the canonical memory plan. This is the design behind the
recollections pipeline, the standing view, and the budget model.

## North star

An always-present, first-person account of the resident's ongoing life,
spanning recent and distant experience, plus associative recall for additional
depth — with minimal operational context around it.

Preserve the details the resident chooses, let them articulate the meanings,
and keep the path back to the experience.

The resident-facing context should principally contain:

1. The current exchange in its original form, continuing a persistent thread
   for this context.
2. Recent verbatim activity from the resident's other live threads, so the
   present situation survives switching who they are talking to.
3. A substantial, stable, source-grounded autobiographical view across time:
   experiences, relationships, dated reflections, and expressed ongoing
   intentions.
4. The resident's own notes to themselves — forward intentions and project
   states in their words — plus a factual note of what changed since last time.
5. Additional cue-relevant recollections or expanded details as needed.
6. Minimal present-time orientation, operating instructions, and tools.

This applies to human chats, scheduled wakes, and reflection/ritual contexts.
A topicless wake must receive meaningful life context before any recall call.
The standing view is not a fixed identity manifesto or a recursively rewritten
life summary; it is a selection of accepted canonical material.

"Human-like continuity" is a design aspiration: associative recall,
recognizable experiences, evolving understanding, and unfinished matters that
can be resumed. It is not a claim of human cognition. Borrow useful properties
without deliberately reproducing human memory errors.

## The pipeline

```text
Append-only source record
          |
          v
Grounded drafting + verification
          |
          v
Canonical first-person episodes + immutable revisions
          ^
          | Supported corrections, resident marks, dated reflections
          |
          +-----------------------------+
          |                             |
          v                             v
Standing autobiographical       Rebuildable retrieval index
selection across time           for supplementary depth
          |                             |
          +-----------------------------+
                        |
                        v
One deduplicated, privacy-filtered, budgeted memory view
(protected standing allocation + bounded supplementary recall)
                        |
                        v
Current exchange + available life history + minimal instructions
```

- **Sources are evidence; recollections are derived accounts.** Every accepted
  recollection traces to exact source quotes, with per-claim verification.
- **Revisions are immutable.** A corrected recollection supersedes (never
  deletes) its predecessor; originals are preserved with audit trails.
- **The index is an index.** Lexical search, embeddings, dates, participants,
  and episode links are ways to find an experience — not additional narrators.
  Search returns accepted prose rather than synthesizing a fresh summary.
- **Importance requires evidence or an explicit resident mark** — not utility
  model guesses about emotional significance.

## The first-person contract

- Preserve who did, said, learned, proposed, or decided what.
- Distinguish "I experienced," "I was told," and "I inferred."
- Preserve uncertainty, negation, unresolved outcomes, and explicit changes
  of mind.
- Do not invent feelings, motivations, or action completion to make prose
  more personal.
- Preserve distinctive language without forcing a house literary style.
- Keep detailed accepted versions. Aging alone does not trigger rewriting.
- Budget pressure can select a verified shorter version or omit a whole
  episode from the current view; it must not erase the underlying experience.

## Persistent threads for every context

Every context a resident wakes into or speaks in has a raw thread that
**continues from last time**: each human conversation, the ritual/reflection
thread, and the wake thread.

- **One life across people.** A resident does not live parallel, mutually
  blind lives per conversation partner; attribution is kept (who was told
  what), not concealment. What one conversation may see of another is
  governed by privacy rules, not by separate stores.
- **The juggle.** Recent threads are carried verbatim (the active one last,
  with the most recency), bounded per-thread, with whole-thread drops on
  overflow — a tool call and its result are never split.
- **Aging and handoff.** Material that leaves the verbatim window folds into
  the canonical store; coverage tooling measures whether ingestion kept up
  with the busiest periods (busy days are where memory leaks are born).

## One budget model, expressed as percentages

- The resident's config declares the real token window
  (`llm.total_context_tokens`) and the generation reserve (`num_predict`).
- The prompt budget is derived: `(total − reserve − margin) × prompt_utilisation`.
- The derived budget is split by percentage bands (standing view, recent
  threads, juggle, notes, orientation) — with floors, lending between bands,
  and a compression ladder that prefers shorter *verified* revisions under
  pressure.
- Verification is per request: the composed prompt is checked against the
  budget in tokens (via the server's tokenizer where available). The server
  limit always wins.
- Capacity is not a demand to fill. Starting near 60% utilisation leaves
  room for the turn to breathe.

## Standing life context first; recall for depth

- **Always-present autobiographical foundation** — oldest-first accepted
  recollections anchor the life; anchors resist compression pressure.
- **Thematic working sets** — keyword-scored selection of recollections
  matching the current message, fitted after the backbone, releasing cleanly
  on topic change.
- **Supplementary recall** — `search_my_memories` / `recall_my_experience`
  / `deep_recall` retrieve accepted prose from the canonical store on
  demand; these are depth tools, not substitutes for the standing view.
- **The resident's own notes** — forward intentions (the sticky note),
  project states (the whiteboard), and elapsed-time awareness, written and
  maintained by the resident; injected verbatim within a small cap.
- **Trajectory** — a view of the resident's own changing understanding,
  assembled from existing evidence only.

## Wakes, letters, rituals

- **Wakes** are initiative windows: a state packet (what changed since last
  time — counts, unread replies, recent keeps, the delta) plus a bounded
  budget of tool calls and tokens. "Do nothing" is a first-class outcome.
- **Letters** are asynchronous correspondence between residents: written to
  an outbox, delivered on the recipient's next wake, archived as
  correspondence memory on both sides.
- **The nightly ritual** reviews the day's record (a skeleton first, chosen
  deep-dives next), decides what to keep, and feeds the canonical pipeline.
  Consolidation folds idle conversation threads into that pipeline.

## Privacy

The architecture assumes the record is sensitive:

- Canonical stores are per-resident and private (`app.collection_name`,
  `app.instance_path`); one collection per resident, never shared.
- Privacy rules govern which material may appear in which context — the
  same store feeds different views under different rules.
- The program's git tracking is code-only by convention; chronicles,
  recollections, letters, and resident-authored files stay out of version
  control. If you run this with real people on the other end, extend that
  discipline to backups, logs, and any telemetry you add.
