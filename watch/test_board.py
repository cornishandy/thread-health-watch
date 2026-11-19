#!/usr/bin/env python3
"""Fixtures for the quiet-hub board, digest gate, health line and list look."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("DIGEST_PY", str(BUILD / "settled-digest" / "digest.py"))
os.environ.setdefault("BOARD_PY", str(HERE / "board.py"))

import board
import triage
import watch_v2

REPORT = Path.home() / "Library/Caches/thread-watch-v2/publication-report.json"
LIVE_KEY = json.loads(REPORT.read_text())["findings"][0]["key"]


def load_mod(name, path):
    path = str(path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        loader = importlib.machinery.SourceFileLoader(name, path)
        spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def local_at(hour, minute=0):
    zoned = datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return zoned.timestamp()


def sample_board():
    return {
        "candidates": [{"root": "r", "kind": "red_idle", "key": "m"}],
        "waiting_cards": [{"request_id": "card-1", "age_s": 700}],
        "held_new": [],
        "publication_flagged": [],
        "health": "healthy",
        "last_completed_scan": time.time(),
        "stale": False,
    }


class BoardFieldTests(unittest.TestCase):
    def test_red_idle_with_and_without_needs_you(self):
        raw = "RAW-REPLY-SHOULD-NOT-APPEAR"
        for needs in (True, False):
            view = {
                "root": "root-1",
                "runs": [{"status": "completed", "run_id": "run-1", "thread_id": "root-1"}],
                "reply": {"text": raw, "verdict": "red", "needs_you": needs, "message_id": "m1", "at": 3},
                "running": False,
                "newest_user": "",
                "quiet_seconds": 0,
            }
            found = board.candidate_for(view, 100)
            self.assertEqual(found["kind"], "red_idle")
            self.assertIn("t3-thread://v1/", found["link"])
            text = board.digest_text(
                {"candidates": [found], "waiting_cards": [], "publication": [], "health": "healthy",
                 "last_completed_scan": 100, "stale": False, "fingerprint": "abc"},
                board.empty_digest_state(), "3pm", "evt", now=100,
            )
            self.assertIn("red_idle", text)
            self.assertIn(found["link"], text)
            self.assertNotIn(raw, text)
            self.assertNotIn("Needs you", text.split("Candidates:")[-1])

    def test_plain_question_is_a_board_field(self):
        text = "The run finished.\nCan you check this?"
        self.assertTrue(board.is_plain_question(text))
        self.assertFalse(triage.needs_you(text))
        view = {
            "root": "root-q",
            "runs": [{"status": "completed", "run_id": "run-q", "thread_id": "root-q"}],
            "reply": {"text": text, "verdict": None, "message_id": "mq", "at": 5},
            "running": False,
            "newest_user": "please",
            "quiet_seconds": 0,
        }
        found = board.candidate_for(view, 10)
        self.assertEqual(found["kind"], "text_ask")

    def test_failed_scheduled_task_is_a_candidate(self):
        view = {
            "root": "root-s",
            "runs": [{"status": "failed", "run_id": "run-s", "thread_id": "root-s", "end": 4}],
            "reply": None,
            "running": True,
            "newest_user": "scheduled-task-message: wake",
            "quiet_seconds": 0,
        }
        found = board.candidate_for(view, 10)
        self.assertEqual(found["kind"], "failed_or_stopped")

    def test_scheduled_prefix_skips_no_verdict_only(self):
        view = {
            "root": "root-n",
            "runs": [{"status": "completed", "run_id": "run-n", "thread_id": "root-n"}],
            "reply": None,
            "running": False,
            "newest_user": "scheduled-task-message: wake",
            "quiet_seconds": 0,
        }
        self.assertIsNone(board.candidate_for(view, 10))
        view["newest_user"] = "what happened?"
        self.assertEqual(board.candidate_for(view, 10)["kind"], "no_verdict")

    def test_settled_and_archived_roots_are_not_candidates(self):
        now = 10_000
        def run(thread_id, run_id, status, end, requested):
            return {"run_id": run_id, "thread_id": thread_id, "status": status,
                    "end": end, "requested": requested, "ordinal": 1}
        frame = {
            "threads": {
                "settled": {"top": True, "settled": 50, "archived": 0},
                "archived": {"top": True, "settled": 0, "archived": 40},
                "open": {"top": True, "settled": 0, "archived": 0},
                "busy": {"top": True, "settled": 10, "archived": 0},
            },
            "members": {name: [name] for name in ("settled", "archived", "open", "busy")},
            "runs": {
                "settled": [run("settled", "r1", "completed", 20, 10)],
                "archived": [run("archived", "ra", "completed", 20, 10)],
                "open": [run("open", "ro", "failed", 30, 10)],
                "busy": [run("busy", "rb", "running", 0, now - 2700)],
            },
            "answers": {"r1": [{"message_id": "final-settled", "at": 20, "verdict": "green"}]},
            "items": {},
            "plain_asks": {},
            "user_heads": {},
        }
        candidates, finals = board.views_from_frame(frame, now, board.empty_digest_state())
        by_root = {item["root"]: item["kind"] for item in candidates}
        self.assertNotIn("settled", by_root)
        self.assertNotIn("archived", by_root)
        self.assertEqual(by_root.get("open"), "failed_or_stopped")
        self.assertEqual(by_root.get("busy"), "stall")
        self.assertEqual(finals, ["final-settled"])

    def test_morning_queue_keeps_one_final_per_root(self):
        state = {"final_reply_watermark": 100}
        frame = {
            "threads": {
                "root": {"top": True, "settled": 0, "archived": 0},
                "other": {"top": True, "settled": 0, "archived": 0},
            },
            "members": {"root": ["root"], "other": ["other"]},
            "runs": {
                "root": [
                    {"run_id": "old", "thread_id": "root", "ordinal": 1, "status": "completed", "requested": 40, "end": 50},
                    {"run_id": "mid", "thread_id": "root", "ordinal": 2, "status": "completed", "requested": 140, "end": 150},
                    {"run_id": "new", "thread_id": "root", "ordinal": 3, "status": "completed", "requested": 200, "end": 220},
                ],
                "other": [
                    {"run_id": "o1", "thread_id": "other", "ordinal": 1, "status": "completed", "requested": 160, "end": 180},
                ],
            },
            "answers": {
                "old": [{"message_id": "before-watermark", "at": 50, "verdict": "green"}],
                "mid": [{"message_id": "earlier-today", "at": 150, "verdict": "green"}],
                "new": [{"message_id": "newest", "at": 220, "verdict": "green"}],
                "o1": [{"message_id": "other-final", "at": 180, "verdict": "green"}],
            },
            "items": {},
            "plain_asks": {},
            "user_heads": {"root": (160, "still going"), "other": (110, "hello")},
        }
        _candidates, finals = board.views_from_frame(frame, 1_000, state)
        self.assertEqual(sorted(finals), ["newest", "other-final"])

    def test_morning_queue_drops_a_final_andy_replied_after(self):
        state = {"final_reply_watermark": 100}
        frame = {
            "threads": {"root": {"top": True, "settled": 0, "archived": 0}},
            "members": {"root": ["root"]},
            "runs": {"root": [{"run_id": "r1", "thread_id": "root", "ordinal": 1, "status": "completed", "requested": 180, "end": 200}]},
            "answers": {"r1": [{"message_id": "answered", "at": 200, "verdict": "green"}]},
            "items": {},
            "plain_asks": {},
            "user_heads": {"root": (250, "thanks")},
        }
        _candidates, finals = board.views_from_frame(frame, 1_000, state)
        self.assertEqual(finals, [])

    def test_morning_queue_skips_a_scheduled_task_root(self):
        state = {"final_reply_watermark": 100}
        frame = {
            "threads": {
                "hub": {"top": True, "settled": 0, "archived": 0},
                "root": {"top": True, "settled": 0, "archived": 0},
            },
            "members": {"hub": ["hub"], "root": ["root"]},
            "runs": {
                "hub": [{"run_id": "h1", "thread_id": "hub", "ordinal": 1, "status": "completed", "requested": 180, "end": 200}],
                "root": [{"run_id": "r1", "thread_id": "root", "ordinal": 1, "status": "completed", "requested": 180, "end": 210}],
            },
            "answers": {
                "h1": [{"message_id": "hourly", "at": 200, "verdict": "green"}],
                "r1": [{"message_id": "kept", "at": 210, "verdict": "green"}],
            },
            "items": {},
            "plain_asks": {},
            "user_heads": {
                "hub": (50, "scheduled-task-message: guardian-events-v1"),
                "root": (40, "please look"),
            },
        }
        _candidates, finals = board.views_from_frame(frame, 1_000, state)
        self.assertEqual(finals, ["kept"])

    def test_morning_queue_keeps_a_settled_unread_final(self):
        state = {"final_reply_watermark": 100}
        frame = {
            "threads": {"settled": {"top": True, "settled": 300, "archived": 0}},
            "members": {"settled": ["settled"]},
            "runs": {"settled": [{"run_id": "r1", "thread_id": "settled", "ordinal": 1, "status": "completed", "requested": 180, "end": 200}]},
            "answers": {"r1": [{"message_id": "settled-unread", "at": 200, "verdict": "green"}]},
            "items": {},
            "plain_asks": {},
            "user_heads": {"settled": (150, "ship it")},
        }
        candidates, finals = board.views_from_frame(frame, 1_000, state)
        self.assertEqual(finals, ["settled-unread"])
        self.assertNotIn("settled", {item["root"] for item in candidates})

    def test_fingerprint_ignores_ages_heartbeat_and_report_time(self):
        first = sample_board()
        second = sample_board()
        second["waiting_cards"] = [{"request_id": "card-1", "age_s": 900}]
        second["heartbeat"] = 1
        second["checked_at"] = "2026-11-15T20:51:07-05:00"
        self.assertEqual(board.fingerprint(first), board.fingerprint(second))
        young = sample_board()
        young["waiting_cards"] = [{"request_id": "card-1", "age_s": 100}]
        self.assertNotEqual(board.fingerprint(first), board.fingerprint(young))

    def test_trial_input_total_sums_one_run(self):
        self.assertEqual(board.trial_input_total([100, 900]), 1000)


class PosterTests(unittest.TestCase):
    def test_unchanged_board_at_15_posts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "digest-state.json"
            snapshot = sample_board()
            snapshot["fingerprint"] = board.fingerprint(snapshot)
            state = board.empty_digest_state()
            state["last_consumed_fingerprint"] = snapshot["fingerprint"]
            calls = []
            sent = board.maybe_post_digest(
                {"_state": str(Path(tmp) / "state.json")}, snapshot, local_at(15),
                sender=lambda body: calls.append(body), digest_state=state, state_path=path,
            )
            self.assertEqual(sent, [])
            self.assertEqual(calls, [])

    def test_restart_does_not_post_a_slot_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "digest-state.json"
            snapshot = sample_board()
            snapshot["fingerprint"] = "changed"
            state = board.empty_digest_state()
            calls = []
            config = {"_state": str(Path(tmp) / "state.json")}
            first = board.maybe_post_digest(
                config, snapshot, local_at(15), sender=lambda body: calls.append(body),
                digest_state=state, state_path=path,
            )
            second = board.maybe_post_digest(
                config, snapshot, local_at(15, 5), sender=lambda body: calls.append(body),
                digest_state=state, state_path=path,
            )
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["kind"], "thread_health_digest")
            self.assertEqual(first[0]["slot"], "3pm")
            self.assertEqual(second, [])
            self.assertEqual(len(calls), 1)
            self.assertIsNone(state["last_consumed_fingerprint"])

    def test_http_failure_and_success_without_a_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "digest-state.json"
            snapshot = sample_board()
            snapshot["fingerprint"] = "fresh"
            state = board.empty_digest_state()
            config = {"_state": str(Path(tmp) / "state.json")}

            def boom(_body):
                raise urllib.error.URLError("down")

            sent = board.maybe_post_digest(
                config, snapshot, local_at(15), sender=boom, digest_state=state, state_path=path,
            )
            day = datetime.fromtimestamp(local_at(15)).astimezone().date().isoformat()
            self.assertEqual(sent, [])
            self.assertNotIn("3pm", (state.get("posted") or {}).get(day, {}))
            self.assertIsNone(state["last_consumed_fingerprint"])
            sent = board.maybe_post_digest(
                config, snapshot, local_at(15), sender=lambda body: None,
                digest_state=state, state_path=path,
            )
            self.assertEqual(len(sent), 1)
            self.assertIn("3pm", state["posted"][day])
            self.assertIsNone(state["last_consumed_fingerprint"])

    def test_a_late_reply_consumes_only_its_fingerprint(self):
        state = board.empty_digest_state()
        board.register_event(state, "old", "fp-old", "3pm", "events")
        board.register_event(state, "new", "fp-new", "9pm", "events")
        self.assertEqual(board.consume(state, "old", "events"), "fp-old")
        self.assertEqual(state["last_consumed_fingerprint"], "fp-old")
        self.assertIsNone(board.consume(state, "old", "andy"))
        self.assertEqual(state["last_consumed_fingerprint"], "fp-old")
        self.assertEqual(board.consume(state, "new", "events"), "fp-new")
        self.assertEqual(state["last_consumed_fingerprint"], "fp-new")

    def test_both_afternoon_slots_after_21(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = board.empty_digest_state()
            snapshot = sample_board()
            snapshot["fingerprint"] = "both"
            sent = board.maybe_post_digest(
                {"_state": str(Path(tmp) / "state.json")}, snapshot, local_at(21, 30),
                sender=lambda body: None, digest_state=state, state_path=Path(tmp) / "digest-state.json",
            )
            self.assertEqual([body["slot"] for body in sent], ["9pm"])
            day = datetime.fromtimestamp(local_at(21, 30)).astimezone().date().isoformat()
            skipped = state["posted"][day]["3pm"]
            self.assertTrue(skipped.get("skipped"))
            self.assertNotIn("event_id", skipped)
            self.assertEqual([event.get("slot") for event in state["events"].values()], ["9pm"])

    def test_first_write_seals_the_morning_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 1_700_000_000.0
            pub = root / "publication.json"
            pub.write_text("{}\n")
            frame = {
                "threads": {"root": {"top": True, "settled": 0.0, "archived": 0.0}},
                "members": {"root": ["root"]},
                "runs": {"root": [
                    {"run_id": "old-run", "thread_id": "root", "ordinal": 1, "status": "completed", "requested": 1, "end": 2},
                    {"run_id": "new-run", "thread_id": "root", "ordinal": 2, "status": "completed", "requested": now + 1, "end": now + 2},
                ]},
                "answers": {
                    "old-run": [{"message_id": "before-cutover", "at": now - 50, "verdict": "green", "needs_you": False}],
                    "new-run": [{"message_id": "since-cutover", "at": now + 2, "verdict": "green", "needs_you": False}],
                },
            }
            config = {
                "_state": str(root / "state.json"),
                "board_path": str(root / "board.json"),
                "digest_state_path": str(root / "digest-state.json"),
                "publication_report": str(root / "report.json"),
                "database": str(root / "missing.sqlite"),
            }
            previous = os.environ.get("PUBLICATION_CONFIG")
            os.environ["PUBLICATION_CONFIG"] = str(pub)
            try:
                written = board.sync_after_scan(config, {"incidents": {}}, frame, now, "shadow")
                board.sync_after_scan(config, {"incidents": {}}, frame, now + 10, "shadow")
            finally:
                if previous is None:
                    os.environ.pop("PUBLICATION_CONFIG", None)
                else:
                    os.environ["PUBLICATION_CONFIG"] = previous
            digest = json.loads((root / "digest-state.json").read_text())
            self.assertEqual(digest["started_at"], now)
            self.assertEqual(digest["final_reply_watermark"], now)
            self.assertEqual(written["final_reply_ids"], ["since-cutover"])
            text = board.digest_text(written, digest, "9am", "morning", now=now)
            self.assertIn("since-cutover", text)
            self.assertNotIn("before-cutover", text)
            board.register_event(digest, "morning", written["fingerprint"], "9am", "9am")
            digest["events"]["morning"]["snapshot_at"] = now + 40
            board.consume(digest, "morning", "9am")
            self.assertEqual(digest["final_reply_watermark"], now + 40)
            aged = board.empty_digest_state()
            aged["events"]["old"] = {"slot": "3pm"}
            aged["final_reply_watermark"] = 0
            board.note_started(aged, now)
            self.assertEqual(aged["final_reply_watermark"], 0)
            self.assertNotIn("started_at", aged)


class PublicationTests(unittest.TestCase):
    def test_live_key_is_quoted_and_a_removed_key_is_not(self):
        self.assertTrue(LIVE_KEY)
        self.assertNotIn("webhook", LIVE_KEY)
        now = 1_000_000.0
        state = board.empty_digest_state()
        findings = [{"key": LIVE_KEY, "text": "one finding"}]
        built = board.build_board({}, {"incidents": {}}, now, state, [], findings, set())
        text = board.digest_text(built, state, "3pm", "evt", now=now)
        self.assertIn(LIVE_KEY, text)
        self.assertIn("none new", text)
        built = board.build_board({}, {"incidents": {}}, now + 10, state, [], [], set())
        text = board.digest_text(built, state, "3pm", "evt", now=now + 10)
        self.assertNotIn(LIVE_KEY, text)

    def test_unmapped_finding_is_not_new_before_60_quiet_minutes(self):
        now = 2_000_000.0
        state = board.empty_digest_state()
        findings = [{"key": "dirty:/tmp/unmapped-repo", "text": "dirty"}]
        first = board.annotate_findings(findings, [], now, state, set())
        self.assertFalse(first[0]["mapped"])
        self.assertFalse(first[0]["flagged"])
        later = board.annotate_findings(findings, [], now + 3599, state, set())
        self.assertFalse(later[0]["flagged"])
        due = board.annotate_findings(findings, [], now + 3600, state, set())
        self.assertTrue(due[0]["flagged"])


class HealthAndListTests(unittest.TestCase):
    def test_a_digest_never_posted_makes_health_notify_once(self):
        health = load_mod("health_watch_case", BUILD / "settle-sweep" / "health.py")
        now = local_at(9, 16)
        snapshot = {"last_completed_scan": now, "health": "healthy", "fingerprint": "new"}
        digest_state = {"last_consumed_fingerprint": "old", "replies": {}, "events": {}}
        fire, state, found = health.check(now, snapshot, now, digest_state, {})
        self.assertTrue(fire)
        self.assertTrue(any(item.startswith("digest:") for item in found))
        fire, _state, _found = health.check(now, snapshot, now, digest_state, state)
        self.assertFalse(fire)

    def test_fresh_install_waits_for_a_real_miss(self):
        health = load_mod("health_install_case", BUILD / "settle-sweep" / "health.py")
        now = local_at(23, 51)
        fire, state, found = health.check(now, {}, now, {}, {})
        self.assertFalse(fire)
        self.assertNotIn("scan", found)
        self.assertFalse(any(item.startswith("digest:") for item in found))
        later = now + 15 * 60
        fire, state, found = health.check(later, {}, later, {}, state)
        self.assertIn("scan", found)
        started = {"started_at": now, "replies": {}, "events": {}}
        same_night = {"fingerprint": "new", "last_completed_scan": now, "health": "healthy"}
        self.assertIsNone(health.digest_late(now, same_night, started))
        tomorrow = local_at(9, 16) + 86400
        morning = {"fingerprint": "new", "last_completed_scan": tomorrow, "health": "healthy"}
        self.assertEqual(health.digest_late(tomorrow, morning, started), "9am")

    def test_per_event_off_returns_before_any_post(self):
        calls = []
        state = triage.new_state()
        state["outbox"] = {"event_id": "e", "payload": {"kind": "thread_health_v2"}, "ids": []}
        delivered = watch_v2.deliver(
            state, {"per_event_delivery": False}, time.time(), sender=lambda payload: calls.append(payload),
        )
        self.assertFalse(delivered)
        self.assertEqual(calls, [])
        self.assertTrue(board.per_event_enabled({}))
        publication = load_mod("publication_case", HERE / "publication_watch.py")
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "route.json"
            route.write_text(json.dumps({
                "per_event_delivery": False,
                "webhook_url": "https://example.invalid/stub",
                "guardian_thread_id": "t",
            }))
            result = publication.send_event({"guardian_config": str(route)}, {"findings": []})
            self.assertEqual(result, {"skipped": True})

    def test_parser_fixtures(self):
        digest = load_mod("digest_case", BUILD / "settled-digest" / "digest.py")
        two = """Note

