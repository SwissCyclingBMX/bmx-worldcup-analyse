#!/usr/bin/env python3
"""
ingest_sqorz.py

Ingest Sqorz-style payloads (classRanks / competitorRankSummaries / competitorRankDetails)
into the existing bmx.db schema (events + picks).

Input options:
  1) --payload-file payload.json
  2) --url ... [--post-json '{"accountId":"...","eventId":"..."}']

Examples:
  python3 ingest_sqorz.py --payload-file payload.json --event-id 20251128_usap_bmx

  python3 ingest_sqorz.py \\
    --url "https://example/api/event/summary" \\
    --post-json '{"accountId":"...","regionCode":"US","eventId":"..."}' \\
    --series USABMX
"""

import argparse
import datetime as dt
import json
import re
import sqlite3
import zlib
from typing import Any, Dict, List, Optional, Tuple

import requests

from ingest import DEFAULT_DB_PATH, init_db, normalize_event_type, now_iso, upsert_event, upsert_picks

SERIES_TO_CODE = {
    "USABMX": "usap",
    "FFC": "ffc",
    "SCC": "scc",
    "OTHER": "other",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB path (default: bmx.db)")
    ap.add_argument("--payload-file", help="Path to JSON payload file")
    ap.add_argument("--url", help="API URL returning payload JSON")
    ap.add_argument("--post-json", help="Optional JSON body string for POST requests")
    ap.add_argument("--event-id", help="Target event_id (default: derived from payload)")
    ap.add_argument(
        "--series",
        default="USABMX",
        help="Series label for derived event_id: FFC | USABMX | SCC | Other (default: USABMX)",
    )
    ap.add_argument(
        "--series-code",
        default="",
        help="Optional explicit series code for derived event_id (overrides --series).",
    )
    ap.add_argument(
        "--event-type",
        default="",
        help="Optional Wettkampf Typ override: WC | WM | EC | EM | USABMX | FFC | SCC | Other",
    )
    ap.add_argument(
        "--all-classes",
        action="store_true",
        help="Ingest all classes (default ingests Men/Women Pro + Elite)",
    )
    ap.add_argument(
        "--class-contains",
        action="append",
        default=["Men Pro", "Women Pro", "Men Elite", "Women Elite"],
        help="Optional filter: only ingest classes whose className contains this text (case-insensitive). Repeatable.",
    )
    return ap.parse_args()


def resolve_series_code(args: argparse.Namespace) -> str:
    if str(args.series_code or "").strip():
        code = re.sub(r"[^a-z0-9]+", "", str(args.series_code).lower().strip())
        return code or "usap"
    series_label = str(args.series or "USABMX").strip().upper()
    return SERIES_TO_CODE.get(series_label, "other")


def resolve_event_type(args: argparse.Namespace) -> str:
    explicit = normalize_event_type(getattr(args, "event_type", ""))
    if explicit:
        return explicit
    series_label = str(args.series or "Other").strip()
    mapping = {
        "USABMX": "USABMX",
        "FFC": "FFC",
        "SCC": "SCC",
        "Other": "Other",
    }
    return mapping.get(series_label, "Other")


def unwrap_payload(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict) and "classRanks" in obj:
        return obj
    if isinstance(obj, dict):
        for k in ("data", "result", "payload", "response"):
            v = obj.get(k)
            if isinstance(v, dict) and "classRanks" in v:
                return v
    raise RuntimeError("Payload does not contain classRanks")


def normalize_sqorz_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return u
    if "/json/event/" in u:
        return u
    if "our.sqorz.com" not in u:
        return u
    m = re.search(r"/event/([a-f0-9]{24})(?:/|$)", u, flags=re.IGNORECASE)
    if m:
        return f"https://our.sqorz.com/json/event/{m.group(1)}"
    return u


def load_payload(args: argparse.Namespace) -> Dict[str, Any]:
    raw: Any
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    elif args.url:
        headers = {"accept": "application/json", "user-agent": "HeatScout/1.0"}
        url = normalize_sqorz_url(args.url)
        if args.post_json:
            body = json.loads(args.post_json)
            r = requests.post(url, headers=headers, json=body, timeout=30)
        else:
            r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        raw = r.json()
    else:
        raise RuntimeError("Provide either --payload-file or --url")
    return unwrap_payload(raw)


def parse_event_date(s: Optional[str]) -> Tuple[str, str]:
    if not s:
        d = dt.date.today()
        return d.strftime("%Y%m%d"), d.isoformat()
    s2 = str(s).strip()
    # expected: YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s2)
    if m:
        y, mo, d = m.groups()
        return f"{y}{mo}{d}", f"{y}-{mo}-{d}"
    # fallback: DD-MM-YYYY
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s2)
    if m:
        d, mo, y = m.groups()
        return f"{y}{mo}{d}", f"{y}-{mo}-{d}"
    d = dt.date.today()
    return d.strftime("%Y%m%d"), d.isoformat()


