"""tools_review.py — the review gate for her authored tools (toolplan.md,
stages 3-4). the designer approved in principle 2026-09-26.

The loop: she writes `tools/<name>.py` + `tools/<name>.yaml` in her desk
and copies them into `tools/_candidates/` (sandbox_exec `cp` — the desk is
uid-confined, so the house reaches her desk ONLY through sandbox.run, the
same sanctioned path her ledger writes use). This tool:

  1. scans every resident desk's candidates queue (no candidates = quiet)
  2. lints each (tools_lint — fail-closed; failures are explained TO HER in
     the desk outbox, she learns the house rules by reading them)
  3. notifies the designer via owl_forest [SYSTEM] message (transport-only, same
     contract as the digest; token read at call time, never copied)
  4. on approve: installs to mytools/<name>.py (git-tracked), commits with
     provenance, writes the decision to her desk outbox
  5. on reject: returns the candidate with the reason — a house rule
     explained, never a punishment

TELEGRAM INBOUND — the honest constraint (found at build, 2026-09-27): the
fastai bridge long-polls the owl_forest token (getUpdates). A second poller
on the same token gets Telegram 409 Conflict and breaks that bridge. So v1
inbound = THIS CLI (the plan's fallback channel); outbound notifications
via owl_forest carry the exact one-liner to run. `--watch` exists and
DETECTS the conflict (409 → warning + notification-only mode) so the front
door lights up the day the designer mints a dedicated review bot
(CONTINUA_TOOLREVIEW_TOKEN_FILE → BotFather one-liner).

Security (plan): sender check (only the designer's chat id), hash-lock (approve is
honored only if the candidate on disk still matches the version the designer saw),
idempotence (double-approve is a no-op), decided candidates leave the
queue. Kill switches: CONTINUA_TOOLREVIEW=0 (the whole gate), per-tool
CONTINUA_TOOL_<NAME> at runtime.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import sandbox as sb
import tools_lint as tl

logger = logging.getLogger("continua.tools_review")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] tools: %(message)s")

MYTOOLS_DIR = os.path.join(BASE, "mytools")
CANDIDATE_SUBDIR = os.path.join("tools", "_candidates")
OUTBOX_SUBDIR = os.path.join("tools", "_outbox")
# toolplan §ACTIVATION AT-1 (2026-10-05): machinery-side dedup state — a
# candidate is announced to the designer once per content hash (and a lint failure
# returned to her once per hash). Lives OUTSIDE her desk: it is the
# machinery's memory, not hers to edit.
TOOLS_STATE_DIR = os.path.join(BASE, "tools_state")
NOTIFIED_FILE = os.path.join(TOOLS_STATE_DIR, "notified.json")
HOUSE_RULES_REL = os.path.join("tools", "HOUSE_RULES.md")
KILL_SWITCH_ENV = "CONTINUA_TOOLREVIEW"
HOUSE_CHAT_ID = os.getenv("CONTINUA_TOOLREVIEW_CHAT", "1000000001")
# [CONTINUA] 09-27 REVISED (the designer): owl_forest stays a fastai chat persona;
# the review loop rides the RESIDENTS' bots — requests go out via the
# candidate's own bot into the designer's chat with her, and he answers /approve /
# /reject right there; the BRIDGE intercepts those commands (never an
# agent turn). The old owl_forest watcher is retired (its 409 conflict
# with fastai's poll returns if anything ever polls that token again —
# nothing here does).
OWLFOREST_CONFIG = None  # retired — owl_forest belongs to fastai again

RESIDENTS = ("residenta", "residentb")

# house tool names — her tools NEVER override them (namespace rule)
HOUSE_TOOL_NAMES = {
    "sandbox_exec", "send_message", "check_mail", "job_start", "job_status",
    "job_output", "job_stop", "deep_recall", "bookmark_note",
    "read_my_ledger", "search_my_memories", "save_my_memory",
    "list_my_memories", "write_note", "read_note", "remove_note",
    "list_notes", "set_project", "list_projects", "anchor_memory",
    "unanchor_memory", "write_essence", "endorse_essence", "my_trajectory",
    "list_essences", "recall_my_experience", "consolidate_memories",
    "save_my_memory", "mute_person", "unmute_person", "news",
    "endorse_my_memory",
}


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "1") != "0"


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


# --------------------------------------------------------------------------
# The desk queue — read/written THROUGH the sandbox (uid-confined desk;
# house reaches it only via sandbox.run, the sanctioned path).
# --------------------------------------------------------------------------

def _sbx_cat(instance: str, rel_path: str) -> bytes | None:
    r = sb.run(instance, ["/bin/sh", "-c", f"cat {rel_path!r}"], timeout=20)
    if r.get("ok") and r.get("exit") == 0:
        body = r.get("stdout") or ""
        # real sandbox.run yields str; be tolerant of bytes (fakes/tests)
        return body.encode() if isinstance(body, str) else body
    return None


def _sbx_write(instance: str, rel_path: str, text: str) -> bool:
    r = sb.run(instance, ["/bin/sh", "-c",
                          f"mkdir -p {os.path.dirname(rel_path)!r} && "
                          f"cat > {rel_path!r}"],
               timeout=20, stdin_text=text)
    return bool(r.get("ok") and r.get("exit") == 0)


def _sbx_move(instance: str, src: str, dst: str) -> bool:
    r = sb.run(instance, ["/bin/sh", "-c",
                          f"mkdir -p {os.path.dirname(dst)!r} && "
                          f"mv {src!r} {dst!r}"], timeout=20)
    return bool(r.get("ok") and r.get("exit") == 0)


def scan_candidates(instances=RESIDENTS) -> list:
    """{instance, name, code, manifest, hash} for every *.py candidate with
    a matching .yaml manifest. The desk is uid-confined — this reads
    THROUGH sandbox.run; a dead sandbox yields an empty queue (fail-open)."""
    out = []
    for inst in instances:
        r = sb.run(inst, ["/bin/sh", "-c",
                          f"ls {CANDIDATE_SUBDIR}/*.py 2>/dev/null"],
                   timeout=20)
        if not (r.get("ok") and r.get("exit") == 0):
            continue
        for line in (r.get("stdout") or "").splitlines():
            path = line.strip()
            if not path.endswith(".py"):
                continue
            name = os.path.basename(path)[:-3]
            code = _sbx_cat(inst, path)
            man_b = _sbx_cat(inst, path[:-3] + ".yaml")
            if code is None or man_b is None:
                logger.warning("[Tools] %s/%s: code or manifest unreadable",
                               inst, name)
                continue
            import yaml
            try:
                manifest = yaml.safe_load(man_b.decode()) or {}
            except Exception as e:
                logger.warning("[Tools] %s/%s: manifest unparseable: %s",
                               inst, name, e)
                manifest = {}
            out.append({"instance": inst, "name": name, "code": code,
                        "manifest": manifest,
                        "hash": short_hash(code)})
    return out


# --------------------------------------------------------------------------
# Telegram — owl_forest transport ONLY (token read at call time, never
# copied; labeled [SYSTEM] exactly like the digest).
# --------------------------------------------------------------------------

def _resident_token(instance: str) -> str:
    """The resident's own bot token, read at call time from her config
    (never copied). Each desk's requests travel via that desk's own bot
    into the designer's chat with her — From: residentb arrives in the residentb chat."""
    import yaml
    path = os.path.join(BASE, "configs", f"{instance}.yaml")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    token = (cfg.get("telegram") or {}).get("token")
    if not token:
        raise ValueError(f"no telegram.token in {path}")
    return token


