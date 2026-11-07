#!/usr/bin/env python3
"""Daily stale-thread pass for the Thread Health Watch (trial to 14 Nov 2026; Andy picked it 7 Nov 2026).

Finds Andy's unsettled top-level threads idle for 48 h or more, writes one extract per thread (Andy's messages,
agent relays, and every reply after his last message), checks the git repos and GitHub PRs those replies name,
and says for each thread whether the Watch may settle it. Read-only on T3's database.

  stale_sweep.py                    find candidates, write extracts and summary.json, print the summary
  stale_sweep.py --settle ID ...    re-check each thread now, then settle it through T3's guarded auto-settle
                                    (refused if the thread changed since its extract); logs the ones that worked
  stale_sweep.py --min-idle-hours N use another idle limit (tests)
  stale_sweep.py --test-ids ID ...  test only: treat these threads (settled or not) as candidates, output to daily/test
"""
import hashlib, json, os, re, sqlite3, subprocess, sys
from datetime import date, datetime, timedelta, timezone

HOME = os.path.expanduser("~")
DB = f"{HOME}/.t3/userdata/statev2.sqlite"
BASE = f"{HOME}/Library/Caches/stale-thread-triage"
LOG = f"{BASE}/settled-log.jsonl"
SETTLE_LIB = f"{HOME}/Library/Caches/settle-sweep"  # t3session.py: T3's own guarded settle route
TRIAL_END = date(2026, 11, 14)
ENV = open(f"{HOME}/.t3/userdata/environment-id").read().strip()
ACTIVE = ("queued", "preparing", "starting", "running", "waiting")
KEEP_OPEN = re.compile(r"(?i)\b(keep|leave)\s+(this\s+|the\s+|it\s+)?(thread\s+)?(open|unsettled|active)\b|\bdon'?t\s+settle\b|\bdo\s+not\s+settle\b|\bnot\s+settle\b")
REPO = re.compile(re.escape(f"{HOME}/Documents/ChatGPT/") + r"([A-Za-z0-9._-]+)")
PR = re.compile(r"github\.com/(cornishandy/[A-Za-z0-9._-]+)/pull/(\d+)")
FOOTER_LIMIT, REPLY_HEAD, REPLY_TAIL = 9000, 1500, 1500


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def link(tid):
    return f"t3-thread://v2/{ENV}/{tid}"


def plain(text):
    return (text or "").replace("’", "'").replace("‘", "'")


def housekeeping(p):
    return bool(p.get("idleCompaction") or p.get("notification") or p.get("delegatedCompletion"))


def clip(text):
    """Whole reply if short; else its start and end (requests and footers sit at the end), with the cut marked."""
    if 'itemprop="settle-' in text and len(text) <= FOOTER_LIMIT:
        return text
    if len(text) <= REPLY_HEAD + REPLY_TAIL:
        return text
    return f"{text[:REPLY_HEAD]}\n…(CUT {len(text) - REPLY_HEAD - REPLY_TAIL} chars; read this message in the thread if the cut part could hold a request)…\n{text[-REPLY_TAIL:]}"


def repo_state(name):
    path = f"{HOME}/Documents/ChatGPT/{name}"
    if not os.path.isdir(f"{path}/.git"):
        return None
    out = subprocess.run(["git", "-C", path, "status", "--porcelain=v1", "-b"], capture_output=True, text=True, timeout=30).stdout
    lines = out.splitlines()
    head = lines[0][3:] if lines else "?"
    changed = [l for l in lines[1:] if not re.search(r" 2(\.[^/]*)?\"?$", l)]  # iCloud "name 2.ext" copies are junk
    return f"{name}: {head}; {len(changed)} uncommitted"


def pr_state(repo, num):
    out = subprocess.run(["gh", "pr", "view", num, "-R", repo, "--json", "state,mergeStateStatus"], capture_output=True, text=True, timeout=30)
    if out.returncode:
        return f"{repo}#{num}: state unknown"
    j = json.loads(out.stdout)
    return f"{repo}#{num}: {j.get('state')}" + (f" ({j.get('mergeStateStatus')})" if j.get("state") == "OPEN" else "")


def tree_count(con, tid, table, cond="", *args):
    """Rows in `table` for this thread or any helper thread under it, matching `cond`."""
    return con.execute(f"""select count(*) from {table} x join orchestration_v2_projection_threads t on t.thread_id=x.thread_id
        where (json_extract(t.payload_json,'$.lineage.rootThreadId')=? or t.thread_id=?) {cond}""", (tid, tid, *args)).fetchone()[0]


