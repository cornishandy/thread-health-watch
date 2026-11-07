#!/usr/bin/env python3
"""Rules and read-only replays for the v2 thread-health watcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import urllib.error

import metrics
import replay_v2 as replay
import triage
import watch_v2 as watch

DB = Path.home() / ".t3" / "userdata" / "statev2.sqlite"
STALL_ROOT = "28b6dd49-36f4-4851-984d-e403acb26c7e"
SILENT_ROOT = "6b57a494-c587-470a-a41b-11321f430396"
CHILD_ROOT = "f85c0aaf-7750-46e8-a855-17bce71961c6"
WINDOW_START = triage.stamp("2026-10-24T00:00:00Z")
WINDOW_END = triage.stamp("2026-10-26T12:00:00Z")
FAILED_STAMPS = ("2026-10-25T09:03:47Z", "2026-10-25T23:25:23Z")
HIST = {}


def setUpModule():
    global HIST
    roots = [STALL_ROOT, SILENT_ROOT, CHILD_ROOT]
    for when in FAILED_STAMPS:
        roots.extend(replay.roots_with_failed_tools(DB, when))
    HIST.update(replay.load_histories(DB, root_ids=roots, start=WINDOW_START, end=WINDOW_END, excluded=()))


def run(ordinal, status, requested, end, thread_id="root", run_id=None):
    return {
        "run_id": run_id or f"{thread_id}-{ordinal}",
        "thread_id": thread_id,
        "ordinal": ordinal,
        "status": status,
        "requested": requested,
        "end": end,
    }


def item(item_id, type_, status, start, end, ordinal=1, deadline=0):
    return {
        "id": item_id, "ordinal": ordinal, "type": type_, "status": status,
        "start": start, "end": end, "updated": end or start, "deadline": deadline,
    }


def answer(verdict, at, needs=False, running=False, message_id="m1"):
    return {"message_id": message_id, "at": at, "verdict": verdict, "needs_you": needs, "running": running}


def history(root="root", runs=None, items_by=None, answers_by=None, humans=None,
            members=None, requests=None, settled=0, archived=0, extra_runs=None):
    members = list(members or [root])
    if root not in members:
        members.insert(0, root)
    runs_by = {root: list(runs or [])}
    if extra_runs:
        runs_by.update(extra_runs)
    return {
        "root": root,
        "members": members,
        "runs": runs_by,
        "items": items_by or {},
        "answers": answers_by or {},
        "humans": {root: list(humans or [])},
        "requests": requests or {},
        "settled_at": settled,
        "archived_at": archived,
    }


def apply(state, hist, now, startup=False):
    return triage.apply_snapshot(state, triage.assemble_snap(hist, now), now, startup=startup)


def codes(state, code=None):
    found = [incident for incident in state["incidents"].values() if incident["status"] != "resolved"]
    if code:
        found = [incident for incident in found if incident["code"] == code]
    return found


class VerdictTests(unittest.TestCase):
    def test_not_wrapped_up_is_never_green(self):
        samples = [
            "Not wrapped up",
            "not wrapped up",
            "NOT WRAPPED UP",
            "Not wrapped up yet",
            "🔴 Not wrapped up",
        ]
        for text in samples:
            self.assertEqual(triage.parse_verdict(text), "red", text)
            self.assertNotEqual(triage.parse_verdict(text), "green", text)

    def test_last_marker_wins_and_red_pattern_beats_a_green_reading(self):
        self.assertEqual(triage.parse_verdict("Not wrapped up\nSafe to settle"), "green")
        self.assertEqual(triage.parse_verdict("Safe to settle\nNot wrapped up"), "red")
        self.assertEqual(triage.parse_verdict("itemprop=\"settle-yes\""), "green")
        self.assertEqual(triage.parse_verdict("itemprop=\"settle-no\""), "red")
        self.assertEqual(triage.parse_verdict("Don\u2019t settle yet"), "red")
        self.assertEqual(triage.parse_verdict("Wrapped up"), "green")

    def test_needs_you_requires_text_and_running_row_is_a_table_row(self):
        self.assertFalse(triage.needs_you("| Needs you | |"))
        self.assertFalse(triage.needs_you("The reader needs you to check the page."))
        self.assertTrue(triage.needs_you("| Needs you | pick a host |"))
        self.assertTrue(triage.running_row("| Running | check the page |"))
        self.assertFalse(triage.running_row("Running is a word"))


class RuleTests(unittest.TestCase):
    def test_run_ending_order(self):
        failed = run(1, "failed", 0, 100)
        self.assertEqual(triage.evaluate_run_ending(failed, None, False, False, False)["code"], "unanswered_or_failed")
        cancelled = run(1, "cancelled", 0, 100)
        self.assertEqual(triage.evaluate_run_ending(cancelled, None, True, False, False)["action"], "resolve")
        completed = run(1, "completed", 0, 100)
        green = answer("green", 90, needs=True)
        self.assertEqual(triage.evaluate_run_ending(completed, green, False, False, False)["action"], "resolve")
        red_ask = answer("red", 90, needs=True, message_id="ask-1")
        decision = triage.evaluate_run_ending(completed, red_ask, False, True, False)
        self.assertEqual(decision["action"], "notify")
        self.assertEqual(decision["code"], "human_blocked")
        red_running = answer("red", 90, running=True)
        watched = triage.evaluate_run_ending(completed, red_running, False, True, False)
        self.assertEqual(watched["action"], "watch")
        self.assertIsNone(watched["grace"])
        red = answer("red", 90)
        red_decision = triage.evaluate_run_ending(completed, red, False, False, False)
        self.assertEqual(red_decision["code"], "red_unresolved")
        self.assertEqual(red_decision["priority"], "low")
        self.assertEqual(red_decision["grace"], triage.GRACE["red_unresolved"])
        self.assertEqual(triage.evaluate_run_ending(completed, answer(None, 90), False, False, False)["code"], "no_verdict")

    def test_older_footer_does_not_answer_the_current_run(self):
        runs = [run(1, "completed", 0, 50), run(2, "completed", 80, 100)]
        hist = history(runs=runs, answers_by={"root-1": [answer("green", 40)], "root-2": []})
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 90))
        apply(state, hist, 100)
        apply(state, hist, 400)
        self.assertTrue(codes(state, "unanswered_or_failed"))
        self.assertEqual(codes(state, "unanswered_or_failed")[0]["status"], "due")

    def test_cancelled_incident_resolves_when_a_human_arrives(self):
        hist = history(runs=[run(1, "cancelled", 0, 100)], humans=[150])
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 50))
        apply(state, hist, 100)
        self.assertTrue(codes(state, "unanswered_or_failed"))
        apply(state, hist, 160)
        self.assertFalse(codes(state, "unanswered_or_failed"))

    def test_newer_busy_run_defers_and_a_later_green_resolves(self):
        runs = [run(1, "failed", 0, 100), run(2, "running", 120, 0), run(3, "completed", 200, 250)]
        hist = history(runs=runs, answers_by={"root-3": [answer("green", 240)]})
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 50))
        apply(state, hist, 100)
        opened = codes(state, "unanswered_or_failed")
        self.assertTrue(opened)
        apply(state, hist, 130)
        deferred = codes(state, "unanswered_or_failed")
        self.assertEqual(deferred[0]["status"], "watching")
        self.assertTrue(deferred[0].get("paused"))
        apply(state, hist, 250)
        self.assertFalse(codes(state, "unanswered_or_failed"))

    def test_settle_resolves_open_incidents(self):
        hist = history(runs=[run(1, "failed", 0, 100)], settled=300)
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 50))
        apply(state, hist, 100)
        apply(state, hist, 200)
        self.assertTrue(codes(state, "unanswered_or_failed"))
        apply(state, hist, 300)
        self.assertFalse(codes(state))

    def test_stall_is_due_at_45_minutes_even_while_a_command_runs(self):
        command = item("cmd", "command_execution", "running", 0, 0)
        hist = history(
            runs=[run(1, "running", 0, 0)],
            items_by={"root-1": [item("done", "command_execution", "completed", 0, 1000), command]},
        )
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 1000))
        apply(state, hist, 1000 + triage.STALL_DUE)
        stall = codes(state, "stalled")
        self.assertEqual(len(stall), 1)
        self.assertEqual(stall[0]["status"], "due")
        self.assertEqual(stall[0]["due_at"], 1000 + triage.STALL_DUE)
        snap = triage.assemble_snap(hist, 1000 + triage.STALL_DUE)
        self.assertTrue(any(entry["type"] == "command_execution" for entry in snap["running_items"]))

    def test_explicit_deadline_defers_and_progress_clears_before_due(self):
        running = item("cmd", "command_execution", "running", 0, 0, deadline=1000 + 10000)
        hist = history(runs=[run(1, "running", 0, 0)], items_by={"root-1": [running]})
        assessed = triage.assess_stall(0, 1000, 1000 + triage.STALL_DUE, [running])
        self.assertGreater(assessed["due_at"], 1000 + triage.STALL_DUE)
        cleared = history(
            runs=[run(1, "running", 0, 0)],
            items_by={"root-1": [item("done", "command_execution", "completed", 0, 1000 + 2000)]},
        )
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(cleared, 1000))
        apply(state, cleared, 1000 + triage.STALL_OPEN + 10)
        apply(state, cleared, 1000 + 2000)
        self.assertFalse(any(incident["status"] == "due" and incident["code"] == "stalled" for incident in state["incidents"].values()))

    def test_stalled_redelivery_is_three_hours(self):
        state = triage.new_state()
        incident, _logs = triage.open_incident(
            state, code="stalled", root="root", run=run(1, "running", 0, 0), now=3000,
            grace=None, status="due", due_at=2700, opened_at=1800, extra="1800",
        )
        incident["status"] = "delivered"
        incident["delivered_at"] = 3000
        self.assertFalse(triage.redelivery_ready(incident, 3000 + triage.REDELIVER))
        self.assertTrue(triage.redelivery_ready(incident, 3000 + triage.STALL_REDELIVER))

    def test_stopped_member_is_not_suppressed_by_a_busy_sibling(self):
        child = "child"
        sibling = "sibling"
        failure = run(1, "failed", 0, 100, thread_id=child)
        sibling_run = run(1, "running", 0, 0, thread_id=sibling)
        root_run = run(1, "running", 0, 0)
        hist = history(
            members=["root", child, sibling],
            runs=[root_run],
            extra_runs={child: [failure], sibling: [sibling_run]},
            answers_by={"root-1": [answer("red", 50, needs=True, running=True)]},
        )
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 50))
        apply(state, hist, 100)
        apply(state, hist, 100 + 300)
        stopped = codes(state, "stopped_member")
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["status"], "due")
        self.assertEqual(stopped[0]["member"], child)
        self.assertTrue(triage.assemble_snap(hist, 400)["busy_member"])

    def test_stopped_member_recovery_paths(self):
        child = "child"
        failed = run(1, "failed", 0, 100, thread_id=child)
        later = run(2, "completed", 200, 250, thread_id=child)
        hist = history(
            members=["root", child],
            runs=[run(1, "running", 0, 0)],
            extra_runs={child: [failed, later]},
            answers_by={later["run_id"]: [answer("green", 240)]},
        )
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 50))
        apply(state, hist, 100)
        self.assertTrue(codes(state, "stopped_member"))
        apply(state, hist, 250)
        self.assertFalse(codes(state, "stopped_member"))

    def test_failed_tool_clears_inside_the_grace_and_is_due_without_recovery(self):
        failed = item("tool", "dynamic_tool", "failed", 0, 100)
        recovered = item("next", "command_execution", "completed", 90, 110, ordinal=2)
        hist = history(runs=[run(1, "running", 0, 0)], items_by={"root-1": [failed, recovered]})
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 90))
        apply(state, hist, 100)
        apply(state, hist, 110)
        apply(state, hist, 100 + 600)
        self.assertFalse(any(incident["code"] == "failed_tool" and incident["status"] == "due" for incident in state["incidents"].values()))
        stuck = history(runs=[run(1, "running", 0, 0)], items_by={"root-1": [failed]})
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(stuck, 90))
        apply(state, stuck, 100)
        apply(state, stuck, 700)
        self.assertTrue(any(incident["code"] == "failed_tool" and incident["status"] == "due" for incident in state["incidents"].values()))

    def test_notices_are_once_per_id_and_carry_no_text(self):
        hist = history(
            runs=[run(1, "running", 0, 0)],
            answers_by={"root-1": [answer("red", 10, needs=True, message_id="ask-9")]},
            requests={"root": [{"id": "req-1", "start": 5, "end": 0, "status": "pending"}]},
            settled=1,
            members=["root", "child"],
            extra_runs={"child": [run(1, "running", 0, 0, thread_id="child")]},
        )
        state = triage.new_state()
        apply(state, hist, 20, startup=True)
        self.assertEqual(state["notices"], 0)
        apply(state, hist, 30)
        self.assertEqual(state["notices"], 0)
        rendered = json.dumps(state)
        self.assertNotIn("pick a host", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("NOTIFY", json.dumps(state.get("days")))

    def test_outcomes_fire_once_only_when_every_dependency_is_terminal(self):
        outcome = {"id": "outcome-1", "wake_when": "all_terminal", "dependency_thread_ids": ["a", "b"]}
        state = triage.new_state()
        self.assertFalse(triage.outcome_ready(outcome, {"a": "terminal"}))
        self.assertFalse(triage.outcome_ready(outcome, {"a": "terminal", "b": "unknown"}))
        self.assertFalse(triage.outcome_ready(outcome, {"a": "terminal", "b": "busy"}))
        self.assertFalse(triage.outcome_ready({**outcome, "wake_when": "other"}, {"a": "terminal", "b": "terminal"}))
        triage.apply_outcomes(state, [outcome], {"a": "terminal", "b": "terminal"}, 10)
        triage.apply_outcomes(state, [outcome], {"a": "terminal", "b": "terminal"}, 20)
        opened = [incident for incident in state["incidents"].values() if incident["code"] == "outcome_trigger"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["status"], "due")

    def test_review_change_is_counted_and_not_delivered(self):
        first = history(runs=[run(1, "running", 0, 0)], items_by={"root-1": [item("a", "command_execution", "completed", 0, 10)]})
        second = history(runs=[run(1, "running", 0, 0)], items_by={"root-1": [item("b", "command_execution", "completed", 0, 20, ordinal=2)]})
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(first, 20))
        apply(state, second, 30)
        self.assertEqual(state["review_changed"], 1)
        payload = triage.take_wake(state, 30, "event")
        self.assertIsNone(payload)


class PayloadTests(unittest.TestCase):
    def test_payload_is_metadata_only_and_strings_stay_short(self):
        state = triage.new_state()
        long_root = "thread:provider:claudeAgent:" + ("ab" * 120)
        self.assertGreater(len(long_root), 200)
        self.assertRegex(long_root, r"^[A-Za-z0-9:._%-]+$")
        triage.open_incident(
            state, code="unanswered_or_failed", root=long_root, run=run(1, "failed", 0, 10),
            now=400, grace=0, status="due", opened_at=10,
        )
        incident = next(iter(state["incidents"].values()))
        payload = triage.render_payload("event-1", [incident], 0, 400)
        self.assertEqual(set(payload), set(triage.PAYLOAD_KEYS))
        record = payload["incidents"][0]
        self.assertTrue(set(record) <= set(triage.PUBLIC_KEYS))
        self.assertEqual(record["root"], long_root)
        self.assertEqual(record["link"], "t3-thread://v1/" + long_root)
        self.assertLessEqual(len(triage.dump_payload(payload)), triage.MAX_BYTES)
        rendered = json.dumps(payload)
        self.assertNotIn("title", rendered)
        self.assertNotIn("sha256:", rendered)
        prose = triage.new_state()
        prose_root = "The model said hello and asked for a title"
        triage.open_incident(
            prose, code="unanswered_or_failed", root=prose_root, run=run(1, "failed", 0, 10),
            now=400, grace=0, status="due", opened_at=10,
        )
        hidden = triage.render_payload("event-2", list(prose["incidents"].values()), 0, 400)
        hidden_text = json.dumps(hidden)
        self.assertNotIn(prose_root, hidden_text)
        self.assertNotIn("hello", hidden_text)
        self.assertEqual(prose["incidents"][next(iter(prose["incidents"]))]["root"], prose_root)

    def test_over_limit_queue_keeps_every_incident(self):
        ready = []
        for index in range(12):
            ready.append({
                "id": f"id{index:02d}",
                "code": "unanswered_or_failed",
                "root": f"root-{index}",
                "member": None,
                "run_ordinal": index,
                "run_status": "failed",
                "opened_at": index,
                "due_at": index,
                "status": "due",
                "item_type": None,
                "item_status": None,
                "item_at": None,
                "outcome_id": None,
            })
        original = list(ready)
        payload, chosen, waiting = triage.pack_incidents(ready, 100, "event", True)
        self.assertGreater(payload["more_pending"], 0)
        self.assertEqual(len(payload["incidents"]) + payload["more_pending"], 12)
        self.assertEqual(len(chosen) + len(waiting), 12)
        self.assertEqual(len(original), 12)
        self.assertTrue(all(incident["status"] == "due" for incident in original))
        self.assertLessEqual(len(triage.dump_payload(payload)), triage.MAX_BYTES)

    def test_acks_and_unacked_redelivery(self):
        state = triage.new_state()
        incident, _logs = triage.open_incident(
            state, code="no_verdict", root="root", run=run(1, "completed", 0, 10),
            now=700, grace=0, status="due", opened_at=10,
        )
        payload = triage.take_wake(state, 700, "event", ack=False)
        self.assertIsNotNone(payload)
        self.assertEqual(incident["status"], "delivered")
        self.assertFalse(triage.redelivery_ready(incident, 700 + 3599))
        self.assertTrue(triage.redelivery_ready(incident, 700 + 3600))
        changed = triage.apply_acks(state, triage.load_ack_ids(['{"incident_ids":["' + incident["id"] + '"]}']), 8000)
        self.assertEqual(changed, [incident["id"]])
        self.assertEqual(incident["status"], "acked")
        self.assertFalse(triage.redelivery_ready(incident, 8000 + 99999))


class PresenceAndDeliveryTests(unittest.TestCase):
    def parse(self, idle=1, assertions="PreventUserIdleDisplaySleep 0", locked=False):
        return watch.parse_presence(
            '"HIDIdleTime" = ' + str(int(idle * 1e9)),
            assertions,
            {"IOConsoleLocked": locked, "IOConsoleUsers": [{"kCGSSessionOnConsoleKey": True}]},
        )

    def test_presence_policy(self):
        self.assertEqual(watch.policy(self.parse(10800, "PreventUserIdleDisplaySleep 1"))["name"], "active")
        electron = 'PreventUserIdleDisplaySleep 0\nPreventUserIdleSystemSleep 1\nNoIdleSleepAssertion named: "Electron"'
        self.assertEqual(watch.policy(self.parse(10800, electron))["name"], "extended_away")
        self.assertEqual(watch.policy(self.parse(10800, "PreventUserIdleDisplaySleep 1", locked=True))["name"], "extended_away")
        scans = [watch.policy(self.parse(value))["scan_seconds"] for value in (1, 600, 3600, 10800, 1)]
        self.assertEqual(scans, [5, 30, 120, 600, 5])
        with self.assertRaises(ValueError):
            watch.parse_presence("", "", {})

    def test_backoff_and_queue_pressure_do_not_drop_or_touch_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.sqlite"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE orchestration_v2_projection_runs(thread_id TEXT, status TEXT, requested_at TEXT);
                CREATE TABLE scheduled_task_webhook_pending(task_id TEXT, state TEXT);
            """)
            db.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('guardian','queued','1970-01-01T00:10:00Z')")
            db.commit()
            before = path.read_bytes()
            config = {"database": str(path), "guardian_thread_id": "guardian", "scheduled_task_id": "task"}
            pressure = watch.queue_pressure(config, 1000)
            self.assertTrue(pressure["blocked"])
            self.assertEqual(before, path.read_bytes())
            state = triage.new_state()
            triage.open_incident(
                state, code="unanswered_or_failed", root="root", run=run(1, "failed", 0, 10),
                now=1000, grace=0, status="due", opened_at=10,
            )
            sent = []
            self.assertFalse(watch.deliver(state, config, 1000, sender=sent.append, pressure=pressure))
            self.assertEqual(sent, [])
            self.assertEqual(codes(state, "unanswered_or_failed")[0]["status"], "due")
            db.execute("DELETE FROM orchestration_v2_projection_runs")
            db.commit()
            db.close()
            pressure = watch.queue_pressure(config, 1000)
            self.assertFalse(watch.deliver(state, config, 1000, sender=lambda payload: (_ for _ in ()).throw(urllib.error.URLError("secret-url")), pressure=pressure))
            self.assertNotIn("secret-url", json.dumps(state))
            self.assertGreater(state["retry_at"], 1000)
            self.assertFalse(watch.deliver(state, config, 1010, sender=sent.append, pressure=pressure))
            self.assertEqual(sent, [])
            self.assertTrue(watch.deliver(state, config, state["retry_at"], sender=sent.append, pressure=pressure))
            self.assertEqual(len(sent), 1)
            self.assertEqual(codes(state)[0]["status"], "delivered")

    def test_atomic_state_is_private_and_dry_run_does_not_write_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            watch.atomic_json(path, {"version": 2})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            config = {
                "database": str(DB),
                "enabled": True,
                "_state": Path(tmp) / "state.json",
                "_log": Path(tmp) / "watch.log",
                "_lock": Path(tmp) / "watch.lock",
                "_acks": Path(tmp) / "acks.jsonl",
                "_shadow": Path(tmp) / "shadow.log",
                "_outcomes": Path(tmp) / "missing.json",
                "excluded_thread_ids": [],
                "webhook_url": "https://example.invalid/placeholder",
            }
            path.unlink()

            def fake_scan(config, state, now):
                return state, [], set(), []

            original = watch.scan
            watch.scan = fake_scan
            try:
                watch.run_once(config, "dry-run", now=1000, presence={"idle_seconds": 1, "locked": False, "media_awake": False})
            finally:
                watch.scan = original
            self.assertFalse(path.exists())
            self.assertFalse((Path(tmp) / "shadow.log").exists())


