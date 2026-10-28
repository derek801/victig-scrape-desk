# Scrape Desk: context for Claude

VICTIG's sales team builds HR prospect lists by scraping LinkedIn Sales Navigator with PhantomBuster
("Sales Navigator Search Export" phantoms, one per rep's LinkedIn account). Results flow PhantomBuster →
Clay (enrichment, email finding, verification) → EmailBison campaign. This app replaces the manual part:
picking the next search and pressing Launch in PhantomBuster every day. Read README.md first for the
user-facing picture; this file is the working knowledge behind it.

## Layout

- `app/main.py`: FastAPI routes, auth (Google @victig.com, or a shared `DESK_PASSWORD`, or none locally), `/api/state` for the page
- `app/runner.py`: scheduler thread, ticks every 60s: poll our runs → record hand-launched runs → health/breaker → launch due accounts
- `app/slicer.py`: builds each region's slice queue, records results, splits overflow, restarts finished regions
- `app/searchurl.py`: builds/parses Sales Nav people-search URLs
- `app/pb.py`: PhantomBuster API v2 client + run-log parser
- `app/geo.py`: fills missing LinkedIn county location ids, slowly
- `app/alerts.py`: Slack alerts with grace period / reminders / "resolved"
- `app/seed.py`: first-boot data (regions, accounts, counties, calibration, past runs)
- `app/jobs/doordash*.py`: optional Bison campaign-10 hygiene job (off by default)
- `app/static/index.html`: the whole UI, vanilla JS + d3 map
- `scripts/simulate.py`: end-to-end test against a fake PhantomBuster. Run it after any change to runner/slicer.

## Things that are true and not obvious

- **Sales Nav shows at most 2,500 results per search.** That's why searches are sliced by county and
  company-headcount band. Headcount band ids: B 1-10, C 11-50, D 51-200, E 201-500, F 501-1,000,
  G 1,001-5,000, H 5,001-10,000, I 10,001+. Default is D-I (51+).
- **URL encoding matters.** Filter text is URL-encoded inside the query, then the query is encoded again with
  parentheses left bare (`searchurl.build`). This matches what the reps' phantoms already receive.
  REGION values need LinkedIn geo ids; titles work as free text.
- **County geo ids:** `data/counties.csv`. 1,476 came from searches the team actually ran (trusted). The
  rest come from LinkedIn's public jobs typeahead
  `https://www.linkedin.com/jobs-guest/api/typeaheadHits?typeaheadType=GEO&query=<County>, <State>` (no login).
  It returns 429 quickly when hit in parallel and stays blocked for a long time. Keep it slow and serial
  (`geo.py` does 2/min with a 1h hold on any 429).
- **Launching:** `POST /api/v2/agents/launch {id, argument}` with the agent's *saved* argument plus a new
  `salesNavigatorSearchUrl`. The saved argument stays untouched, so Clay's PhantomBuster source keeps
  pulling results normally. The saved argument contains the rep's LinkedIn session cookie: **never log or
  print it.**
- **Run logs are the source of truth** (`containers/fetch-output`): `Total results count: N` = search size,
  the last `(N this launch)` = profiles scraped, `[error]` lines carry failures. Login-type errors are
  listed in `pb.LOGIN_PATTERNS`. "Your organization has reached the limit of 100,000 leads" is PhantomBuster's
  lead storage being full: the CSV still saves, so it's ignored for success/failure.
- **Hand-launched runs** (a rep pressing Launch in PhantomBuster) are imported every 15 minutes and count
  toward that account's daily profile budget, since LinkedIn sees one account no matter who launched.
- **Slice size estimates** come from `data/calibration.json` (results per resident by band, fitted on past
  runs, typical miss ~28%), times a per-region factor that moves 30% toward reality after each run. Each run also records real-size ratios per
  county and band (`county_ratio`), which override the national figure next time. Before launch `slicer.next_ready`
  re-estimates the slice and splits it if it now looks over 2,500.
- **PhantomBuster org:** "VICTIG Workspace", id `3152587389729387`. The API key is per workspace.
- Agent ids per rep are in `seed.ACCOUNTS`.

## Rules for working on this

- **The desk launches scrapes on real people's LinkedIn accounts.** Keep it in dry run unless the owner
  says otherwise. Never raise daily budgets, add launches, or loosen the breaker without asking.
  Overuse can get a rep's LinkedIn account restricted.
- A failing step must block its own account and trip the global breaker, never loop. Alert once per
  problem, never per item.
- Secrets only in env vars (Render dashboard or `.env.local`, which is gitignored). Nothing secret in the repo.
- After changing runner/slicer: `python scripts/simulate.py` must print "all checks passed".

## Run

```
pip install -r requirements.txt
python -m uvicorn app.main:app --port 4330     # http://localhost:4330, no auth locally
python scripts/simulate.py
```
Rebuilding data (needs numpy/scipy): `scripts/build_counties.py [--no-lookup]`, then `scripts/calibrate.py`.
Delete `data/local/` to reseed the local database.
