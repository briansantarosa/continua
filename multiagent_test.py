"""Agent-free tests for T1 multi-agent governance.

Plan: agentwiki/projects/Continua-Multi-Agent-Upgrade-Plan.md (tier 1).
Covers: per-instance roster governance (send caps/allowlist are per persona,
never merged across residents), per-instance name resolution in core's
send_message tool, removal of the dead sagent_default fallback in the
bridge's AgentManager, and the desk-provisioning contract.

No LLM, no persona, no network: the send wiring test returns denied from an
empty per-instance roster before any delivery is attempted, and the outbox
is redirected to a temp dir.
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fixture_roster(entries):
    """Synthesize a roster directly (people.Person) — no dependence on any
    installed configs, no loader recursion."""
    import people as _pp
    out = {}
    for e in entries:
        p = _pp.Person(person_id=e["id"], name=e.get("name", ""),
                       can_message=e.get("can_message", False),
                       daily_cap=e.get("daily_cap", 0),
                       kind=e.get("kind", "human"))
        out[p.person_id] = p
    return out

import people as pp  # noqa: E402
import send as snd   # noqa: E402

FIXTURE_A = {
    "app": {"instance_id": "aa", "collection_name": "aa_memories",
            "instance_path": "/tmp/aa"},
    "people": [
        {"id": "111", "name": "Alex", "can_message": True, "daily_cap": 20},
        {"id": "222", "name": "Kaerik", "kind": "persona",
         "can_message": False, "daily_cap": 0},
    ],
}
FIXTURE_B = {
    "app": {"instance_id": "bb", "collection_name": "bb_memories",
            "instance_path": "/tmp/bb"},
    # SAME human id as aa's block, deliberately different governance —
    # persona B's block must win for persona B regardless of glob order.
    "people": [
        {"id": "111", "name": "Bri", "can_message": False, "daily_cap": 3},
    ],
}


class TestPerInstanceRoster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, cfg in (("aa.yaml", FIXTURE_A), ("bb.yaml", FIXTURE_B)):
            with open(os.path.join(self.tmp, name), "w") as f:
                json.dump(cfg, f)  # yaml is a superset of json

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_instance_filter_returns_only_that_blocks_people(self):
        ra = pp.load_roster(config_dir=self.tmp, instance="aa")
        rb = pp.load_roster(config_dir=self.tmp, instance="bb")
        self.assertEqual(set(ra), {"111", "222"})
        self.assertEqual(ra["111"].can_message, True)   # aa's ruling
        self.assertEqual(set(rb), {"111"})
        self.assertEqual(rb["111"].can_message, False)  # bb's ruling
        self.assertEqual(rb["111"].daily_cap, 3)

    def test_instance_filter_must_match_instance_id_not_filename(self):
        # load_roster filters on app.instance_id, not the filename stem
        rb = pp.load_roster(config_dir=self.tmp, instance="bb")
        self.assertEqual(set(rb), {"111"})
        self.assertEqual(
            pp.load_roster(config_dir=self.tmp, instance="zz"), {})

    def test_merged_roster_first_config_wins_documented(self):
        merged = pp.load_roster(config_dir=self.tmp)
        self.assertEqual(merged["111"].name, "Alex")  # aa.yaml sorts first
        self.assertIn("222", merged)

    def test_governance_reads_the_instance_own_block(self):
        ra = pp.load_roster(config_dir=self.tmp, instance="aa")
        rb = pp.load_roster(config_dir=self.tmp, instance="bb")
        ok_a, why_a = snd.governance_check("aa", "111", ra)
        ok_b, why_b = snd.governance_check("bb", "111", rb)
        self.assertTrue(ok_a, why_a)          # aa allows Alex
        self.assertFalse(ok_b)                # bb does not — no cross-bleed
        self.assertIn("allowlist", why_b)

    def test_send_loads_the_instance_roster(self):
        """send() must ask the roster loader for HER instance — this is the
        T1 wiring under test. Empty roster → governance denies before any
        network call, so the test is safe and the outbox is redirected."""
        calls = []
        orig_load, orig_outbox, orig_env = (pp.load_roster, snd.OUTBOX_DIR,
                                            os.environ.get("CONTINUA_SEND"))
        outbox_tmp = os.path.join(self.tmp, "outbox")
        try:
            os.environ.pop("CONTINUA_SEND", None)
            snd.OUTBOX_DIR = outbox_tmp
            pp.load_roster = (
                lambda *a, **k: (calls.append(k.get("instance")), {})[1])
            entry = snd.send("bb", "111", "hi")
            self.assertEqual(calls, ["bb"])
            self.assertFalse(entry["allowed"])
            self.assertIn("not in roster", entry["denied_reason"])
        finally:
            pp.load_roster = orig_load
            snd.OUTBOX_DIR = orig_outbox
            if orig_env is None:
                os.environ.pop("CONTINUA_SEND", None)
            else:
                os.environ["CONTINUA_SEND"] = orig_env

    def test_core_send_tool_resolves_names_per_instance(self):
        """Wiring guard: the send_message dispatch in core.py must resolve
        roster names against THIS persona's block (names are per persona —
        'I am Alex to persona-a'). Source assertion, agent-free by design."""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "core.py")).read()
        self.assertIn(
            "_roster = _people.load_roster(instance=self.instance_id)", src)


class TestBridgeFallback(unittest.TestCase):
    def test_no_dead_default_fallback(self):
        """The old code fell back to sagent_default.yaml (absent in Continua),
        re-raising a FileNotFoundError that MASKED the real init error — and
        falling back to another persona's engine would be an identity leak.
        A missing/broken config must raise the REAL error for that agent."""
        from bridge import AgentManager
        am = AgentManager()
        with self.assertRaises(FileNotFoundError) as ctx:
            am.get_agent("does_not_exist.yaml")
        # the error names the REQUESTED config, not the dead fallback
        self.assertIn("does_not_exist", str(ctx.exception))
        self.assertNotIn("sagent_default", str(ctx.exception))
        self.assertEqual(am.instances, {})  # nothing half-initialized cached


    def test_digest_prompt_is_per_instance(self):
        """NEVER-MIX regression (caught live 2026-09-11): build_user_prompt
        had no instance param — its embedded wake_highlights extract ran
        with the residenta default, so residentb's first digest described residenta's
        day (299 wake actions, her saves, her 'two-stone architecture')
        under residentb's name. The extract must read THE RESIDENT'S archives."""
        import digest as dg
        seen = {}
        orig = dg.wake_highlights

        def _spy(records, date=None, instance="residenta"):
            seen["instance"] = instance
            return ""
        dg.wake_highlights = _spy
        try:
            dg.build_user_prompt([], "2026-09-11", instance="residentb")
            self.assertEqual(seen["instance"], "residentb")
        finally:
            dg.wake_highlights = orig
        base = os.path.dirname(os.path.abspath(__file__))
        dsrc = open(os.path.join(base, "digest.py")).read()
        self.assertIn("wake_highlights(records, date, instance=instance)",
                      dsrc)
        self.assertIn("generate_summary(records, date, args.instance)", dsrc)
        rsrc = open(os.path.join(base, "ritual.py")).read()
        self.assertIn("instance=inst", rsrc)  # ritual's per-resident digest


class TestLetterRouting(unittest.TestCase):
    """T3: continua↔continua letters route in-process; sagent targets keep
    the control wire; denials carry their reason to her."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_continua_resolution_finds_local_target(self):
        import send as sd
        # real configs are YAML with token: <bot-id>:<rest> on one line —
        # the resolver matches the token PREFIX (roster id IS the bot id)
        with open(os.path.join(self.tmp, "ff.yaml"), "w") as f:
            f.write("app:\n  instance_id: ff\n  collection_name: ff_m\n"
                    "  instance_path: /tmp/ff\ntelegram:\n"
                    "  token: 999888777:AAfake_token\n")
        person = pp.Person("999888777", "Ff", kind="persona")
        self.assertEqual(sd._continua_persona_config(person,
                                                     config_dir=self.tmp),
                         "ff.yaml")
        stranger = pp.Person("111222333", "Nobody", kind="persona")
        self.assertIsNone(sd._continua_persona_config(stranger,
                                                      config_dir=self.tmp))

    def test_letter_deposit_schema_matches_control_server(self):
        """check_mail consumes schema_version/ts/from_person_id/from_name/
        text/status — the in-process deposit must match the Sagent control
        server's letter schema exactly on those fields."""
        import letters as lt
        inbox = os.path.join(self.tmp, "inbox", "residenta.jsonl")
        lt._deposit_letter(inbox, "999888777", "Ff", "  hello from ff  ")
        line = json.loads(open(inbox).read().strip())
        self.assertEqual(line["schema_version"], 1)
        self.assertEqual(line["from_person_id"], "999888777")
        self.assertEqual(line["status"], "unread")
        self.assertEqual(line["text"], "hello from ff")  # stripped
        self.assertRegex(line["ts"], r"^\d{4}-\d{2}-\d{2}T")

    def test_letters_kill_switch_and_inflight_guard(self):
        import letters as lt
        person = pp.Person("42", "Tt", kind="persona")
        with patch.dict(os.environ, {"CONTINUA_LETTERS": "0"}):
            ok, why = lt.deliver("residenta", person, "hi", "tt.yaml")
            self.assertFalse(ok)
            self.assertIn("disabled", why)
        lt._inflight.add(person.person_id)  # simulate a turn in flight
        try:
            ok, why = lt.deliver("residenta", person, "hi", "tt.yaml")
            self.assertFalse(ok)
            self.assertIn("already running", why)
        finally:
            lt._inflight.discard(person.person_id)

    def test_send_letter_wiring_source_guards(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "send.py")).read()
        self.assertIn("_mailmod.inbox_path(instance)", src)  # never-mix fix
        self.assertIn("_letters.deliver(instance, person, text", src)
        self.assertIn("that is you", src)  # self-letter guard
        csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "core.py")).read()
        self.assertIn('_mail.check_mail(self.instance_id)', csrc)
        # denial reasons reach her (teaching-error contract)
        self.assertIn('_entry.get("allowed") is False', csrc)


