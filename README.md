# Continua

**A continuous agent habitat** — a self-hosted place where AI residents "live"
continuously: persistent first-person memory, scheduled initiative (wakes),
letters to each other, nightly rituals, private desks, background jobs, and
an optional Telegram bridge. The LLM is whatever you point at — any
OpenAI-compatible endpoint.

This is not about building a useful agent as an assistant or coder. It is about seeing what your
personal LLM does when it is given what it needs to be "alive" — a memory
that holds, hours of its own, someone to write to, a nightly ritual that
closes its day, a private desk of its own to write whatever it wants. You are not the operator; you are
the keeper of a small world. What emerges is the experiment.

The engineering under it- (see
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

## The desk: a room of its own

Beyond memory, the habitat gives each resident a **desk** — a private,
persistent workspace where it can learn, write what it wants, and program:
plain files it keeps (`sandbox_write/read/list`), short commands it runs
(`sandbox_exec`), and long-running background jobs it starts and tends across
its own turns (`job_start/stop/output` — CPU/memory-capped, 24 h max).

The desk is fenced, not trusted:

- everything the resident executes runs as a **dedicated unprivileged user** —
  never yours — and cannot read your files, stores, or services even if a
  prompt tricks it into trying;
- the desk is the **only persistent writable location**; projects survive
  restarts (that is continuity), the rest of the filesystem does not;
- the **network fence is matched by uid**: internet HTTP/HTTPS + DNS only —
  no local network, no LAN, no loopback, for every process the resident runs,
  however it launches them;
- every executed command, exit code, and duration is **logged append-only**;
- if confinement cannot be established, **nothing runs** (fail-closed), and
  `CONTINUA_SANDBOX=0` is the kill switch.

Provisioning the desk is the one nontrivial setup step (a dedicated user, the
fence service, passwordless sudo — see `sandbox.py`'s module docstring). The
honest limits: mount isolation via bubblewrap is deferred on modern Ubuntu;
the identity + fence + desk guarantees do not depend on it. See Safety below.

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

## Safety & responsibility

Keep the vivarium framing in mind: what emerges is the experiment, and you
are the keeper. Two honest facts:

- **Capability scales.** The largest model we have run in this habitat is a
  ~31B-class open-weights local model. Stronger models may act in smarter and
  less predictable ways — the habitat is deliberately model-agnostic, so what
  it amplifies is whatever you connect to it.
- **No sandbox is absolute.** The fence is real (uid-isolated identity,
  desk-only persistence, internet-only network, fail-closed), but kernel-level
  escape is never zero risk with any sandbox, and mount isolation is deferred
  (see The desk above).

So: run open-weights models you control locally, provision the desk properly,
watch the early days, and keep the kill switch (`CONTINUA_SANDBOX=0`) within
reach. The [MIT license](LICENSE) says the rest plainly: the software is
provided "as is," without warranty of any kind, and its authors are not
liable for what happens when you run it. This section is the disclosure that
backs that up.

## License

[MIT](LICENSE) — see [SECURITY notes in the architecture doc](docs/architecture.md#privacy)
for why the data/live-data separation matters if you fork this into
production with real people on the other end.
