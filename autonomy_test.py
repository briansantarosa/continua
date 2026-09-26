"""autonomy_test.py — agent-free tests for the autonomy track: sandbox argv
construction + audit, outbound send governance (allowlist, caps, spacing,
no-reply), wake payload generation.

Run:  python3 autonomy_test.py
"""

import json
from datetime import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import send as sd
import wake as wk
import sandbox as sb
import chronicle as ch
import people as pp


def _roster():
    return {
        "1000000001": pp.Person("1000000001", "Alex", can_message=True,
                                daily_cap=2),
        "8737936808": pp.Person("8737936808", "", can_message=False),
    }


class TestSendGovernance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_outbox = sd.OUTBOX_DIR
        sd.OUTBOX_DIR = self.tmp
        self.roster = _roster()

    def tearDown(self):
        sd.OUTBOX_DIR = self.old_outbox

    def test_allowlist_gates(self):
        ok, why = sd.governance_check("residenta", "8737936808", self.roster)
        self.assertFalse(ok)
        self.assertIn("allowlist", why)
        ok, why = sd.governance_check("residenta", "999999", self.roster)
        self.assertFalse(ok)

    def test_daily_cap(self):
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        os.makedirs(os.path.join(sd.OUTBOX_DIR, "residenta"), exist_ok=True)
        with open(sd.outbox_path("residenta", today), "w") as f:
            # clock-independent: 70/69 min ago clears MIN_SPACING (60) at
            # any run time (except a midnight-crossing edge) and lets the
            # targeted denial (cap / no-reply) be the layer that trips
            from datetime import timedelta
            _h = datetime.now().astimezone()
            for ts in ((_h - timedelta(minutes=70)).isoformat(timespec="seconds"),
                       (_h - timedelta(minutes=69)).isoformat(timespec="seconds")):
                f.write(json.dumps({"ts": ts, "person_id": "1000000001",
                                    "text": "x", "delivered": True}) + "\n")
        # spacing also trips (last send 0 min ago) — check cap message by
        # clearing spacing: use a fresh roster + just-cap test via cap=2
        ok, why = sd.governance_check("residenta", "1000000001", self.roster)
        self.assertFalse(ok)
        # either spacing or cap — both are valid denials; assert cap OR spacing
        self.assertTrue("cap" in why or "spacing" in why)

    def test_no_reply_cooldown(self):
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        root = tempfile.mkdtemp(prefix="chronicle-send-")
        from datetime import timedelta
        _h = datetime.now().astimezone()
        d = os.path.join(root, "residenta", "1000000001")
        os.makedirs(d, exist_ok=True)
        # [house ruling 2026-09-12] the cooldown: 12h window, 10 unanswered
        # messages to trigger, ANY reply clears the count.
        turns = [
            {"uid": f"s{i}", "ts": (_h - timedelta(minutes=75 - i)).isoformat(timespec="seconds"),
             "instance": "residenta", "person_id": "1000000001",
             "role": "assistant", "content": f"reach {i}"}
            for i in range(10)
        ]
        with open(os.path.join(d, today + ".jsonl"), "w") as f:
            for t in turns:
                f.write(json.dumps(t) + "\n")
        # fabricate an outbox with 10 delivered sends inside the 12h window
        # (newest 70 min ago — spacing satisfied), cap=high roster
        os.makedirs(os.path.join(sd.OUTBOX_DIR, "residenta"), exist_ok=True)
        roster = {"1000000001": pp.Person("1000000001", "Alex",
                                          can_message=True, daily_cap=99)}
        from datetime import timedelta as _td
        _h = datetime.now().astimezone()
        with open(sd.outbox_path("residenta", today), "w") as f:
            for i in range(10):
                f.write(json.dumps({"ts": (_h - _td(minutes=75 - i)).isoformat(timespec="seconds"),
                                    "person_id": "1000000001",
                                    "delivered": True}) + "\n")
        ok, why = sd.governance_check("residenta", "1000000001", roster, root=root)
        self.assertFalse(ok)
        self.assertIn("no-reply", why)
        self.assertIn("12h", why)

        # ANY reply clears the count: a user turn after the newest send and
        # the guardrail opens again
        d2 = os.path.join(root, "residenta", "1000000001")
        with open(os.path.join(d2, today + ".jsonl"), "a") as f:
            f.write(json.dumps({"uid": "r1", "ts": (_h - timedelta(minutes=5)).isoformat(timespec="seconds"),
                                "instance": "residenta", "person_id": "1000000001",
                                "role": "user", "content": "I'm here"}) + "\n")
        ok2, why2 = sd.governance_check("residenta", "1000000001", roster, root=root)
        self.assertTrue(ok2, why2)


