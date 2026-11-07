#!/usr/bin/env python3
"""T3 thread-health watcher, version 2. Read-only on the app database.

State, logs, payloads and notifications hold ids, statuses, timestamps and
codes only. The webhook URL is never logged. Message text is classified in
memory and then dropped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import triage

P = "orchestration_v2_projection_"
CHUNK = 400
ANSWER_CHUNK = 40
SCAN_DEADLINE = 1.5
CLOSED_REQUEST = {"resolved", "cancelled", "expired"}


class BudgetExceeded(Exception):
    def __init__(self, cursor):
        super().__init__("read budget exceeded")
        self.cursor = cursor


class BudgetConnection(sqlite3.Connection):
    """Wall-clock budget checked before each statement, not only between SQL ticks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._budget_deadline = 0.0

    def renew(self, seconds):
        if not seconds:
            self._budget_deadline = 0.0
            self.set_progress_handler(None, 0)
            return
        self._budget_deadline = time.monotonic() + float(seconds)
        deadline = self._budget_deadline
        self.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

    def execute(self, sql, parameters=()):
        if self._budget_deadline and time.monotonic() > self._budget_deadline:
            raise sqlite3.OperationalError("read budget exceeded")
        return super().execute(sql, parameters)

    def executemany(self, sql, seq):
        if self._budget_deadline and time.monotonic() > self._budget_deadline:
            raise sqlite3.OperationalError("read budget exceeded")
        return super().executemany(sql, seq)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, separators=(",", ":"), sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def append_lines(path, lines):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 500_000:
        os.replace(path, path.with_suffix(path.suffix + ".1"))
    kept = []
    for line in lines:
        if "http://" in line or "https://" in line or "Authorization" in line:
            continue
        kept.append(line)
    if not kept:
        return
    with path.open("a") as out:
        for line in kept:
            out.write(dt.datetime.now().astimezone().isoformat(timespec="seconds") + " " + line + "\n")
    os.chmod(path, 0o600)


def connect(database, deadline_s=SCAN_DEADLINE):
    """Read-only connection. Does not leave a transaction open.

    deadline_s is wall time from this call. A later statement fails when that
    budget is spent, including time spent in Python between statements.
    """
    text = str(database)
    if text.startswith("file:"):
        uri = text if "mode=ro" in text else text + ("&" if "?" in text else "?") + "mode=ro"
    else:
        uri = Path(text).expanduser().resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2, factory=BudgetConnection, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.renew(deadline_s)
    db.execute("PRAGMA query_only=ON")
    return db


def _budget_error(exc):
    text = str(exc).lower()
    return "budget" in text or "interrupt" in text or "abort" in text


def _transaction(db, fetch):
    """One short read transaction. The budget is wall time, then the lock is released."""
    db.renew(SCAN_DEADLINE)
    try:
        db.execute("BEGIN")
        value = fetch()
        db.commit()
        return value
    except Exception:
        try:
            db.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        db.renew(0)


def _batch_size(resume, stage, phase, default):
    if resume and resume.get("stage") == stage and resume.get("phase") == phase:
        return max(1, int(resume.get("batch") or 1))
    return default


