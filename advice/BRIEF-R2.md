# Needs you advice, round 2: audit the other advisor, then rank one shared option list

Everything in `BRIEF-R1.md` still binds you (hard limits, recommending rules P1–P4, quotes copied exactly). Round 1 is done: `r1-grok.json` and `r1-gemini.json` in the job folder. `check-r1.json` lists every quote the script could not find in its cited source: treat each as false until you find it yourself. `options-canonical.json` is the shared option list Grok built from both files; Andy will see both your rankings side by side.

## Step 1. Audit the other advisor as a hostile reviewer

Go through the other advisor's round-1 file. For every claim that changes a ranking (dates, deadlines, fees, what a thread or file says, what's already done), open the source yourself and return `true`, `false`, `partly` or `unverified`, with your own evidence. Every entry in `check-r1.json` against them gets a verdict too. Don't trust their quotes.

- **Grok** also checks code, diffs and test results either file relies on, re-running what it can.
- **Gemini** also audits each `finding` in `options-canonical.json` (Grok's one- or two-sentence summary of what Andy most needs to know about the item) and returns `finding_verdict` for that item: `true` only if every fact in it holds up in the sources. Anything else and the finding is left out.

## Step 2. Settle disagreements head-on

Where your round-1 top pick differs from theirs, say plainly whether you now agree and why.

## Step 3. Rank every option

For each item, rank every option in `options-canonical.json` (1 = best, no ties). You may add at most one new option per item (ID `<n>x`, with `label`, `reply` and `drop`) if both of you missed something real. For each option: `strength` (`strong`, `moderate` or `lean`, no other word), `confidence` 0–100 (how strongly you recommend this option), a full `rationale` with evidence, and **`plain`**: what Andy actually reads.

**`plain` rules** (a script enforces the first three; breaking one drops your rationale for that option from Andy's view):
- at most 45 words, one or two short sentences;
- no quotation marks of any kind;
- every number in it (dates, amounts, counts) also appears in your own rationale or evidence;
- plain words a busy person reads in 10 seconds: no file paths, commit hashes, function names or jargon;
- bold and direct: say why this option ranks where it does.

## Output

Write `<job>/r2-<grok|gemini>.json`:

```json
{"model": "...", "round": 2,
 "audit": [{"item": 9, "claim": "...", "verdict": "true|false|partly|unverified", "evidence": "..."}],
 "disagreements": [{"item": 9, "position": "agree with X | hold my view", "why": "..."}],
 "items": [{"n": 9, "since": "YYYY-MM-DD", "due": "YYYY-MM-DD or null", "due_note": "short, or null",
   "finding_verdict": "true|false|partly|unverified (Gemini only)", "finding_evidence": "... (Gemini only)",
   "ranking": [{"id": "9a", "rank": 1, "strength": "strong", "confidence": 80, "rationale": "...", "plain": "..."}],
   "new_option": null,
   "changed_from_r1": "what moved and why, or 'nothing'"}]}
```

Reply with at most 300 words: false or partly-true audit results first, then your top pick per item with its strength.
