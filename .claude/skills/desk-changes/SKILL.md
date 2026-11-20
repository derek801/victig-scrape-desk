---
name: desk-changes
description: Use before and after ANY change to the RevOps site in this repo (the Scrape Desk and Inbox Health pages) - UI text, layout, tabs, new features, settings, fixes. Read the standing decisions in CHANGES.md first so a change doesn't undo something the owner asked for, then log the change there when it ships.
---

# Changing the RevOps site

The site is live: anything merged to `main` on GitHub (derek801/victig-scrape-desk) auto-deploys on
Render within minutes. Render is on Max's account, so there are no deploy logs here; if a build breaks,
Render keeps the old version up.

## Before

1. `git pull` - Max merges often. Work on a branch, never straight on main.
2. Read `CHANGES.md` → **Standing decisions**. If the request conflicts with one, say so and ask the person
   who asked before going ahead.
3. Read `CLAUDE.md` for the code layout and the safety rules (budgets, breaker, dry run, no Chat posts).

## Doing it

- Run it locally (`python -m uvicorn app.main:app --port 4330`) and look at the result in a browser
  before calling it done. Screenshot visual changes for the requester.
- Runner/slicer changes: `python scripts/simulate.py` must print "all checks passed".
- Open a PR; merge when the requester approves.

## After: log it

Add one line at the top of **Entries** in `CHANGES.md`, in the same PR:

`- YYYY-MM-DD · #PR · <who asked> · <what changed>. Why: <their reason>.`

If the request set a rule that should hold for future work ("the site is called RevOps", "always show X"),
also add or update a bullet under **Standing decisions** with who and when. Write for someone who wasn't
in the conversation: plain words, the reason, no code detail.