class MetricsTests(unittest.TestCase):
    def test_codex_uses_the_last_cumulative_total_and_antigravity_without_columns_is_an_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "sessions" / "rollout-demo.jsonl"
            rollout.parent.mkdir()
            header = {"payload": {"thread_id": "thread-a", "thread_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}
            later = {"payload": {"info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}}}
            rollout.write_text(json.dumps(header) + "\n" + json.dumps(later) + "\n")
            counted = metrics.codex_tokens(["thread-a"], Path(tmp))
            self.assertEqual(counted["total_tokens"], 14)
            self.assertEqual(counted["input_tokens"], 10)
            database = Path(tmp) / "meta.sqlite"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE conversation_summaries(step_count INTEGER)")
            db.commit()
            db.close()
            found = metrics.antigravity_tokens([tmp])
            self.assertFalse(found["available"])
            self.assertEqual(found["label"], "estimate")
            self.assertIsNone(found["tokens"])
            self.assertTrue(found["step_count_column"])


class DatabaseReplayTests(unittest.TestCase):
    def test_connection_is_read_only(self):
        db = replay.connect(DB)
        try:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.Error):
                db.execute("CREATE TABLE watch_v2_should_not_exist(x)")
        finally:
            db.close()

    def test_stall_run_is_due_at_45_minutes_while_a_command_is_running(self):
        hist = HIST[STALL_ROOT]
        run_row = next(row for row in hist["runs"][STALL_ROOT] if row["ordinal"] == 10)
        items = hist["items"][run_row["run_id"]]
        completions = sorted(
            entry["end"] for entry in items
            if entry["type"] not in triage.IGNORED and entry["status"] == "completed" and entry["end"]
        )
        gap = max((right - left, left, right) for left, right in zip(completions, completions[1:]))
        self.assertGreaterEqual(gap[0], 589 * 60)
        anchor, command_end = gap[1], gap[2]
        due = anchor + triage.STALL_DUE
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, anchor))
        apply(state, hist, due)
        stall = [incident for incident in codes(state, "stalled") if incident["run_ordinal"] == 10]
        self.assertTrue(stall)
        self.assertEqual(stall[0]["status"], "due")
        self.assertEqual(stall[0]["due_at"], due)
        snap = triage.assemble_snap(hist, due)
        self.assertTrue(any(
            entry["type"] == "command_execution" and (entry.get("start") or 0) <= due < (entry.get("end") or 0)
            for entry in snap["running_items"]
        ))
        self.assertGreaterEqual(command_end - anchor, 589 * 60)

    def test_silent_completion_is_due(self):
        hist = HIST[SILENT_ROOT]
        run_row = next(row for row in hist["runs"][SILENT_ROOT] if row["ordinal"] == 51)
        self.assertFalse(hist["answers"].get(run_row["run_id"]))
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, run_row["requested"] + 1))
        apply(state, hist, run_row["end"])
        apply(state, hist, run_row["end"] + 300)
        opened = [incident for incident in codes(state, "unanswered_or_failed") if incident["run_ordinal"] == 51]
        self.assertTrue(opened)
        self.assertEqual(opened[0]["status"], "due")
        self.assertAlmostEqual(opened[0]["due_at"], run_row["end"] + 300, delta=1)

    def test_stopped_member_while_a_sibling_stays_busy(self):
        hist = HIST[CHILD_ROOT]
        target = triage.stamp("2026-10-25T04:02:03Z")
        failure = None
        failed_thread = None
        for thread_id, rows in hist["runs"].items():
            if thread_id == CHILD_ROOT:
                continue
            for row in rows:
                if row["status"] == "failed" and row["end"] and abs(row["end"] - target) <= 2:
                    failure = row
                    failed_thread = thread_id
        self.assertIsNotNone(failure)
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, failure["end"] - 1))
        apply(state, hist, failure["end"])
        apply(state, hist, failure["end"] + 300)
        stopped = [incident for incident in codes(state, "stopped_member") if incident.get("member") == failed_thread]
        self.assertTrue(stopped)
        self.assertEqual(stopped[0]["status"], "due")
        self.assertAlmostEqual(stopped[0]["due_at"], failure["end"] + 300, delta=1)
        snap = triage.assemble_snap(hist, failure["end"] + 300)
        self.assertTrue(snap["busy_member"])

    def test_failed_tools_that_recover_within_seconds_are_not_due(self):
        seen = 0
        for when in FAILED_STAMPS:
            target = triage.stamp(when)
            for root, hist in HIST.items():
                hits = []
                for rows in hist["items"].values():
                    for entry in rows:
                        if entry["type"] in triage.TOOLISH_SKIP:
                            continue
                        failed = entry["status"] in {"failed", "error"} or entry["type"] == "error"
                        if failed and entry.get("end") and abs(entry["end"] - target) <= 2:
                            hits.append(entry["end"])
                if not hits:
                    continue
                seen += 1
                state = triage.new_state()
                first = min(hits) - 1
                triage.baseline_snapshot(state, triage.assemble_snap(hist, first))
                for end in sorted(hits):
                    apply(state, hist, end)
                    apply(state, hist, end + 30)
                apply(state, hist, max(hits) + 600)
                due = [incident for incident in state["incidents"].values() if incident["code"] == "failed_tool" and incident["status"] == "due"]
                self.assertFalse(due)
        self.assertGreaterEqual(seen, 2)


