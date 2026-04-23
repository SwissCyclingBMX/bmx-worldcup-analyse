#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import requests

from ingest import init_db, normalize_event_type, upsert_event, upsert_picks, now_iso


DEFAULT_ALLOWED_CLASSES = {"ME", "WE", "MU", "WU", "MJ", "WJ"}

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
    "116-finals": "1/16 Finals",
    "16-finals": "1/16 Finals",
    "1-16-finals": "1/16 Finals",
    "1/16-finals": "1/16 Finals",
    "18-finals": "1/8 Finals",
    "1-8-finals": "1/8 Finals",
    "1/8-finals": "1/8 Finals",
    "14-finals": "1/4 Finals",
    "1-4-finals": "1/4 Finals",
    "1/4-finals": "1/4 Finals",
    "12-finals": "1/2 Finals",
    "1-2-finals": "1/2 Finals",
    "1/2-finals": "1/2 Finals",
    "finals": "Final",
    "overall": "Overall",
    "gate-practice": "Gate practice",
    "overall-times": "Overall times",
}

ROUND_SLUG_TO_KEY = {
    "round-1": 1,
    "moto-1round-1": 1,
    "lcq": 2,
    "116-finals": 3,
    "16-finals": 3,
    "1-16-finals": 3,
    "1/16-finals": 3,
    "18-finals": 4,
    "1-8-finals": 4,
    "1/8-finals": 4,
    "14-finals": 5,
    "1-4-finals": 5,
    "1/4-finals": 5,
    "12-finals": 6,
    "1-2-finals": 6,
    "1/2-finals": 6,
    "finals": 7,
}

CANONICAL_ROUND_SLUGS = [
    "round-1",
    "moto-1round-1",
    "lcq",
    "116-finals",
    "16-finals",
    "1-16-finals",
    "18-finals",
    "1-8-finals",
    "14-finals",
    "1-4-finals",
    "12-finals",
    "1-2-finals",
    "finals",
]


def _walk_values(node: Any):
    if isinstance(node, dict):
        for v in node.values():
            yield v
            yield from _walk_values(v)
    elif isinstance(node, list):
        for v in node:
            yield v
            yield from _walk_values(v)


