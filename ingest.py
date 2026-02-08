#!/usr/bin/env python3
"""
ingest.py — ChronoRace BMX Live Timing -> SQLite (inkl. Event-Metadaten)

Beispiele:
  # Historisch (einmalig)
  python3 ingest.py --events 20250614_bmx --once

  # Live (polling)
  python3 ingest.py --events 20250615_bmx --sleep 15

  # Mehrere Events einmalig
  python3 ingest.py --events 20250614_bmx 20250615_bmx --once
"""

import argparse
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

HEADERS = {"accept": "application/json", "user-agent": "HeatScout/1.0"}
DEFAULT_DB_PATH = "bmx.db"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def walk(node: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(node, dict):
        return
    yield node
    childs = node.get("Childs")
    if isinstance(childs, list):
        for c in childs:
            yield from walk(c)


def http_get_json(url: str, timeout: int = 20, retries: int = 2, backoff: float = 1.5) -> Any:
    last_err = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(backoff ** i)
    raise last_err  # type: ignore


def cms_url(event_id: str) -> str:
    return f"https://prod.chronorace.be/api/results/uci/dh/cms/{event_id}"


def results_url(event_id: str, round_key: int) -> str:
    # bestätigtes Muster
    return f"https://prod.chronorace.be/api/results/generic/uci/{event_id}/bmx?key={round_key}%2Fresults"


def extract_round_keys_from_cms(cms_data: Dict[str, Any]) -> List[int]:
    keys = set()
    for n in walk(cms_data):
        name = (n.get("DisplayName") or n.get("Name") or "").strip().lower()
        if name == "live timing":
            route = n.get("Route", "")
            m = re.search(r'"key"\s*:\s*"(\d+)"', route)
            if m:
                keys.add(int(m.group(1)))
    return sorted(keys)


def extract_event_meta(event_id: str, cms_data: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    CMS top level enthält z.B.:
    DisplayName: "2025 BMX RACING WORLD CUP - ROUND 1 - Sarrians, 14 JUN 2025, FRA"
    Wir speichern:
      - display_name (voller String)
      - location/date/country (heuristisch, falls Muster passt)
    """
    display = (cms_data.get("DisplayName") or cms_data.get("Name") or "").strip()

    location = None
    country = None
    event_date = None

    m = re.search(r"-\s*([^,]+),\s*(\d{1,2}\s+[A-Z]{3}\s+\d{4}),\s*([A-Z]{3})\s*$", display)
    if m:
        location = m.group(1).strip()
        event_date = m.group(2).strip()
        country = m.group(3).strip()

    return {
        "event_id": event_id,
        "display_name": display or event_id,
        "location": location,
        "country": country,
        "event_date": event_date,
        "last_seen": now_iso(),
    }


def pick_nation(r: Dict[str, Any]) -> Optional[str]:
    return r.get("Nation") or r.get("NOC") or r.get("CountryCode")


def init_db(conn: sqlite3.Connection) -> None:
    # Event-Metadaten
    conn.execute("""
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        display_name TEXT,
        location TEXT,
        country TEXT,
        event_date TEXT,
        last_seen TEXT
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date)")

    # Picks (Fakten)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS picks (
        event_id TEXT NOT NULL,
        group_id INTEGER,
        round_key INTEGER NOT NULL,
        round_title TEXT,
        heat_id INTEGER NOT NULL,
        heat_title TEXT,
        heat_status TEXT,
        start_time_string TEXT,
        bib INTEGER NOT NULL,
        name TEXT,
        nation TEXT,
        pick_order INTEGER,
        lane TEXT,
        lane_idx INTEGER,
        uci_id TEXT,
        start TEXT,
        t1 TEXT,
        t2 TEXT,
        t3 TEXT,
        t4 TEXT,
        time TEXT,
        seen_at TEXT NOT NULL,
        PRIMARY KEY (event_id, round_key, heat_id, bib)
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_picks_event ON picks(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_picks_event_nation ON picks(event_id, nation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_picks_event_heat ON picks(event_id, round_key, heat_id)")

    conn.commit()

    # Add new columns if table existed before
    ensure_pick_columns(conn)


def ensure_pick_columns(conn: sqlite3.Connection) -> None:
    wanted = {
        "uci_id": "TEXT",
        "start": "TEXT",
        "t1": "TEXT",
        "t2": "TEXT",
        "t3": "TEXT",
        "t4": "TEXT",
        "time": "TEXT",
        "rank": "INTEGER",
    }
    cur = conn.execute("PRAGMA table_info(picks)")
    existing = {row[1] for row in cur.fetchall()}
    for col, col_type in wanted.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {col_type}")
    conn.commit()


def upsert_event(conn: sqlite3.Connection, meta: Dict[str, Optional[str]]) -> None:
    conn.execute("""
    INSERT INTO events (event_id, display_name, location, country, event_date, last_seen)
    VALUES (:event_id, :display_name, :location, :country, :event_date, :last_seen)
    ON CONFLICT(event_id) DO UPDATE SET
        display_name=excluded.display_name,
        location=excluded.location,
        country=excluded.country,
        event_date=excluded.event_date,
        last_seen=excluded.last_seen
    """, meta)
    conn.commit()


def upsert_picks(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany("""
    INSERT INTO picks (
        event_id, group_id, round_key, round_title,
        heat_id, heat_title, heat_status, start_time_string,
        bib, name, nation, pick_order, lane, lane_idx,
        uci_id, start, t1, t2, t3, t4, time, rank,
        seen_at
    ) VALUES (
        :event_id, :group_id, :round_key, :round_title,
        :heat_id, :heat_title, :heat_status, :start_time_string,
        :bib, :name, :nation, :pick_order, :lane, :lane_idx,
        :uci_id, :start, :t1, :t2, :t3, :t4, :time, :rank,
        :seen_at
    )
    ON CONFLICT(event_id, round_key, heat_id, bib) DO UPDATE SET
        group_id=excluded.group_id,
        round_title=excluded.round_title,
        heat_title=excluded.heat_title,
        heat_status=excluded.heat_status,
        start_time_string=excluded.start_time_string,
        name=excluded.name,
        nation=excluded.nation,
        pick_order=excluded.pick_order,
        lane=excluded.lane,
        lane_idx=excluded.lane_idx,
        uci_id=excluded.uci_id,
        start=excluded.start,
        t1=excluded.t1,
        t2=excluded.t2,
        t3=excluded.t3,
        t4=excluded.t4,
        time=excluded.time,
        rank=excluded.rank,
        seen_at=excluded.seen_at
    """, rows)
    conn.commit()


def ingest_event_once(conn: sqlite3.Connection, event_id: str, verbose: bool = True) -> Tuple[int, int]:
    """
    Returns: (num_rounds_ok, num_rows_upserted)
    """
    cms_data_any = http_get_json(cms_url(event_id))
    if not isinstance(cms_data_any, dict):
        raise RuntimeError(f"[{event_id}] CMS returned {type(cms_data_any).__name__}, expected dict")

    cms_data: Dict[str, Any] = cms_data_any

    # 1) Event meta speichern
    meta = extract_event_meta(event_id, cms_data)
    upsert_event(conn, meta)

    # 2) Round keys (Live Timing) aus CMS ziehen
    round_keys = extract_round_keys_from_cms(cms_data)
    if verbose:
        print(f"[{event_id}] round keys: {round_keys}")

    seen_at = now_iso()
    total_rows = 0
    rounds_ok = 0

    for rk in round_keys:
        try:
            data = http_get_json(results_url(event_id, rk))
        except Exception as e:
            if verbose:
                print(f"[{event_id}] round_key {rk} fetch failed: {e}")
            continue

        # robust: manche very old events liefern anderes JSON
        if not isinstance(data, dict):
            if verbose:
                print(f"[{event_id}] round_key {rk} returned {type(data).__name__}, skipping.")
            continue

        heats = data.get("Heats")
        if not isinstance(heats, list):
            if verbose:
                print(f"[{event_id}] round_key {rk} missing Heats list, skipping.")
            continue

        rounds_ok += 1

        group_id = data.get("GroupId")
        round_title = data.get("Title")

        rows: List[Dict[str, Any]] = []
        for heat in heats:
            heat_id = heat.get("Id")
            heat_title = heat.get("Title")
            heat_status = heat.get("Status")
            start_time = heat.get("StartTimeString")

            heatdata = heat.get("HeatData", [])
            if not isinstance(heatdata, list):
                continue

            for r in heatdata:
                bib = r.get("Bib")
                if bib is None:
                    continue

                name = (f"{r.get('FirstName','')} {r.get('LastName','')}".strip())
                nation = pick_nation(r)

                rows.append({
                    "event_id": event_id,
                    "group_id": group_id,
                    "round_key": rk,
                    "round_title": round_title,
                    "heat_id": heat_id,
                    "heat_title": heat_title,
                    "heat_status": heat_status,
                    "start_time_string": start_time,
                    "bib": int(bib),
                    "name": name,
                    "nation": nation,
                    "pick_order": r.get("LaneSelectionOrder"),
                    "lane": r.get("Lane"),
                    "lane_idx": r.get("LaneIdx"),
                    "uci_id": r.get("UciId"),
                    "start": r.get("Start"),
                    "t1": r.get("T1"),
                    "t2": r.get("T2"),
                    "t3": r.get("T3"),
                    "t4": r.get("T4"),
                    "time": r.get("Time"),
                    "rank": r.get("Pos"),
                    "seen_at": seen_at,
                })

        upsert_picks(conn, rows)
        total_rows += len(rows)

    if verbose:
        print(f"[{event_id}] rounds ok: {rounds_ok}/{len(round_keys)} | rows upserted: {total_rows} @ {seen_at}")

    return rounds_ok, total_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--events",
        nargs="+",
        required=True,
        help="Eine oder mehrere Event-IDs, z.B. 20250614_bmx 20250615_bmx",
    )
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB file (default: bmx.db)")
    ap.add_argument("--once", action="store_true", help="Einmal ingestieren und beenden (historische Events)")
    ap.add_argument("--sleep", type=int, default=15, help="Polling-Intervall in Sekunden (default: 15)")
    ap.add_argument("--quiet", action="store_true", help="Weniger Output")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    init_db(conn)

    if args.once:
        for ev in args.events:
            ingest_event_once(conn, ev, verbose=not args.quiet)
        return

    while True:
        for ev in args.events:
            ingest_event_once(conn, ev, verbose=not args.quiet)
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
