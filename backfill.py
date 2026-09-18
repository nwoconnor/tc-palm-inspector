#!/usr/bin/env python3
"""
Phase 5 -- load DBPR's inspection history for the last HISTORY_YEARS (two) years,
plus the matching yearly archives of the weekly emergency-closure extracts.

Nothing older than the cutoff is ever pulled, by decision. DBPR does publish
files back to 2016; the source list below keeps them documented but they are
skipped unless they overlap the window.

DBPR publishes the history in four eras, each in a different place and format:

  FY2023-24 onward   statewide .xlsx   /hr/inspections/fdinspi_YYYY.xlsx
  FY2021-22, 22-23   statewide .xlsx   /sto/file_download/hr/fdinspi_YYYY.xlsx
  FY2019-20          per-district .xls /sto/file_download/extracts/{d}fdinspi_1920.xls
  FY2016-17 .. 20-21 per-district .csv /sto/file_download/hr/{d}fdinspi_YYYY.csv

The per-district files have no header row; the column layout is the documented
one in every era, and dbpr_fetch.py's mapping copes with all of them. Files
already in the `runs` table are skipped, so this is safe to re-run.

Closures: /hr/inspections/documents/{year}-EOS.zip bundles a year of weekly
extracts. The zip's name does not reliably match the dates inside, so every
spreadsheet in every zip is loaded and the dates on the rows are trusted.

Narratives are NOT fetched here -- run scrape_narratives.py afterwards.

Usage:
  python3 backfill.py                     # everything not yet loaded
  python3 backfill.py --years 1617 1718   # just these fiscal years
  python3 backfill.py --closures-only
  python3 backfill.py --cached            # reuse files already in data/raw
"""

import argparse
import io
import sqlite3
import sys
import zipfile
from datetime import datetime

import dbpr_fetch as ingest

BASE = ingest.BASE
# Oldest first, so an interrupted run leaves a contiguous block of history.
INSPECTION_SOURCES = [
    ("1617", "csv-district", BASE + "/sto/file_download/hr/{d}fdinspi_1617.csv"),
    ("1718", "csv-district", BASE + "/sto/file_download/hr/{d}fdinspi_1718.csv"),
    ("1819", "csv-district", BASE + "/sto/file_download/hr/{d}fdinspi_1819.csv"),
    ("1920", "xls-district", BASE + "/sto/file_download/extracts/{d}fdinspi_1920.xls"),
    ("2021", "csv-district", BASE + "/sto/file_download/hr/{d}fdinspi_2021.csv"),
    ("2122", "xlsx", BASE + "/sto/file_download/hr/fdinspi_2122.xlsx"),
    ("2223", "xlsx", BASE + "/sto/file_download/hr/fdinspi_2223.xlsx"),
    ("2324", "xlsx", BASE + "/hr/inspections/fdinspi_2324.xlsx"),
    ("2425", "xlsx", BASE + "/hr/inspections/fdinspi_2425.xlsx"),
    ("2526", "xlsx", BASE + "/hr/inspections/fdinspi_2526.xlsx"),
]
EOS_ARCHIVE_URL = BASE + "/hr/inspections/documents/{year}-EOS.zip"
EOS_ARCHIVE_YEARS = list(range(2015, 2026))


def label(fy):
    return f"FY20{fy[:2]}-{fy[2:]}"


def fy_end(fy):
    """Last day of the fiscal year, e.g. '2425' -> 2025-06-30."""
    return f"20{fy[2:]}-06-30"


def xls_rows(body):
    """Legacy Excel (BIFF) via xlrd; date cells become datetimes for parse_date."""
    import xlrd
    wb = xlrd.open_workbook(file_contents=body)
    ws = wb.sheet_by_index(0)
    out = []
    for r in range(ws.nrows):
        row = []
        for c in range(ws.ncols):
            cell = ws.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(xlrd.xldate_as_datetime(cell.value, wb.datemode))
            elif cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer():
                row.append(int(cell.value))
            else:
                row.append("" if cell.value is None else cell.value)
        out.append(row)
    return out


