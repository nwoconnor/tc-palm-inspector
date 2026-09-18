#!/usr/bin/env python3
"""
Phase 2 -- fetch inspection narratives from DBPR detail pages.

For every inspection we hold, the public detail page carries the inspector's actual
observations. This fetches those pages for rows where `narrative IS NULL`, newest
first, and stores the parsed violation text back on the inspection.

Findings from the Phase 1 probe that shape this script:
  * There is NO terms-of-use gate -- a plain GET returns the full report. The gate
    check is still here as a fallback in case DBPR turns one on later.
  * Pages are served over a slow classic-ASP stack, so requests are paced and retried.

Politeness: at least one second between requests, exponential backoff on failure.

Usage:
  python3 scrape_narratives.py                      # 200 newest unfetched pages
  python3 scrape_narratives.py --all                # keep going until done
  python3 scrape_narratives.py --limit 50 --delay 2
  python3 scrape_narratives.py --retry-unavailable  # another go at pages that failed
  python3 scrape_narratives.py --no-export          # skip rewriting public/data.json
"""

import argparse
import html as htmllib
import json
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path

DB_PATH = Path("data") / "inspections.db"
DETAIL_URL = "https://www.myfloridalicense.com/inspectionDetail.asp?InspVisitID={visit}&id={lic}"
TERMS_URL = "https://www.myfloridalicense.com/insptermsofuse.asp"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MIN_DELAY = 1.0          # the brief's floor: never faster than one request a second
DEFAULT_DELAY = 1.3
DEFAULT_LIMIT = 200
MAX_RETRIES = 3

VIOLATION_CODE = re.compile(r"^\d{1,2}[A-Za-z]?-\d{2}-\d$")
SEVERITY = re.compile(r"^\s*(High Priority|Intermediate|Basic)\s*-\s*", re.I)
# Inspectors' inline markers, e.g. "**Repeat Violation**  **Admin Complaint**"
MARKER = re.compile(r"\*\*\s*([^*]+?)\s*\*\*")
TAG = re.compile(r"(?is)<[^>]+>")
SCRIPT_STYLE = re.compile(r"(?is)<(script|style).*?</\1>")


# ─────────────────────────────────────────────
# FETCHING
# ─────────────────────────────────────────────
class Fetcher:
    """One cookie jar for the whole run, so a terms gate would only need clearing once."""

    def __init__(self, delay=DEFAULT_DELAY, retries=MAX_RETRIES):
        self.delay = max(MIN_DELAY, delay)
        self.retries = retries
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", UA),
                                  ("Accept", "text/html,application/xhtml+xml")]
        self._last = 0.0
        self.terms_cleared = False

    def _pace(self):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url):
        """Return (final_url, html). Raises the last error after exhausting retries."""
        last = None
        for attempt in range(self.retries):
            self._pace()
            try:
                with self.opener.open(url, timeout=60) as r:
                    return r.geturl(), r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                # 404/410 are permanent -- DBPR no longer serves that page.
                if e.code in (404, 410):
                    raise
                last = e
            except Exception as e:
                last = e
            # Exponential backoff with jitter, so a wobble does not become a hammer.
            time.sleep((2 ** attempt) * 1.5 + random.uniform(0, 0.5))
        raise last

    def clear_terms(self):
        """Accept the terms page once, if DBPR ever starts requiring it."""
        if self.terms_cleared:
            return
        try:
            self.get(TERMS_URL)
        except Exception:
            pass
        self.terms_cleared = True


