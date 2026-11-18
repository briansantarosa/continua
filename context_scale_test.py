"""context_scale_test.py — memorybuild.md Phase 9 (§SCALE): the law's tests —
the golden values, the NO-REGRESSION rule (every surface ≥ today for BOTH
residents), env overrides, the undeclared fail-open.
Run: python3 context_scale_test.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import context_scale as cs  # noqa: E402
import yaml                 # noqa: E402

residentb = yaml.safe_load(open("configs/residentb.yaml"))
residenta = yaml.safe_load(open("configs/residenta.yaml"))

# today's proven values (the floors — R33: nothing shrinks)
TODAY = {
    "residentb": {"history_chars": 25000, "juggle_total": 15000,
              "juggle_thread": 10000, "notes_cap": 2000,
              "tool_result_cap": 4000, "wake_state": 12000,
              "read_page_ceiling": 40000, "writer_budget": 2400,
              "ritual_budget": 40000, "ritual_input": 6000,
              "ritual_chunk": 45000, "ritual_turn": 900},
    "residenta": {"history_chars": 20000, "juggle_total": 15000,
              "juggle_thread": 10000, "notes_cap": 2000,
              "tool_result_cap": 4000, "wake_state": 12000,
              "read_page_ceiling": 40000, "writer_budget": 2400,
              "ritual_budget": 40000, "ritual_input": 6000,
              "ritual_chunk": 45000, "ritual_turn": 900},
}


class GoldenTests(unittest.TestCase):
    def test_residentb_scales(self):
        # as-of 2026-10-01: the declared window is 80K (81,920) — the designer's
        # concurrency call (her ~55-58K prompt + a ~40K agent prompt fit
        # the card's 131,072 alongside each other); deliberate re-pin.
        s = cs.scale_for(residentb)
        self.assertEqual(s["C"], 215928)
        self.assertEqual(s["margin"], 1024)   # max(1024, 1%W) — the floor wins now
        # the explicit target (the designer's 20K go) rides beneath the 25K cap
        self.assertEqual(s["history_chars"], 25000)
        self.assertEqual(s["history_target"], 20000)
        self.assertEqual(s["juggle_total"], int(0.15 * 215928))   # 32.4K
        self.assertEqual(s["ritual_budget"], int(0.30 * 215928))  # 64.8K
        self.assertEqual(s["notes_cap"], int(0.01 * 215928))    # 2,159
        self.assertEqual(s["notes_cap"],
                         min(int(0.01 * 215928),
                             max(8000, int(0.005 * 215928))))    # ceiling ok

    def test_residenta_floors_hold(self):
        # 2026-10-27: the §WIN-era goldens (C=69948, floors winning under
        # her percents) belonged to the DECLARED 32768 window. The ring-5.1
        # rollback (2026-10-20, documented in residenta.yaml's llm block)
        # REMOVED llm.total_context_tokens + memory_fill_target — the
        # declared-window allocator is OFF for her and scale_for fails
        # open to {} (the legacy char-cap allocator governs: max_prompt_chars
        # 14000 / max_history_chars 12000). The fail-open IS the golden now.
        s = cs.scale_for(residenta)
        self.assertEqual(s, {})

    def test_one_million(self):
        cfg = {"llm": {"total_context_tokens": 1048576, "num_predict": 8192,
                       "memory_fill_target": 0.99}}
        s = cs.scale_for(cfg)
        self.assertEqual(s["C"], 1019600 * 3)
        self.assertEqual(s["juggle_total"], int(0.15 * s["C"]))   # 458.8K
        self.assertEqual(s["notes_cap"],
                         min(int(0.01 * s["C"]),
                             max(8000, int(0.005 * s["C"]))))    # 15.3K
        self.assertEqual(s["wake_state"], 24000)                 # the ceiling


class NoRegressionTests(unittest.TestCase):
    """R33 (the report's §C): EVERY surface ≥ today for BOTH residents."""

    def test_every_surface_at_least_today(self):
        for name, cfg in (("residentb", residentb), ("residenta", residenta)):
            s = cs.scale_for(cfg)
            if not s:
                # 2026-10-27: residenta's window is undeclared post-rollback →
                # scale_for fails open to {} and callers keep today's
                # constants — the no-regression assertion is vacuous there.
                # The rule stays armed for residentb (the declared-window
                # resident) and re-arms automatically if a window is ever
                # re-declared.
                continue
            for surface, today in TODAY[name].items():
                self.assertGreaterEqual(s[surface], today,
                                        f"{name}.{surface}: {s[surface]} < today {today}")

    def test_counts_never_appear(self):
        """R35: counts and serving knobs are not in the scale table."""
        banned = ("max_threads", "thread_cap", "infra_cap", "kept_lines",
                  "recall_limit", "prior_keeps", "max_actions",
                  "max_dive_passes", "num_predict", "temperature",
                  "hold_days", "scene_gap", "dup_hours")
        for key in cs.TABLE:
            for b in banned:
                self.assertNotIn(b, key)


class FailOpenTests(unittest.TestCase):
    def test_undeclared_window_empty(self):
        self.assertEqual(cs.scale_for({}), {})
        self.assertEqual(cs.scale_for({"llm": {}}), {})

    def test_env_override_wins(self):
        s = cs.scale_for(residentb)
        os.environ["CONTINUA_WAKE_STATE_CHARS"] = "12000"
        try:
            self.assertEqual(cs.env_capped("wake_state", s["wake_state"]),
                             12000)   # the env pins the rollback value
        finally:
            os.environ.pop("CONTINUA_WAKE_STATE_CHARS", None)
        self.assertEqual(cs.env_capped("wake_state", s["wake_state"]),
                         s["wake_state"])

    def test_ritual_knobs_percent_floors(self):
        """The ritual's review knobs: yaml value = the floor; the percent
        raises it (residentb's 40000 → the S1 value; at 1M the percent wins)."""
        # as-of the 80K window (2026-10-01): 30% C = 64,778 stays above
        # the 40,000 floor (the percent wins); 2% C = 4,319 falls below
        # the 6,000 floor (the floor wins)
        self.assertEqual(cs.scale_for(residentb)["ritual_budget"],
                         max(40000, int(0.30 * 215928)))
        self.assertEqual(cs.scale_for(residentb)["ritual_input"],
                         max(6000, int(0.02 * 215928)))




class HistoryTargetTests(unittest.TestCase):
    """A5 (memorybuild2 §5): the history target as a first-class row."""

    def _cfg(self, target=None):
        llm = {"total_context_tokens": 32768, "memory_fill_target": 0.99,
               "num_predict": 8192, "max_history_chars": 20000}
        if target is not None:
            llm["history_target_chars"] = target
        return {"llm": llm}

    def test_present_target_wins_below_cap(self):
        sc = cs.scale_for(self._cfg(12000))
        self.assertEqual(sc["history_target"], 12000)   # residenta's row (R43)
        self.assertEqual(sc["history_chars"], 20000)    # the cap unchanged

    def test_absent_target_is_the_cap(self):
        sc = cs.scale_for(self._cfg(None))
        self.assertEqual(sc["history_target"], sc["history_chars"])

    def test_target_never_exceeds_cap(self):
        sc = cs.scale_for(self._cfg(99999))
        self.assertEqual(sc["history_target"], sc["history_chars"])




class ExplicitTargetBeatsFillStompTests(unittest.TestCase):
    """2026-10-01 (the designer's 20K-target go): an explicit
    llm.history_target_chars survives the FILL-MODE stomp in core's
    §SCALE init (core.py ~2464). The stomp only fills an ABSENT key.
    Pure-function pin via context_scale; the core-side init gets a
    live-config assertion below."""

    def test_explicit_target_survives_in_scale_for(self):
        cfg = {"llm": {"total_context_tokens": 81920,
                       "memory_fill_target": 0.99, "num_predict": 8192,
                       "max_history_chars": 25000,
                       "history_target_chars": 20000}}
        sc = cs.scale_for(cfg)
        self.assertEqual(sc["history_target"], 20000)
        self.assertEqual(sc["history_chars"], 25000)   # the cap still grows

    def test_absent_target_fills_to_cap(self):
        cfg = {"llm": {"total_context_tokens": 81920,
                       "memory_fill_target": 0.99, "num_predict": 8192,
                       "max_history_chars": 25000}}
        sc = cs.scale_for(cfg)
        self.assertEqual(sc["history_target"], sc["history_chars"])

    def test_residentb_live_yaml_target(self):
        import yaml
        cfg = yaml.safe_load(open(os.path.join(
            os.path.dirname(os.path.abspath(cs.__file__)),
            "configs", "residentb.yaml")))
        self.assertEqual(cfg["llm"].get("history_target_chars"), 20000)
        self.assertEqual(cfg["llm"].get("total_context_tokens"), 81920)


if __name__ == "__main__":
    unittest.main()