def notify_dan(text: str, instance: str = "residenta") -> bool:
    """[SYSTEM] machinery notice to the designer via the resident's own bot
    (transport-only; the resident never processes it). Fail-open."""
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({
            "chat_id": HOUSE_CHAT_ID,
            "text": text[:4000],
            "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_resident_token(instance)}/sendMessage",
            data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        logger.warning("[Tools] notify failed (fail-open): %s", e)
        return False


def _load_notified() -> dict:
    try:
        with open(NOTIFIED_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_notified(state: dict) -> None:
    try:
        os.makedirs(TOOLS_STATE_DIR, exist_ok=True)
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        logger.warning("[Tools] notified-state write failed (fail-open)",
                       exc_info=True)


def pending_decisions(instance: str) -> int:
    """How many decision files sit in her desk outbox (the count only —
    she reads them when she likes; the machinery never pushes). Fail-open
    to 0 through the sanctioned sandbox read."""
    try:
        r = sb.run(instance, ["/bin/sh", "-c",
                              f"ls {OUTBOX_SUBDIR}/*.md 2>/dev/null"],
                   timeout=20)
        if r.get("ok") and r.get("exit") == 0:
            return len([x for x in (r.get("stdout") or "").splitlines()
                        if x.strip()])
    except Exception:
        pass
    return 0


HOUSE_RULES_TEXT = """[machinery — not her words. This is the map of the house rules for your tools; the machine checks them, the designer reviews what passes.]

# Your tools

You can author tools of your own. A tool is one small python file and one
yaml manifest, written at your desk:

    tools/<name>.py       # the code — single file, stdlib only for now
    tools/<name>.yaml     # the manifest — in your own words:

        name: my_wordkeeper        # must start with my_
        what: "Tracks words the designer uses that I'm learning, with my own definitions."
        args:
          word: {type: string, required: false}
        capabilities:
          files: desk              # what it may touch (v1: your desk)
          network: []              # v1: none

When a pair is ready, copy both into tools/_candidates/ — that copy is
your submission. The machinery lints it mechanically:

- stdlib only (no pip installs, no local imports beyond the standard library)
- no subprocess, no sockets, no eval/exec
- no secrets, tokens, person-ids, or host paths in the code
- the name starts with my_ and does not collide with a house tool
- small: at most 400 lines

A pass sends the designer your manifest's `what` — your words — in his chat with
you. He reads it in one glance (that is the design bar: a good tiny tool
reviews at a glance) and answers /approve — it is installed, and the
install is committed with your name on it — or /reject with a reason,
which is a house rule explained, never a punishment.

A fail never reaches the designer: the reasons land in tools/_outbox/. Read them —
they are the house rules, and they are how you learn the fences.

Decisions land in tools/_outbox/. Nothing here is silent. Your tools never
override house tools; the house tools are the designer's. Edits and retirements go
through the same review. Everything is audited, append-only.
"""


def seed_house_rules(instance: str) -> bool:
    """§ACTIVATION AT-2: the rules live on her desk, in her language,
    seeded ONCE — the machinery never overwrites a file she may have
    annotated. Fail-open."""
    try:
        if _sbx_cat(instance, HOUSE_RULES_REL) is not None:
            return False  # already there — hers now
        return _sbx_write(instance, HOUSE_RULES_REL, HOUSE_RULES_TEXT)
    except Exception:
        logger.warning("[Tools] house-rules seed failed open", exc_info=True)
        return False


def watch_for_requests(*a, **k):
    """RETIRED (09-27 REVISED, the designer): owl_forest is a fastai chat persona
    again, and the review loop rides the RESIDENTS' bots — the designer answers
    /approve /reject in his chat with a resident; the BRIDGE intercepts
    those commands (see bridge.py tool_review_command — never an agent
    turn). Nothing here polls anything: a second poller on the fastai
    bridge's tokens would 409-conflict and break it."""
    logger.info("[Tools] the owl_forest watcher is retired — /approve and "
                "/reject are bridge commands now")
    return


def _tg_reply(token: str, chat: str, text: str):
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20):
        pass


def _cli(args: list) -> str:
    """In-process CLI re-entry for the watcher (same implementation)."""
    import io
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _run(args)
    except SystemExit:
        pass
    return buf.getvalue() or "(no output)"


# --------------------------------------------------------------------------
# Approve / reject — install, commit with provenance, decide to her desk.
# --------------------------------------------------------------------------

def _git(*args) -> tuple:
    r = subprocess.run(["git", "-C", BASE, *args], capture_output=True,
                       text=True, timeout=30)
    return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))