# ─────────────────────────────────────────────
# PARSING
# ─────────────────────────────────────────────
def _clean(fragment):
    """Strip tags from one table cell and normalise whitespace, keeping line breaks."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    s = re.sub(r"(?i)</(p|div|tr)>", "\n", s)
    s = TAG.sub("", s)
    s = htmllib.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in s.split("\n")]
    return "\n".join(l for l in lines if l).strip()


def _split_flags(text):
    """Pull the **Repeat Violation** style markers out into a flag list."""
    flags = [m.strip() for m in MARKER.findall(text)]
    stripped = MARKER.sub("", text)
    stripped = re.sub(r"[ \t]+", " ", stripped)
    stripped = re.sub(r"\n{2,}", "\n", stripped).strip()
    return stripped, flags


def _as_violation(code, body):
    text, flags = _split_flags(body)
    sev = SEVERITY.match(text)
    severity = sev.group(1).title() if sev else None
    if sev:
        text = text[sev.end():].strip()
    return {"code": code, "severity": severity, "text": text, "flags": flags}


def parse_structured(page):
    """Walk the violations table row by row. Precise, and keeps code/text paired."""
    lower = page.lower()
    anchor = lower.find(">observation<")
    if anchor == -1:
        anchor = lower.find("observation")
    if anchor == -1:
        return None
    out = []
    for row in re.split(r"(?i)<tr\b", page[anchor:])[1:]:
        cells = re.split(r"(?i)<td\b", row)[1:]
        if len(cells) < 3:
            continue
        cells = [c.split(">", 1)[1] if ">" in c else c for c in cells]
        code = _clean(cells[0])
        if not VIOLATION_CODE.match(code):
            continue
        out.append(_as_violation(code, _clean(cells[2])))
    return out


def parse_flattened(page):
    """Fallback: flatten the page to text and pair codes with the lines beneath them."""
    x = SCRIPT_STYLE.sub(" ", page)
    x = TAG.sub("\n", x)
    lines = [l.strip() for l in htmllib.unescape(x).split("\n") if l.strip()]
    try:
        start = lines.index("Observation") + 1
    except ValueError:
        return None
    out, cur = [], None
    for l in lines[start:]:
        if VIOLATION_CODE.match(l):
            cur = [l, []]
            out.append(cur)
        elif cur is not None:
            if l.lower().startswith("licensing portal") or l.startswith("©"):
                break
            cur[1].append(l)
    return [_as_violation(c, " ".join(t)) for c, t in out]


def parse_summary(page):
    """Read the Inspection Information block.

    The page prints all six column headings first and only then the six values, so
    the result is the third value after the last heading -- not the line after the
    'Result' heading, which is still a heading.
    """
    x = SCRIPT_STYLE.sub(" ", page)
    x = TAG.sub("\n", x)
    lines = [l.strip() for l in htmllib.unescape(x).split("\n") if l.strip()]
    try:
        head = lines.index("Basic Violations")
    except ValueError:
        return {}
    vals = lines[head + 1:head + 5]
    out = {}
    if len(vals) >= 1:
        out["inspection_type"] = vals[0]
    if len(vals) >= 2 and re.match(r"^\d{2}/\d{2}/\d{4}$", vals[1]):
        out["inspection_date"] = vals[1]
    if len(vals) >= 3:
        out["result"] = vals[2]
    return out


def looks_like_detail_page(page):
    low = page.lower()
    return "inspection information" in low or "licensing portal" in low


def parse_page(page, expected_violations):
    """Return a narrative dict ready to store."""
    violations = parse_structured(page)
    how = "structured"
    if not violations:
        alt = parse_flattened(page)
        if alt:
            violations, how = alt, "flattened"
    if violations is None:
        violations = []

    if not violations and expected_violations > 0:
        # The extract says this inspection had violations but the page shows none --
        # DBPR is no longer serving the detail for it.
        return {"status": "unavailable",
                "reason": "page served but no violation rows found",
                "violations": []}
    rec = {"status": "ok", "parser": how, "violations": violations}
    rec.update(parse_summary(page))
    return rec


# ─────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────
def pending_rows(conn, limit, retry_unavailable=False, year=None):
    cond = ["visit_id != ''", "license_id NOT IN ('','0')"]
    params = []
    if retry_unavailable:
        cond.append("(narrative IS NULL OR json_extract(narrative,'$.status') != 'ok')")
    else:
        cond.append("narrative IS NULL")
    if year:
        cond.append("inspection_date LIKE ?")
        params.append(f"{year}-%")
    q = (f"SELECT visit_id, license_id, name, inspection_date, total_violations "
         f"FROM inspections WHERE {' AND '.join(cond)} "
         f"ORDER BY inspection_date DESC, visit_number DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, params).fetchall()


def scrape(conn, limit=DEFAULT_LIMIT, delay=DEFAULT_DELAY, retry_unavailable=False,
           year=None, verbose=True):
    rows = pending_rows(conn, limit, retry_unavailable, year)
    if not rows:
        print("nothing to fetch -- every inspection already has a narrative")
        return {"fetched": 0}

    total_remaining = conn.execute(
        "SELECT COUNT(*) FROM inspections WHERE narrative IS NULL "
        "AND visit_id != '' AND license_id NOT IN ('','0')").fetchone()[0]
    print(f"{len(rows)} pages to fetch now ({total_remaining} without a narrative in total), "
          f"{max(MIN_DELAY, delay):.1f}s apart -- about "
          f"{len(rows) * max(MIN_DELAY, delay) / 60:.0f} min\n")

    f = Fetcher(delay=delay)
    stats = {"ok": 0, "empty": 0, "unavailable": 0, "failed": 0, "violations": 0}
    started = time.time()

    for n, (vid, lid, name, d, expected) in enumerate(rows, 1):
        url = DETAIL_URL.format(visit=vid, lic=lid)
        try:
            final, page = f.get(url)
            if "insptermsofuse" in final.lower():
                f.clear_terms()
                final, page = f.get(url)
            if not looks_like_detail_page(page):
                rec = {"status": "unavailable", "reason": "not a detail page",
                       "violations": []}
            else:
                rec = parse_page(page, expected)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                rec = {"status": "unavailable", "reason": f"HTTP {e.code}", "violations": []}
            else:
                stats["failed"] += 1
                if verbose:
                    print(f"  [{n}/{len(rows)}] FAILED {name} -- HTTP {e.code} (will retry next run)")
                continue
        except Exception as e:
            # Transient: leave narrative NULL so the next run picks it up again.
            stats["failed"] += 1
            if verbose:
                print(f"  [{n}/{len(rows)}] FAILED {name} -- {type(e).__name__} (will retry next run)")
            continue

        rec["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        rec["url"] = url
        conn.execute("UPDATE inspections SET narrative=? WHERE visit_id=?",
                     (json.dumps(rec), vid))
        conn.commit()

        nv = len(rec["violations"])
        stats["violations"] += nv
        if rec["status"] != "ok":
            stats["unavailable"] += 1
        elif nv == 0:
            stats["empty"] += 1
        else:
            stats["ok"] += 1

        if verbose and (n <= 5 or n % 25 == 0 or rec["status"] != "ok"):
            rate = (time.time() - started) / n
            left = (len(rows) - n) * rate / 60
            tag = rec["status"] if rec["status"] != "ok" else f"{nv} violations"
            print(f"  [{n}/{len(rows)}] {d} {name[:38]:40s} {tag:28s} ~{left:.0f} min left")

    mins = (time.time() - started) / 60
    print(f"\ndone in {mins:.1f} min -- "
          f"{stats['ok']} with violations, {stats['empty']} clean, "
          f"{stats['unavailable']} unavailable, {stats['failed']} failed (retried next run)")
    print(f"{stats['violations']:,} violation narratives stored")
    stats["fetched"] = stats["ok"] + stats["empty"] + stats["unavailable"]
    return stats


def coverage(conn):
    rows = conn.execute("""
      SELECT substr(inspection_date,1,4) AS yr, COUNT(*),
             SUM(CASE WHEN narrative IS NOT NULL THEN 1 ELSE 0 END),
             SUM(CASE WHEN json_extract(narrative,'$.status')='ok' THEN 1 ELSE 0 END)
      FROM inspections GROUP BY yr ORDER BY yr""").fetchall()
    print("\nnarrative coverage by year:")
    for yr, total, have, ok in rows:
        print(f"  {yr}: {have}/{total} fetched, {ok} usable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--all", action="store_true", help="no limit; fetch everything outstanding")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help=f"seconds between requests (floor {MIN_DELAY})")
    ap.add_argument("--year", help="only inspections in this calendar year")
    ap.add_argument("--retry-unavailable", action="store_true",
                    help="also re-try pages previously marked unavailable")
    ap.add_argument("--no-export", action="store_true",
                    help="skip rewriting public/data.json afterwards")
    ap.add_argument("--coverage", action="store_true", help="just report coverage and exit")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    if args.coverage:
        coverage(conn)
        return 0

    scrape(conn, limit=None if args.all else args.limit, delay=args.delay,
           retry_unavailable=args.retry_unavailable, year=args.year)
    coverage(conn)

    if not args.no_export:
        import dbpr_fetch as ingest
        views = {}
        for label, year in ((str(ingest.CURRENT_YEAR), ingest.CURRENT_YEAR), ("all", None)):
            tiers = ingest.compute_tiers(conn, year)
            counts = {}
            for t in tiers.values():
                counts[t["tier"]] = counts.get(t["tier"], 0) + 1
            views[label] = {"counts": counts, "tiers": tiers}
        ingest.export_json(conn, views)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