class BlockerRegressionTests(unittest.TestCase):
    def test_retries_do_not_starve_unsent_incidents(self):
        state = triage.new_state()
        for index in range(80):
            triage.open_incident(
                state, code="unanswered_or_failed", root=f"root-{index}",
                run=run(index + 1, "failed", 100, 200, thread_id=f"root-{index}"),
                now=1000, grace=0, status="due", opened_at=200 + index,
            )
        sent = set()
        for at in range(1000, 1000 + 48 * 3600, 600):
            payload = triage.take_wake(state, at, f"page-{at}", ack=False)
            if payload:
                sent.update(incident["id"] for incident in payload["incidents"])
                self.assertLessEqual(len(triage.dump_payload(payload)), triage.MAX_BYTES)
        self.assertEqual(len(sent), 80)
        self.assertFalse(any(not incident.get("first_delivered_at") for incident in state["incidents"].values()))

    def test_paused_incident_is_not_ready_for_redelivery(self):
        completed = run(1, "completed", 1000, 1100)
        hist = history(runs=[completed], answers_by={completed["run_id"]: [answer("red", 1099)]})
        state = triage.new_state()
        apply(state, hist, 1800, startup=True)
        triage.take_wake(state, 1800, "first", ack=False)
        hist["runs"]["root"].append(run(2, "running", 2000, 0))
        apply(state, hist, 5500)
        ready = triage.ready_incidents(state, 5500)
        self.assertFalse(any(incident.get("paused") for incident in ready))
        self.assertTrue(any(incident.get("paused") for incident in state["incidents"].values()))

    def test_failed_child_between_polls_still_opens_stopped_member(self):
        completed = run(1, "completed", 1000, 1100)
        hist = history(
            runs=[completed],
            answers_by={completed["run_id"]: [answer("red", 1099, running=True)]},
            members=["root", "child"],
            extra_runs={"child": [run(1, "running", 1050, 0, thread_id="child")]},
        )
        state = triage.new_state()
        apply(state, hist, 1200, startup=True)
        self.assertIn("root", state["threads"]["root"]["members"])
        self.assertIn("child", state["threads"]["root"]["members"])
        hist["runs"]["child"][0].update(status="failed", end=1250)
        hist["runs"]["child"].append(run(2, "running", 1251, 0, thread_id="child"))
        apply(state, hist, 1600)
        stopped = codes(state, "stopped_member")
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["member"], "child")
        self.assertEqual(stopped[0]["run_ordinal"], 1)

    def test_silent_root_completion_before_retry_still_opens(self):
        hist = history(runs=[run(1, "running", 1000, 0)])
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 1100))
        hist["runs"]["root"][0].update(status="completed", end=1200)
        hist["runs"]["root"].append(run(2, "running", 1201, 0))
        apply(state, hist, 1600)
        opened = codes(state, "unanswered_or_failed")
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["run_ordinal"], 1)

    def test_long_ids_page_by_bytes_and_stay_intact(self):
        ready = []
        for index in range(6):
            root = f"thread:provider:claudeAgent:{index}-" + ("a" * 280)
            self.assertGreater(len(root), 200)
            ready.append({
                "id": f"{index:016x}",
                "code": "stopped_member",
                "root": "root",
                "member": root,
                "run_ordinal": index,
                "run_status": "failed",
                "opened_at": index,
                "due_at": index,
                "status": "due",
                "item_type": None,
                "item_status": None,
                "item_at": None,
                "outcome_id": None,
                "first_delivered_at": None,
            })
        payload, chosen, waiting = triage.pack_incidents(ready, 100, "event", True)
        self.assertGreater(payload["more_pending"], 0)
        self.assertEqual(len(chosen) + len(waiting), 6)
        self.assertLessEqual(len(triage.dump_payload(payload)), triage.MAX_BYTES)
        self.assertTrue(all(incident["member"] == record["member"] for incident, record in zip(chosen, payload["incidents"])))
        self.assertTrue(all(record["link"] == "t3-thread://v1/" + record["member"] for record in payload["incidents"]))

    def test_read_budget_is_wall_time_and_does_not_hold_a_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.sqlite"
            writer = sqlite3.connect(path)
            writer.execute("CREATE TABLE sample(x)")
            writer.execute("INSERT INTO sample VALUES (1)")
            writer.commit()
            reader = watch.connect(path, deadline_s=1.5)
            try:
                reader.execute("SELECT count(*) FROM sample").fetchone()
                self.assertFalse(reader.in_transaction)
            finally:
                reader.close()
            reader = watch.connect(path, deadline_s=0.01)
            try:
                reader.execute("SELECT count(*) FROM sample").fetchone()
                time.sleep(0.03)
                with self.assertRaises(sqlite3.Error):
                    reader.execute("SELECT count(*) FROM sample").fetchone()
            finally:
                reader.close()
            writer.close()

    def test_red_unresolved_is_low_from_the_start(self):
        completed = run(1, "completed", 0, 100)
        hist = history(runs=[completed], answers_by={completed["run_id"]: [answer("red", 90)]})
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 50))
        apply(state, hist, 100)
        incident = codes(state, "red_unresolved")[0]
        self.assertEqual(triage.priority_at(incident, incident["opened_at"]), "low")
        self.assertEqual(triage.priority_at(incident, incident["opened_at"] + 60), "low")
        self.assertEqual(incident["due_at"], incident["opened_at"] + 600)


