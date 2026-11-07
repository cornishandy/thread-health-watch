# Session handoff

For the next thread that works on the Watch from this project. Keep it short and current.

## Where things stand (7 Nov 2026)

- Watcher v2 and the publication watch run under launchd from the live copies (paths in `scripts/live-paths.txt`). This repo mirrors those files; `scripts/check.sh` must say `ALL CLEAR` before every commit.
- The Watch itself is a long-running T3 thread. From its next handover on, its successor starts in this T3 project with this folder as its workspace. The live files still run from `~/Library/Caches` and `~/.claude`.
- The daily stale-thread pass is a trial until 14 Nov 2026 (rule 12). After that the Watch asks Andy to keep or stop it.
- Two tests in `watch/test_watch_v2.py` fail on the live copy because they expect at least 8 unsettled threads (see README, Tests). Not fixed; it's test data, not daemon behaviour.
- No license yet: Andy hasn't chosen one.

## How to change something

1. Edit the live file and its repo copy identically.
2. Run the three test suites (README, Tests) and `scripts/check.sh`.
3. If a launchd job's file changed, confirm its next run is clean before saying it's done.
4. Commit with the GitHub noreply identity (already set in this repo's git config) and push.

## Never

- Commit live data, the private config, handoff files or anything `.gitignore` lists.
- Put secrets, home-folder paths, the Mac's username, host names, emails, other people's names, or anything about projects under a standing hold into the repo. `scripts/check.sh` reads the owner's forbidden words from `~/.config/thread-health-watch/private-patterns.txt`, outside the repo; keep that file current.
- Rewrite published history except to remove a leak (and then rotate whatever leaked).
- Commit from the Watch's routine wakes. Changes here are deliberate work in their own thread.

## Outside this repo

- T3 scheduled tasks: "Thread health events" (the webhook that wakes the Watch) and "Daily stale-thread pass" (9 AM daily, until the trial ends). Both are bound to the current Watch thread and move with it at each handover.
- `~/Library/Caches/settle-sweep/` (`t3session.py`, `t3ws.cjs`): the guarded settle helper `stale_sweep.py --settle` uses. It belongs to the separate settle sweep.
- `~/Library/Caches/open-threads/`: Andy's list (`andy-items.json`) and the open-threads page. Private; never copied here.
