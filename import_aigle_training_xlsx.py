#!/usr/bin/env python3
"""
Import Aigle start timing XLSX rows into the local BMX training database.

Example:
  python3 import_aigle_training_xlsx.py \
    --xlsx "/path/to/Aigle_Zeiten_All.xlsx" \
    --db bmx.db
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


DEFAULT_DB_PATH = "bmx.db"
DEFAULT_EVENT_ID = "20260303_aigle_training"
DEFAULT_DISPLAY_NAME = "Aigle Startzeiten Training"
DEFAULT_LOCATION = "Aigle"
DEFAULT_COUNTRY = "SUI"
DEFAULT_CATEGORY = "Aigle Training"


FIRST_NAME_COLUMNS = ("Vorname", "Name", "First name", "Firstname")
LAST_NAME_COLUMNS = ("Nachname/ Trabsponder", "Unnamed: 3", "Nachname", "Last name", "Lastname")
TRANSPONDER_COLUMNS = ("Trabsponder", "Transponder", "Unnamed: 4")


def clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return " ".join(str(value).strip().split())


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_seconds(value: Any) -> Optional[str]:
    num = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(num):
        return None
    return f"{float(num):.3f}"


def format_clock(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    text = str(value).strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.notna(parsed):
        return parsed.strftime("%H:%M")
    return text[:5]


def format_day_label(value: Any) -> str:
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return ""
    return dt.strftime("%d.%m.")


def first_existing_value(row: pd.Series, columns: tuple[str, ...]) -> Any:
    for col in columns:
        if col in row.index:
            value = row.get(col)
            if not clean_text(value):
                continue
            return value
    return None


def ensure_training_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS training_times (
          event_id TEXT NOT NULL,
          category TEXT,
          bib INTEGER,
          name TEXT,
          nation TEXT,
          gate TEXT,
          start TEXT,
          t1 TEXT,
          source_file TEXT,
          ingested_at TEXT NOT NULL,
          kink TEXT,
          bottom TEXT,
          interim TEXT,
          t1_in TEXT,
          total TEXT,
          split_count INTEGER,
          split_cumulative TEXT,
          split_deltas TEXT,
          training_block_id TEXT,
          training_block_label TEXT,
          training_block_time TEXT,
          source_kind TEXT,
          PRIMARY KEY (event_id, category, bib, name, gate, start, t1, source_file)
        )
        """
    )
    cur = conn.execute("PRAGMA table_info(training_times)")
    existing = {row[1] for row in cur.fetchall()}
    wanted = {
        "kink": "TEXT",
        "bottom": "TEXT",
        "interim": "TEXT",
        "t1_in": "TEXT",
        "total": "TEXT",
        "split_count": "INTEGER",
        "split_cumulative": "TEXT",
        "split_deltas": "TEXT",
        "training_block_id": "TEXT",
        "training_block_label": "TEXT",
        "training_block_time": "TEXT",
        "source_kind": "TEXT",
    }
    for col, col_type in wanted.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE training_times ADD COLUMN {col} {col_type}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_training_event ON training_times(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_training_event_name ON training_times(event_id, name)")


def make_name(row: pd.Series) -> str:
    first = clean_text(first_existing_value(row, FIRST_NAME_COLUMNS))
    last = clean_text(first_existing_value(row, LAST_NAME_COLUMNS))
    transponder = clean_text(first_existing_value(row, TRANSPONDER_COLUMNS))
    name = " ".join(part for part in [first, last] if part)
    return name or transponder


def init_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            display_name TEXT,
            location TEXT,
            country TEXT,
            event_type TEXT,
            event_date TEXT,
            last_seen TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date)")
    cur = conn.execute("PRAGMA table_info(events)")
    existing = {row[1] for row in cur.fetchall()}
    if "event_type" not in existing:
        conn.execute("ALTER TABLE events ADD COLUMN event_type TEXT")


