#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import requests

from ingest import init_db, upsert_event, upsert_picks, now_iso


ALLOWED_CLASSES = {"ME", "WE", "MU", "WU", "MJ", "WJ"}

CLASS_TO_GROUP = {
    "ME": 91,
    "WE": 92,
    "MU": 93,
    "WU": 94,
    "MJ": 95,
    "WJ": 96,
}

ROUND_SLUG_TO_TITLE = {
    "round-1": "Round 1",
    "moto-1round-1": "Round 1",
    "lcq": "LCQ",
    "18-finals": "1/8 Finals",
    "14-finals": "1/4 Finals",
    "12-finals": "1/2 Finals",
    "finals": "Final",
    "overall": "Overall",
    "gate-practice": "Gate practice",
    "overall-times": "Overall times",
}

ROUND_SLUG_TO_KEY = {
    "round-1": 1,
    "moto-1round-1": 1,
    "lcq": 2,
    "18-finals": 3,
    "14-finals": 4,
    "12-finals": 5,
    "finals": 6,
}


def parse_date(s: str) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    # format: DD-MM-YYYY
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", s)
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{y}{mth}{d}"


def clean_name(name: str) -> str:
    if not name:
        return ""
    n = name.strip()
    n = re.sub(r"^UEC BMX\s+", "", n, flags=re.IGNORECASE)
    n = re.sub(r"\s*\((morning|afternoon)\)\s*$", "", n, flags=re.IGNORECASE)
    return n


def infer_series(name: str) -> str:
    n = (name or "").lower()
    if "european championships" in n or "european championship" in n:
        return "em"
    if "european cup" in n:
        return "euc"
    return "uec"


def build_event_id(date_yyyymmdd: str, series_code: str, used: Dict[str, int]) -> str:
    base = f"{date_yyyymmdd}_{series_code}_bmx"
    if base not in used:
        used[base] = 1
        return base
    used[base] += 1
    return f"{base}_{used[base]}"


def extract_uci_id(raw: str) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"(\d{8,})", str(raw))
    return m.group(1) if m else None


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM events WHERE event_id = ? LIMIT 1", (event_id,))
    return cur.fetchone() is not None


def _extract_attr_payload(text: str, attr: str) -> Optional[Dict[str, Any]]:
    for quote in ['"', "'"]:
        token = f"{attr}={quote}"
        start = text.find(token)
        if start != -1:
            start += len(token)
            end = text.find(quote, start)
            if end != -1:
                data = html.unescape(text[start:end])
                return json.loads(data)
    return None