class ExampleConfigTests(unittest.TestCase):
    def test_example_files_use_placeholders(self):
        root = Path(__file__).resolve().parent
        config = json.loads((root / "config.example.json").read_text())
        for key in ("database", "enabled", "excluded_thread_ids", "guardian_thread_id", "scheduled_task_id", "webhook_url"):
            self.assertIn(key, config)
        self.assertEqual(config["webhook_url"], "https://example.invalid/placeholder")
        self.assertEqual(config["guardian_thread_id"], "thread-example-guardian")
        outcomes = json.loads((root / "open-outcomes.example.json").read_text())["outcomes"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["wake_when"], "all_terminal")
        self.assertIn("dependency_thread_ids", outcomes[0])



class UnresolvedOutcomeTest(unittest.TestCase):
    def test_unresolved_dependency_blocks_trigger(self):
        outcome = {"id": "x", "wake_when": "all_terminal", "dependency_thread_ids": ["a"], "unresolved": ["original work"]}
        self.assertFalse(triage.outcome_ready(outcome, {"a": "terminal"}))
        outcome.pop("unresolved")
        self.assertTrue(triage.outcome_ready(outcome, {"a": "terminal"}))


def incident_events(logs):
    return [entry for entry in logs if entry.get("event") in {"opened", "due", "resolved"}]


