#!/usr/bin/env python3
"""Quiet-hub board, fingerprint and afternoon digest gate.

Metadata only: ids, kinds, links, ages and keys. Message text is classified
in memory and dropped. Paths come from arguments, config or env; the live
path is the default. This module never opens live/config.json.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import triage

STALL_DUE = 2700  # 45 minutes, same moment a stall becomes due
CARD_GRACE = 600  # 10 minutes
QUIET_NEW = 3600  # a finding is not "new" before 60 quiet minutes
ACTIVE_RUN = ("queued", "starting", "running", "preparing", "waiting")
FAILURE = triage.FAILURE
BUSY = triage.BUSY
SLOTS = (("3pm", 15), ("9pm", 21))
SCHEDULED_PREFIX = "scheduled-task-message:"
HOME = Path.home()
LIVE_BOARD = HOME / "Library/Caches/thread-watch-v2/live/board.json"
LIVE_DIGEST = HOME / "Library/Caches/thread-watch-v2/live/digest-state.json"
LIVE_REPORT = HOME / "Library/Caches/thread-watch-v2/publication-report.json"
LIVE_PUB_CONFIG = HOME / "Library/Caches/thread-watch-v2/publication-config.json"
LIVE_DB = HOME / ".t3/userdata/statev2.sqlite"


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


DIGEST_LOCK_WAIT = 60.0


@contextlib.contextmanager
def digest_lock(path, wait=None):
    """Hold the digest-state lock for one read-modify-write.

    The scanner, the digest poster and guardian-wake (digest, record) all take
    it, so a reply recorded while a scan is running is not overwritten by the
    copy the scan loaded earlier (17 Nov 9 PM). Not re-entrant: never nest it.
    A timeout raises TimeoutError (an OSError) rather than waiting forever.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (DIGEST_LOCK_WAIT if wait is None else wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"digest state busy: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def board_path(config=None):
    if config and config.get("board_path"):
        return Path(config["board_path"])
    env = os.environ.get("BOARD_PATH")
    if env:
        return Path(env)
    if config and config.get("_state"):
        return Path(config["_state"]).resolve().parent / "board.json"
    return LIVE_BOARD


def digest_state_path(config=None):
    if config and config.get("digest_state_path"):
        return Path(config["digest_state_path"])
    env = os.environ.get("DIGEST_STATE")
    if env:
        return Path(env)
    if config and config.get("_state"):
        return Path(config["_state"]).resolve().parent / "digest-state.json"
    return LIVE_DIGEST


def report_path(config=None):
    if config and config.get("publication_report"):
        return Path(config["publication_report"])
    env = os.environ.get("PUBLICATION_REPORT")
    return Path(env) if env else LIVE_REPORT


def empty_digest_state():
    return {
        "version": 1,
        "posted": {},
        "events": {},
        "last_consumed_fingerprint": None,
        "publication_quiet_since": {},
        "final_reply_watermark": 0,
        "replies": {},
    }


def note_started(state, now):
    """Remember when a brand-new digest state is first written.

    A state that already has posts or replies keeps no new clock, so an old
    file does not suddenly treat today's slots as not-yet-due. The morning
    watermark starts at that same clock, so the first 9 AM queue lists only
    finals since this state was created. A later qualifying 9 AM reply still
    moves the watermark forward.
    """
    if not isinstance(state, dict):
        return state
    if isinstance(state.get("started_at"), (int, float)):
        return state
    if state.get("posted") or state.get("events") or state.get("replies") or state.get("last_consumed_fingerprint"):
        return state
    state["started_at"] = now
    state["final_reply_watermark"] = now
    return state


def is_plain_question(text):
    """A completed reply whose last line ends with ? and has no Needs-you row.

    classify_answer does not catch this: it only flags a red verdict plus a
    Needs-you row.
    """
    if not text or triage.needs_you(text):
        return False
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return bool(lines) and lines[-1].endswith("?")


def _link(thread_id):
    return triage.link_for(thread_id) or ""


def candidate_for(view, now):
    """One open candidate for a root, or None.

    view keys: root, runs (oldest first), reply (or None), running (bool),
    newest_user (str), quiet_seconds (number). reply may carry text, verdict,
    needs_you, message_id. A newest user message that starts with
    scheduled-task-message: skips the no-verdict kind only.
    """
    root = view.get("root") or ""
    runs = list(view.get("runs") or [])
    reply = view.get("reply") or None
    running = bool(view.get("running"))
    newest_user = view.get("newest_user") or ""
    quiet = float(view.get("quiet_seconds") or 0)
    latest = runs[-1] if runs else None
    failed = None
    for run in runs:
        status = run.get("status") or ""
        if status in FAILURE:
            failed = run
    later_than_failure = False
    if failed is not None:
        fail_end = failed.get("end") or 0
        for run in runs:
            if run is failed:
                continue
            if (run.get("requested") or 0) >= fail_end and run.get("status") not in FAILURE:
                later_than_failure = True
        if reply and (reply.get("at") or 0) > (fail_end or 0) and reply.get("verdict") == "green":
            later_than_failure = True
    if failed is not None and not later_than_failure:
        run = failed
        return _candidate(root, run.get("thread_id") or root, "failed_or_stopped", run.get("run_id"), run.get("run_id"))
    if running and quiet >= STALL_DUE and latest:
        return _candidate(root, latest.get("thread_id") or root, "stall", latest.get("run_id"), latest.get("run_id"))
    if reply and reply.get("verdict") == "red" and not running:
        return _candidate(root, root, "red_idle", reply.get("message_id"), reply.get("message_id"))
    if reply and not running and is_plain_question(reply.get("text") or ""):
        return _candidate(root, root, "text_ask", reply.get("message_id"), reply.get("message_id"))
    if latest and (latest.get("status") or "") not in FAILURE | BUSY and not running:
        no_verdict = not reply or not reply.get("verdict")
        asks = reply and is_plain_question(reply.get("text") or "")
        if no_verdict and not asks:
            if str(newest_user).startswith(SCHEDULED_PREFIX):
                return None
            return _candidate(root, root, "no_verdict", latest.get("run_id"), latest.get("run_id"))
    return None


def _candidate(root, thread_id, kind, key, ref):
    ident = str(key or ref or kind)
    return {
        "root": root,
        "thread_id": thread_id or root,
        "kind": kind,
        "key": ident,
        "ref": str(ref or ident),
        "link": _link(thread_id or root),
    }


def cards_from(cards, now, titles=None):
    titles = titles or {}
    out = []
    for card in cards or []:
        start = float(card.get("start") or 0)
        age = max(0, now - start)
        thread_id = card.get("thread_id") or ""
        out.append({
            "request_id": card.get("id"),
            "thread_id": thread_id,
            "link": _link(thread_id),
            "title": titles.get(thread_id) or "",
            "age_s": int(age),
        })
    return out


def held_from_state(state):
    found = []
    for incident in (state or {}).get("incidents", {}).values():
        if incident.get("code") != "outcome_trigger":
            continue
        if incident.get("status") in {"resolved", "acked"}:
            continue
        found.append(incident.get("id"))
    return sorted(x for x in found if x)


def fingerprint(board):
    """Semantic fingerprint. Ages, heartbeat and report timestamps are left out."""
    candidates = []
    for item in board.get("candidates") or []:
        candidates.append(f"{item.get('root')}|{item.get('kind')}|{item.get('key')}")
    cards = []
    for card in board.get("waiting_cards") or []:
        if int(card.get("age_s") or 0) >= CARD_GRACE:
            cards.append(str(card.get("request_id")))
    payload = {
        "candidates": sorted(candidates),
        "cards_past_10": sorted(cards),
        "held_new": sorted(board.get("held_new") or []),
        "publication_flagged": sorted(board.get("publication_flagged") or []),
        "health": board.get("health") or "healthy",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def local_dt(now):
    return datetime.fromtimestamp(now).astimezone()


def clock(now):
    return local_dt(now).strftime("%-I:%M %p")


def board_line(board, now):
    """Footer fragment: 'Board updated h:mm · watch healthy', or the red form."""
    scanned = board.get("last_completed_scan") if board else None
    when = clock(scanned or now)
    stale = not board or board.get("stale") or board.get("health") == "stale"
    if scanned:
        age = now - float(scanned)
        if age > 15 * 60:
            stale = True
            late = int(age // 60)
            when = f"{clock(scanned)}, {late} min behind"
    if stale:
        return f'<span itemprop="over">Board updated {when}</span>'
    return f"Board updated {when} · watch healthy"


def finding_body(key):
    if not isinstance(key, str) or ":" not in key:
        return None
    return key.split(":", 1)[1]


def map_project(key, projects):
    """Longest publication-config path that contains, or is contained by, the finding."""
    body = finding_body(key) or ""
    best = None
    for project in projects or []:
        path = project.get("path") or ""
        if not path:
            continue
        if body == path or body.startswith(path.rstrip("/") + "/") or path.startswith(body.rstrip("/") + "/"):
            if best is None or len(path) > len(best.get("path") or ""):
                best = project
    return best


def annotate_findings(findings, projects, now, digest_state, active_ids=None):
    """Flag a finding only after 60 quiet minutes. An active run is not quiet.

    An unmapped finding still counts and is not called new before those 60
    minutes. digest_state['publication_quiet_since'] is updated in place.
    """
    active_ids = set(active_ids or ())
    quiet = digest_state.setdefault("publication_quiet_since", {})
    seen = set()
    out = []
    for finding in findings or []:
        key = finding.get("key")
        if not key:
            continue
        seen.add(key)
        project = map_project(key, projects)
        mapped = project is not None
        busy = mapped and project.get("id") in active_ids
        if busy:
            quiet.pop(key, None)
            flagged = False
        else:
            since = quiet.get(key)
            if since is None:
                quiet[key] = now
                since = now
            flagged = (now - float(since)) >= QUIET_NEW
        out.append({
            "key": key,
            "text": finding.get("text") or "",
            "mapped": mapped,
            "flagged": bool(flagged),
        })
    for key in list(quiet):
        if key not in seen:
            quiet.pop(key, None)
    return out


def active_project_ids(database, projects):
    """Project ids with a run in queued, starting, running, preparing or waiting.

    Threads match the project's workspace_root, including worktreePath.
    Read-only. Returns an empty set if the database can't be read.
    """
    ids = [p.get("id") for p in projects or [] if p.get("id")]
    if not ids or not database or not Path(database).exists():
        return set()
    marks = ",".join("?" for _ in ids)
    sql = f"""
        SELECT DISTINCT t.project_id
        FROM orchestration_v2_projection_threads t
        JOIN orchestration_v2_projection_runs r ON r.thread_id = t.thread_id
        WHERE t.deleted_at IS NULL
          AND t.project_id IN ({marks})
          AND r.status IN ('queued','starting','running','preparing','waiting')
    """
    try:
        con = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        try:
            rows = {row[0] for row in con.execute(sql, ids)}
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return set()
    return rows


def git_line(annotated, total=None, reported=None):
    """New means flagged (60 quiet minutes) and not shown by the last consumed digest."""
    rows = list(annotated or [])
    count = total if total is not None else len(rows)
    reported_keys = set(reported or [])
    new = [row for row in rows if row.get("flagged") and row.get("key") not in reported_keys]
    if not new:
        text = f"Git and Netlify: {count} findings, none new"
    else:
        shown = "; ".join(row["key"] for row in new)
        text = f"Git and Netlify: {count} findings, {len(new)} new: {shown}"
    keys = [row["key"] for row in rows]
    return text, keys


def views_from_frame(frame, now, digest_state):
    """Candidates, cards, held ids and final-reply ids from a finished scan frame."""
    frame = frame or {}
    threads = frame.get("threads") or {}
    members = frame.get("members") or {}
    runs = frame.get("runs") or {}
    answers = frame.get("answers") or {}
    items = frame.get("items") or {}
    plain = frame.get("plain_asks") or {}
    heads = frame.get("user_heads") or {}
    views = []
    final_ids = []
    watermark = float((digest_state or {}).get("final_reply_watermark") or 0)
    for root, member_ids in members.items():
        info = threads.get(root) or {}
        if not info.get("top"):
            continue
        root_runs = []
        running = False
        quiet = 0
        for thread_id in member_ids:
            for run in runs.get(thread_id) or []:
                root_runs.append(run)
                ended = bool(run.get("end") and run["end"] <= now)
                status = run.get("status") if ended else "running"
                if status in BUSY or run.get("status") in BUSY:
                    running = True
                    stamps = [run.get("requested") or 0]
                    for item in items.get(run.get("run_id")) or []:
                        stamps.append(item.get("updated") or item.get("start") or 0)
                    quiet = max(quiet, now - max(stamps))
        root_runs.sort(key=lambda run: (run.get("requested") or 0, run.get("ordinal") or 0))
        settled = bool(info.get("settled") and info["settled"] <= now)
        archived = bool(info.get("archived"))
        final_id = _unread_final(root, runs, answers, heads, watermark)
        if final_id:
            final_ids.append(final_id)
        # Settled and archived roots stay off the candidate list. A busy run is still open work.
        # Settling is not reading: the newest unread final stays on the morning queue.
        if (settled or archived) and not running:
            continue
        reply = _latest_reply(root_runs, answers, plain)
        head = ""
        for thread_id in member_ids:
            stamp, text = heads.get(thread_id) or (0, "")
            if text and (not head or stamp >= 0):
                head = text if thread_id == root else head or text
        if root in heads:
            head = heads[root][1]
        views.append({
            "root": root,
            "runs": root_runs,
            "reply": reply,
            "running": running,
            "newest_user": head,
            "quiet_seconds": quiet if running else 0,
        })
    candidates = []
    for view in views:
        found = candidate_for(view, now)
        if found:
            candidates.append(found)
    return candidates, final_ids


def _unread_final(root, runs, answers, heads, watermark):
    """Newest final on this root after the watermark, if it is still unread.

    One entry per root. Andy has read it once a user message in the root is
    newer than that reply. A root whose newest user message is a scheduled-task
    wake is automated, so it is not an unread result. The watermark comparison
    stays strictly after.
    """
    stamp, text = heads.get(root) or (0, "")
    if str(text or "").startswith(SCHEDULED_PREFIX):
        return None
    try:
        stamp = float(stamp or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    newest = None
    newest_at = float(watermark or 0)
    for run in runs.get(root) or []:
        for answer in answers.get(run.get("run_id")) or []:
            at = answer.get("at") or 0
            if at > newest_at and answer.get("message_id"):
                newest = answer
                newest_at = at
    if newest is None or stamp > newest_at:
        return None
    return newest.get("message_id")


def _latest_reply(runs, answers, plain):
    best = None
    for run in runs:
        for answer in answers.get(run.get("run_id")) or []:
            if best is None or (answer.get("at") or 0) >= (best.get("at") or 0):
                best = dict(answer)
                best["run_id"] = run.get("run_id")
                asks = plain.get(run.get("run_id")) or []
                if asks and asks[-1].get("message_id") == answer.get("message_id"):
                    best["text"] = "?"
                elif "text" not in best:
                    best["text"] = ""
    return best


def build_board(frame, state, now, digest_state, projects, findings, active_ids):
    digest_state = digest_state if isinstance(digest_state, dict) else empty_digest_state()
    candidates, final_ids = views_from_frame(frame, now, digest_state)
    annotated = annotate_findings(findings, projects, now, digest_state, active_ids)
    held = held_from_state(state)
    consumed_held = set((digest_state or {}).get("consumed_held") or [])
    board = {
        "last_completed_scan": now,
        "stale": False,
        "health": "healthy",
        "waiting_cards": cards_from((frame or {}).get("cards") or [], now),
        "candidates": candidates,
        "held_outcome_ids": held,
        "held_new": [item for item in held if item not in consumed_held],
        "publication": annotated,
        "publication_flagged": [row["key"] for row in annotated if row.get("flagged")],
        "final_reply_ids": [item for item in final_ids if item],
    }
    board["fingerprint"] = fingerprint(board)
    return board


def mark_stale(path, now=None):
    """Keep the previous board and set stale. Do not move last_completed_scan."""
    current = load_json(path) or {}
    if not isinstance(current, dict):
        current = {}
    current["stale"] = True
    current["health"] = "stale"
    current.pop("fingerprint", None)
    current["fingerprint"] = fingerprint(current)
    atomic_json(path, current)
    return current


def write_board(path, board):
    board = dict(board)
    board.pop("fingerprint", None)
    board["fingerprint"] = fingerprint(board)
    atomic_json(path, board)
    return board


def due_slots(now, digest_state, force=False):
    local = local_dt(now)
    day = local.date().isoformat()
    posted = (digest_state.get("posted") or {}).get(day) or {}
    due = []
    for name, hour in SLOTS:
        if name in posted and not force:
            continue
        slot_at = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if local >= slot_at or force:
            if local >= slot_at:
                due.append(name)
    return day, due


def maybe_post_digest(config, board, now, sender=None, digest_state=None, force=False, state_path=None):
    """Post 3 PM and 9 PM when the fingerprint differs. 9 AM is not posted here.

    The slot is marked posted only after HTTP success. A failure leaves the
    slot unset and does not touch last_consumed_fingerprint. Success also
    leaves last_consumed_fingerprint unchanged until a qualifying reply.
    """
    path = Path(state_path) if state_path else digest_state_path(config)
    state = digest_state if isinstance(digest_state, dict) else (load_json(path) or empty_digest_state())
    for key, value in empty_digest_state().items():
        state.setdefault(key, value if not isinstance(value, dict) else dict(value))
    note_started(state, now)
    fp = board.get("fingerprint") or fingerprint(board)
    if not force and fp == state.get("last_consumed_fingerprint"):
        _merge_posts(path, now, {}, {}, None)
        return []
    day, slots = due_slots(now, state, force=False)
    # One catch-up post. Earlier missed slots that day are skipped, with no event.
    posts = {}
    events = {}
    shown = None
    if len(slots) > 1:
        posted_day = state.setdefault("posted", {}).setdefault(day, {})
        for slot in slots[:-1]:
            posted_day[slot] = {"skipped": True, "at": now}
            posts[(day, slot)] = posted_day[slot]
        slots = slots[-1:]
    sent = []
    for slot in slots:
        event_id = str(uuid.uuid4())
        body = {
            "event_id": event_id,
            "kind": "thread_health_digest",
            "slot": slot,
            "board_path": str(board_path(config)),
        }
        ok = _post(config, body, sender)
        if not ok:
            log_trial(Path(path).resolve().parent / "trial.jsonl", "digest_failed", slot=slot, fingerprint=fp, consumed=False)
            continue
        state.setdefault("posted", {}).setdefault(day, {})[slot] = {"event_id": event_id, "at": now}
        posts[(day, slot)] = state["posted"][day][slot]
        state.setdefault("events", {})[event_id] = {
            "fingerprint": fp,
            "slot": slot,
            "at": now,
            "snapshot_at": now,
            "day": day,
            "source": "events",
            "held": list(board.get("held_outcome_ids") or []),
            "publication_flagged": list(board.get("publication_flagged") or []),
        }
        events[event_id] = state["events"][event_id]
        state["last_slot"] = slot
        state["last_result"] = digest_result(board, slot, state.get("publication_reported"))
        shown = (slot, event_id, board)
        log_trial(Path(path).resolve().parent / "trial.jsonl", "digest_posted", slot=slot, fingerprint=fp, consumed=False)
        sent.append(body)
    _merge_posts(path, now, posts, events, shown)
    return sent


def _merge_posts(path, now, posts, events, shown):
    """Write only what the poster owns onto a fresh read of the digest state.

    The HTTP post runs outside the lock, so guardian-wake can record a reply
    meanwhile. Keys guardian-wake already wrote for the same event (its
    rendered result and flagged set) win over the posting-time copy.
    """
    with digest_lock(path):
        fresh = load_json(path)
        if not isinstance(fresh, dict):
            fresh = empty_digest_state()
        for key, value in empty_digest_state().items():
            fresh.setdefault(key, value if not isinstance(value, dict) else dict(value))
        note_started(fresh, now)
        for (day, slot), entry in posts.items():
            fresh.setdefault("posted", {}).setdefault(day, {})[slot] = dict(entry)
        for event_id, event in events.items():
            merged = dict(event)
            merged.update(fresh.setdefault("events", {}).get(event_id) or {})
            fresh["events"][event_id] = merged
        if shown:
            slot, event_id, board = shown
            event = fresh["events"].get(event_id) or {}
            if "result" not in event and event_id not in (fresh.get("replies") or {}):
                fresh["last_slot"] = slot
                fresh["last_result"] = digest_result(board, slot, fresh.get("publication_reported"))
        atomic_json(path, fresh)
    return fresh


def _post(config, body, sender):
    try:
        if sender:
            sender(body)
            return True
        url = (config or {}).get("webhook_url")
        if not url:
            return False
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "T3-thread-guardian/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as result:
            return 200 <= result.status < 300
    except (OSError, ValueError, urllib.error.URLError, TypeError):
        return False


def register_event(digest_state, event_id, fingerprint_value, slot, source):
    """Remember a digest event so a later reply can consume its own fingerprint.

    snapshot_at is the clock when the digest was shown. Morning consumption
    stops at that time, not at the later record time.
    """
    shown = time.time()
    digest_state.setdefault("events", {})[event_id] = {
        "fingerprint": fingerprint_value,
        "slot": slot,
        "at": shown,
        "snapshot_at": shown,
        "source": source,
        "day": local_dt(shown).date().isoformat(),
    }
    return digest_state


def consume(digest_state, event_id, source, log_path=None):
    """Consume only that event's fingerprint, and only from the events or 9 AM task.

    An Andy message (any other source) leaves last_consumed_fingerprint alone.
    A late reply consumes the fingerprint stored when that event was posted,
    not whatever the board says now. The 9 AM watermark is the snapshot the
    digest showed, so a reply that arrives while the model is still writing
    stays on the next morning queue.
    """
    if source not in {"events", "9am"}:
        return None
    event = (digest_state.get("events") or {}).get(event_id)
    if not event:
        return None
    digest_state["last_consumed_fingerprint"] = event.get("fingerprint")
    digest_state["last_consumed_event"] = event_id
    if "publication_flagged" in event:
        digest_state["publication_reported"] = sorted(event.get("publication_flagged") or [])
    replied = time.time()
    slot = event.get("slot")
    digest_state.setdefault("replies", {})[event_id] = {
        "at": replied,
        "source": source,
        "slot": slot,
        "day": local_dt(replied).date().isoformat(),
    }
    if slot:
        digest_state["last_slot"] = slot
    digest_state.setdefault("last_result", "no new list item")
    if slot == "9am":
        cutoff = event.get("snapshot_at")
        if not isinstance(cutoff, (int, float)):
            cutoff = event.get("at")
        if isinstance(cutoff, (int, float)):
            digest_state["final_reply_watermark"] = cutoff
        digest_state["consumed_held"] = list(event.get("held") or digest_state.get("consumed_held") or [])
    elif event.get("held"):
        digest_state["consumed_held"] = list(event.get("held"))
    if log_path:
        log_trial(log_path, "digest_replied", slot=slot, fingerprint=event.get("fingerprint"), consumed=True)
    return event.get("fingerprint")


def _on_today(reply, event, now):
    """Slot name alone is not an answer. Yesterday's reply does not cover today."""
    today = local_dt(now).date()
    for value in ((reply or {}).get("at"), (event or {}).get("at")):
        if isinstance(value, (int, float)):
            return local_dt(float(value)).date() == today
    day = (reply or {}).get("day") or (event or {}).get("day")
    return bool(day) and str(day) == today.isoformat()


def _slot_answered(digest_state, name, now):
    replies = (digest_state or {}).get("replies") or {}
    events = (digest_state or {}).get("events") or {}
    for event_id, reply in replies.items():
        event = events.get(event_id) or {}
        if reply.get("source") not in {"events", "9am"} and event.get("source") not in {"events", "9am"}:
            continue
        if not _on_today(reply, event, now):
            continue
        if event.get("slot") == name or reply.get("slot") == name:
            return True
    return False


def _slot_after_start(digest_state, slot_at):
    """A slot is late only after the digest state exists, and only if it falls later."""
    started = (digest_state or {}).get("started_at")
    if isinstance(started, (int, float)):
        return slot_at.timestamp() > float(started)
    state = digest_state or {}
    if state.get("posted") or state.get("events") or state.get("replies") or state.get("last_consumed_fingerprint"):
        return True
    return False


def digest_overdue(now, board, digest_state):
    """True when a due digest has no qualifying reply 15 minutes after the slot.

    9 AM is always due. Afternoon slots are due only when the board changed.
    A digest that was never posted still counts once it is due.
    """
    local = local_dt(now)
    fp = (board or {}).get("fingerprint")
    consumed = (digest_state or {}).get("last_consumed_fingerprint")
    changed = bool(fp) and fp != consumed
    checks = [("9am", 9)]
    if changed:
        checks.extend(SLOTS)
    for name, hour in checks:
        slot_at = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if local < slot_at:
            continue
        if not _slot_after_start(digest_state, slot_at):
            continue
        if (local - slot_at).total_seconds() < 15 * 60:
            continue
        if not _slot_answered(digest_state, name, now):
            return True
    return False


def trial_input_total(calls):
    """Sum of input tokens over every model call in one run. Not a lifetime sum."""
    return sum(int(call) for call in calls)


TRIAL_FIELDS = (
    "ts", "local_date", "slot", "event", "fingerprint", "consumed", "candidate_kind",
    "thread_id", "first_seen", "judged", "visible_at", "latency_s", "session_id",
    "input_tokens_first", "input_tokens_total", "output_tokens_total", "ownership",
)


def log_trial(path, event, **fields):
    """Append one planned trial row. No path means in-memory callers write nothing.

    input_calls, when passed, is summed with trial_input_total for this run only.
    Step 1's first-call comparison stays with the coordinator.
    """
    if not path:
        return None
    calls = fields.pop("input_calls", None)
    if calls is not None and fields.get("input_tokens_total") is None:
        fields["input_tokens_total"] = trial_input_total(calls)
    if fields.get("latency_s") is None and fields.get("visible_at") is not None and fields.get("first_seen") is not None:
        fields["latency_s"] = float(fields["visible_at"]) - float(fields["first_seen"])
    now = time.time()
    row = {key: None for key in TRIAL_FIELDS}
    row["ts"] = now
    row["local_date"] = local_dt(now).date().isoformat()
    row["event"] = event
    for key, value in fields.items():
        if key in row:
            row[key] = value
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError):
        return None
    return row


def digest_result(snapshot, slot, reported=None):
    """Short last-digest line for the feed. No message text and no links."""
    snapshot = snapshot or {}
    reported_keys = set(reported or [])
    finals = [item for item in (snapshot.get("final_reply_ids") or []) if item]
    candidates = list(snapshot.get("candidates") or [])
    flagged = [key for key in (snapshot.get("publication_flagged") or []) if key not in reported_keys]
    held = list(snapshot.get("held_new") or [])
    if slot == "9am" and finals:
        noun = "final reply" if len(finals) == 1 else "final replies"
        return f"{len(finals)} {noun}"
    parts = []
    if candidates:
        parts.append(", ".join(str(item.get("kind") or "candidate") for item in candidates))
    if held:
        parts.append(f"{len(held)} held")
    if flagged:
        parts.append(f"{len(flagged)} new findings")
    if not parts:
        return "no new list item"
    return "; ".join(parts)


def sync_after_scan(config, state, frame, now, mode):
    """Write the board after a finished scan. An early return only marks stale."""
    path = board_path(config)
    if frame is None:
        if state.get("read_cursor") or state.get("hydration_cursor"):
            mark_stale(path, now)
        return None
    dpath = digest_state_path(config)
    projects = []
    findings = []
    report = load_json(report_path(config)) or {}
    if isinstance(report, dict):
        findings = list(report.get("findings") or [])
    pub = load_json(os.environ.get("PUBLICATION_CONFIG") or str(LIVE_PUB_CONFIG)) or {}
    if isinstance(pub, dict):
        projects = list(pub.get("projects") or [])
    database = (config or {}).get("database") or os.environ.get("T3_DB") or str(LIVE_DB)
    try:
        active = active_project_ids(database, projects)
    except (OSError, sqlite3.Error):
        active = set()
    _fill_user_heads(config, frame)
    # Load, build and save under the lock, so a reply guardian-wake records
    # during this scan is read here rather than overwritten (17 Nov 9 PM).
    with digest_lock(dpath):
        digest_state = load_json(dpath) or empty_digest_state()
        note_started(digest_state, now)
        board = build_board(frame, state, now, digest_state, projects, findings, active)
        write_board(path, board)
        atomic_json(dpath, digest_state)
    trial_path = Path(dpath).resolve().parent / "trial.jsonl"
    log_trial(trial_path, "scan", fingerprint=board.get("fingerprint"), consumed=False)
    for item in board.get("candidates") or []:
        log_trial(
            trial_path, "scan", fingerprint=board.get("fingerprint"),
            candidate_kind=item.get("kind"), thread_id=item.get("thread_id"), consumed=False,
        )
    if mode == "deliver":
        maybe_post_digest(config, board, now, state_path=dpath)
    return board


def _fill_user_heads(config, frame):
    """Newest user-message prefix per thread, so a scheduled-task wake can skip no_verdict.

    Runs outside the scan budget. Stores 48 characters, never the whole message.
    """
    if frame.get("user_heads"):
        return
    database = (config or {}).get("database")
    if not database or not Path(database).exists():
        frame["user_heads"] = {}
        return
    heads = {}
    try:
        con = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                """SELECT thread_id, created_at, substr(json_extract(payload_json,'$.text'), 1, 48)
                   FROM orchestration_v2_projection_messages WHERE role='user'"""
            )
            for thread_id, created, head in rows:
                stamp = triage.stamp(created)
                prev = heads.get(thread_id)
                if prev is None or stamp >= prev[0]:
                    heads[thread_id] = (stamp, head or "")
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        heads = {}
    frame["user_heads"] = heads


def digest_text(board, digest_state, slot, event_id, now=None):
    """What guardian-wake digest prints for the model to judge. No raw replies."""
    now = time.time() if now is None else now
    board = board or {}
    fp = board.get("fingerprint") or fingerprint(board)
    lines = [f"slot: {slot}", f"fingerprint: {fp}", f"event_id: {event_id}"]
    finals = [item for item in (board.get("final_reply_ids") or []) if item]
    # 9 AM still prints new final replies when the afternoon fingerprint is unchanged.
    morning_new = slot == "9am" and bool(finals)
    if fp and fp == (digest_state or {}).get("last_consumed_fingerprint") and not morning_new:
        lines.append("DUPLICATE")
        lines.append('Write only: Nothing new needs you.')
        lines.append("Do not record.")
        return "\n".join(lines)
    overdue = [c for c in board.get("waiting_cards") or [] if int(c.get("age_s") or 0) >= CARD_GRACE]
    if not overdue:
        lines.append("No question cards waiting over 10 minutes.")
    else:
        bits = []
        for card in overdue:
            link = card.get("link") or ""
            title = card.get("title") or card.get("thread_id") or "a thread"
            bits.append(f"[{title}]({link})" if link else title)
        lines.append(f"Waiting on you over 10 minutes: {len(overdue)} question cards, in " + " and ".join(bits) + ".")
    lines.append("Candidates:")
    found = board.get("candidates") or []
    if not found:
        lines.append("- none")
    for item in found:
        lines.append(f"- {item.get('kind')} {item.get('link') or item.get('root')}")
    held = board.get("held_new") or []
    lines.append("Held outcomes: " + (", ".join(held) if held else "none"))
    text, keys = git_line(board.get("publication") or [], reported=(digest_state or {}).get("publication_reported"))
    lines.append(text)
    for key in keys:
        lines.append(f"key: {key}")
    lines.append(board_line(board, now))
    if slot == "9am":
        lines.append("Final replies since the last consumed 9 AM digest:")
        ids = board.get("final_reply_ids") or []
        lines.append("- none" if not ids else "\n".join(f"- {item}" for item in ids))
    return "\n".join(lines)


def per_event_enabled(config):
    """On when the key is absent, so current tests keep posting."""
    return (config or {}).get("per_event_delivery", True) is not False
