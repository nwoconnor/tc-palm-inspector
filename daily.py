#!/usr/bin/env python3
"""
Phase 3 -- the daily job.

Runs once a morning (GitHub Actions) and does, in order:

  1. Cheap change check. HEAD the two district files and compare Last-Modified and
     size with the previous run; look for weekly closure files we have not seen.
     If nothing has changed, stop here -- no download, no rebuild, no commit.
  2. Download whatever did change and confirm with a SHA-256 against the last run.
     (DBPR says the extracts refresh weekly. We poll daily because "weekly" has
     meant Monday, Tuesday and Friday in the files we have seen.)
  3. Ingest the new rows, fetch narratives for the newest inspections that lack
     one (bounded, so the job stays short), detect alerts, send them, and rewrite
     public/data.json.
  4. In the last week of June / first week of July, snapshot the outgoing fiscal
     year's district file to data/archive/ before DBPR resets it.

The statewide fiscal-year .xlsx files are NOT touched here. They are the historical
record and are loaded by dbpr_fetch.py (Phase 1) and the Phase 5 backfill.

Exit codes: 0 = ran (or nothing to do), 1 = a step failed. Writes changed=true|false
to $GITHUB_OUTPUT when running under Actions so the commit step can be conditional.

Usage:
  python3 daily.py                 # dry run for delivery: alerts are shown, not sent
  python3 daily.py --send          # the real thing (needs Slack/SMTP env vars)
  python3 daily.py --force         # rebuild even if nothing changed upstream
  python3 daily.py --scrape-limit 300
"""

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import dbpr_fetch as ingest
import alerts as alerts_mod
import delivery
import scrape_narratives as scraper

ARCHIVE_DIR = Path("data") / "archive"
DEFAULT_SCRAPE_LIMIT = 150          # ~3 minutes at the scraper's pacing
EOS_WEEKS_TO_CHECK = 4              # Sundays to look back for closure files


def say(msg=""):
    print(msg, flush=True)


