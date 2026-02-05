#!/usr/bin/env python3
"""
import_training.py — Download ChronoRace training XLS and import into SQLite.

Example:
  python3 import_training.py --event 20250614_bmx
  python3 import_training.py --event 20250614_bmx --files sar1_bmx_training.xls sar2_bmx_training.xls
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
import requests

DEFAULT_DB_PATH = "bmx.db"
BASE_URL = "https://chronorace.blob.core.windows.net/webresources/{event_id}/{filename}"
DEFAULT_FILES = ["sar1_bmx_training.xls"]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_training_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
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
        PRIMARY KEY (event_id, category, bib, name, gate, start, t1, source_file)
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_training_event ON training_times(event_id)")
    conn.commit()


def download_file(event_id: str, filename: str, outdir: Path) -> Path:
    url = BASE_URL.format(event_id=event_id, filename=filename)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / filename
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    target.write_bytes(r.content)
    return target


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    # Expected columns: Category, Nr, Name, Nat, Gate, Start, T1
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    col_map = {
        "Category": "category",
        "Nr": "bib",
        "Name": "name",
        "Nat": "nation",
        "Gate": "gate",
        "Start": "start",
        "T1": "t1",
    }
    # Keep only known columns
    keep = [c for c in df.columns if c in col_map]
    df = df[keep].rename(columns=col_map)
    return df


def load_xls(path: Path) -> pd.DataFrame:
    return pd.read_excel(path, engine="xlrd")


def upsert_training(conn: sqlite3.Connection, event_id: str, df: pd.DataFrame, source_file: str) -> int:
    if df.empty:
        return 0

    df = normalize_columns(df)
    df["event_id"] = event_id
    df["source_file"] = source_file
    df["ingested_at"] = now_iso()

    rows = df.to_dict(orient="records")
    conn.executemany("""
    INSERT OR IGNORE INTO training_times (
        event_id, category, bib, name, nation, gate, start, t1, source_file, ingested_at
    ) VALUES (
        :event_id, :category, :bib, :name, :nation, :gate, :start, :t1, :source_file, :ingested_at
    )
    """, rows)
    conn.commit()
    return conn.total_changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True, help="Event ID, z.B. 20250614_bmx")
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB file (default: bmx.db)")
    ap.add_argument("--files", nargs="+", default=DEFAULT_FILES, help="Training XLS filenames (download)")
    ap.add_argument("--local", nargs="+", help="Local XLS path(s) to import (skip download)")
    ap.add_argument("--outdir", default="out/training", help="Download folder")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_training_table(conn)

    total = 0

    if args.local:
        for lp in args.local:
            path = Path(lp)
            if not path.exists():
                print(f"[{args.event}] local file not found: {path}")
                continue
            try:
                df = load_xls(path)
            except Exception as e:
                print(f"[{args.event}] read failed for {path}: {e}")
                continue
            n = upsert_training(conn, args.event, df, path.name)
            total += n
            print(f"[{args.event}] {path.name}: imported {n} rows")
    else:
        for fname in args.files:
            try:
                path = download_file(args.event, fname, Path(args.outdir))
            except Exception as e:
                print(f"[{args.event}] download failed for {fname}: {e}")
                continue

            try:
                df = load_xls(path)
            except Exception as e:
                print(f"[{args.event}] read failed for {path}: {e}")
                continue

            n = upsert_training(conn, args.event, df, fname)
            total += n
            print(f"[{args.event}] {fname}: imported {n} rows")

    print(f"[{args.event}] total imported: {total}")


if __name__ == "__main__":
    main()
