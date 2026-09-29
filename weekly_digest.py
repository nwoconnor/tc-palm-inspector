#!/usr/bin/env python3
"""
Build the weekly digest for the private "Indian River Weekly" dashboard.

What counts as new: inspections and emergency closures that ARRIVED since the last
confirmed digest -- tracked by id in a local state file, not by inspection date,
because DBPR often posts an inspection several days after it happens.

Two steps, so a failed upload never loses anything:
  python3 weekly_digest.py build     # writes data/weekly_out/{digest,status}.json,
                                     # prints a one-line JSON summary
  python3 weekly_digest.py confirm   # after a successful upload: remember what was shown

The database is opened read-only; nothing in the repository changes. The state
file and output folder are git-ignored. With no state file yet, the first digest
covers the last fourteen days.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import alerts as alerts_mod
import dbpr_fetch as ingest

DB_PATH = Path("data") / "inspections.db"
STATE_PATH = Path("data") / "weekly_state.json"
OUT_DIR = Path("data") / "weekly_out"
def dashboard_url():
    """The public site's address, from DASHBOARD_URL or the repo's GitHub remote,
    so no account name is written into the repository."""
    import os, re, subprocess
    if os.environ.get("DASHBOARD_URL"):
        return os.environ["DASHBOARD_URL"]
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except Exception:
        remote = ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote)
    return f"https://{m.group(1)}.github.io/{m.group(2)}/" if m else ""


DASHBOARD = dashboard_url()
KIND_ORDER = ["closure", "high_priority", "hp_jump"]
MAX_NOTES = 3
FIRST_RUN_DAYS = 14


def ro_connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def current_keys(conn):
    visits = {r[0] for r in conn.execute("SELECT visit_id FROM inspections")}
    closures = {f"{lic}:{d}" for lic, d in conn.execute("SELECT license_number, closed_date FROM closures")}
    return visits, closures


def load_state(conn):
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text())
        return set(s["visit_ids"]), set(s["closure_keys"]), s.get("confirmed_at")
    # First run: treat everything older than two weeks as already seen.
    week_ago = (date.today() - timedelta(days=FIRST_RUN_DAYS)).isoformat()
    visits = {r[0] for r in conn.execute(
        "SELECT visit_id FROM inspections WHERE inspection_date < ?", (week_ago,))}
    closures = {f"{lic}:{d}" for lic, d in conn.execute(
        "SELECT license_number, closed_date FROM closures WHERE closed_date < ?", (week_ago,))}
    return visits, closures, week_ago + "T00:00:00"


def item_from_group(g, names):
    p = g["payload"]
    lic = g["license_number"]
    est_name, est_city, est_addr = names.get(lic, (g["name"], p.get("city"), p.get("address")))
    return {
        "license_number": lic,
        "name": est_name or g["name"],
        "city": est_city or p.get("city") or "",
        "address": est_addr or p.get("address") or "",
        "date": g["date"],
        "kinds": g["kinds"],
        "high": p.get("high_priority"),
        "intermediate": p.get("intermediate"),
        "basic": p.get("basic"),
        "total": p.get("total_violations"),
        "previous_high": p.get("previous_high_priority"),
        "previous_date": p.get("previous_date"),
        "jump": p.get("jump"),
        "disposition": p.get("disposition"),
        "inspection_type": p.get("inspection_type"),
        "condition": p.get("condition"),
        "reopen_date": p.get("reopen_date"),
        "notes": (p.get("notes") or [])[:MAX_NOTES],
        "state_report_url": p.get("detail_url"),
        "dashboard_url": f"{DASHBOARD}#/r/{lic}",
    }


def build():
    conn = ro_connect()
    seen_v, seen_c, since = load_state(conn)
    now_v, now_c = current_keys(conn)
    new_v, new_c = now_v - seen_v, now_c - seen_c

    new_rows = []
    if new_v:
        marks = ",".join("?" * len(new_v))
        new_rows = conn.execute(
            f"SELECT visit_id, total_violations, high_priority FROM inspections "
            f"WHERE visit_id IN ({marks})", list(new_v)).fetchall()

    # Alert triggers, kept only where the triggering record is new.
    triggers = []
    for a in alerts_mod.detect(conn):
        k = a["key"]
        if k.startswith("eos:"):
            _, lic, d = k.split(":", 2)
            if f"{lic}:{d}" in new_c:
                triggers.append(a)
        else:
            vid = k[5:] if k.startswith("insp:") else k
            if vid in new_v:
                triggers.append(a)
    groups = alerts_mod.group_for_display(triggers)
    names = {lic: (n, c, a) for lic, n, c, a in conn.execute(
        "SELECT license_number, name, city, address FROM establishments")}
    items = [item_from_group(g, names) for g in groups]

    tiers = ingest.compute_tiers(conn, ingest.CURRENT_YEAR)
    standings = {}
    for t in tiers.values():
        standings[t["tier"]] = standings.get(t["tier"], 0) + 1
    data_through = conn.execute("SELECT MAX(inspection_date) FROM inspections").fetchone()[0]
    conn.close()

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    # Date and time, so a second run on the same day adds a digest instead of
    # overwriting the first. The page shows only the date part.
    digest_id = datetime.now().strftime("%Y-%m-%d-%H%M")
    has_new = bool(new_rows or new_c)
    digest = {
        "digest_id": digest_id,
        "created_at": now,
        "covers_from": (since or "")[:10],
        "data_through": data_through,
        "new_inspections": len(new_rows),
        "new_clean": sum(1 for _, tot, _ in new_rows if tot == 0),
        "new_with_high_priority": sum(1 for _, _, hp in new_rows if hp and hp > 0),
        "counts": {k: sum(1 for i in items if k in i["kinds"]) for k in KIND_ORDER},
        "items": items,
        "standings_year": ingest.CURRENT_YEAR,
        "standings": standings,
        "dashboard_url": DASHBOARD,
    }
    status = {
        "last_checked": now,
        "last_result": "new" if has_new else "nothing_new",
        "data_through": data_through,
        "schedule": "Mondays at 9:00 am",
        "dashboard_url": DASHBOARD,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "digest.json").write_text(json.dumps(digest, indent=1))
    (OUT_DIR / "status.json").write_text(json.dumps(status, indent=1))
    (OUT_DIR / "pending_state.json").write_text(json.dumps(
        {"confirmed_at": now, "visit_ids": sorted(now_v), "closure_keys": sorted(now_c)}))
    print(json.dumps({
        "status": "new" if has_new else "nothing_new",
        "digest_id": digest_id,
        "new_inspections": len(new_rows),
        "items": len(items),
        "counts": digest["counts"],
        "digest_file": str((OUT_DIR / "digest.json").resolve()),
        "status_file": str((OUT_DIR / "status.json").resolve()),
    }))


def confirm():
    pending = OUT_DIR / "pending_state.json"
    if not pending.exists():
        print("nothing to confirm -- run build first")
        return 1
    STATE_PATH.write_text(pending.read_text())
    pending.unlink()
    print("confirmed: the next digest starts from here")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()
    elif cmd == "confirm":
        sys.exit(confirm())
    else:
        print("usage: weekly_digest.py build|confirm")
        sys.exit(2)