# ─────────────────────────────────────────────
# CHANGE DETECTION
# ─────────────────────────────────────────────
def head(url):
    """Return (last_modified, content_length) or (None, None) if HEAD is refused."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": ingest.UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.headers.get("Last-Modified"), r.headers.get("Content-Length")
    except Exception as e:
        say(f"  ! HEAD failed for {url.rsplit('/', 1)[-1]}: {type(e).__name__} -- will download")
        return None, None


def last_run(conn, fname):
    """(sha256, last_modified) from the most recent ingest of this file, or (None, None)."""
    row = conn.execute("SELECT sha256, last_modified FROM runs WHERE file=? "
                       "ORDER BY run_at DESC LIMIT 1", (fname,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def seen_files(conn):
    return {r[0] for r in conn.execute("SELECT DISTINCT file FROM runs")}


def recent_sundays(n=EOS_WEEKS_TO_CHECK):
    today = date.today()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    return [(sunday - timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(n)]


# ─────────────────────────────────────────────
# ARCHIVE
# ─────────────────────────────────────────────
def fiscal_year_label(d):
    """'2526' for any date in the fiscal year that runs 2025-07-01 .. 2026-06-30."""
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def maybe_archive(conn, district_csv: Path):
    """Keep a copy of the outgoing fiscal year's district file around the rollover.

    The district extract only ever holds the *current* fiscal year and resets on
    July 1. Any run between June 24 and July 7 whose data still belongs to the
    fiscal year ending that June overwrites the snapshot, so the last one wins.
    Every inspection is also already in the database; this is the raw-file backup.
    """
    today = date.today()
    in_window = (today.month == 6 and today.day >= 24) or (today.month == 7 and today.day <= 7)
    if not in_window or not district_csv.exists():
        return None
    latest = conn.execute("SELECT MAX(inspection_date) FROM inspections "
                          "WHERE source LIKE 'district%'").fetchone()[0]
    if not latest:
        return None
    latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
    ending_fy = fiscal_year_label(date(today.year, 6, 30))
    if fiscal_year_label(latest_d) != ending_fy:
        return None                     # file has already rolled to the new year
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"district{ingest.EXPECTED_DISTRICT}_FY{ending_fy}.csv"
    shutil.copyfile(district_csv, dest)
    say(f"  archived outgoing fiscal year file -> {dest}")
    return dest


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def write_output(changed):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually deliver alerts")
    ap.add_argument("--force", action="store_true", help="rebuild even if upstream is unchanged")
    ap.add_argument("--scrape-limit", type=int, default=DEFAULT_SCRAPE_LIMIT)
    ap.add_argument("--no-scrape", action="store_true")
    args = ap.parse_args()

    ingest.RAW_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ingest.DB_PATH)
    conn.executescript(ingest.SCHEMA)
    ingest.ensure_columns(conn)
    run_at = datetime.now().isoformat(timespec="seconds")
    d = ingest.EXPECTED_DISTRICT
    say(f"Daily run -- {run_at}")

    # ── 1. What changed upstream? ────────────────────────────────────────────
    say("\n[1] Checking DBPR for changes")
    files = {
        f"{d}fdinspi.csv": ingest.DISTRICT_INSPECTIONS_URL.format(d=d),
        f"hrfood{d}.csv": ingest.DISTRICT_LICENSES_URL.format(d=d),
    }
    to_fetch = {}
    for fname, url in files.items():
        prev_sha, prev_lm = last_run(conn, fname)
        lm, size = head(url)
        if lm and prev_lm and lm == prev_lm and not args.force:
            say(f"  = {fname}: unchanged (Last-Modified {lm})")
            continue
        say(f"  ~ {fname}: {'no previous run' if not prev_lm else f'Last-Modified {prev_lm} -> {lm}'}")
        to_fetch[fname] = url

    known = seen_files(conn)
    new_eos = [ymd for ymd in recent_sundays() if f"EOS_{ymd}.xlsx" not in known]
    if new_eos:
        say(f"  ~ closure files to try: {', '.join(new_eos)}")

    if not to_fetch and not new_eos and not args.force:
        say("\nNothing has changed upstream. Done.")
        write_output(False)
        conn.close()
        return 0

    # ── 2. Download and confirm by hash ──────────────────────────────────────
    say("\n[2] Downloading")
    changed = args.force
    bodies = {}
    for fname, url in to_fetch.items():
        body = ingest.download(url, ingest.RAW_DIR / fname)
        if body is None:
            say(f"  ! could not download {fname}; leaving previous data in place")
            continue
        prev_sha, _ = last_run(conn, fname)
        sha = hashlib.sha256(body).hexdigest()
        if sha == prev_sha and not args.force:
            say(f"  = {fname}: Last-Modified moved but content is identical -- skipping")
            # Still record the run so the HEAD check is quiet tomorrow.
            ingest.record_run(conn, run_at, fname, body, 0)
            continue
        bodies[fname] = body
        changed = True

    eos_bodies = {}
    for ymd in new_eos:
        b = ingest.download(ingest.CLOSURES_URL.format(ymd=ymd),
                            ingest.RAW_DIR / f"EOS_{ymd}.xlsx", required=False, expect_xlsx=True)
        if b:
            eos_bodies[ymd] = b
            changed = True
        else:
            say(f"  - EOS_{ymd}.xlsx not published yet")

    if not changed:
        say("\nDownloaded files match what we already had. Done.")
        conn.commit()
        write_output(False)
        conn.close()
        return 0

    # ── 3. Ingest ────────────────────────────────────────────────────────────
    say("\n[3] Ingesting")
    before = conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
    district_name = f"{d}fdinspi.csv"
    if district_name in bodies:
        items, counties, _ = ingest.rows_to_inspections(ingest.csv_rows(bodies[district_name]),
                                                        f"district{d}")
        n = ingest.upsert_inspections(conn, items)
        ingest.record_run(conn, run_at, district_name, bodies[district_name], n)
    lic_name = f"hrfood{d}.csv"
    if lic_name in bodies:
        n, how = ingest.load_licenses(conn, bodies[lic_name])
        ingest.record_run(conn, run_at, lic_name, bodies[lic_name], n)
        say(f"  licences: {n} ({how})")
    if eos_bodies:
        irc = {r[0] for r in conn.execute("SELECT license_number FROM establishments")}
        for ymd, b in eos_bodies.items():
            n = ingest.load_closures(conn, b, f"EOS_{ymd}", irc)
            ingest.record_run(conn, run_at, f"EOS_{ymd}.xlsx", b, n)
            say(f"  EOS_{ymd}: {n} Indian River closure rows")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
    say(f"  inspections: {before} -> {after} (+{after - before})")

    if district_name in bodies:
        maybe_archive(conn, ingest.RAW_DIR / district_name)

    # ── 4. Narratives for the newest inspections ─────────────────────────────
    if not args.no_scrape:
        say(f"\n[4] Narratives (up to {args.scrape_limit} pages)")
        scraper.scrape(conn, limit=args.scrape_limit, verbose=False)

    # ── 5. Alerts ────────────────────────────────────────────────────────────
    say("\n[5] Alerts")
    pending = alerts_mod.detect(conn)
    groups = alerts_mod.group_for_display(pending)
    on_record = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    if on_record == 0 and pending:
        # First ever run on this database: everything in history would fire at once.
        # Seed instead, exactly as `alerts.py --seed` would, and start clean tomorrow.
        alerts_mod.record(conn, pending, sent=False, seeded=True)
        say(f"  first run: seeded {len(pending)} historical alerts without sending")
        pending, groups = [], []
    say(f"  {len(pending)} new triggers across {len(groups)} establishments")

    # ── 6. Export (before sending, so a delivery failure never costs the data) ─
    say("\n[6] Export")
    views = {}
    for label, year in ((str(ingest.CURRENT_YEAR), ingest.CURRENT_YEAR), ("all", None)):
        tiers = ingest.compute_tiers(conn, year)
        counts = {}
        for t in tiers.values():
            counts[t["tier"]] = counts.get(t["tier"], 0) + 1
        views[label] = {"counts": counts, "tiers": tiers}
    ingest.export_json(conn, views)
    conn.commit()

    # ── 7. Deliver ───────────────────────────────────────────────────────────
    if groups:
        cfg = delivery.Config()
        counts = views[str(ingest.CURRENT_YEAR)]["counts"]
        html_body = delivery.render_html(groups, counts, cfg.dashboard_url)
        text_body = delivery.render_text(groups, counts, cfg.dashboard_url)
        subject = delivery.subject_line(groups)
        delivery.PREVIEW_PATH.write_text(html_body)
        say(f"\n[7] Deliver: {subject}")
        if not args.send:
            say("  dry run -- nothing sent (pass --send)")
        else:
            ok_any = False
            if cfg.slack_ready:
                ok, detail = delivery.send_slack(
                    delivery.render_slack(groups, counts, cfg.dashboard_url), cfg)
                say(f"  [{'ok' if ok else 'FAILED'}] slack: {detail}"); ok_any |= ok
            if cfg.email_ready:
                ok, detail = delivery.send_email(subject, html_body, text_body, cfg)
                say(f"  [{'ok' if ok else 'FAILED'}] email: {detail}"); ok_any |= ok
            if not cfg.slack_ready and not cfg.email_ready:
                say("  no channel configured -- alerts stay pending")
            if ok_any:
                alerts_mod.record(conn, pending, sent=True)
                say(f"  recorded {len(pending)} alerts as sent")
    else:
        say("\n[7] Deliver: nothing new to send")

    conn.commit()
    conn.close()
    write_output(True)
    say("\nDone.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        say(f"\nFAILED: {type(e).__name__}: {e}")
        raise
