#!/usr/bin/env python3
"""
Alert detection for the Indian River inspection tracker.

Answers one question: what happened that somebody should be told about?

Three triggers, per the build brief:
  closure        -- any Indian River emergency closure (from the weekly EOS extract
                    or from an emergency-order disposition on an inspection)
  high_priority  -- any inspection with >= 5 high-priority violations
  hp_jump        -- an establishment's high-priority count rose by >= 3 versus its
                    own previous visit

Every alert carries a stable `key`. Once a (kind, key) pair is in the `alerts`
table it never fires again, so re-ingesting the same DBPR file -- or re-running the
job twice in a day -- cannot produce a duplicate.

Usage:
  python3 alerts.py                 # show what is currently pending
  python3 alerts.py --seed          # mark everything pending as already handled
  python3 alerts.py --since 2026-09-01
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data") / "inspections.db"

HIGH_PRIORITY_ALERT_MIN = 5   # >= this many high-priority violations fires an alert
HP_JUMP_MIN = 3               # a rise of >= this many versus the baseline visit fires

# The "sharply worse" trigger measures against the establishment's previous ROUTINE
# inspection, not its previous visit of any kind. A call-back only re-checks items
# already cited, so it structurally records fewer violations and the next routine
# inspection would look like a jump every time. Licensing visits are pre-opening
# checks and are not a baseline either.
BASELINE_TYPES = {"routine - food"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  kind TEXT, key TEXT,
  license_number TEXT, name TEXT, inspection_date TEXT,
  detected_at TEXT, payload TEXT, sent_at TEXT, seeded INTEGER DEFAULT 0,
  PRIMARY KEY (kind, key)
);
"""

DETAIL_URL = "https://www.myfloridalicense.com/inspectionDetail.asp?InspVisitID={visit}&id={lic}"


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def _is_closure_disposition(d):
    d = (d or "").strip().lower()
    return ("emergency" in d and "callback" not in d) or "temporarily closed" in d


SEVERITY_RANK = {"High Priority": 0, "Intermediate": 1, "Basic": 2}
MAX_NOTES = 4


def _notes_from_narrative(narrative, limit=MAX_NOTES):
    """Pull the most serious inspector observations out of a stored narrative.

    Returns [] when the narrative has not been scraped yet, so alerts still work
    before Phase 2 has caught up.
    """
    if not narrative:
        return []
    try:
        rec = json.loads(narrative)
    except ValueError:
        return []
    if rec.get("status") != "ok":
        return []
    vs = sorted(rec.get("violations", []),
                key=lambda v: SEVERITY_RANK.get(v.get("severity"), 3))
    out = []
    for v in vs[:limit]:
        text = " ".join((v.get("text") or "").split())
        if not text:
            continue
        out.append({"code": v.get("code"), "severity": v.get("severity"),
                    "text": text, "flags": v.get("flags") or []})
    return out


def _detail_url(visit_id, license_id):
    if visit_id and license_id and str(license_id) not in ("0", ""):
        return DETAIL_URL.format(visit=visit_id, lic=license_id)
    return None


# ─────────────────────────────────────────────
# DETECTION
# ─────────────────────────────────────────────
def detect(conn, since=None):
    """Return every alert the data currently justifies, newest first.

    `since` (YYYY-MM-DD) limits detection to inspections/closures on or after that
    date. Each alert dict has kind, key, and the fields needed to render it.
    """
    found = []

    # 1. Closures from the weekly EOS extract -- these carry the condition text.
    q = ("SELECT license_number, closed_date, name, address, city, condition, reopen_date "
         "FROM closures")
    params = []
    if since:
        q += " WHERE closed_date >= ?"
        params.append(since)
    for lic, cd, name, addr, city, cond, reopen in conn.execute(q, params):
        found.append({
            "kind": "closure", "key": f"eos:{lic}:{cd}",
            "license_number": lic, "name": name, "inspection_date": cd,
            "payload": {"source": "emergency closure list", "closed_date": cd,
                        "reopen_date": reopen, "condition": cond or "not stated",
                        "address": addr, "city": city},
        })

    # 2. Closures visible only as an emergency-order disposition. The EOS extract is
    #    published weekly, so an inspection can show the order days before the list does.
    q = ("SELECT visit_id, license_id, license_number, name, address, city, inspection_date, "
         "disposition, high_priority, total_violations, narrative "
         "FROM inspections WHERE disposition != ''")
    params = []
    if since:
        q += " AND inspection_date >= ?"
        params.append(since)
    for (vid, lid, lic, name, addr, city, d, disp, hp, tot, narr) in conn.execute(q, params):
        if not _is_closure_disposition(disp):
            continue
        found.append({
            "kind": "closure", "key": f"insp:{vid}",
            "license_number": lic, "name": name, "inspection_date": d,
            "payload": {"source": "inspection disposition", "disposition": disp,
                        "high_priority": hp, "total_violations": tot,
                        "address": addr, "city": city,
                        "detail_url": _detail_url(vid, lid),
                        "notes": _notes_from_narrative(narr)},
        })

    # 3 & 4. Per-inspection triggers. Walk each establishment's visits in order so the
    #        previous routine inspection's high-priority count is available for the
    #        jump test.
    q = ("SELECT visit_id, license_id, license_number, name, address, city, inspection_date, "
         "inspection_type, disposition, high_priority, intermediate, basic, total_violations, "
         "narrative FROM inspections ORDER BY license_number, inspection_date, visit_number")
    rows = conn.execute(q).fetchall()
    baseline = {}   # license_number -> (high_priority, date) of the last routine visit
    for (vid, lid, lic, name, addr, city, d, itype, disp, hp, inter, basic, tot, narr) in rows:
        before = baseline.get(lic)
        if (itype or "").strip().lower() in BASELINE_TYPES:
            baseline[lic] = (hp, d)
        if since and (d or "") < since:
            continue
        common = {
            "license_number": lic, "name": name, "inspection_date": d,
            "payload": {"inspection_type": itype, "disposition": disp,
                        "high_priority": hp, "intermediate": inter, "basic": basic,
                        "total_violations": tot, "address": addr, "city": city,
                        "detail_url": _detail_url(vid, lid),
                        "notes": _notes_from_narrative(narr)},
        }
        if hp >= HIGH_PRIORITY_ALERT_MIN:
            a = {"kind": "high_priority", "key": vid}
            a.update(common)
            found.append(a)
        if before is not None and hp - before[0] >= HP_JUMP_MIN:
            a = {"kind": "hp_jump", "key": vid}
            a.update({**common, "payload": {**common["payload"],
                                            "previous_high_priority": before[0],
                                            "previous_date": before[1],
                                            "jump": hp - before[0]}})
            found.append(a)

    already = {(k, key) for k, key in conn.execute("SELECT kind, key FROM alerts")}
    new = [a for a in found if (a["kind"], a["key"]) not in already]
    new.sort(key=lambda a: (a["inspection_date"] or "", a["name"] or ""), reverse=True)
    return new


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def record(conn, alerts, sent=False, seeded=False):
    """Write alerts to the alerts table so they never fire again."""
    now = datetime.now().isoformat(timespec="seconds")
    for a in alerts:
        conn.execute("""
          INSERT INTO alerts (kind, key, license_number, name, inspection_date,
            detected_at, payload, sent_at, seeded)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(kind, key) DO NOTHING
        """, (a["kind"], a["key"], a["license_number"], a["name"], a["inspection_date"],
              now, json.dumps(a["payload"]), now if sent else None, 1 if seeded else 0))
    conn.commit()
    return len(alerts)


