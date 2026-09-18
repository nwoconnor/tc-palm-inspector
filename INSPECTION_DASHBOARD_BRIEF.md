# Indian River County Restaurant Inspection Dashboard — Build Brief

Paste this into Claude Code from the `tc-palm-inspector` repo root. Work through the phases in order; stop and report after Phase 1 before continuing.

## Context

The existing `inspect.py` uses the Anthropic API + web search to summarize the week's inspections and deliver an HTML report via email/Slack on a Monday GitHub Actions cron. That is being replaced by a real data pipeline. **Keep** the repo, workflow file, and email/Slack/HTML delivery code. **Replace** the data layer with the DBPR pipeline below.

A starter script `dbpr_fetch.py` already exists in the repo (Phase 1 data layer). Run it first; fix what breaks; then build on it.

## Data sources (Florida DBPR, Division of Hotels & Restaurants)

All public, no auth. Indian River = county code 41, Region/District 4. Filter every file to county 41.

| Purpose | URL |
|---|---|
| Inspections, current fiscal year (Jul 1 → run date), District 4 | `https://www2.myfloridalicense.com/sto/file_download/extracts/4fdinspi.csv` |
| Active food-service licenses, District 4 | `https://www2.myfloridalicense.com/sto/file_download/extracts/hrfood4.csv` |
| Statewide FY25-26 inspections (covers Jan–Jun 2026) | `https://www2.myfloridalicense.com/hr/inspections/fdinspi_2526.xlsx` |
| Older fiscal years (all-time backfill) | same pattern: `fdinspi_2425.xlsx`, `fdinspi_2324.xlsx`, … back to 2016 |
| Weekly emergency closures (statewide) | `https://www2.myfloridalicense.com/hr/inspections/documents/EOS_Weekly_Extract_YYYY-MM-DD.xlsx` (date = Sunday) |
| Inspection detail page (narratives) | `https://www.myfloridalicense.com/inspectionDetail.asp?InspVisitID={visit_id}&id={license_id}` |
| Column layout reference | `https://www2.myfloridalicense.com/hotels-restaurants/public-records/` |

Inspection CSV layout (0-indexed, "Extracts After 1/1/2013"): district 0, county code 1, county name 2, license type 3, license number 4, business name 5, address 6, city 7, zip 8, inspection number 9, visit number 10, inspection class 11, inspection type 12, disposition 13, inspection date 14, total violations 17, high priority 18, intermediate 19, basic 20, PDA status 21, violation counts for codes 1–58 at 22–79, license ID 80, inspection visit ID 81. The CSV may or may not have a header row — detect it.

DBPR states the extracts refresh **weekly**, but poll **daily** and skip the rebuild when the file hash is unchanged.

## Known risks to verify in Phase 1

1. Confirm District 4 actually contains county 41 (the script scans districts 1–7 if not).
2. The detail pages sit behind a terms-of-use acceptance (`insptermsofuse.asp`). Determine whether a GET, a cookie, or a POST is required. Rate-limit detail fetches (≥1 s between requests, retries with backoff).
3. Real disposition and inspection-type strings must be captured from the data and used to tune the tier logic. Don't assume documented values.

## Storage

SQLite at `data/inspections.db`, committed to the repo. Tables:

- `establishments` — license_number PK, license_id, name, address, city, zip, license_type, seats, risk_level, status, last_inspection_date
- `inspections` — visit_id PK, license_id, license_number, name/address/city/zip, inspection_number, visit_number, inspection_class, inspection_type, disposition, inspection_date, total_violations, high_priority, intermediate, basic, violations (JSON of code→count), narrative (text, filled in Phase 2), source
- `closures` — license_number + closed_date PK, name, address, city, condition, reopen_date, source
- `runs` — run_at, file, sha256, rows_kept

Export `public/data.json` for the dashboard after each change.

## Tier rules

Two views: **2026** (calendar year) and **All time**. Same rules, different date range. Closed overrides Shame; Shame/Redeemed override Fame. A restaurant appears in exactly one tier per view.

- **Closed** — on the DBPR emergency closure list, or any inspection with an emergency-order / temporarily-closed disposition. Tag **Reopened** once a passing emergency-order callback exists. Stays in this tier for the year. Show condition (roaches, sewage, etc.), closure date, reopen date, reinspection narrative.
- **Shame** — any inspection with **≥5 high-priority violations**. Badge **Repeat** if it happened 2+ times. Show violation narratives.
- **Redeemed** — was Shame, then a later inspection had **zero total violations**. Displayed inside the Shame section with a distinct badge and link to the clean report.
- **Fame** — latest inspection had zero total violations and never hit Shame/Closed in the window. Show consecutive clean-inspection streak.
- **Neutral** — everyone else.

Establishment identity: key on license number. Flag "previously at this address" when a different license existed at the same address; do **not** merge histories across owners.

## Alerts

Reuse the existing Slack/email delivery. On each run, diff new inspection rows and fire on:
- any Indian River closure
- any inspection with ≥5 high-priority violations
- any establishment whose high-priority count jumped vs. its prior visit (define a threshold, e.g. +3)

## Phases

**Phase 1 — data layer (do now, then stop and report)**
1. `pip install openpyxl`, run `python dbpr_fetch.py`.
2. Fix any parsing/column issues. Report: rows kept per file, date range, full list of distinct dispositions and inspection types, tier counts, and the detail-page probe result (attach `data/raw/probe_detail_*.html` if the probe failed).
3. Tune `is_closure_disposition` and the Redeemed/Reopened matching to the real strings.

**Phase 2 — narratives**
Scraper that fetches detail pages only for visit_ids with `narrative IS NULL`, newest first, handles the terms gate, stores parsed violation text. Backfill the current fiscal year, then older years; tolerate pages DBPR no longer serves ("narrative unavailable").

**Phase 3 — daily job + alerts**
Replace the Monday cron with daily ~06:00 ET. Hash check → early exit if unchanged. Otherwise ingest, scrape new narratives, compute diffs, send alerts, export data.json, commit. Each July 1, snapshot the prior fiscal-year file to `data/archive/`.

**Phase 4 — dashboard**
Static React on GitHub Pages reading `public/data.json`. Top bar with 2026 / All-time toggle and tier counts ("Fame 41 · Shame 12 · Closed 3"). Sections: Closures, Wall of Shame (with Redeemed), Wall of Fame, Everyone else. Search across all tiers. Restaurant card → full inspection history with narratives.

**Phase 5 — historical backfill**
Load FY files back to 2016 for All-time counts. Narratives newest-first as far as DBPR serves them.

## Working style

Explain what you changed and why in plain language, keep the repo tidy, and ask before deleting anything from the existing tracker.