class BaselineSealTests(unittest.TestCase):
    def test_same_scan_recovery_is_not_opened(self):
        failed = run(1, "failed", 10, 20)
        done = run(2, "completed", 21, 30)
        hist = history(runs=[run(1, "running", 0, 0)])
        state = triage.new_state()
        triage.baseline_snapshot(state, triage.assemble_snap(hist, 5))
        hist["runs"]["root"] = [failed, done]
        hist["answers"][done["run_id"]] = [answer("green", 29)]
        logs = apply(state, hist, 40)
        self.assertEqual(incident_events(logs), [])
        self.assertEqual(state["incidents"], {})

    def test_unsealed_root_does_not_walk_history(self):
        hist = history(runs=[run(1, "failed", 0, 10), run(2, "failed", 20, 30)])
        state = triage.new_state()
        state["baseline"] = True
        logs = apply(state, hist, 40)
        opened = [entry for entry in logs if entry["event"] == "opened"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(next(iter(state["incidents"].values()))["run_ordinal"], 2)
        self.assertIn("watermarks", state["threads"]["root"])
        again = apply(state, hist, 50)
        self.assertEqual(incident_events(again), [])

    def test_startup_seals_settled_history_and_second_pass_is_quiet(self):
        old = run(1, "completed", 0, 10, thread_id="settled-root")
        child_failed = run(1, "failed", 0, 12, thread_id="child")
        child_done = run(2, "completed", 13, 16, thread_id="child")
        current = run(1, "failed", 0, 20, thread_id="open-root")
        world = {
            "threads": {
                "settled-root": {"root": "settled-root", "settled": 5, "archived": 0},
                "open-root": {"root": "open-root", "settled": 0, "archived": 0},
                "child": {"root": "settled-root", "settled": 0, "archived": 0},
            },
            "members": {
                "settled-root": ["settled-root", "child"],
                "open-root": ["open-root"],
            },
            "runs": {
                "settled-root": [old],
                "child": [child_failed, child_done],
                "open-root": [current],
            },
            "items": {},
            "answers": {},
            "humans": {},
            "requests": {},
        }
        state = triage.new_state()
        logs, _waiting, _roots = watch.apply_world(state, world, 50)
        self.assertTrue(state["baseline"])
        self.assertEqual(state["threads"]["settled-root"]["watermarks"]["settled-root"], 10)
        self.assertEqual(state["threads"]["settled-root"]["watermarks"]["child"], 16)
        self.assertEqual(state["threads"]["open-root"]["watermarks"]["open-root"], 20)
        self.assertEqual([incident["root"] for incident in state["incidents"].values()], ["open-root"])
        self.assertEqual(len([entry for entry in logs if entry["event"] == "resolved"]), 0)
        again, _waiting, _roots = watch.apply_world(state, world, 60)
        self.assertEqual(incident_events(again), [])


class RealStartupScanTests(unittest.TestCase):
    """Read-only scans of the live database. State and logs stay in a temp directory."""

    def config(self, tmp):
        source = Path(__file__).resolve().parent / "shadow" / "shadow-config.json"
        config = watch.load_config(source)
        root = Path(tmp)
        config["_state"] = root / "state.json"
        config["_log"] = root / "watch.log"
        config["_lock"] = root / "watch.lock"
        config["_acks"] = root / "acks.jsonl"
        config["_shadow"] = root / "shadow.log"
        return config

    def test_second_scan_writes_no_incident_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp)
            presence = {"idle_seconds": 1, "locked": False, "media_awake": False}
            now = time.time()
            state, _report = watch.run_once(config, "shadow", now=now, presence=presence)
            shadow = config["_shadow"]
            first = shadow.read_text().splitlines() if shadow.exists() else []
            opened = [line for line in first if "event=opened " in line]
            self.assertGreaterEqual(len(opened), 8)
            self.assertLessEqual(len(opened), 80)
            self.assertLess(len(state["handled_runs"]), 200)
            self.assertGreater(len(state["threads"]), 200)
            self.assertTrue(all("watermarks" in row for row in state["threads"].values()))
            watch.run_once(config, "shadow", now=now + 5, presence=presence)
            second = shadow.read_text().splitlines()[len(first):]
            leaked = [line for line in second if "event=opened " in line or "event=due " in line or "event=resolved " in line]
            self.assertEqual(len(leaked), 0)

    def test_tiny_budget_split_seals_before_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp)
            fired = {"done": False}
            original = watch.BudgetConnection.execute

            def execute(self, sql, parameters=()):
                if not fired["done"] and self._budget_deadline and "LIMIT" in sql.upper():
                    fired["done"] = True
                    self._budget_deadline = time.monotonic() - 1
                return original(self, sql, parameters)

            watch.BudgetConnection.execute = execute
            try:
                state = triage.new_state()
                now = time.time()
                state, logs, _waiting, _roots = watch.scan(config, state, now)
            finally:
                watch.BudgetConnection.execute = original
            self.assertTrue(fired["done"])
            self.assertFalse(state.get("baseline"))
            self.assertTrue(state.get("read_cursor"))
            self.assertEqual(incident_events(logs), [])
            self.assertEqual(state["threads"], {})
            now = time.time()
            state, logs, _waiting, _roots = watch.scan(config, state, now)
            opened = [entry for entry in logs if entry["event"] == "opened"]
            self.assertTrue(state.get("baseline"))
            self.assertNotIn("read_cursor", state)
            self.assertGreaterEqual(len(opened), 8)
            self.assertLessEqual(len(opened), 80)
            self.assertGreater(len(state["threads"]), 200)
            self.assertTrue(all("watermarks" in row for row in state["threads"].values()))
            state, later, _waiting, _roots = watch.scan(config, state, now + 5)
            self.assertEqual(incident_events(later), [])