def normalize_slug(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    s = s.split("#", 1)[0].split("?", 1)[0].strip()
    s = s.strip("/")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    s = s.replace("_", "-").strip().lower()
    return s


def split_event_root_and_seed_slug(url: str) -> Tuple[str, str]:
    raw = (url or "").strip()
    if not raw:
        return "", ""
    cleaned = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    m = re.match(r"^(https?://[^/]+/event/[^/]+)(?:/([^/]+))?$", cleaned, flags=re.IGNORECASE)
    if m:
        return m.group(1), normalize_slug(m.group(2) or "")
    return cleaned.rsplit("/", 1)[0], normalize_slug(cleaned.rsplit("/", 1)[-1])


def event_uuid_from_props(props: Dict[str, Any]) -> str:
    event = props.get("event", {}) or {}
    return str(event.get("uuid") or "").strip().lower()


def is_overall_slug(slug: str) -> bool:
    s = normalize_slug(slug)
    return s in {"overall", "overall-times"}


def discover_round_slugs(props: Dict[str, Any], seed_slug: str = "") -> List[str]:
    found: List[str] = []
    seen = set()

    def add_slug(raw: str) -> None:
        s = normalize_slug(raw)
        if not s:
            return
        if s not in seen:
            seen.add(s)
            found.append(s)

    if seed_slug:
        add_slug(seed_slug)

    for raw in _walk_values(props):
        if not isinstance(raw, str):
            continue
        s = normalize_slug(raw)
        if not s:
            continue
        if s in ROUND_SLUG_TO_TITLE:
            add_slug(s)
            continue
        # Parse candidate slug tokens from longer strings/URLs.
        for tok in re.findall(
            r"(moto-\d+round-\d+|round-\d+|lcq|1[/\-]?16-finals|16-finals|1[/\-]?8-finals|18-finals|1[/\-]?4-finals|14-finals|1[/\-]?2-finals|12-finals|finals|overall-times|overall)",
            s,
            flags=re.IGNORECASE,
        ):
            add_slug(tok)

    for s in CANONICAL_ROUND_SLUGS:
        add_slug(s)
    return found


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


def infer_event_type_label(name: str) -> str:
    return {
        "em": "EM",
        "euc": "EC",
        "uec": "Other",
    }.get(infer_series(name), "Other")


def event_type_to_series_code(event_type: Optional[str], fallback: str) -> str:
    event_type_norm = normalize_event_type(event_type)
    mapping = {
        "WC": "wc",
        "WM": "wch",
        "EC": "euc",
        "EM": "em",
        "USABMX": "usap",
        "FFC": "ffc",
        "SCC": "scc",
        "Other": "other",
    }
    return mapping.get(event_type_norm or "", fallback)


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


def class_to_cat_gender(code: str) -> Tuple[Optional[str], Optional[str]]:
    code = (code or "").strip().upper()
    if code == "ME":
        return "Elite", "M"
    if code == "WE":
        return "Elite", "W"
    if code == "MU":
        return "U23", "M"
    if code == "WU":
        return "U23", "W"
    if code == "MJ":
        return "Junior", "M"
    if code == "WJ":
        return "Junior", "W"
    return None, None


def race_class_allowed(code: str, include_all_classes: bool = False) -> bool:
    class_code = (code or "").strip().upper()
    if not class_code:
        return False
    if include_all_classes:
        return True
    return class_code in DEFAULT_ALLOWED_CLASSES


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM events WHERE event_id = ? LIMIT 1", (event_id,))
    return cur.fetchone() is not None


def _is_training_like_name(name: str) -> bool:
    text = (name or "").strip().lower()
    return any(token in text for token in ["practice", "training", "gate practice"])


def find_linked_race_event(
    conn: sqlite3.Connection,
    city: str,
    date_yyyymmdd: str,
    exclude_event_id: str = "",
) -> Optional[str]:
    city_l = (city or "").strip().lower()
    if not city_l:
        return None

    rows = conn.execute(
        """
        SELECT e.event_id, e.display_name, e.event_date,
               EXISTS(SELECT 1 FROM picks p WHERE p.event_id = e.event_id LIMIT 1) AS has_picks
        FROM events e
        WHERE lower(coalesce(e.location, '')) = ?
          AND e.event_id <> ?
        """,
        (city_l, exclude_event_id or ""),
    ).fetchall()

    candidates: List[Tuple[int, int, str]] = []
    for event_id, display_name, event_date, has_picks in rows:
        if _is_training_like_name(display_name or ""):
            continue
        if not has_picks:
            continue
        date_digits = re.sub(r"\D+", "", str(event_date or ""))[:8]
        if not (date_digits.isdigit() and date_yyyymmdd.isdigit()):
            continue
        dist = abs(int(date_digits) - int(date_yyyymmdd))
        # cap to nearby event weekends; ignore distant same-city events
        if dist > 3:
            continue
        future_bias = 0 if int(date_digits) >= int(date_yyyymmdd) else 1
        candidates.append((dist, future_bias, str(event_id)))

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][2]


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


def make_event_meta(
    props: Dict[str, Any],
    used_ids: Dict[str, int],
    event_type_override: Optional[str] = None,
) -> Tuple[str, str, str, str, str, str]:
    event = props.get("event", {}) or {}
    name = clean_name(event.get("name", ""))
    city = event.get("city", "") or ""
    ioc = (event.get("ioc_code", "") or "").upper()
    date_raw = event.get("start_date") or event.get("end_date") or ""
    date_yyyymmdd = parse_date(date_raw) or dt.date.today().strftime("%Y%m%d")
    inferred_series_code = infer_series(name)
    event_type = normalize_event_type(event_type_override) or infer_event_type_label(name)
    series_code = event_type_to_series_code(event_type, inferred_series_code)
    event_id = build_event_id(date_yyyymmdd, series_code, used_ids)
    display_name = name if name else f"{series_code.upper()} Event"
    return event_id, display_name, city, date_yyyymmdd, ioc, event_type


