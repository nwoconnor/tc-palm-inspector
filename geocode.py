#!/usr/bin/env python3
"""
Give each establishment a map position.

DBPR records carry street addresses but no coordinates. This looks each address up
once with the US Census Bureau's batch geocoder -- free, public, no account or key
-- and stores latitude/longitude on the establishment. An address is only looked up
again if it changes, so after the first run the daily job sends a handful of new
addresses at most.

Misses get two more chances: the Census again with a cleaned-up address (suite
numbers stripped, highways spelled the way its map expects), then OpenStreetMap,
confined to the county and paced at one request a second. Any result outside the
county -- typically a mobile vendor's out-of-county base -- is recorded as
`outside_county` and gets no pin.

The Census geocoder places a point by interpolating along the street's address
range, so a pin can sit a few doors from the real building. Good enough to show
where a place is; the dashboard says so under the map.

Usage:
  python3 geocode.py            # look up anything new or changed
  python3 geocode.py --retry    # also retry addresses that did not match before
"""

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data") / "inspections.db"
BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BATCH_MAX = 9000            # the service accepts up to 10,000 rows per request

# Fallback for addresses the Census map lacks (private clubs, new plazas).
# OpenStreetMap's Nominatim asks for at most one request a second and a
# User-Agent naming the application; results are OpenStreetMap data (ODbL),
# which the dashboard's map already credits.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "tc-palm-inspector/1.0 (restaurant inspection dashboard)"

# Indian River County, with a little margin. A result outside this box is a
# mobile vendor's out-of-county base or a bad match -- no pin beats a wrong pin.
COUNTY_BOX = {"south": 27.54, "north": 27.87, "west": -80.88, "east": -80.30}


def in_county(lat, lon):
    b = COUNTY_BOX
    return lat is not None and b["south"] <= lat <= b["north"] and b["west"] <= lon <= b["east"]