class GuardianJobTests(unittest.TestCase):
    def test_waiting_card_is_due_two_minutes_after_it_appears_and_resolves(self):
        state = triage.new_state()
        cards = [{"id": "req-1", "thread_id": "thread-a", "start": 1000}]
        logs = triage.apply_waiting_cards(state, cards, 1000)
        incident = next(item for item in state["incidents"].values() if item["code"] == "waiting_card")
        self.assertEqual(len([entry for entry in logs if entry["event"] == "opened"]), 1)
        self.assertEqual(incident["due_at"], 1120)
        self.assertEqual(incident["opened_at"], 1000)
        self.assertEqual(incident["status"], "watching")
        self.assertEqual(triage.priority_at(incident, 1000), "high")
        still = triage.apply_waiting_cards(state, cards, 1100)
        self.assertEqual([entry for entry in still if entry["event"] == "opened"], [])
        self.assertEqual(incident["status"], "watching")
        due = triage.apply_waiting_cards(state, cards, 1120)
        self.assertEqual(incident["status"], "due")
        self.assertTrue(any(entry["event"] == "due" for entry in due))
        incident["status"] = "delivered"
        triage.apply_acks(state, [incident["id"]], 1130)
        self.assertEqual(incident["status"], "acked")
        again = triage.apply_waiting_cards(state, cards, 1140)
        self.assertEqual(sum(item["code"] == "waiting_card" for item in state["incidents"].values()), 1)
        self.assertEqual([entry for entry in again if entry["event"] == "opened"], [])
        triage.apply_waiting_cards(state, [], 1200)
        self.assertEqual(incident["status"], "resolved")
        self.assertEqual(incident["resolve_reason"], "request_resolved")

    def test_a_later_green_run_does_not_resolve_a_live_card(self):
        done = run(1, "completed", 10, 40, thread_id="thread-a")
        world = {
            "threads": {"thread-a": {"root": "thread-a", "settled": 0, "archived": 0, "top": True}},
            "members": {"thread-a": ["thread-a"]},
            "runs": {"thread-a": [done]},
            "items": {},
            "answers": {"thread-a-1": [answer("green", 40)]},
            "humans": {"thread-a": []},
            "requests": {"thread-a": [{
                "id": "req-1", "start": 1000, "end": 0, "status": "pending", "kind": "user_input",
            }]},
            "cards": [{"id": "req-1", "thread_id": "thread-a", "start": 1000}],
            "signatures": [("thread-a", "msg-1")],
        }
        state = triage.new_state()
        watch.apply_world(state, world, 2000)
        card = next(item for item in state["incidents"].values() if item["code"] == "waiting_card")
        self.assertNotEqual(card["status"], "resolved")
        second, _waiting, _roots = watch.apply_world(state, world, 2010)
        self.assertEqual([entry for entry in second if entry["event"] == "resolved"], [])
        self.assertEqual(card["status"], "due")
        self.assertEqual(card["resolve_reason"] if "resolve_reason" in card else None, None)

    def test_unchanged_threads_open_nothing_on_the_second_scan(self):
        world = {
            "threads": {},
            "members": {},
            "runs": {},
            "items": {},
            "answers": {},
            "humans": {},
            "requests": {},
            "cards": [],
            "signatures": [("thread-a", "msg-1"), ("thread-b", "msg-2")],
        }
        state = triage.new_state()
        first, _waiting, _roots = watch.apply_world(state, world, 1000)
        second, _waiting, _roots = watch.apply_world(state, world, 1010)
        opened = [entry for entry in first if entry["event"] == "opened" and entry["code"] == "threads_changed"]
        self.assertEqual(len(opened), 1)
        self.assertEqual([entry for entry in second if entry["event"] == "opened"], [])
        self.assertEqual(sum(item["code"] == "threads_changed" for item in state["incidents"].values()), 1)

    def test_acked_signature_reopens_once_after_a_new_assistant_message(self):
        state = triage.new_state()
        first_pairs = [("thread-a", "msg-1")]
        opened = triage.apply_threads_changed(state, first_pairs, 100)
        self.assertEqual(len([entry for entry in opened if entry["event"] == "opened"]), 1)
        incident = next(item for item in state["incidents"].values() if item["code"] == "threads_changed")
        payload = triage.render_payload("event-1", [incident], 0, 100)
        rendered = triage.dump_payload(payload).decode()
        self.assertNotIn("thread-a", rendered)
        self.assertNotIn("msg-1", rendered)
        self.assertNotIn("root", payload["incidents"][0])
        self.assertEqual(payload["incidents"][0]["code"], "threads_changed")
        self.assertEqual(payload["incidents"][0]["priority"], "low")
        incident["status"] = "delivered"
        triage.apply_acks(state, [incident["id"]], 110)
        self.assertEqual(state["acked_threads_signature"], triage.threads_signature(first_pairs))
        self.assertEqual(triage.apply_threads_changed(state, first_pairs, 120), [])
        reopened = triage.apply_threads_changed(state, [("thread-a", "msg-2")], 130)
        self.assertEqual(len([entry for entry in reopened if entry["event"] == "opened"]), 1)
        quiet = triage.apply_threads_changed(state, [("thread-a", "msg-2")], 140)
        self.assertEqual([entry for entry in quiet if entry["event"] == "opened"], [])
        live = [
            item for item in state["incidents"].values()
            if item["code"] == "threads_changed" and item["status"] not in {"resolved", "acked"}
        ]
        self.assertEqual(len(live), 1)

    def test_a_question_does_not_call_notify(self):
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "database": str(root / "missing.sqlite"),
                "_state": root / "state.json",
                "_log": root / "watch.log",
                "_acks": root / "acks.jsonl",
                "_outcomes": None,
                "guardian_thread_id": "guardian",
                "scheduled_task_id": "task",
                "webhook_url": "https://example.invalid/placeholder",
                "excluded_thread_ids": [],
            }
            watch.atomic_json(config["_state"], triage.new_state())

            def fake_scan(config, state, now):
                triage.apply_waiting_cards(
                    state, [{"id": "req-q", "thread_id": "thread-q", "start": now - 30}], now,
                )
                return state, [{
                    "event": "opened", "code": "waiting_card", "id": "x", "root": "thread-q", "at": "t",
                }], {"thread-q"}, ["thread-q"]

            original_scan = watch.scan
            original_pressure = watch.queue_pressure
            watch.scan = fake_scan
            watch.queue_pressure = lambda _config, _now: {
                "blocked": True, "queued_reviews": 0, "pending_windows": 0, "oldest_queued_seconds": 0,
            }
            try:
                watch.run_once(
                    config, "deliver", now=10_000, sender=lambda _payload: None, notifier=notes.append,
                    presence={"idle_seconds": 1, "locked": False, "media_awake": False},
                )
            finally:
                watch.scan = original_scan
                watch.queue_pressure = original_pressure
        self.assertEqual(notes, [])