def round_title_from_slug(slug: str, fallback: str) -> str:
    s = normalize_slug(slug)
    if s in ROUND_SLUG_TO_TITLE:
        return ROUND_SLUG_TO_TITLE[s]
    return fallback or "Round"


def round_key_and_title(slug: str, fallback: str) -> Tuple[int, str]:
    s = normalize_slug(slug)
    fb = (fallback or "").strip()
    text = f"{s} {fb}".lower()

    if is_overall_slug(s) or "overall times" in text:
        return 0, "Overall"
    if s == "gate-practice" or "gate practice" in text:
        return 0, "Gate practice"

    if re.search(r"\bround[\s\-]*1\b|\bmoto[\s\-]*1round[\s\-]*1\b", text):
        return 1, "Round 1"
    if "lcq" in text or "last chance" in text:
        return 2, "LCQ"
    if re.search(r"1[/\-\s]?16|16[\s\-]*final", text):
        return 3, "1/16 Finals"
    if re.search(r"1[/\-\s]?8|8[\s\-]*final|eighth", text):
        return 4, "1/8 Finals"
    if re.search(r"1[/\-\s]?4|quarter", text):
        return 5, "1/4 Finals"
    if re.search(r"1[/\-\s]?2|semi", text):
        return 6, "1/2 Finals"
    if "final" in text:
        return 7, "Final"

    if s in ROUND_SLUG_TO_KEY:
        return ROUND_SLUG_TO_KEY[s], round_title_from_slug(s, fb)
    return 0, (fb or "Round")


