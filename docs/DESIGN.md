# Thread health watch v2: script first, model on anomalies only

Draft 1, 25 Oct 2026, Opus 5.5 medium (lead). For pre-OK (Astra) and design challenge (Sol).

## Problem (measured 25 Oct)
- Today's daemon `~/Library/Caches/thread-watch/adaptive_watch.py` (the guardian's launchd job, read-only on T3's `statev2.sqlite`) already scans every 5 s–10 min by Mac presence and emits 8 reason codes. It POSTs every batch to the webhook of scheduled task `guardian-events-v1`, which wakes the bound guardian thread (a model run) each time; at most one queued review.
- The guardian thread was running a few hundred model wakes a day.
- Over two days about 70% of webhook deliveries carried only `review_changed_activity` (routine activity); the rest were mostly finished-work and no-activity checks, with a handful of questions and stopped runs.
- Almost every guardian reply was a routine "nothing new" wake, yet each one re-read a very large handoff. That re-reading was the cost driver, and a large share of the weekly Codex allowance.
- Observed deviation: the guardian model once launched a fix task despite its hard rule "no new model/source job".

## Goal
Routine checks done by script. A model wakes only on an anomaly the script cannot classify, with a compact digest (≤ 4 KB), never the handoff history. Keep: dead-end detection (all 8 codes), the open outcomes the chain keeps open, the protected plans it names, read-only behaviour, no secrets. Measure wakes/day and tokens/wake before and after.

## Design (draft 2, after Astra NO-GO and Sol challenge, 25 Oct 22:20)
Keep the scan loop, presence policy, read-only DB access and delivery/backoff. Replace "every event goes to the model" with a durable **incident** list in state: each incident `{id, root, code, run_id, opened_at, priority, status: watching|due|delivered|acked|resolved, next_check}`. Scripts resolve incidents deterministically; only `due` incidents are delivered.

### Signals the scan must produce (fixes Sol 2 and 4)
- **Run endings** are detected per root by latest run id/status change (busy→idle or new terminal run), independent of the activity signature. At startup, every unsettled root whose latest run is terminal is evaluated once.
- **Failed tools** inside busy runs are tracked (latest failed item id/time).
- `review_changed_activity` is still computed cheaply and counted, never delivered.

