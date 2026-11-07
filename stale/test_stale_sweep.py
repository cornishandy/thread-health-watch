#!/usr/bin/env python3
"""Fixtures for stale_sweep.py: Astra's 7 cases from 7 Nov 2026 plus a plain settle case. Run: python3 test_stale_sweep.py"""
import importlib.util, json, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ss", os.path.join(HERE, "stale_sweep.py"))
ss = importlib.util.module_from_spec(spec); spec.loader.exec_module(ss)

NOW = datetime.now(timezone.utc)
OLD = (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def t(minutes):  # a time `minutes` after OLD
    return (datetime.fromisoformat(OLD.replace("Z", "+00:00")) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def db():
    path = tempfile.mktemp(suffix=".sqlite")
    c = sqlite3.connect(path)
    c.executescript("""
    create table orchestration_v2_projection_threads(thread_id, project_id, title, payload_json, deleted_at, archived_at);
    create table orchestration_v2_projection_messages(thread_id, created_at, role, payload_json, streaming);
    create table orchestration_v2_projection_runs(thread_id, status, requested_at, completed_at, ordinal);
    create table orchestration_v2_projection_runtime_requests(thread_id, status);
    create table scheduled_tasks(thread_id, title, next_run_at, enabled);
    create table orchestration_events(stream_id, event_type, occurred_at, command_id);""")
    return path, c

def thread(c, tid, title="T", parent=None, root=None, **extra):
    p = dict(title=title, createdAt=OLD, updatedAt=OLD, lineage={"parentThreadId": parent, "rootThreadId": root or tid}, **extra)
    c.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,null,null)", (tid, "p", title, json.dumps(p)))

def msg(c, tid, at, role, text, by="user"):
    c.execute("insert into orchestration_v2_projection_messages values (?,?,?,?,0)", (tid, at, role, json.dumps({"text": text, "createdBy": by})))

def run(path, *argv):
    ss.DB = path
    ss.BASE = tempfile.mkdtemp()
    ss.LOG = os.path.join(ss.BASE, "settled-log.jsonl")
    return ss

def assess(path, tid):
    con = sqlite3.connect(path)
    p = json.loads(con.execute("select payload_json from orchestration_v2_projection_threads where thread_id=?", (tid,)).fetchone()[0])
    return ss.assess(con, tid, p, NOW, 48)

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name); fails += 0 if cond else 1

path, c = db()
# 0. plain case: Andy asked, assistant answered, idle 5 days -> settle
thread(c, "plain"); msg(c, "plain", t(0), "user", "Do X."); msg(c, "plain", t(1), "assistant", "Done X.")
# 1. curly apostrophe keep-open
thread(c, "curly"); msg(c, "curly", t(0), "user", "Don’t settle this thread"); msg(c, "curly", t(1), "assistant", "Acknowledged.")
# 1b. keep-open relayed by an agent
thread(c, "relayed"); msg(c, "relayed", t(0), "user", "Keep it open, Andy said.", by="agent"); msg(c, "relayed", t(1), "assistant", "OK.")
# 2. Andy's message followed only by an agent message
thread(c, "hidden"); msg(c, "hidden", t(0), "user", "Please finish this."); msg(c, "hidden", t(1), "user", "Background report; no answer to Andy.", by="agent")
# 3. pending question in a helper thread
thread(c, "root3"); msg(c, "root3", t(0), "user", "Go."); msg(c, "root3", t(1), "assistant", "Started a helper.")
thread(c, "child3", parent="root3", root="root3"); c.execute("insert into orchestration_v2_projection_runtime_requests values ('child3','pending')")
# 4. a request in a reply without a footer, followed by another reply
thread(c, "mock"); msg(c, "mock", t(0), "user", "Make mockups.")
msg(c, "mock", t(1), "assistant", "Please review mockup A; your approval is still needed."); msg(c, "mock", t(2), "assistant", "Unrelated result B.")
c.commit()

