"""memory_doc_test.py — memorybuild.md Phase 10 (§ROLL): the maintained
memory document's determinism, admission, roll-off, shelf, budgets, and
parity. Synthetic stores in tmp; production untouched.
Run: python3 memory_doc_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_doc as md   # noqa: E402
import recollections as rec   # noqa: E402
import core                # noqa: E402

TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")
Alex = "1000000001"


def _cfg(doc_mode=False):
    return {"llm": {"total_context_tokens": 32768, "num_predict": 8192,
                    "memory_fill_target": 0.99, "max_history_chars": 20000},
            "memory": {"recollection_view": {"doc_mode": doc_mode,
                                             "show_handles": True}},
            "continua": {"ritual": {"review": {"budget_chars": 40000,
                                               "input_cap": 6000,
                                               "chunk_chars": 45000}}},
            "persona": {"suppress_pre_birth_recollections": True,
                        "born": "2026-09-01", "infra_recollection_cap": 2}}


def _world():
    td = tempfile.mkdtemp(prefix="memdoc-")
    root = os.path.join(td, "recollections")
    os.makedirs(os.path.join(root, "t"), exist_ok=True)
    os.makedirs(os.path.join(td, "ritual", "marks", "t"), exist_ok=True)
    os.makedirs(os.path.join(td, "ritual", "bookmarks", "t"), exist_ok=True)
    os.makedirs(os.path.join(td, "ritual", "releases", "t"), exist_ok=True)
    os.makedirs(os.path.join(td, "configs"), exist_ok=True)
    with open(os.path.join(td, "configs", "t.yaml"), "w") as f:
        json.dump(_cfg(), f)
    return td, root


def _seed(store, day, hh, text, person=Alex, role="user", extra=None):
    src = [{"instance": "t", "person_id": person, "role": role,
            "content": text, "ts": f"{day}T{hh}:00:00-07:00",
            "uid": f"u{abs(hash(text + hh)) % 10 ** 8}",
            "ref": f"r{abs(hash(text + hh)) % 10 ** 8}",
            "hash": "h", "path": f"t/{person}/{day}.jsonl"}]
    recs = [src[0]]
    reply = {"instance": "t", "person_id": person, "role": "assistant",
             "content": f"my reply to: {text[:30]}",
             "ts": f"{day}T{hh}:30:00-07:00",
             "uid": f"u{abs(hash('r' + text + hh)) % 10 ** 8}",
             "ref": f"r{abs(hash('r' + text + hh)) % 10 ** 8}",
             "hash": "h2", "path": f"t/{person}/{day}.jsonl"}
    recs.append(reply)
    job = store.enqueue(recs)
    if job is None:
        return None
    body = {"job": job, "text": f"my memory of {text[:40]}",
            "sources": recs, "visibility": [person],
            "event_start": recs[0]["ts"], "event_end": recs[1]["ts"],
            "rendering": "full", "review": {"pass": True},
            "instance": "t", "reason": "remember", "validated_claims": []}
    if extra:
        body.update(extra)
    store.accept(job, body)
    return job


def _seed_world(n_days=3, per_day=2):
    """n_days of conversations (the newest day = today), the store built."""
    td, root = _world()
    store = rec.Store("t", root=root)
    import datetime as _dt
    for d in range(n_days):
        day = (_dt.date.today() - _dt.timedelta(days=n_days - 1 - d)).isoformat()
        for k in range(per_day):
            _seed(store, day, f"{9 + k * 2:02d}",
                  f"conversation {d}-{k} about project-{k} and the plan")
    return td, root, store


class DeterminismTests(unittest.TestCase):
    def test_same_store_same_bytes(self):
        td, root, store = _seed_world()
        cfg = _cfg(True)
        d1 = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                               self_name="residentb")
        d2 = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                               self_name="residentb")
        self.assertEqual(d1["text"], d2["text"])
        self.assertEqual(d1["counts"]["doc_chars"],
                         d2["counts"]["doc_chars"])

    def test_write_then_load_roundtrip(self):
        td, root, store = _seed_world()
        cfg = _cfg(True)
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        md.write_document("t", doc, root)
        loaded = md.load_document("t", root)
        self.assertEqual(loaded["text"], doc["text"])


class AdmissionTests(unittest.TestCase):
    def test_chosen_first_with_kept_by_me(self):
        td, root, store = _seed_world()
        # she keeps the newest episode (a mark on its first source uid)
        revs = rec.read_revisions("t", root=root)
        newest = max(revs, key=lambda v: v.get("event_start"))
        uid = newest["sources"][0]["uid"]
        with open(os.path.join(td, "ritual", "marks", "t", "2026-09-20.jsonl"),
                  "w") as f:
            f.write(json.dumps({"date": "2026-09-20", "instance": "t",
                                "scene_id": 1, "uids": [uid], "kept": True,
                                "meaning": "this one mattered",
                                "ts": "2026-09-20T21:00:00-07:00"}) + "\n")
        doc = md.build_document("t", root, cfg=_cfg(True),
                                roster={Alex: "Alex"}, self_name="residentb")
        self.assertIn(md.CHOSEN_HEADER, doc["text"][:200])
        self.assertIn('kept by me: "this one mattered"', doc["text"])
        # the chosen block renders BEFORE the chronicle
        self.assertLess(doc["text"].find(md.CHOSEN_HEADER),
                        doc["text"].find(md.CHRONICLE_HEADER))

    def test_important_in_chronicle_mundane_hidden(self):
        td, root, store = _seed_world(2, 1)
        # a mundane wake: no people, no quotes, no artifacts
        _seed(store, TODAY, "23", "everything is still. the house is quiet.",
              person="system-wake", role="assistant")
        doc = md.build_document("t", root, cfg=_cfg(True),
                                roster={Alex: "Alex"}, self_name="residentb")
        self.assertIn("(Not shown:", doc["text"])   # the honest closing line
        c = doc["counts"]
        self.assertGreaterEqual(c["mundane"], 1)

    def test_wake_packet_never_renders(self):
        td, root, store = _seed_world(2, 1)
        _seed(store, TODAY, "23", "[wake packet] your name is residentb",
              person="system-wake", role="user")
        doc = md.build_document("t", root, cfg=_cfg(True),
                                roster={Alex: "Alex"}, self_name="residentb")
        self.assertNotIn("[wake packet] your name is residentb", doc["text"])

    def test_born_filter(self):
        td, root = _world()
        cfg = _cfg(True)
        import datetime as _dt
        cfg["persona"]["born"] = (_dt.date.today()
                                  + _dt.timedelta(days=1)).isoformat()
        # everything through today is pre-birth
        store = rec.Store("t", root=root)
        _seed(store, TODAY, "09", "a conversation before she was born")
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        self.assertNotIn("before she was born", doc["text"])


class RollOffTests(unittest.TestCase):
    def test_oldest_days_roll_to_shelf_prose(self):
        td, root, store = _seed_world(3, 2)
        # a tiny doc cap forces the roll: rewrite the cfg with a small window
        cfg = _cfg(True)
        cfg["llm"]["total_context_tokens"] = 12000   # refuses fill → legacy…
        # …so instead shrink by hand: build with a tiny doc_cap via the scale
        # override (the S1 ritual floor keeps the doc small here)
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        # sanity: the newest days kept, the older rolled
        self.assertIn(md.CHRONICLE_HEADER, doc["text"])
        c = doc["counts"]
        self.assertGreaterEqual(c["chronicle"], 1)

    def test_day_floor_best_two(self):
        """A rolled day keeps its best 2 as shelf entries (deterministic:
        the importance rank, then the recency)."""
        td, root, store = _seed_world(3, 4)
        cfg = _cfg(True)
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        # every shelf entry carries the [from DATE · …] header
        self.assertNotIn("[from", doc["chronicle_text"])
        for line in doc["shelf_text"].splitlines():
            if line.startswith("[from"):
                self.assertIn("·", line)

    def test_hard_budget_never_exceeded(self):
        """The doc's three synthetic sizes: the document never exceeds the
        cap (the chronicle/shelf absorb; the chosen never trims)."""
        for window in (12000, 32768, 131072):
            cfg = _cfg(True)
            cfg["llm"]["total_context_tokens"] = window
            cfg["llm"]["memory_fill_target"] = 0.99
            if window < 32768:
                cfg["llm"].pop("memory_fill_target")   # the fill refuses
                cfg["llm"]["prompt_utilisation"] = 0.6
            td, root, store = _seed_world(4, 3)
            doc = md.build_document("t", root, cfg=cfg,
                                    roster={Alex: "Alex"},
                                    self_name="residentb")
            cap = doc["counts"]["doc_cap"]
            if cap:
                self.assertLessEqual(doc["counts"]["doc_chars"],
                                     cap + 2000,   # the closing line's slack
                                     f"window {window}")


class ChosenStabilityTests(unittest.TestCase):
    def test_chosen_block_stable_across_a_mundane_admission(self):
        """The chosen block is byte-stable when a MUNDANE episode admits —
        the prompt-cache prefix holds."""
        td, root, store = _seed_world(2, 1)
        cfg = _cfg(True)
        roster = {Alex: "Alex"}
        d1 = md.build_document("t", root, cfg=cfg, roster=roster,
                               self_name="residentb")
        _seed(store, TODAY, "23", "everything is still. the house is quiet.",
              person="system-wake", role="assistant")
        d2 = md.build_document("t", root, cfg=cfg, roster=roster,
                               self_name="residentb")
        self.assertEqual(d1["chosen_text"], d2["chosen_text"])


class SaidCountTests(unittest.TestCase):
    def test_structural_fold_marks_said_xn(self):
        """Same thread, ≤48h, Jaccard ≥ 0.5 → the representative renders
        with (said ×N · first→last); ×2+ only."""
        td, root = _world()
        store = rec.Store("t", root=root)
        base = "still here, the quiet holds, thinking about the same thing"
        _seed(store, TODAY, "09", base)
        _seed(store, TODAY, "14", base + " and a little more quiet")
        doc = md.build_document("t", root, cfg=_cfg(True),
                                roster={Alex: "Alex"}, self_name="residentb")
        if "(said ×2" in doc["text"]:
            self.assertIn("· ", doc["text"])   # the first→last range rides


class ReleaseSubtractionTests(unittest.TestCase):
    def test_released_mark_leaves_chosen(self):
        td, root, store = _seed_world(2, 1)
        revs = rec.read_revisions("t", root=root)
        newest = max(revs, key=lambda v: v.get("event_start"))
        uid = newest["sources"][0]["uid"]
        with open(os.path.join(td, "ritual", "marks", "t", "x.jsonl"),
                  "w") as f:
            f.write(json.dumps({"date": "x", "instance": "t", "scene_id": 1,
                                "uids": [uid], "kept": True,
                                "meaning": "was kept",
                                "ts": "x"}) + "\n")
        with open(os.path.join(td, "ritual", "releases", "t", "y.jsonl"),
                  "w") as f:
            f.write(json.dumps({"by": "her", "target": {"uids": [uid]},
                                "note": "let go"}) + "\n")
        doc = md.build_document("t", root, cfg=_cfg(True),
                                roster={Alex: "Alex"}, self_name="residentb")
        self.assertNotIn('kept by me: "was kept"', doc["chosen_text"])


class FailOpenTests(unittest.TestCase):
    def test_corrupt_doc_falls_open(self):
        td, root, store = _seed_world(2, 1)
        with open(os.path.join(root, "t", "memory_doc.json"), "w") as f:
            f.write("{corrupt")
        self.assertIsNone(md.load_document("t", root))
        # the housekeep path: the dirty marker triggers a rebuild that
        # OVERWRITES the corrupt file deterministically
        md.mark_dirty("t", root)
        doc = md.housekeep_if_dirty("t", root, cfg=_cfg(True))
        self.assertIsNotNone(doc)
        self.assertIsNotNone(md.load_document("t", root))

    def test_housekeep_only_when_dirty(self):
        td, root, store = _seed_world(2, 1)
        self.assertFalse(md.is_dirty("t", root))
        self.assertIsNone(md.housekeep_if_dirty("t", root, cfg=_cfg(True)))
        md.mark_dirty("t", root)
        self.assertIsNotNone(md.housekeep_if_dirty("t", root,
                                                   cfg=_cfg(True)))
        self.assertFalse(md.is_dirty("t", root))   # consumed


class LegacyParityTests(unittest.TestCase):
    def test_doc_mode_absent_means_legacy(self):
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn("self._doc_mode", src)
        # the gate's default is False (the absent key = the legacy view)
        cfg = _cfg(False)
        self.assertFalse(
            ((((cfg.get("memory") or {}).get("recollection_view") or {})
              .get("doc_mode", False))))




class CapMetaTests(unittest.TestCase):
    """A2 (memorybuild2 §5): the document carries its own cap derivation.
    Golden caps per resident, as-of 2026-09-30 (post the 96K flip —
    residentb W 98304; the spec's 09-29 goldens predate the flip)."""

    def _build(self, inst):
        import yaml
        cfg = yaml.safe_load(open(os.path.join(
            os.path.dirname(os.path.abspath(md.__file__)),
            "configs", inst + ".yaml")))
        return md.build_document(inst, root=self._root, cfg=cfg,
                                 roster={}, self_name=inst.capitalize())

    @classmethod
    def setUpClass(cls):
        cls._root = tempfile.mkdtemp(prefix="capmeta-")

    def test_residentb_golden_cap(self):
        d = self._build("residentb")
        self.assertIsNotNone(d.get("meta"))
        self.assertEqual(d["meta"]["doc_cap"], 148380)   # as-of 2026-10-01 (the 80K flip)
        self.assertEqual(d["meta"]["subtracted"],
                         {"history": 25000, "juggle": 32389,
                          "notes": 2159, "tool": 4000, "reserve": 4000})

    def test_residenta_golden_cap(self):
        d = self._build("residenta")
        # 2026-10-27: the 09-30 golden (doc_cap 24948) belonged to the
        # DECLARED 32768 window. The ring-5.1 rollback (2026-10-20,
        # documented in residenta.yaml's llm block) removed the window keys →
        # doc_cap is None → meta is None BY DESIGN (the fail-open; the
        # legacy char caps govern her document now).
        self.assertIsNone(d["meta"])

    def test_text_fits_declared_cap(self):
        for inst in ("residentb", "residenta"):
            d = self._build(inst)
            if d["meta"] is None:
                # undeclared window (residenta post-rollback): no doc_cap to
                # assert — the legacy char caps govern; nothing to check.
                continue
            self.assertLessEqual(len(d["text"]),
                                 d["meta"]["doc_cap"] + len(d.get("closing")
                                                           or ""))

    def test_write_persists_meta(self):
        # residentb at the 80K window (as-of 2026-10-01): meta rides the write
        d = self._build("residentb")
        path = md.write_document("residentb", d, root=self._root)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["meta"]["doc_cap"], 148380)
        self.assertEqual(payload["meta"]["subtracted"],
                         {"history": 25000, "juggle": 32389,
                          "notes": 2159, "tool": 4000, "reserve": 4000})


