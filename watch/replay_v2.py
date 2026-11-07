#!/usr/bin/env python3
"""Replay recent T3 history through triage.py. Metadata only. Read-only.

The replay baselines at the window start. It does not repeat the daemon's
startup sweep of older terminal roots. Historical running state is inferred
from item and run timestamps because the database stores final status only.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

import triage

DB = Path.home() / ".t3" / "userdata" / "statev2.sqlite"
P = "orchestration_v2_projection_"
LIVE_CONFIG = Path.home() / "Library" / "Caches" / "thread-watch" / "adaptive-config.json"
CHUNK = 400


def connect(database):
    text = str(database)
    if text.startswith("file:"):
        uri = text if "mode=ro" in text else text + ("&" if "?" in text else "?") + "mode=ro"
    else:
        uri = Path(text).expanduser().resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 90
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    db.execute("BEGIN")
    return db


def output(label, data):
    rendered = json.dumps(data, sort_keys=True)
    if re.search(r"https?://|Authorization|Bearer ", rendered, re.I):
        raise ValueError("unsafe output blocked")
    print(label, rendered, flush=True)


def quantize(value):
    return round(float(value), 3)


def chunks(values, size=CHUNK):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def live_excluded():
    """Exclusion ids from the live config. The webhook is dropped immediately."""
    try:
        data = json.loads(LIVE_CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    data.pop("webhook_url", None)
    ids = list(data.get("excluded_thread_ids") or [])
    del data
    return ids


def thread_root(db, thread_id):
    row = db.execute(
        f"""SELECT json_extract(payload_json,'$.lineage.relationshipToParent') relation,
                   json_extract(payload_json,'$.lineage.rootThreadId') root_id
            FROM {P}threads WHERE thread_id=?""",
        (thread_id,),
    ).fetchone()
    if row and row["relation"] == "subagent" and row["root_id"]:
        return row["root_id"]
    return thread_id


def roots_with_failed_tools(database, when_iso, slack=2.0):
    """Root ids of non-assistant failed tools whose end is within slack seconds."""
    target = triage.stamp(when_iso)
    db = connect(database)
    try:
        low = triage.iso(target - 180)
        high = triage.iso(target + 180)
        rows = db.execute(
            f"""SELECT thread_id, type, status, updated_at,
                       json_extract(payload_json,'$.completedAt') completed
                FROM {P}turn_items
                WHERE updated_at >= ? AND updated_at <= ?
                  AND (status IN ('failed','error') OR type='error')""",
            (low, high),
        )
        roots = []
        for row in rows:
            if row["type"] in triage.TOOLISH_SKIP:
                continue
            end = triage.stamp(row["completed"]) or triage.stamp(row["updated_at"])
            if abs(end - target) <= slack:
                roots.append(thread_root(db, row["thread_id"]))
        return sorted(set(roots))
    finally:
        db.close()


def _item(row):
    status = row["status"]
    end = triage.stamp(row["completed"])
    updated = triage.stamp(row["updated_at"])
    if status not in triage.BUSY and not end:
        end = updated
    start = triage.stamp(row["started"])
    if not start:
        choices = [value for value in (end, updated) if value]
        start = min(choices) if choices else 0.0
    deadline = triage.stamp(row["deadline"] or row["deadline_at"] or row["due_at"])
    return {
        "id": row["turn_item_id"],
        "ordinal": row["ordinal"] or 0,
        "type": row["type"],
        "status": status,
        "start": start,
        "end": end,
        "updated": updated,
        "deadline": deadline,
    }


def _overlaps(run, start, end):
    if run["requested"] > end:
        return False
    if run["end"] and run["end"] < start:
        return False
    return True


def load_histories(database, root_ids=None, start=None, end=None, excluded=()):
    """Metadata histories for assemble_snap. Message text is classified and dropped."""
    end = time.time() if end is None else end
    start = end - 72 * 3600 if start is None else start
    excluded = set(excluded or ())
    wanted = None if root_ids is None else set(root_ids)
    db = connect(database)
    try:
        threads = {}
        members = {}
        for row in db.execute(
            f"""SELECT thread_id,
                       json_extract(payload_json,'$.lineage.relationshipToParent') relation,
                       json_extract(payload_json,'$.lineage.rootThreadId') root_id,
                       json_extract(payload_json,'$.settledAt') settled_at,
                       json_extract(payload_json,'$.archivedAt') archived_payload,
                       archived_at, deleted_at
                FROM {P}threads WHERE deleted_at IS NULL"""
        ):
            root = row["thread_id"]
            if row["relation"] == "subagent" and row["root_id"]:
                root = row["root_id"]
            archived = triage.stamp(row["archived_at"] or row["archived_payload"])
            threads[row["thread_id"]] = {
                "root": root,
                "settled": triage.stamp(row["settled_at"]),
                "archived": archived,
            }
            members.setdefault(root, [])
            if row["thread_id"] not in members[root]:
                members[root].append(row["thread_id"])
        selected = []
        for root, group in list(members.items()):
            if root in excluded:
                continue
            group = [tid for tid in group if tid not in excluded]
            members[root] = group
            if wanted is not None and root not in wanted:
                continue
            info = threads.get(root) or {"settled": 0, "archived": 0}
            if info.get("archived") and info["archived"] < start and wanted is None:
                continue
            selected.append(root)
        selected_threads = {tid for root in selected for tid in members.get(root, [root])}
        runs = {}
        for row in db.execute(
            f"""SELECT run_id, thread_id, ordinal, status, requested_at, completed_at
                FROM {P}runs"""
        ):
            if row["thread_id"] not in selected_threads:
                continue
            runs.setdefault(row["thread_id"], []).append({
                "run_id": row["run_id"],
                "thread_id": row["thread_id"],
                "ordinal": row["ordinal"] or 0,
                "status": row["status"],
                "requested": triage.stamp(row["requested_at"]),
                "end": triage.stamp(row["completed_at"]),
            })
        for rows in runs.values():
            rows.sort(key=lambda run: (run["requested"], run["ordinal"]))
        requests = {}
        for row in db.execute(
            f"""SELECT runtime_request_id, thread_id, status, created_at, resolved_at
                FROM {P}runtime_requests"""
        ):
            if row["thread_id"] not in selected_threads:
                continue
            requests.setdefault(row["thread_id"], []).append({
                "id": row["runtime_request_id"],
                "start": triage.stamp(row["created_at"]),
                "end": triage.stamp(row["resolved_at"]),
                "status": row["status"],
            })
        humans = {}
        for row in db.execute(
            f"""SELECT thread_id, created_at FROM {P}messages WHERE role='user'"""
        ):
            if row["thread_id"] not in selected:
                continue
            humans.setdefault(row["thread_id"], []).append(triage.stamp(row["created_at"]))
        for values in humans.values():
            values.sort()
        run_ids = [
            run["run_id"]
            for rows in runs.values()
            for run in rows
            if _overlaps(run, start, end)
        ]
        items = {}
        answers = {}
        for group in chunks(run_ids):
            marks = ",".join("?" for _ in group)
            for row in db.execute(
                f"""SELECT turn_item_id, run_id, ordinal, type, status, updated_at,
                           json_extract(payload_json,'$.startedAt') started,
                           json_extract(payload_json,'$.completedAt') completed,
                           json_extract(payload_json,'$.deadline') deadline,
                           json_extract(payload_json,'$.deadlineAt') deadline_at,
                           json_extract(payload_json,'$.dueAt') due_at
                    FROM {P}turn_items WHERE run_id IN ({marks})""",
                group,
            ):
                items.setdefault(row["run_id"], []).append(_item(row))
            for row in db.execute(
                f"""SELECT run_id, turn_item_id, updated_at,
                           json_extract(payload_json,'$.completedAt') completed,
                           json_extract(payload_json,'$.text') text
                    FROM {P}turn_items
                    WHERE type='assistant_message' AND status='completed' AND run_id IN ({marks})""",
                group,
            ):
                text = row["text"] or ""
                at = triage.stamp(row["completed"]) or triage.stamp(row["updated_at"])
                answers.setdefault(row["run_id"], []).append(triage.classify_answer(text, row["turn_item_id"], at))
                del text
        for rows in answers.values():
            rows.sort(key=lambda answer: answer["at"])
        histories = {}
        for root in selected:
            group = members.get(root, [root])
            root_runs = {tid: runs.get(tid, []) for tid in group}
            ids = [run["run_id"] for rows in root_runs.values() for run in rows]
            info = threads.get(root) or {"settled": 0, "archived": 0}
            histories[root] = {
                "root": root,
                "members": list(group),
                "runs": root_runs,
                "items": {run_id: items.get(run_id, []) for run_id in ids if run_id in items},
                "answers": {run_id: answers.get(run_id, []) for run_id in ids if run_id in answers},
                "humans": {root: humans.get(root, [])},
                "requests": {tid: requests.get(tid, []) for tid in group},
                "settled_at": info.get("settled") or 0,
                "archived_at": info.get("archived") or 0,
            }
        return histories
    finally:
        db.close()


def _add(times, start, end, when):
    if when and start < when <= end:
        times.add(quantize(when))


def interesting_times(history, start, end):
    """Run boundaries, stall thresholds, failed-tool grace, humans, requests."""
    times = set()
    root = history["root"]
    completions = []
    busy_events = []
    for rows in history["runs"].values():
        for run in rows:
            _add(times, start, end, run["requested"])
            _add(times, start, end, run["end"])
            busy_events.append((run["requested"], 1))
            if run["end"]:
                busy_events.append((run["end"], -1))
            for item in history["items"].get(run["run_id"], []):
                if item["type"] in triage.IGNORED:
                    continue
                if item.get("status") == "completed" and item.get("end"):
                    completions.append(item["end"])
                    busy_events.append((item["end"], 0))
                failed = item.get("status") in {"failed", "error"} or item["type"] == "error"
                if item["type"] == "assistant_message" or not failed or not item.get("end"):
                    continue
                _add(times, start, end, item["end"])
                later = None
                for other in history["items"].get(run["run_id"], []):
                    if other["type"] in triage.IGNORED or other.get("status") != "completed":
                        continue
                    if other.get("end") and item["end"] < other["end"]:
                        later = other["end"] if later is None else min(later, other["end"])
                if later and later <= item["end"] + triage.GRACE["failed_tool"]:
                    _add(times, start, end, later)
                else:
                    grace_at = item["end"] + triage.GRACE["failed_tool"]
                    run_end = run["end"] or end
                    if grace_at < run_end:
                        _add(times, start, end, grace_at)
    completions = sorted(set(completions))
    # Stall thresholds while the lineage is busy and quiet.
    busy_events.sort(key=lambda item: (item[0], -item[1] if False else 0))
    grouped = {}
    for when, delta in busy_events:
        grouped.setdefault(when, []).append(delta)
    ordered = sorted(grouped)
    busy = 0
    busy_since = 0.0
    last = 0.0
    completion_set = set(completions)
    for index, when in enumerate(ordered):
        for delta in grouped[when]:
            busy += delta
            if when in completion_set:
                last = max(last, when)
        if busy <= 0:
            busy = 0
            busy_since = 0.0
            continue
        if not busy_since:
            busy_since = when
        nxt = ordered[index + 1] if index + 1 < len(ordered) else end
        anchor = max(last, busy_since)
        if nxt - anchor >= triage.STALL_OPEN:
            _add(times, start, end, anchor + triage.STALL_OPEN)
            _add(times, start, end, anchor + triage.STALL_DUE)
            _add(times, start, end, nxt)
    for when in history["humans"].get(root, []):
        _add(times, start, end, when)
    for rows in history["requests"].values():
        for request in rows:
            _add(times, start, end, request["start"])
            _add(times, start, end, request["end"])
    _add(times, start, end, history.get("settled_at"))
    _add(times, start, end, history.get("archived_at"))
    return times


def build_events(histories, start, end):
    start_q = quantize(start)
    end_q = quantize(end)
    baseline = []
    events = []
    for history in histories.values():
        baseline.append((start_q, triage.assemble_snap(history, start_q)))
        for when in sorted(interesting_times(history, start_q, end_q)):
            events.append((when, triage.assemble_snap(history, when)))
    events.sort(key=lambda pair: pair[0])
    return baseline, events


def opened_counts(state):
    counts = Counter()
    for bucket in state.get("days", {}).values():
        for code, count in (bucket.get("opened") or {}).items():
            counts[code] += count
    return dict(counts)


def main():
    end = time.time()
    start = end - 72 * 3600
    started = time.monotonic()
    histories = load_histories(DB, start=start, end=end, excluded=live_excluded())
    baseline, events = build_events(histories, start, end)
    state, wakes = triage.simulate(baseline, events, quantize(end), ack=True)
    by_day = Counter(wake["day"] for wake in wakes)
    hours = max((end - start) / 3600, 1)
    output("roots", len(histories))
    output("events", len(events))
    output("wakes_by_local_day", dict(sorted(by_day.items())))
    output("wakes", len(wakes))
    output("wakes_per_24h", round(len(wakes) / hours * 24, 2))
    output("incidents_opened_by_code", opened_counts(state))
    output("ack_assumption", "each delivery acked")
    output("unacked_redelivery_events", triage.unacked_redelivery_events(state, end))
    output("seconds", round(time.monotonic() - started, 1))


if __name__ == "__main__":
    main()