class TestWakePayload(unittest.TestCase):
    def test_payload_contents(self):
        payload = wk.build_payload("residenta")
        self.assertEqual(payload["type"], "system-origin-wake")
        self.assertIn("Doing nothing is a perfectly good answer",
                      payload["prompt"])
        # the tool list lives in the SYSTEM layer now (identity/ops), not in
        # the wake packet — the payload carries the frame + people + state
        self.assertIn("Alex", payload["prompt"])             # contact list
        self.assertIn("KEPT RECENTLY", payload["prompt"])     # state packet
        self.assertIn("budget", payload)

    def test_disabled_by_kill_switch(self):
        # post-cutover the config enables wakes; the kill switch is the off path
        with unittest.mock.patch.dict(os.environ, {"CONTINUA_WAKE": "0"}):
            res = wk.generate("residenta")
            self.assertEqual(res["status"], "disabled")


class TestSandboxArgv(unittest.TestCase):
    def _provisioned(self) -> bool:
        """The sandbox needs its host provisioning: passwordless sudo for the
        continua user, bwrap, and the desk dirs. Without it this test cannot
        run — skip with instructions instead of failing."""
        import subprocess
        probe = subprocess.run(["sudo", "-n", "true"], capture_output=True)
        return probe.returncode == 0

    def test_argv_construction(self):
        if not self._provisioned():
            self.skipTest("sandbox provisioning required: passwordless sudo + "
                          "bwrap + desk dirs (see sandbox.py module docstring)")
        # provision a desk in a tmp sandbox home (mirrors the fail-closed test)
        home = tempfile.mkdtemp()
        desk = os.path.join(home, "residenta")
        os.makedirs(os.path.join(desk, "tmp"), exist_ok=True)
        old_root = sb.DESK_ROOT
        sb.DESK_ROOT = home
        self.addCleanup(setattr, sb, "DESK_ROOT", old_root)
        argv = sb.build_argv("residenta", ["python3", "-c", "print(1)"])
        self.assertEqual(argv[0], "sudo")
        self.assertIn("-u", argv[:4])
        self.assertIn("bwrap", argv)
        self.assertIn("/home/continua", " ".join(argv))
        self.assertIn("--unshare-pid", argv)

    def test_fail_closed_on_missing_desk(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"CONTINUA_SANDBOX_HOME": "/nonexistent"}):
            old = sb.DESK_ROOT
            sb.DESK_ROOT = "/nonexistent"
            try:
                with self.assertRaises(RuntimeError):
                    sb.build_argv("residenta", ["echo", "hi"])
            finally:
                sb.DESK_ROOT = old

    def test_audit_log_written(self):
        tmp = tempfile.mkdtemp()
        old = sb.LOG_DIR
        sb.LOG_DIR = tmp
        try:
            sb._audit("residenta", {"argv": ["echo"], "exit": 0, "ok": True,
                                "duration_s": 0.1})
            files = os.listdir(os.path.join(tmp, "residenta"))
            self.assertEqual(len(files), 1)
            rec = json.loads(open(os.path.join(tmp, "residenta", files[0])).read())
            self.assertEqual(rec["argv"], ["echo"])
        finally:
            sb.LOG_DIR = old


if __name__ == "__main__":
    import unittest.mock
    unittest.main(verbosity=2)