---
| <span itemprop="settle-no">no</span> | open |   |
|---|---|---|
| Worth reading · 2 | 8. first |   |
| Worth reading · 2 | 33. second |   |
"""
        rows = digest.parse_reply(two)["rows"]
        self.assertEqual([label for label, _cell, _rest in rows], ["Worth reading", "Worth reading"])
        mixed = """Note

---
| <span itemprop="settle-no">no</span> | open |   |
|---|---|---|
| Needs you | **3. Hulu screenshot** · take it |   |
| Worth reading · 5 | 8. one |   |
|   | 33. two |   |
|   | 34. three |   |
|   | 35. four |   |
|   | 37. five |   |
"""
        parsed = digest.parse_reply(mixed)["rows"]
        self.assertEqual(sum(1 for label, _c, _r in parsed if label == "Needs you"), 1)
        self.assertEqual(sum(1 for label, _c, _r in parsed if label == "Worth reading"), 5)
        feed = load_mod("feed_case", BUILD / "settle-sweep" / "feed.py")
        lists = feed.rows_to_lists(parsed)
        self.assertEqual(len(lists["needs"]), 1)
        self.assertEqual(len(lists["reading"]), 5)
        self.assertNotIn("Worth reading", json.dumps(lists["needs"]))

    def test_each_feed_trigger_turns_the_line_red(self):
        feed = load_mod("feed_red", BUILD / "settle-sweep" / "feed.py")
        now = local_at(12)
        answered = {
            "last_consumed_fingerprint": "same",
            "replies": {"a": {"slot": "9am", "source": "9am", "at": now}},
            "events": {"a": {"slot": "9am", "source": "9am", "at": now}},
        }
        items = [{"n": 3, "topic": "Hulu screenshot"}, {"n": 8, "topic": "Worth reading", "glance": "**x**"}]
        healthy = {
            "last_completed_scan": now,
            "health": "healthy",
            "fingerprint": "same",
            "waiting_cards": [{"request_id": "c", "age_s": 30, "title": "Soon"}],
            "publication": [{"key": "dirty:/tmp/repo", "flagged": False}],
        }
        quiet = feed.status_html(now, healthy, now, answered, items)
        self.assertIn("Watch healthy", quiet)
        self.assertNotIn('class="watch-status bad"', quiet)
        self.assertNotIn("http", quiet)
        cases = []
        stale = dict(healthy, last_completed_scan=now - 16 * 60)
        cases.append(feed.status_html(now, stale, now, answered, items))
        cases.append(feed.status_html(now, healthy, now - 91 * 60, answered, items))
        late = dict(answered, last_consumed_fingerprint="old")
        late["replies"] = {}
        noon_late = local_at(9, 16)
        cases.append(feed.status_html(
            noon_late,
            {"last_completed_scan": noon_late, "health": "healthy", "fingerprint": "new", "waiting_cards": [], "publication": []},
            noon_late, late, items,
        ))
        waiting = dict(healthy)
        waiting["waiting_cards"] = [{"request_id": "c", "age_s": 601, "title": "Movie companion"}]
        cases.append(feed.status_html(now, waiting, now, answered, items))
        flagged = dict(healthy)
        flagged["publication"] = [{"key": "dirty:/tmp/repo", "flagged": True}]
        cases.append(feed.status_html(now, flagged, now, answered, items))
        for html in cases:
            self.assertIn('class="watch-status bad"', html)
            self.assertNotIn("http://", html)
            self.assertNotIn("https://", html)

    def test_read_commands_and_footer(self):
        wake = load_mod("wake_case", BUILD / "bin" / "guardian-wake")
        items = [
            {"n": 3, "topic": "Hulu screenshot", "since": "2026-10-26", "row": "Take the shot", "glance": "keep"},
            {"n": 35, "topic": "Worth reading", "since": "2026-11-11", "row": "three", "glance": "**x**"},
            {"n": 36, "topic": "Ship the page", "since": "2026-11-12", "row": "a to-do"},
            {"n": 37, "topic": "Worth reading", "since": "2026-11-12", "row": "five"},
        ]
        kept, cleared = wake.apply_read(items, "33-37 read")
        self.assertEqual(cleared, [35, 37])
        self.assertIn(36, [item["n"] for item in kept])
        self.assertIn(3, [item["n"] for item in kept])
        kept, cleared = wake.apply_read(items, "read all")
        self.assertEqual(sorted(item["n"] for item in kept), [3, 36])
        self.assertEqual(wake.shown_glance({"topic": "Worth reading", "glance": "**x**"}), "x")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            board_path = tmp / "board.json"
            now = time.time()
            snapshot = {
                "last_completed_scan": now,
                "stale": False,
                "health": "healthy",
                "fingerprint": "fp",
                "waiting_cards": [],
                "candidates": [],
                "publication": [],
            }
            board_path.write_text(json.dumps(snapshot))
            items_path = tmp / "andy-items.json"
            shown = [
                {"n": 3, "topic": "Hulu screenshot", "since": "2026-10-26", "row": "Take the shot"},
                {"n": 8, "topic": "Worth reading", "since": "2026-11-07", "row": "full text", "glance": "**x**",
                 "due_note": "refund and replies expected by about 25 Nov"},
            ]
            items_path.write_text(json.dumps(shown))
            advice = {"8": {"topic": "Worth reading", "finding": "keep this advice"}}
            (tmp / "advice.json").write_text(json.dumps(advice))
            before_items = items_path.read_bytes()
            before_advice = (tmp / "advice.json").read_bytes()
            env = os.environ.copy()
            env.update({
                "ANDY_ITEMS": str(items_path),
                "ANDY_ADVICE": str(tmp / "advice.json"),
                "ANDY_SEEN": str(tmp / "seen.json"),
                "BOARD_PATH": str(board_path),
                "BOARD_PY": str(HERE / "board.py"),
                "DIGEST_STATE": str(tmp / "digest-state.json"),
                "OPEN_THREADS_DIR": str(tmp),
                "GUARDIAN_SLOT": "9pm",
            })
            proc = subprocess.run(
                [sys.executable, str(BUILD / "bin" / "guardian-wake"), "footer", "--slot", "9pm"],
                capture_output=True, text=True, env=env, timeout=60, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout
            self.assertIn("watch healthy", out)
            self.assertEqual(out.count("| Needs you |"), 1)
            self.assertEqual(out.count("Worth reading ·"), 1)
            self.assertNotIn("Open threads", out)
            reading = [line for line in out.splitlines() if 'itemprop="reading"' in line]
            self.assertEqual(len(reading), 1)
            self.assertNotIn("<strong>", reading[0])
            self.assertNotIn("**", reading[0])
            self.assertIn(">8.</span> x", reading[0])
            self.assertEqual(items_path.read_bytes(), before_items)
            self.assertEqual((tmp / "advice.json").read_bytes(), before_advice)
            seen = json.loads((tmp / "seen.json").read_text())
            self.assertEqual(seen["8"]["glance"], "**x**")
            self.assertEqual(seen["3"]["since"], "2026-10-26")

    def test_record_keeps_glance_since_and_advice(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            items = [
                {"n": 8, "topic": "Worth reading", "since": "2026-11-07", "row": "full", "glance": "**x**"},
            ]
            items_path = tmp / "andy-items.json"
            items_path.write_text(json.dumps(items))
            advice_path = tmp / "advice.json"
            advice_path.write_text(json.dumps({"8": {"topic": "Worth reading", "finding": "advice stays"}}))
            handoff = tmp / "HANDOFF.md"
            handoff.write_text("Reviewed event IDs: []\n\n## 1. State\n- earlier\n\n## 2. Decisions\n\n")
            event = "11111111-1111-1111-1111-111111111111"
            digest_path = tmp / "digest-state.json"
            state = board.empty_digest_state()
            board.register_event(state, event, "fp-record", "3pm", "events")
            digest_path.write_text(json.dumps(state))
            before_items = items_path.read_bytes()
            before_advice = advice_path.read_bytes()
            env = os.environ.copy()
            env.update({
                "ANDY_ITEMS": str(items_path),
                "ANDY_ADVICE": str(advice_path),
                "ANDY_SEEN": str(tmp / "seen.json"),
                "BOARD_PY": str(HERE / "board.py"),
                "DIGEST_STATE": str(digest_path),
                "GUARDIAN_HANDOFF": str(handoff),
                "OPEN_THREADS_DIR": str(tmp),
            })
            proc = subprocess.run(
                [sys.executable, str(BUILD / "bin" / "guardian-wake"), "record", event, "Recorded the digest."],
                capture_output=True, text=True, env=env, timeout=60, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(items_path.read_bytes(), before_items)
            self.assertEqual(advice_path.read_bytes(), before_advice)
            saved = json.loads(digest_path.read_text())
            self.assertEqual(saved["last_consumed_fingerprint"], "fp-record")

    def test_rules_diff_and_check_wording(self):
        rules = (BUILD / "rules" / "guardian-attention.md").read_text()
        old = (HERE / "cutover" / "guardian-attention.md.pre-c3").read_text()
        self.assertNotEqual(rules, old)
        self.assertIn("Keep Andy's open-threads list current", old)
        self.assertNotIn("Keep Andy's open-threads list current", rules)
        self.assertIn("Worth reading · N", rules)
        wake = (BUILD / "bin" / "guardian-wake").read_text()
        self.assertIn("Do not update open-threads.md", wake)
        self.assertNotIn("If it shows changes, update", wake)
        prompt = (HERE / "GUARDIAN-PROMPT.md").read_text()
        self.assertTrue(prompt.startswith("> Superseded 16 Nov 2026."))
        run = (BUILD / "settle-sweep" / "run.sh").read_text()
        self.assertIn("health.py", run)



class MorningRunFixes(unittest.TestCase):
    """16 Nov 2026 first real 9 AM run: 17 standing findings called new; idle scans 20 min apart."""

    def rows(self):
        return [
            {"key": "a", "text": "A", "mapped": True, "flagged": True},
            {"key": "b", "text": "B", "mapped": True, "flagged": True},
            {"key": "c", "text": "C", "mapped": True, "flagged": False},
        ]

    def test_git_line_counts_only_unreported_flagged(self):
        text, keys = board.git_line(self.rows(), reported=["a"])
        self.assertEqual(text, "Git and Netlify: 3 findings, 1 new: b")
        self.assertEqual(keys, ["a", "b", "c"])
        text, _ = board.git_line(self.rows(), reported=["a", "b"])
        self.assertEqual(text, "Git and Netlify: 3 findings, none new")
        text, _ = board.git_line(self.rows())
        self.assertEqual(text, "Git and Netlify: 3 findings, 2 new: a; b")

    def test_consume_replaces_reported_with_the_events_flagged(self):
        state = board.empty_digest_state()
        state["publication_reported"] = ["old"]
        state["events"] = {
            "e1": {"fingerprint": "f1", "slot": "3pm", "at": 1.0, "snapshot_at": 1.0, "publication_flagged": ["a", "b"]},
            "e0": {"fingerprint": "f0", "slot": "9pm", "at": 1.0, "snapshot_at": 1.0},
        }
        board.consume(state, "e0", "events")
        self.assertEqual(state["publication_reported"], ["old"], "an event from before the fix keeps the set")
        board.consume(state, "e1", "events")
        self.assertEqual(state["publication_reported"], ["a", "b"])
        board.consume(state, "e1", "andy")
        self.assertEqual(state["publication_reported"], ["a", "b"], "Andy's turn never consumes")

    def test_digest_text_after_consume_says_none_new(self):
        snap = {"fingerprint": "f2", "publication": self.rows(), "publication_flagged": ["a", "b"],
                "last_completed_scan": time.time(), "candidates": [], "waiting_cards": []}
        state = board.empty_digest_state()
        state["publication_reported"] = ["a", "b"]
        text = board.digest_text(snap, state, "3pm", "e9")
        self.assertIn("Git and Netlify: 3 findings, none new", text)
        state["publication_reported"] = ["a"]
        self.assertIn("1 new: b", board.digest_text(snap, state, "3pm", "e9"))

    def test_digest_result_uses_reported(self):
        snap = {"publication_flagged": ["a", "b"], "candidates": [], "held_new": []}
        self.assertEqual(board.digest_result(snap, "3pm", ["a"]), "1 new findings")
        self.assertEqual(board.digest_result(snap, "3pm", ["a", "b"]), "no new list item")
        self.assertEqual(board.digest_result(snap, "3pm"), "2 new findings")

    def test_posted_event_remembers_flagged_and_reply_consumes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "digest-state.json"
            snap = {"fingerprint": "f3", "publication": self.rows(), "publication_flagged": ["a", "b"],
                    "candidates": [], "held_new": [], "held_outcome_ids": []}
            sent = []
            now = datetime(2026, 11, 16, 15, 5).astimezone().timestamp()
            out = board.maybe_post_digest({}, snap, now, sender=sent.append, state_path=path)
            self.assertEqual(len(out), 1)
            state = json.loads(path.read_text())
            event_id = out[0]["event_id"]
            self.assertEqual(state["events"][event_id]["publication_flagged"], ["a", "b"])
            board.consume(state, event_id, "events")
            self.assertEqual(state["publication_reported"], ["a", "b"])

    def test_paused_scan_resumes_in_seconds(self):
        idle = {"name": "extended_away", "scan_seconds": 600, "review_seconds": 7200}
        self.assertEqual(watch_v2.next_scan_at(100.0, {"read_cursor": {"stage": "x"}}, idle), 105.0)
        self.assertEqual(watch_v2.next_scan_at(100.0, {"hydration_cursor": {"stage": "detail"}}, idle), 105.0)
        self.assertEqual(watch_v2.next_scan_at(100.0, {}, idle), 700.0)
        self.assertEqual(watch_v2.next_scan_at(100.0, None, idle), 700.0)


class LostReplyFixes(unittest.TestCase):
    """17 Nov 9 PM: a reply recorded while a scan was building its board was overwritten."""

    EVENT = "3e5eaff3-0000-4000-8000-000000000001"

    def sandbox(self, tmp):
        root = Path(tmp)
        (root / "publication.json").write_text("{}\n")
        handoff = root / "HANDOFF.md"
        handoff.write_text("Reviewed event IDs: []\n\n## 1. State\n- earlier\n\n## 2. Decisions\n\n")
        (root / "andy-items.json").write_text("[]")
        config = {
            "_state": str(root / "state.json"),
            "board_path": str(root / "board.json"),
            "digest_state_path": str(root / "digest-state.json"),
            "publication_report": str(root / "report.json"),
            "database": str(root / "missing.sqlite"),
        }
        state = board.empty_digest_state()
        state["started_at"] = 1.0
        state["posted"] = {"2000-01-01": {"9pm": {"event_id": self.EVENT, "at": 2.0}}}
        board.register_event(state, self.EVENT, "fp-9pm", "9pm", "events")
        (root / "digest-state.json").write_text(json.dumps(state))
        env = os.environ.copy()
        env.update({
            "ANDY_ITEMS": str(root / "andy-items.json"),
            "ANDY_ADVICE": str(root / "advice.json"),
            "ANDY_SEEN": str(root / "seen.json"),
            "BOARD_PY": str(HERE / "board.py"),
            "BOARD_PATH": str(root / "board.json"),
            "DIGEST_STATE": str(root / "digest-state.json"),
            "GUARDIAN_HANDOFF": str(handoff),
            "OPEN_THREADS_DIR": str(root),
            "T3_DB": str(root / "missing.sqlite"),
        })
        return root, config, env

    def record_proc(self, env):
        return subprocess.Popen(
            [sys.executable, str(BUILD / "bin" / "guardian-wake"), "record", self.EVENT, "Recorded the digest."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )

    def frame(self):
        return {"threads": {}, "members": {}, "runs": {}, "answers": {}}

    def with_pub(self, root):
        previous = os.environ.get("PUBLICATION_CONFIG")
        os.environ["PUBLICATION_CONFIG"] = str(root / "publication.json")
        return previous

    def restore_pub(self, previous):
        if previous is None:
            os.environ.pop("PUBLICATION_CONFIG", None)
        else:
            os.environ["PUBLICATION_CONFIG"] = previous

    def test_reply_recorded_while_scan_builds_board_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, env = self.sandbox(tmp)
            original = board.build_board
            procs = []

            def slow_build(*args, **kwargs):
                # The hub records its reply in the middle of the scan's board build.
                procs.append(self.record_proc(env))
                time.sleep(1.5)
                return original(*args, **kwargs)

            previous = self.with_pub(root)
            board.build_board = slow_build
            try:
                board.sync_after_scan(config, {"incidents": {}}, self.frame(), time.time(), "shadow")
            finally:
                board.build_board = original
                self.restore_pub(previous)
            out, err = procs[0].communicate(timeout=90)
            self.assertEqual(procs[0].returncode, 0, err)
            self.assertIn("recorded", out)
            saved = json.loads((root / "digest-state.json").read_text())
            self.assertIn(self.EVENT, saved.get("replies") or {})
            self.assertEqual(saved["last_consumed_fingerprint"], "fp-9pm")

    def test_reply_recorded_while_digest_posts_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, env = self.sandbox(tmp)
            path = root / "digest-state.json"
            snapshot = sample_board()
            snapshot["fingerprint"] = "fresh-after"
            outcome = {}

            def sender(_body):
                # A reply to the earlier digest lands while this post is in flight.
                proc = self.record_proc(env)
                try:
                    out, err = proc.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                    outcome["timeout"] = True
                outcome["out"] = out
                outcome["code"] = proc.returncode

            stale = json.loads(path.read_text())
            stale["posted"] = {}
            sent = board.maybe_post_digest(config, snapshot, local_at(15, 1), sender=sender, digest_state=stale, state_path=path)
            self.assertFalse(outcome.get("timeout"), "record waited on a lock held across the HTTP post")
            self.assertEqual(outcome.get("code"), 0)
            self.assertEqual(len(sent), 1)
            saved = json.loads(path.read_text())
            self.assertIn(self.EVENT, saved.get("replies") or {})
            self.assertEqual(saved["last_consumed_fingerprint"], "fp-9pm")
            new_id = sent[0]["event_id"]
            self.assertIn(new_id, saved["events"])
            day = datetime.fromtimestamp(local_at(15, 1)).astimezone().date().isoformat()
            self.assertEqual(saved["posted"][day]["3pm"]["event_id"], new_id)
            self.assertIn("2000-01-01", saved["posted"])

    def test_scan_keeps_its_own_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, _env = self.sandbox(tmp)
            report = {"findings": [{"key": "dirty:/x", "text": "x"}]}
            (root / "report.json").write_text(json.dumps(report))
            previous = self.with_pub(root)
            try:
                now = time.time()
                board.sync_after_scan(config, {"incidents": {}}, self.frame(), now, "shadow")
            finally:
                self.restore_pub(previous)
            saved = json.loads((root / "digest-state.json").read_text())
            self.assertEqual(saved["publication_quiet_since"].get("dirty:/x"), now)
            self.assertIn(self.EVENT, saved["events"])
            self.assertTrue((root / "board.json").exists())

    def test_record_lock_timeout_stays_retryable(self):
        import contextlib, io, runpy
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = root / "digest-state.json"
            handoff = root / "HANDOFF.md"
            handoff.write_text("Reviewed event IDs: []\n\n## 1. State\n- earlier\n\n## 2. Decisions\n\n")
            snap = root / "board.json"
            snap.write_text("{}")
            state = board.empty_digest_state()
            board.register_event(state, self.EVENT, "fp-9pm", "9pm", "events")
            board.atomic_json(digest, state)
            wake = runpy.run_path(str(BUILD / "bin" / "guardian-wake"), run_name="lock_timeout_case")
            record = wake["record"]
            record.__globals__["paths"] = lambda: {"digest": str(digest), "handoff": str(handoff), "board": str(snap)}
            record.__globals__["load_board"] = lambda: board
            saved_wait = board.DIGEST_LOCK_WAIT
            board.DIGEST_LOCK_WAIT = 0.05
            try:
                out = io.StringIO()
                with board.digest_lock(digest):
                    with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
                        record(self.EVENT, "Recorded the digest.")
                self.assertNotEqual(stop.exception.code, 0)
                self.assertIn("NOT RECORDED", out.getvalue())
                self.assertNotIn("recorded.", out.getvalue())
                self.assertNotIn(self.EVENT, handoff.read_text())
                retry = io.StringIO()
                with contextlib.redirect_stdout(retry):
                    record(self.EVENT, "Recorded the digest.")
            finally:
                board.DIGEST_LOCK_WAIT = saved_wait
            self.assertIn("recorded.", retry.getvalue())
            self.assertIn(self.EVENT, handoff.read_text())
            self.assertIn(self.EVENT, board.load_json(digest)["replies"])

    def test_posts_persist_and_a_reload_does_not_post_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, _env = self.sandbox(tmp)
            path = root / "digest-state.json"
            snapshot = sample_board()
            snapshot["fingerprint"] = "persisted"
            calls = []
            first = board.maybe_post_digest(config, snapshot, local_at(15, 2), sender=calls.append, state_path=path)
            second = board.maybe_post_digest(config, snapshot, local_at(15, 7), sender=calls.append, state_path=path)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(len(calls), 1)
            saved = json.loads(path.read_text())
            day = datetime.fromtimestamp(local_at(15, 2)).astimezone().date().isoformat()
            self.assertEqual(saved["posted"][day]["3pm"]["event_id"], first[0]["event_id"])
            self.assertEqual(saved["events"][first[0]["event_id"]]["fingerprint"], "persisted")
            self.assertIn(self.EVENT, saved["events"])


if __name__ == "__main__":
    unittest.main()
