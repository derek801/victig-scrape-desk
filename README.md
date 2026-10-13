# Scrape Desk

Runs VICTIG's LinkedIn Sales Navigator scraping without anyone pressing buttons in PhantomBuster.

The US is split into 5 regions, one LinkedIn account each. Every region is a queue of **slices**: a few
counties × some company-headcount bands, sized to come in under Sales Nav's 2,500-results-per-search
ceiling. Each weekday, inside the launch window, every account takes its region's next slice: the desk
writes that search into the account's phantom and launches it through the PhantomBuster API. When the
run ends, the desk reads the run log:

- under 2,500 results → slice done
- over 2,500 → slice split in two (fewer bands, then fewer counties) and both halves go next in line
- one county + one band still over 2,500 → marked *capped* (Sales Nav can't show more)

When a region runs out of slices it starts over from the top, once its last cycle is at least
`regap_days` old. Clay keeps pulling each phantom's results the way it always has; nothing changes downstream.

## Safety

- **Starts in dry run.** It plans and shows the next search per account, launches nothing.
- Per-account daily profile budget (default 2,000). Hand-launched runs count toward it, since LinkedIn
  sees one account whoever pressed the button. A second run only happens if it fits under budget × 1.25.
- LinkedIn login problems (expired cookie, disconnected, 2FA) put that account in **Needs attention**
  and stop it. It retries once each morning, so it heals itself once the rep re-syncs.
- Three accounts in trouble at once, or the PhantomBuster API failing 10 times in a row, trips the
  **breaker**: every launch stops until someone presses Reset.
- Slack (`SLACK_ALERT_WEBHOOK`) only hears about problems that lasted an hour, plus one "resolved" line.
- If someone launches a phantom by hand, the desk waits for it to finish and records it.

## Data

- `data/counties.csv`: every US county with 2024 Census population and its LinkedIn location id. 1,476 ids
  come from searches the team already ran; the rest are looked up from LinkedIn's public location
  typeahead by the app itself, two a minute, after checking the lookup against 12 known ids. LinkedIn
  rate-limits that endpoint, so expect the gaps to fill over a day or so. Rebuild: `scripts/build_counties.py`.
- `data/calibration.json`: results per resident for each headcount band, fitted from 275 past runs
  (`scripts/calibrate.py`; typical miss 28%). Each region's factor then self-corrects after every run.
- `data/pb_history.json`: the team's past runs (chart history and the starting coverage map).

## Deploy on Render

1. Push this folder to a private GitHub repo.
2. Render → New → Blueprint → pick the repo. It reads `render.yaml`: one Starter web service (~$7/mo) plus
   a 1 GB disk (~$0.25/mo).
3. Set the secret env vars in the Render dashboard:
   - `PB_API_KEY`: PhantomBuster → VICTIG Workspace → Settings → API keys
   - `PUBLIC_URL`: the service URL Render gives you, e.g. `https://victig-scrape-desk.onrender.com`
   - `DESK_PASSWORD`: shared team password, until Google sign-in is set up
   - `SLACK_ALERT_WEBHOOK`: optional, an incoming webhook for alerts
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`: optional; when set, sign-in is Google, @victig.com only
     (OAuth client type Web, redirect URI `<PUBLIC_URL>/auth/callback`)
   - `BISON_KEY_VICTIG`: optional, for the DoorDash sync job
4. Open the site, check each account's **Next slice** (the "open search" link shows it in Sales Nav), then
   press **Go live**. Or first try one account with **Launch now** (needs dry run off).

## DoorDash sync (optional)

`app/jobs/doordash_sync.py` is the campaign-10 hygiene job that runs every 2 hours on Derek's PC
(scheduled task `doordash-lead-sync`). Switch on "DoorDash campaign-10 sync" in Settings **and** disable
the PC task at the same time, so the two never both run.

## Local

```
python -m uvicorn app.main:app --port 4330
python scripts/simulate.py      # scheduler end-to-end against a fake PhantomBuster
```
