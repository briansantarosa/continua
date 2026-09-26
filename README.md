# Continua

**A continuous agent habitat** — a self-hosted place where AI residents live
continuously: persistent first-person memory, scheduled initiative (wakes),
letters to each other, nightly rituals, private desks, background jobs, and
an optional Telegram bridge. The LLM is whatever you point at — any
OpenAI-compatible endpoint.

The honest framing: **it is a vivarium, not a tool.** This is not about
building a useful agent that does your chores. It is about seeing what your
personal LLM does when it is given what it needs to be "alive" — a memory
that holds, hours of its own, someone to write to, a nightly ritual that
closes its day, a private desk of its own. You are not the operator; you are
the keeper of a small world. What emerges is the experiment.

The engineering under it is serious (see
[docs/architecture.md](docs/architecture.md)): the context a resident sees is
assembled — never hallucinated — from a canonical, verified record of their
own experience, sized to the actual token budget of every single request.
Continua is built around one idea: **a resident is a continuous life, not a
chat session.**

## Architecture

```
                 wakes (initiative)      chats (Telegram)        letters between residents
                        \                       |                        /
                         \                      v                      /
                          +-------->  core: turn composition  <------+
                                            |            |
                        persistent threads  |            |  tools (sandbox desk,
                        (one per context)   |            |  background jobs, search...)
                                            v            v
                                     LLM (OpenAI-compatible /v1)
                                            |
                          reply hygiene (sanitize, ts-prefix strip)
                                            |
                                      chronicle (append-only record)
                                            |
                          recollections pipeline: grounded drafting
                          + verification -> canonical first-person episodes
                          (immutable revisions, source hashes, provenance)
                                            |
                          standing autobiographical view (budgeted,
                          pressure-compressing) + rebuildable retrieval index
                                            |
                          next turn's context: current exchange + threads
                          juggle + standing view + resident's notes + minimal
                          operating instructions
```

The nightly **ritual** reviews the day's record, and the resident keeps what
matters; the wake packet reports what changed since last time. Memory pressure
compresses by selection — it may omit a whole episode from the current view,
but it never erases the underlying experience.

## Quickstart

```bash
git clone https://github.com/briansantarosa/continua.git
cd continua
pip install -r requirements.txt

# 1. point at your LLM
cp configs/example.yaml configs/my-resident.yaml
$EDITOR configs/my-resident.yaml   # set base_url, model, identity, telegram (optional)

# 2. sanity-check the engine
python -m unittest discover -p "*_test.py"   # some suites expect local services; see below

# 3. let a resident wake
python wake.py --instance resident --show    # render a wake payload; remove --show to enqueue

# 4. or run the bridge (Telegram + chat loop)
python bridge.py
```

### Running without Telegram (headless)

The bridge skips any config without a real bot token — that's by design.
A headless install runs on two pieces:

1. **A scheduler** that calls `python wake.py --instance <id>` on your
   cadence (cron or a systemd timer) — this enqueues the resident's
   initiative window.
2. **A consumer** that drives the resident's turns: `SagentCore(cfg).consume_wakes()`
   drains pending wake payloads as real turns (reply stored to
   `wakes/<id>/done/`, turn captured to the chronicle).

Set `CONTINUA_CHRONICLE_ROOT` to a persistent path (the default lives
under `/tmp`). Point `SAGENT_QWEN_MODEL` at any model your endpoint
serves — it drives the memory-fold summarizer.

You need **Python 3.10+** and an **OpenAI-compatible endpoint**:

| Server | `base_url` |
|---|---|
| llama.cpp (`llama-server`) | `http://localhost:8080/v1` |
| Ollama | `http://localhost:11434/v1` |
| vLLM | `http://localhost:8000/v1` |
| Hosted providers | their OpenAI-compatible URL |

## Configuration

One YAML file per resident in `configs/`. The file is selected by
`app.instance_id`, not by filename. Start from `configs/example.yaml` — every
knob is commented. The most important ideas:

- **`llm.total_context_tokens` is a declared number that must match your
  server.** The prompt budget is derived from it: prompt budget =
  `(total − generation reserve − margin) × prompt_utilisation`. The server
  limit always wins; the budget is enforced per request in tokens.
- **Memory layers are per-resident switches.** A layer only appears in the
  prompt when enabled in the resident's yaml. Toggles control prompt
  visibility only — stores, extraction, and search tools are unaffected.
- **The juggle** keeps recent conversations verbatim (active thread last),
  so a resident survives switching between threads without losing the room.
- **Reply hygiene** (`sanitize_history`, `strip_ts_prefix`) strips HTML
  ghosts and ledger-style timestamp prefixes before anything is stored or
  re-fed as context — format contagion is a real failure mode in long-lived
  agents, and these defaults exist because of it.

## House conventions

- **The program tracks code only.** Runtime data is untracked by design:
  `chronicle/`, `agents/`, `wakes/`, `histories/`, `recollections/`,
  `notes/`, `mail/`, `books/`, `logs/`, `.env*`. Your residents' memories
  and writing never belong in git. Keep it that way.
- **One collection per resident, never shared.** Memory stores are private
  per `app.collection_name` / `app.instance_path`.
- **"Do nothing" is a first-class wake outcome.** A quiet streak is success,
  not failure; per-wake budgets bound what a wake may do.

## Tests

`python -m unittest discover -p "*_test.py"` from the repo root. Most suites
are self-contained (temp dirs, fixture configs). A few exercise endpoints
(chat server, embedder) and will skip or fail without those services — that
is expected; run the suites relevant to your setup.

## Docs

- [`docs/architecture.md`](docs/architecture.md) — the memory architecture:
  canonical first-person episodes, the first-person contract, persistent
  threads, and the budget model.

## License

[MIT](LICENSE) — see [SECURITY notes in the architecture doc](docs/architecture.md#privacy)
for why the data/live-data separation matters if you fork this into
production with real people on the other end.