def fetch_event_payload(url: str) -> Dict[str, Any]:
    headers = {"X-Inertia": "true", "X-Requested-With": "XMLHttpRequest"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    if "application/json" in r.headers.get("Content-Type", ""):
        return r.json()
    # fallback: parse data-page from HTML
    text = r.text
    # try to parse payload from HTML
    for attr in ["data-page", "data-payload"]:
        payload = _extract_attr_payload(text, attr)
        if payload:
            return payload
    # last resort: try to find JSON object in text
    for pat in [
        r'({"component":.*?"props":.*?})\\s*</script>',
        r'({"component":.*?"props":.*?})',
    ]:
        m = re.search(pat, text, re.S)
        if m:
            return json.loads(m.group(1))
    raise RuntimeError(f"Could not parse JSTiming payload for {url}")


def extract_props(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "props" in payload:
        return payload["props"]
    if "view" in payload and "properties" in payload["view"]:
        return payload["view"]["properties"]
    if "view" in payload and "props" in payload["view"]:
        return payload["view"]["props"]
    return payload


def make_event_meta(props: Dict[str, Any], used_ids: Dict[str, int]) -> Tuple[str, str, str, str, str]:
    event = props.get("event", {}) or {}
    name = clean_name(event.get("name", ""))
    city = event.get("city", "") or ""
    ioc = (event.get("ioc_code", "") or "").upper()
    date_raw = event.get("start_date") or event.get("end_date") or ""
    date_yyyymmdd = parse_date(date_raw) or dt.date.today().strftime("%Y%m%d")
    series_code = infer_series(name)
    event_id = build_event_id(date_yyyymmdd, series_code, used_ids)
    display_name = name if name else f"{series_code.upper()} Event"
    return event_id, display_name, city, date_yyyymmdd, ioc


def round_title_from_slug(slug: str, fallback: str) -> str:
    if slug in ROUND_SLUG_TO_TITLE:
        return ROUND_SLUG_TO_TITLE[slug]
    return fallback or "Round"


def upsert_training_times(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
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
          PRIMARY KEY (event_id, category, bib, name, gate, start, t1, source_file)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_training_event ON training_times(event_id)")
    conn.executemany(
        """
        INSERT OR IGNORE INTO training_times (
          event_id, category, bib, name, nation, gate, start, t1, source_file, ingested_at
        ) VALUES (
          :event_id, :category, :bib, :name, :nation, :gate, :start, :t1, :source_file, :ingested_at
        )
        """,
        rows,
    )
    return len(rows)


def parse_riders(heat: Dict[str, Any]) -> List[Dict[str, Any]]:
    riders = heat.get("riders") or []
    out = []
    for r in riders:
        add = r.get("additional_columns") or {}
        out.append(
            {
                "bib": r.get("plate"),
                "name": (r.get("name") or "").strip(),
                "nation": (r.get("ioc_code") or "").upper(),
                "uci_id": extract_uci_id(r.get("id") or ""),
                "rank": r.get("rank"),
                "result": r.get("result"),
                "start": add.get("c11") or add.get("c11_cname"),
                "t1": add.get("c12") or add.get("c12_cname"),
                "finish": add.get("c14") or add.get("c14_cname"),
            }
        )
    return out


def ingest_race_event(conn: sqlite3.Connection, url: str, event_id: str) -> int:
    payload = fetch_event_payload(url)
    props = extract_props(payload)
    round_slug = (props.get("activeRoundSlug") or "").strip()
    round_title = round_title_from_slug(round_slug, props.get("activeRoundName"))
    if round_slug == "overall":
        return 0
    round_key = ROUND_SLUG_TO_KEY.get(round_slug, 0)
    heats = props.get("heats") or []
    rows = []
    for h in heats:
        class_code = (h.get("class_code") or "").strip()
        if class_code not in ALLOWED_CLASSES:
            continue
        group_id = CLASS_TO_GROUP.get(class_code)
        heat_id = h.get("id")
        heat_title = h.get("name") or f"Heat {heat_id}"
        heat_status = h.get("is_live") or ""
        start_time_string = h.get("result_time") or ""
        for r in parse_riders(h):
            if not r["name"]:
                continue
            time_val = r["finish"] or r["result"]
            rows.append(
                {
                    "event_id": event_id,
                    "group_id": group_id,
                    "round_key": round_key,
                    "round_title": round_title,
                    "heat_id": int(heat_id) if str(heat_id).isdigit() else 0,
                    "heat_title": heat_title,
                    "heat_status": heat_status,
                    "start_time_string": start_time_string,
                    "bib": int(r["bib"]) if str(r["bib"]).strip().isdigit() else None,
                    "name": r["name"],
                    "nation": r["nation"],
                    "pick_order": None,
                    "lane": None,
                    "lane_idx": None,
                    "uci_id": r["uci_id"],
                    "start": r["start"].strip() if isinstance(r["start"], str) else r["start"],
                    "t1": r["t1"].strip() if isinstance(r["t1"], str) else r["t1"],
                    "t2": None,
                    "t3": None,
                    "t4": None,
                    "time": time_val.strip() if isinstance(time_val, str) else time_val,
                    "rank": r["rank"],
                    "seen_at": now_iso(),
                }
            )
    if rows:
        upsert_picks(conn, rows)
    return len(rows)


def ingest_training_event(conn: sqlite3.Connection, url: str, event_id: str) -> int:
    payload = fetch_event_payload(url)
    props = extract_props(payload)
    heats = props.get("heats") or []
    rows = []
    for h in heats:
        class_code = (h.get("class_code") or "").strip()
        # keep all classes in training (will be filtered later)
        heat_name = h.get("name") or ""
        for r in parse_riders(h):
            if not r["name"]:
                continue
            rows.append(
                {
                    "event_id": event_id,
                    "category": class_code,
                    "bib": int(r["bib"]) if str(r["bib"]).strip().isdigit() else None,
                    "name": r["name"],
                    "nation": r["nation"],
                    "gate": heat_name,
                    "start": r["start"].strip() if isinstance(r["start"], str) else r["start"],
                    "t1": r["t1"].strip() if isinstance(r["t1"], str) else r["t1"],
                    "source_file": props.get("activeRoundSlug") or "gate-practice",
                    "ingested_at": now_iso(),
                }
            )
    return upsert_training_times(conn, rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="bmx.db")
    parser.add_argument("--race", action="append", default=[], help="Race round-1 URL (one per event)")
    parser.add_argument("--training", action="append", default=[], help="Gate practice URL")
    args = parser.parse_args()

    if not args.race and not args.training:
        raise SystemExit("Provide --race and/or --training URLs")

    conn = sqlite3.connect(args.db)
    init_db(conn)

    used_ids: Dict[str, int] = {}
    race_meta: List[Dict[str, Any]] = []

    # Ingest race events (all rounds)
    for race_url in args.race:
        try:
            payload = fetch_event_payload(race_url)
        except Exception as e:
            raise RuntimeError(f"Failed to load {race_url}: {e}") from e
        props = extract_props(payload)
        event_id, display_name, city, date_yyyymmdd, country = make_event_meta(props, used_ids)
        upsert_event(
            conn,
            {
                "event_id": event_id,
                "display_name": display_name,
                "location": city,
                "country": country,
                "event_date": f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}",
                "last_seen": now_iso(),
            },
        )
        race_meta.append(
            {
                "event_id": event_id,
                "city": (city or "").lower(),
                "date": date_yyyymmdd,
                "display_name": display_name,
            }
        )
        # generate round urls from base
        base = race_url.rsplit("/", 1)[0]
        slugs = ["round-1", "lcq", "18-finals", "14-finals", "12-finals", "finals"]
        if "moto-1round-1" in race_url:
            slugs[0] = "moto-1round-1"
        for slug in slugs:
            url = f"{base}/{slug}"
            try:
                ingest_race_event(conn, url, event_id)
            except Exception:
                continue

    # Ingest training (gate practice) and link to nearest race by city/date
    for train_url in args.training:
        payload = fetch_event_payload(train_url)
        props = extract_props(payload)
        event_id, display_name, city, date_yyyymmdd, country = make_event_meta(props, used_ids)
        # link to closest race event by city/date if possible
        linked_event_id = event_id
        if race_meta and city:
            city_l = city.lower()
            candidates = [m for m in race_meta if m["city"] == city_l]
            if candidates:
                # choose closest date
                def _dist(m):
                    return abs(int(m["date"]) - int(date_yyyymmdd))
                candidates.sort(key=_dist)
                linked_event_id = candidates[0]["event_id"]
        if not event_exists(conn, linked_event_id):
            upsert_event(
                conn,
                {
                    "event_id": linked_event_id,
                    "display_name": display_name,
                    "location": city,
                    "country": country,
                    "event_date": f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}",
                    "last_seen": now_iso(),
                },
            )
        else:
            conn.execute("UPDATE events SET last_seen = ? WHERE event_id = ?", (now_iso(), linked_event_id))
        ingest_training_event(conn, train_url, linked_event_id)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