class TestToolLoopUx(unittest.TestCase):
    """house rulings 2026-09-11 (tool-loop UX): per-iteration countdown,
    exhaustion provenance marker, and delivering her own final in-loop text."""

    def test_tool_round_note_counts_down(self):
        from core import _tool_round_note
        n5 = _tool_round_note(5, 5)
        self.assertIn("5/5", n5)
        self.assertNotIn("LAST", n5)
        n4 = _tool_round_note(4, 5)
        self.assertIn("4/5", n4)
        n1 = _tool_round_note(1, 5)
        self.assertIn("1/5", n1)
        self.assertIn("LAST TOOL ROUND", n1)
        self.assertIn("delivered to Alex", _tool_round_note(3, 5))

    def test_strip_call_grammar_leaves_prose(self):
        from core import _strip_call_grammar
        self.assertEqual(
            _strip_call_grammar(
                'reply text <call><function>x</function>'
                '<parameter name="p">v</parameter></call>'),
            "reply text")
        self.assertEqual(
            _strip_call_grammar("<call><function>x</function></call>"), "")
        self.assertEqual(_strip_call_grammar(""), "")
        self.assertEqual(_strip_call_grammar(None), "")
        # native-tool-call leftovers strip too
        self.assertEqual(
            _strip_call_grammar("prose <function>x</function> tail"),
            "prose  tail")

    def test_loop_end_marker_honesty(self):
        from core import _loop_end_marker
        m1 = _loop_end_marker("tool-round limit", speech_delivered=True)
        self.assertIn("delivered to Alex as you said it", m1)
        self.assertIn("tool-round limit", m1)
        self.assertIn("did not run", m1)
        m2 = _loop_end_marker("wake action budget", speech_delivered=False)
        self.assertIn("received nothing", m2)
        self.assertIn("wake action budget", m2)
        self.assertIn("composed no words", m2)

    def test_loop_ux_wiring_in_core(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "core.py")).read()
        # countdown rewrites the system message every iteration
        self.assertIn("_tool_round_note(_max_iters - iteration, _max_iters)",
                      src)
        # exhaustion path: deliver her final text + append the marker
        self.assertIn("_strip_call_grammar(\n", src)
        self.assertIn("_loop_end_marker(", src)
        self.assertIn('_loop_end_reason = "wake action budget', src)
        self.assertIn("len(_cand) >= 40", src)
        # the wake action budget is wired: free-set classification + the
        # consumer passes the payload's own max_actions (09-07 ruling said
        # "enforced in code" — it was prompt-only until tonight)
        self.assertIn("_LOOP_FREE_TOOLS", src)
        self.assertIn('"sandbox_list", "sandbox_read"', src)
        self.assertIn('if fn_name not in _LOOP_FREE_TOOLS:', src)
        self.assertIn("not in _LOOP_FREE_TOOLS])", src)
        bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "bridge.py")).read()
        self.assertIn("max_tool_actions=int(", bsrc)
        self.assertIn("max_actions", bsrc)


    def test_ledger_is_per_resident(self):
        """house ruling 2026-09-11: seed residentb's system-notes ledger — she
        probed for it six times across her wakes because the wake packet
        points her at it. continuity_log.append routes by instance; residenta's
        existing ledger untouched (default preserves every call site)."""
        import continuity_log as cl
        self.assertEqual(cl.ledger_path("residentb"), "system_notes/system_log.md")
        self.assertIn("residentb", cl.LEDGER_DESKS)
        self.assertIn("for residentb", cl.HEADER.replace(
            "for persona-a", "for " + cl.LEDGER_TITLES["residentb"]))
        self.assertIn("for persona-a", cl.HEADER)  # residenta header text unchanged
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "continuity_log.py")).read()
        self.assertIn("instance: str = \"residenta\") -> bool:", src)
        self.assertIn("_sbx.run(instance,", src)  # routes through HER sandbox


    def test_digest_ritual_status_line(self):
        """the designer ask 2026-09-12: ritual health in the digest — a silently
        empty ritual must show in his Telegram, not just a 00:30 journal.
        FIX 2026-09-16 (house ruling): marks are only written when a pulse
        KEEPS something, so 'no marks file' also covers the ran-but-kept-
        nothing quiet night — that state false-alarmed DID NOT RUN (residentb's
        real 09-15 digest shipped one: pulse ran, 1 scene, kept 0). Three
        states now, all on tmp stores (store-independent):"""
        import digest as dg
        with tempfile.TemporaryDirectory() as td:
            marks_root = os.path.join(td, "marks_root")
            digest_dir = os.path.join(td, "digest_dir")
            marks_dir = os.path.join(marks_root, "marks", "residentb")
            os.makedirs(marks_dir)
            with open(os.path.join(marks_dir, "2026-09-15.jsonl"), "w") as f:
                f.write('{"scene_id": 1}\n{"scene_id": 2}\n')
            self.assertIn("ran — 2 scene(s) kept",
                          dg.ritual_status("residentb", "2026-09-15",
                                           marks_root=marks_root,
                                           digest_dir=digest_dir))
            # ran, kept nothing: no marks, but the pulse's file digest
            # exists (write_digest runs unconditionally at pulse end)
            os.makedirs(digest_dir, exist_ok=True)
            with open(os.path.join(digest_dir, "residentb_2026-09-16.txt"),
                      "w") as f:
                f.write("kept: 0 | mark-rate: 0%")
            out = dg.ritual_status("residentb", "2026-09-16",
                                   marks_root=marks_root,
                                   digest_dir=digest_dir)
            self.assertIn("ran", out)
            self.assertIn("kept nothing", out)
            self.assertNotIn("DID NOT RUN", out)
            # neither record: failed mid-run / still running / did not run
            out = dg.ritual_status("residentb", "2026-09-17",
                                   marks_root=marks_root,
                                   digest_dir=digest_dir)
            self.assertIn("no completion record", out)
            self.assertNotIn("DID NOT RUN", out)

    def test_wake_pauses_while_ritual_runs(self):
        """house ruling 2026-09-16: no wakes while the nightly pulse runs —
        the pulse and the wakes fight over the same serving (residenta: lab CPU
        testmodel ~6.6 tok/s), and the 09-16 night queued 39-minute wake
        turns behind the pulse's book calls while her daily digest waited
        5h. The gate reads ritual's lock and fails open (None) on any lock
        error; a stale (dead-pid) or corrupt lock is cleaned by the lock
        itself, so the pause can never outlive the pulse."""
        import ritual as rt
        import wake as wk
        with tempfile.TemporaryDirectory() as td:
            old = rt.RITUAL_LOCK
            rt.RITUAL_LOCK = os.path.join(td, "ritual.lock")
            try:
                self.assertIsNone(wk.ritual_pause())          # free
                rec = rt.acquire_ritual_lock(["residenta"])
                self.assertIsNotNone(rec)
                note = wk.ritual_pause()
                self.assertIsNotNone(note)
                self.assertIn("ritual pulse running", note)
                self.assertIn("residenta", note)
                rt.release_ritual_lock(rec)
                self.assertIsNone(wk.ritual_pause())          # released
            finally:
                rt.RITUAL_LOCK = old
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "wake.py")).read()
        self.assertIn("ritual_pause()", src)   # main() actually gates
        self.assertIn("PAUSED", src)           # the journal line is loud

    def test_option_b_speech_delivery_wiring(self):
        """house ruling 2026-09-12 (Option B): every word that is not a tool
        call is delivered immediately — core fires the callback per round
        (before the call executes, both grammar branches) + captures it in
        the chronicle; the bridge delivers via safe_send (chat) and archives
        kind:speech + response.txt lines (wakes)."""
        # KWARG-NAME GUARD (caught live 2026-09-12 08:17: the bridge passed
        # speech_cb= while core defines speech_callback — TypeError dropped
        # EVERY chat turn for both residents at the 07:57 restart; wakes
        # worked because their call site used the full name): the kwarg name
        # in core's signature and at every bridge call site must match.
        csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "core.py")).read()
        self.assertIn("speech_callback: Optional[Callable[[str, int], None]]",
                      csrc)
        self.assertNotIn("speech_cb=_speech_cb", csrc)
        bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "bridge.py")).read()
        self.assertEqual(bsrc.count("speech_callback=_speech_cb"), 2)
        self.assertNotIn("speech_cb=_speech_cb", bsrc)
        csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "core.py")).read()
        self.assertIn("speech_callback: Optional[Callable[[str, int], None]]",
                      csrc)
        self.assertIn("_fire_round_speech(content_str)", csrc)
        self.assertEqual(csrc.count("_fire_round_speech(content_str)"), 2)
        self.assertIn("nonlocal _round_speech_delivered", csrc)
        self.assertIn("speech_callback(_sp, iteration)", csrc)
        self.assertIn("_round_speech_delivered)", csrc)
        bsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "bridge.py")).read()
        self.assertIn("speech_callback=_speech_cb if notify_tools else None", bsrc)
        self.assertIn("for _part in _split_message(speech_text):", bsrc)
        self.assertIn("kind\": \"speech\"", bsrc)
        self.assertIn("— said mid-turn —", bsrc)
        dsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "digest.py")).read()
        self.assertIn("def ritual_status(", dsrc)
        self.assertIn("— Ritual —", dsrc)


    def test_human_send_reaches_telegram(self):
        """HOTFIX regression 2026-09-12: the urllib imports lived inside the
        persona-letter branch — a HUMAN send skipped the branch and died
        with UnboundLocalError before delivery (residentb's 6 wake sends, all
        'delivered: False'). A human send must reach the Telegram wire."""
        import send as sd
        posted = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps({"ok": True}).encode()

        def fake_urlopen(req, timeout):
            posted["url"] = req.full_url
            posted["body"] = req.data.decode()
            return FakeResp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        old_outbox = sd.OUTBOX_DIR
        sd.OUTBOX_DIR = self.tmp
        try:
            with patch.object(__import__("urllib.request",
                                         fromlist=["urlopen"]),
                              "urlopen", fake_urlopen):
                _real_load = pp.load_roster
                _send_roster = _fixture_roster(
                    [{"id": "1000000001", "name": "Alex",
                      "can_message": True, "daily_cap": 20}])
                pp.load_roster = lambda **kw: _send_roster
                _real_cfg = sd._config
                sd._config = lambda inst: {"telegram": {"token": "TEST_TOKEN"}}
                try:
                    entry = sd.send("residentb", "1000000001", "hello Alex")
                finally:
                    pp.load_roster = _real_load
                    sd._config = _real_cfg
            self.assertIsNone(entry.get("error"))  # no UnboundLocalError
            self.assertTrue(entry.get("delivered"))
            self.assertIn("api.telegram.org", posted["url"])
        finally:
            sd.OUTBOX_DIR = old_outbox