def rows_for(kind, body):
    if kind == "xlsx":
        return ingest.xlsx_rows(body)
    if kind == "xls-district":
        return xls_rows(body)
    return ingest.csv_rows(body)


def load_year(conn, run_at, fy, kind, url_tmpl, cached):
    """Download one fiscal year and load its Indian River rows. Returns rows loaded."""
    districts = [ingest.EXPECTED_DISTRICT] + [d for d in range(1, 8) if d != ingest.EXPECTED_DISTRICT]
    if kind == "xlsx":
        districts = [None]
    for d in districts:
        url = url_tmpl.format(d=d) if d else url_tmpl
        fname = url.rsplit("/", 1)[-1]
        body = ingest.download(url, ingest.RAW_DIR / fname, cached=cached,
                               expect_xlsx=(kind == "xlsx"), required=(d in (None, ingest.EXPECTED_DISTRICT)))
        if body is None:
            if d is None or d == ingest.EXPECTED_DISTRICT:
                ingest.log("  not served by DBPR")
            continue
        if kind == "xls-district" and body[:4] != b"\xd0\xcf\x11\xe0":
            ingest.log("  x not a legacy Excel file (soft 404)")
            continue
        try:
            items, counties, _ = ingest.rows_to_inspections(rows_for(kind, body), f"fy{fy}")
        except ValueError as e:
            ingest.log(f"  !! {e}")
            return 0
        cutoff = ingest.history_cutoff()
        dropped = sum(1 for i in items if (i["inspection_date"] or "") < cutoff)
        items = [i for i in items if (i["inspection_date"] or "") >= cutoff]
        if dropped:
            ingest.log(f"  {dropped} rows older than {cutoff} not loaded (two-year window)")
        if not items:
            if d is not None:
                ingest.log(f"  no Indian River rows in district {d} file"
                           + (" -- trying the other districts" if d == ingest.EXPECTED_DISTRICT else ""))
                continue
            ingest.log("  !! no Indian River rows -- check the column mapping above")
            return 0
        dates = sorted(i["inspection_date"] for i in items if i["inspection_date"])
        n = ingest.upsert_inspections(conn, items)
        ingest.record_run(conn, run_at, fname, body, n)
        conn.commit()
        ingest.log(f"  loaded {n:,} rows, {dates[0]} -> {dates[-1]}")
        return n
    return 0