def map_category_gender(class_name: str, competitor_gender: Any) -> Tuple[str, str]:
    n = (class_name or "").lower()
    n_norm = re.sub(r"[^a-z0-9]+", " ", n).strip()

    female_tokens = {
        "women", "woman", "female", "femme", "femmes", "fille", "filles",
        "girl", "girls", "lady", "ladies", "damen", "dame", "feminin",
    }
    male_tokens = {
        "men", "man", "male", "homme", "hommes", "garcon", "garcons",
        "boy", "boys", "masculin",
    }

    tokens = set(n_norm.split()) if n_norm else set()
    is_junior = (
        "junior" in n_norm
        or bool(re.search(r"\bu19\b", n_norm))
        or bool(re.search(r"\bunder\s*19\b", n_norm))
    )
    is_u23 = bool(re.search(r"\bu23\b", n_norm)) or bool(re.search(r"\bunder\s*23\b", n_norm))

    if is_junior:
        category = "Junior"
    elif is_u23:
        category = "U23"
    elif "pro" in n or "elite" in n:
        category = "Elite"
    else:
        category = "Elite"

    g = str(competitor_gender or "").strip().upper()
    if g in {"2", "F", "W"} or bool(tokens & female_tokens):
        gender = "W"
    elif g in {"1", "M"} or bool(tokens & male_tokens):
        gender = "M"
    else:
        gender = "M"
    return category, gender


def map_group_id(category: str, gender: str, class_code: str) -> int:
    known = {
        ("Elite", "M"): 91,
        ("Elite", "W"): 92,
        ("U23", "M"): 93,
        ("U23", "W"): 94,
        ("Junior", "M"): 95,
        ("Junior", "W"): 96,
    }
    if (category, gender) in known:
        return known[(category, gender)]
    return 1000 + (zlib.crc32(str(class_code).encode("utf-8")) % 8000)


def map_round(phase_code: str, phase_name: str) -> Tuple[int, str]:
    pc = (phase_code or "").upper()
    pn = (phase_name or "").upper()
    # Round 1 / motos can come in multiple variants depending on organizer/language.
    if (
        pc in {"M1", "M2", "M3", "R1", "R2", "R3"}
        or "MOTO" in pn
        or "MANCHE" in pn
        or bool(re.search(r"\b(ROUND|RUNDE)\s*[123]\b", pn))
    ):
        return 1, "Round 1"
    if "LCQ" in pc or "LCQ" in pn or "LAST CHANCE" in pn:
        return 2, "LCQ"
    if "1/32" in pn or ("32" in pc and "F" in pc):
        return 3, "1/32 Finals"
    if "1/16" in pn or "16" in pc and "F" in pc:
        return 4, "1/16 Finals"
    if "1/8" in pn or "8" in pc and "F" in pc:
        return 5, "1/8 Finals"
    if pc == "4F" or "QUARTER" in pn:
        return 6, "1/4 Finals"
    if pc == "2F" or "SEMI" in pn:
        return 7, "1/2 Finals"
    if pc.startswith("1F") or "MAIN" in pn or "FINAL" in pn:
        return 8, "Final"
    return 99, phase_name or phase_code or "Round"