def assess(con, tid, p, now, hours):
    """(listed, why_not_settle or None, facts) for one thread."""
    msgs = [(ca, role, json.loads(m)) for ca, role, m in con.execute(
        "select created_at, role, payload_json from orchestration_v2_projection_messages where thread_id=? and streaming=0 order by created_at", (tid,))]
    real = [m for m in msgs if not housekeeping(m[2])]
    last_run = con.execute("""select max(coalesce(r.completed_at, r.requested_at)) from orchestration_v2_projection_runs r
        join orchestration_v2_projection_threads t on t.thread_id=r.thread_id
        where json_extract(t.payload_json,'$.lineage.rootThreadId')=? or t.thread_id=?""", (tid, tid)).fetchone()[0]
    quiet = max(x for x in [iso(real[-1][0]) if real else None, iso(p.get("updatedAt")), iso(last_run), iso(p.get("createdAt"))] if x)
    if now - quiet < timedelta(hours=hours):
        return False, None, None
    andy = [(ca, m.get("text") or "") for ca, role, m in real if role == "user" and m.get("createdBy", "user") == "user"]
    relays = [(ca, m.get("text") or "") for ca, role, m in real if role == "user" and m.get("createdBy", "user") != "user"]
    last_andy = andy[-1][0] if andy else ""
    replies = [(ca, m.get("text") or "") for ca, role, m in real if role == "assistant" and ca > last_andy]
    busy = tree_count(con, tid, "orchestration_v2_projection_runs", f"and x.status in ({','.join('?' * len(ACTIVE))})", *ACTIVE)
    pending = tree_count(con, tid, "orchestration_v2_projection_runtime_requests", "and x.status='pending'")
    why = None
    if p.get("pinnedAt"): why = "pinned"
    elif p.get("snoozedUntil"): why = "snoozed"
    elif p.get("autoSettleDisabledAt"): why = "auto-settle turned off"
    elif busy: why = "work still running in it or a helper"
    elif pending: why = "a question card is pending in it or a helper"
    elif any(KEEP_OPEN.search(plain(t)) for _, t in andy + relays): why = "asked to keep it open"
    elif andy and not replies: why = "Andy's last message has no reply"
    return True, why, dict(quiet=quiet, andy=andy, relays=relays, replies=replies, last_andy=last_andy)


def unsettled_by_hand(con):
    """Threads this trial settled that T3 later unsettled other than by starting a run. T3 logs an unsettle caused by a
    run (a message or a scheduled task) in the same command as that run's `run.created` event; any other unsettle,
    or one whose cause can't be read, counts as a hand unsettle (Astra, 7 Nov 2026: treat ambiguous as a stop)."""
    hits = []
    if not os.path.exists(LOG):
        return hits
    seen = {}
    for line in open(LOG):
        e = json.loads(line)
        seen[e["threadId"]] = e["settledAt"]
    for tid, at in seen.items():
        for when, cmd in con.execute(
                "select occurred_at, command_id from orchestration_events where stream_id=? and event_type='thread.unsettled'", (tid,)):
            if iso(when) <= iso(at):
                continue
            by_run = bool(cmd) and con.execute(
                "select 1 from orchestration_events where command_id=? and event_type='run.created' limit 1", (cmd,)).fetchone()
            if not by_run:
                title = con.execute("select title from orchestration_v2_projection_threads where thread_id=?", (tid,)).fetchone()
                hits.append(f"[{title[0] if title else tid}]({link(tid)})")
                break
    return hits


def settle(ids):
    if date.today() > TRIAL_END:
        sys.exit("TRIAL ENDED (14 Nov 2026): no settling; ask Andy keep or stop")
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    summary = json.load(open(f"{BASE}/daily/{datetime.now().strftime('%Y-%m-%d')}/summary.json"))
    taken = {c["threadId"]: c for c in summary["candidates"]}
    batch = []
    for tid in ids:
        row = con.execute("select payload_json from orchestration_v2_projection_threads where thread_id=?", (tid,)).fetchone()
        if tid not in taken or taken[tid]["action"] != "settle" or not row:
            print(f"SKIP {tid}: not marked settle in today's summary"); continue
        p = json.loads(row[0])
        if p.get("settledAt"):
            print(f"SKIP {tid}: already settled"); continue
        listed, why, _ = assess(con, tid, p, now, summary["minIdleHours"])
        if not listed or why:
            print(f"SKIP {tid}: {why or 'no longer idle'}"); continue
        batch.append({"threadId": tid, "snapshotAt": taken[tid]["extractAt"]})
    if not batch:
        return
    sys.path.insert(0, SETTLE_LIB)
    from t3session import Session
    with Session("stale-thread pass") as s:
        results = s.settle(batch)
    with open(LOG, "a") as f:
        for r in results:
            if r.get("ok"):
                f.write(json.dumps({"threadId": r["threadId"], "settledAt": datetime.now(timezone.utc).isoformat()}) + "\n")
    for r in results:
        print(("SETTLED " if r.get("ok") else "REFUSED ") + r["threadId"] + ("" if r.get("ok") else f": {r.get('error')}"))


