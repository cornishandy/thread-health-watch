#!/usr/bin/env python3
"""Read-only daily metrics for the thread-health watcher. Standard library only.

Codex totals come from the last cumulative usage object in a matched rollout.
Antigravity has no per-session token counter in local metadata; that number
is null and labeled an estimate.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import triage

USAGE_KEYS = ("total_token_usage", "thread_token_usage", "turn_token_usage")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
SKIP_DIR = ("cache", "cookies", "code cache", "gpu", "shader")
SKIP_NAME = ("oauth", "cookie", "credential")


def day_slice(state, day):
    bucket = (state.get("days") or {}).get(day) or {}
    return {
        "day": day,
        "opened": dict(bucket.get("opened") or {}),
        "resolved": dict(bucket.get("resolved") or {}),
        "delivered": dict(bucket.get("delivered") or {}),
        "class": dict(bucket.get("class") or {}),
        "deliveries": bucket.get("deliveries") or 0,
        "payload_bytes": bucket.get("payload_bytes") or 0,
        "notices": bucket.get("notices") or 0,
        "review_changed": (state.get("review_by_day") or {}).get(day, 0),
        "wakes": (state.get("wakes_by_day") or {}).get(day, 0),
    }


def _bounds(day):
    import datetime as dt
    zone = dt.datetime.now().astimezone().tzinfo
    start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    end = start + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return (
        start.astimezone(dt.timezone.utc).strftime(fmt),
        end.astimezone(dt.timezone.utc).strftime(fmt),
    )


def guardian_runs(database, thread_ids, day):
    """Count runs requested on a local day for the configured guardian chain."""
    ids = [thread_id for thread_id in thread_ids or [] if thread_id]
    if not database or not ids:
        return {"threads": {}, "total": 0, "label": "unconfigured"}
    low, high = _bounds(day)
    text = str(database)
    if text.startswith("file:"):
        uri = text if "mode=ro" in text else text + ("&" if "?" in text else "?") + "mode=ro"
    else:
        uri = Path(text).expanduser().resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        counts = {}
        for index in range(0, len(ids), 400):
            group = ids[index:index + 400]
            marks = ",".join("?" for _ in group)
            rows = db.execute(
                f"""SELECT thread_id, count(*)
                    FROM orchestration_v2_projection_runs
                    WHERE thread_id IN ({marks}) AND requested_at >= ? AND requested_at < ?
                    GROUP BY thread_id""",
                [*group, low, high],
            )
            counts.update({row[0]: row[1] for row in rows})
        return {"threads": counts, "total": sum(counts.values()), "label": "measured"}
    finally:
        db.close()


def _header_ids(obj, found):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"thread_id", "parent_thread_id"} and isinstance(value, str):
                found.add(value)
            else:
                _header_ids(value, found)
    elif isinstance(obj, list):
        for value in obj[:30]:
            _header_ids(value, found)


def _last_usage(obj, holder):
    if isinstance(obj, dict):
        for key in USAGE_KEYS:
            inner = obj.get(key)
            if isinstance(inner, dict) and any(field in inner for field in TOKEN_FIELDS):
                holder.append(inner)
        for value in obj.values():
            _last_usage(value, holder)
    elif isinstance(obj, list):
        for value in obj[:30]:
            _last_usage(value, holder)


def codex_tokens(thread_ids, sessions_dir):
    """Sum the last cumulative usage of rollout files whose header names a chain thread.

    Lines are not added together. A later total_token_usage replaces the earlier one.
    """
    wanted = {thread_id for thread_id in thread_ids or [] if thread_id}
    path = Path(sessions_dir).expanduser() if sessions_dir else None
    if not wanted or path is None or not path.exists():
        return {"matched": 0, "files": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "label": "unconfigured"}
    totals = {field: 0 for field in TOKEN_FIELDS}
    matched = 0
    files = 0
    for rollout in path.glob("**/rollout-*.jsonl"):
        files += 1
        header_ids = set()
        try:
            lines = []
            with rollout.open("r", errors="replace") as handle:
                for _ in range(80):
                    line = handle.readline()
                    if not line:
                        break
                    lines.append(line)
            for line in lines:
                try:
                    _header_ids(json.loads(line), header_ids)
                except json.JSONDecodeError:
                    continue
            name_hit = any(thread_id and thread_id in rollout.name for thread_id in wanted)
            if not (header_ids & wanted) and not name_hit:
                continue
            last = None
            with rollout.open("r", errors="replace") as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    found = []
                    _last_usage(obj, found)
                    if found:
                        last = found[-1]
            if not last:
                continue
            matched += 1
            for field in TOKEN_FIELDS:
                totals[field] += int(last.get(field) or 0)
        except OSError:
            continue
    totals.update(matched=matched, files=files, label="measured" if matched else "none")
    return totals


def _sqlite_columns(path):
    found = []
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        db.execute("PRAGMA query_only=ON")
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if "fts" in table.lower():
                continue
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            found.append((table, columns))
    finally:
        db.close()
    return found


def antigravity_tokens(roots=None):
    """Look for per-session token counters. Opaque blobs and message bodies are not read."""
    if roots is None:
        home = Path.home()
        roots = [home / ".gemini", home / "Library" / "Application Support" / "Antigravity"]
    token_columns = []
    step_count = False
    databases = 0
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in __import__("os").walk(root):
            base = Path(dirpath).name.lower()
            if any(skip in base for skip in SKIP_DIR):
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if not any(skip in name.lower() for skip in SKIP_DIR)]
            for name in filenames:
                low = name.lower()
                if any(skip in low for skip in SKIP_NAME):
                    continue
                if not low.endswith((".db", ".sqlite", ".sqlite3")):
                    continue
                databases += 1
                try:
                    tables = _sqlite_columns(Path(dirpath) / name)
                except sqlite3.Error:
                    continue
                for _table, columns in tables:
                    if "step_count" in columns:
                        step_count = True
                    token_columns.extend(
                        column for column in columns
                        if any(word in column.lower() for word in ("token", "usage"))
                    )
    available = bool(token_columns)
    return {
        "available": available,
        "label": "measured" if available else "estimate",
        "tokens": None if not available else None,
        "databases": databases,
        "step_count_column": step_count,
        "token_columns": sorted(set(token_columns)),
        "note": (
            "Per-session token columns were found."
            if available else
            "No per-session input, output, or total token columns under ~/.gemini "
            "or Application Support/Antigravity. conversation_summaries.step_count "
            "is not a token total. gen_metadata and step payloads were not read."
        ),
    }


def report(state, *, day, database=None, thread_ids=(), sessions_dir=None, antigravity_roots=None):
    return {
        "incidents": day_slice(state, day),
        "guardian_runs": guardian_runs(database, thread_ids, day) if database else {"threads": {}, "total": 0, "label": "unconfigured"},
        "codex": codex_tokens(thread_ids, sessions_dir),
        "antigravity": antigravity_tokens(antigravity_roots),
    }