def parse_local_time_from_ms(ms: Any, utc_offset_minutes: Any) -> Optional[str]:
    try:
        if ms is None:
            return None
        ms_i = int(ms)
        offset = int(utc_offset_minutes or 0)
        ts = dt.datetime.utcfromtimestamp(ms_i / 1000.0) + dt.timedelta(minutes=offset)
        return ts.strftime("%H:%M:%S")
    except Exception:
        return None


def build_race_start_map(payload: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    out: Dict[Tuple[str, str], str] = {}
    utc_offset = payload.get("utcOffset")
    for r in payload.get("raceOrder", []) or []:
        pbc = str(r.get("phaseBlockCode") or "").strip()
        rn = str(r.get("raceName") or "").strip()
        st = parse_local_time_from_ms(r.get("startTime"), utc_offset)
        if pbc and rn and st:
            out[(pbc, rn)] = st
    return out


def get_nation(comp: Dict[str, Any], group_country: Dict[str, str], default_region: str) -> Optional[str]:
    gid = str(comp.get("groupId") or "").strip().upper()
    if re.match(r"^[A-Z]{3}$", gid):
        return gid
    if gid in group_country:
        return group_country[gid]
    return (default_region or "").strip().upper()[:3] or None


def stable_heat_id(class_code: str, phase_code: str, race_name: str) -> int:
    key = f"{class_code}|{phase_code}|{race_name}".encode("utf-8")
    return int(zlib.crc32(key) % 1000000) + 1


def to_int(v: Any) -> Optional[int]:
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(float(str(v)))
    except Exception:
        return None


def resolve_bib(comp: Dict[str, Any], class_code: str) -> int:
    """
    picks.bib is INTEGER NOT NULL.
    For alphanumeric plates (e.g. 11B), fall back to memberId, then deterministic hash.
    """
    plate_raw = str(comp.get("plate") or "").strip()
    bib = to_int(plate_raw)
    if bib is not None:
        return bib

    member_id = to_int(comp.get("memberId"))
    if member_id is not None:
        return member_id

    key = f"{class_code}|{plate_raw}|{comp.get('firstName') or ''}|{comp.get('lastName') or ''}"
    return 700000000 + (zlib.crc32(key.encode("utf-8")) % 200000000)


def clean_time(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s


def first_time(detail: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for k in keys:
        if k in detail:
            val = clean_time(detail.get(k))
            if val is not None:
                return val
    return None


def class_allowed(class_name: str, filters: List[str]) -> bool:
    if not class_name:
        return False
    if not filters:
        return True
    n = (class_name or "").lower()
    return any(f.lower() in n for f in filters)


def ingest_payload(conn: sqlite3.Connection, payload: Dict[str, Any], args: argparse.Namespace) -> Tuple[str, int]:
    event_summary = payload.get("eventSummary", {}) or {}
    event_name = str(event_summary.get("eventName") or payload.get("eventName") or "USA BMX Event").strip()
    date_key, date_iso = parse_event_date(event_summary.get("eventDate"))
    region = str(payload.get("regionCode") or "").upper()
    payload_event_id = str(payload.get("eventId") or event_summary.get("eventId") or "").strip()
    series_code = resolve_series_code(args)
    event_id = args.event_id or f"{date_key}_{series_code}_bmx"
    if payload_event_id:
        event_id = args.event_id or f"{date_key}_{series_code}_{payload_event_id[:8]}_bmx"

    upsert_event(
        conn,
        {
            "event_id": event_id,
            "display_name": event_name,
            "location": None,
            "country": region or None,
            "event_type": resolve_event_type(args),
            "event_date": date_iso,
            "last_seen": now_iso(),
        },
    )

    group_country: Dict[str, str] = {}
    for g in payload.get("groups", []) or []:
        gid = str(g.get("groupId") or "").strip().upper()
        gct = str(g.get("groupCountryCode") or "").strip().upper()
        if gid and gct:
            group_country[gid] = gct

    race_start_map = build_race_start_map(payload)
    seen_at = now_iso()
    rows: List[Dict[str, Any]] = []

    class_filters = [] if args.all_classes else (
        args.class_contains or ["Men Pro", "Women Pro", "Men Elite", "Women Elite"]
    )
    for cls in payload.get("classRanks", []) or []:
        class_name = str(cls.get("className") or "").strip()
        if not class_allowed(class_name, class_filters):
            continue
        class_code = str(cls.get("classCode") or cls.get("perpetualClassCode") or "").strip()
        details = cls.get("competitorRankSummaries") or []
        if not isinstance(details, list):
            continue

        for comp in details:
            first = str(comp.get("firstName") or "").strip()
            last = str(comp.get("lastName") or "").strip()
            name = f"{first} {last}".strip() or str(comp.get("name") or "").strip()
            bib = resolve_bib(comp, class_code)
            category, gender = map_category_gender(class_name, comp.get("gender"))
            group_id = map_group_id(category, gender, class_code)
            nation = get_nation(comp, group_country, region)
            uci_id = str(comp.get("memberId") or "").strip() or None

            rank_details = comp.get("competitorRankDetails") or []
            if not isinstance(rank_details, list):
                continue
            for d in rank_details:
                phase_code = str(d.get("phaseCode") or d.get("phaseBlockCode") or "").strip()
                phase_block = str(d.get("phaseBlockCode") or "").strip()
                phase_name = str(d.get("phaseName") or "").strip()
                race_name = str(d.get("raceName") or "").strip()
                if not phase_code:
                    continue
                round_key, round_title = map_round(phase_code, phase_name)
                heat_id = stable_heat_id(class_code, phase_code, race_name or phase_name)
                heat_title = f"{phase_name} {race_name}".strip() if phase_name or race_name else phase_code
                start_time_string = (
                    race_start_map.get((phase_block, race_name))
                    or race_start_map.get((phase_code, race_name))
                    or None
                )
                race_pos = to_int(d.get("racePosition"))
                rank = to_int(d.get("rank"))
                # Sqorz payloads vary by event:
                # - some provide hillTime/cornerTime/time
                # - some only provide time
                start_val = first_time(
                    d,
                    [
                        "start",
                        "startTime",
                        "reactionTime",
                        "hillTime",
                    ],
                )
                t1_val = first_time(
                    d,
                    [
                        "t1",
                        "t1Time",
                        "cornerTime",
                        "split1",
                    ],
                )
                t2_val = first_time(d, ["t2", "t2Time", "split2"])
                t3_val = first_time(d, ["t3", "t3Time", "split3"])
                tval = first_time(d, ["time", "finishTime", "resultTime"])

                rows.append(
                    {
                        "event_id": event_id,
                        "group_id": group_id,
                        "round_key": round_key,
                        "round_title": round_title,
                        "heat_id": heat_id,
                        "heat_title": heat_title,
                        "heat_status": None,
                        "start_time_string": start_time_string,
                        "bib": bib,
                        "name": name,
                        "nation": nation,
                        "pick_order": race_pos,
                        # Sqorz payload currently has race position but no lane-pick field.
                        "lane": None,
                        "lane_idx": None,
                        "uci_id": uci_id,
                        "start": start_val,
                        "t1": t1_val,
                        "t2": t2_val,
                        "t3": t3_val,
                        "t4": None,
                        "time": tval,
                        "rank": rank,
                        "seen_at": seen_at,
                    }
                )

    upsert_picks(conn, rows)
    return event_id, len(rows)


def main() -> None:
    args = parse_args()
    payload = load_payload(args)
    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    init_db(conn)
    event_id, nrows = ingest_payload(conn, payload, args)
    conn.close()
    print(f"ingested event_id={event_id} rows={nrows}")


if __name__ == "__main__":
    main()