def clean_street(street):
    """Normalise DBPR's free-typed street field into something geocoders recognise."""
    s = (street or "").upper()
    s = s.split(",")[0]                                          # "6170 20TH STREET, FIREHOUSE SUBS"
    s = re.sub(r"\s+(STE|SUITE|UNIT|APT|BLDG|#|VIN)\b.*$", "", s)  # suite / unit / trailer VIN
    s = re.sub(r"\s#\S*.*$", "", s)
    s = re.sub(r"\b(N|S|NORTH|SOUTH)\s+(US\s+)?(HWY|HIGHWAY)\s+1\b", "US HIGHWAY 1", s)
    s = re.sub(r"\bUS\s+(HWY\s+)?1\b(\s+(NORTH|SOUTH|N|S))?", "US HIGHWAY 1", s)
    s = re.sub(r"\b(HWY|HIGHWAY|S|N)\s+A1A\b", "STATE ROAD A1A", s)
    s = re.sub(r"\bPT\b", "POINT", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_city(city):
    c = (city or "").strip().title()
    return {"Vero": "Vero Beach", "Town Of Orchid": "Orchid"}.get(c, c)


def ensure_columns(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(establishments)")}
    for col, typ in (("lat", "REAL"), ("lon", "REAL"), ("geocode_status", "TEXT"),
                     ("geocoded_address", "TEXT"), ("geocoded_at", "TEXT"),
                     ("geocode_source", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE establishments ADD COLUMN {col} {typ}")


def address_line(addr, city, zp):
    return f"{(addr or '').strip()}|{(city or '').strip()}|FL|{(zp or '').strip()[:5]}"


def pending(conn, retry=False):
    """Establishments never looked up, whose address changed, with a hand correction
    not yet applied, or (with retry) that failed."""
    overrides = load_overrides()
    out = []
    for lic, addr, city, zp, status, used in conn.execute(
            "SELECT license_number, address, city, zip, geocode_status, geocoded_address "
            "FROM establishments WHERE COALESCE(address,'') != ''"):
        line = address_line(addr, city, zp)
        if (status is None or used != line or (retry and status != "match")
                or (lic in overrides and status != "match")):
            out.append((lic, addr, city, zp, line))
    return out


def census_batch(rows):
    """rows: [(id, street, city, zip)] -> {id: (status, lat, lon, matched_address)}"""
    buf = io.StringIO()
    w = csv.writer(buf)
    for rid, street, city, zp in rows:
        w.writerow([rid, street, city, "FL", (zp or "")[:5]])
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\n"
            f"Public_AR_Current\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; "
            f"filename=\"addresses.csv\"\r\nContent-Type: text/csv\r\n\r\n"
            f"{buf.getvalue()}\r\n--{boundary}--\r\n").encode("utf-8")
    req = urllib.request.Request(BATCH_URL, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        text = r.read().decode("utf-8", errors="replace")
    out = {}
    for rec in csv.reader(io.StringIO(text)):
        if not rec:
            continue
        rid = rec[0]
        status = (rec[2] if len(rec) > 2 else "").strip().lower()
        if status == "match" and len(rec) > 5 and "," in rec[5]:
            lon, lat = (float(x) for x in rec[5].split(","))
            out[rid] = ("match", lat, lon, rec[4])
        else:
            out[rid] = ("tie" if status == "tie" else "no_match", None, None, None)
    return out


OVERRIDES_PATH = Path("data") / "geocode_overrides.csv"


def load_overrides():
    """license_number -> (lat, lon) from data/geocode_overrides.csv, for the rare
    address no geocoder can place (usually a misspelling in DBPR's record)."""
    out = {}
    if OVERRIDES_PATH.exists():
        with OVERRIDES_PATH.open(newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out[row["license_number"].strip()] = (float(row["lat"]), float(row["lon"]))
                except (KeyError, ValueError):
                    continue
    return out


def nominatim(street, city, zp):
    """One OpenStreetMap lookup, confined to the county. Returns (lat, lon) or None.
    Pass city=None to search on street + ZIP only, for records with the wrong town."""
    b = COUNTY_BOX
    place = f"{street}, {city}, FL" if city else f"{street}, FL"
    q = urllib.parse.urlencode({
        "q": f"{place} {zp[:5] if zp else ''}".strip(), "format": "jsonv2",
        "limit": 1, "countrycodes": "us", "bounded": 1,
        "viewbox": f"{b['west']},{b['north']},{b['east']},{b['south']}"})
    req = urllib.request.Request(f"{NOMINATIM_URL}?{q}", headers={"User-Agent": NOMINATIM_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        hits = json.loads(r.read().decode("utf-8"))
    if hits:
        return float(hits[0]["lat"]), float(hits[0]["lon"])
    return None


def geocode(conn, retry=False, verbose=True, use_osm=True):
    """Three passes: Census as written, Census with a cleaned address, then OpenStreetMap."""
    ensure_columns(conn)
    todo = pending(conn, retry)
    if not todo:
        if verbose:
            print("geocode: nothing new to look up")
        return {"looked_up": 0}
    say = print if verbose else (lambda *a, **k: None)
    say(f"geocode: {len(todo)} address(es) to place")
    found = {}                                     # lic -> (lat, lon, source)

    # Pass 1: as written.
    for i in range(0, len(todo), BATCH_MAX):
        res = census_batch([(lic, a, c, z) for lic, a, c, z, _ in todo[i:i + BATCH_MAX]])
        for lic, (status, lat, lon, _) in res.items():
            if status == "match":
                found[lic] = (lat, lon, "census")
    say(f"  census, as written: {len(found)} placed")

    # Pass 2: cleaned address, for the misses.
    miss = [t for t in todo if t[0] not in found]
    retry_rows = [(lic, clean_street(a), clean_city(c), z) for lic, a, c, z, _ in miss]
    retry_rows = [r for r, (lic, a, c, z, _) in zip(retry_rows, miss) if (r[1], r[2]) != (a.upper(), c)]
    if retry_rows:
        before = len(found)
        res = census_batch(retry_rows)
        for lic, (status, lat, lon, _) in res.items():
            if status == "match":
                found[lic] = (lat, lon, "census_cleaned")
        say(f"  census, cleaned address: +{len(found) - before}")

    # Pass 3: OpenStreetMap, one a second.
    miss = [t for t in todo if t[0] not in found]
    if use_osm and miss:
        before = len(found)
        for lic, a, c, z, _ in miss:
            time.sleep(1.1)
            try:
                hit = nominatim(clean_street(a), clean_city(c), z or "")
                if not hit:
                    time.sleep(1.1)
                    hit = nominatim(clean_street(a), None, z or "")
            except Exception:
                hit = None
            if hit:
                found[lic] = (hit[0], hit[1], "osm")
        say(f"  openstreetmap: +{len(found) - before}")

    overrides = load_overrides()
    for lic, *_ in todo:
        if lic in overrides:
            found[lic] = (*overrides[lic], "override")

    now = datetime.now().isoformat(timespec="seconds")
    stats = {"match": 0, "no_match": 0, "outside_county": 0}
    for lic, a, c, z, line in todo:
        lat, lon, src = found.get(lic, (None, None, None))
        if lat is None:
            status = "no_match"
        elif not in_county(lat, lon):
            status, lat, lon = "outside_county", None, None
        else:
            status = "match"
        stats[status] += 1
        conn.execute("UPDATE establishments SET lat=?, lon=?, geocode_status=?, geocode_source=?, "
                     "geocoded_address=?, geocoded_at=? WHERE license_number=?",
                     (lat, lon, status, src if status == "match" else None, line, now, lic))
    conn.commit()
    say(f"geocode: {stats['match']} placed, {stats['outside_county']} outside the county "
        f"(no pin), {stats['no_match']} not found")
    stats["looked_up"] = len(todo)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--retry", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    geocode(conn, retry=args.retry)
    total, placed = conn.execute(
        "SELECT COUNT(*), SUM(geocode_status='match') FROM establishments").fetchone()
    print(f"coverage: {placed}/{total} establishments have a map position")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
