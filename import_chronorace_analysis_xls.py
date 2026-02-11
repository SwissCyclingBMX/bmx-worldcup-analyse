#!/usr/bin/env python3
"""
Import ChronoRace heat-analysis XLS into picks table.

Example:
  python3 import_chronorace_analysis_xls.py \
    --event-id 20240210_bmx \
    --xls "/Users/davidgraf/Downloads/rot1_bmx_analysis.xls" \
    --db bmx.db
"""

import argparse
import re
import sqlite3
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd


GROUP_BY_CATEGORY = {
    "Men Elite": 91,
    "Women Elite": 92,
    "Men Under 23": 95,
    "Women Under 23": 96,
    "Men Junior": 93,
    "Women Junior": 94,
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_heat_label(label: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    s = str(label or "").strip()
    m = re.match(r"^(.*?)\s*-\s*(.*?)\s*-\s*Heat\s*(\d+)$", s, flags=re.IGNORECASE)
    if not m:
        return None, None, None
    category_text = m.group(1).strip()
    round_title = m.group(2).strip()
    heat_num = int(m.group(3))
    group_id = GROUP_BY_CATEGORY.get(category_text)
    heat_title = f"Heat {heat_num}"
    return group_id, round_title, heat_title


def parse_float(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.upper() in {"DNF", "DNS", "-"}:
        return None
    try:
        x = float(s)
        if x <= 0:
            return None
        return f"{x:.3f}"
    except Exception:
        return None


def parse_rank(v) -> Optional[int]:
    if v is None:
        return None
    # Keep numeric values as-is (e.g. 1.0 -> 1), avoid turning 1.0 into 10.
    try:
        if pd.notna(v):
            r = int(float(v))
            return r if r > 0 else None
    except Exception:
        pass
    s = str(v).strip()
    if not s:
        return None
    # Handle strings like "1." safely.
    s = re.sub(r"\.$", "", s)
    try:
        r = int(float(s.replace(",", ".")))
        return r if r > 0 else None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", required=True, help="Event id in picks/events table, e.g. 20240210_bmx")
    ap.add_argument("--xls", required=True, help="ChronoRace analysis .xls file")
    ap.add_argument("--db", default="bmx.db", help="SQLite DB path")
    args = ap.parse_args()

    df = pd.read_excel(args.xls, sheet_name="ChronoRace", header=1)
    if df.empty:
        print("No rows in xls.")
        return

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    seen_at = now_iso()

    total = 0
    matched = 0
    for _, row in df.iterrows():
        heat_label = row.get("Heat")
        bib_val = row.get("Nr")
        if pd.isna(heat_label) or pd.isna(bib_val):
            continue

        group_id, round_title, heat_title = parse_heat_label(str(heat_label))
        if group_id is None or not round_title or not heat_title:
            continue

        try:
            bib = int(float(str(bib_val).strip()))
        except Exception:
            continue

        # Column mapping in this XLS:
        # Time   -> Start
        # Time.1 -> T1
        # Time.2 -> T2
        # Time.3 -> T3
        # Time.4 -> Finish
        start = parse_float(row.get("Time"))
        t1 = parse_float(row.get("Time.1"))
        t2 = parse_float(row.get("Time.2"))
        t3 = parse_float(row.get("Time.3"))
        finish = parse_float(row.get("Time.4"))
        rank = parse_rank(row.get("Pos"))

        total += 1
        cur.execute(
            """
            UPDATE picks
            SET
              start = COALESCE(?, start),
              t1    = COALESCE(?, t1),
              t2    = COALESCE(?, t2),
              t3    = COALESCE(?, t3),
              time  = COALESCE(?, time),
              rank  = COALESCE(?, rank),
              seen_at = ?
            WHERE event_id = ?
              AND group_id = ?
              AND round_title = ?
              AND heat_title = ?
              AND bib = ?
            """,
            (
                start,
                t1,
                t2,
                t3,
                finish,
                rank,
                seen_at,
                args.event_id,
                group_id,
                round_title,
                heat_title,
                bib,
            ),
        )
        if cur.rowcount > 0:
            matched += cur.rowcount

    conn.commit()
    conn.close()

    print(f"event_id={args.event_id} | rows_read={total} | rows_updated={matched}")


if __name__ == "__main__":
    main()