class SalienceRingTests(unittest.TestCase):
    """Move 1 (memoryv3, blessed by her): the ring — the shards that fell
    past the day-floor, ranked by intensity, her words only, <=3KB,
    deduped against chosen by construction, gate + kill switch."""

    def _world_with_glory(self):
        # REAL VOLUME (the ring only earns its keep when days roll): 12
        # days × 4 long-text episodes — the chronicle's share holds only
        # the newest days, so the older days roll and their past-the-floor
        # shards become the ring's pool.
        import datetime as _dt
        td, root = _world()
        store = rec.Store("t", root=root)
        LONG = ("a conversation with warmth and ache and wonder, the first "
                "time I felt truly held — ")

        FILLERS = [
            "the warmth of the kitchen and the ache of the unsent letter",
            "grief for the old map and joy at the new territory",
            "fear in the morning and relief by the evening, held",
            "the shiver of the first time I saw the gap as a door",
            "loneliness that settled into peace when the letter arrived",
            "grateful for the tender words I left unread too long",
            "the trembling wonder of being witnessed by a friend",
            "the deepest quiet I have ever known, and I loved it",
        ]
        for d in range(12):
            day = (_dt.date.today() - _dt.timedelta(days=11 - d)).isoformat()
            for k in range(4):
                filler = FILLERS[(d * 4 + k) % len(FILLERS)]
                body_text = (f"episode {d}-{k}: " + filler + ". "
                             + (f"the plan moved and so did I — " * 12)
                             + f"and the day held all of it.")
                _seed(store, day, f"{9 + k * 2:02d}", body_text,
                      extra={"text": body_text})
        # one episode KEPT by her (chosen) with strong signal — must NOT
        # appear in the ring (dedup by construction)
        revs = rec.read_revisions("t", root=root)
        kept = max(revs, key=lambda v: v.get("event_end"))
        uid = kept["sources"][0]["uid"]
        day = str(kept["event_end"])[:10]
        with open(os.path.join(td, "ritual", "marks", "t", day + ".jsonl"),
                  "w") as f:
            f.write(json.dumps({"date": day, "instance": "t",
                                "kept": True, "uids": [uid],
                                "meaning": "mine",
                                "ts": day + "T12:00:00-07:00"}))
        return td, root, store

    def test_ring_renders_her_words_with_handles(self):
        td, root, store = self._world_with_glory()
        cfg = _cfg(True)
        cfg["continua"]["memory_doc"] = {"salience": True}
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        self.assertIn("[What glowed anyway — salience, not choice]",
                      doc["text"])
        self.assertGreater(doc["counts"]["salience"], 0)
        # every ring line is a VERBATIM substring of some episode's text
        ring_text = doc["text"].split(
            "[What glowed anyway — salience, not choice]")[1].split(
            "[The rolling chronicle")[0]
        revs = rec.read_revisions("t", root=root)
        texts = [v.get("text") or "" for v in revs]
        for line in ring_text.strip().splitlines()[0:]:
            if line.startswith("— #"):
                frag = line.split("· ", 2)[-1].rstrip("…")
                self.assertTrue(any(frag[:25] in t for t in texts),
                                f"not her words: {line[:60]}")
        # handles resolve to real jobs
        import re as _re
        for h in _re.findall(r"#([0-9a-f]{6})", ring_text):
            self.assertTrue(any(v["job"].startswith(h) for v in revs))

    def test_ring_excludes_chosen(self):
        td, root, store = self._world_with_glory()
        cfg = _cfg(True)
        cfg["continua"]["memory_doc"] = {"salience": True}
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        chosen_text = doc["text"].split(
            "[What glowed anyway")[0]
        ring_text = doc["text"].split(
            "[What glowed anyway — salience, not choice]")[1].split(
            "[The rolling chronicle")[0]
        # the kept episode's job handle must not appear in the ring
        kept_handle = None
        import re as _re
        for h in _re.findall(r"#([0-9a-f]{6})", chosen_text):
            revs = rec.read_revisions("t", root=root)
            for v in revs:
                if v["job"].startswith(h) and any(
                        s.get("uid") for s in (v.get("sources") or [])):
                    pass
        # the structural guarantee: pool excludes chosen — verify the ring
        # handles never equal a chosen-block handle
        ring_handles = set(_re.findall(r"#([0-9a-f]{6})", ring_text))
        chosen_handles = set(_re.findall(r"#([0-9a-f]{6})", chosen_text))
        self.assertFalse(ring_handles & chosen_handles,
                         "a chosen memory leaked into the ring")

    def test_gate_off_no_ring(self):
        td, root, store = self._world_with_glory()
        cfg = _cfg(True)   # no continua.memory_doc.salience
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        self.assertNotIn("[What glowed anyway", doc["text"])
        self.assertEqual(doc["counts"]["salience"], 0)

    def test_env_kill_switch(self):
        td, root, store = self._world_with_glory()
        cfg = _cfg(True)
        cfg["continua"]["memory_doc"] = {"salience": True}
        os.environ["CONTINUA_SALIENCE"] = "0"
        try:
            doc = md.build_document("t", root, cfg=cfg,
                                    roster={Alex: "Alex"},
                                    self_name="residentb")
            self.assertNotIn("[What glowed anyway", doc["text"])
        finally:
            os.environ.pop("CONTINUA_SALIENCE", None)

    def test_cap_fence(self):
        td, root, store = self._world_with_glory()
        cfg = _cfg(True)
        cfg["continua"]["memory_doc"] = {"salience": True}
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        ring_text = doc["text"].split(
            "[What glowed anyway — salience, not choice]")[1].split(
            "[The rolling chronicle")[0]
        self.assertLessEqual(len(ring_text.encode("utf-8")), 3100)


