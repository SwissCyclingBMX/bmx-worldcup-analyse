#!/usr/bin/env python3
"""
Backfill missing events.location and events.event_date from events.display_name.

Example:
  python3 backfill_events_meta.py --db bmx.db
"""

import argparse
import re
import sqlite3
from datetime import datetime


def clean_spaces(s: str) -> str:
    return " ".join(str(s or "").strip().split())


def derive_from_display_name(display_name: str):
    dn = clean_spaces(display_name)
    if not dn:
        return None, None

    m = re.search(
        r"ROUND\s*(\d+)\s*-\s*([^,]+),\s*([0-9]{1,2}(?:-[0-9]{1,2})?\s+[A-Z]{3}\s+[0-9]{4}),\s*([A-Z]{3})\s*$",
        dn,
        flags=re.IGNORECASE,
    )
    if m:
        rnd = int(m.group(1))
        place = clean_spaces(m.group(2))
        date_txt = clean_spaces(m.group(3)).upper()
        return f"ROUND {rnd} - {place}", date_txt

    if "WORLD CHAMPIONSHIP" in dn.upper():
        return "World Championships", None

    return None, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="bmx.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    rows = cur.execute("SELECT event_id, display_name, location, event_date FROM events").fetchall()

    updated = 0
    now = datetime.now().isoformat(timespec="seconds")
    for event_id, display_name, location, event_date in rows:
        loc_old = clean_spaces(location)
        date_old = clean_spaces(event_date)
        loc_new, date_new = derive_from_display_name(display_name)

        loc_final = loc_old
        date_final = date_old
        if (not loc_old) or loc_old.upper() == "UNKNOWN":
            if loc_new:
                loc_final = loc_new
        if not date_old and date_new:
            date_final = date_new

        if loc_final != loc_old or date_final != date_old:
            cur.execute(
                "UPDATE events SET location=?, event_date=?, last_seen=? WHERE event_id=?",
                (loc_final or None, date_final or None, now, event_id),
            )
            updated += cur.rowcount

    conn.commit()
    conn.close()
    print(f"events updated: {updated}")


if __name__ == "__main__":
    main()

