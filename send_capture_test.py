"""send_capture_test.py — memorybuild.md Phase 1 (W4): a delivered human
send joins the conversation it belongs to. Synthetic outboxes/histories in
a tmp dir; the real chronicle module writes into the tmp root. Run:
  python3 send_capture_test.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import send_capture as sc     # noqa: E402
import chronicle as ch        # noqa: E402

SEQ = "2220002222"
Alex = "1000000001"
TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")


def _world():
    td = tempfile.mkdtemp(prefix="sendcap-")
    os.makedirs(os.path.join(td, "outbound", "residentb"), exist_ok=True)
    os.makedirs(os.path.join(td, "chronicle", "residentb", SEQ), exist_ok=True)
    os.makedirs(os.path.join(td, "wakes", "residentb"), exist_ok=True)
    return td


def _orow(base, pid, ts, text, delivered=True, kind="human"):
    p = os.path.join(base, "outbound", "residentb", f"{ts[:10]}.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "instance": "residentb", "person_id": pid,
                            "kind": kind, "text": text,
                            "delivered": delivered}) + "\n")


def _chron_rows(base, pid, day=TODAY):
    p = os.path.join(base, "chronicle", "residentb", pid, f"{day}.jsonl")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


class ReconcileTests(unittest.TestCase):
    def test_captures_chronicle_and_history_with_wake_prefix(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T18:49:09-07:00",
              "No bulb. That narrows it down.")
        n = sc.reconcile("residentb", "residentb.yaml", from_wake=True, base=base)
        self.assertEqual(n, 1)
        rows = _chron_rows(base, SEQ)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "assistant")
        self.assertEqual(rows[0]["content"], "No bulb. That narrows it down.")
        self.assertTrue(rows[0]["uid"].startswith("send-capture-"))
        self.assertEqual(rows[0]["source"], "send_message")
        h = json.load(open(os.path.join(
            base, "histories", "residentb_yaml", f"{SEQ}.json")))
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["role"], "assistant")
        self.assertEqual(h[0]["content"],
                         "[sent from a wake at 18:49] No bulb. That "
                         "narrows it down.")
        self.assertEqual(h[0]["ts"], f"{TODAY}T18:49:09-07:00")

    def test_chat_origin_no_prefix(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T12:00:00-07:00", "a chat-side send")
        n = sc.reconcile("residentb", "residentb.yaml", from_wake=False, base=base)
        self.assertEqual(n, 1)
        h = json.load(open(os.path.join(
            base, "histories", "residentb_yaml", f"{SEQ}.json")))
        self.assertEqual(h[0]["content"], "a chat-side send")

    def test_idempotent_across_runs(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T18:49:09-07:00", "once only")
        self.assertEqual(sc.reconcile("residentb", "residentb.yaml", True, base=base), 1)
        self.assertEqual(sc.reconcile("residentb", "residentb.yaml", True, base=base), 0)
        self.assertEqual(len(_chron_rows(base, SEQ)), 1)
        h = json.load(open(os.path.join(
            base, "histories", "residentb_yaml", f"{SEQ}.json")))
        self.assertEqual(len(h), 1)

    def test_idempotent_even_without_cursor(self):
        """Defense in depth: delete the cursor — the chronicle uid check
        still prevents duplicates."""
        base = _world()
        _orow(base, SEQ, f"{TODAY}T18:49:09-07:00", "once only")
        sc.reconcile("residentb", "residentb.yaml", True, base=base)
        os.remove(os.path.join(base, "wakes", "residentb", "send_capture.json"))
        self.assertEqual(sc.reconcile("residentb", "residentb.yaml", True, base=base), 0)
        self.assertEqual(len(_chron_rows(base, SEQ)), 1)

    def test_letters_and_failed_sends_skipped(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T10:00:00-07:00", "a letter",
              delivered=True, kind="letter")
        _orow(base, Alex, f"{TODAY}T11:00:00-07:00", "failed",
              delivered=False)
        self.assertEqual(sc.reconcile("residentb", "residentb.yaml", True, base=base), 0)
        self.assertEqual(_chron_rows(base, SEQ), [])
        self.assertEqual(_chron_rows(base, Alex), [])

    def test_gate_off_noop_and_env_kill_switch(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T12:00:00-07:00", "x")
        os.environ["CONTINUA_SEND_CAPTURE"] = "0"
        try:
            self.assertFalse(sc.enabled("residentb"))
            self.assertEqual(
                sc.reconcile("residentb", "residentb.yaml", True, base=base), 0)
        finally:
            os.environ.pop("CONTINUA_SEND_CAPTURE", None)
        # gate off in yaml (a config without the key)
        self.assertFalse(sc.enabled("residentb", config_dir=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "configs_nosuch")))
        self.assertEqual(_chron_rows(base, SEQ), [])

    def test_gate_on_reads_real_yaml(self):
        # the shipped yaml has send_capture: true (Phase 1)
        self.assertTrue(sc.enabled("residentb"))
        self.assertTrue(sc.enabled("residenta"))

    def test_in_memory_histories_updated_and_saved(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T18:49:09-07:00", "join me")
        hist = [{"role": "user", "content": "earlier", "ts": "earlier-ts"}]
        saved = {}

        def save_fn(cfg, pid, h):
            saved[f"{cfg}::{pid}"] = list(h)

        n = sc.reconcile("residentb", "residentb.yaml", True, base=base,
                         user_histories={"residentb.yaml::" + SEQ: hist},
                         save_fn=save_fn)
        self.assertEqual(n, 1)
        self.assertEqual(len(hist), 2)              # the SAME list object
        self.assertIn("residentb.yaml::" + SEQ, saved)  # saved through the fn
        self.assertEqual(saved["residentb.yaml::" + SEQ][-1]["content"],
                         "[sent from a wake at 18:49] join me")

    def test_concurrent_appends_produce_valid_json(self):
        import threading
        base = _world()
        for i in range(50):
            _orow(base, SEQ, f"{TODAY}T18:{i:02d}:00-07:00", f"msg {i}")
        ths = []
        # reconcile itself is sequential per row; hammer the lock directly
        def hammer(k):
            with sc._lock(SEQ):
                hpath = os.path.join(base, "histories", "residentb_yaml",
                                     f"{SEQ}.json")
                try:
                    with open(hpath, encoding="utf-8") as f:
                        h = json.load(f)
                except Exception:
                    h = []
                h.append({"role": "assistant", "content": f"z{k}",
                          "ts": f"{TODAY}T20:00:00-07:00-{k}"})
                os.makedirs(os.path.dirname(hpath), exist_ok=True)
                tmp = hpath + f".tmp{k}"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(h, f)
                os.replace(tmp, hpath)
        for k in range(20):
            t = threading.Thread(target=hammer, args=(k,))
            ths.append(t)
            t.start()
        for t in ths:
            t.join()
        sc.reconcile("residentb", "residentb.yaml", True, base=base)
        h = json.load(open(os.path.join(base, "histories", "residentb_yaml",
                                        f"{SEQ}.json")))
        self.assertEqual(len(h), 70)  # 50 rows + 20 hammer appends, all valid

    def test_chronicle_respects_the_root(self):
        """Regression (incident 2026-09-29d): the append MUST land under the
        given root — a dropped root argument wrote test rows to the
        production chronicle. This test pins the root passing."""
        base = _world()
        _orow(base, SEQ, f"{TODAY}T18:49:09-07:00", "root test")
        n = sc.reconcile("residentb", "residentb.yaml", True, base=base)
        self.assertEqual(n, 1)
        # the row MUST be in the tmp chronicle only
        self.assertEqual(len(_chron_rows(base, SEQ)), 1)
        # and MUST NOT be in the production chronicle
        prod = os.path.join(os.path.dirname(os.path.abspath(
            sc.__file__)), "chronicle", "residentb", SEQ,
            f"{TODAY}.jsonl")
        if os.path.exists(prod):
            with open(prod, encoding="utf-8") as f:
                # 2026-09-30: the PRODUCTION file legitimately contains
                # send-capture records now (the wake reconcile ran on
                # live data). Assert no leak of THIS test's row: the
                # test's text is "root test", carried verbatim into any
                # leaked capture content.
                self.assertNotIn("root test", f.read())

    def test_two_people_one_wake(self):
        base = _world()
        _orow(base, SEQ, f"{TODAY}T18:49:09-07:00", "to Sequoia")
        _orow(base, Alex, f"{TODAY}T19:00:00-07:00", "to Alex")
        n = sc.reconcile("residentb", "residentb.yaml", True, base=base)
        self.assertEqual(n, 2)
        self.assertEqual(len(_chron_rows(base, SEQ)), 1)
        self.assertEqual(len(_chron_rows(base, Alex)), 1)


if __name__ == "__main__":
    unittest.main()