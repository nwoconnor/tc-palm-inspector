# Indian River County restaurant inspection tracker

Pulls Florida DBPR's public inspection extracts for county 41 (Indian River),
stores them in SQLite, works out who is on the Wall of Fame and the Wall of Shame,
and sends alerts when something serious happens.

## The pieces

| File | What it does |
|---|---|
| `dbpr_fetch.py` | Downloads the DBPR extracts, filters to county 41, loads `data/inspections.db`, computes tiers, writes `public/data.json`. |
| `scrape_narratives.py` | Fetches the DBPR detail page for each inspection and stores the inspector's actual observations. |
| `daily.py` | The daily job: change check, ingest, narratives, alerts, export. Run by GitHub Actions. |
| `alerts.py` | Works out what is worth telling someone about, and remembers what has already been sent. |
| `delivery.py` | Renders the HTML report and sends it to Slack and/or email. |
| `data/inspections.db` | SQLite store. Committed on purpose. |
| `public/index.html` | The dashboard: one static page, React loaded from a CDN, no build step. Served by GitHub Pages. |
| `public/data.json` | The dashboard's index: every establishment, its tier in each view, latest-inspection summary. ~45 KB compressed. |
| `public/establishments/` | One file per licence with full inspection history and narratives, loaded when a card is opened. |
| `backfill.py` | One-off history load: fiscal years 2016-17 onward and the closure archives back to 2015. |
| `data/archive/` | Snapshot of each outgoing fiscal year's district file, taken around July 1. Committed. |
| `data/raw/` | Raw downloads, ~56 MB. Not committed. |
| `.github/workflows/daily.yml` | Schedules `daily.py` every morning and commits what changed. |
| `.github/workflows/pages.yml` | Publishes `public/` to GitHub Pages whenever it changes. |

## Running it

```bash
pip install openpyxl

python3 dbpr_fetch.py            # download everything and rebuild
python3 dbpr_fetch.py --cached   # reuse data/raw, no downloads (fast)

python3 scrape_narratives.py      # 200 newest pages without a narrative
python3 scrape_narratives.py --all   # keep going until everything is fetched

python3 alerts.py                # show what is pending
python3 delivery.py              # dry run -> data/report_preview.html
```

`delivery.py` sends nothing unless you pass `--send`. Open the preview file in a
browser to see exactly what would go out.

## Before the first real send: seed the alert history

On a fresh database every past closure and bad inspection counts as "new" — about
90 triggers. Seeding marks them all as already handled so the first live run only
reports genuinely new things:

```bash
python3 alerts.py --seed
```

After that, `alerts.py` only ever reports what has appeared since. An alert is
recorded against a stable key, so re-ingesting the same DBPR file, or running the
job twice in one day, cannot send a duplicate. If a send fails, nothing is marked
as sent and it is retried on the next run.

## What fires an alert

| Trigger | Rule |
|---|---|
| Emergency closure | On the weekly DBPR closure list, or an emergency-order disposition on an inspection |
| Serious violations | An inspection with 5 or more high-priority violations |
| Sharply worse | High-priority count up 3 or more versus that establishment's own previous **routine** inspection (call-backs and licensing visits are not a baseline — they would make every following inspection look like a jump) |

One bad inspection can trip more than one trigger; the report shows the
establishment once with every reason listed. Thresholds live at the top of
`alerts.py` (`HIGH_PRIORITY_ALERT_MIN`, `HP_JUMP_MIN`).

## Narratives

`scrape_narratives.py` fetches one public detail page per inspection and stores the
violation text on that inspection. It only touches rows where `narrative IS NULL`,
newest first, so it is safe to stop and restart — it picks up where it left off.

Requests are paced at least a second apart with exponential backoff, per the brief.
A full pass over ~1,250 inspections takes roughly 25 minutes.

```bash
python3 scrape_narratives.py --coverage            # how much is done
python3 scrape_narratives.py --year 2026           # restrict to a year
python3 scrape_narratives.py --retry-unavailable   # another go at pages that failed
```

Each narrative is stored as JSON on the `inspections.narrative` column:

