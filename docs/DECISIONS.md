# Decisions

Andy's decisions about the Watch, newest first, in his own words where he gave them. Times are US Eastern.

## 7 Nov 2026

- **9:45 AM: fold each Needs you item, with its dates and the advisors' ranked options.** "have them go through each of the Needs You items in the Thread Health Watch, and give me rank choice options based on their recommendations and show strength of their recommendation and rationale. also have the Needs You section items be collapsed to single line that I can click on to expand, and from have the gemini/grok cohort have another collapse/expand below that where I can see their answers. Make the expand/collapse part of regular project, and also put the date it was left unanswered by me, and if there's any due date associated with it." The footer script does the folding, so the Watch never writes the markup; item dates are `since`, `due` and `due_note` in `andy-items.json`, and the advice sits in its own file so the Watch's edits can't erase it.
- **8:34 AM: a GitHub home, public after checks.** "is this in github, if not, create a project, write documentation, commit all changes, and push to github. make public after doing comprehensive security and privacy checks, and have new thread health watch's next handover start from that new project." This repo is the result. It mirrors the live copies rather than replacing them (see the README, "Why the repo is a mirror"). It was pushed private first and made public only after an independent final check, with one clean commit.
- **8:27 AM: a daily stale-thread pass, as a trial to 14 Nov.** He picked "Daily, trial to 14 Nov (Recommended)": "Once a day the Watch does it, skipping pinned, snoozed and 'keep open' threads, and lists each thread it settled. Stops early if you unsettle one or an item was missed." Rule 12 in `rules/guardian-attention.md`; the script is `stale/stale_sweep.py`.
- **8:22 AM: worth-reading items must show.** "I don't see that new section in the current thread health watch (69)." Rule 11: "Worth reading" items are allowed on his list and stay until he says he's read them.
- **7:43 AM: capture what's worth reading, and keep it until addressed.** "analyze useful information that I should read … add those to ongoing Thread Health Watch thread footer table, and make it persist until I address it."
- **Watch publication drift too** (time not recorded). He asked for ongoing monitoring of uncommitted, unpushed and unmerged changes, and of Netlify pages that are behind their local versions. That became `watch/publication_watch.py`, a separate launchd job every 30 minutes, leaving the original daemon unchanged.

## 26 Oct 2026

- **4:00 AM: settling belongs to a script.** "gemini still having trouble closing settled threads. they are building up too fast. I asked for reasonable prompt closing of settled threads." Rule 10: a separate settle sweep does it; the Watch never settles threads itself (rule 12 is the one dated exception).
- **3:52 AM: keep a conservative running list.** "keep running total of open item or useful info to read (being conservative, such as assuming info was useful to me and having to disprove it to discard)." Rule 9.
- **Overnight: a list of what's worth reading.** "I want running list of links to those that have usable, useful, interesting information … that I've not responded to, and any open for later type stuff." Rule 8.
- **2:09 AM: take load off Andy, strictly.** "you gotta help me out with the open input-required threads, I know it's what I asked for, but I'm overwhelmed and it's not sustainable." Then: "why didn't argon handle this appropriately? can you teach it. be strict if you have to." And: "make gemini do this thread efficiently but moreso proficiently." This is where `rules/guardian-attention.md` comes from: the Watch answers waiting question cards itself where the answer is safe, never relays questions, and stays quiet when nothing needs him.
- **About 9:30 AM: go live.** "Go now (Recommended)", after asking at 8:45 AM to be shown the switch-over first. Watcher v2 replaced the old daemon at 9:32 AM.

## 25 Oct 2026

- **9:53 PM: script first, model on anomalies only.** "Yes, do it" to redesigning the watch so a script does the routine checks and a model wakes only when something needs judgment. Design: [DESIGN.md](DESIGN.md).
