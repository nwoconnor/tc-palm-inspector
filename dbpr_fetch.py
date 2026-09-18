#!/usr/bin/env python3
"""
Phase 1 — Indian River County restaurant inspection data layer.

What it does:
  1. Downloads the DBPR District 4 inspection CSV (current fiscal year), the
     District 4 license CSV, the FY25-26 statewide inspection XLSX (for Jan-Jun 2026),
     and the weekly emergency-closure XLSX files.
  2. Verifies Indian River (county 41) is really in District 4; if not, scans
     all seven district files and tells you which one has it.
  3. Filters to Indian River, loads everything into data/inspections.db (SQLite).
  4. Computes Fame / Shame / Redeemed / Closed tiers for 2026 and all-time.
  5. Probes one inspection detail page to see whether narratives are reachable.
  6. Writes public/data.json and data/phase1_summary.txt.

Run:
  pip install openpyxl
  python3 dbpr_fetch.py            # download everything fresh
  python3 dbpr_fetch.py --cached   # reuse data/raw, skip downloads (fast iteration)

Raw downloads are kept in data/raw/.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
COUNTY_CODE = "41"
COUNTY_NAME = "Indian River"
EXPECTED_DISTRICT = 4
SHAME_HIGH_PRIORITY_MIN = 5
CURRENT_YEAR = 2026

# Only these inspection types can put an establishment on the Wall of Fame.
# "Food-Licensing Inspection" is a pre-opening paperwork check, not a hygiene
# inspection, and a brand-new restaurant that has never been inspected for food
# safety should not be held up as a model. Add "Complaint Full"/"Complaint Partial"
# here if you decide a clean complaint inspection should also count.
FAME_QUALIFYING_TYPES = {"routine - food"}

BASE = "https://www2.myfloridalicense.com"
DISTRICT_INSPECTIONS_URL = BASE + "/sto/file_download/extracts/{d}fdinspi.csv"
DISTRICT_LICENSES_URL = BASE + "/sto/file_download/extracts/hrfood{d}.csv"
STATEWIDE_FY_URL = BASE + "/hr/inspections/fdinspi_{fy}.xlsx"
CLOSURES_URL = BASE + "/hr/inspections/documents/EOS_Weekly_Extract_{ymd}.xlsx"
DETAIL_URL = "https://www.myfloridalicense.com/inspectionDetail.asp?InspVisitID={visit}&id={lic}"
TERMS_URL = "https://www.myfloridalicense.com/insptermsofuse.asp"

# Fiscal years to load in Phase 1. Phase 5 extends this back to "1617".
FISCAL_YEARS = ["2526"]

# Weekly emergency-closure files. The inspection data starts 2025-07-01, so we
# need roughly that many Sundays to line closures up with the inspections we hold.
CLOSURE_WEEKS_TO_TRY = 65

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "raw"
PUBLIC_DIR = Path("public")
DB_PATH = DATA_DIR / "inspections.db"
SUMMARY_PATH = DATA_DIR / "phase1_summary.txt"
JSON_PATH = PUBLIC_DIR / "data.json"                 # the index the dashboard loads first
DETAIL_DIR = PUBLIC_DIR / "establishments"           # one file per licence, loaded on demand

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Positional layout for "Extracts After 1/1/2013" (0-indexed), as documented by DBPR.
# Columns 0-20 are stable across the CSV and XLSX extracts. Columns 21+ shift by one
# in the XLSX files, which carry an extra unnamed column at index 21 -- see
# resolve_columns(), which detects the shift instead of assuming it.
POS = {
    "district": 0, "county_code": 1, "county_name": 2, "license_type": 3,
    "license_number": 4, "name": 5, "address": 6, "city": 7, "zip": 8,
    "inspection_number": 9, "visit_number": 10, "inspection_class": 11,
    "inspection_type": 12, "disposition": 13, "inspection_date": 14,
    "total_violations": 17, "high_priority": 18, "intermediate": 19,
    "basic": 20, "pda": 21, "viol_start": 22, "viol_end": 79,  # inclusive
    "license_id": 80, "visit_id": 81,
}

# Fields that live after the extra XLSX column and therefore move with the offset.
SHIFTING_FIELDS = ("pda", "viol_start", "viol_end", "license_id", "visit_id")

# Header-name hints, matched as substrings against the real DBPR header row.
HEADER_HINTS = {
    "county_code": ["county number", "county code", "countynumber"],
    "county_name": ["county name", "countyname"],
    "license_type": ["license type"],
    "license_number": ["license number", "licensenumber", "license num"],
    "name": ["business name", "dba", "location name"],
    "address": ["location address", "address"],
    "city": ["location city", "city"],
    "zip": ["zip"],
    "inspection_number": ["inspection number"],
    "visit_number": ["visit number"],
    "inspection_class": ["inspection class"],
    "inspection_type": ["inspection type"],
    "disposition": ["disposition"],
    "inspection_date": ["inspection date"],
    "total_violations": ["total violations"],
    "high_priority": ["high priority"],
    "intermediate": ["intermediate"],
    "basic": ["basic"],
    "license_id": ["license id", "licenseid"],
    "visit_id": ["visit id", "inspection visit id", "inspvisitid"],
}

# Column names in the District license file (hrfood{d}.csv). That file is 35 columns
# wide -- the DBPR layout page shows two columns it does not actually contain -- so
# everything here is resolved by header name rather than position.
LICENSE_HINTS = {
    "name": ["business name"],
    "licensee": ["licensee name"],
    "address": ["location street address"],
    "city": ["location city"],
    "zip": ["location zip"],
    "county_code": ["location county code"],
    "license_number": ["license number"],
    "status": ["primary status"],
    "expiry": ["license expiry"],
    "last_inspection": ["last inspection date"],
    "seats": ["number of seats"],
    "risk_level": ["base risk level"],
}

LOG = []
LAST_MODIFIED = {}   # dest filename -> Last-Modified header from the most recent download


def log(msg=""):
    print(msg)
    LOG.append(msg)


def norm(s):
    """Lowercase, collapse whitespace -- for header matching."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


