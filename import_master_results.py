#!/usr/bin/env python3
import argparse
import sqlite3
from typing import Optional

import pandas as pd


def init_master_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS master_results (
        uci_event_id TEXT,
        uci_id TEXT,
        bib INTEGER,
        last_name TEXT,
        first_name TEXT,
        gender TEXT,
        category TEXT,
        klasse TEXT,
        year INTEGER,
        date TEXT,
        location TEXT,
        track TEXT,
        host_nation TEXT,
        rank INTEGER,
        time TEXT,
        irm TEXT,
        source TEXT,
        PRIMARY KEY (uci_event_id, uci_id, bib, category, gender)
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_master_event ON master_results(uci_event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_master_year ON master_results(year)")
    conn.commit()


def import_parquet(path: str, klasse: str = "CM") -> int:
    df = pd.read_parquet(path)
    # Filter to BMX World Championship class + relevant categories
    df = df[df["Klasse"] == klasse].copy()
    df = df[df["Kategorie"].isin(["Elite", "U23", "Junior"])].copy()

    # Normalize gender
    def norm_gender(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if s.startswith("M"):
            return "M"
        if s.startswith("W"):
            return "W"
        return None

    df["gender"] = df["Geschlecht"].apply(norm_gender)
    df["rank"] = pd.to_numeric(df["Rang"], errors="coerce").astype("Int64")
    df["bib"] = pd.to_numeric(df["BIB"], errors="coerce").astype("Int64")

    out = pd.DataFrame({
        "uci_event_id": df["UCIEventID"].astype(str),
        "uci_id": df["UCI ID"].astype(str),
        "bib": df["bib"],
        "last_name": df["Nachname"],
        "first_name": df["Vorname"],
        "gender": df["gender"],
        "category": df["Kategorie"],
        "klasse": df["Klasse"],
        "year": df["Jahr"].astype("Int64"),
        "date": df["Datum"].astype(str),
        "location": df["Ort"],
        "track": df["Track"],
        "host_nation": df["HostNation"],
        "rank": df["rank"],
        "time": df["Zeit"].astype(str),
        "irm": df["IRM"].astype(str),
        "source": df["Quelle"],
    })

    conn = sqlite3.connect("bmx.db")
    init_master_table(conn)
    out.to_sql("master_results", conn, if_exists="append", index=False, method="multi")
    conn.commit()
    conn.close()
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--klasse", default="CM")
    args = ap.parse_args()
    n = import_parquet(args.parquet, klasse=args.klasse)
    print(f"Imported {n} rows into master_results.")


if __name__ == "__main__":
    main()