class ThreadsAckRaceTest(unittest.TestCase):
    def test_ack_of_delivered_signature_does_not_cover_later_change(self):
        state = triage.initial_state() if hasattr(triage, "initial_state") else triage.new_state()
        triage.apply_threads_changed(state, [("t1", "m1")], 1000)
        inc = next(i for i in state["incidents"].values() if i["code"] == "threads_changed")
        seen = inc["signature"]
        inc["status"] = "delivered"; inc["first_delivered_at"] = 1001; inc["delivered_at"] = 1001
        triage.apply_threads_changed(state, [("t1", "m2")], 1002)
        self.assertEqual(inc["signature"], seen)
        triage._remember_threads_ack(state, inc)
        inc["status"] = "acked"; inc["acked_at"] = 1003
        logs = triage.apply_threads_changed(state, [("t1", "m2")], 1004)
        reopened = [i for i in state["incidents"].values() if i["code"] == "threads_changed" and i["status"] not in {"resolved", "acked"}]
        self.assertEqual(len(reopened), 1)


class AckFormatTest(unittest.TestCase):
    def test_guardian_ack_lines(self):
        lines = ['{"incident_id": "abc", "decision": "fine", "at": "x"}', '{bad json', '{"incident_ids": ["d"]}', 'e']
        self.assertEqual(triage.load_ack_ids(lines), ["abc", "d", "e"])

if __name__ == "__main__":
    unittest.main()