def main():
    if "--settle" in sys.argv:
        return settle(sys.argv[sys.argv.index("--settle") + 1:])
    test_ids = sys.argv[sys.argv.index("--test-ids") + 1:] if "--test-ids" in sys.argv else None
    if date.today() > TRIAL_END and not test_ids:
        print("TRIAL ENDED (14 Nov 2026): don't sweep; ask Andy keep or stop"); return
    hours = 0 if test_ids else (float(sys.argv[sys.argv.index("--min-idle-hours") + 1]) if "--min-idle-hours" in sys.argv else 48)
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out_dir = f"{BASE}/daily/" + ("test" if test_ids else datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(out_dir, exist_ok=True)
    if test_ids:
        rows = [r for r in con.execute("select thread_id, payload_json from orchestration_v2_projection_threads") if r[0] in test_ids]
    else:
        rows = con.execute("""select thread_id, payload_json from orchestration_v2_projection_threads
            where deleted_at is null and archived_at is null and json_extract(payload_json,'$.settledAt') is null
            and json_extract(payload_json,'$.lineage.parentThreadId') is null""").fetchall()
    summary = []
    for tid, pj in rows:
        p = json.loads(pj)
        title = p.get("title") or ""
        if title.lower().startswith("thread health watch"):
            continue
        extract_at = datetime.now(timezone.utc).isoformat()
        listed, why, f = assess(con, tid, p, now, hours)
        if not listed:
            continue
        sched = con.execute("select title, next_run_at from scheduled_tasks where thread_id=? and enabled=1", (tid,)).fetchall()
        text = [f"# {title}\n{link(tid)} · idle since {f['quiet']:%Y-%m-%d %H:%M} UTC · action: {'settle' if not why else 'list only: ' + why}\n"]
        if sched:
            text.append("Scheduled tasks bound here (they reopen it when they run): " + "; ".join(f"{t} next {n}" for t, n in sched) + "\n")
        text.append(f"\n## Andy's messages ({len(f['andy'])})\n")
        text += [f"- [{ca[:16]}] {t[:2500 if ca == f['last_andy'] else 600]}\n" for ca, t in f["andy"]]
        if f["relays"]:
            text.append(f"\n## Agent messages ({len(f['relays'])}; relays of Andy's answers, handover notes; last 8)\n")
            text += [f"- [{ca[:16]}] {t[:600]}\n" for ca, t in f["relays"][-8:]]
        text.append(f"\n## Every reply after Andy's last message ({len(f['replies'])})\n")
        text += [f"\n### [{ca[:16]}]\n{clip(t)}\n" for ca, t in f["replies"]]
        body = "".join(text)
        states = [s for s in (repo_state(r) for r in sorted(set(REPO.findall(body)))) if s]
        states += [pr_state(r, n) for r, n in sorted(set(PR.findall(body)))]
        if states:
            body += "\n## Repos and PRs named in this thread (git status without fetch; gh pr view)\n" + "".join(f"- {s}\n" for s in states)
        slug = (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "untitled") + "-" + hashlib.sha1(tid.encode()).hexdigest()[:8]
        path = f"{out_dir}/{slug}.md"
        open(path, "w").write(body)
        summary.append(dict(threadId=tid, title=title, link=link(tid), action="settle" if not why else "list-only",
                            reason=why, idleSince=f["quiet"].isoformat(), extractAt=extract_at, extract=path, chars=len(body),
                            cut=body.count("…(CUT "), repos=states, scheduled=[t for t, _ in sched]))
    hits = unsettled_by_hand(con)
    json.dump(dict(at=now.isoformat(), minIdleHours=hours, candidates=summary, unsettledByHand=hits),
              open(f"{out_dir}/summary.json", "w"), indent=1)
    print(f"{len(summary)} thread(s) idle {hours:g} h or more; extracts in {out_dir}")
    for s in summary:
        extra = f" · {s['cut']} long replies cut" if s["cut"] else ""
        extra += f" · repos: {'; '.join(s['repos'])}" if s["repos"] else ""
        extra += f" · scheduled: {', '.join(s['scheduled'])}" if s["scheduled"] else ""
        print(f"- {s['action']}{' (' + s['reason'] + ')' if s['reason'] else ''} · [{s['title']}]({s['link']}) · {s['chars']} chars · {s['extract']}{extra}")
    print("KILL TEST: " + ("STOP, unsettled by hand: " + ", ".join(hits) if hits else "pass"))


if __name__ == "__main__":
    main()
