#!/usr/bin/env python3
import argparse
import datetime
import re
import sqlite3
from typing import Any, Dict, List, Optional

import requests

from ingest import init_db, upsert_event, upsert_picks, now_iso


BASE_DEFAULT = "https://prod.server.tissottiming.com/competitions/bmxwch2025"
DEFAULT_EVENT_ID = "20250726_wch_bmx"


def http_get_json(url: str) -> Any:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def map_group_id(name: str) -> Optional[int]:
    if not name:
        return None
    n = name.strip().lower()
    if "men elite" in n:
        return 91
    if "women elite" in n:
        return 92
    if "men u23" in n:
        return 93
    if "women u23" in n:
        return 94
    if "men junior" in n or "junior men" in n:
        return 95
    if "women junior" in n or "junior women" in n:
        return 96
    return None


def map_round_key(title: str) -> int:
    t = (title or "").strip().lower()
    if "round 1" in t or t == "round1":
        return 1
    if "last chance" in t or "lcq" in t:
        return 2
    if "1/8" in t or "eighth" in t:
        return 3
    if "1/4" in t or "quarter" in t:
        return 4
    if "1/2" in t or "semi" in t:
        return 5
    if "final" in t:
        return 6
    # fallback: put unknown rounds at end
    return 99


def normalize_round_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return "Round"
    # unify known labels
    tl = t.lower()
    if "last chance" in tl:
        return "LCQ"
    if "quarter" in tl and "1/4" not in tl:
        return "1/4 Finals"
    if "semi" in tl and "1/2" not in tl:
        return "1/2 Finals"
    return t


def parse_heat_number(name: str) -> int:
    if not name:
        return 0
    m = re.search(r"(\\d+)", name)
    return int(m.group(1)) if m else 0


def extract_split(result: Dict[str, Any], key_name: str) -> Optional[str]:
    splits = result.get("splits") or []
    for s in splits:
        if str(s.get("key")).lower() == key_name.lower():
            return s.get("value")
        if str(s.get("name")).lower() == key_name.lower():
            return s.get("value")
    return None


def fetch_events(base: str) -> List[Dict[str, Any]]:
    data = http_get_json(f"{base}/events")
    return data if isinstance(data, list) else []


def fetch_phases(base: str, event_num: int) -> List[Dict[str, Any]]:
    data = http_get_json(f"{base}/events/{event_num}/phases")
    return data if isinstance(data, list) else []


def fetch_results(base: str, event_num: int, phase_num: int) -> Dict[str, Any]:
    return http_get_json(f"{base}/events/{event_num}/phases/{phase_num}/results")


def fetch_startlist(base: str, event_num: int, phase_num: int) -> Dict[str, Any]:
    return http_get_json(f"{base}/events/{event_num}/phases/{phase_num}/startlist")


def build_pick_order_map(startlist_json: Dict[str, Any]) -> Dict[tuple, int]:
    """
    Returns mapping: (heat_name, rider_key_or_bib) -> pick_order (1-based)
    """
    result: Dict[tuple, int] = {}
    heats = startlist_json.get("heats") or []
    for heat in heats:
        heat_name = heat.get("name") or ""
        results = heat.get("results") or []
        for idx, res in enumerate(results, start=1):
            rider = res.get("rider") or {}
            rider_key = rider.get("key")
            bib = rider.get("bib")
            if rider_key is not None:
                result[(heat_name, str(rider_key))] = idx
            if bib is not None:
                result[(heat_name, int(bib))] = idx
    return result