def load_closure_archive(conn, run_at, year, cached):
    url = EOS_ARCHIVE_URL.format(year=year)
    fname = f"{year}-EOS.zip"
    body = ingest.download(url, ingest.RAW_DIR / fname, cached=cached, required=False)
    if body is None or body[:2] != b"PK":
        ingest.log(f"  {fname}: not served")
        return 0
    irc = {r[0] for r in conn.execute("SELECT license_number FROM establishments")}
    total, files, dates = 0, 0, []
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        for member in sorted(z.namelist()):
            if not member.lower().endswith(".xlsx") or member.startswith("__MACOSX"):
                continue
            stem = member.rsplit("/", 1)[-1].replace("EOS_Weekly_Extract_", "EOS_").replace(".xlsx", "")
            data = z.read(member)
            if not ingest.looks_like_xlsx(data):
                continue
            files += 1
            n = ingest.load_closures(conn, data, stem, irc, since=ingest.history_cutoff())
            total += n
            dates.append(stem.replace("EOS_", ""))
    ingest.record_run(conn, run_at, fname, body, total)
    conn.commit()
    span = f"{min(dates)} .. {max(dates)}" if dates else "no weekly files"
    ingest.log(f"  {fname}: {files} weekly files ({span}), {total} Indian River closure rows")
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", help="fiscal years as YYYY, e.g. 1617 2425 (default: all)")
    ap.add_argument("--cached", action="store_true", help="reuse files already in data/raw")
    ap.add_argument("--force", action="store_true", help="reload files already in the runs table")
    ap.add_argument("--closures-only", action="store_true")
    ap.add_argument("--no-closures", action="store_true")
    ap.add_argument("--no-export", action="store_true")
    args = ap.parse_args()

    ingest.RAW_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ingest.DB_PATH)
    conn.executescript(ingest.SCHEMA)
    ingest.ensure_columns(conn)
    run_at = datetime.now().isoformat(timespec="seconds")
    done = {r[0] for r in conn.execute("SELECT DISTINCT file FROM runs")}

    before = conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
    ingest.log(f"Backfill -- {run_at}\n  inspections before: {before:,}")

    if not args.closures_only:
        wanted = set(args.years) if args.years else None
        cutoff = ingest.history_cutoff()
        ingest.log(f"  window: inspections on or after {cutoff} ({ingest.HISTORY_YEARS} years)")
        for fy, kind, url in INSPECTION_SOURCES:
            if wanted and fy not in wanted:
                continue
            if fy_end(fy) < cutoff:
                continue                        # entirely before the window
            fname = url.format(d=ingest.EXPECTED_DISTRICT).rsplit("/", 1)[-1]
            ingest.log(f"\n[{label(fy)}] {fname}")
            if fname in done and not args.force:
                ingest.log("  already loaded -- skipping (use --force to reload)")
                continue
            load_year(conn, run_at, fy, kind, url, args.cached)

    if not args.no_closures:
        ingest.log("\n[Emergency closure archives]")
        for year in EOS_ARCHIVE_YEARS:
            if year < int(ingest.history_cutoff()[:4]) - 1:
                continue                        # archive names run a year behind their contents
            if f"{year}-EOS.zip" in done and not args.force:
                ingest.log(f"  {year}-EOS.zip: already loaded")
                continue
            load_closure_archive(conn, run_at, year, args.cached)

    after = conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
    ingest.log(f"\ninspections after: {after:,} (+{after - before:,})")
    dr = conn.execute("SELECT MIN(inspection_date), MAX(inspection_date) FROM inspections").fetchone()
    ingest.log(f"date range now: {dr[0]} -> {dr[1]}")
    ingest.log("rows by year: " + ", ".join(
        f"{y}={n:,}" for y, n in conn.execute(
            "SELECT substr(inspection_date,1,4), COUNT(*) FROM inspections GROUP BY 1 ORDER BY 1")))
    ingest.log(f"establishments: {conn.execute('SELECT COUNT(*) FROM establishments').fetchone()[0]:,}"
               f"   closures: {conn.execute('SELECT COUNT(*) FROM closures').fetchone()[0]:,}")
    ingest.log("\ndispositions across all years:")
    for disp, n in conn.execute("SELECT disposition, COUNT(*) FROM inspections GROUP BY 1 ORDER BY 2 DESC"):
        flag = ("   <- CLOSED" if ingest.is_closure_disposition(disp)
                else "   <- REOPENED" if ingest.is_reopen_disposition(disp) else "")
        ingest.log(f"  {n:6,}  {disp}{flag}")
    ingest.log("inspection types across all years:")
    for t, n in conn.execute("SELECT inspection_type, COUNT(*) FROM inspections GROUP BY 1 ORDER BY 2 DESC"):
        ingest.log(f"  {n:6,}  {t}")

    if not args.no_export:
        views = {}
        for lbl, year in ((str(ingest.CURRENT_YEAR), ingest.CURRENT_YEAR), ("all", None)):
            tiers = ingest.compute_tiers(conn, year)
            counts = {}
            for t in tiers.values():
                counts[t["tier"]] = counts.get(t["tier"], 0) + 1
            views[lbl] = {"counts": counts, "tiers": tiers}
            ingest.report_tiers(tiers, f"calendar {year}" if year else "all time")
        ingest.export_json(conn, views)
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