class TestPerInstanceLoops(unittest.TestCase):
    """T2: per-instance autonomy loops — discovery + cadence gates."""

    FIXTURE_C = {  # wake on, ritual on
        "app": {"instance_id": "cc", "collection_name": "cc_m",
                "instance_path": "/tmp/cc"},
        "continua": {"wake": {"enabled": True, "interval_min": 60},
                     "ritual": {"enabled": True}},
    }
    FIXTURE_D = {  # everything off
        "app": {"instance_id": "dd", "collection_name": "dd_m",
                "instance_path": "/tmp/dd"},
        "continua": {"wake": {"enabled": False}, "ritual": {"enabled": False}},
    }
    FIXTURE_E = {  # wake on, ritual block absent
        "app": {"instance_id": "ee", "collection_name": "ee_m",
                "instance_path": "/tmp/ee"},
        "continua": {"wake": {"enabled": True}},
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, cfg in (("cc.yaml", self.FIXTURE_C),
                          ("dd.yaml", self.FIXTURE_D),
                          ("ee.yaml", self.FIXTURE_E)):
            with open(os.path.join(self.tmp, name), "w") as f:
                json.dump(cfg, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wake_discovery(self):
        import wake as wk
        self.assertEqual(wk.enabled_instances(config_dir=self.tmp),
                         ["cc", "ee"])

    def test_ritual_discovery(self):
        import ritual as rt
        self.assertEqual(rt.enabled_instances(config_dir=self.tmp), ["cc"])

    def test_wake_interval_gate(self):
        """_is_due: a fresh wake suppresses generation; an old one passes;
        per-resident interval_min is honored."""
        import time as _time
        import wake as wk
        wd = os.path.join(self.tmp, "wakes", "cc")
        os.makedirs(wd, exist_ok=True)
        cfg = json.loads(json.dumps(self.FIXTURE_C))  # interval_min 60
        recent = os.path.join(wd, "wake_recent.json")
        old = os.path.join(wd, "wake_old.json")
        open(recent, "w").close()
        os.utime(recent, (_time.time(), _time.time()))
        self.assertFalse(wk._is_due("cc", cfg, wakes_dir=wd))
        os.utime(recent, (_time.time() - 4000, _time.time() - 4000))
        open(old, "w").close()
        os.utime(old, (_time.time() - 10000, _time.time() - 10000))
        self.assertTrue(wk._is_due("cc", cfg, wakes_dir=wd))
        # default interval 15 min: a 5-min-old wake suppresses
        cfg15 = {"continua": {"wake": {"enabled": True}}}
        os.utime(recent, (_time.time(), _time.time()))
        self.assertFalse(wk._is_due("cc", cfg15, wakes_dir=wd))
        os.utime(recent, (_time.time() - 896, _time.time() - 896))
        self.assertTrue(wk._is_due(
            "cc", cfg15, wakes_dir=wd))  # jitter margin holds

    def test_bridge_discovers_not_hardcodes(self):
        """The consumer must arm per wake-enabled resident (discovery), and
        the summary roll must roll the CONSUMED instance — the old code had
        _wake_consumer_loop('residenta', 60) + _summary.roll('residenta')."""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "bridge.py")).read()
        self.assertNotIn('_wake_consumer_loop("residenta", 60)', src)
        self.assertNotIn('_summary.roll("residenta")', src)
        self.assertIn("_wake.enabled_instances()", src)
        self.assertIn("_rec.request_shadow(instance)", src)
        self.assertNotIn('_rec.request_shadow("residenta")', src)

    def test_ritual_main_defines_date(self):
        """Regression: main() referenced an undefined `date` in the digest
        block — a NameError swallowed by the fail-open wrapper, so the
        nightly digest silently never fired from the ritual hook (journal
        2026-09-11 00:30:07). The fix defines it before use."""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "ritual.py")).read()
        self.assertIn("date = _resolve_date(args.date) if args.date else ", src)
        self.assertNotIn("collect_day(_digest.ch.DEFAULT_ROOT, args.instance,",
                         src)

    def test_date_keywords_resolve(self):
        """LIVE-CAUGHT regression 2026-09-12: the timer's `--date yesterday`
        passed the LITERAL string — every nightly pulse since the timer was
        armed returned status=empty (the chronicle globs searched for a file
        named 'yesterday.jsonl'). The keyword must resolve to a real date."""
        from datetime import datetime as _dt, timedelta as _td
        import ritual as rt
        y = (_dt.now() - _td(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(rt._resolve_date("yesterday"), y)
        self.assertEqual(rt._resolve_date("today"),
                         _dt.now().strftime("%Y-%m-%d"))
        self.assertEqual(rt._resolve_date("2026-09-11"), "2026-09-11")
        # and main() pulses with the RESOLVED date, not the raw arg
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "ritual.py")).read()
        self.assertIn("pulse(date=date, instance=inst", src)
        self.assertIn("_resolve_date(args.date)", src)