```json
{ "status": "ok", "result": "Met Inspection Standards",
  "violations": [ { "code": "35A-03-4", "severity": "High Priority",
                    "text": "Roach activity present as evidenced by live roaches found. 2 live roaches on top of dish machine.",
                    "flags": ["Warning"] } ] }
```

`status` is `ok` when the page was read (a clean inspection legitimately has an
empty `violations` list) or `unavailable` when DBPR no longer serves that page.
A network failure stores nothing, leaving the row `NULL` so the next run retries it.
Alerts quote the most serious observations, so a closure reports what was actually
found rather than just a violation count.

There are two parsers. The structured one walks the detail table row by row; if DBPR
changes that markup, a flattened-text parser takes over. Both were checked against
the extract's own violation counts and agreed on every page tested.

## The dashboard

`public/index.html` is the whole site. It is plain HTML that loads React and a tiny
templating helper from a CDN, so there is nothing to build or install: GitHub Pages
serves the `public/` folder as-is, and every bot commit updates the live site.

- **2026 / All time** toggle with the tier counts as tiles (click one to jump to it).
- Sections in order: **Closed**, **Wall of Shame** (Redeemed shown inside it with a
  green badge), **Wall of Fame**, **Everyone else**. Search filters all of them.
- A card opens the establishment's full history: closures with their condition and
  reopen date, every inspection with its violation counts, the inspector's
  observations, and a link to the official report. The Redeemed badge's clean
  inspection is outlined.
- Works at phone width and in dark mode; the chosen time period is remembered.

To look at it locally:

```bash
python3 -m http.server 8765 --directory public
```

then open http://localhost:8765. The page loads `data.json` first (about 45 KB
compressed) and one small file per establishment on demand, so it stays quick as
years of history accumulate.

### Publishing on GitHub Pages

`.github/workflows/pages.yml` publishes `public/` to GitHub Pages on every push to
`main` that touches it. The daily job starts it explicitly after its own data
commit, because GitHub never lets a workflow's automatic push trigger another
workflow — without that step the site would silently stop updating. GitHub's
simpler "deploy from a branch" mode cannot serve a `public/` folder (only the root
or `/docs`), which is why this is a workflow.

Once, on the repo: **Settings → Pages → Build and deployment → Source: GitHub
Actions.** The site is then at `https://<user>.github.io/tc-palm-inspector/`; put
that address in the `DASHBOARD_URL` repository variable so alert reports link to it.

## History (Phase 5)

The tracker holds the **most recent two years** and never pulls anything older
(`HISTORY_YEARS` in `dbpr_fetch.py`). DBPR does publish inspection files back to
2016, in four different places and formats, all linked from the public-records
page; `backfill.py` knows all of them but only loads the ones inside the window:

| Years | Format | Where |
|---|---|---|
| FY2023-24 onward | statewide `.xlsx` | `/hr/inspections/fdinspi_YYYY.xlsx` |
| FY2021-22, 2022-23 | statewide `.xlsx` | `/sto/file_download/hr/fdinspi_YYYY.xlsx` |
| FY2019-20 | per-district legacy `.xls` | `/sto/file_download/extracts/4fdinspi_1920.xls` (needs `xlrd`) |
| FY2016-17 to 2020-21 | per-district `.csv`, no header row | `/sto/file_download/hr/4fdinspi_YYYY.csv` |

Two header vocabularies exist — long names (`Inspection Visit ID`) and
abbreviations (`INSP_VST_ID`) — and FY2025-26's header is stale (82 names over 83
columns). The mapper knows both, detects the stale case, and **refuses to load any
file whose visit IDs do not come out unique**, because that is the signature of a
misaligned column and every row would be wrong.

Closures: `/hr/inspections/documents/{year}-EOS.zip` bundles a year of weekly
extracts. The year in the name does not match the dates inside (2025-EOS.zip holds
2024), so every spreadsheet in every zip is loaded and the row dates are trusted.
This is what gives pre-2026 closures their condition text.

```bash
python3 backfill.py              # everything not yet loaded (safe to re-run)
python3 backfill.py --cached     # reuse the ~400 MB already in data/raw
python3 backfill.py --closures-only
python3 scrape_narratives.py --all   # then the narratives, newest first
```