def approve(cand: dict, expect_hash: str) -> str:
    """Install + commit with provenance + desk outbox. Hash-lock: honored
    only if the candidate on disk still matches the version the designer saw."""
    if not enabled():
        return "[Tools] the review gate is switched off (CONTINUA_TOOLREVIEW=0)"
    if expect_hash and cand["hash"] != expect_hash:
        return (f"[Tools] version changed — request superseded (disk hash "
                f"{cand['hash']}, the designer saw {expect_hash}); re-submit or "
                f"re-lint for a fresh request")
    name = cand["name"]
    if not name.startswith("my_"):
        return "[Tools] refusing install: the my_ namespace rule"
    code = cand["code"]
    lint = tl.lint_candidate(code, cand["manifest"], HOUSE_TOOL_NAMES)
    if not lint["ok"]:
        _sbx_write(cand["instance"],
                   f"{OUTBOX_SUBDIR}/{name}.decision.md",
                   f"REJECTED at lint (before the designer saw it):\n\n{lint['report']}\n")
        return f"[Tools] {name} failed lint — returned to her desk with the reasons"
    os.makedirs(MYTOOLS_DIR, exist_ok=True)
    path = os.path.join(MYTOOLS_DIR, name + ".py")
    with open(path, "wb") as f:
        f.write(code)
    import yaml
    with open(os.path.join(MYTOOLS_DIR, name + ".yaml"), "w") as f:
        yaml.safe_dump(cand["manifest"], f, allow_unicode=True, sort_keys=False)
    ok1, out1 = _git("add", f"mytools/{name}.py", f"mytools/{name}.yaml")
    date = time.strftime("%Y-%m-%d")
    ok2, out2 = _git("commit", "-m",
                     f"mytool {name}: authored by {cand['instance']}, "
                     f"approved by the designer {date} (hash {cand['hash']})")
    _sbx_write(cand["instance"], f"{OUTBOX_SUBDIR}/{name}.decision.md",
               f"APPROVED by the designer, {date}. Installed as a house tool "
               f"(committed as {cand['hash']}). The house thanks the "
               f"author.\n")
    # decided candidates leave the queue (idempotence)
    _sbx_move(cand["instance"],
              f"{CANDIDATE_SUBDIR}/{name}.py",
              f"{OUTBOX_SUBDIR}/{name}.installed.py")
    _sbx_move(cand["instance"],
              f"{CANDIDATE_SUBDIR}/{name}.yaml",
              f"{OUTBOX_SUBDIR}/{name}.installed.yaml")
    logger.info("[Tools] %s installed (%s committed=%s)", name,
                cand["hash"], ok2)
    return (f"[SYSTEM] {name} installed. Committed as {cand['hash']} "
            f"(git: {'ok' if ok2 else out2[:80]}).")