def upsert_training_times(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    def _sig(row: Dict[str, Any]) -> tuple:
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
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS training_times (
          event_id TEXT NOT NULL,
          category TEXT,
          bib INTEGER,
          name TEXT,
          nation TEXT,
          gate TEXT,
          kink TEXT,
          bottom TEXT,
          interim TEXT,
          t1_in TEXT,
          total TEXT,
          training_block_id TEXT,
          training_block_label TEXT,
          training_block_time TEXT,
          start TEXT,
          t1 TEXT,
          source_kind TEXT,
          source_file TEXT,
          ingested_at TEXT NOT NULL,
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
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_training_event_dedup
        ON training_times(event_id, category, bib, name, nation, gate, start, t1)
        """
    )
    event_ids = sorted({str(r.get("event_id") or "").strip() for r in rows if str(r.get("event_id") or "").strip()})
    existing_sigs = set()
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        for existing_row in conn.execute(
            f"""
            SELECT event_id, category, bib, name, nation, gate, start, t1, source_file
            FROM training_times
            WHERE event_id IN ({placeholders})
            """,
            event_ids,
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
                )
            )
    insert_rows: List[Dict[str, Any]] = []
    batch_sigs = set()
    for row in rows:
        sig = _sig(row)
        if sig in existing_sigs or sig in batch_sigs:
            continue
        insert_rows.append(row)
        batch_sigs.add(sig)
    conn.executemany(
        """
        INSERT OR IGNORE INTO training_times (
          event_id, category, bib, name, nation, gate,
          kink, bottom, interim, t1_in, total,
          training_block_id, training_block_label, training_block_time,
          start, t1, source_kind, source_file, ingested_at
        ) VALUES (
          :event_id, :category, :bib, :name, :nation, :gate,
          :kink, :bottom, :interim, :t1_in, :total,
          :training_block_id, :training_block_label, :training_block_time,
          :start, :t1, :source_kind, :source_file, :ingested_at
        )
        """,
        insert_rows,
    )
    return len(insert_rows)


def upsert_master_results(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS master_results (
          uci_event_id TEXT NOT NULL,
          uci_id TEXT NOT NULL,
          bib INTEGER NOT NULL,
          last_name TEXT,
          first_name TEXT,
          gender TEXT NOT NULL,
          category TEXT NOT NULL,
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
        """
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO master_results (
          uci_event_id, uci_id, bib, last_name, first_name, gender, category, klasse,
          year, date, location, track, host_nation, rank, time, irm, source
        ) VALUES (
          :uci_event_id, :uci_id, :bib, :last_name, :first_name, :gender, :category, :klasse,
          :year, :date, :location, :track, :host_nation, :rank, :time, :irm, :source
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
        start_val = add.get("c11")
        t1_val = add.get("c12")
        finish_val = add.get("c14")
        # fallback to result if splits missing (common in gate practice)
        res_val = r.get("result")
        if start_val in (None, "") and res_val not in (None, ""):
            start_val = res_val
        if finish_val in (None, "") and res_val not in (None, ""):
            finish_val = res_val
        out.append(
            {
                "bib": r.get("plate"),
                "name": (r.get("name") or "").strip(),
                "nation": (r.get("ioc_code") or "").upper(),
                "uci_id": extract_uci_id(r.get("id") or ""),
                "rank": r.get("rank"),
                "result": r.get("result"),
                "start": start_val,
                "t1": t1_val,
                "finish": finish_val,
            }
        )
    return out


def ingest_race_props(
    conn: sqlite3.Connection,
    props: Dict[str, Any],
    event_id: str,
    include_all_classes: bool = False,
) -> int:
    round_slug = normalize_slug(str(props.get("activeRoundSlug") or ""))
    round_key, round_title = round_key_and_title(round_slug, str(props.get("activeRoundName") or ""))
    if round_key == 0 and round_title == "Overall":
        return 0
    heats = props.get("heats") or []
    rows = []
    for h in heats:
        class_code = (h.get("class_code") or "").strip()
        if not race_class_allowed(class_code, include_all_classes=include_all_classes):
            continue
        group_id = CLASS_TO_GROUP.get(class_code)
        heat_id = h.get("id")
        heat_title = h.get("name") or f"Heat {heat_id}"
        heat_status = h.get("is_live") or ""
        start_time_string = h.get("result_time") or ""
        heat_rows_before = len(rows)
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
        if len(rows) == heat_rows_before:
            # Keep upcoming heats visible even when JSTiming exposes only the heat shell
            # (no riders/results yet) on future-round pages like LCQ.
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
                    "bib": 0,
                    "name": "",
                    "nation": "",
                    "pick_order": None,
                    "lane": None,
                    "lane_idx": None,
                    "uci_id": None,
                    "start": None,
                    "t1": None,
                    "t2": None,
                    "t3": None,
                    "t4": None,
                    "time": None,
                    "rank": None,
                    "seen_at": now_iso(),
                }
            )
    if rows:
        upsert_picks(conn, rows)
    return len(rows)


def ingest_race_event(conn: sqlite3.Connection, url: str, event_id: str) -> int:
    payload = fetch_event_payload(url)
    props = extract_props(payload)
    return ingest_race_props(conn, props, event_id)


def ingest_training_props(conn: sqlite3.Connection, props: Dict[str, Any], event_id: str) -> int:
    heats = props.get("heats") or []
    rows = []
    source_file = props.get("activeRoundSlug") or "gate-practice"
    for h in heats:
        class_code = (h.get("class_code") or "").strip()
        # keep all classes in training (will be filtered later)
        heat_name = h.get("name") or ""
        explicit_block_time = ""
        for cand in [h.get("result_time"), h.get("start_time"), heat_name, source_file]:
            s = str(cand or "").strip()
            m = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", s)
            if m:
                explicit_block_time = m.group(1)
                break
        block_label = heat_name.strip() or explicit_block_time or str(source_file)
        block_id = "|".join(
            [
                "jstiming",
                str(event_id or "").strip(),
                str(source_file or "").strip(),
                str(class_code or "").strip(),
                str(block_label or "").strip(),
            ]
        )
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
                    "kink": None,
                    "bottom": None,
                    "interim": None,
                    "t1_in": None,
                    "total": None,
                    "training_block_id": block_id,
                    "training_block_label": block_label,
                    "training_block_time": explicit_block_time,
                    "start": r["start"].strip() if isinstance(r["start"], str) else r["start"],
                    "t1": r["t1"].strip() if isinstance(r["t1"], str) else r["t1"],
                    "source_kind": "jstiming",
                    "source_file": source_file,
                    "ingested_at": now_iso(),
                }
            )
    return upsert_training_times(conn, rows)