def ingest_tissot(base: str, event_id: str, display_name: str, event_date: str, only_events: Optional[List[int]] = None) -> int:
    conn = sqlite3.connect("bmx.db")
    init_db(conn)

    # Event meta (single WM event_id)
    meta = {
        "event_id": event_id,
        "display_name": display_name,
        "location": "World Championships",
        "country": None,
        "event_date": event_date,
        "last_seen": now_iso(),
    }
    upsert_event(conn, meta)

    events = fetch_events(base)
    total_rows = 0
    seen_at = now_iso()

    for ev in events:
        ev_num = ev.get("number") or ev.get("id")
        if ev_num is None:
            continue
        ev_num = int(ev_num)
        if only_events and ev_num not in only_events:
            continue

        ev_name = ev.get("name") or ""
        group_id = map_group_id(ev_name)

        phases = fetch_phases(base, ev_num)
        for ph in phases:
            ph_num = ph.get("number") or ph.get("id")
            if ph_num is None:
                continue
            ph_num = int(ph_num)
            ph_name = ph.get("name") or ph.get("title") or f"Phase {ph_num}"
            round_title = normalize_round_title(ph_name)
            round_key = map_round_key(ph_name)

            pick_order_map: Dict[tuple, int] = {}
            try:
                startlist = fetch_startlist(base, ev_num, ph_num)
                if isinstance(startlist, dict):
                    pick_order_map = build_pick_order_map(startlist)
            except Exception:
                pick_order_map = {}

            data = fetch_results(base, ev_num, ph_num)
            heats = data.get("heats") or []
            for heat in heats:
                heat_name = heat.get("name") or ""
                heat_number = parse_heat_number(heat_name)
                # make heat_id unique across events by prefixing event number
                heat_id = ev_num * 1000 + heat_number

                for res in heat.get("results", []):
                    rider = res.get("rider") or {}
                    bib = rider.get("bib")
                    if bib is None:
                        continue

                    start = extract_split(res, "Reaction Time")
                    t1 = extract_split(res, "Corner 1")
                    t2 = extract_split(res, "Corner 2")
                    t3 = extract_split(res, "Corner 3")
                    time = res.get("time") or res.get("value")

                    rider_key = rider.get("key")
                    pick_order = None
                    if rider_key is not None:
                        pick_order = pick_order_map.get((heat_name, str(rider_key)))
                    if pick_order is None:
                        pick_order = pick_order_map.get((heat_name, int(bib)))

                    rows = {
                        "event_id": event_id,
                        "group_id": group_id,
                        "round_key": round_key,
                        "round_title": round_title,
                        "heat_id": heat_id,
                        "heat_title": heat_name,
                        "heat_status": None,
                        "start_time_string": None,
                        "bib": int(bib),
                        "name": rider.get("name"),
                        "nation": rider.get("nation"),
                        "pick_order": pick_order,
                        "lane": None,
                        "lane_idx": None,
                        "uci_id": rider.get("uciRiderId"),
                        "start": start,
                        "t1": t1,
                        "t2": t2,
                        "t3": t3,
                        "t4": None,
                        "time": time,
                        "seen_at": seen_at,
                    }
                    upsert_picks(conn, [rows])
                    total_rows += 1

    conn.close()
    return total_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", default=DEFAULT_EVENT_ID)
    ap.add_argument(
        "--competition",
        default="bmxwch2025",
        help="Tissot competition id, e.g. bmxwch2025, bmxwch2024",
    )
    ap.add_argument(
        "--event-date",
        default="2025-07-26",
        help="Event date (YYYY-MM-DD) stored in events table",
    )
    ap.add_argument(
        "--display-name",
        default="BMX World Championships",
        help="Display name stored in events table",
    )
    ap.add_argument(
        "--years",
        help="Comma-separated years to import (e.g. 2024,2023,2022). Overrides --competition/--event-id.",
    )
    ap.add_argument(
        "--only-events",
        help="Comma-separated event numbers (e.g. 1,2,3) to restrict import",
    )
    args = ap.parse_args()

    only_events = None
    if args.only_events:
        only_events = [int(x.strip()) for x in args.only_events.split(",") if x.strip().isdigit()]

    total = 0
    if args.years:
        years = [y.strip() for y in args.years.split(",") if y.strip().isdigit()]
        for y in years:
            comp = f"bmxwch{y}"
            base = f"https://prod.server.tissottiming.com/competitions/{comp}"
            event_id = f"{y}0802_wch_bmx"
            display = f"BMX World Championships {y}"
            rows = ingest_tissot(
                base=base,
                event_id=event_id,
                display_name=display,
                event_date=f"{y}-08-02",
                only_events=only_events,
            )
            print(f"[tissot] {y}: imported rows: {rows}")
            total += rows
    else:
        base = f"https://prod.server.tissottiming.com/competitions/{args.competition}"
        rows = ingest_tissot(
            base=base,
            event_id=args.event_id,
            display_name=args.display_name,
            event_date=args.event_date,
            only_events=only_events,
        )
        print(f"[tissot] imported rows: {rows}")
        total = rows
    print(f"[tissot] total imported: {total}")


if __name__ == "__main__":
    main()