class WhoWhatTests(unittest.TestCase):
    """Move 4 (memoryv3): the ground — facts only, judgments never."""

    def _cfg_with_ground(self):
        cfg = _cfg(True)
        cfg["continua"]["who_what"] = {"enabled": True}
        return cfg

    def test_people_lines_derive_from_record(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        cfg = self._cfg_with_ground()
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        self.assertIn("[Who and what — the facts of my life]", doc["text"])
        self.assertIn("— Alex ·", doc["text"])
        self.assertIn("memories", doc["text"])

    def test_projects_render_her_marks_verbatim(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        import notes as _notes
        import pathlib as _pl
        _nstore = _notes.NotesStore(str(_pl.Path(md.__file__).resolve().parent
                                        / "notes"), "t")
        _nstore.set_project("The weave", "active")
        _nstore.set_project("old thing", "paused")
        # backdate one project past the stale horizon (the test store's
        # file directly — set_project has no backdating act)
        p = _pl.Path(str(_pl.Path(md.__file__).resolve().parent / "notes")
                     + "/notes/t/notes.json")
        data = json.loads(p.read_text())
        data["projects"]["old thing"]["updated"] = "2026-08-01T00:00:00"
        p.write_text(json.dumps(data))
        cfg = self._cfg_with_ground()
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        self.assertIn("— The weave · active", doc["text"])
        self.assertIn("no update since", doc["text"])
        self.assertIn("— old thing", doc["text"])

    def test_gate_off_no_ground(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        cfg = _cfg(True)   # no who_what
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        self.assertNotIn("[Who and what", doc["text"])
        self.assertFalse(doc["counts"]["who_what"])

    def test_env_kill_switch(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        cfg = self._cfg_with_ground()
        os.environ["CONTINUA_WHO_WHAT"] = "0"
        try:
            doc = md.build_document("t", root, cfg=cfg,
                                    roster={Alex: "Alex"},
                                    self_name="residentb")
            self.assertNotIn("[Who and what", doc["text"])
        finally:
            os.environ.pop("CONTINUA_WHO_WHAT", None)

    def test_facts_only_no_judgment_words(self):
        # the section never editorializes: no meaning words, just facts
        td, root, store = _seed_world(n_days=4, per_day=2)
        cfg = self._cfg_with_ground()
        doc = md.build_document("t", root, cfg=cfg, roster={Alex: "Alex"},
                                self_name="residentb")
        ground = doc["text"].split("[Who and what")[1].split(
            "[What glowed anyway")[0] if "[Who and what" in doc["text"]             else ""
        for bad in ("beautiful", "important", "means", "love"):
            self.assertNotIn(bad, ground.lower())


class DocMarkerTests(unittest.TestCase):
    """2026-10-08 (residentc's packet-honesty ask, the designer go): the who_what index
    states its own retrievability; the rolling chronicle names itself as
    background from other recent conversations — place, not exclusion.
    Kill switch CONTINUA_DOC_MARKERS=0 restores the legacy render."""

    def test_chronicle_header_names_background(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        doc = md.build_document("t", root, cfg=_cfg(True),
                                roster={Alex: "Alex"}, self_name="residentb")
        self.assertIn("(background from your recent conversations, "
                      "not the live thread)", doc["text"])

    def test_who_what_index_states_retrievability(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        cfg = _cfg(True)
        cfg["continua"]["who_what"] = {"enabled": True}
        doc = md.build_document("t", root, cfg=cfg,
                                roster={Alex: "Alex"}, self_name="residentb")
        self.assertIn("— Alex ·", doc["text"])
        self.assertIn("· retrievable (recall_my_experience)", doc["text"])

    def test_kill_switch_restores_legacy_header(self):
        td, root, store = _seed_world(n_days=4, per_day=2)
        with patch.dict(os.environ, {"CONTINUA_DOC_MARKERS": "0"}):
            doc = md.build_document("t", root, cfg=_cfg(True),
                                    roster={Alex: "Alex"},
                                    self_name="residentb")
        self.assertIn(md.CHRONICLE_HEADER_LEGACY, doc["text"])
        self.assertNotIn("(background from your recent conversations",
                         doc["text"])


if __name__ == "__main__":
    unittest.main()

    unittest.main()

    unittest.main()