def ingest_training_event(conn: sqlite3.Connection, url: str, event_id: str) -> int:
    payload = fetch_event_payload(url)
    props = extract_props(payload)
    return ingest_training_props(conn, props, event_id)


def ingest_overall_event(conn: sqlite3.Connection, url: str, event_id: str) -> int:
    payload = fetch_event_payload(url)
    props = extract_props(payload)
    event = props.get("event", {}) or {}
    name = clean_name(event.get("name", ""))
    city = event.get("city", "") or ""
    ioc = (event.get("ioc_code", "") or "").upper()
    date_raw = event.get("start_date") or event.get("end_date") or ""
    date_yyyymmdd = parse_date(date_raw) or ""
    year = int(date_yyyymmdd[:4]) if date_yyyymmdd[:4].isdigit() else None

    heats = props.get("heats") or []
    rows = []
    for h in heats:
        class_code = (h.get("class_code") or "").strip().upper()
        if class_code not in DEFAULT_ALLOWED_CLASSES:
            continue
        category, gender = class_to_cat_gender(class_code)
        for r in (h.get("riders") or []):
            full_name = (r.get("name") or "").strip()
            if not full_name:
                continue
            bib = r.get("plate")
            uci_id = extract_uci_id(r.get("id") or "") or ""
            rank = r.get("rank")
            result = r.get("result")
            rows.append(
                {
                    "uci_event_id": event_id,
                    "uci_id": uci_id,
                    "bib": int(bib) if str(bib).strip().isdigit() else 0,
                    "last_name": "",
                    "first_name": full_name,
                    "gender": gender or "",
                    "category": category or "",
                    "klasse": "EM" if "european championship" in name.lower() else "EC",
                    "year": year,
                    "date": date_yyyymmdd,
                    "location": city,
                    "track": "",
                    "host_nation": ioc,
                    "rank": int(rank) if str(rank).strip().isdigit() else None,
                    "time": result,
                    "irm": "",
                    "source": "jstiming_overall",
                }
            )
    return upsert_master_results(conn, rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="bmx.db")
    parser.add_argument("--race", action="append", default=[], help="Race round-1 URL (one per event)")
    parser.add_argument("--training", action="append", default=[], help="Gate practice URL")
    parser.add_argument(
        "--event-type",
        default="",
        help="Optional Wettkampf Typ override: WC | WM | EC | EM | USABMX | FFC | SCC | Other",
    )
    parser.add_argument(
        "--all-classes",
        action="store_true",
        help="Ingest all JSTiming race classes into picks (for archive/backfill use).",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logs for round discovery/fallbacks")
    args = parser.parse_args()

    if not args.race and not args.training:
        raise SystemExit("Provide --race and/or --training URLs")

    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
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
        event_id, display_name, city, date_yyyymmdd, country, event_type = make_event_meta(
            props,
            used_ids,
            event_type_override=args.event_type,
        )
        base, seed_slug = split_event_root_and_seed_slug(race_url)
        upsert_event(
            conn,
            {
                "event_id": event_id,
                "display_name": display_name,
                "location": city,
                "country": country,
                "event_type": event_type,
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
                "event_uuid": event_uuid_from_props(props),
                "event_root": base,
            }
        )

        # ingest currently loaded round first (already fetched)
        ingest_race_props(conn, props, event_id, include_all_classes=args.all_classes)

        # discover round slugs dynamically, then fallback to canonical slugs
        slugs = discover_round_slugs(props, seed_slug=seed_slug)
        seen_round_slugs = {normalize_slug(str(props.get("activeRoundSlug") or seed_slug))}
        for slug in slugs:
            if is_overall_slug(slug):
                continue
            if normalize_slug(slug) in seen_round_slugs:
                continue
            url = f"{base}/{slug}"
            try:
                payload_round = fetch_event_payload(url)
                props_round = extract_props(payload_round)
                n_rows = ingest_race_props(conn, props_round, event_id, include_all_classes=args.all_classes)
                if n_rows > 0:
                    seen_round_slugs.add(normalize_slug(slug))
            except Exception as e:
                if args.verbose:
                    print(f"[jstiming] round fetch failed event={event_id} slug={slug}: {e}")
                continue

        # overall results (final ranks): try discovered overall-like slugs first, fallback to /overall
        overall_candidates = []
        for s in slugs:
            if is_overall_slug(s):
                overall_candidates.append(f"{base}/{s}")
        overall_candidates.append(f"{base}/overall")

        tried = set()
        for overall_url in overall_candidates:
            if overall_url in tried:
                continue
            tried.add(overall_url)
            try:
                n = ingest_overall_event(conn, overall_url, event_id)
                if n > 0:
                    break
            except Exception as e:
                if args.verbose:
                    print(f"[jstiming] overall fetch failed event={event_id} url={overall_url}: {e}")
                continue

    # Ingest training (gate practice) and link to nearest race by city/date
    for train_url in args.training:
        payload = fetch_event_payload(train_url)
        props = extract_props(payload)
        event_id, display_name, city, date_yyyymmdd, country, event_type = make_event_meta(
            props,
            used_ids,
            event_type_override=args.event_type,
        )
        train_root, _ = split_event_root_and_seed_slug(train_url)
        train_uuid = event_uuid_from_props(props)
        # link to closest race event by city/date if possible
        linked_event_id = event_id
        if race_meta:
            exact_matches = [
                m for m in race_meta
                if (train_uuid and m.get("event_uuid") == train_uuid)
                or (train_root and m.get("event_root") == train_root)
            ]
            if exact_matches:
                linked_event_id = exact_matches[0]["event_id"]
            elif city:
                city_l = city.lower()
                candidates = [m for m in race_meta if m["city"] == city_l]
                if candidates:
                    # choose closest date
                    def _dist(m):
                        return abs(int(m["date"]) - int(date_yyyymmdd))
                    candidates.sort(key=_dist)
                    linked_event_id = candidates[0]["event_id"]
        if linked_event_id == event_id:
            db_linked_event_id = find_linked_race_event(conn, city, date_yyyymmdd, exclude_event_id=event_id)
            if db_linked_event_id:
                linked_event_id = db_linked_event_id
        if not event_exists(conn, event_id):
            upsert_event(
                conn,
                {
                    "event_id": event_id,
                    "display_name": display_name,
                    "location": city,
                    "country": country,
                    "event_type": event_type,
                    "event_date": f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}",
                    "last_seen": now_iso(),
                },
            )
        else:
            conn.execute("UPDATE events SET last_seen = ? WHERE event_id = ?", (now_iso(), event_id))

        # Keep a dedicated training event visible in the event list.
        ingest_training_props(conn, props, event_id)

        # If this training belongs to a race event, mirror it there as well so
        # race-centric views can still access current-event training data.
        if linked_event_id != event_id:
            if not event_exists(conn, linked_event_id):
                upsert_event(
                    conn,
                    {
                        "event_id": linked_event_id,
                        "display_name": display_name,
                        "location": city,
                        "country": country,
                        "event_type": event_type,
                        "event_date": f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}",
                        "last_seen": now_iso(),
                    },
                )
            else:
                conn.execute("UPDATE events SET last_seen = ? WHERE event_id = ?", (now_iso(), linked_event_id))
            ingest_training_props(conn, props, linked_event_id)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