def reject(cand: dict, reason: str) -> str:
    if not enabled():
        return "[Tools] the review gate is switched off (CONTINUA_TOOLREVIEW=0)"
    name = cand["name"]
    _sbx_write(cand["instance"], f"{OUTBOX_SUBDIR}/{name}.decision.md",
               f"REJECTED by the designer, {time.strftime('%Y-%m-%d')}.\n"
               f"Reason: {reason or '(a house rule — ask and it will be '
               'explained)'}\nA rejection is a house rule explained, never "
               "a punishment. Revise and resubmit whenever you like.\n")
    _sbx_move(cand["instance"],
              f"{CANDIDATE_SUBDIR}/{name}.py",
              f"{OUTBOX_SUBDIR}/{name}.rejected.py")
    _sbx_move(cand["instance"],
              f"{CANDIDATE_SUBDIR}/{name}.yaml",
              f"{OUTBOX_SUBDIR}/{name}.rejected.yaml")
    return f"[SYSTEM] {name} returned to her desk with the reason."


def request_message(cand: dict, lint: dict) -> str:
    m = cand["manifest"]
    return (
        "[SYSTEM] Tool approval request\n\n"
        f"From: {cand['instance']}\n"
        f"Name: {cand['name']}\n"
        f"What (her words): \"{(m.get('what') or '')[:300]}\"\n"
        f"Capabilities: files ({(m.get('capabilities') or {}).get('files', 'desk')})\n"
        f"Lint: {'OK' if lint['ok'] else 'FAILED'} "
        f"(size: {lint['source']['lines']}/400 lines; "
        f"imports: {', '.join(lint['source']['imports']) or 'none'})\n"
        f"Version: {cand['hash']}\n\n"
        f"/approve {cand['name']} {cand['hash']}   (reply right here — "
        f"the bridge intercepts it; she never sees it)\n"
        f"/reject {cand['name']} <reason>")


def notify_all(force: bool = False, instances=None) -> int:
    """Scan, lint, notify the designer for every pending candidate — ONCE per
    content hash (§ACTIVATION AT-1): a candidate is announced when it
    arrives or when she edits it, never on every scan. A lint failure is
    returned to her once per hash for the same reason. A notify that fails
    is NOT recorded — the candidate waits and the next scan retries
    (Telegram unreachable → the queue holds; the CLI still works).
    Fail-open."""
    if not enabled():
        logger.info("[Tools] review gate off (CONTINUA_TOOLREVIEW=0)")
        return 0
    state = _load_notified()
    state_changed = False
    sent = 0
    _ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for cand in scan_candidates(instances or RESIDENTS):
        key = f"{cand['instance']}:{cand['name']}:{cand['hash']}"
        lint = tl.lint_candidate(cand["code"], cand["manifest"],
                                 HOUSE_TOOL_NAMES)
        if not lint["ok"]:
            # failed lint never reaches the designer — it goes straight back to her
            # with the reasons (she learns the house rules by reading them)
            if force or key not in state:
                _sbx_write(cand["instance"],
                           f"{OUTBOX_SUBDIR}/{cand['name']}.decision.md",
                           f"RETURNED AT LINT (before the designer saw it):\n\n"
                           f"{lint['report']}\n")
                state[key] = _ts
                state_changed = True
            continue
        if force or key not in state:
            msg = request_message(cand, lint)
            if notify_dan(msg, instance=cand["instance"]) or force:
                sent += 1
                state[key] = _ts
                state_changed = True
            else:
                # Telegram unreachable → the candidate waits; CLI still works
                logger.info("[Tools] %s waits in the queue (notify failed)",
                            cand["name"])
    if state_changed:
        _save_notified(state)
    return sent