CITY_FIXES = {"Vero Bch": "Vero Beach", "Vero Beach Fl": "Vero Beach", "Sebastian Fl": "Sebastian"}


def clean_city(v):
    """DBPR abbreviates some cities ('VERO BCH'); use the real name so the same
    town does not appear twice in searches and cards."""
    c = str(v or "").strip().title()
    return CITY_FIXES.get(c, c)


def normalize_license(v):
    """
    Reduce a license number to its digits.

    The inspection extracts carry bare numbers ('4100027'); the license file carries
    a type prefix ('SEA4100027', 'MFD...', 'NOS...'); the closure file carries an
    integer. Stripping to digits is what makes the three join.
    """
    d = re.sub(r"\D", "", str(v or ""))
    return d.lstrip("0") or d


# ─────────────────────────────────────────────
# DOWNLOAD HELPERS
# ─────────────────────────────────────────────
def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers


def looks_like_xlsx(body):
    """XLSX files are zip archives. DBPR answers some missing weeks with an HTML
    error page under a 200 status, so the magic bytes are the only reliable test."""
    return bool(body) and body[:2] == b"PK"


def download(url, dest: Path, required=True, cached=False, expect_xlsx=False):
    """Fetch url to dest. With cached=True, reuse dest if it already exists."""
    if cached and dest.exists():
        body = dest.read_bytes()
        if expect_xlsx and not looks_like_xlsx(body):
            dest.unlink(missing_ok=True)
            return None
        log(f"  · {dest.name} (cached)  {len(body)/1024:,.0f} KB")
        return body
    try:
        body, headers = fetch(url)
    except urllib.error.HTTPError as e:
        if required:
            log(f"  x HTTP {e.code} for {url}")
        return None
    except Exception as e:
        if required:
            log(f"  x {type(e).__name__}: {e} for {url}")
        return None
    if expect_xlsx and not looks_like_xlsx(body):
        # Soft 404: an HTML error page served with a 200 status.
        if required:
            log(f"  x not a spreadsheet (soft 404, {len(body)/1024:,.0f} KB of HTML) for {url}")
        return None
    dest.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()[:12]
    lm = headers.get("Last-Modified", "n/a")
    LAST_MODIFIED[dest.name] = headers.get("Last-Modified")
    log(f"  + {dest.name}  {len(body)/1024:,.0f} KB  sha={sha}  Last-Modified: {lm}")
    return body


# ─────────────────────────────────────────────
# PARSING
# ─────────────────────────────────────────────
def parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # leave as-is so it's visible in the summary if something's odd


def to_int(v):
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return 0


def looks_like_header(row):
    """A data row has a numeric county code in col B; a header does not."""
    try:
        return not str(row[1]).strip().isdigit()
    except IndexError:
        return True


def resolve_columns(header, data_width):
    """
    Map field -> column index.

    Named columns are matched by header text. Columns from PDA Status onward are then
    shifted by however much wider the data rows are than the named header, which is
    how the extra unnamed column in the XLSX extracts is absorbed.
    """
    cols = dict(POS)
    how = "positional (no header row)"
    offset = 0
    if header:
        lowered = [norm(h) for h in header]
        matched = 0
        for field, hints in HEADER_HINTS.items():
            for i, h in enumerate(lowered):
                if h and any(hint in h for hint in hints):
                    cols[field] = i
                    matched += 1
                    break
        named_width = max((i for i, h in enumerate(lowered) if h), default=-1) + 1
        offset = max(0, data_width - named_width)
        if offset:
            for f in SHIFTING_FIELDS:
                cols[f] += offset
        how = (f"header ({matched}/{len(HEADER_HINTS)} matched by name), "
               f"named width {named_width}, data width {data_width}, "
               f"trailing-block offset +{offset}")
    return cols, how, offset


