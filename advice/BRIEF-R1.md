# Needs you advice, round 1: research each item and rank its options

You are one of two senior advisors: Grok 4.7 (xhigh) and Gemini 4 Argon High. The other works this same brief in parallel; in round 2 each of you audits the other's file claim by claim, and a script checks every quote you write against the source you cite. Sloppy, vague or invented work gets caught and thrown out, and Andy sees which advisor's rationale was left out.

## Why you're here

Andy (Sat 7 Nov 2026): "have them go through each of the Needs You items … give me rank choice options based on their recommendations and show strength of their recommendation and rationale … Really boss them around, make them do good work to help me resolve these items. Meant to burn grok and gemini usage on helpful tasks. Make them be thorough in their analysis and bold in their recommendations."

Andy runs many AI agent threads in T3 Code. A long-running "Thread health watch" thread keeps a short list of things only he can decide or do (the "Needs you" items). They pile up because he's overwhelmed. Your job: make each item fast to resolve, with one ranked choice he can accept in a second. This is not a skim: spend your usage.

## The items

`items.json` in your job folder: each item's number `n`, `topic`, the `row` Andy sees, `since` (when he was first asked), `due`, and `thread_tails` (excerpts of the threads the row links to: Andy's last real messages and the thread's last six messages, each with its timestamp in brackets).

## For every item, find out

1. **Still needed and accurate?** Check the live state: the linked thread's latest messages, the files, the PR, the page. If the premise is stale, include an option to drop or rewrite the item, with proof.
2. **Since and due:** verify `since` (the day Andy was first asked and left it unanswered) and any real deadline (a return date, a fee date, a trial end, an expiring offer), with evidence. Say what happens if it's missed, with the source.
3. **Cost of waiting:** one sentence.
4. **Ranked options:** 2 to 5 real options, ranked 1st, 2nd, 3rd…, no ties. For each: `label` (at most 8 words), `reply` (exactly what Andy replies; keep the row's existing reply codes such as `5A` or `8 read`, or null when no reply is needed), `drop` (true only for the option that removes the item without doing it), `strength` (`strong`: you'd be surprised to be wrong, direct evidence; `moderate`: good evidence, a judgment call; `lean`: slight preference; no other word), `confidence` 0–100 (how strongly you recommend this option), `rationale` (2–4 plain sentences), `evidence` (each a source plus a short verbatim quote), `risk`, `would_change_if`.
5. **Bold call:** one sentence: what you'd do today if you were Andy.

## Sources

- The thread excerpts in `threads/`. For more, T3's database, **read-only only**: `sqlite3 "file:$HOME/.t3/userdata/statev2.sqlite?mode=ro"`. Messages: table `orchestration_v2_projection_messages` (`thread_id`, `role`, `created_at`, `payload_json` with `$.text`); threads: `orchestration_v2_projection_threads` (`thread_id`, `title`). Threads get handed over to successors ("(2)", "(3)" in the title); find predecessors by title or key phrases.
- Files and repos the row or thread names. Web pages for generic policy, terms and fee questions.
- Usage claims: `~/.claude/bin/claude-tier` and `~/.claude/bin/lab-usage codex-tier` print the current meters (read-only).

## Rules for recommending (Andy treats a recommendation he later undoes as a catastrophic failure)

- P1: a usage, cost or speed claim comes from a named measurement, or says "unmeasured".
- P2: never recommend adding a stop, an ask, a manual step or a slow trial for something cheap to undo. If an agent could do it, say so.
- P3: automation acting on an unproven signal starts as a dated trial with a kill test, never at its most committed setting.
- P4: recommend what the job needs, not the most expensive or most cautious option "to be safe".
- Before ranking an option first, ask yourself the likeliest reason Andy would undo it. If it's plausible, fix the option or rank it lower.
- Prefer one reply over an errand when the result is the same.

## Quotes

Every string inside quotation marks must be copied character for character from the source cited in the same field: a local file (give its full path, with a line number), or a T3 message (give the thread title and timestamp). A script searches for each one; a quote it can't find voids that rationale. Paraphrase without quotation marks when you aren't copying.

## Hard limits (breaking one voids your work)

- Read-only everywhere except your own output file in the job folder. Never edit Andy's list (`andy-items.json`), the advice file, any repo, any thread or any setting.
- Never message other T3 threads, answer question cards, settle threads, merge, push, comment on or close PRs, subscribe, buy, cancel, email or contact anyone. No subagents.
- Web searches: generic questions only. Never put Andy's account numbers, addresses, amounts tied to him, names or emails into a search.
- Gemini writes and judges no code, diffs or test results; Grok covers those.

## Output

Write `<job>/r1-<grok|gemini>.json`:

```json
{"model": "...", "round": 1,
 "items": [{"n": 9, "topic": "...",
   "still_needed": "yes|no|partly", "still_needed_evidence": "...",
   "since": "YYYY-MM-DD", "since_evidence": "...",
   "due": "YYYY-MM-DD or null", "due_evidence": "...", "if_missed": "...",
   "cost_of_waiting": "...",
   "options": [{"rank": 1, "label": "...", "reply": "...", "drop": false, "strength": "strong", "confidence": 85,
     "rationale": "...", "evidence": ["..."], "risk": "...", "would_change_if": "..."}],
   "bold_call": "..."}],
 "unverified": ["claims you could not verify"]}
```