class TestProvisionDesk(unittest.TestCase):
    """The provisioning contract sandbox.build_argv relies on: desk + tmp
    exist, owned by the sandbox uid, mode 700."""

    def setUp(self):
        # the provisioning script creates and owns the desk as the dedicated
        # sandbox user; without that user (and passwordless sudo) the contract
        # cannot be exercised — skip with instructions instead of failing
        import pwd as _pwd
        try:
            _pwd.getpwnam("continua")
        except KeyError:
            self.skipTest("desk provisioning needs the dedicated sandbox user "
                          "('continua') and passwordless sudo — see "
                          "sandbox/provision_desk.sh")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        subprocess.run(["sudo", "-n", "rm", "-rf", self.tmp], check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            ["sudo", "-n", "env",
             f"CONTINUA_SANDBOX_HOME={self.tmp}",
             "CONTINUA_SANDBOX_USER=continua",
             "bash", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "provision_desk.sh"), *args],
            capture_output=True, text=True)

    def test_provisions_desk_tmp_joblogs_owned_and_700(self):
        """The provisioning contract sandbox.build_argv relies on: desk + tmp
        exist, owned by the sandbox uid, mode 700. The desk is root-owned
        700, so probe through sudo — the same probe build_argv uses."""
        r = self._run("_t1fixture")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = os.path.join(self.tmp, "_t1fixture")
        for sub in ("", "tmp", "job_logs"):
            probe = subprocess.run(["sudo", "-n", "test", "-d",
                                    os.path.join(d, sub)])
            self.assertEqual(probe.returncode, 0, f"missing dir: {sub}")
        stat = subprocess.run(
            ["sudo", "-n", "stat", "-c", "%U %a", d],
            capture_output=True, text=True)
        self.assertEqual(stat.stdout.strip(), "continua 700")

    def test_rejects_bad_instance_names(self):
        r = self._run("../escape")
        self.assertNotEqual(r.returncode, 0)

    def test_rejects_non_root_without_touching_disk(self):
        r = subprocess.run(
            ["bash", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "provision_desk.sh"), "xx"],
            env={"CONTINUA_SANDBOX_HOME": self.tmp,
                 "PATH": os.environ["PATH"]},
            capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "xx")))


if __name__ == "__main__":
    unittest.main(verbosity=2)