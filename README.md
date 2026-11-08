# Thread health watch

A small set of local scripts and rules that keep an eye on Andy's T3 Code threads. Scripts do the routine checking; a model in one long-running T3 thread (the Watch) wakes only when something needs judgment. The Watch answers what it safely can, keeps one short list of what genuinely needs Andy, and tidies up threads that have gone quiet.

The scripts only read T3's local database; none of them commits, pushes or deploys. The one change they make in T3 is the daily pass settling idle threads, through T3's own guarded command and a five-minute sign-in that's revoked straight afterwards (see step 5 of the setup).

## Status

- [x] Watcher daemon v2 (`watch/watch_v2.py`): live since 26 Oct 2026, wakes the Watch only on incidents a script can't clear
- [x] Watch rules (`rules/guardian-attention.md`, rules 1 to 12) and the v2 add-on (`watch/GUARDIAN-PROMPT.md`)
- [x] Publication watch (`watch/publication_watch.py`): live since 7 Nov 2026, every 30 minutes
- [ ] Needs you advice (`bin/needs-you-advice`, `advice/`): a trial until 21 Nov 2026, then Andy keeps or stops it
- [ ] Daily stale-thread pass (`stale/stale_sweep.py`): a trial until 14 Nov 2026, then Andy decides whether to keep it
- [x] This repo mirrors the live copies, checked by `scripts/check.sh`
- [ ] Known test failures: see [Tests](#tests)
- [ ] No license chosen yet, so all rights are reserved

## How it fits together

```
T3's database (read-only)
   │
   ├─ watch/watch_v2.py ── launchd, always on ── incidents ──┐
   │                                                         ├─► webhook ─► T3 scheduled task "Thread health events"
   ├─ watch/publication_watch.py ── launchd, every 30 min ───┘                 │
   │      (git and Netlify drift → publication-tasks.md)                       ▼
   │                                                               the Watch thread (a model)
   └─ stale/stale_sweep.py ◄── T3 scheduled task, daily 9 AM ────── follows rules/guardian-attention.md
                                                                   runs bin/guardian-wake and bin/open-threads
                                                                   keeps Andy's list (its reply footer)
```

1. **The watcher** scans T3's database on a schedule that follows whether the Mac is in use. It turns what it sees into incidents: a run that failed or stopped without answering, a run stalled for 30 minutes, a red footer nobody is acting on, a waiting question card, new replies in Andy's threads. Most incidents resolve by themselves; only the ones still open after a grace period go out, at most 10 per wake, as metadata only (ids, statuses, times, links; never message text).
2. **The webhook** is a T3 scheduled task. It wakes the Watch thread with the incident payload. Its URL carries a secret and lives only in T3 and in the local `live/config.json`.
3. **The Watch** handles each wake by the rules in `rules/guardian-attention.md`. `bin/guardian-wake check` does the mechanical part (duplicates, waiting cards, thread changes), the model does the judgment, `bin/guardian-wake record` saves a receipt, and `bin/guardian-wake footer` prints Andy's list from `andy-items.json`: each item folds to one line with the day it was first left unanswered and any due date, and opens to the item's text plus a second fold with ranked options from the Grok and Gemini advisors (`andy-items-advice.json`), when there are some.
4. **The publication watch** compares local git repos and Netlify sites with what's published, and wakes the Watch only when its findings change.
5. **The daily stale-thread pass** lists threads idle for 12 hours or more (48 until Andy changed it on 8 Nov 2026), writes an extract of each, and lets the Watch settle the ones with nothing left for Andy, after putting anything unanswered on his list.

The design and its history are in [docs/DESIGN.md](docs/DESIGN.md); the decisions behind it, in Andy's words, are in [docs/DECISIONS.md](docs/DECISIONS.md).

## What's in the repo

| Repo path | What it does | Live copy (the one that runs) | Run by |
|---|---|---|---|
| `watch/watch_v2.py` | the watcher daemon: scan, incidents, delivery | `~/Library/Caches/thread-watch-v2/` | launchd (`launchd/t3-thread-guardian.plist.template`) |
| `watch/triage.py` | the incident rules, pure functions | same folder | `watch_v2.py` |
| `watch/metrics.py` | daily counts of wakes and incidents | same folder | by hand |
| `watch/replay_v2.py` | replays recent T3 history through the rules | same folder | tests, by hand |
| `watch/publication_watch.py`, `watch/publication-launcher.mjs` | git and Netlify drift report | same folder | launchd (`launchd/t3-publication-watch.plist.template`) |
| `watch/GUARDIAN-PROMPT.md` | what the Watch does on a v2 wake | same folder | the Watch thread |
| `watch/config.example.json`, `watch/open-outcomes.example.json` | the shape of the private config and outcome list | same folder | tests |
| `bin/guardian-wake` | the mechanical part of each wake | `~/.claude/bin/` | the Watch thread |
| `bin/open-threads` | lists unsettled top-level threads with their last verdict | `~/.claude/bin/` | `guardian-wake`, the Watch |
| `bin/needs-you-advice` | the advice trial: finds items without current advice, makes job folders, checks quotes and numbers, merges both advisors' rankings, the kill test | `~/.claude/bin/` | the Grok advice thread; the Watch (`dropped`) |
| `advice/LEAD.md`, `advice/BRIEF-R1.md`, `advice/BRIEF-R2.md` | the Grok lead's procedure and the two rounds' briefs | `~/Library/Caches/needs-you-advice/` | the Grok advice thread, Gemini |
| `rules/guardian-attention.md` | the Watch's binding rules | `~/.claude/rules-reference/` | the Watch thread |
| `stale/stale_sweep.py` | the daily stale-thread pass | `~/Library/Caches/stale-thread-triage/` | the Watch's daily scheduled run |
| `docs/DESIGN.md` | design notes for v2 | `~/Library/Caches/thread-watch-v2/` | reading |
| `scripts/check.sh`, `scripts/live-paths.txt`, `scripts/allowlist.txt` | repo-versus-live, allowlist and privacy checks | (repo only) | before every commit |
| `launchd/*.plist.template` | launchd job templates | `~/Library/LaunchAgents/` (own labels) | launchd |

`scripts/live-paths.txt` is the exact file-by-file map.

**Why the repo is a mirror, not the running copy.** The live files stay in `~/Library/Caches` and `~/.claude`, where launchd and the Watch already use them. This repo sits in `~/Documents`, which syncs to iCloud; running daemons from there risks sync conflicts and macOS folder-permission prompts. So the repo is a copy that must stay byte-identical to the live files, and `scripts/check.sh` says when it isn't.

## Setting it up on a Mac

You need macOS, Python 3 (standard library only), Node for the publication launcher, T3 Code with its database at `~/.t3/userdata/statev2.sqlite`, and for the publication watch the `gh` and `netlify` command-line tools, already signed in.

1. Copy each file to its live path from `scripts/live-paths.txt`.
2. Create `~/Library/Caches/thread-watch-v2/live/config.json` from `watch/config.example.json`: the Watch's thread id, the scheduled task id and the webhook URL from T3. Keep that file private (it holds the webhook secret).
3. For the publication watch, create `publication-config.json` beside `publication_watch.py` with your repos and Netlify sites. Projects under a standing hold are simply left out of it.
4. Fill in a label and `__HOME__` in each file in `launchd/`, save them to `~/Library/LaunchAgents/`, and load them with `launchctl bootstrap gui/$(id -u) <file>`.
5. `stale/stale_sweep.py --settle` settles through T3's own guarded command, using a small helper that isn't in this repo (`~/Library/Caches/settle-sweep/t3session.py` and `t3ws.cjs`, which ask T3's CLI for a five-minute sign-in and revoke it afterwards). Without it, the pass can still list and extract, but not settle.

### Helpers this repo expects but doesn't include

These belong to the owner's wider setup, so they're named here rather than copied:

- `~/Library/Caches/settle-sweep/t3session.py` and `t3ws.cjs`: the guarded settle helper (step 5).
- `~/.claude/bin/claude-tier`: prints the usage line that `bin/guardian-wake footer` puts at the end of the Watch's footer.
- `~/.claude/bin/handover`: the living-handoff tool; `bin/guardian-wake record` runs its `check` against the Watch thread, and the Watch's handoff file (a path inside `bin/guardian-wake`) holds its receipts.
- `~/Library/Caches/thread-watch/adaptive-config.json`: the old v1 watcher's config, read by `watch/replay_v2.py` for its exclusion list (history only; v1 no longer runs).

## Tests

```
(cd watch && python3 -B -m unittest test_publication_watch)  # fixtures only
(cd watch && python3 -B -m unittest test_watch_v2)           # needs the owner's T3 database
python3 -B stale/test_stale_sweep.py                          # fixtures, but needs a local T3 install (it reads T3's environment id on import)
```

`test_watch_v2.py` replays real history from the local T3 database, so it only runs on the owner's Mac; `test_stale_sweep.py` builds its own fixture database but still needs T3 installed. On 7 Nov 2026 two of `test_watch_v2.py`'s 50 tests fail on the live copy (`RealStartupScanTests`): they expect at least 8 unsettled top-level threads, and after that morning's clean-up there were 2. In a fresh clone the same two tests can't find the private `shadow/shadow-config.json` and error instead. The other suites pass.

## Changing things

1. Edit the live copy and the repo copy the same way (or edit here and copy to the live path).
2. Run the tests above.
3. Run `scripts/check.sh`. It must end `ALL CLEAR` before any commit or push.
4. If a daemon's file changed, check its next run (`launchctl print gui/$(id -u)/<label>` and its log) before calling it done.

## Privacy

The repo is public, so these never go in it:

- live data: `live/`, `shadow/`, `cutover/`, `challenge/`, state, logs, acks, the outcome list, the publication config and reports, handoff files, the settled log, daily extracts and dumps (all in `.gitignore`);
- secrets: webhook URLs, tokens, sign-ins;
- personal details: home-folder paths, the Mac's username, network host names, email addresses, other people's names, and anything about projects under a standing hold.

`scripts/check.sh` enforces this. Only files named in `scripts/allowlist.txt` may be published, so a new file has to be added there on purpose. The words that must never appear are kept outside the repo, in `~/.config/thread-health-watch/private-patterns.txt` (one extended regular expression per line), so the check never has to spell them out. It also runs [gitleaks](https://github.com/gitleaks/gitleaks) over the tree and the whole history, and fails if any commit was made from an address that isn't a GitHub noreply one.

What does stay, on purpose: Andy's first name and his dated quotes about this tool (his own words in his own repo), opaque T3 thread ids that only mean something inside his T3 install, and home-relative `~/` paths.