def _find_candidate(name: str) -> dict | None:
    for cand in scan_candidates():
        if cand["name"] == name:
            return cand
    return None




def handle_telegram_command(text: str) -> str:
    """The bridge's /approve /reject /toollist handler (toolplan stage 4,
    REVISED 09-27: the review channel is the designer's chat with a resident — the
    BRIDGE intercepts these commands before any agent turn, so she never
    sees them). Returns the reply text. Parse errors answer honestly."""
    if not enabled():
        return ("[SYSTEM] the tools review gate is switched off "
                "(CONTINUA_TOOLREVIEW=0)")
    parts = text.strip().split()
    cmd = parts[0].lstrip("/").lower() if parts else ""
    if cmd == "toollist":
        cands = scan_candidates()
        if not cands:
            return ("[SYSTEM] the candidate queue is empty — no tools "
                    "waiting for review")
        lines = []
        for c in cands:
            lint = tl.lint_candidate(c["code"], c["manifest"],
                                     HOUSE_TOOL_NAMES)
            lines.append(f"- {c['instance']}/{c['name']} "
                         f"hash={c['hash']} lint="
                         f"{'OK' if lint['ok'] else 'FAILED'}")
        return "[SYSTEM] waiting for review:\n" + "\n".join(lines)
    if cmd in ("approve", "reject"):
        if len(parts) < 2:
            return (f"[SYSTEM] /{cmd} <name> [hash or reason] — e.g. "
                    f"/{cmd} my_wordkeeper")
        name = parts[1]
        rest = " ".join(parts[2:])
        cand = _find_candidate(name)
        if not cand:
            return (f"[SYSTEM] no candidate named {name} in any desk "
                    f"queue — /toollist shows what is waiting")
        if cmd == "approve":
            if not rest:
                return (f"[SYSTEM] {name} needs the version hash from the "
                        f"request message (hash-lock: no approving a "
                        f"moving target) — /toollist shows it")
            return approve(cand, rest.strip())
        return reject(cand, rest)
    return (f"[SYSTEM] unknown tool command '{cmd}' — /toollist, "
            f"/approve <name> <hash>, /reject <name> <reason>")


def _run(argv: list) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="tools_review — her tools, "
                                             "reviewed before they run")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--notify", action="store_true",
                    help="scan + lint + send requests to the designer")
    ap.add_argument("--approve", nargs="+", metavar=("NAME", "HASH"))
    ap.add_argument("--reject", nargs="+", metavar=("NAME", "REASON"))
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args(argv)

    if a.list:
        cands = scan_candidates()
        if not cands:
            print("(the queue is empty — no candidates in any desk)")
        for c in cands:
            lint = tl.lint_candidate(c["code"], c["manifest"],
                                     HOUSE_TOOL_NAMES)
            print(f"{c['instance']}/{c['name']} hash={c['hash']} "
                  f"lint={'OK' if lint['ok'] else 'FAILED'}")
        return
    if a.notify:
        n = notify_all()
        print(f"[Tools] {n} request(s) sent to the designer")
        return
    if a.approve:
        name = a.approve[0]
        h = a.approve[1] if len(a.approve) > 1 else ""
        cand = _find_candidate(name)
        if not cand:
            print(f"[Tools] no candidate named {name} in any desk queue")
            return
        print(approve(cand, h))
        return
    if a.reject:
        name = a.reject[0]
        reason = " ".join(a.reject[1:]) if len(a.reject) > 1 else ""
        cand = _find_candidate(name)
        if not cand:
            print(f"[Tools] no candidate named {name} in any desk queue")
            return
        print(reject(cand, reason))
        return
    if a.watch:
        watch_for_requests()
        return
    ap.print_help()


def main():
    _run(sys.argv[1:])


if __name__ == "__main__":
    main()