def rows_to_inspections(rows, source):
    """Return (list of dicts for county 41 rows, counties seen, notes)."""
    rows = list(rows)
    if not rows:
        return [], {}, "empty file"
    header = rows[0] if looks_like_header(rows[0]) else None
    data = rows[1:] if header else rows
    data_width = max((len(r) for r in data[:2000]), default=0)
    cols, how, offset = resolve_columns(header, data_width)
    log(f"  column mapping for {source}: {how}")

    seen_counties = {}
    out = []
    for r in data:
        if len(r) < 20:
            continue
        cc = str(r[cols["county_code"]]).strip()
        cn = str(r[cols["county_name"]]).strip()
        seen_counties[cc] = cn
        if cc != COUNTY_CODE and COUNTY_NAME.lower() not in cn.lower():
            continue
        viol = {}
        for n, i in enumerate(range(cols["viol_start"], cols["viol_end"] + 1), start=1):
            if i < len(r) and to_int(r[i]):
                viol[str(n)] = to_int(r[i])
        out.append({
            "visit_id": str(r[cols["visit_id"]]).strip() if cols["visit_id"] < len(r) else "",
            "license_id": str(r[cols["license_id"]]).strip() if cols["license_id"] < len(r) else "",
            "license_number": normalize_license(r[cols["license_number"]]),
            "license_display": str(r[cols["license_number"]]).strip(),
            "license_type": str(r[cols["license_type"]]).strip(),
            "name": str(r[cols["name"]]).strip(),
            "address": str(r[cols["address"]]).strip(),
            "city": clean_city(r[cols["city"]]),
            "zip": str(r[cols["zip"]]).strip(),
            "inspection_number": str(r[cols["inspection_number"]]).strip(),
            "visit_number": to_int(r[cols["visit_number"]]),
            "inspection_class": str(r[cols["inspection_class"]]).strip(),
            "inspection_type": str(r[cols["inspection_type"]]).strip(),
            "disposition": str(r[cols["disposition"]]).strip(),
            "inspection_date": parse_date(r[cols["inspection_date"]]),
            "total_violations": to_int(r[cols["total_violations"]]),
            "high_priority": to_int(r[cols["high_priority"]]),
            "intermediate": to_int(r[cols["intermediate"]]),
            "basic": to_int(r[cols["basic"]]),
            "violations": json.dumps(viol),
            "source": source,
        })

    # Sanity check: visit_id must be unique per row, or the trailing block is misaligned.
    ids = [o["visit_id"] for o in out]
    uniq = len(set(ids))
    if out and uniq < len(ids):
        log(f"  !! {source}: visit_id is not unique ({uniq} distinct / {len(ids)} rows) "
            f"-- trailing columns are probably misaligned")
    blank = sum(1 for i in ids if not i)
    if blank:
        log(f"  !! {source}: {blank} rows have a blank visit_id")
    log(f"  counties present in {source}: "
        + ", ".join(f"{k} {v}" for k, v in sorted(seen_counties.items())[:12])
        + (" ..." if len(seen_counties) > 12 else ""))
    log(f"  Indian River rows kept from {source}: {len(out)} "
        f"({uniq} distinct visit_ids)")
    return out, seen_counties, how


