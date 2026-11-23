# Change ledger

What changed on the RevOps site (this repo) and, more importantly, **why and who asked**. Code history
says what; this file says what the owner wanted, so later changes don't undo a decision by accident.

**Read "Standing decisions" before changing anything. Add an entry for every change you ship.**
How: see `.claude/skills/desk-changes/SKILL.md`.

## Standing decisions

Rules that came from a person and still hold. Change one only when its owner says so, and note who and when.

- **The site is called "RevOps"** (header and sign-in page). Each tab is named for the page it shows
  ("Scrape Desk", "Inbox Health"), and the browser tab reads "RevOps · <tab>". New pages get a new tab,
  not a new site name. (Derek, 2026-11-20)
- **Safety stays with a person.** Dry run by default; never raise daily budgets, add launches or loosen the
  breaker without the owner asking. Rotation defaults to dry; ordering domains is off by default.
  (Derek, 2026-10-13; see CLAUDE.md)
- **The site never posts to Chat or Slack.** Notifications go through the Make "RevOps Daily Connection
  Check", which reads `/api/check`. (Max, 2026-11-09, #13)
- **Rotation reacts to lasting problems, not quiet weeks.** The score uses lifetime numbers; the recent window is information, except bounce spikes and hard-fails. (Max, 2026-11-23, #37)
- **VICTIG brand look** (Poppins/Ubuntu, #333 + #84B637 green). Keep new UI in it. (Max, 2026-11-19, #33)

## Entries

Newest first. One line per change: date, PR, who asked, what changed and why.

- 2026-11-23 · #38 · Max · County map: hovering a county shows a card with the county and state, the date it was last scraped (and how many days ago) and its region, replacing the slow browser tooltip. `/api/coverage` now also returns the last-scraped timestamp. Why: the map only said "scraped N days ago" after a long hover delay and gave no date.
- 2026-11-23 · #37 · Max · Health score and zone now come from lifetime numbers; the last 7-14 days is shown on the hover card for information, and only a bounce spike in it (2%+ caps at Danger, 5%+ retires) can change the zone. Why: victigbackgroundhelp.com (1.6% lifetime reply, 1.2% bounce, warm-up 96) fell to Danger at 53.8 on a quiet week, and a quiet week must not get a good domain swapped out for a bench domain. Standing rule: recent dips inform, they do not trigger rotation.
- 2026-11-23 · #36 · Max · Inbox Health: hovering a domain's score, zone or reason shows a card with the signals behind it (the number behind each, its score, the points it cost) and what is needed to reach the next zone. Why: four domains sat in Danger and nobody could tell why.
- 2026-11-20 · #35 · Derek · Header renamed "Scrape Desk" → "RevOps"; tabs keep naming their page; browser tab
  title follows the open tab; sign-in page says RevOps. Why: the site now covers more than scraping.
  Also started this ledger.

### Before the ledger (from merged PRs, all by Max unless noted)

- 2026-11-19 #33-34 VICTIG brand restyle; layout and motion pass for both pages
- 2026-11-19 #30-32 Health score ignores positive replies and idle warm-up; rotation worst-5-first; Hypertide paging
- 2026-11-16-18 #24-29 Stuck-run watchdog; 10am check reports only broken automation; reply/bounce-by-day graph; canceled domains excluded
- 2026-11-12-13 #15-23 Domain health score and zones; bench tracking + automatic rotation (dry); bench ordering in Hypertide (off by default)
- 2026-11-05-11 #10-14 Inbox Health tab, trend column, reply rate as headline signal, /api/check, no Chat posts
- 2026-11-02-04 #7-9 Old runs load as history; split oversized slices by title group; Clay table row tracking
- 2026-10-19-30 #1-6 Slice packing to 2,200; per-county size learning; county geo ids filled; favicon
- 2026-10-13-14 Derek: first build of the Scrape Desk; CLAUDE.md
