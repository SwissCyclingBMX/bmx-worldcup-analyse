#!/usr/bin/env python3
"""
ingest_bmx_racer.py

Poll BMX-Racer training display pages and ingest them into training_times.

The page is HTML-only (no JSON API), so we scrape the visible live table:
  https://<host>/display.php?nr=<board>
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sqlite3
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from ingest import DEFAULT_DB_PATH, init_db, now_iso, upsert_event
from ingest_jstiming import upsert_training_times

HEADERS = {"user-agent": "HeatScout/1.0"}
ALLOWED_HOSTS = {"weinfelden.bmx-racer.com"}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="BMX-Racer display URL, e.g. https://host/display.php?nr=1")
    ap.add_argument("--event-id", required=True, help="Target event_id in bmx.db")
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB path (default: bmx.db)")
    ap.add_argument("--display-name", default="", help="Optional display name stored in events")
    ap.add_argument("--location", default="", help="Optional location stored in events")
    ap.add_argument("--country", default="", help="Optional country code stored in events")
    return ap.parse_args()


def fetch_html(url: str) -> str:
    host = (urlparse(url).hostname or "").strip().lower()
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(
            f"Unsupported BMX-Racer host '{host}'. This mapper is currently only valid for Weinfelden."
        )
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_live_table(html_text: str) -> str:
    m = re.search(
        r'<table[^>]+class="tableScoreLive"[^>]*>(.*?)</table>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        raise RuntimeError("Could not find tableScoreLive in BMX-Racer page")
    return m.group(1)


def extract_rows(table_html: str) -> List[List[str]]:
    out: List[List[str]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL)
        cleaned: List[str] = []
        for cell in cells:
            text = re.sub(r"<[^>]+>", " ", cell)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            cleaned.append(text)
        if cleaned:
            out.append(cleaned)
    return out


def parse_board_id(url: str) -> str:
    parsed = urlparse(url)
    nr = parse_qs(parsed.query).get("nr", [""])[0].strip()
    return nr or "1"


def infer_location(url: str, explicit_location: str = "") -> str:
    if explicit_location.strip():
        return explicit_location.strip()
    host = (urlparse(url).hostname or "").strip().lower()
    label = host.split(".", 1)[0] if host else ""
    if not label:
        return "BMX Racer"
    return label.replace("-", " ").title()


def parse_timestamp_cell(raw: str) -> Tuple[Optional[str], Optional[str]]:
    text = str(raw or "").strip()
    if not text:
        return None, None
    m = re.search(r"(\d{2})\.(\d{2})\.\s*\|\s*(\d{2}:\d{2})", text)
    if not m:
        return None, None
    day, month, hhmm = m.groups()
    now = dt.datetime.now()
    year = now.year
    try:
        candidate = dt.datetime(year, int(month), int(day))
        # If the board still shows late-December rows in early January, avoid future dates.
        if candidate.date() > now.date() + dt.timedelta(days=30):
            candidate = dt.datetime(year - 1, int(month), int(day))
    except Exception:
        return None, None
    return candidate.strftime("%Y-%m-%d"), f"{hhmm}:00"


def parse_gate(raw: str) -> Tuple[str, Optional[int]]:
    text = str(raw or "").strip()
    m = re.search(r"(\d+)", text)
    if not m:
        return text or "Gate", None
    gate_num = int(m.group(1))
    return f"Gate Nr - {gate_num}", gate_num


def norm_time(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return text
    return None


def safe_delta(later: Optional[str], earlier: Optional[str]) -> Optional[str]:
    try:
        if later is None or earlier is None:
            return None
        val = round(float(later) - float(earlier), 3)
        if val < 0:
            return None
        return f"{val:.3f}".rstrip("0").rstrip(".")
    except Exception:
        return None


def parse_training_rows(rows: List[List[str]], event_id: str, source_file: str) -> Tuple[List[Dict[str, object]], Optional[str]]:
    out: List[Dict[str, object]] = []
    latest_event_date: Optional[str] = None
    current_date: Optional[str] = None
    current_start_time: Optional[str] = None
    current_block_label: Optional[str] = None
    current_block_id: Optional[str] = None
    ingested_at = now_iso()

    for cells in rows[1:]:
        if len(cells) < 2:
            continue
        first = cells[0].strip()
        second = cells[1].strip().upper()

        if second == "GATE":
            current_date, current_start_time = parse_timestamp_cell(first)
            current_block_label = first
            current_block_id = "|".join(
                [
                    "bmxracer",
                    str(event_id or "").strip(),
                    str(source_file or "").strip(),
                    str(current_date or "").strip(),
                    str(current_start_time or "").strip(),
                ]
            )
            if current_date:
                latest_event_date = max(latest_event_date or current_date, current_date)
            continue

        if len(cells) < 7:
            continue

        gate_label, bib = parse_gate(cells[0])
        name = cells[1].strip()
        if not name:
            continue

        kink = norm_time(cells[2])
        interim = norm_time(cells[4])
        total = norm_time(cells[6])
        bottom = safe_delta(interim, kink)
        t1_in = safe_delta(total, interim)

        row = {
            "event_id": event_id,
            "category": "",
            "bib": bib,
            "name": name,
            "nation": None,
            "gate": gate_label,
            "kink": kink,
            "bottom": bottom,
            "interim": interim,
            "t1_in": t1_in,
            "total": total,
            "training_block_id": current_block_id or f"bmxracer|{event_id}|{source_file}|{gate_label}",
            "training_block_label": current_block_label or gate_label,
            "training_block_time": current_start_time,
            # Canonical app semantics: start = Startzeit, t1 = T1/Gesamt.
            "start": interim,
            "t1": total,
            "source_kind": "bmxracer",
            "source_file": source_file,
            "ingested_at": ingested_at,
        }
        # Keep a stable event_date even when only archive rows are shown.
        if current_date:
            latest_event_date = max(latest_event_date or current_date, current_date)
        # Store the run timestamp in gate text when present so it remains inspectable.
        if current_start_time:
            row["gate"] = f"{gate_label} | {current_start_time}"
        out.append(row)

    return out, latest_event_date


def build_event_meta(args: argparse.Namespace, event_date: Optional[str]) -> Dict[str, Optional[str]]:
    location = infer_location(args.url, args.location)
    display_name = args.display_name.strip() or f"{location} BMX-Racer Training"
    return {
        "event_id": args.event_id,
        "display_name": display_name,
        "location": location,
        "country": args.country.strip().upper()[:3] or None,
        "event_type": "Other",
        "event_date": event_date,
        "last_seen": now_iso(),
    }


def ingest_once(args: argparse.Namespace) -> int:
    text = fetch_html(args.url)
    table_html = extract_live_table(text)
    rows = extract_rows(table_html)
    source_file = f"display.php?nr={parse_board_id(args.url)}"
    training_rows, event_date = parse_training_rows(rows, args.event_id, source_file)

    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute('PRAGMA busy_timeout = 60000')
    try:
        init_db(conn)
        upsert_event(conn, build_event_meta(args, event_date))
        inserted = upsert_training_times(conn, training_rows)
        conn.commit()
    finally:
        conn.close()
    return inserted


def main() -> int:
    args = parse_args()
    inserted = ingest_once(args)
    print(f"[bmx_racer] rows processed: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