def csv_rows(body: bytes):
    text = body.decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def xlsx_rows(body: bytes):
    try:
        import openpyxl
    except ImportError:
        log("  x openpyxl not installed -- run: pip install openpyxl")
        return []
    if not looks_like_xlsx(body):
        log("  x expected an XLSX file but got something else -- skipping")
        return []
    wb = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    out = []
    for row in ws.iter_rows(values_only=True):
        out.append(["" if v is None else v for v in row])
    wb.close()
    return out


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS establishments (
  license_number TEXT PRIMARY KEY,
  license_display TEXT, license_id TEXT, name TEXT, address TEXT, city TEXT, zip TEXT,
  license_type TEXT, seats INTEGER, risk_level TEXT, status TEXT,
  last_inspection_date TEXT, in_license_file INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inspections (
  visit_id TEXT PRIMARY KEY,
  license_id TEXT, license_number TEXT, license_type TEXT,
  name TEXT, address TEXT, city TEXT, zip TEXT,
  inspection_number TEXT, visit_number INTEGER, inspection_class TEXT,
  inspection_type TEXT, disposition TEXT, inspection_date TEXT,
  total_violations INTEGER, high_priority INTEGER, intermediate INTEGER, basic INTEGER,
  violations TEXT, narrative TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS closures (
  license_number TEXT, closed_date TEXT, name TEXT, address TEXT, city TEXT,
  condition TEXT, reopen_date TEXT, source TEXT,
  PRIMARY KEY (license_number, closed_date)
);
CREATE TABLE IF NOT EXISTS runs (
  run_at TEXT, file TEXT, sha256 TEXT, rows_kept INTEGER, last_modified TEXT
);
-- One row per thing we have ever alerted on, so a re-ingest or a re-run never
-- sends the same alert twice. See alerts.py.
CREATE TABLE IF NOT EXISTS alerts (
  kind TEXT,            -- closure | high_priority | hp_jump
  key TEXT,             -- stable identity for the triggering event
  license_number TEXT, name TEXT, inspection_date TEXT,
  detected_at TEXT, payload TEXT, sent_at TEXT, seeded INTEGER DEFAULT 0,
  PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_insp_lic ON inspections(license_number, inspection_date);
CREATE INDEX IF NOT EXISTS idx_insp_narr ON inspections(narrative, inspection_date);
"""


def ensure_columns(conn):
    """Add columns introduced after the first release to an existing database."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "last_modified" not in have:
        conn.execute("ALTER TABLE runs ADD COLUMN last_modified TEXT")


def record_run(conn, run_at, fname, body, rows_kept):
    conn.execute("INSERT INTO runs (run_at, file, sha256, rows_kept, last_modified) "
                 "VALUES (?,?,?,?,?)",
                 (run_at, fname, hashlib.sha256(body).hexdigest(), rows_kept,
                  LAST_MODIFIED.get(fname)))


def upsert_inspections(conn, items):
    n = 0
    for it in items:
        if not it["visit_id"]:
            continue
        conn.execute("""
          INSERT INTO inspections (visit_id, license_id, license_number, license_type, name, address,
            city, zip, inspection_number, visit_number, inspection_class, inspection_type, disposition,
            inspection_date, total_violations, high_priority, intermediate, basic, violations, source)
          VALUES (:visit_id, :license_id, :license_number, :license_type, :name, :address, :city, :zip,
            :inspection_number, :visit_number, :inspection_class, :inspection_type, :disposition,
            :inspection_date, :total_violations, :high_priority, :intermediate, :basic, :violations, :source)
          ON CONFLICT(visit_id) DO UPDATE SET
            disposition=excluded.disposition, total_violations=excluded.total_violations,
            high_priority=excluded.high_priority, intermediate=excluded.intermediate,
            basic=excluded.basic, violations=excluded.violations
        """, it)
        # Only overwrite license_id when the incoming row actually has one; a '0'
        # or empty value must never clobber a good id (narratives depend on it).
        conn.execute("""
          UPDATE inspections SET license_id=:license_id
          WHERE visit_id=:visit_id AND (license_id IS NULL OR license_id IN ('','0'))
        """, it)
        conn.execute("""
          INSERT INTO establishments (license_number, license_display, license_id, name, address,
            city, zip, license_type)
          VALUES (:license_number, :license_display, :license_id, :name, :address, :city, :zip,
            :license_type)
          ON CONFLICT(license_number) DO UPDATE SET
            name=excluded.name, address=excluded.address, city=excluded.city,
            zip=excluded.zip, license_type=excluded.license_type,
            license_display=excluded.license_display,
            license_id=CASE WHEN excluded.license_id NOT IN ('','0')
                            THEN excluded.license_id ELSE establishments.license_id END
        """, it)
        n += 1
    return n


def load_licenses(conn, body):
    """District license file, resolved by header name (the file is 35 columns wide)."""
    rows = csv_rows(body)
    if not rows:
        return 0, "empty file"
    header = rows[0]
    lowered = [norm(h) for h in header]
    cols = {}
    for field, hints in LICENSE_HINTS.items():
        for i, h in enumerate(lowered):
            if h and any(hint in h for hint in hints):
                cols[field] = i
                break
    missing = [f for f in LICENSE_HINTS if f not in cols]
    if missing:
        return 0, f"could not find columns: {missing}"

    n = 0
    for r in rows[1:]:
        if len(r) <= max(cols.values()):
            continue
        if str(r[cols["county_code"]]).strip() != COUNTY_CODE:
            continue
        lic = normalize_license(r[cols["license_number"]])
        if not lic:
            continue
        # A handful of mobile-food licences carry no Business Name; fall back to the licensee.
        est_name = str(r[cols["name"]]).strip() or str(r[cols["licensee"]]).strip()
        conn.execute("""
          INSERT INTO establishments (license_number, license_display, name, address, city, zip,
            seats, risk_level, status, last_inspection_date, in_license_file)
          VALUES (?,?,?,?,?,?,?,?,?,?,1)
          ON CONFLICT(license_number) DO UPDATE SET
            license_display=excluded.license_display,
            seats=excluded.seats, risk_level=excluded.risk_level, status=excluded.status,
            last_inspection_date=excluded.last_inspection_date, in_license_file=1,
            name=COALESCE(NULLIF(establishments.name,''), excluded.name),
            address=COALESCE(NULLIF(establishments.address,''), excluded.address)
        """, (lic, str(r[cols["license_number"]]).strip(), est_name,
              str(r[cols["address"]]).strip(), clean_city(r[cols["city"]]),
              str(r[cols["zip"]]).strip(), to_int(r[cols["seats"]]),
              str(r[cols["risk_level"]]).strip(), str(r[cols["status"]]).strip(),
              parse_date(r[cols["last_inspection"]])))
        n += 1
    return n, f"resolved {len(cols)}/{len(LICENSE_HINTS)} columns by name"


IRC_CITIES = {"vero beach", "vero bch", "sebastian", "fellsmere", "wabasso", "roseland",
              "orchid", "indian river shores", "gifford", "winter beach"}


def load_closures(conn, body, source, irc_licenses):
    rows = xlsx_rows(body)
    if not rows:
        return 0
    header = [norm(h) for h in rows[0]]

    def col(*names):
        for i, h in enumerate(header):
            if any(nm in h for nm in names):
                return i
        return None

    c_lic, c_name = col("license"), col("business name")
    c_addr, c_city, c_cond = col("address"), col("city"), col("condition")
    # 'Date' is the closure date; 'Date of order to vacate' is when it was lifted.
    c_date = col("date of order to close", "date")
    c_reopen = col("date of order to vacate", "reopen", "re-open", "rescind")
    if c_reopen == c_date:
        c_reopen = None
    if c_lic is None or c_date is None:
        return 0

    n = 0
    for r in rows[1:]:
        if len(r) <= max(x for x in (c_lic, c_date) if x is not None):
            continue
        lic = normalize_license(r[c_lic])
        city = norm(r[c_city]) if c_city is not None else ""
        if lic not in irc_licenses and city not in IRC_CITIES:
            continue
        conn.execute("""
          INSERT OR REPLACE INTO closures (license_number, closed_date, name, address, city, condition,
            reopen_date, source) VALUES (?,?,?,?,?,?,?,?)
        """, (lic, parse_date(r[c_date]),
              str(r[c_name]).strip() if c_name is not None else "",
              str(r[c_addr]).strip() if c_addr is not None else "",
              city.title(),
              str(r[c_cond]).strip() if c_cond is not None else "",
              parse_date(r[c_reopen]) if c_reopen is not None else None, source))
        n += 1
    return n


# ─────────────────────────────────────────────
# TIERS
# ─────────────────────────────────────────────
# Tuned to the disposition strings that actually appear in the Indian River data:
#   Inspection Completed - No Further Action / Warning Issued / Call Back - Complied /
#   Call Back - Extension given, pending / Administrative complaint recommended /
#   Emergency order recommended / Emergency Order Callback Complied /
#   Emergency Order Callback Time Extension / Call Back - Admin. complaint recommended
# Note: DBPR never emits a literal "temporarily closed" disposition in this data --
# closures show up as the three "Emergency ..." strings plus the weekly EOS extract.
def is_closure_disposition(d):
    """An inspection that put the establishment under an emergency closure order."""
    d = norm(d)
    return ("emergency" in d and "callback" not in d) or "temporarily closed" in d


def is_emergency_callback(d):
    """A follow-up visit made while an emergency order was in force."""
    d = norm(d)
    return "emergency" in d and "callback" in d


def is_reopen_disposition(d):
    """An emergency callback that cleared the order."""
    return is_emergency_callback(d) and "complied" in norm(d)


def compute_tiers(conn, year=None):
    """Return dict license_number -> tier info. year=None means all-time."""
    params = []
    where = ""
    if year:
        where = "WHERE inspection_date LIKE ?"
        params = [f"{year}-%"]
    rows = conn.execute(f"""
      SELECT license_number, name, city, inspection_date, disposition, total_violations,
             high_priority, inspection_type, visit_id
      FROM inspections {where} ORDER BY license_number, inspection_date, visit_number
    """, params).fetchall()

    closures = {}
    for lic, cd, rd, cond in conn.execute(
            "SELECT license_number, closed_date, reopen_date, condition FROM closures"):
        if not year or (cd or "").startswith(str(year)):
            closures.setdefault(lic, []).append(
                {"closed_date": cd, "reopen_date": rd, "condition": cond})

    by_lic = {}
    for lic, name, city, d, disp, tot, hp, itype, vid in rows:
        by_lic.setdefault(lic, {"name": name, "city": city, "visits": []})["visits"].append(
            {"date": d, "disposition": disp, "total": tot, "high": hp,
             "type": itype, "visit_id": vid})

    tiers = {}
    # Establishments with an EOS closure but no inspection in the window still count.
    for lic in closures:
        by_lic.setdefault(lic, {"name": None, "city": None, "visits": []})

    for lic, info in by_lic.items():
        v = info["visits"]
        cl = closures.get(lic, [])
        closure_visits = [x for x in v if is_closure_disposition(x["disposition"])]
        closed = bool(cl) or bool(closure_visits)

        shame_hits = [x for x in v if x["high"] >= SHAME_HIGH_PRIORITY_MIN]

        # Fame is judged on routine inspections only. Find the most recent one; the
        # establishment qualifies when it was spotless and nothing since then -- of
        # any type -- has found a violation.
        routine_idx = [i for i, x in enumerate(v)
                       if norm(x["type"]) in FAME_QUALIFYING_TYPES]
        fame_clean = False
        if routine_idx:
            last_routine = routine_idx[-1]
            fame_clean = all(x["total"] == 0 for x in v[last_routine:])

        # The streak counts consecutive spotless routine inspections, so a follow-up
        # visit cannot pad it.
        clean_streak = 0
        for i in reversed(routine_idx):
            if v[i]["total"] == 0:
                clean_streak += 1
            else:
                break

        redeemed = False
        redemption_visit = None
        if shame_hits:
            last_shame = max(x["date"] for x in shame_hits)
            later_clean = [x for x in v if x["date"] > last_shame and x["total"] == 0]
            if later_clean:
                redeemed = True
                redemption_visit = later_clean[-1]["visit_id"]

        reopened = (any(c.get("reopen_date") for c in cl)
                    or any(is_reopen_disposition(x["disposition"]) for x in v))

        if closed:
            tier = "closed"
        elif shame_hits:
            tier = "redeemed" if redeemed else "shame"
        elif fame_clean:
            tier = "fame"
        else:
            tier = "neutral"

        tiers[lic] = {
            "name": info["name"], "city": info["city"], "tier": tier,
            "reopened": bool(reopened) if closed else False,
            "clean_streak": clean_streak,
            "shame_count": len(shame_hits),
            "repeat_shame": len(shame_hits) >= 2,
            "redeemed": redeemed,
            "redemption_visit_id": redemption_visit,
            "max_high_priority": max((x["high"] for x in v), default=0),
            "visits": len(v),
            "routine_visits": len(routine_idx),
            "last_visit": v[-1]["date"] if v else None,
            "closures": cl,
        }
    return tiers


def report_tiers(tiers, label):
    counts = {}
    for t in tiers.values():
        counts[t["tier"]] = counts.get(t["tier"], 0) + 1
    log(f"\n-- Tiers: {label} --")
    log(f"  Fame {counts.get('fame',0)} · Shame {counts.get('shame',0)} · "
        f"Redeemed {counts.get('redeemed',0)} · Closed {counts.get('closed',0)} · "
        f"Neutral {counts.get('neutral',0)} · Total establishments {len(tiers)}")
    for tier in ("closed", "shame", "redeemed", "fame"):
        picks = sorted((t for t in tiers.values() if t["tier"] == tier),
                       key=lambda t: (-t["clean_streak"] if tier == "fame" else -t["max_high_priority"],
                                      t["name"] or ""))[:6]
        if picks:
            log(f"  {tier.upper()} sample:")
            for t in picks:
                if tier == "fame":
                    extra = f"streak {t['clean_streak']}"
                else:
                    extra = f"max HP {t['max_high_priority']}, shame hits {t['shame_count']}"
                    if t.get("repeat_shame"):
                        extra += ", REPEAT"
                    if t.get("reopened"):
                        extra += ", reopened"
                    if t["closures"]:
                        extra += f", condition: {t['closures'][0].get('condition') or 'n/a'}"
                log(f"    - {t['name']} ({t['city']}) -- {extra}, last visit {t['last_visit']}")
    return counts


# ─────────────────────────────────────────────
# ADDRESS REUSE
# ─────────────────────────────────────────────
def address_key(addr, city):
    return re.sub(r"[^a-z0-9]", "", norm(addr)) + "|" + re.sub(r"[^a-z0-9]", "", norm(city))


def previous_tenants(conn):
    """license_number -> [other license numbers seen at the same address]."""
    by_addr = {}
    for lic, addr, city in conn.execute(
            "SELECT license_number, address, city FROM establishments WHERE address != ''"):
        by_addr.setdefault(address_key(addr, city), set()).add(lic)
    out = {}
    for _, lics in by_addr.items():
        if len(lics) > 1:
            for l in lics:
                out[l] = sorted(lics - {l})
    return out


# ─────────────────────────────────────────────
# JSON EXPORT
# ─────────────────────────────────────────────
def export_json(conn, views):
    """Write the dashboard's data.

    Two layers, so the page stays fast as history grows: public/data.json is a
    small index (every establishment, its tier in each view, and a one-line
    summary of its latest inspection) and public/establishments/<licence>.json
    holds that establishment's full inspection history with narratives, fetched
    only when someone opens its card. Detail files carry no timestamp, so a
    daily re-export only changes the ones whose data actually changed.
    """
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    prev = previous_tenants(conn)
    ests = []
    written = set()
    for row in conn.execute("""
          SELECT license_number, license_display, license_id, name, address, city, zip,
                 license_type, seats, risk_level, status, last_inspection_date, in_license_file
          FROM establishments ORDER BY name
        """):
        (lic, disp, lid, name, addr, city, zp, ltype, seats, risk, status,
         last_insp, in_lic) = row
        insp = []
        for i in conn.execute("""
              SELECT visit_id, license_id, inspection_date, inspection_type, inspection_class,
                     disposition, total_violations, high_priority, intermediate, basic,
                     violations, narrative
              FROM inspections WHERE license_number=? ORDER BY inspection_date DESC, visit_number DESC
            """, (lic,)):
            insp.append({
                "visit_id": i[0], "date": i[2], "type": i[3], "class": i[4],
                "disposition": i[5], "total": i[6], "high": i[7],
                "intermediate": i[8], "basic": i[9],
                "violations": json.loads(i[10] or "{}"),
                "narrative": i[11],
                "detail_url": (DETAIL_URL.format(visit=i[0], lic=i[1])
                               if i[0] and i[1] and i[1] not in ("0", "") else None),
            })
        cls = [{"closed_date": c[0], "reopen_date": c[1], "condition": c[2],
                "source": c[3]}
               for c in conn.execute("""SELECT closed_date, reopen_date, condition, source
                                        FROM closures WHERE license_number=?
                                        ORDER BY closed_date DESC""", (lic,))]
        for i in insp:
            i["narrative"] = _narrative_for_export(i["narrative"])
        base = {
            "license_number": lic, "license_display": disp, "license_id": lid,
            "name": name, "address": addr, "city": city, "zip": zp,
            "license_type": ltype, "seats": seats, "risk_level": risk, "status": status,
            "last_inspection_date": last_insp, "active_license": bool(in_lic),
            "previously_at_address": prev.get(lic, []),
        }
        detail = dict(base, inspections=insp, closures=cls)
        (DETAIL_DIR / f"{lic}.json").write_text(json.dumps(detail, separators=(",", ":")))
        written.add(f"{lic}.json")
        latest = insp[0] if insp else None
        ests.append(dict(base, inspection_count=len(insp), closure_count=len(cls),
                         latest={k: latest[k] for k in ("date", "type", "disposition", "total",
                                                        "high", "intermediate", "basic")}
                         if latest else None))

    # Drop detail files for licences that no longer exist in the database.
    for stale in DETAIL_DIR.glob("*.json"):
        if stale.name not in written:
            stale.unlink()

    dr = conn.execute("SELECT MIN(inspection_date), MAX(inspection_date) FROM inspections").fetchone()
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "county": COUNTY_NAME,
        "county_code": COUNTY_CODE,
        "date_range": {"first_inspection": dr[0], "last_inspection": dr[1]},
        "shame_threshold_high_priority": SHAME_HIGH_PRIORITY_MIN,
        "views": views,
        "establishments": ests,
    }
    JSON_PATH.write_text(json.dumps(payload, separators=(",", ":")))
    kb = JSON_PATH.stat().st_size / 1024
    dkb = sum(f.stat().st_size for f in DETAIL_DIR.glob("*.json")) / 1024
    log(f"\n  wrote {JSON_PATH} ({kb:,.0f} KB index) + {len(written)} detail files "
        f"in {DETAIL_DIR}/ ({dkb:,.0f} KB)")


def _narrative_for_export(raw):
    """Stored narratives are JSON text; hand the dashboard the parsed object, minus
    bookkeeping it does not need."""
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return {"status": rec.get("status"), "result": rec.get("result"),
            "violations": rec.get("violations", [])}


# ─────────────────────────────────────────────
# DETAIL PAGE PROBE
# ─────────────────────────────────────────────
def parse_detail(html_text):
    """Return (list of {code, text}, page_result_string) from a detail page."""
    import html as htmllib
    x = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_text)
    x = re.sub(r"(?is)<[^>]+>", "\n", x)
    lines = [l.strip() for l in htmllib.unescape(x).split("\n") if l.strip()]
    try:
        start = lines.index("Observation") + 1
    except ValueError:
        return [], None
    code_re = re.compile(r"^\d{1,2}[A-Z]?-\d{2}-\d$")
    items, cur = [], None
    for l in lines[start:]:
        if code_re.match(l):
            cur = {"code": l, "text": []}
            items.append(cur)
        elif cur is not None:
            if l.lower().startswith("licensing portal") or "Â©" in l:
                break
            cur["text"].append(l)
    for it in items:
        it["text"] = " ".join(it["text"]).strip()
    return items, None


def probe_detail(conn):
    row = conn.execute("""
      SELECT visit_id, license_id, name FROM inspections
      WHERE visit_id != '' AND license_id NOT IN ('','0') AND total_violations > 0
      ORDER BY inspection_date DESC LIMIT 1
    """).fetchone()
    if not row:
        log("\n-- Detail probe: no visit with usable IDs found --")
        return
    visit_id, license_id, name = row
    url = DETAIL_URL.format(visit=visit_id, lic=license_id)
    log(f"\n-- Detail page probe --\n  {name}\n  {url}")
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA)]

    def get(u):
        with opener.open(u, timeout=60) as r:
            return r.geturl(), r.read().decode("utf-8", errors="replace")

    try:
        final, html_text = get(url)
        (RAW_DIR / "probe_detail_direct.html").write_text(html_text)
        # A real terms gate would redirect to insptermsofuse.asp or show an Accept
        # control. The phrase "Terms of Use" alone is just the inline footer link.
        gated = ("insptermsofuse" in final.lower()
                 or re.search(r"name=['\"]?(accept|agree)", html_text, re.I) is not None
                 or re.search(r"value=['\"]\s*I\s+Accept", html_text, re.I) is not None)
        items, _ = parse_detail(html_text)
        log(f"  direct GET -> {final}  ({len(html_text):,} chars)")
        log(f"  terms gate blocking access: {gated}")
        log(f"  violations parsed from page: {len(items)}")
        for it in items[:3]:
            log(f"    {it['code']}  {it['text'][:110]}")
        if gated or not items:
            get(TERMS_URL)
            final2, html2 = get(url)
            (RAW_DIR / "probe_detail_after_terms.html").write_text(html2)
            items2, _ = parse_detail(html2)
            log(f"  after visiting terms page -> {final2}  violations parsed: {len(items2)}")
        else:
            log("  => narratives are reachable with a plain GET; no cookie or POST needed.")
    except Exception as e:
        log(f"  x probe failed: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cached", action="store_true",
                    help="reuse files already in data/raw instead of downloading")
    ap.add_argument("--weeks", type=int, default=CLOSURE_WEEKS_TO_TRY,
                    help="how many Sundays of emergency-closure files to try")
    args = ap.parse_args()
    cached = args.cached

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    ensure_columns(conn)
    run_at = datetime.now().isoformat(timespec="seconds")

    log(f"Phase 1 run -- {run_at}" + ("  (cached mode)" if cached else "") + "\n")

    # 1. District inspection file
    log(f"[1] District {EXPECTED_DISTRICT} inspections (current fiscal year)")
    district = EXPECTED_DISTRICT
    body = download(DISTRICT_INSPECTIONS_URL.format(d=district),
                    RAW_DIR / f"{district}fdinspi.csv", cached=cached)
    kept = 0
    if body:
        items, counties, _ = rows_to_inspections(csv_rows(body), f"district{district}")
        kept = upsert_inspections(conn, items)
        record_run(conn, run_at, f"{district}fdinspi.csv", body, kept)
        log(f"  county 41 present in District {district}: {'41' in counties}  "
            f"(districts confirmed by county list above)")
    if kept == 0:
        log(f"  ! No Indian River rows in District {EXPECTED_DISTRICT}. Scanning the others...")
        for d in range(1, 8):
            if d == EXPECTED_DISTRICT:
                continue
            b = download(DISTRICT_INSPECTIONS_URL.format(d=d), RAW_DIR / f"{d}fdinspi.csv",
                         required=False, cached=cached)
            if not b:
                continue
            items, _, _ = rows_to_inspections(csv_rows(b), f"district{d}")
            if items:
                log(f"  -> Indian River is in District {d}. Set EXPECTED_DISTRICT = {d}.")
                district = d
                kept = upsert_inspections(conn, items)
                record_run(conn, run_at, f"{d}fdinspi.csv", b, kept)
                break

    # 2. Statewide fiscal-year files
    for fy in FISCAL_YEARS:
        log(f"\n[2] Statewide FY{fy[:2]}-{fy[2:]} inspections")
        body = download(STATEWIDE_FY_URL.format(fy=fy), RAW_DIR / f"fdinspi_{fy}.xlsx",
                        cached=cached, expect_xlsx=True)
        if body:
            items, _, _ = rows_to_inspections(xlsx_rows(body), f"fy{fy}")
            n = upsert_inspections(conn, items)
            record_run(conn, run_at, f"fdinspi_{fy}.xlsx", body, n)
            log(f"  upserted {n} rows")

    # 3. License master
    log(f"\n[3] District {district} active licenses")
    body = download(DISTRICT_LICENSES_URL.format(d=district), RAW_DIR / f"hrfood{district}.csv",
                    cached=cached)
    if body:
        n, how = load_licenses(conn, body)
        record_run(conn, run_at, f"hrfood{district}.csv", body, n)
        log(f"  {how}")
        log(f"  Indian River active food-service licenses: {n}")

    # 4. Weekly emergency closures
    log(f"\n[4] Weekly emergency closure files (last {args.weeks} Sundays)")
    irc_licenses = {r[0] for r in conn.execute("SELECT license_number FROM establishments")}
    today = date.today()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    found, missing, total_closures = 0, 0, 0
    for i in range(args.weeks):
        ymd = (sunday - timedelta(weeks=i)).strftime("%Y-%m-%d")
        dest = RAW_DIR / f"EOS_{ymd}.xlsx"
        b = download(CLOSURES_URL.format(ymd=ymd), dest, required=False, cached=cached,
                     expect_xlsx=True)
        if b:
            found += 1
            n = load_closures(conn, b, f"EOS_{ymd}", irc_licenses)
            total_closures += n
            record_run(conn, run_at, f"EOS_{ymd}.xlsx", b, n)
        else:
            missing += 1
    log(f"  files found: {found}   not served: {missing}   "
        f"Indian River closure rows loaded: {total_closures}")

    conn.commit()

    # 5. Sanity stats
    log("\n[5] Database")
    for tbl in ("establishments", "inspections", "closures"):
        log(f"  {tbl}: {conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]}")
    log("  rows by source: " + ", ".join(
        f"{s}={n}" for s, n in conn.execute(
            "SELECT source, COUNT(*) FROM inspections GROUP BY 1 ORDER BY 1")))
    dr = conn.execute("SELECT MIN(inspection_date), MAX(inspection_date) FROM inspections").fetchone()
    log(f"  inspection date range: {dr[0]} -> {dr[1]}")
    log("  dispositions seen:")
    for disp, n in conn.execute(
            "SELECT disposition, COUNT(*) FROM inspections GROUP BY disposition ORDER BY 2 DESC"):
        flag = ""
        if is_closure_disposition(disp):
            flag = "   <- treated as CLOSED"
        elif is_reopen_disposition(disp):
            flag = "   <- treated as REOPENED"
        elif is_emergency_callback(disp):
            flag = "   <- emergency callback, order still open"
        log(f"    {n:4d}  {disp}{flag}")
    log("  inspection types seen:")
    for t, n in conn.execute(
            "SELECT inspection_type, COUNT(*) FROM inspections GROUP BY inspection_type ORDER BY 2 DESC"):
        log(f"    {n:4d}  {t}")
    bad_ids = conn.execute(
        "SELECT COUNT(*) FROM inspections WHERE visit_id='' OR license_id IN ('','0')").fetchone()[0]
    log(f"  rows without a usable license_id (blocks narratives): {bad_ids}")
    bad_dates = conn.execute(
        "SELECT COUNT(*) FROM inspections "
        "WHERE inspection_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchone()[0]
    log(f"  rows with an unparsed inspection_date: {bad_dates}")
    matched = conn.execute("SELECT COUNT(*) FROM establishments WHERE in_license_file=1").fetchone()[0]
    log(f"  establishments matched to the active-license file: {matched}"
        f" / {conn.execute('SELECT COUNT(*) FROM establishments').fetchone()[0]}")
    prev = previous_tenants(conn)
    log(f"  addresses with more than one license (previous tenants): {len(prev)}")

    # 6. Tiers
    views = {}
    for label, year in ((str(CURRENT_YEAR), CURRENT_YEAR), ("all", None)):
        tiers = compute_tiers(conn, year)
        counts = report_tiers(tiers, f"calendar {year}" if year else "all time (data loaded so far)")
        views[label] = {"counts": counts, "tiers": tiers}

    # 7. JSON export
    export_json(conn, views)

    # 8. Detail page probe
    probe_detail(conn)

    conn.commit()
    conn.close()
    SUMMARY_PATH.write_text("\n".join(LOG))
    log(f"\nSummary written to {SUMMARY_PATH}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