def upsert_event(conn: sqlite3.Connection, meta: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO events (event_id, display_name, location, country, event_type, event_date, last_seen)
        VALUES (:event_id, :display_name, :location, :country, :event_type, :event_date, :last_seen)
        ON CONFLICT(event_id) DO UPDATE SET
            display_name=excluded.display_name,
            location=excluded.location,
            country=excluded.country,
            event_type=coalesce(excluded.event_type, events.event_type),
            event_date=excluded.event_date,
            last_seen=excluded.last_seen
        """,
        meta,
    )


def build_rows(df: pd.DataFrame, event_id: str, source_file: str) -> List[Dict[str, Any]]:
    ingested_at = now_iso()
    rows: List[Dict[str, Any]] = []
    for _, raw in df.iterrows():
        name = make_name(raw)
        if not name:
            continue

        kink = format_seconds(raw.get("kink"))
        split = format_seconds(raw.get("split"))
        bottom = format_seconds(raw.get("bottom"))
        if not bottom:
            continue

        day_label = format_day_label(raw.get("Date"))
        clock = format_clock(raw.get("time"))
        block_label = " | ".join(part for part in [day_label, clock] if part)
        transponder = clean_text(first_existing_value(raw, TRANSPONDER_COLUMNS))
        gate = f"Transponder {transponder}" if transponder else ""

        cumulative_values = [v for v in [kink, bottom] if v]
        delta_values = [v for v in [kink, split] if v]

        rows.append(
            {
                "event_id": event_id,
                "category": DEFAULT_CATEGORY,
                "bib": None,
                "name": name,
                "nation": None,
                "gate": gate,
                "kink": kink,
                "bottom": split,
                "interim": bottom,
                "t1_in": None,
                "total": bottom,
                "split_count": len(cumulative_values),
                "split_cumulative": ",".join(cumulative_values),
                "split_deltas": ",".join(delta_values),
                "training_block_id": f"aigle|{event_id}|{day_label}|{clock}",
                "training_block_label": block_label,
                "training_block_time": clock,
                "start": bottom,
                "t1": None,
                "source_kind": "aigle_xlsx",
                "source_file": source_file,
                "ingested_at": ingested_at,
            }
        )
    return rows


def row_signature(row: Dict[str, Any]) -> tuple:
    return (
        row.get("event_id") or "",
        row.get("category") or "",
        row.get("bib") if row.get("bib") is not None else -1,
        row.get("name") or "",
        row.get("nation") or "",
        row.get("gate") or "",
        row.get("start") or "",
        row.get("t1") or "",
        row.get("source_file") or "",
        row.get("training_block_id") or "",
    )


def dedupe_rows(conn: sqlite3.Connection, rows: List[Dict[str, Any]], event_id: str) -> List[Dict[str, Any]]:
    existing_sigs = set()
    for existing_row in conn.execute(
        """
        SELECT event_id, category, bib, name, nation, gate, start, t1, source_file, training_block_id
        FROM training_times
        WHERE event_id = ?
        """,
        (event_id,),
    ):
        existing_sigs.add(
            (
                existing_row[0] or "",
                existing_row[1] or "",
                existing_row[2] if existing_row[2] is not None else -1,
                existing_row[3] or "",
                existing_row[4] or "",
                existing_row[5] or "",
                existing_row[6] or "",
                existing_row[7] or "",
                existing_row[8] or "",
                existing_row[9] or "",
            )
        )

    out: List[Dict[str, Any]] = []
    batch_sigs = set()
    for row in rows:
        sig = row_signature(row)
        if sig in existing_sigs or sig in batch_sigs:
            continue
        out.append(row)
        batch_sigs.add(sig)
    return out


def insert_rows(conn: sqlite3.Connection, rows: List[Dict[str, Any]], event_id: str) -> int:
    if not rows:
        return 0
    insert_rows = dedupe_rows(conn, rows, event_id)
    if not insert_rows:
        return 0
    conn.executemany(
        """
        INSERT OR IGNORE INTO training_times (
          event_id, category, bib, name, nation, gate,
          kink, bottom, interim, t1_in, total,
          split_count, split_cumulative, split_deltas,
          training_block_id, training_block_label, training_block_time,
          start, t1, source_kind, source_file, ingested_at
        ) VALUES (
          :event_id, :category, :bib, :name, :nation, :gate,
          :kink, :bottom, :interim, :t1_in, :total,
          :split_count, :split_cumulative, :split_deltas,
          :training_block_id, :training_block_label, :training_block_time,
          :start, :t1, :source_kind, :source_file, :ingested_at
        )
        """,
        insert_rows,
    )
    return len(insert_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", required=True, help="Path to Aigle_Zeiten_All.xlsx")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB path")
    parser.add_argument("--event-id", default=DEFAULT_EVENT_ID)
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--country", default=DEFAULT_COUNTRY)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        raise SystemExit(f"XLSX not found: {xlsx_path}")

    df = pd.read_excel(xlsx_path, sheet_name=0, engine="openpyxl")
    rows = build_rows(df, args.event_id, xlsx_path.name)
    event_date = pd.to_datetime(df.get("Date"), errors="coerce").max()

    conn = sqlite3.connect(args.db, timeout=60)
    try:
        init_events_table(conn)
        ensure_training_table(conn)
        upsert_event(
            conn,
            {
                "event_id": args.event_id,
                "display_name": args.display_name,
                "location": args.location,
                "country": args.country,
                "event_type": "Other",
                "event_date": event_date.strftime("%Y-%m-%d") if pd.notna(event_date) else None,
                "last_seen": now_iso(),
            },
        )
        inserted = insert_rows(conn, rows, args.event_id)
        conn.commit()
    finally:
        conn.close()

    print(f"[{args.event_id}] rows prepared: {len(rows)}")
    print(f"[{args.event_id}] rows inserted: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
