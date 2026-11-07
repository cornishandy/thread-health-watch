#!/usr/bin/env python3
"""Pure thread-health rules. No database, network, or message text storage.

State, logs and payloads keep ids, statuses, timestamps and codes only.
Message text is accepted only by the classifiers below and is not returned.
"""
from __future__ import annotations

import hashlib
import json
import re
import datetime as dt

BUSY = {"preparing", "queued", "starting", "running", "waiting"}
FAILURE = {"failed", "cancelled", "interrupted"}
IGNORED = {"reasoning", "thinking", "checkpoint", "user_message"}
LIVE = {"pending", "running", "waiting"}
TOOLISH_SKIP = IGNORED | {"assistant_message"}

GRACE = {
    "unanswered_or_failed": 300,
    "stopped_member": 300,
    "red_unresolved": 600,
    "no_verdict": 600,
    "failed_tool": 600,
}
STALL_OPEN = 1800
STALL_DUE = 2700
STALL_REDELIVER = 10800
REDELIVER = 3600
WAKE_GAP = 600
LOW_BATCH = 3600
MAX_INCIDENTS = 10
MAX_BYTES = 4096
DAY_WAKE_NOTICE = 30
CARD_DUE = 120
HIGH = {"unanswered_or_failed", "stalled", "stopped_member", "failed_tool", "outcome_trigger", "waiting_card"}
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9:._%-]{1,512}$")
INCIDENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:@/=+\-]{1,120}$")
LINK_RE = re.compile(r"^t3-thread://v1/[A-Za-z0-9:._%-]{1,512}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
CODES = {
    "unanswered_or_failed", "stopped_member", "red_unresolved", "no_verdict",
    "failed_tool", "stalled", "outcome_trigger", "human_blocked",
    "waiting_card", "threads_changed",
}
LOW_CODES = {"no_verdict", "red_unresolved", "threads_changed"}

# Last marker wins. "Not wrapped up" is its own alternative so it cannot
# match the green "Wrapped up" pattern. Red patterns on that marker win.
VERDICT_RE = re.compile(
    r"itemprop\s*=\s*[\"']settle-(yes|no)[\"']"
    r"|safe to settle"
    r"|don[\u2019']t settle yet"
    r"|not wrapped up"
    r"|wrapped up",
    re.IGNORECASE,
)
ASK_RE = re.compile(r"^\s*\|\s*(?:Needs you|Waiting on you)\b(.*)$", re.IGNORECASE)
CARD_RE = re.compile(r"i[\u2019']m waiting on you now", re.IGNORECASE)
RUNNING_RE = re.compile(r"(?:^|\n)\s*\|\s*Running\b")
PUBLIC_KEYS = (
    "id", "code", "root", "member", "link", "run_ordinal", "run_status",
    "item_type", "item_status", "item_at", "age_seconds", "outcome_id", "priority",
)
PAYLOAD_KEYS = ("event_id", "kind", "incidents", "more_pending")

# Implemented fallbacks the daemon and replay both rely on. See the build report.
DEVIATIONS = (
    "Turn-item metadata has no deadline, deadlineAt, or dueAt field, so a running item never defers a stall.",
    "Payload fields are an allowlist. Ids must match the database id format and links must be t3-thread://v1/<id>. Long ids stay whole. The 4096-byte cap pages by dropping incidents into more_pending.",
    "A stopped member is not also opened as unanswered_or_failed. The root's own terminal run uses run-ending rules.",
    "Snooze is not a suppressor. Draft 2 does not mention it.",
    "wake_when other than all_terminal does not fire.",
    "The 72h replay baselines at the window start and does not re-evaluate every older terminal root.",
    "Replay wakes/day assumes each delivery is acked. The daemon still redelivers when acks.jsonl does not cover the id.",
    "Priority is computed when a payload is built. red_unresolved is low from the start and waits for the hourly low-priority slot.",
    "Historical running status is inferred from startedAt and completedAt. The database stores only the final status.",
    "Each database read is its own transaction with a 1.5 second wall-clock budget. Assistant text is classified after that transaction closes.",
)


def stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


def iso(epoch):
    if not epoch:
        return None
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds")


def local_day(epoch):
    return dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def link_for(thread_id):
    if not isinstance(thread_id, str) or not THREAD_ID_RE.fullmatch(thread_id):
        return None
    link = "t3-thread://v1/" + thread_id
    if not LINK_RE.fullmatch(link):
        return None
    return link


def public_value(key, value):
    """Allowlisted payload value, or None when the value is not metadata."""
    if value is None:
        return None
    if key in {"root", "member"}:
        if isinstance(value, str) and THREAD_ID_RE.fullmatch(value):
            return value
        return None
    if key == "id":
        if isinstance(value, str) and (INCIDENT_ID_RE.fullmatch(value) or TOKEN_RE.fullmatch(value)):
            return value
        return None
    if key == "link":
        if isinstance(value, str) and LINK_RE.fullmatch(value):
            return value
        return None
    if key == "outcome_id":
        if isinstance(value, str) and (THREAD_ID_RE.fullmatch(value) or TOKEN_RE.fullmatch(value)):
            return value
        return None
    if key == "code":
        if value in CODES:
            return value
        return None
    if key == "priority":
        if value in {"high", "normal", "low"}:
            return value
        return None
    if key in {"run_status", "item_type", "item_status"}:
        if isinstance(value, str) and TOKEN_RE.fullmatch(value):
            return value
        return None
    if key == "item_at":
        if isinstance(value, str) and ISO_RE.fullmatch(value):
            return value
        return None
    if key in {"run_ordinal", "age_seconds"}:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None
    return None


def incident_id(code, root, member, run_id, extra=""):
    raw = "|".join([code or "", root or "", member or "", run_id or "", extra or ""])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def threads_signature(pairs):
    """Hash of sorted (top-level thread id, latest assistant message id). No list is kept."""
    ordered = sorted((str(thread_id or ""), str(message_id or "")) for thread_id, message_id in pairs or [])
    raw = json.dumps(ordered, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_verdict(text):
    """Return 'red', 'green', or None from the last verdict marker.

    'Not wrapped up' is red. It never counts as the green 'Wrapped up'.
    """
    if not text:
        return None
    found = list(VERDICT_RE.finditer(text))
    if not found:
        return None
    span = found[-1].group(0).lower().replace("\u2019", "'")
    if "settle-no" in span or "don't settle" in span or "not wrapped up" in span:
        return "red"
    if "settle-yes" in span or span.strip() == "safe to settle" or span.strip() == "wrapped up":
        return "green"
    return None


def needs_you(text):
    """True for a non-empty Needs you / Waiting on you row, or the waiting card line.

    A prose mention of "needs you" is not a row and does not count.
    """
    if not text:
        return False
    for line in text.splitlines():
        if CARD_RE.search(line):
            return True
        match = ASK_RE.match(line)
        if not match:
            continue
        rest = re.sub(r"[|\s`*:_#>-]+", "", match.group(1))
        if rest:
            return True
    return False


def running_row(text):
    return bool(text and RUNNING_RE.search(text))


def classify_answer(text, message_id, at):
    """Classify in memory. The returned dict has no message text."""
    return {
        "message_id": message_id,
        "at": at,
        "verdict": parse_verdict(text),
        "needs_you": needs_you(text),
        "running": running_row(text),
    }


def new_state():
    return {
        "version": 2,
        "baseline": False,
        "threads": {},
        "incidents": {},
        "handled_runs": {},
        "handled_members": {},
        "notified_requests": {},
        "notified_messages": {},
        "notified_orphans": {},
        "triggered_outcomes": {},
        "review_changed": 0,
        "review_by_day": {},
        "last_review": 0,
        "last_delivery": 0,
        "last_low": 0,
        "threads_signature": "",
        "acked_threads_signature": "",
        "retry_at": 0,
        "delivery_failures": 0,
        "outbox": None,
        "wakes_by_day": {},
        "day_notified": {},
        "days": {},
        "notices": 0,
    }


def _day_bucket(state, day):
    bucket = state["days"].setdefault(day, {
        "opened": {}, "resolved": {}, "delivered": {}, "class": {},
        "deliveries": 0, "payload_bytes": 0, "notices": 0,
    })
    return bucket


def _count(mapping, key):
    mapping[key] = mapping.get(key, 0) + 1


def priority_at(incident, now):
    code = incident["code"]
    status = incident.get("run_status") or ""
    if code in {"unanswered_or_failed", "stopped_member"} and status in {"cancelled", "interrupted"}:
        return "low"
    if code in LOW_CODES:
        return "low"
    if code in HIGH:
        return "high"
    return "normal"


def _rank(incident, now):
    return {"high": 0, "normal": 1, "low": 2}[priority_at(incident, now)]


def evaluate_run_ending(run, answer, later_human, busy_member, live_request):
    """Ordered run-ending decision. answer is classify_answer output or None."""
    status = (run or {}).get("status") or ""
    if status in {"cancelled", "interrupted"} and later_human:
        return {"action": "resolve", "reason": "later_human"}
    if status in FAILURE or not answer:
        priority = "low" if status in {"cancelled", "interrupted"} else "high"
        return {"action": "open", "code": "unanswered_or_failed", "priority": priority,
                "grace": GRACE["unanswered_or_failed"], "class": "WAKE"}
    verdict = answer.get("verdict")
    if verdict == "green":
        return {"action": "resolve", "reason": "green"}
    if verdict == "red" and answer.get("needs_you"):
        return {"action": "notify", "code": "human_blocked", "message_id": answer.get("message_id"),
                "class": "NOTIFY"}
    if verdict == "red" and answer.get("running") and (busy_member or live_request):
        return {"action": "watch", "code": "red_unresolved", "priority": "low", "grace": None, "class": "WAKE"}
    if verdict == "red":
        return {"action": "open", "code": "red_unresolved", "priority": "low",
                "grace": GRACE["red_unresolved"], "class": "WAKE"}
    return {"action": "open", "code": "no_verdict", "priority": "low",
            "grace": GRACE["no_verdict"], "class": "WAKE"}


def _ended_resolved(run, answer, later_human):
    if not run or not run.get("end"):
        return False
    decision = evaluate_run_ending(run, answer, later_human, False, False)
    if decision["action"] == "resolve":
        return True
    return decision.get("code") == "human_blocked"


def stopped_member_recovered(failure, member_runs, root_runs, human_times, answers_for, now):
    """failure is a run dict with end <= now and status in FAILURE.

    Busy siblings, a busy root, and a Needs you row do not recover it.
    """
    if not failure or not failure.get("end") or failure["end"] > now:
        return False
    if failure["status"] == "cancelled" and any(h > failure["end"] for h in human_times):
        return True
    for run in member_runs:
        if run["requested"] <= failure["end"] or not run.get("end") or run["end"] > now:
            continue
        if run["status"] == "completed" and answers_for(run):
            return True
    for run in root_runs:
        if run["requested"] <= failure["end"] or not run.get("end") or run["end"] > now:
            continue
        answer = answers_for(run)
        if answer and answer.get("verdict") == "green":
            return True
        if answer and answer.get("verdict") == "red" and answer.get("needs_you") and answer.get("at", 0) > failure["end"]:
            return True
    return False


def stall_deadline(running_items, now):
    """Latest explicit future deadline, or 0 when no item carries one."""
    latest = 0.0
    for item in running_items:
        deadline = item.get("deadline") or 0
        if deadline > now:
            latest = max(latest, deadline)
    return latest


def assess_stall(last_completed, busy_since, now, running_items):
    """Open at 30 quiet minutes, due at 45, whether or not something is running."""
    if busy_since is None:
        return None
    anchor = max(last_completed or 0, busy_since)
    quiet = now - anchor
    if quiet < STALL_OPEN:
        return None
    due = anchor + STALL_DUE
    deferred = stall_deadline(running_items, now)
    if deferred:
        due = max(due, deferred)
    status = "due" if now >= due else "watching"
    return {"anchor": anchor, "due_at": due, "status": status, "quiet": quiet}


def assess_failed_tool(failed_at, later_completed, now, busy):
    """Due 10 minutes after a failed tool with no later completed item.

    A later completed item in the same run clears it before it is due.
    """
    if not failed_at or not busy:
        return {"status": "clear"}
    if later_completed and later_completed > failed_at:
        return {"status": "clear"}
    due = failed_at + GRACE["failed_tool"]
    if now >= due:
        return {"status": "due", "due_at": due}
    return {"status": "watching", "due_at": due}


def outcome_ready(outcome, dependency_states):
    """all_terminal fires only when every dependency is positively terminal.

    A missing, excluded or unreadable dependency is 'unknown' and blocks the trigger.
    """
    if not outcome or outcome.get("wake_when") != "all_terminal":
        return False
    deps = outcome.get("dependency_thread_ids") or []
    if not deps or outcome.get("unresolved"):
        # Named dependencies without a known thread id are unknown: never fire.
        return False
    for thread_id in deps:
        if dependency_states.get(thread_id) != "terminal":
            return False
    return True


def _log(event, incident):
    return {
        "event": event,
        "code": incident["code"],
        "id": incident["id"],
        "root": incident["root"],
        "at": iso(incident.get("resolved_at") or incident.get("due_at") or incident["opened_at"]),
    }


def _touch_day(state, incident, field, when):
    bucket = _day_bucket(state, local_day(when))
    _count(bucket[field], incident["code"])
    if field == "opened":
        _count(bucket["class"], incident.get("class") or "WAKE")


def open_incident(state, *, code, root, run, now, grace, status, member=None, item=None,
                  outcome_id=None, opened_at=None, due_at=None, extra="", anchor=None):
    opened = opened_at if opened_at is not None else now
    if due_at is None and grace is not None:
        due_at = opened + grace
    if status == "due" or (due_at is not None and now >= due_at and status != "watching"):
        status = "due"
    elif status != "watching":
        status = "watching" if due_at and now < due_at else status
    iid = incident_id(code, root, member, (run or {}).get("run_id"), extra or (item or {}).get("id") or "")
    existing = state["incidents"].get(iid)
    if existing and existing["status"] != "resolved":
        return existing, []
    if existing and existing["status"] == "resolved":
        return existing, []
    incident = {
        "id": iid,
        "code": code,
        "root": root,
        "member": member,
        "run_id": (run or {}).get("run_id"),
        "run_ordinal": (run or {}).get("ordinal"),
        "run_status": (run or {}).get("status"),
        "opened_at": opened,
        "due_at": due_at,
        "status": "due" if due_at is not None and now >= due_at else "watching",
        "class": "WAKE",
        "item_type": (item or {}).get("type"),
        "item_status": (item or {}).get("status"),
        "item_at": iso((item or {}).get("end") or (item or {}).get("at")),
        "outcome_id": outcome_id,
        "anchor": anchor,
        "delivered_at": None,
        "first_delivered_at": None,
        "acked_at": None,
        "resolved_at": None,
        "due_logged": False,
        "next_check": due_at,
    }
    if incident["status"] == "watching" and due_at is None:
        incident["status"] = "watching"
    state["incidents"][iid] = incident
    _touch_day(state, incident, "opened", opened)
    logs = [_log("opened", incident)]
    if incident["status"] == "due" and not incident["due_logged"]:
        incident["due_logged"] = True
        logs.append(_log("due", incident))
    return incident, logs


def resolve_incident(state, incident, now, reason):
    if not incident or incident["status"] == "resolved":
        return []
    if incident["status"] in {"watching", "due"} and not incident.get("first_delivered_at"):
        incident["class"] = "DROP"
        bucket = _day_bucket(state, local_day(incident["opened_at"]))
        _count(bucket["class"], "DROP")
    incident["status"] = "resolved"
    incident["resolved_at"] = now
    incident["resolve_reason"] = reason
    _touch_day(state, incident, "resolved", now)
    return [_log("resolved", incident)]


def promote(state, now):
    logs = []
    for incident in state["incidents"].values():
        if incident.get("paused") or incident["status"] != "watching" or not incident.get("due_at"):
            continue
        if now >= incident["due_at"]:
            incident["status"] = "due"
            if not incident.get("due_logged"):
                incident["due_logged"] = True
                logs.append(_log("due", incident))
    return logs


def redelivery_ready(incident, now):
    if incident["status"] != "delivered" or incident.get("acked_at") or not incident.get("delivered_at"):
        return False
    wait = STALL_REDELIVER if incident["code"] == "stalled" else REDELIVER
    return now - incident["delivered_at"] >= wait


def ready_incidents(state, now):
    promote(state, now)
    ready = []
    for incident in state["incidents"].values():
        if incident.get("paused"):
            continue
        if incident["status"] == "due" or redelivery_ready(incident, now):
            ready.append(incident)
    return ready


def project_incident(incident, now):
    record = {}
    values = {
        "id": incident["id"],
        "code": incident["code"],
        "root": incident.get("root"),
        "member": incident.get("member"),
        "link": link_for(incident.get("member") or incident.get("root")),
        "run_ordinal": incident.get("run_ordinal"),
        "run_status": incident.get("run_status"),
        "item_type": incident.get("item_type"),
        "item_status": incident.get("item_status"),
        "item_at": incident.get("item_at"),
        "age_seconds": max(0, int(now - incident["opened_at"])),
        "outcome_id": incident.get("outcome_id"),
        "priority": priority_at(incident, now),
    }
    for key in PUBLIC_KEYS:
        value = public_value(key, values.get(key))
        if value is None:
            continue
        record[key] = value
    return record


def render_payload(event_id, incidents, more_pending, now):
    body = {
        "event_id": event_id,
        "kind": "thread_health_v2",
        "incidents": [project_incident(incident, now) for incident in incidents],
        "more_pending": more_pending,
    }
    return body


def dump_payload(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _delivery_key(incident, now):
    """Never-delivered incidents first, then retries oldest delivery first."""
    if not incident.get("first_delivered_at"):
        return (0, _rank(incident, now), incident.get("opened_at") or 0, incident["id"])
    return (1, 0, incident.get("delivered_at") or 0, incident["id"])


def pack_incidents(ready, now, event_id, low_allowed):
    """Fit at most 10 incidents and 4096 serialized bytes.

    Never-delivered incidents go out before retries. Retries rotate by the
    oldest last delivery. Paused incidents are excluded. Incidents that do
    not fit stay in the returned waiting list. Nothing is dropped.
    """
    ready = [incident for incident in ready if not incident.get("paused")]
    ordered = sorted(ready, key=lambda incident: _delivery_key(incident, now))
    eligible = [incident for incident in ordered if low_allowed or _rank(incident, now) < 2]
    held = [incident for incident in ordered if incident not in eligible]
    chosen = []
    for incident in eligible:
        trial = chosen + [incident]
        more = len(ready) - len(trial)
        payload = render_payload(event_id, trial, more, now)
        if len(trial) > MAX_INCIDENTS or len(dump_payload(payload)) > MAX_BYTES:
            break
        chosen.append(incident)
    while chosen:
        more = len(ready) - len(chosen)
        payload = render_payload(event_id, chosen, more, now)
        if len(dump_payload(payload)) <= MAX_BYTES and len(chosen) <= MAX_INCIDENTS:
            break
        chosen.pop()
    more = len(ready) - len(chosen)
    payload = render_payload(event_id, chosen, more, now)
    chosen_ids = {incident["id"] for incident in chosen}
    waiting = [incident for incident in ready if incident["id"] not in chosen_ids]
    # held lows are already inside ready, so they are in waiting.
    assert len(waiting) + len(chosen) == len(ready)
    assert not held or all(incident in waiting or incident in chosen for incident in held)
    return payload, chosen, waiting


def _remember_threads_ack(state, incident):
    if not incident or incident.get("code") != "threads_changed":
        return
    signature = incident.get("signature") or state.get("threads_signature") or ""
    state["acked_threads_signature"] = signature


def apply_acks(state, ack_ids, now):
    changed = []
    for ack_id in ack_ids:
        incident = state["incidents"].get(ack_id)
        if incident and incident["status"] == "delivered" and not incident.get("acked_at"):
            incident["status"] = "acked"
            incident["acked_at"] = now
            _remember_threads_ack(state, incident)
            changed.append(ack_id)
    return changed


def load_ack_ids(lines):
    """Accept a JSON object with incident_ids, or one id per line. No other fields are kept."""
    found = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict):
                found.extend(str(item) for item in obj.get("incident_ids") or [])
                if obj.get("incident_id"):
                    found.append(str(obj["incident_id"]))
            continue
        found.append(text.split()[0])
    return found


def _answer_at(answers, now):
    chosen = None
    for answer in answers or []:
        if answer.get("at", 0) <= now:
            chosen = answer
    return chosen


def _run_at(runs, now):
    chosen = None
    for run in runs or []:
        if run["requested"] <= now:
            chosen = run
        else:
            break
    return chosen


def _view_run(run, now):
    if not run:
        return None
    ended = bool(run.get("end") and run["end"] <= now)
    status = run["status"] if ended else "running"
    viewed = dict(run)
    viewed["status"] = status
    viewed["ended"] = ended
    return viewed


def _progress(items, now):
    best = 0.0
    for item in items:
        if item["type"] in IGNORED:
            continue
        end = item.get("end") or 0
        if item.get("status") == "completed" and end and end <= now:
            best = max(best, end)
    return best


def _running(items, now):
    found = []
    for item in items:
        start = item.get("start") or 0
        end = item.get("end") or 0
        if start and start > now:
            continue
        if end > now or (not end and item.get("status") in BUSY):
            found.append(item)
    return found


def _latest_failed_tool(items, now):
    best = None
    for item in items:
        if item["type"] in TOOLISH_SKIP:
            continue
        failed = item.get("status") in {"failed", "error"} or item["type"] == "error"
        end = item.get("end") or 0
        if not failed or not end or end > now:
            continue
        if best is None or end > best["end"] or (end == best["end"] and item["id"] > best["id"]):
            best = item
    if not best:
        return None, 0.0
    later = 0.0
    for item in items:
        if item["type"] in IGNORED:
            continue
        end = item.get("end") or 0
        if item.get("status") == "completed" and end and best["end"] < end <= now:
            later = max(later, end)
    return best, later


def assemble_snap(history, now):
    """Build one root snapshot at `now` from preloaded metadata. No message text."""
    root = history["root"]
    runs_by = history["runs"]
    root_runs = runs_by.get(root, [])
    current = _view_run(_run_at(root_runs, now), now)
    members = []
    busy_ids = []
    for thread_id in history["members"]:
        viewed = _view_run(_run_at(runs_by.get(thread_id, []), now), now)
        if not viewed:
            continue
        entry = {
            "thread_id": thread_id,
            "run_id": viewed["run_id"],
            "ordinal": viewed["ordinal"],
            "status": viewed["status"],
            "requested": viewed["requested"],
            "end": viewed["end"] if viewed["ended"] else 0,
            "ended": viewed["ended"],
        }
        members.append(entry)
        if viewed["status"] in BUSY:
            busy_ids.append(viewed["run_id"])
    busy = bool(busy_ids) or bool(current and current["status"] in BUSY)
    if current and current["status"] in BUSY and current["run_id"] not in busy_ids:
        busy_ids.append(current["run_id"])
    item_pool = []
    for run_id in busy_ids:
        item_pool.extend(history["items"].get(run_id, []))
    if current:
        item_pool.extend(history["items"].get(current["run_id"], []))
    last_completed = _progress(item_pool, now)
    running = _running(item_pool, now)
    failed, later = _latest_failed_tool(item_pool, now)
    answer = None
    if current:
        answer = _answer_at(history["answers"].get(current["run_id"], []), now if not current["ended"] else current["end"] + 1e-6)
        if current["ended"] and answer and answer["at"] > current["end"] + 1:
            answer = None
    seen_humans = [h for h in history["humans"].get(root, []) if h <= now]
    later_human = bool(current and current["ended"] and any(h > current["end"] for h in seen_humans))
    live_ids = []
    closed_request = {"resolved", "cancelled", "expired"}
    for thread_id in history["members"]:
        for request in history["requests"].get(thread_id, []):
            start = request["start"]
            end = request["end"]
            if start <= now and request["status"] not in closed_request and (not end or end > now):
                live_ids.append(request["id"])
    settled = bool(history.get("settled_at") and history["settled_at"] <= now)
    archived = bool(history.get("archived_at") and history["archived_at"] <= now)
    busy_since = 0.0
    if busy:
        starts = [entry["requested"] for entry in members if entry["status"] in BUSY]
        if current and current["status"] in BUSY:
            starts.append(current["requested"])
        busy_since = min(starts) if starts else now
    orphaned = []
    if settled:
        orphaned = [entry["thread_id"] + ":" + entry["run_id"] for entry in members if entry["status"] in BUSY and entry["thread_id"] != root]
    tuples = []
    for run_id in sorted(set(busy_ids)):
        visible = [item for item in history["items"].get(run_id, []) if (item.get("start") or 0) <= now]
        if not visible:
            continue
        last = max(visible, key=lambda item: (item.get("ordinal") or 0, item["id"]))
        tuples.append((last["id"], last["type"], last["status"], last.get("updated") or 0))
    signature = hashlib.sha256(json.dumps(tuples).encode()).hexdigest()[:16]
    viewed_members = []
    for entry in members:
        if entry["thread_id"] == root:
            continue
        raw = _run_at(runs_by.get(entry["thread_id"], []), now)
        viewed_members.append(_view_run(raw, now) | {"thread_id": entry["thread_id"]})
    return {
        "root": root,
        "as_of": now,
        "settled": settled,
        "archived": archived,
        "busy": busy,
        "busy_since": busy_since,
        "run": current,
        "answer": answer,
        "later_human": later_human,
        "busy_member": any(entry["status"] in BUSY and entry["thread_id"] != root for entry in members),
        "live_request_ids": sorted(set(live_ids)),
        "members": viewed_members,
        "root_runs": [_view_run(run, now) for run in root_runs if run["requested"] <= now],
        "last_completed": last_completed,
        "running_items": running,
        "failed_item": failed,
        "completed_after_failure": later,
        "signature": signature,
        "orphaned_members": orphaned,
        "human_times": seen_humans,
        "history": history,
    }


def _terminal_runs(runs, now):
    found = []
    for run in runs or []:
        end = run.get("end") or 0
        if end and end <= now and run.get("status") not in BUSY:
            found.append(run)
    found.sort(key=lambda run: (run["end"], run.get("ordinal") or 0, run.get("run_id") or ""))
    return found


def _terminal_marks(snap):
    """Latest terminal end per thread that has already ended by this snapshot."""
    now = snap.get("as_of") or 0
    history = snap.get("history") or {}
    threads = set(history.get("members") or [])
    root = snap.get("root")
    if root:
        threads.add(root)
    marks = {}
    for thread_id in threads:
        ended = _terminal_runs((history.get("runs") or {}).get(thread_id), now)
        if ended:
            marks[thread_id] = ended[-1]["end"]
    return marks


def _watermarks(state, root):
    return dict((state["threads"].get(root) or {}).get("watermarks") or {})


def _remember(state, snap):
    previous = state["threads"].get(snap["root"]) or {}
    marks = dict(previous.get("watermarks") or {})
    for thread_id, end in _terminal_marks(snap).items():
        if end > (marks.get(thread_id) or 0):
            marks[thread_id] = end
    members = {}
    for entry in snap.get("members") or []:
        members[entry["thread_id"]] = [entry["run_id"], entry["status"]]
    run = snap.get("run") or {}
    if run.get("run_id"):
        members[snap["root"]] = [run.get("run_id"), run.get("status")]
    state["threads"][snap["root"]] = {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "busy": snap.get("busy"),
        "signature": snap.get("signature"),
        "progress_at": snap.get("last_completed") or 0,
        "members": members,
        "watermarks": marks,
    }


def seal_snapshot(state, snap):
    """Remember each thread's latest terminal end. Opens nothing."""
    _remember(state, snap)


def baseline_snapshot(state, snap):
    seal_snapshot(state, snap)
    state["baseline"] = True


def _answers_until(history, run, now):
    if not run:
        return None
    limit = run["end"] if run.get("ended") and run.get("end") else now
    return _answer_at(history["answers"].get(run["run_id"], []), limit + 1e-3)


def _generic_recovery(incident, snap, now):
    if incident["code"] == "stopped_member":
        return None
    if snap.get("settled") or snap.get("archived"):
        return "resolve"
    newer = [run for run in snap.get("root_runs") or [] if run.get("ordinal", 0) > (incident.get("run_ordinal") or 0)]
    if not newer:
        return None
    history = snap["history"]
    for run in newer:
        if not run.get("ended"):
            continue
        answer = _answers_until(history, run, now)
        humans = snap.get("human_times") or []
        later_human = any(h > run["end"] for h in humans)
        if _ended_resolved(run, answer, later_human):
            return "resolve"
    latest = newer[-1]
    if latest.get("status") in BUSY:
        return "defer"
    return None


def _recovery_already_visible(snap, viewed, decision, now):
    """True when this same snapshot would resolve the incident immediately."""
    if decision.get("action") not in {"open", "watch"}:
        return False
    probe = {
        "code": decision.get("code") or "",
        "run_ordinal": viewed.get("ordinal") or 0,
        "opened_at": viewed.get("end") or now,
        "run_status": viewed.get("status") or "",
    }
    return _generic_recovery(probe, snap, now) == "resolve"


def _eval_root_run(state, snap, run, now):
    """Evaluate one ended root run. A newer busy run is not this run's recovery."""
    viewed = _view_run(run, now)
    if not viewed or not viewed.get("ended") or viewed["run_id"] in state["handled_runs"]:
        return []
    current = snap.get("run") or {}
    if current.get("run_id") == viewed["run_id"]:
        answer = snap.get("answer")
        later_human = snap.get("later_human")
        busy_member = snap.get("busy_member")
        live = bool(snap.get("live_request_ids"))
    else:
        answer = _answers_until(snap["history"], viewed, now)
        later_human = any(h > viewed["end"] for h in (snap.get("human_times") or []))
        busy_member = False
        live = False
    decision = evaluate_run_ending(viewed, answer, later_human, busy_member, live)
    state["handled_runs"][viewed["run_id"]] = decision["action"]
    if _recovery_already_visible(snap, viewed, decision, now):
        state["handled_runs"][viewed["run_id"]] = "recovered"
        return []
    before = set(state["incidents"])
    _open_from_decision(state, snap, decision, now, viewed["end"] or now, run=viewed)
    return _new_logs(state, before, now)


def apply_snapshot(state, snap, now, startup=False):
    """Apply one root snapshot. Returns opened/resolved/due log dicts (codes and ids only)."""
    logs = []
    root = snap["root"]
    history = snap["history"]
    run = snap.get("run")
    if startup and not snap.get("settled") and not snap.get("archived") and run and run.get("ended"):
        logs.extend(_eval_root_run(state, snap, run, now))
    elif state.get("baseline"):
        row = state["threads"].get(root) or {}
        # A thread with no watermark has not been sealed. The startup sweep is
        # the only historical open, and it is the latest ended run, not the backlog.
        if "watermarks" not in row:
            if not snap.get("settled") and not snap.get("archived") and run and run.get("ended"):
                logs.extend(_eval_root_run(state, snap, run, now))
        else:
            mark = (row.get("watermarks") or {}).get(root) or 0
            for ended in _terminal_runs(history["runs"].get(root), now):
                if ended["end"] <= mark:
                    continue
                logs.extend(_eval_root_run(state, snap, ended, now))
    # Re-evaluate a watching red once the lineage is idle.
    if run and run["run_id"] in state["handled_runs"]:
        for incident in list(state["incidents"].values()):
            if incident["root"] != root or incident["run_id"] != run["run_id"] or incident["status"] != "watching":
                continue
            if incident["code"] != "red_unresolved" or incident.get("due_at"):
                continue
            decision = evaluate_run_ending(run, snap.get("answer"), snap.get("later_human"),
                                            snap.get("busy_member"), bool(snap.get("live_request_ids")))
            if decision["action"] == "resolve" or decision["action"] == "notify":
                logs.extend(resolve_incident(state, incident, now, decision.get("reason") or decision["action"]))
                if decision["action"] == "notify":
                    _open_from_decision(state, snap, decision, now, now)
            elif decision["action"] == "open":
                incident["due_at"] = now + (decision.get("grace") or GRACE["red_unresolved"])
                incident["next_check"] = incident["due_at"]
                incident["opened_at"] = now
                if now >= incident["due_at"]:
                    incident["status"] = "due"
                    logs.append(_log("due", incident))
    logs.extend(_stopped_members(state, snap, now, startup))
    logs.extend(_recover_open(state, snap, now))
    logs.extend(_stall(state, snap, now))
    logs.extend(_failed_tool(state, snap, now))
    _review(state, snap, now)
    logs.extend(promote(state, now))
    _remember(state, snap)
    if startup:
        state["baseline"] = True
    return logs


def _new_logs(state, before, now):
    logs = []
    for iid, incident in state["incidents"].items():
        if iid in before:
            continue
        logs.append(_log("opened", incident))
        if incident["status"] == "due":
            logs.append(_log("due", incident))
            incident["due_logged"] = True
    return logs


def _open_from_decision(state, snap, decision, now, opened_at, run=None):
    # human_blocked is still detected so a Needs-you answer does not become a
    # wake of its own. The assistant message is covered by threads_changed.
    if decision["action"] == "notify":
        return None
    if decision["action"] == "resolve":
        return None
    status = "watching"
    grace = decision.get("grace")
    if decision["action"] == "watch":
        grace = None
    incident, _logs = open_incident(
        state, code=decision["code"], root=snap["root"], run=run if run is not None else snap.get("run"), now=now,
        grace=grace, status=status, opened_at=opened_at,
        item=_representative_item(snap) if decision["code"] == "stalled" else None,
    )
    return incident


def _representative_item(snap):
    running = snap.get("running_items") or []
    if not running:
        return None
    return min(running, key=lambda item: item.get("start") or 0)


def _open_stopped(state, snap, now, run, thread_id):
    logs = []
    history = snap["history"]
    key = thread_id + "|" + run["run_id"]
    if key in state["handled_members"]:
        return logs
    if snap.get("settled") or snap.get("archived"):
        return logs
    failure = {
        "run_id": run["run_id"], "ordinal": run.get("ordinal"), "status": run.get("status"),
        "requested": run.get("requested"), "end": run.get("end"), "thread_id": thread_id,
    }
    member_runs = history["runs"].get(thread_id, [])

    def answers_for(candidate, _history=history, _now=now):
        return _answers_until(_history, candidate, _now)

    if stopped_member_recovered(
        failure, member_runs, snap.get("root_runs") or [], snap.get("human_times") or [], answers_for, now,
    ):
        state["handled_members"][key] = now
        return logs
    before = set(state["incidents"])
    open_incident(
        state, code="stopped_member", root=snap["root"], member=thread_id, run=failure,
        now=now, grace=GRACE["stopped_member"], status="watching", opened_at=run.get("end") or now,
        extra=run["run_id"],
    )
    state["handled_members"][key] = now
    logs.extend(_new_logs(state, before, now))
    return logs


def _stopped_since(state, snap, now):
    """Every member failure that ended after that member's watermark.

    A later busy retry is not recovery: only a later ended run can clear it.
    """
    logs = []
    history = snap["history"]
    root = snap["root"]
    marks = _watermarks(state, root)
    for thread_id in history.get("members") or []:
        if thread_id == root:
            continue
        mark = marks.get(thread_id) or 0
        for run in _terminal_runs(history["runs"].get(thread_id), now):
            if run.get("status") not in FAILURE or run["end"] <= mark:
                continue
            logs.extend(_open_stopped(state, snap, now, run, thread_id))
    return logs


def _stopped_current(state, snap, now):
    """Latest member run only. Older failures belong to the sealed watermark."""
    logs = []
    root = snap["root"]
    for member in snap.get("members") or []:
        if member["thread_id"] == root:
            continue
        if member["status"] not in FAILURE or not member.get("ended"):
            continue
        logs.extend(_open_stopped(state, snap, now, member, member["thread_id"]))
    return logs


def _stopped_members(state, snap, now, startup):
    if not startup and not state.get("baseline"):
        return []
    row = state["threads"].get(snap["root"]) or {}
    if startup or "watermarks" not in row:
        return _stopped_current(state, snap, now)
    return _stopped_since(state, snap, now)


def _recover_open(state, snap, now):
    logs = []
    history = snap["history"]
    for incident in list(state["incidents"].values()):
        if incident["root"] != snap["root"] or incident["status"] in {"resolved", "acked"}:
            continue
        # A live card and the open-thread signature are not run incidents.
        # A later green run must not clear them.
        if incident["code"] in {"waiting_card", "threads_changed"}:
            continue
        if incident["code"] == "unanswered_or_failed" and incident.get("run_status") in {"cancelled", "interrupted"}:
            if any(when > (incident.get("opened_at") or 0) for when in snap.get("human_times") or []):
                logs.extend(resolve_incident(state, incident, now, "later_human"))
                continue
        if snap.get("settled") or snap.get("archived"):
            logs.extend(resolve_incident(state, incident, now, "settled"))
            continue
        if incident["code"] == "stopped_member":
            failure = {"run_id": incident["run_id"], "status": incident.get("run_status"), "end": incident["opened_at"],
                       "requested": 0, "ordinal": incident.get("run_ordinal")}
            member_runs = history["runs"].get(incident.get("member"), [])
            def answers_for(run, _history=history, _now=now):
                return _answers_until(_history, run, _now)
            if stopped_member_recovered(failure, member_runs, snap.get("root_runs") or [], snap.get("human_times") or [], answers_for, now):
                logs.extend(resolve_incident(state, incident, now, "recovered"))
            continue
        effect = _generic_recovery(incident, snap, now)
        if effect == "resolve":
            incident["paused"] = False
            logs.extend(resolve_incident(state, incident, now, "later_run"))
        elif effect == "defer":
            incident["paused"] = True
            if incident["status"] == "due":
                incident["status"] = "watching"
        elif incident.get("paused"):
            incident["paused"] = False
            if incident.get("due_at") and now >= incident["due_at"]:
                incident["status"] = "due"
                if not incident.get("due_logged"):
                    incident["due_logged"] = True
                    logs.append(_log("due", incident))
    return logs


def _stall(state, snap, now):
    logs = []
    root = snap["root"]
    open_stalls = [incident for incident in state["incidents"].values()
                   if incident["root"] == root and incident["code"] == "stalled" and incident["status"] not in {"resolved", "acked"}]
    if not snap.get("busy"):
        for incident in open_stalls:
            logs.extend(resolve_incident(state, incident, now, "idle"))
        return logs
    assessment = assess_stall(snap.get("last_completed") or 0, snap.get("busy_since"), now, snap.get("running_items") or [])
    if not assessment:
        for incident in open_stalls:
            if (snap.get("last_completed") or 0) > (incident.get("anchor") or 0) + 1e-6:
                logs.extend(resolve_incident(state, incident, now, "progress"))
        return logs
    run = snap.get("run")
    extra = str(int(assessment["anchor"]))
    before = set(state["incidents"])
    incident, _ = open_incident(
        state, code="stalled", root=root, run=run, now=now, grace=None,
        status=assessment["status"], due_at=assessment["due_at"], opened_at=assessment["anchor"] + STALL_OPEN,
        extra=extra, anchor=assessment["anchor"], item=_representative_item(snap),
    )
    if incident["id"] not in before:
        logs.extend(_new_logs(state, before, now))
    elif incident["status"] != "resolved":
        # Same quiet period. Refresh due time if a deadline moved it, but do not reopen.
        if incident["status"] in {"watching", "due"} and assessment["due_at"]:
            incident["due_at"] = assessment["due_at"]
            incident["next_check"] = assessment["due_at"]
            if now >= assessment["due_at"] and incident["status"] == "watching":
                incident["status"] = "due"
                if not incident.get("due_logged"):
                    incident["due_logged"] = True
                    logs.append(_log("due", incident))
    # A newer completion clears the stall inside the same run.
    for incident in open_stalls:
        if (snap.get("last_completed") or 0) > (incident.get("anchor") or 0) + 1e-6:
            logs.extend(resolve_incident(state, incident, now, "progress"))
    return logs


def _failed_tool(state, snap, now):
    logs = []
    root = snap["root"]
    existing = [incident for incident in state["incidents"].values()
                if incident["root"] == root and incident["code"] == "failed_tool" and incident["status"] not in {"resolved", "acked"}]
    item = snap.get("failed_item")
    later = snap.get("completed_after_failure") or 0
    assessment = assess_failed_tool(item["end"] if item else 0, later, now, snap.get("busy"))
    if assessment["status"] == "clear" or not snap.get("busy"):
        for incident in existing:
            logs.extend(resolve_incident(state, incident, now, "recovered_tool"))
        return logs
    for incident in existing:
        if abs((incident.get("opened_at") or 0) - item["end"]) > 1e-3 or (later and later > incident["opened_at"]):
            logs.extend(resolve_incident(state, incident, now, "recovered_tool"))
    before = set(state["incidents"])
    open_incident(
        state, code="failed_tool", root=root, run=snap.get("run"), now=now, grace=None,
        status=assessment["status"], due_at=assessment["due_at"], opened_at=item["end"],
        item=item, extra=item["id"],
    )
    logs.extend(_new_logs(state, before, now))
    return logs


def _review(state, snap, now):
    if not snap.get("busy"):
        return
    previous = state["threads"].get(snap["root"], {})
    if previous.get("signature") and previous.get("signature") != snap.get("signature"):
        state["review_changed"] = state.get("review_changed", 0) + 1
        day = local_day(now)
        state["review_by_day"][day] = state["review_by_day"].get(day, 0) + 1
        bucket = _day_bucket(state, day)
        _count(bucket["class"], "DROP")


def _due_if_ready(incident, now):
    logs = []
    if incident["status"] == "watching" and incident.get("due_at") and now >= incident["due_at"]:
        incident["status"] = "due"
        if not incident.get("due_logged"):
            incident["due_logged"] = True
            logs.append(_log("due", incident))
    return logs


def apply_waiting_cards(state, cards, now):
    """One high-priority incident per live user_input request. Due two minutes after it appears.

    Resolved when the request leaves the live set. An ack does not resolve it and does not open another.
    """
    live = {}
    for card in cards or []:
        request_id = str(card.get("id") or "")
        thread_id = str(card.get("thread_id") or "")
        if request_id and thread_id:
            live[request_id] = card
    logs = []
    for incident in list(state["incidents"].values()):
        if incident.get("code") != "waiting_card" or incident["status"] == "resolved":
            continue
        if incident.get("request_id") not in live:
            logs.extend(resolve_incident(state, incident, now, "request_resolved"))
            continue
        logs.extend(_due_if_ready(incident, now))
    for request_id, card in live.items():
        start = float(card.get("start") or now)
        before = set(state["incidents"])
        incident, _created = open_incident(
            state, code="waiting_card", root=card["thread_id"], run=None, now=now,
            grace=None, status="watching", opened_at=start, due_at=start + CARD_DUE,
            extra=request_id,
        )
        incident["request_id"] = request_id
        if incident["id"] not in before:
            logs.extend(_new_logs(state, before, now))
        else:
            logs.extend(_due_if_ready(incident, now))
    return logs


def apply_threads_changed(state, pairs, now):
    """One low-priority incident while the open-thread signature differs from the acked hash.

    A change while that incident is still open updates the hash it will record on ack.
    After the ack, only a different signature opens another incident.
    """
    digest = threads_signature(pairs)
    state["threads_signature"] = digest
    acked = state.get("acked_threads_signature") or ""
    if digest == acked:
        return []
    open_ones = [
        incident for incident in state["incidents"].values()
        if incident.get("code") == "threads_changed" and incident["status"] not in {"resolved", "acked"}
    ]
    if open_ones:
        # Only an undelivered incident takes the newer hash. A delivered one keeps the
        # signature the guardian saw, so its ack can't cover later replies; once acked,
        # the differing signature opens a fresh incident.
        for incident in open_ones:
            if not incident.get("first_delivered_at"):
                incident["signature"] = digest
        return []
    before = set(state["incidents"])
    incident, _created = open_incident(
        state, code="threads_changed", root="", run=None, now=now,
        grace=0, status="due", opened_at=now, extra=digest,
    )
    incident["signature"] = digest
    return _new_logs(state, before, now)


def apply_outcomes(state, outcomes, dependency_states, now):
    logs = []
    for outcome in outcomes or []:
        oid = str(outcome.get("id") or "")
        if not oid or oid in state["triggered_outcomes"]:
            continue
        if not outcome_ready(outcome, dependency_states):
            continue
        before = set(state["incidents"])
        open_incident(
            state, code="outcome_trigger", root=oid, run={"run_id": oid, "ordinal": 0, "status": "terminal"},
            now=now, grace=0, status="due", opened_at=now, outcome_id=oid, extra=oid,
        )
        state["triggered_outcomes"][oid] = now
        logs.extend(_new_logs(state, before, now))
    return logs


def dependency_status(thread_id, known, excluded):
    """known maps thread id to 'busy', 'terminal', or omits it. Excluded is unknown."""
    if not thread_id or thread_id in excluded or thread_id not in known:
        return "unknown"
    return known[thread_id]


def note_delivery(state, payload, incidents, now):
    raw = dump_payload(payload)
    day = local_day(now)
    bucket = _day_bucket(state, day)
    bucket["deliveries"] += 1
    bucket["payload_bytes"] += len(raw)
    state["wakes_by_day"][day] = state["wakes_by_day"].get(day, 0) + 1
    state["last_delivery"] = now
    if any(_rank(incident, now) == 2 for incident in incidents):
        state["last_low"] = now
    for incident in incidents:
        incident["status"] = "delivered"
        incident["delivered_at"] = now
        incident["class"] = "WAKE"
        if not incident.get("first_delivered_at"):
            incident["first_delivered_at"] = now
        _touch_day(state, incident, "delivered", now)
    notice = None
    if state["wakes_by_day"][day] >= DAY_WAKE_NOTICE and day not in state["day_notified"]:
        state["day_notified"][day] = now
        notice = state["wakes_by_day"][day]
    return notice


def ack_delivery(state, incidents, now):
    for incident in incidents:
        if incident["status"] == "delivered":
            incident["status"] = "acked"
            incident["acked_at"] = now
            _remember_threads_ack(state, incident)


def low_slot_open(state, now):
    if not state.get("last_low"):
        return True
    return now - state["last_low"] >= LOW_BATCH


def next_wake_at(state, now):
    ready = ready_incidents(state, now)
    gap = state["last_delivery"] + WAKE_GAP if state.get("last_delivery") else now
    highs = [incident for incident in ready if _rank(incident, now) < 2]
    if highs:
        return max(now, gap)
    if ready:
        low_at = state["last_low"] + LOW_BATCH if state.get("last_low") else now
        return max(now, gap, low_at)
    future = [incident["due_at"] for incident in state["incidents"].values()
              if incident["status"] == "watching" and incident.get("due_at") and incident["due_at"] > now]
    return min(future) if future else None


def take_wake(state, now, event_id, ack=False):
    """Deliver due incidents if the 10 minute gap allows it. Returns the payload or None."""
    if state.get("last_delivery") and now < state["last_delivery"] + WAKE_GAP:
        return None
    ready = ready_incidents(state, now)
    if not ready:
        return None
    payload, chosen, _waiting = pack_incidents(ready, now, event_id, low_slot_open(state, now))
    if not chosen:
        return None
    note_delivery(state, payload, chosen, now)
    if ack:
        ack_delivery(state, chosen, now)
    return payload


def simulate(baseline, events, end, ack=True, max_steps=200000):
    """Replay snapshots. baseline is a list of (time, snap) recorded with no evaluation.

    events is a list of (time, snap) with start < time <= end. Returns state and payloads.
    """
    state = new_state()
    for _at, snap in baseline:
        baseline_snapshot(state, snap)
    state["baseline"] = True
    pending = sorted((at, index, snap) for index, (at, snap) in enumerate(events))
    wakes = []
    cursor = 0
    scheduled = []
    steps = 0
    def push(when):
        if when is not None and when <= end:
            scheduled.append(when)
    while cursor < len(pending) or scheduled:
        steps += 1
        if steps > max_steps:
            raise RuntimeError("replay clock did not advance")
        next_event = pending[cursor][0] if cursor < len(pending) else None
        next_clock = min(scheduled) if scheduled else None
        if next_event is None and next_clock is None:
            break
        if next_clock is None or (next_event is not None and next_event <= next_clock):
            at = next_event
            batch = []
            while cursor < len(pending) and pending[cursor][0] == at:
                batch.append(pending[cursor][2])
                cursor += 1
            for snap in batch:
                apply_snapshot(state, snap, at)
            if at in scheduled:
                scheduled = [item for item in scheduled if item != at]
        else:
            at = next_clock
            scheduled = [item for item in scheduled if item != at]
        promote(state, at)
        payload = take_wake(state, at, "replay-%d" % int(at), ack=ack)
        if payload:
            wakes.append({"at": at, "day": local_day(at), "incidents": len(payload["incidents"]),
                          "more_pending": payload["more_pending"], "bytes": len(dump_payload(payload))})
        push(next_wake_at(state, at + 1e-3))
        # Avoid a tight loop if next_wake_at returns `at`.
        scheduled = sorted({item for item in scheduled if item > at})
    return state, wakes


def unacked_redelivery_events(state, end):
    """How many extra sends the 60 minute / 3 hour cadence would add with no acks."""
    total = 0
    for incident in state["incidents"].values():
        start = incident.get("first_delivered_at")
        if not start:
            continue
        stop = incident.get("resolved_at") or end
        cadence = STALL_REDELIVER if incident["code"] == "stalled" else REDELIVER
        at = start + cadence
        while at < stop:
            total += 1
            at += cadence
    return total


def string_fields(value):
    found = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for inner in value.values():
            found.extend(string_fields(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(string_fields(inner))
    return found