The daily job fetches up to 400 narratives a run, so it works through any backlog
on its own at roughly a year of history every three days.

## The daily job

`daily.py` is what GitHub Actions runs each morning. It is deliberately cheap when
nothing has happened:

1. **Change check.** A HEAD request on the two district files compares Last-Modified
   with the previous run, and the last four Sundays are checked for closure files we
   have not seen. If nothing moved, it stops — no download, no rebuild, no commit.
2. **Download and confirm.** Anything that looks changed is downloaded and its SHA-256
   compared with the last run, so a touched-but-identical file is still skipped.
3. **Ingest, narratives, alerts, export.** New rows go into the database, up to 400
   inspections without narratives get them (about eight minutes), alerts are detected and
   sent, and `public/data.json` is rewritten. Export happens *before* sending, so a
   delivery failure never costs the data.
4. **Fiscal-year archive.** Between June 24 and July 7, the outgoing year's district
   file is copied to `data/archive/` before DBPR resets it on July 1.

The workflow then commits `data/inspections.db`, `public/data.json` and `data/archive/`
only when the job reports a change.

**Schedule.** DBPR's files have refreshed at about 10:48 UTC every day we have looked,
so the job runs at 11:30 UTC (07:30 EDT / 06:30 EST). Running at 06:00 ET sharp would
land before the refresh and always be a day behind. Change the cron in
`.github/workflows/daily.yml` if the refresh time moves. Note that GitHub's cron is
UTC and does not follow daylight saving.

**First run safety.** If the `alerts` table is empty — a brand-new database — the job
seeds every historical alert instead of sending it, exactly like `alerts.py --seed`.
So the first scheduled run is quiet and the second reports only what is new.

To run it by hand: **Actions → Daily inspection check → Run workflow**. Tick *force*
to rebuild even when DBPR's files are unchanged.

```bash
python3 daily.py            # locally: dry run, alerts shown but not sent
python3 daily.py --force    # rebuild regardless of upstream changes
python3 daily.py --send     # the real thing
```

### Setting up on GitHub

1. Create the repository and push this folder to it.
2. **Settings → Secrets and variables → Actions.** Add the secrets from the table below
   (Slack alone is fine to start). Add `DASHBOARD_URL` as a *variable*, not a secret.
3. **Settings → Actions → General → Workflow permissions:** set *Read and write* so the
   job can commit.
4. Run the workflow once by hand from the Actions tab and check the log.

## Configuration

Set these as environment variables locally, or as GitHub Actions secrets for the
daily job (`DASHBOARD_URL` goes in as a repository *variable*). No credentials are
stored in this repo.

| Variable | Needed for | Notes |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Slack | Incoming-webhook URL |
| `SMTP_HOST` | Email | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | Email | Default `587`. Use `465` for implicit SSL. |
| `SMTP_USER` | Email | Omit for an unauthenticated relay |
| `SMTP_PASSWORD` | Email | App password, not your account password |
| `SMTP_STARTTLS` | Email | `1` (default) or `0` |
| `ALERT_FROM` | Email | From address |
| `ALERT_TO` | Email | Comma-separated recipients |
| `DASHBOARD_URL` | Both | Link shown in the report footer |

Whichever channel is configured is used; the other is skipped with a note.
`python3 delivery.py` prints the configuration status without ever printing a secret.

## Data notes worth remembering

- The statewide `.xlsx` extracts carry an **extra unnamed column at index 21**, so
  everything after it sits one place right of the published layout. `dbpr_fetch.py`
  detects this rather than assuming it, and checks that visit IDs come out unique.
- The district licence file is **35 columns, not the 37** the layout page lists.
  Columns are resolved by header name.
- Licence numbers are formatted differently in each file (`4100027`, `SEA4100027`,
  and a bare integer). Everything is normalised to digits before joining.
- The weekly closure extract is only served back to **January 2026**. Older
  closures are visible only as emergency-order dispositions, without condition text.
- DBPR answers some missing weekly files with an HTML error page under a
  **200 OK** status, so downloads are checked for the zip magic bytes.
- Inspection detail pages (narratives) need **no terms acceptance** — a plain GET works.