check("plain thread settles", assess(path, "plain")[1] is None)
check("curly-apostrophe keep-open is list-only", assess(path, "curly")[1] == "asked to keep it open")
check("relayed keep-open is list-only", assess(path, "relayed")[1] == "asked to keep it open")
check("Andy's unanswered message is list-only", assess(path, "hidden")[1] == "Andy's last message has no reply")
check("pending question in a helper blocks the parent", assess(path, "root3")[1] == "a question card is pending in it or a helper")
f = assess(path, "mock")[2]
check("every reply after Andy's message is kept", any("mockup A" in r[1] for r in f["replies"]) and len(f["replies"]) == 2)
long = "x" * 5000 + " Please approve the layout."
check("a long reply keeps its end", "Please approve the layout." in ss.clip(long) and "CUT" in ss.clip(long))

# 5. kill test: hand unsettle just after a sub-second settle, and one followed by a later message run
run(path)
os.makedirs(ss.BASE, exist_ok=True)
settled = (datetime.fromisoformat(t(10).replace("Z", "+00:00")) + timedelta(milliseconds=900)).isoformat()
open(ss.LOG, "w").write(json.dumps({"threadId": "plain", "settledAt": settled}) + "\n" + json.dumps({"threadId": "mock", "settledAt": settled}) + "\n")
c.execute("insert into orchestration_events values ('plain','thread.unsettled',?,'cmd-click-1')", (t(11),))   # clicked, no run
c.execute("insert into orchestration_events values ('mock','thread.unsettled',?,'cmd-click-2')", (t(12),))    # clicked...
c.execute("insert into orchestration_events values ('mock','run.created',?,'cmd-msg-2')", (t(12),))           # ...and a message 0 s later
c.execute("insert into orchestration_v2_projection_runs values ('mock','completed',?,?,1)", (t(12), t(13)))
c.commit()
hits = ss.unsettled_by_hand(sqlite3.connect(path))
check("hand unsettle is caught", any("plain" in h for h in hits))
check("hand unsettle followed by a message 0 s later is caught", any("mock" in h for h in hits))
c.execute("insert into orchestration_events values ('curly','thread.unsettled',?,'cmd-run-3')", (t(30),))
c.execute("insert into orchestration_events values ('curly','run.created',?,'cmd-run-3')", (t(30),))
c.execute("insert into orchestration_v2_projection_runs values ('curly','completed',?,?,1)", (t(30), t(31)))
open(ss.LOG, "a").write(json.dumps({"threadId": "curly", "settledAt": settled}) + "\n"); c.commit()
check("a run reopening a thread is not a hand unsettle", not any("curly" in h for h in ss.unsettled_by_hand(sqlite3.connect(path))))

# 6. two threads with the same title get separate extracts (full main() run on the fixture DB)
thread(c, "rev1", title="Review"); msg(c, "rev1", t(0), "user", "a"); msg(c, "rev1", t(1), "assistant", "Approve plan 1?")
thread(c, "rev2", title="Review"); msg(c, "rev2", t(0), "user", "b"); msg(c, "rev2", t(1), "assistant", "Approve plan 2?"); c.commit()
ss.repo_state = lambda name: None; ss.pr_state = lambda r, n: "x"
sys.argv = ["stale_sweep.py"]; ss.main()
day = os.path.join(ss.BASE, "daily", datetime.now().strftime("%Y-%m-%d"))
s = json.load(open(os.path.join(day, "summary.json")))
paths = [x["extract"] for x in s["candidates"] if x["title"] == "Review"]
check("same titles get different extract files", len(paths) == 2 and len(set(paths)) == 2)

# 7. re-check before settling: a new message since the extract makes it ineligible
msg(c, "rev1", NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "user", "wait, one more thing"); c.commit()
check("re-check refuses a thread that changed", assess(path, "rev1")[0] is False)

print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