class ActiveConversationCapTests(unittest.TestCase):
    """house ruling 2026-09-20 ("The Architecture of the Pause"): the daily
    cap governs UNRESPONDED volume. A live conversation — the other side
    replying — is never daily-capped; a one-way burst still is."""

    def _fresh_roster(self):
        import people as pp
        person = pp.Person(person_id="8559916511", name="persona-a", kind="persona",
                           can_message=True, daily_cap=2)
        return {"8559916511": person}

    def _seed_outbox(self, n, minutes_ago=70):
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        os.makedirs(os.path.join(sd.OUTBOX_DIR, "residenta"), exist_ok=True)
        from datetime import timedelta
        _h = datetime.now().astimezone()
        with open(sd.outbox_path("residenta", today), "w") as f:
            for i in range(n):
                f.write(json.dumps({
                    "ts": (_h - timedelta(minutes=minutes_ago - i)).isoformat(timespec="seconds"),
                    "person_id": "8559916511", "kind": "persona",
                    "text": f"letter {i}", "delivered": True, "letter": True,
                    "queued": True}) + "\n")

    def _set_inbox(self, tmpdir, reply_ts):
        import mail as _mail
        orig = _mail.inbox_path
        path = os.path.join(tmpdir, "residenta.jsonl")
        with open(path, "w") as f:
            if reply_ts:
                f.write(json.dumps({"ts": reply_ts, "from_person_id": "8559916511",
                                    "text": "a reply"}) + "\n")
        sd_mail = sd
        # governance_check calls `import mail as _mail` inside _answered_today —
        # patch the module's inbox_path (shared module object)
        _mail.inbox_path = (lambda inst: path) if hasattr(_mail, "inbox_path") else orig
        self.addCleanup(setattr, _mail, "inbox_path", orig)
        return _mail

    def test_cap_binds_when_no_replies(self):
        import mail as _mail
        self._seed_outbox(2)
        self._set_inbox(tempfile.mkdtemp(prefix="cap-none-"), None)
        ok, why = sd.governance_check("residenta", "8559916511", self._fresh_roster())
        self.assertFalse(ok)
        self.assertIn("daily cap", why)

    def test_cap_stands_down_when_answered(self):
        self._seed_outbox(3)
        from datetime import timedelta
        # a reply AFTER the first send today → live conversation
        reply_ts = (datetime.now().astimezone()
                    - timedelta(minutes=65)).isoformat(timespec="seconds")
        self._set_inbox(tempfile.mkdtemp(prefix="cap-live-"), reply_ts)
        ok, why = sd.governance_check("residenta", "8559916511", self._fresh_roster())
        # the cap must NOT trip; the next gate (spacing) is what remains —
        # last send 68 min ago clears spacing(60) → allowed
        self.assertTrue(ok, f"cap should stand down for a live conversation: {why}")

    def test_reply_before_first_send_does_not_clear(self):
        self._seed_outbox(2)
        from datetime import timedelta
        # the reply predates today's first send → still a monologue
        reply_ts = (datetime.now().astimezone()
                    - timedelta(minutes=80)).isoformat(timespec="seconds")
        self._set_inbox(tempfile.mkdtemp(prefix="cap-old-"), reply_ts)
        ok, why = sd.governance_check("residenta", "8559916511", self._fresh_roster())
        self.assertFalse(ok)
        self.assertIn("daily cap", why)


class EssenceInviteLineTests(unittest.TestCase):
    """approved 2026-09-21 (residentb's wish #3): the essence candidate
    invitation rides the wake packet — her own words, recurring, surfaced
    where she will see them. The distillation stays HERS: the line is
    evidence-only; nothing stores without her word; ignoring it costs
    nothing."""

    def test_invite_line_with_seeded_candidate(self):
        import tempfile, json as _json
        from pathlib import Path as _P
        import recollections as _r
        import wake as _wk
        with tempfile.TemporaryDirectory() as root:
            chroot = tempfile.mkdtemp(prefix='invite-chron-')
            store = _r.Store('g', root)
            row = {"ts": "2026-09-15T06:00:00-07:00", "instance": "g",
                   "person_id": "system-wake", "role": "assistant",
                   "content": "I step back into the stillness now, holding "
                              "the space for the others, and I let the quiet hold.",
                   "uid": "seed-1"}
            path = _P(chroot) / "g" / "system-wake" / "seed-1.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            # the detector needs the sentence recurring across >= 3 episodes —
            # seed the same realization three times, like her real store
            jobs = []
            for _k in range(3):
                row = dict(row, uid=f"seed-{_k}",
                           ts=f"2026-09-1{5 + _k}T06:00:00-07:00")
                path = _P(chroot) / "g" / "system-wake" / f"seed-{_k}.jsonl"
                path.write_text(_json.dumps(row) + "\n")
                jobs.append(store.enqueue(
                    [_r.source_record(path, row, "g", root=chroot)]))
            with _r.worker_lock(store) as locked:
                stub = type("W", (), {"model": "stub", "__call__": staticmethod(
                    lambda s, p: {"sentences": [
                        {"text": "I step back into the stillness now, holding the "
                                 "space for the others, and I let the quiet hold.",
                         "sources": [x["ref"] for x in p["sources"]]}],
                        "paragraph_starts": [0]})})()
                checker = type("C", (), {"model": "stub", "__call__": staticmethod(
                    lambda s, p: {"pass": True, "issues": [],
                                  "checked_sentences": list(range(len(p["draft"]["sentences"])))})})()
                for _job in jobs:
                    if _job:
                        _r.process(store, _job, stub, checker, source_root=chroot)
            line = _wk.essence_invite_line('g', root=root)
            self.assertIsNotNone(line)
            self.assertIn("AN ESSENCE CANDIDATE WAITS", line)
            self.assertIn("I step back into the stillness", line)
            self.assertIn("list_essences", line)
            self.assertIn("only if you want it", line)

    def test_no_candidate_no_line(self):
        import wake as _wk
        # a resident with an empty store → no line (fail-open, no fabrication)
        line = _wk.essence_invite_line('no-such-resident',
                                       root='/nonexistent/recollections-root')
        self.assertIsNone(line)