### Run-ending evaluation (fixes Astra 2, Sol 3)
For the root's latest run, in order:
1. Run `failed`/`cancelled`/`interrupted`, or no completed assistant message created in this run → incident `unanswered_or_failed`. Cancelled/interrupted with a later human message → resolved.
2. Current-run answer has a green verdict (`settle-yes`, "Safe to settle", "Wrapped up") → resolved.
3. Current-run answer red with a non-empty **Needs you** row (an older run's footer never counts) → `human_blocked`: NOTIFY once per message id; resolved until the next run.
4. Red with a Running row and a busy lineage member or live runtime request → `watching`; re-evaluated when that member ends.
5. Any other red (nothing running, nobody asked) → incident `red_unresolved`.
6. No verdict at all → incident `no_verdict`.

### Recovery (Astra 2, "busy parent is not proof")
An incident opened on run N resolves only when a later run of the same root ends and is evaluated as resolved by the rules above, or when Andy settles/archives the root. A newer busy run only defers the check (status `watching`). Grace before an incident is `due`: 5 min for failed/unanswered, 10 min for red_unresolved/no_verdict, 10 min for a failed tool with no later completed item.

### Stopped child runs (draft 2.1, fixes Astra r2 blocker)
- Every lineage member (not just the root) whose latest run turns `failed`/`cancelled`/`interrupted` opens its own incident `stopped_member`, keyed by (member thread id, run id). Another busy member, the root being busy, or any "Needs you" row never suppresses or resolves it.
- Recovery: a later run of the **same member** ends evaluated as resolved (completed with an answer), or a later run of the root starts after the failure **and** that root run ends resolved (green verdict, or human_blocked whose answer was created after the failure). Otherwise it is due after 5 min grace. Cancelled member runs followed by a later human message in the root → resolved.
- Replay requirement: a child run fails while a sibling stays busy → incident due 5 minutes after the failure.

### Clarifications (Astra r2 shoulds)
- Verdict parsing: take the **last** verdict marker in the current-run answer; red markers (`settle-no`, "Don't settle yet", "Not wrapped up") take precedence; "Not wrapped up" never matches green.
- Stalls and failed tools can clear within the same run (a later completed item). Re-delivery cadence: an unacked incident re-delivers after 60 min, except `stalled` which follows its own 3 h cadence.
- The 4 KB payload limit is enforced on serialized bytes; incidents that don't fit wait with `more_pending`.
- The 12–15 wakes/day figure is a draft-1 estimate; the build must report a draft-2 replay estimate.

### Stalls (fixes Astra 1, Sol 1)
A busy root with no completed item for 30 min becomes an incident `stalled`, **regardless of running items** (status alone never proves liveness). It is due at 45 min, then re-delivered at most every 3 h for the same run. Exception: a running item with an explicit future deadline in its metadata defers to that deadline (if no such field exists, no exception).

### Questions and other notices (Astra Q3)
- Live runtime requests: NOTIFY per request id (no request lost to rate limiting: one notification may cover several, "3 threads are waiting on you").
- Text-only question: current-run answer with Needs you and no later human message → NOTIFY once per message id.
- Work under a settled parent → NOTIFY.
- Notifications carry counts only, never titles or text.

### Open outcomes and holds (fixes Astra 5)
- `open-outcomes.json` migrates the **full** inventory from handoff sections 2, 3, 5, 6: every open outcome, hold and hands-off rule, each with `dependency_thread_ids` (including known backing/delegated threads named there) and `wake_when`.
- `all_terminal` triggers only when every dependency is positively read as terminal (not busy, no live request); a missing, excluded or unreadable dependency is `unknown` → no trigger. Each trigger delivers once.
- The inventory is checked line by line against the handoff at final OK, before the old chain retires.

### Delivery and digest (fixes Astra 3 and 4)
- Payload `{event_id, kind: "thread_health_v2", incidents: [...], more_pending: n}`: **metadata only** — incident id, code, root and member thread ids, `t3-thread://` link, run ordinal/status, item types/status/timestamps, ages, matching outcome ids. No titles, no message text, no excerpts; nothing content-bearing in state, logs or notifications.
- Max 10 incidents per wake (≤ 4 KB); the rest wait (`more_pending`).
- **Acknowledgement:** the guardian appends covered incident ids to `acks.jsonl` (in the watch folder) at the end of its reply. Delivered-but-unacked incidents are re-delivered after 60 min.
- **No daily cap that drops.** Wakes at least 10 min apart; priority `high` (failed/unanswered, stalled, outcome triggers) first; `low` (no_verdict, red_unresolved older than 6 h, cancelled) batched at most hourly. Above 30 wakes in a day: one notification to Andy, queue continues.

### The guardian model
New thread, Gemini 4 Argon High (Andy's 25 Oct choice), standing prompt [`GUARDIAN-PROMPT.md`](../watch/GUARDIAN-PROMPT.md) (≤ 1K words): read the payload and `open-outcomes.json`; for each incident read the named threads with small limits (≤ 3 items, ≤ 2K chars each); report findings to Andy; append acks. Hard rule: never starts, steers, cancels, settles or edits anything (it reports; Andy acts). Handover only when `handover check` says so.

### Metrics
As draft 1, plus incidents opened/resolved/delivered per code per day.

### Rollout
1. Build in `~/Library/Caches/thread-watch-v2/` with tests: pure classifier unit tests + targeted replays of real incidents from a private replay harness (not in this repo): a stall of nearly 10 hours → WAKE at 45 min; a silent completion → WAKE; six idle red roots → classified, not dropped; failed tools that recovered → no WAKE; 72 h replay estimate ~12–15 wakes/day.
2. Shadow (`--shadow`, separate state, no delivery/notify) ≥ 8 h beside the live daemon. Pass: every non-routine finding the live guardian made maps to a v2 incident; wakes/day within estimate.
3. Final OK (Astra) incl. the outcome inventory. Cutover with delivery gated: steer old guardian → disable `guardian-events-v1` (pause) → back up live config/state/plist → stop old daemon → start v2 under the same launchd label (its outbox waits) → launch new guardian → add it to exclusions → rebind task to it, enable → verify first wake and ack → old chain unpins/settles itself after verified handover. Rollback = restore backups, rebind to the old thread.
4. ≥ 24 h live, then report to the plan-review thread.

## Draft 1 open questions (answered by Astra; kept for history)
- Is DROP on `review_changed_activity` safe, given the other codes plus the 45-min stall rule?
- Footer-verdict heuristic for "finished cleanly": too weak or too strict?
- Is NOTIFY (no model) enough for questions/approvals?

## Draft 2.2: the guardian's new job (26 Oct, after Andy's 2–4 AM instructions)
Overnight, Andy changed what the guardian is for (`~/.claude/rules-reference/guardian-attention.md`, quoted in the live handoff §2):
- The guardian answers other threads' waiting question cards itself (strict rule 3).
- It keeps `~/Library/Caches/open-threads/open-threads.md` current: one line per useful new reply.
- It stays quiet: "Nothing needs you."
- Settling is a script's job now (settle sweep).
- Andy is overwhelmed by interruptions.

v2 must feed that job. Changes:
1. **`waiting_card`** (WAKE, high). One incident per live `user_input` runtime request in an unsettled thread outside the exclusion list. Due 2 min after it appears. Resolved when the request resolves; no ack needed, but acks still count. This replaces NOTIFY for `question_or_approval`.
2. **`threads_changed`** (WAKE, low; hourly slot). Signature = sorted (top-level unsettled thread id, latest completed assistant message id). When it differs from the signature the guardian last acked, keep one open incident. A new change re-arms it after an ack. Text-only questions and work under a settled parent fold into it: no separate NOTIFY.
3. **No macOS notifications** from v2, except one "watch over budget" (above 30 wakes in a day) and one for read errors (as today).
4. **Guardian wake prompt** = `GUARDIAN-PROMPT.md`, which keeps `guardian-attention.md` as the binding procedure (`guardian-wake check/record/footer`, the open-threads list, andy-items) and adds the v2 payload: incidents to check, then acks.
5. **Switch-over target:** keep the current guardian thread (Thread health watch (49)) and the same scheduled task and webhook. Only the daemon behind the webhook changes, plus a one-line steer to the guardian. No rebind, no new thread, no exclusion change.