# ─────────────────────────────────────────────
# GROUPING FOR DISPLAY
# ─────────────────────────────────────────────
KIND_LABEL = {
    "closure": "Emergency closure",
    "high_priority": f"{HIGH_PRIORITY_ALERT_MIN}+ high-priority violations",
    "hp_jump": f"High-priority count up {HP_JUMP_MIN}+ since last routine inspection",
}
KIND_ORDER = ["closure", "high_priority", "hp_jump"]


def group_for_display(alerts):
    """Collapse alerts to one entry per establishment+date, keeping every reason.

    A single bad inspection can trip both the high-priority and the jump trigger;
    readers want one line about the restaurant, not two.
    """
    groups = {}
    for a in alerts:
        gk = (a["license_number"], a["inspection_date"])
        g = groups.setdefault(gk, {
            "license_number": a["license_number"], "name": a["name"],
            "date": a["inspection_date"], "kinds": [], "payload": {},
        })
        if a["kind"] not in g["kinds"]:
            g["kinds"].append(a["kind"])
        # Closure payloads are the most informative, so let them win the merge.
        if a["kind"] == "closure":
            g["payload"] = {**g["payload"], **a["payload"]}
        else:
            g["payload"] = {**a["payload"], **g["payload"]}
    out = list(groups.values())
    for g in out:
        g["kinds"].sort(key=lambda k: KIND_ORDER.index(k))
        g["severity"] = KIND_ORDER.index(g["kinds"][0])
    out.sort(key=lambda g: (g["severity"], -(g["payload"].get("high_priority") or 0),
                            g["date"] or ""))
    return out


def tier_counts(conn, year=None):
    """Light-weight counts for the report header, read from public/data.json if present."""
    p = Path("public") / "data.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        key = str(year) if year else "all"
        return d.get("views", {}).get(key, {}).get("counts", {})
    except (ValueError, OSError):
        return {}


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--since", help="only consider inspections/closures on or after YYYY-MM-DD")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently pending as handled without sending")
    args = ap.parse_args()

    conn = connect(args.db)
    pending = detect(conn, since=args.since)
    groups = group_for_display(pending)

    total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    print(f"alerts already on record: {total}")
    print(f"pending now: {len(pending)} triggers across {len(groups)} establishments\n")

    for g in groups:
        reasons = ", ".join(KIND_LABEL[k] for k in g["kinds"])
        p = g["payload"]
        print(f"  {g['date']}  {g['name']} ({p.get('city','')})")
        print(f"      {reasons}")
        if "condition" in p:
            print(f"      condition: {p['condition']}  reopened: {p.get('reopen_date') or 'not yet'}")
        if p.get("high_priority") is not None:
            extra = ""
            if "previous_high_priority" in p:
                extra = f"  (was {p['previous_high_priority']} on {p.get('previous_date')}, +{p['jump']})"
            print(f"      high priority {p['high_priority']}, total {p.get('total_violations')}{extra}")
        for nt in p.get("notes", []):
            flags = f"  [{', '.join(nt['flags'])}]" if nt["flags"] else ""
            print(f"      · {nt['severity']}: {nt['text'][:110]}{flags}")
        if p.get("detail_url"):
            print(f"      {p['detail_url']}")
        print()

    if args.seed:
        n = record(conn, pending, sent=False, seeded=True)
        print(f"seeded {n} alerts as already handled -- the next run starts from a clean slate")
    conn.close()


if __name__ == "__main__":
    main()