def _paged(db, sql, params, phase, stage, resume, default):
    batch = _batch_size(resume, stage, phase, default)
    rows = []
    offset = 0
    while True:
        limit = batch
        start = offset

        def fetch(limit=limit, start=start):
            found = []
            for row in db.execute(sql, tuple(params) + (limit, start)):
                found.append({key: row[key] for key in row.keys()})
            return found

        try:
            page = _transaction(db, fetch)
        except sqlite3.OperationalError as exc:
            if not _budget_error(exc):
                raise
            smaller = max(1, limit // 2) if limit > 1 else 1
            raise BudgetExceeded({"stage": stage, "phase": phase, "batch": smaller}) from exc
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < limit:
            break
    return rows


def _in_groups(db, sql_for_marks, ids, phase, stage, resume, default):
    batch = _batch_size(resume, stage, phase, default)
    rows = []
    ids = [item for item in ids if item]
    index = 0
    while index < len(ids):
        group = ids[index:index + batch]
        marks = ",".join("?" for _ in group)
        sql = sql_for_marks(marks)

        def fetch(sql=sql, group=tuple(group)):
            return [{key: row[key] for key in row.keys()} for row in db.execute(sql, group)]

        try:
            page = _transaction(db, fetch)
        except sqlite3.OperationalError as exc:
            if not _budget_error(exc):
                raise
            smaller = max(1, batch // 2) if batch > 1 else 1
            raise BudgetExceeded({"stage": stage, "phase": phase, "batch": smaller}) from exc
        rows.extend(page)
        index += batch
    return rows


def _latest_assistant_messages(db, thread_ids, stage, resume):
    """Latest completed assistant message per thread. streaming=0. One short transaction per batch."""
    found = {}
    ids = [thread_id for thread_id in thread_ids if thread_id]
    if not ids:
        return found
    batch = _batch_size(resume, stage, "signatures", 20)
    index = 0
    sql = f"""SELECT message_id FROM {P}messages
              WHERE thread_id=? AND role='assistant' AND streaming=0
              ORDER BY created_at DESC, message_id DESC LIMIT 1"""
    while index < len(ids):
        group = ids[index:index + batch]

        def fetch(group=tuple(group)):
            rows = {}
            for thread_id in group:
                row = db.execute(sql, (thread_id,)).fetchone()
                rows[thread_id] = row["message_id"] if row else ""
            return rows

        try:
            page = _transaction(db, fetch)
        except sqlite3.OperationalError as exc:
            if not _budget_error(exc):
                raise
            smaller = max(1, batch // 2) if batch > 1 else 1
            raise BudgetExceeded({"stage": stage, "phase": "signatures", "batch": smaller}) from exc
        found.update(page)
        index += len(group)
    return found


def chunks(values, size=CHUNK):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def command(argv):
    return subprocess.run(argv, capture_output=True, timeout=5, check=True).stdout


def parse_presence(hid, assertions, session):
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', hid)
    if not match:
        raise ValueError("Mac idle signal unavailable")
    idle = int(match.group(1)) / 1_000_000_000
    if isinstance(session, list):
        session = session[0] if session else {}
    if not isinstance(session, dict) or "IOConsoleLocked" not in session:
        raise ValueError("Mac lock signal unavailable")
    locked = bool(session["IOConsoleLocked"])
    users = session.get("IOConsoleUsers", [])
    on_console = any(item.get("kCGSSessionOnConsoleKey", False) for item in users)
    display_awake = re.search(r"^\s*PreventUserIdleDisplaySleep\s+(\d+)\s*$", assertions, re.M)
    video = bool(display_awake and int(display_awake.group(1))) and on_console and not locked
    return {"idle_seconds": idle, "locked": locked or not on_console, "media_awake": video}


class MacPresence:
    def __init__(self):
        self.assertions_at = 0
        self.assertions = ""
        self.locked_since = None

    def sample(self, now):
        hid = command(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"]).decode("utf8", errors="replace")
        session = plistlib.loads(command(["/usr/sbin/ioreg", "-n", "Root", "-d", "1", "-a"]))
        if now - self.assertions_at >= 20:
            self.assertions = command(["/usr/bin/pmset", "-g", "assertions"]).decode("utf8", errors="replace")
            self.assertions_at = now
        presence = parse_presence(hid, self.assertions, session)
        if presence["locked"]:
            self.locked_since = self.locked_since if self.locked_since is not None else now
            presence["idle_seconds"] = max(300, presence["idle_seconds"], now - self.locked_since)
        else:
            self.locked_since = None
        return presence


def policy(presence):
    idle = presence["idle_seconds"]
    if not presence["locked"] and (presence["media_awake"] or idle < 300):
        return {"name": "active", "scan_seconds": 5, "review_seconds": 120}
    if idle < 1800:
        return {"name": "away", "scan_seconds": 30, "review_seconds": 600}
    if idle < 7200:
        return {"name": "long_away", "scan_seconds": 120, "review_seconds": 1800}
    return {"name": "extended_away", "scan_seconds": 600, "review_seconds": 7200}


def load_config(path):
    config = json.loads(Path(path).read_text())
    base = Path(path).resolve().parent

    def resolve(key, default):
        value = config.get(key) or default
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = base / target
        return target

    config["_state"] = resolve("state_path", "state.json")
    config["_log"] = resolve("log_path", "watch.log")
    config["_lock"] = resolve("lock_path", "watch.lock")
    config["_acks"] = resolve("acks_path", "acks.jsonl")
    config["_shadow"] = resolve("shadow_log_path", "shadow.log")
    config["_outcomes"] = resolve("open_outcomes_path", "open-outcomes.example.json")
    return config


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


def read_world(database, excluded, needed_answer_runs=None, busy_run_ids=None, resume=None, stage="frame"):
    """Projection metadata. Each batch is its own short read transaction.

    Assistant text is copied out and classified only after that transaction
    commits. needed_answer_runs None loads every completed assistant. A set
    limits the read. busy_run_ids limits turn-item metadata. None loads items
    for runs currently busy. A batch that exceeds the wall-clock budget raises
    BudgetExceeded with a cursor for the next scan.
    """
    excluded = set(excluded or ())
    db = connect(database, deadline_s=SCAN_DEADLINE)
    try:
        thread_rows = _paged(
            db,
            f"""SELECT thread_id,
                       json_extract(payload_json,'$.lineage.parentThreadId') parent_id,
                       json_extract(payload_json,'$.lineage.relationshipToParent') relation,
                       json_extract(payload_json,'$.lineage.rootThreadId') root_id,
                       json_extract(payload_json,'$.settledAt') settled_at,
                       json_extract(payload_json,'$.archivedAt') archived_payload,
                       archived_at
                FROM {P}threads WHERE deleted_at IS NULL
                ORDER BY thread_id LIMIT ? OFFSET ?""",
            (),
            "threads",
            stage,
            resume,
            CHUNK,
        )
        threads = {}
        members = {}
        for row in thread_rows:
            root = row["thread_id"]
            if row["relation"] == "subagent" and row["root_id"]:
                root = row["root_id"]
            if root in excluded or row["thread_id"] in excluded:
                if root in excluded:
                    continue
            threads[row["thread_id"]] = {
                "root": root,
                "settled": triage.stamp(row["settled_at"]),
                "archived": triage.stamp(row["archived_at"] or row["archived_payload"]),
                "top": row["parent_id"] is None,
            }
            if row["thread_id"] in excluded:
                continue
            members.setdefault(root, [])
            if root in excluded:
                continue
            if row["thread_id"] not in members[root]:
                members[root].append(row["thread_id"])
        run_rows = _paged(
            db,
            f"""SELECT run_id, thread_id, ordinal, status, requested_at, completed_at
                FROM {P}runs ORDER BY run_id LIMIT ? OFFSET ?""",
            (),
            "runs",
            stage,
            resume,
            CHUNK,
        )
        runs = {}
        for row in run_rows:
            if row["thread_id"] not in threads:
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
        request_rows = _paged(
            db,
            f"""SELECT runtime_request_id, thread_id, kind, status, created_at, resolved_at
                FROM {P}runtime_requests ORDER BY runtime_request_id LIMIT ? OFFSET ?""",
            (),
            "requests",
            stage,
            resume,
            CHUNK,
        )
        requests = {}
        cards = []
        for row in request_rows:
            if row["thread_id"] not in threads:
                continue
            requests.setdefault(row["thread_id"], []).append({
                "id": row["runtime_request_id"],
                "start": triage.stamp(row["created_at"]),
                "end": triage.stamp(row["resolved_at"]),
                "status": row["status"],
                "kind": row["kind"],
            })
            info = threads[row["thread_id"]]
            if (
                row["kind"] == "user_input"
                and row["status"] == "pending"
                and row["thread_id"] not in excluded
                and not info.get("settled")
            ):
                cards.append({
                    "id": row["runtime_request_id"],
                    "thread_id": row["thread_id"],
                    "start": triage.stamp(row["created_at"]),
                })
        top_ids = sorted(
            thread_id for thread_id, info in threads.items()
            if info.get("top") and not info.get("settled") and thread_id not in excluded
        )
        latest_messages = _latest_assistant_messages(db, top_ids, stage, resume)
        signatures = [(thread_id, latest_messages.get(thread_id, "")) for thread_id in top_ids]
        human_rows = _paged(
            db,
            f"""SELECT thread_id, created_at, message_id
                FROM {P}messages WHERE role='user' ORDER BY message_id LIMIT ? OFFSET ?""",
            (),
            "humans",
            stage,
            resume,
            CHUNK,
        )
        humans = {}
        for row in human_rows:
            if row["thread_id"] not in threads:
                continue
            humans.setdefault(row["thread_id"], []).append(triage.stamp(row["created_at"]))
        if busy_run_ids is None:
            busy_run_ids = [run["run_id"] for rows in runs.values() for run in rows if run["status"] in triage.BUSY]
        item_rows = _in_groups(
            db,
            lambda marks: f"""SELECT turn_item_id, run_id, ordinal, type, status, updated_at,
                           json_extract(payload_json,'$.startedAt') started,
                           json_extract(payload_json,'$.completedAt') completed,
                           json_extract(payload_json,'$.deadline') deadline,
                           json_extract(payload_json,'$.deadlineAt') deadline_at,
                           json_extract(payload_json,'$.dueAt') due_at
                    FROM {P}turn_items WHERE run_id IN ({marks})""",
            list(busy_run_ids),
            "items",
            stage,
            resume,
            CHUNK,
        )
        items = {}
        for row in item_rows:
            items.setdefault(row["run_id"], []).append(_item(row))
        if needed_answer_runs is None:
            answer_rows = _paged(
                db,
                f"""SELECT run_id, turn_item_id, updated_at,
                           json_extract(payload_json,'$.completedAt') completed,
                           json_extract(payload_json,'$.text') text
                    FROM {P}turn_items
                    WHERE type='assistant_message' AND status='completed'
                    ORDER BY turn_item_id LIMIT ? OFFSET ?""",
                (),
                "answers",
                stage,
                resume,
                ANSWER_CHUNK,
            )
        else:
            answer_rows = _in_groups(
                db,
                lambda marks: f"""SELECT run_id, turn_item_id, updated_at,
                                 json_extract(payload_json,'$.completedAt') completed,
                                 json_extract(payload_json,'$.text') text
                          FROM {P}turn_items
                          WHERE type='assistant_message' AND status='completed' AND run_id IN ({marks})""",
                sorted(needed_answer_runs),
                "answers",
                stage,
                resume,
                ANSWER_CHUNK,
            )
        answers = {}
        for row in answer_rows:
            text = row.get("text") or ""
            at = triage.stamp(row.get("completed")) or triage.stamp(row.get("updated_at"))
            answers.setdefault(row["run_id"], []).append(triage.classify_answer(text, row["turn_item_id"], at))
            row["text"] = ""
        for rows in answers.values():
            rows.sort(key=lambda answer: answer["at"])
        return {
            "threads": threads,
            "members": members,
            "runs": runs,
            "items": items,
            "answers": answers,
            "humans": humans,
            "requests": requests,
            "cards": cards,
            "signatures": signatures,
        }
    finally:
        db.close()


def history_for(world, root):
    members = list(world["members"].get(root) or [root])
    if root not in members:
        members.insert(0, root)
    runs = {tid: world["runs"].get(tid, []) for tid in members}
    ids = [run["run_id"] for rows in runs.values() for run in rows]
    info = world["threads"].get(root) or {}
    return {
        "root": root,
        "members": members,
        "runs": runs,
        "items": {run_id: world["items"][run_id] for run_id in ids if run_id in world["items"]},
        "answers": {run_id: world["answers"][run_id] for run_id in ids if run_id in world["answers"]},
        "humans": {root: world["humans"].get(root, [])},
        "requests": {tid: world["requests"].get(tid, []) for tid in members},
        "settled_at": info.get("settled") or 0,
        "archived_at": info.get("archived") or 0,
    }


def _latest(runs):
    return runs[-1] if runs else None


def _live_threads(world, now):
    found = set()
    for thread_id, rows in world["requests"].items():
        for request in rows:
            if request["start"] <= now and request["status"] not in CLOSED_REQUEST and (not request["end"] or request["end"] > now):
                found.add(thread_id)
    return found


def _busy_root(world, root, now):
    for thread_id in world["members"].get(root, [root]):
        run = _latest(world["runs"].get(thread_id, []))
        if not run:
            continue
        ended = bool(run["end"] and run["end"] <= now)
        status = run["status"] if ended else "running"
        if status in triage.BUSY:
            return True
    return False


def select_roots(world, state, now, excluded):
    excluded = set(excluded or ())
    startup = not state.get("baseline")
    open_roots = {
        incident["root"]
        for incident in state["incidents"].values()
        if incident["status"] not in {"resolved", "acked"}
    }
    live = _live_threads(world, now)
    chosen = []
    for root, members in world["members"].items():
        if root in excluded:
            continue
        info = world["threads"].get(root) or {}
        if info.get("archived") and root not in open_roots:
            continue
        remembered = state["threads"].get(root) or {}
        latest = _latest(world["runs"].get(root, []))
        changed = False
        if latest and (remembered.get("run_id") != latest["run_id"] or remembered.get("status") != latest["status"]):
            changed = True
        member_now = {}
        busy = False
        for thread_id in members:
            run = _latest(world["runs"].get(thread_id, []))
            if not run:
                continue
            ended = bool(run["end"] and run["end"] <= now)
            status = run["status"] if ended else "running"
            member_now[thread_id] = [run["run_id"], status]
            if status in triage.BUSY:
                busy = True
        prior_members = remembered.get("members") or {}
        members_changed = member_now != prior_members
        root_live = any(thread_id in live for thread_id in members)
        settled = bool(info.get("settled") and info["settled"] <= now)
        if startup:
            if settled and not busy and not root_live and root not in open_roots:
                continue
            if not latest and not busy and not root_live:
                continue
        else:
            if not (busy or root_live or changed or members_changed or root in open_roots):
                continue
        chosen.append(root)
    return sorted(chosen)


def answer_runs_for(world, roots, state, startup):
    needed = set()
    open_incidents = [
        incident for incident in state["incidents"].values()
        if incident["status"] not in {"resolved", "acked"}
    ]
    for root in roots:
        for thread_id in world["members"].get(root, [root]):
            rows = world["runs"].get(thread_id, [])
            if not rows:
                continue
            if startup:
                needed.update(run["run_id"] for run in rows)
                continue
            marks = ((state.get("threads") or {}).get(root) or {}).get("watermarks") or {}
            mark = marks.get(thread_id) or 0
            needed.add(rows[-1]["run_id"])
            for run in rows:
                if (run.get("end") or 0) > mark:
                    needed.add(run["run_id"])
            for incident in open_incidents:
                if incident["root"] != root and incident.get("member") != thread_id:
                    continue
                floor = incident.get("run_ordinal") or 0
                for run in rows:
                    if run["ordinal"] >= floor:
                        needed.add(run["run_id"])
    return needed


def dependency_map(world, excluded, now):
    excluded = set(excluded or ())
    live = _live_threads(world, now)
    known = {}
    for thread_id, info in world["threads"].items():
        if thread_id in excluded or info.get("root") in excluded:
            continue
        run = _latest(world["runs"].get(thread_id, []))
        busy = thread_id in live
        if run:
            ended = bool(run["end"] and run["end"] <= now)
            status = run["status"] if ended else "running"
            busy = busy or status in triage.BUSY
        known[thread_id] = "busy" if busy else "terminal"
    return known


def load_outcomes(path):
    data = load_json(path, {})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("outcomes") or [])
    return []


def _seal_snap(world, root, now):
    """Watermarks and latest run ids only. No answers, items, or incidents."""
    members = list(world["members"].get(root) or [root])
    if root not in members:
        members.insert(0, root)
    runs = {thread_id: world["runs"].get(thread_id, []) for thread_id in members}
    viewed = []
    busy = False
    current = None
    for thread_id in members:
        latest = _latest(runs.get(thread_id) or [])
        if not latest:
            continue
        end = latest.get("end") or 0
        status = latest["status"] if end and end <= now else "running"
        if status in triage.BUSY:
            busy = True
        entry = dict(latest)
        entry["status"] = status
        entry["ended"] = bool(end and end <= now)
        if thread_id == root:
            current = entry
        else:
            viewed.append(entry | {"thread_id": thread_id})
    return {
        "root": root,
        "as_of": now,
        "run": current,
        "busy": busy,
        "signature": None,
        "last_completed": 0,
        "members": viewed,
        "history": {"members": members, "runs": runs},
    }


def seal_world(state, world, now, excluded=()):
    """Set every root and member watermark to its latest terminal end."""
    excluded = set(excluded or ())
    for root in world.get("members") or {}:
        if root in excluded:
            continue
        triage.seal_snapshot(state, _seal_snap(world, root, now))


def apply_world(state, world, now, excluded=(), outcomes=()):
    startup = not state.get("baseline")
    roots = select_roots(world, state, now, excluded)
    if startup:
        # Seal before any evaluation, including when this scan is the tick that
        # finishes a budget-split read. Unsealed threads must not be walked.
        seal_world(state, world, now, excluded)
    logs = []
    waiting = set()
    for root in roots:
        before = state["notices"]
        logs.extend(triage.apply_snapshot(state, triage.assemble_snap(history_for(world, root), now), now, startup=startup))
        if state["notices"] > before:
            waiting.add(root)
    if "cards" in world:
        logs.extend(triage.apply_waiting_cards(state, world.get("cards") or [], now))
    if "signatures" in world:
        logs.extend(triage.apply_threads_changed(state, world.get("signatures") or [], now))
    if startup:
        state["baseline"] = True
    states = {}
    known = dependency_map(world, excluded, now)
    for outcome in outcomes or []:
        for thread_id in outcome.get("dependency_thread_ids") or []:
            states[thread_id] = triage.dependency_status(thread_id, known, set(excluded))
    logs.extend(triage.apply_outcomes(state, outcomes, states, now))
    return logs, waiting, roots


def format_log(entry):
    return "event={event} code={code} id={id} root={root} at={at}".format(**entry)


def summary(state, policy_name, mode):
    codes = {}
    due = 0
    for incident in state["incidents"].values():
        if incident["status"] == "resolved":
            continue
        codes[incident["code"]] = codes.get(incident["code"], 0) + 1
        if incident["status"] == "due":
            due += 1
    body = {
        "mode": mode,
        "policy": policy_name,
        "threads": len(state["threads"]),
        "busy_roots": sum(1 for row in state["threads"].values() if row.get("busy")),
        "incidents": codes,
        "due": due,
        "review_changed": state.get("review_changed") or 0,
    }
    rendered = json.dumps(body, sort_keys=True)
    if re.search(r"https?://|Authorization|Bearer ", rendered, re.I):
        raise ValueError("unsafe output blocked")
    return body


def scan(config, state, now):
    excluded = set(config.get("excluded_thread_ids") or [])
    startup = not state.get("baseline")
    resume = state.get("read_cursor") or None
    try:
        frame = read_world(
            config["database"], excluded, needed_answer_runs=set(), busy_run_ids=[],
            resume=resume if resume and resume.get("stage") == "frame" else None,
            stage="frame",
        )
    except BudgetExceeded as paused:
        state["read_cursor"] = paused.cursor
        return state, [], set(), []
    roots = select_roots(frame, state, now, excluded)
    answers = answer_runs_for(frame, roots, state, startup)
    busy_ids = []
    for root in roots:
        for thread_id in frame["members"].get(root, []):
            for run in frame["runs"].get(thread_id, []):
                ended = bool(run["end"] and run["end"] <= now)
                status = run["status"] if ended else "running"
                if status in triage.BUSY or run["status"] in triage.BUSY:
                    busy_ids.append(run["run_id"])
    try:
        if answers or busy_ids:
            detail = read_world(
                config["database"], excluded, needed_answer_runs=answers, busy_run_ids=busy_ids,
                resume=resume if resume and resume.get("stage") == "detail" else None,
                stage="detail",
            )
            frame["items"] = detail["items"]
            frame["answers"] = detail["answers"]
    except BudgetExceeded as paused:
        state["read_cursor"] = paused.cursor
        return state, [], set(), []
    state.pop("read_cursor", None)
    outcomes = load_outcomes(config["_outcomes"]) if config.get("_outcomes") else []
    logs, waiting, roots = apply_world(state, frame, now, excluded, outcomes)
    return state, logs, waiting, roots


def queue_pressure(config, now):
    database = config["database"]
    db = connect(database, deadline_s=5)
    try:
        queued, oldest = db.execute(
            f"""SELECT count(*), min(requested_at) FROM {P}runs
                WHERE thread_id=? AND status IN ('queued','preparing','starting')""",
            (config.get("guardian_thread_id"),),
        ).fetchone()
        windows = db.execute(
            "SELECT count(*) FROM scheduled_task_webhook_pending WHERE task_id=?",
            (config.get("scheduled_task_id"),),
        ).fetchone()[0]
    finally:
        db.close()
    return {
        "queued_reviews": queued,
        "pending_windows": windows,
        "oldest_queued_seconds": max(0, now - triage.stamp(oldest)) if oldest else 0,
        "blocked": queued + windows >= 1,
    }


def deliver(state, config, now, sender=None, pressure=None):
    """POST due incidents. Failures record a backoff and never the exception text."""
    if pressure and pressure["blocked"]:
        return False
    if now < state.get("retry_at", 0):
        return False
    outbox = state.get("outbox")
    if not outbox:
        if state.get("last_delivery") and now < state["last_delivery"] + triage.WAKE_GAP:
            return False
        ready = triage.ready_incidents(state, now)
        if not ready:
            return False
        event_id = str(uuid.uuid4())
        payload, chosen, _waiting = triage.pack_incidents(ready, now, event_id, triage.low_slot_open(state, now))
        if not chosen:
            return False
        outbox = {"event_id": event_id, "payload": payload, "ids": [incident["id"] for incident in chosen]}
        state["outbox"] = outbox
    payload = outbox["payload"]
    try:
        if sender:
            sender(payload)
        else:
            request = urllib.request.Request(
                config["webhook_url"],
                data=triage.dump_payload(payload),
                headers={"Content-Type": "application/json", "User-Agent": "T3-thread-guardian/2.0"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as result:
                if not 200 <= result.status < 300:
                    raise ValueError("Webhook refused delivery")
    except (OSError, ValueError, urllib.error.URLError):
        state["delivery_failures"] = state.get("delivery_failures", 0) + 1
        state["retry_at"] = now + min(600, 15 * 2 ** min(state["delivery_failures"], 6))
        state["last_error"] = "delivery_failed"
        return False
    incidents = [state["incidents"][item] for item in outbox["ids"] if item in state["incidents"]]
    state["_last_sent"] = {"event_id": outbox["event_id"], "count": len(outbox["ids"])}
    notice = triage.note_delivery(state, payload, incidents, now)
    state["outbox"] = None
    state["delivery_failures"] = 0
    state["retry_at"] = 0
    state["last_error"] = None
    state["_day_notice"] = notice
    return True


_read_error_notified_at = 0.0


def notify_read_error(config, now=None):
    """One read-error notification per 30 minutes. The message carries no error text."""
    global _read_error_notified_at
    now = time.time() if now is None else now
    state = None
    try:
        if config.get("_state"):
            state = load_json(config["_state"], None)
    except (OSError, ValueError, TypeError):
        state = None
    last = _read_error_notified_at
    if isinstance(state, dict):
        try:
            last = max(last, float(state.get("error_notification_at") or 0))
        except (TypeError, ValueError):
            pass
    if now - last <= 1800:
        return
    notify("watch read error")
    _read_error_notified_at = now
    if isinstance(state, dict) and state.get("version") == 2:
        state["error_notification_at"] = now
        try:
            atomic_json(config["_state"], state)
        except OSError:
            pass


def notify(message, runner=None):
    if not message or "http://" in message or "https://" in message:
        return
    if runner:
        runner(message)
        return
    safe = message.replace("\\", "").replace('"', "")
    subprocess.run(
        ["/usr/bin/osascript", "-e", f'display notification "{safe}" with title "T3 needs you"'],
        capture_output=True,
        timeout=5,
    )


def read_acks(path, state, now):
    file = Path(path)
    if not file.exists():
        return []
    return triage.apply_acks(state, triage.load_ack_ids(file.read_text().splitlines()), now)


def status_report(state):
    due = sum(1 for incident in state.get("incidents", {}).values() if incident.get("status") == "due")
    return {
        "heartbeat": state.get("heartbeat"),
        "policy": state.get("policy"),
        "last_scan": state.get("last_scan"),
        "last_delivery": state.get("last_delivery"),
        "delivery_failures": state.get("delivery_failures"),
        "last_error": state.get("last_error"),
        "open_incidents": sum(1 for incident in state.get("incidents", {}).values() if incident.get("status") not in {"resolved", "acked"}),
        "due": due,
        "review_changed": state.get("review_changed"),
    }


def run_once(config, mode, now=None, sender=None, notifier=None, presence=None):
    now = time.time() if now is None else now
    if mode == "dry-run":
        state = triage.new_state()
    else:
        state = load_json(config["_state"], triage.new_state())
        if not isinstance(state, dict) or state.get("version") != 2:
            raise SystemExit("Guardian state requires review")
    if presence is None:
        presence = MacPresence().sample(now)
    selected = policy(presence)
    state, logs, waiting, _roots = scan(config, state, now)
    state["last_scan"] = now
    state["policy"] = selected["name"]
    state["heartbeat"] = now
    report = summary(state, selected["name"], mode)
    lines = [format_log(entry) for entry in logs]
    if mode == "dry-run":
        return state, report
    if mode == "shadow":
        append_lines(config["_shadow"], lines)
        atomic_json(config["_state"], state)
        return state, report
    read_acks(config["_acks"], state, now)
    try:
        pressure = queue_pressure(config, now)
    except (OSError, sqlite3.Error):
        pressure = {"blocked": False, "queued_reviews": 0, "pending_windows": 0, "oldest_queued_seconds": 0}
        state["last_error"] = "queue_pressure"
    state["queue_pressure"] = {key: pressure[key] for key in ("blocked", "queued_reviews", "pending_windows")}
    delivered = deliver(state, config, now, sender=sender, pressure=pressure)
    if state.get("_day_notice"):
        notify("watch over budget", runner=notifier)
    state.pop("_day_notice", None)
    if delivered:
        sent = state.pop("_last_sent", {}) or {}
        append_lines(config["_log"], ["delivered event=" + str(sent.get("event_id")) + " count=" + str(sent.get("count", 0))])
    append_lines(config["_log"], lines)
    atomic_json(config["_state"], state)
    report["delivered"] = delivered
    return state, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.status:
        state = load_json(config["_state"], triage.new_state()) or triage.new_state()
        print(json.dumps(status_report(state), sort_keys=True))
        return
    if not config.get("database"):
        raise SystemExit("Missing guardian configuration")
    mode = "dry-run" if args.dry_run else "shadow" if args.shadow else "deliver"
    if mode == "deliver" and not config.get("webhook_url"):
        raise SystemExit("Missing guardian delivery route")
    if not config.get("enabled", False):
        raise SystemExit("Guardian disabled")
    lock_path = Path(config["_lock"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Guardian already running")
        stopping = False

        def stop(*_args):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        presence_reader = MacPresence()
        next_scan = 0
        last_profile = None
        while not stopping:
            now = time.time()
            try:
                config = load_config(args.config)
                if not config.get("enabled", False):
                    append_lines(config["_log"], ["disabled"])
                    return
                presence = presence_reader.sample(now)
                selected = policy(presence)
                if selected["name"] != last_profile:
                    next_scan = 0
                    last_profile = selected["name"]
                if now < next_scan and mode != "dry-run":
                    pass
                else:
                    _state, report = run_once(config, mode, now=now, presence=presence)
                    next_scan = now + selected["scan_seconds"]
                    if mode == "dry-run":
                        print(json.dumps(report, sort_keys=True))
                        return
            except (OSError, sqlite3.Error, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
                if mode == "dry-run":
                    print(json.dumps({"error": type(error).__name__}))
                    raise SystemExit(1)
                append_lines(config["_log"], ["read_failed type=" + type(error).__name__])
                notify_read_error(config, now)
            if args.once or mode == "dry-run":
                return
            time.sleep(5)


if __name__ == "__main__":
    main()
