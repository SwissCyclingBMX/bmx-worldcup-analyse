import sqlite3
import os
import zipfile
import requests
import unicodedata
import datetime
import re
from typing import List, Optional, Tuple, Dict, Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

DB_PATH = "bmx.db"
DB_URL_ZIP = "https://github.com/SwissCyclingBMX/bmx-worldcup-analyse/releases/download/db-latest/bmx_db.zip"
DB_PATH_CLOUD = "/tmp/bmx.db"

GROUP_MAP = {
    91: "Elite Men",
    92: "Elite Women",
    93: "U23 Men",
    94: "U23 Women",
    95: "Junior Men",
    96: "Junior Women",
}

# WM UCIEventID map (Copenhagen 2025)
WCH_UCI_EVENT_MAP = {
    2025: {
        "Elite Men": "332756",
        "Elite Women": "332757",
        "Junior Men": "332758",
        "Junior Women": "332759",
        "U23 Men": "332760",
        "U23 Women": "332761",
    }
}
NOT_UPCOMING_STATUS = {
    "finished",
    "completed",
    "done",
    "ended",
    "official",
}


# ----------------------------
# Helpers: text normalization
# ----------------------------
def norm_name(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))  # strip accents
    # remove punctuation (keep letters, numbers, spaces)
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = s.upper()
    s = " ".join(s.split())
    return s


def norm_name_key(s: str) -> str:
    """Order-insensitive name key (handles 'LAST First' vs 'First LAST')."""
    base = norm_name(s)
    if not base:
        return ""
    tokens = base.split()
    tokens.sort()
    return " ".join(tokens)


def norm_uci_id(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    # keep digits only
    s = re.sub(r"\\D+", "", s)
    return s


def auto_height(df: pd.DataFrame, row_h: int = 28, min_h: int = 120) -> int:
    if df is None:
        return min_h
    n = 0
    try:
        n = len(df)
    except Exception:
        n = 0
    return max(min_h, int((n + 1) * row_h))


def norm_location(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = s.upper()
    s = " ".join(s.split())
    return s


def fmt_table(df: pd.DataFrame, time_cols: Optional[List[str]] = None, score_cols: Optional[List[str]] = None) -> pd.DataFrame:
    out = df.copy()
    time_cols = time_cols or []
    score_cols = score_cols or []
    for c in time_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").apply(
                lambda v: "" if pd.isna(v) else f"{v:.3f}"
            )
    for c in score_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").apply(
                lambda v: "" if pd.isna(v) else f"{v:.1f}"
            )
    return out


def final_rank_map_for_event(event_id: str, gid: Optional[int], events_df: pd.DataFrame, master: pd.DataFrame) -> Dict[str, Any]:
    if master.empty:
        return {}
    event_id_str = str(event_id)
    # WM: explicit mapping
    if "wch" in event_id_str.lower():
        try:
            year = int(event_id_str[:4])
        except Exception:
            year = None
        cat_label = GROUP_MAP.get(gid, "")
        uci_event_id = WCH_UCI_EVENT_MAP.get(year, {}).get(cat_label)
        if not uci_event_id:
            return {}
        mr = master[master["uci_event_id"].astype(str) == str(uci_event_id)].copy()
    # EC/EM: stored with event_id as uci_event_id
    elif "_euc_" in event_id_str or "_em_" in event_id_str:
        mr = master[master["uci_event_id"].astype(str) == event_id_str].copy()
    else:
        # WC: match by year + location (CDM)
        try:
            year = int(event_id_str[:4])
        except Exception:
            year = None
        loc = ""
        if events_df is not None and "event_id" in events_df.columns:
            row = events_df.loc[events_df["event_id"] == event_id]
            if not row.empty:
                loc = row.iloc[0].get("loc_clean", "") or row.iloc[0].get("location", "")
        loc_norm = norm_location(loc)
        mr = master.copy()
        if "klasse" in mr.columns:
            mr = mr[mr["klasse"].isin(["CDM"])]
        if year is not None and "year" in mr.columns:
            mr = mr[mr["year"] == year]
        if loc_norm and "location" in mr.columns:
            mr = mr[mr["location"].apply(norm_location) == loc_norm]
        # filter by category/gender when possible
        if gid in GROUP_MAP:
            cat_label = GROUP_MAP.get(gid, "")
            if "Elite" in cat_label:
                cat = "Elite"
            elif "U23" in cat_label:
                cat = "U23"
            elif "Junior" in cat_label:
                cat = "Junior"
            else:
                cat = None
            gender = "M" if "Men" in cat_label else "W" if "Women" in cat_label else None
            if cat and "category" in mr.columns:
                mr = mr[mr["category"] == cat]
            if gender and "gender" in mr.columns:
                mr = mr[mr["gender"] == gender]

    if mr.empty:
        return {}
    mr["name_key"] = (mr["first_name"].astype(str) + " " + mr["last_name"].astype(str)).apply(norm_name_key)
    mr["rank"] = pd.to_numeric(mr["rank"], errors="coerce").astype("Int64")
    return mr.set_index("name_key")["rank"].to_dict()


def render_html_table(df: pd.DataFrame, html: Optional[str] = None, row_h: int = 26, min_h: int = 120) -> None:
    if df is None:
        return
    if html is None:
        html = df.to_html(index=False, escape=False, classes="bmx-table")
    style = """
    <style>
      table.bmx-table { font-size: 12px; width: 100%; border-collapse: collapse; }
      table.bmx-table th, table.bmx-table td { border: 1px solid #e6e6e6; padding: 4px 6px; text-align: center; }
      table.bmx-table th { background: #f6f7f9; font-weight: 600; }
      table.bmx-table td:first-child, table.bmx-table th:first-child { text-align: left; }
      @media (prefers-color-scheme: dark) {
        table.bmx-table { color: #f1f3f5; background: #1b1b1b; }
        table.bmx-table th, table.bmx-table td { color: #f1f3f5; background: #1b1b1b; border: 1px solid #333; }
        table.bmx-table th { background: #242424; }
      }
    </style>
    """
    height = max(min_h, int((len(df) + 1) * row_h))
    components.html(style + html, height=height, scrolling=False)


def safe_in_clause(values: List[str]) -> Tuple[str, List[str]]:
    """Returns ('?, ?, ?', params) for IN clause."""
    values = [v for v in values if v]
    if not values:
        # caller should handle empty
        return "(NULL)", []
    return "(" + ",".join(["?"] * len(values)) + ")", values


# ----------------------------
# DB load functions
# ----------------------------
@st.cache_data(ttl=30)
def load_events(cache_bust: int = 0) -> pd.DataFrame:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        # try download from GitHub release (Streamlit Cloud)
        try:
            r = requests.get(DB_URL_ZIP, timeout=30)
            r.raise_for_status()
            zip_path = "/tmp/bmx_db.zip"
            with open(zip_path, "wb") as f:
                f.write(r.content)
            with zipfile.ZipFile(zip_path, "r") as zf:
                db_members = [m for m in zf.namelist() if m.lower().endswith(".db")]
                if not db_members:
                    return pd.DataFrame()
                # extract first .db file found
                member = db_members[0]
                zf.extract(member, "/tmp")
                # normalize to /tmp/bmx.db if needed
                extracted_path = os.path.join("/tmp", member)
                if extracted_path != DB_PATH_CLOUD:
                    try:
                        os.replace(extracted_path, DB_PATH_CLOUD)
                    except Exception:
                        pass
            db_path = DB_PATH_CLOUD
        except Exception:
            return pd.DataFrame()
    try:
        conn = sqlite3.connect(db_path)
        try:
            df = pd.read_sql_query(
                """
                SELECT event_id, display_name, location, country, event_date
                FROM events
                ORDER BY event_id DESC
                """,
                conn,
            )
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    # Derive year from event_id (YYYYMMDD_...)
    df["year"] = df["event_id"].astype(str).str.slice(0, 4)

    # Create labels:
    # - label_short: "ROUND X - Location"
    # - label_analysis: "ROUND X - Location - YYYY"
    base = df["display_name"].fillna(df["event_id"]).astype(str).str.strip()
    base = base.str.replace(r"\s+", " ", regex=True)

    def extract_location(text: str) -> str:
        if not isinstance(text, str):
            return ""
        m = re.search(r"ROUND\\s*\\d+\\s*[-–—]\\s*([^,]+)", text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
        part = text.split(" - ")[-1]
        return part.split(",")[0].strip()

    loc_col = df["location"].fillna("").astype(str).str.strip()
    loc_fallback = base.map(extract_location)

    # Prefer explicit location column; else use parsed location from display_name.
    # But if location already includes "ROUND X - ...", prefer the parsed location (without it).
    loc_clean = loc_col.where(loc_col != "", loc_fallback).fillna("")
    # Simpler detection: any location starting with "ROUND"
    loc_has_round = loc_col.str.upper().str.startswith("ROUND", na=False)
    loc_clean = loc_clean.where(~loc_has_round, loc_fallback)
    # Some location fields already include "ROUND X - ...": strip such prefixes,
    # including different dash characters, even if they appear multiple times.
    loc_clean = loc_clean.str.replace("–", "-", regex=False).str.replace("—", "-", regex=False)
    loc_clean = loc_clean.str.replace(
        r"(?i)^(?:ROUND\\s*\\d+\\s*[-–—]\\s*)+",
        "",
        regex=True,
    ).str.strip()
    # If still present anywhere (e.g. duplicated), drop everything up to the last ROUND marker.
    loc_clean = loc_clean.str.replace(
        r"(?i)^.*ROUND\\s*\\d+\\s*[-–—]\\s*",
        "",
        regex=True,
    ).str.strip()
    # Final safety: remove any remaining "ROUND X - " chunks anywhere.
    loc_clean = loc_clean.str.replace(
        r"(?i)ROUND\\s*\\d+\\s*[-–—]\\s*",
        "",
        regex=True,
    ).str.strip()
    df["loc_clean"] = loc_clean


    # Series detection
    is_wch = (
        df["event_id"].astype(str).str.contains("wch", case=False, regex=False)
        | df["display_name"].fillna("").astype(str).str.contains("world championship", case=False, regex=False)
        | df["display_name"].fillna("").astype(str).str.contains("world championships", case=False, regex=False)
    )
    is_em = (
        df["event_id"].astype(str).str.contains("_em_", case=False, regex=False)
        | df["display_name"].fillna("").astype(str).str.contains("european championship", case=False, regex=False)
        | df["display_name"].fillna("").astype(str).str.contains("european championships", case=False, regex=False)
    )
    is_euc = (
        df["event_id"].astype(str).str.contains("_euc_", case=False, regex=False)
        | df["display_name"].fillna("").astype(str).str.contains("european cup", case=False, regex=False)
    )
    is_wc = (
        df["display_name"].fillna("").astype(str).str.contains("world cup", case=False, regex=False)
    )

    df["series"] = "wc"
    df.loc[is_euc, "series"] = "euc"
    df.loc[is_em, "series"] = "em"
    df.loc[is_wch, "series"] = "wch"

    # Determine which events have race picks (avoid counting practice/training as rounds)
    race_event_ids = set()
    try:
        conn = sqlite3.connect(db_path)
        race_event_ids = set(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT event_id FROM picks WHERE round_key IS NOT NULL AND round_key > 0"
            ).fetchall()
        )
    except Exception:
        race_event_ids = set()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Assign sequential rounds per year and series (WC and EC separate)
    df["_event_day"] = pd.to_datetime(df["event_id"].astype(str).str.slice(0, 8), format="%Y%m%d", errors="coerce")
    df["round_num"] = pd.NA
    mask_round = ~(is_wch | is_em)
    if race_event_ids:
        mask_round = mask_round & df["event_id"].isin(race_event_ids)
    for (yr, series), grp in df.loc[mask_round].sort_values(["_event_day", "event_id"]).groupby(["year", "series"]):
        df.loc[grp.index, "round_num"] = range(1, len(grp) + 1)

    df["label_short"] = "ROUND " + df["round_num"].astype("Int64").astype(str) + " - " + loc_clean
    # European Cup prefix
    df.loc[df["series"] == "euc", "label_short"] = (
        "EC-" + df.loc[df["series"] == "euc", "label_short"]
    )
    df["label_short"] = df["label_short"].where(df["round_num"].notna(), df["display_name"])
    df["label_short"] = df["label_short"].str.strip()
    df["label_analysis"] = df["label_short"] + " - " + df["year"].astype(str)

    # Championships: no round numbering
    wch_label = "World Championships - " + df["year"].astype(str)
    em_label = "European Championships - " + df["year"].astype(str)
    df["label_short"] = df["label_short"].where(~is_wch, wch_label)
    df["label_analysis"] = df["label_analysis"].where(~is_wch, wch_label)
    df["label_short"] = df["label_short"].where(~is_em, em_label)
    df["label_analysis"] = df["label_analysis"].where(~is_em, em_label)
    df = df.drop(columns=["_event_day", "round_num", "series"])

    # Disambiguate duplicates only within the same year (avoid cross-year event_id noise)
    dup = df.duplicated(subset=["year", "label_short"], keep=False)
    df["label_short"] = df["label_short"].where(~dup, df["label_short"] + " (" + df["event_id"] + ")")
    dup_a = df.duplicated(subset=["year", "label_analysis"], keep=False)
    df["label_analysis"] = df["label_analysis"].where(
        ~dup_a, df["label_analysis"] + " (" + df["event_id"] + ")"
    )

    return df


@st.cache_data(ttl=10)
def load_picks_for_event(event_id: str) -> pd.DataFrame:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        return pd.DataFrame()
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT * FROM picks WHERE event_id = ?",
            conn,
            params=[event_id],
        )
    finally:
        conn.close()
    return normalize_picks_df(df)


@st.cache_data(ttl=10)
def load_picks_for_events(event_ids: List[str]) -> pd.DataFrame:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        return pd.DataFrame()
    event_ids = [e for e in event_ids if e]
    if not event_ids:
        return pd.DataFrame()

    in_sql, params = safe_in_clause(event_ids)
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            f"SELECT * FROM picks WHERE event_id IN {in_sql}",
            conn,
            params=params,
        )
    finally:
        conn.close()
    return normalize_picks_df(df)


@st.cache_data(ttl=60)
def load_master_results() -> pd.DataFrame:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        return pd.DataFrame()
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query("SELECT * FROM master_results", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


def live_event_ids_today(events_df: pd.DataFrame) -> List[str]:
    if events_df is None or events_df.empty:
        return []
    if "event_date" not in events_df.columns:
        return []

    dates = pd.to_datetime(events_df["event_date"], errors="coerce").dt.date
    today = datetime.date.today()
    live_ids = events_df.loc[dates == today, "event_id"].dropna().unique().tolist()
    return sorted(live_ids)


def short_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return ""
    # If multiple parts, show first initial + last token
    if len(parts) >= 2:
        first = parts[0][0].upper()
        last = parts[-1]
        return f"{first}. {last}"
    return parts[0]


def normalize_picks_df(df: pd.DataFrame) -> pd.DataFrame:
    """Make columns consistent across historical schema changes."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # Ensure required columns exist
    for c in ["group_id", "round_key", "round_title", "heat_id", "heat_title", "heat_status", "start_time_string"]:
        if c not in df.columns:
            df[c] = None

    # chosen_lane: prefer lane_idx; else lane; else None
    if "lane_idx" in df.columns and "chosen_lane" not in df.columns:
        df["chosen_lane"] = df["lane_idx"]
    elif "lane" in df.columns and "chosen_lane" not in df.columns:
        df["chosen_lane"] = df["lane"]
    elif "chosen_lane" not in df.columns:
        df["chosen_lane"] = None

    # pick_order
    if "pick_order" not in df.columns:
        df["pick_order"] = None

    # nation/name/bib
    if "nation" not in df.columns:
        df["nation"] = None
    if "name" not in df.columns:
        df["name"] = None
    if "bib" not in df.columns:
        df["bib"] = None

    # timing fields + uci_id + rank
    for c in ["uci_id", "start", "t1", "t2", "t3", "t4", "time", "rank"]:
        if c not in df.columns:
            df[c] = None

    # start_dt: parse if present
    if "start_dt" in df.columns:
        df["start_dt"] = pd.to_datetime(df["start_dt"], errors="coerce")
    else:
        df["start_dt"] = pd.NaT

    # start_time_string: trim to HH:MM:SS
    if "start_time_string" in df.columns:
        sts = df["start_time_string"].fillna("").astype(str)
        df["start_time_string"] = sts.str.slice(0, 8)

    # group_id numeric
    df["group_id"] = pd.to_numeric(df["group_id"], errors="coerce").astype("Int64")

    # chosen_lane numeric (treat 0 as missing)
    df["chosen_lane"] = pd.to_numeric(df["chosen_lane"], errors="coerce").astype("Int64")
    df.loc[df["chosen_lane"] <= 0, "chosen_lane"] = pd.NA

    # pick_order numeric
    df["pick_order"] = pd.to_numeric(df["pick_order"], errors="coerce").astype("Int64")
    # rank numeric
    if "rank" in df.columns:
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce").astype("Int64")

    # category label
    df["category"] = df["group_id"].astype("Int64").map(GROUP_MAP).fillna(df["group_id"].astype(str))

    # Name normalization for analysis grouping
    df["name_norm"] = df["name"].apply(norm_name)
    df["name_key"] = df["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")
    df["name_short"] = df["name"].apply(short_name)

    return df


def parse_time_to_seconds(val) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return float("nan")
    s = str(val).strip()
    if not s or s in ["-", "None", "nan"]:
        return float("nan")
    try:
        td = pd.to_timedelta(s)
        return td.total_seconds()
    except Exception:
        try:
            return float(s)
        except Exception:
            return float("nan")


def normalize_training_name(name: str) -> str:
    """
    Training files often store names as 'LAST First'. If we detect a token with
    lowercase letters at the end, move it to the front: 'LAST FIRST' -> 'First LAST'.
    """
    if not isinstance(name, str):
        return ""
    parts = [p for p in name.strip().split() if p]
    if len(parts) <= 1:
        return name
    # Prefer token with lowercase letters as the first name.
    idx = None
    for i in range(len(parts) - 1, -1, -1):
        if any(ch.islower() for ch in parts[i]):
            idx = i
            break
    if idx is not None and idx != 0:
        first = parts[idx]
        rest = parts[:idx] + parts[idx + 1 :]
        return " ".join([first] + rest)
    return name


@st.cache_data(ttl=10)
def load_training_for_events(event_ids: List[str]) -> pd.DataFrame:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        return pd.DataFrame()
    event_ids = [e for e in event_ids if e]
    if not event_ids:
        return pd.DataFrame()

    in_sql, params = safe_in_clause(event_ids)
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            f"""
            SELECT event_id, category, bib, name, nation, gate, start, t1
            FROM training_times
            WHERE event_id IN {in_sql}
            """,
            conn,
            params=params,
        )
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

    if df.empty:
        return df

    df["name_norm"] = df["name"].apply(normalize_training_name).apply(norm_name)
    df["name_key"] = df["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")
    df["start_s"] = df["start"].apply(parse_time_to_seconds)
    df["t1_s"] = df["t1"].apply(parse_time_to_seconds)
    return df


def training_stats(df_train: pd.DataFrame) -> pd.DataFrame:
    if df_train.empty:
        return pd.DataFrame()

    def avg_top3(series: pd.Series) -> float:
        s = series.dropna().astype(float).sort_values()
        if s.empty:
            return float("nan")
        return s.head(3).mean()

    def consistency_score(series: pd.Series) -> float:
        s = series.dropna().astype(float)
        if s.empty:
            return float("nan")
        std = s.std()
        if pd.isna(std):
            return float("nan")
        score = 100.0 / (1.0 + std)
        return round(score, 1)

    grp = df_train.groupby("name_key", as_index=False)
    out = grp.agg(
        best_start=("start_s", "min"),
        best_t1=("t1_s", "min"),
        avg_top3_start=("start_s", avg_top3),
        avg_top3_t1=("t1_s", avg_top3),
        name=("name", canonical_name),
        cons_score=("start_s", consistency_score),
    )
    # round averages for display
    for c in ["avg_top3_start", "avg_top3_t1", "best_start", "best_t1"]:
        out[c] = out[c].round(3)
    return out


def race_stats(df_race: pd.DataFrame) -> pd.DataFrame:
    """
    Compute best/avg3/consistency for race data (start/t1 from picks).
    """
    if df_race.empty:
        return pd.DataFrame()

    tmp = df_race.copy()
    tmp["name_norm"] = tmp["name"].apply(norm_name)
    tmp["start_s"] = tmp["start"].apply(parse_time_to_seconds)
    tmp["t1_s"] = tmp["t1"].apply(parse_time_to_seconds)

    def avg_top3(series: pd.Series) -> float:
        s = series.dropna().astype(float).sort_values()
        if s.empty:
            return float("nan")
        return s.head(3).mean()

    def consistency_score(series: pd.Series) -> float:
        s = series.dropna().astype(float)
        if s.empty:
            return float("nan")
        std = s.std()
        if pd.isna(std):
            return float("nan")
        score = 100.0 / (1.0 + std)
        return round(score, 1)

    grp = tmp.groupby("name_norm", as_index=False)
    out = grp.agg(
        best_start=("start_s", "min"),
        best_t1=("t1_s", "min"),
        avg_top3_start=("start_s", avg_top3),
        avg_top3_t1=("t1_s", avg_top3),
        name=("name", canonical_name),
        cons_score=("start_s", consistency_score),
    )
    # round averages for display
    for c in ["avg_top3_start", "avg_top3_t1", "best_start", "best_t1"]:
        out[c] = out[c].round(3)
    return out


# ----------------------------
# Build heats & filtering
# ----------------------------
def build_heats(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "group_id",
        "category",
        "round_key",
        "round_title",
        "heat_id",
        "heat_title",
        "heat_status",
        "start_time_string",
        "start_dt",
    ]
    keep = [c for c in cols if c in df.columns]
    heats = df[keep].drop_duplicates().copy()

    heats = heats.sort_values(
        ["start_dt", "round_key", "heat_id"],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)

    return heats


def add_sui_names_column(heats: pd.DataFrame, df_event: pd.DataFrame, nation_filter: str = "SUI") -> pd.DataFrame:
    """Adds a column with Swiss riders participating in that heat."""
    if heats.empty:
        return heats

    tmp = df_event.copy()
    tmp = tmp[tmp["nation"].fillna("").str.upper() == nation_filter.upper()]
    if tmp.empty:
        heats["SUI"] = ""
        return heats

    # Group Swiss names by heat
    g = (
        tmp.groupby(["round_key", "heat_id"])["name"]
        .apply(lambda s: ", ".join(sorted({x for x in s.dropna().tolist() if x})))
        .reset_index()
        .rename(columns={"name": "SUI"})
    )

    out = heats.merge(g, on=["round_key", "heat_id"], how="left")
    out["SUI"] = out["SUI"].fillna("")
    return out


def filter_upcoming_heats(heats: pd.DataFrame) -> pd.DataFrame:
    """
    "Upcoming" is not fully reliable historically.
    We filter out obvious past states, but keep Confirmed/Upcoming.
    """
    if heats.empty:
        return heats

    status = heats["heat_status"].fillna("").astype(str).str.lower()

    # treat these as NOT upcoming
    not_upcoming = status.isin(list(NOT_UPCOMING_STATUS))

    # keep "confirmed", "upcoming", "scheduled", plus anything unknown (to not hide data)
    out = heats[~not_upcoming].copy()
    return out


def heat_label_row(r: pd.Series) -> str:
    cat = r.get("category", "")
    rt = r.get("round_title", "")
    ht = r.get("heat_title", "")
    stt = r.get("start_time_string", "")
    sui = r.get("SUI", "")
    sui_part = f" | SUI: {sui}" if isinstance(sui, str) and sui.strip() else ""
    time_part = f" | {stt}" if isinstance(stt, str) and stt.strip() else ""
    return f"{cat} | {rt} | {ht}{time_part}{sui_part}"


# ----------------------------
# Analysis
# ----------------------------
def canonical_name(series_original: pd.Series) -> str:
    """Pick the most frequent original name as display name."""
    s = series_original.dropna().astype(str).str.strip()
    if s.empty:
        return ""
    return s.value_counts().index[0]


def classify_tactic(fav_lane: int, fav_share: float, mean_lane: float) -> str:
    # Simple heuristic
    if fav_lane == 1 and fav_share >= 0.4:
        return "Innenbahn-orientiert"
    if fav_lane == 8 and fav_share >= 0.4:
        return "Aussenbahn-orientiert"
    if fav_share >= 0.6:
        return f"Lieblingslane ({fav_lane})"
    if 3.5 <= mean_lane <= 5.5:
        return "Mitte"
    if mean_lane < 3.5:
        return "Eher innen"
    return "Eher aussen"


def rider_summary(df_hist: pd.DataFrame) -> pd.DataFrame:
    if df_hist.empty:
        return df_hist

    # Only require a lane choice for counting picks; pick_order can be missing.
    dfp = df_hist.dropna(subset=["name_norm", "chosen_lane"]).copy()
    if dfp.empty:
        return pd.DataFrame()

    grp = dfp.groupby("name_norm", as_index=False)

    out = grp.agg(
        picks_n=("chosen_lane", "count"),
        mean_pick_order=("pick_order", "mean"),
        mean_chosen_lane=("chosen_lane", "mean"),
        fav_lane=("chosen_lane", lambda s: int(s.value_counts().index[0]) if not s.empty else None),
        fav_lane_count=("chosen_lane", lambda s: int(s.value_counts().iloc[0]) if not s.empty else 0),
        name=("name", canonical_name),
    )

    out["favorite_share"] = (out["fav_lane_count"] / out["picks_n"]).round(2)
    out["mean_pick_order"] = out["mean_pick_order"].round(2)
    out["mean_chosen_lane"] = out["mean_chosen_lane"].round(2)

    out["taktik"] = out.apply(
        lambda r: classify_tactic(
            fav_lane=int(r["fav_lane"]) if pd.notna(r["fav_lane"]) else 0,
            fav_share=float(r["favorite_share"]) if pd.notna(r["favorite_share"]) else 0.0,
            mean_lane=float(r["mean_chosen_lane"]) if pd.notna(r["mean_chosen_lane"]) else 0.0,
        ),
        axis=1,
    )

    out = out.drop(columns=["fav_lane_count"])
    out = out.sort_values(["picks_n", "favorite_share"], ascending=[False, False], kind="stable").reset_index(drop=True)
    return out


def lane_distribution(df_hist: pd.DataFrame) -> pd.DataFrame:
    if df_hist.empty:
        return pd.DataFrame()

    dfp = df_hist.dropna(subset=["name_norm", "chosen_lane", "pick_order"]).copy()
    if dfp.empty:
        return pd.DataFrame()

    out = (
        dfp.groupby(["name_norm", "name", "pick_order", "chosen_lane"], as_index=False)
        .size()
        .rename(columns={"size": "count"})
    )

    # canonical display name per name_norm
    canon = dfp.groupby("name_norm")["name"].apply(canonical_name).reset_index().rename(columns={"name": "name_display"})
    out = out.merge(canon, on="name_norm", how="left")
    out = out.drop(columns=["name"]).rename(columns={"name_display": "name"})

    out = out.sort_values(["name", "pick_order", "chosen_lane"], kind="stable").reset_index(drop=True)
    return out


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="BMX Heat Scout", layout="wide", initial_sidebar_state="expanded")

# Remove internal scrollbars for tables
st.markdown(
    """
    <style>
      div[data-testid="stTable"] { overflow: visible !important; }
      div[data-testid="stTable"] > div { overflow: visible !important; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("BMX Heat Scout")
st.caption("Live-Ansicht aktualisiert sich bei Interaktionen (kein Auto-Refresh).")

if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = 0

events = load_events(cache_bust=st.session_state["cache_bust"])
if events.empty:
    st.error("events-Tabelle ist leer. Bitte ingest.py für mindestens ein Event laufen lassen.")
    st.stop()

# Sidebar: Event Auswahl
st.sidebar.header("Event Auswahl")

# Live only if there is actually something live today (event_date == today) in the latest year
latest_year = events["year"].iloc[0]
live_ids = live_event_ids_today(events[events["year"] == latest_year])

if live_ids:
    mode = st.sidebar.radio("Modus", ["Live", "Archiv (Jahre)"], horizontal=True)
else:
    st.sidebar.caption("Kein Live-Event erkannt – Modus bleibt auf Archiv.")
    mode = "Archiv (Jahre)"

# Which set of events is shown in the CURRENT event dropdown?
if mode == "Live":
    df_current_pool = events[events["event_id"].isin(live_ids)].copy()
else:
    years = sorted(events["year"].dropna().unique().tolist(), reverse=True)
    year_sel = st.sidebar.selectbox("Jahr", years, index=0)
    df_current_pool = events[events["year"] == year_sel].copy()

df_current_pool = df_current_pool.sort_values("event_id", ascending=False)

event_label_current = st.sidebar.selectbox("Event", df_current_pool["label_short"].tolist(), index=0)
event_id = df_current_pool.loc[df_current_pool["label_short"] == event_label_current, "event_id"].iloc[0]
st.sidebar.caption(f"Aktives Event: {event_id}")

# Analyse selection (directly under Event)
default_analysis_labels = []
if event_id in events["event_id"].tolist():
    default_analysis_labels = [events.loc[events["event_id"] == event_id, "label_analysis"].iloc[0]]

analysis_event_labels = st.sidebar.multiselect(
    "Event (Analyse) – frei kombinierbar",
    options=events["label_analysis"].tolist(),
    default=default_analysis_labels,
)

analysis_event_labels = [x for x in analysis_event_labels if x]
analysis_event_ids = events.loc[events["label_analysis"].isin(analysis_event_labels), "event_id"].tolist()
# always include current event for training/race context
if event_id not in analysis_event_ids:
    analysis_event_ids.append(event_id)

# Current event picks
df_event = load_picks_for_event(event_id)
if df_event.empty:
    if mode == "Live":
        st.info("Live-Daten sind noch nicht verfügbar. Bitte später erneut laden.")
    else:
        st.warning(f"Keine Picks-Daten für {event_id}.")
    st.stop()

# Filters (order: Nation, Rider, Kategorie, Geschlecht)
nation = st.sidebar.text_input("Nation Filter (z.B. SUI) – leer = alle", value="SUI").strip().upper()
show_times = st.sidebar.checkbox("Zeiten anzeigen (Start/T1)", value=True)
training_live = st.sidebar.checkbox("Training-Live Ansicht", value=False)

# Rider filter(s) - show only riders that match other filters (nation/category/gender)
# Use session_state for category/gender (widgets are rendered below, but state persists)
level_sel_state = st.session_state.get("level_sel", ["Elite", "U23", "Junior"])
gender_sel_state = st.session_state.get("gender_sel", ["Men", "Women"])

allowed_group_ids_preview = []
levels_all = ["Elite", "U23", "Junior"]
genders_all = ["Men", "Women"]
if not level_sel_state and not gender_sel_state:
    allowed_group_ids_preview = []
else:
    levels_use = level_sel_state if level_sel_state else levels_all
    genders_use = gender_sel_state if gender_sel_state else genders_all
    labels = {f"{lvl} {gen}" for lvl in levels_use for gen in genders_use}
    for gid, cat in GROUP_MAP.items():
        if cat in labels:
            allowed_group_ids_preview.append(gid)

df_rider_pool = df_event.copy()
if nation:
    df_rider_pool = df_rider_pool[df_rider_pool["nation"].fillna("").str.upper() == nation]
if allowed_group_ids_preview:
    df_rider_pool = df_rider_pool[df_rider_pool["group_id"].isin(allowed_group_ids_preview)].copy()
all_names = sorted([n for n in df_rider_pool["name"].dropna().unique().tolist() if isinstance(n, str) and n.strip()])
if "rider_filter" not in st.session_state:
    st.session_state["rider_filter"] = []

if training_live:
    rider_live_selected = st.sidebar.multiselect(
        "Rider Filter (Live, leer = alle)",
        options=all_names,
        default=[],
        key="rider_filter_live",
    )
    rider_selected = "Alle"
else:
    # keep current selections visible, but allow adding more
    options_riders = sorted(set(all_names) | set(st.session_state["rider_filter"]))
    rider_selected_list = st.sidebar.multiselect(
        "Rider Filter (optional, leer = alle)",
        options=options_riders,
        key="rider_filter",
    )
    # allow multi-select; baseline comparison only if exactly one selected
    rider_selected = rider_selected_list[0] if len(rider_selected_list) == 1 else "Alle"
    rider_live_selected = []

# Kategorie Filter
level_sel = st.sidebar.multiselect(
    "Kategorie",
    options=["Elite", "U23", "Junior"],
    default=["Elite", "U23"],
    key="level_sel",
)
gender_sel = st.sidebar.multiselect(
    "Geschlecht",
    options=["Men", "Women"],
    default=["Men", "Women"],
    key="gender_sel",
)

allowed_group_ids = []
levels_all = ["Elite", "U23", "Junior"]
genders_all = ["Men", "Women"]

if not level_sel and not gender_sel:
    # Empty selection means "show all"
    allowed_group_ids = []
else:
    # If one side is empty, treat it as "all" for the other side
    levels_use = level_sel if level_sel else levels_all
    genders_use = gender_sel if gender_sel else genders_all
    labels = {f"{lvl} {gen}" for lvl in levels_use for gen in genders_use}
    for gid, cat in GROUP_MAP.items():
        if cat in labels:
            allowed_group_ids.append(gid)

# Preload analysis history for race stats (used later)
df_hist_all = load_picks_for_events(analysis_event_ids) if analysis_event_ids else pd.DataFrame()
if not df_hist_all.empty and allowed_group_ids:
    df_hist_all = df_hist_all[df_hist_all["group_id"].isin(allowed_group_ids)].copy()

# Apply category filters to current event (empty selection = show all)
if allowed_group_ids:
    df_event = df_event[df_event["group_id"].isin(allowed_group_ids)].copy()

only_upcoming = False
if mode == "Live":
    only_upcoming = st.sidebar.checkbox("Nur anstehende Heats (Live)", value=True)

# Cache reset (keep at bottom of sidebar)
if st.sidebar.button("Cache leeren"):
    st.cache_data.clear()
    st.session_state["cache_bust"] += 1
    st.experimental_rerun()

# (Rider filter moved above)

# ----------------------------
# Training Live View
# ----------------------------
if training_live:
    st.subheader("Training Live – Zeiten (aktuelles Event)")

    metric_options = {
        "Start to Bottom": "start",
        "Start to Turn 1": "t1",
        "Split first Straight": "split_t1",
    }
    metric_label = st.selectbox("Rundenzeit anzeigen:", list(metric_options.keys()), index=0, key="live_metric")
    metric_col = metric_options[metric_label]

    df_live = df_event.copy()
    if nation:
        df_live = df_live[df_live["nation"].fillna("").str.upper() == nation]
    if rider_live_selected:
        df_live = df_live[df_live["name"].isin(rider_live_selected)]

    if df_live.empty:
        st.info("Keine Live-Trainingsdaten mit den aktuellen Filtern.")
        st.stop()

    df_live["start_s"] = df_live["start"].apply(parse_time_to_seconds)
    df_live["t1_s"] = df_live["t1"].apply(parse_time_to_seconds)
    df_live["split_t1"] = df_live["t1_s"] - df_live["start_s"]
    if metric_col in ["start", "t1"]:
        df_live["metric_s"] = df_live[metric_col + "_s"]
    else:
        df_live["metric_s"] = df_live[metric_col]

    # Row key = start_time_string (fallback to heat_id)
    df_live["start_label"] = df_live["start_time_string"].fillna("").astype(str)
    df_live.loc[df_live["start_label"] == "", "start_label"] = df_live["heat_id"].astype(str)

    riders = rider_live_selected if rider_live_selected else sorted(df_live["name"].dropna().unique().tolist())
    mat = (
        df_live[df_live["name"].isin(riders)]
        .groupby(["start_label", "name"])["metric_s"]
        .min()
        .reset_index()
        .pivot(index="start_label", columns="name", values="metric_s")
    )
    mat = mat.reindex(columns=riders)
    mat = mat.reset_index().rename(columns={"start_label": "Start"})
    st.table(mat)

    # Last available gate table
    st.markdown("**Letzte verfügbare Gates (aktuellste Messung):**")
    df_last = df_live.copy()
    # derive a sortable time key from start_time_string
    df_last["start_ts"] = pd.to_timedelta(df_last["start_time_string"], errors="coerce")
    if df_last["start_ts"].notna().any():
        latest_ts = df_last["start_ts"].max()
        df_last = df_last[df_last["start_ts"] == latest_ts].copy()
    else:
        df_last = df_last.sort_values(["heat_id"], ascending=False).head(8).copy()

    df_last = df_last.sort_values(["lane_idx", "lane"], na_position="last", kind="stable")
    # compute split for display
    df_last["split"] = df_last["t1_s"] - df_last["start_s"]
    last_cols = ["start_time_string", "name", "start", "split", "t1"]
    last_cols = [c for c in last_cols if c in df_last.columns]
    df_last = df_last[last_cols].rename(
        columns={
            "start_time_string": "Starttime",
            "name": "Name",
            "start": "Start",
            "split": "Split",
            "t1": "T1",
        }
    )
    st.table(df_last)
    st.stop()

# ----------------------------
# Heats table + selection
# ----------------------------
st.subheader("Heats (nach Filter)")

heats = build_heats(df_event)

# nation filter affects which heats shown (based on startlist membership)
df_filter = df_event.copy()
if nation:
    df_filter = df_filter[df_filter["nation"].fillna("").str.upper() == nation]
# Rider filter affects heats list only (multi-select supported)
if rider_selected_list:
    df_filter = df_filter[df_filter["name"].isin(rider_selected_list)].copy()

heats_f = build_heats(df_filter)

# Add Swiss names column always (independent of nation filter)
heats_f = add_sui_names_column(heats_f, df_event, nation_filter="SUI")

# Extra safety: filter heats by selected riders (multi-select)
if rider_selected_list:
    heat_keys = (
        df_event[df_event["name"].isin(rider_selected_list)][["round_key", "heat_id"]]
        .drop_duplicates()
    )
    heats_f = heats_f.merge(heat_keys, on=["round_key", "heat_id"], how="inner")

# Apply category filter to heats (empty selection = show all)
if allowed_group_ids:
    heats_f = heats_f[heats_f["group_id"].isin(allowed_group_ids)].copy()

# Upcoming filter
if only_upcoming:
    tmp = filter_upcoming_heats(heats_f)
    if tmp.empty:
        st.info("Hinweis: Keine 'anstehenden' Heats gefunden (Event vermutlich nicht live). Zeige stattdessen alle Heats.")
    else:
        heats_f = tmp

# Heat selectbox with Swiss names embedded
if heats_f.empty:
    st.warning("Keine Heats passend zu den Filtern.")
    st.stop()

options = [heat_label_row(r) for _, r in heats_f.iterrows()]
if "heat_choice" not in st.session_state:
    st.session_state["heat_choice"] = options[0] if options else None
if st.session_state["heat_choice"] not in options:
    st.session_state["heat_choice"] = options[0] if options else None
choice = st.selectbox("Heat auswählen", options, index=options.index(st.session_state["heat_choice"]), key="heat_choice")
chosen = heats_f.iloc[options.index(choice)]
rk = int(chosen["round_key"])
hid = int(chosen["heat_id"])
gid = int(chosen["group_id"]) if pd.notna(chosen.get("group_id")) else None
chosen_round_title = chosen.get("round_title")
chosen_heat_title = chosen.get("heat_title")

# Startlist (PickOrder / Lane)
tab_start, tab_rounds = st.tabs(["Startliste - Gate Pick", "Time Analyse"])

with tab_start:
    st.subheader("Startliste - Lane Pick")

    df_heat = df_event[(df_event["round_key"] == rk) & (df_event["heat_id"] == hid)].copy()
    if gid is not None:
        df_heat = df_heat[df_heat["group_id"] == gid].copy()
    if chosen_round_title:
        df_heat = df_heat[df_heat["round_title"] == chosen_round_title].copy()
    if chosen_heat_title:
        df_heat = df_heat[df_heat["heat_title"] == chosen_heat_title].copy()
    df_heat["name_norm"] = df_heat["name"].apply(norm_name)
    df_heat["name_key"] = df_heat["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")

    df_heat = df_heat.sort_values(["pick_order"], na_position="last", kind="stable")

    start_cols = ["nation", "bib", "name", "pick_order", "rank", "chosen_lane"]
    start_cols = [c for c in start_cols if c in df_heat.columns]
    start_df = df_heat[start_cols].copy()
    start_df["name_norm"] = start_df["name"].apply(norm_name)
    start_df["name_key"] = start_df["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")
    start_df["name_short"] = df_heat["name_short"].values
    start_df["Rider"] = start_df["name_short"]

    # Training stats for riders in heat
    if show_times:
        training_source_note = "Training-Zeiten: aktuelles Event (Gate Practice)"
        df_train = load_training_for_events(analysis_event_ids)
        if not df_train.empty:
            stats = training_stats(df_train)
            stats_cols = ["name_key", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "cons_score"]
            stats = stats[stats_cols]
            start_df = start_df.merge(stats, on="name_key", how="left")
            start_df = start_df.rename(
                columns={
                    "best_start": "train_best_start",
                    "best_t1": "train_best_t1",
                    "avg_top3_start": "train_avg3_start",
                    "avg_top3_t1": "train_avg3_t1",
                    "cons_score": "train_cons_score",
                }
            )

        # Race stats for Startliste:
        # - If Round 1: use other selected analysis events (e.g., previous day)
        # - Else: use current event up to (but not including) the selected heat
        is_round1 = False
        rt = str(chosen.get("round_title") or "").strip().lower()
        if rt.startswith("round 1") or rt.startswith("runde 1"):
            is_round1 = True

        if is_round1:
            df_race_hist = df_hist_all.copy() if not df_hist_all.empty else pd.DataFrame()
            # For Round 1: only use previous day of the SAME event/location (no cross-location carryover)
            prev_event_id = None
            try:
                current_date = int(str(event_id)[:8])
                current_loc = (
                    events.loc[events["event_id"] == event_id, "loc_clean"].iloc[0]
                    if "loc_clean" in events.columns and (events["event_id"] == event_id).any()
                    else ""
                )
                series = "euc" if "_euc_" in str(event_id) else "wc"
                if series in ("wc", "euc") and current_loc:
                    ev = events.copy()
                    ev = ev[ev["event_id"] != event_id]
                    if series == "euc":
                        ev = ev[ev["event_id"].astype(str).str.contains("_euc_", regex=False)]
                    else:
                        ev = ev[~ev["event_id"].astype(str).str.contains("_euc_", regex=False)]
                    loc_col = "loc_clean" if "loc_clean" in ev.columns else "location"
                    ev = ev[ev[loc_col] == current_loc]
                    ev = ev[ev["event_id"].astype(str).str.slice(0, 8).str.isdigit()]
                    if not ev.empty:
                        ev["event_date_num"] = ev["event_id"].astype(str).str.slice(0, 8).astype(int)
                        prevs = ev[ev["event_date_num"] < current_date].sort_values("event_date_num")
                        if not prevs.empty:
                            prev_event_id = prevs.iloc[-1]["event_id"]
            except Exception:
                prev_event_id = None

            if prev_event_id:
                if df_race_hist.empty or prev_event_id not in df_race_hist["event_id"].unique():
                    df_prev = load_picks_for_event(prev_event_id)
                    df_race_hist = df_prev.copy() if not df_prev.empty else df_race_hist.iloc[0:0].copy()
                else:
                    df_race_hist = df_race_hist[df_race_hist["event_id"] == prev_event_id].copy()
                race_source_note = "Race-Zeiten: Vortag gleiche Location (gleiche Serie)"
            else:
                df_race_hist = df_race_hist.iloc[0:0].copy()
                race_source_note = "Race-Zeiten: keine Daten (kein Vortag gefunden)"
        else:
            df_race_hist = df_event.copy()
            if not df_race_hist.empty:
                if "start_dt" in df_race_hist.columns and pd.notna(chosen.get("start_dt")):
                    df_race_hist = df_race_hist[df_race_hist["start_dt"] < chosen.get("start_dt")].copy()
                else:
                    # Fallback: use round_key/heat_id ordering
                    df_race_hist = df_race_hist[
                        (df_race_hist["round_key"] < rk)
                        | ((df_race_hist["round_key"] == rk) & (df_race_hist["heat_id"] < hid))
                    ].copy()
            race_source_note = "Race-Zeiten: aktuelles Event (nur bis vor den gewählten Heat)"

        if not df_race_hist.empty:
            heat_riders_norm = set(df_heat["name_norm"].dropna().tolist())
            df_race_hist = df_race_hist[df_race_hist["name_norm"].isin(heat_riders_norm)].copy()
        race = race_stats(df_race_hist) if not df_race_hist.empty else pd.DataFrame()
        if not race.empty:
            race = race[["name_norm", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "cons_score"]]
            start_df = start_df.merge(race, on="name_norm", how="left")
            start_df = start_df.rename(
                columns={
                    "best_start": "race_best_start",
                    "best_t1": "race_best_t1",
                    "avg_top3_start": "race_avg3_start",
                    "avg_top3_t1": "race_avg3_t1",
                    "cons_score": "race_cons_score",
                }
            )

    # Render Startliste
    if show_times:
        def fmt_val(v):
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return f"{v:.3f}" if isinstance(v, (int, float)) else str(v)

        # Baseline: only when a rider is selected
        baseline = {}
        if rider_selected != "Alle":
            base_rows = start_df[start_df["name"] == rider_selected]
            if not base_rows.empty:
                base_row = base_rows.iloc[0]
                baseline = {
                    "race_best_start": base_row.get("race_best_start"),
                    "race_avg3_start": base_row.get("race_avg3_start"),
                    "train_best_start": base_row.get("train_best_start"),
                    "train_avg3_start": base_row.get("train_avg3_start"),
                }

        def color_for(metric, v):
            if metric not in baseline or baseline[metric] is None or pd.isna(baseline[metric]):
                return None
            base = baseline[metric]
            better = v < base  # lower is better for start times
            # brighter, higher-contrast colors for dark mode
            return "#ff3b30" if better else "#34c759"

        def combined_cell(race_v, train_v, metric_race, metric_train, is_baseline):
            race_txt = fmt_val(race_v)
            train_txt = fmt_val(train_v)
            race_color = color_for(metric_race, race_v) if race_txt else None
            train_color = color_for(metric_train, train_v) if train_txt else None
            # Baseline rider should stay black
            if is_baseline:
                race_color = None
                train_color = None
            if not race_txt and train_txt:
                race_txt = "—"
                race_color = None
            return (
                f"<div style='line-height:1.1'>"
                f"<div class='race-val' style='font-size:14px;font-weight:600;color:{race_color if race_color else 'inherit'} !important'>{race_txt}</div>"
                f"<div class='train-val' style='font-size:11px;color:{train_color if train_color else 'inherit'} !important'>{train_txt}</div>"
                f"</div>"
            )

        view = start_df.copy()
        # preserve full name for matching (display may be shortened later)
        if "name" in view.columns:
            view["name_full"] = view["name"]
        view["is_baseline"] = view["name"] == rider_selected
        # Avoid duplicate Rider column
        if "name" in view.columns and "Rider" in view.columns:
            view = view.drop(columns=["name"])
        view = view.rename(columns={"bib": "Plate", "name": "Rider", "rank": "Heat Rank"})
        if "Heat Rank" in view.columns:
            view["Heat Rank"] = pd.to_numeric(view["Heat Rank"], errors="coerce").astype("Int64")
        # Fallback: compute heat rank from time if missing
        if "Heat Rank" in view.columns and view["Heat Rank"].isna().all():
            try:
                tmp = df_heat.copy()
                tmp["time_s"] = tmp["time"].apply(parse_time_to_seconds)
                tmp = tmp.sort_values("time_s", na_position="last")
                tmp["Heat Rank"] = range(1, len(tmp) + 1)
                rank_map = tmp.set_index("name")["Heat Rank"].to_dict()
                if "name_full" in view.columns:
                    view["Heat Rank"] = view["name_full"].map(rank_map)
                else:
                    view["Heat Rank"] = view["Rider"].map(rank_map)
            except Exception:
                pass
        if "name_short" in view.columns:
            view["Rider"] = view["name_short"]
        view["Best Start"] = view.apply(
            lambda r: combined_cell(
                r.get("race_best_start"),
                r.get("train_best_start"),
                "race_best_start",
                "train_best_start",
                r.get("is_baseline"),
            ),
            axis=1,
        )
        view["Ø3 Start"] = view.apply(
            lambda r: combined_cell(
                r.get("race_avg3_start"),
                r.get("train_avg3_start"),
                "race_avg3_start",
                "train_avg3_start",
                r.get("is_baseline"),
            ),
            axis=1,
        )
        view["Best T1"] = view.apply(
            lambda r: combined_cell(r.get("race_best_t1"), r.get("train_best_t1"), "", "", r.get("is_baseline")), axis=1
        )
        view["Ø3 T1"] = view.apply(
            lambda r: combined_cell(r.get("race_avg3_t1"), r.get("train_avg3_t1"), "", "", r.get("is_baseline")), axis=1
        )
        view["Score"] = view.apply(
            lambda r: combined_cell(r.get("race_cons_score"), r.get("train_cons_score"), "", "", r.get("is_baseline")), axis=1
        )

        # Final rank (Master Results) for WM / EC / EM / WC
        master = load_master_results()
        if not master.empty:
            final_map_name = final_rank_map_for_event(event_id, gid, events, master)
            if final_map_name:
                name_src = view["name_full"] if "name_full" in view.columns else view["Rider"]
                view["name_key"] = name_src.apply(norm_name_key)
                view["final_rank"] = view["name_key"].map(final_map_name)

        show_cols = [
            "nation",
            "Plate",
            "Rider",
            "pick_order",
            "Best Start",
            "Ø3 Start",
            "Best T1",
            "Ø3 T1",
            "Score",
            "chosen_lane",
            "Heat Rank",
        ]
        show_cols = [c for c in show_cols if c in view.columns]
        view = view[show_cols]

        style = """
        <style>
          table.dataframe { font-size: 12px; table-layout: fixed; width: 100%; }
          table.dataframe th, table.dataframe td { padding: 6px 6px; border-bottom: 1px solid #eee; }
          table.dataframe th { text-align: left; }
          table.dataframe td { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
          table.dataframe td:nth-child(1), table.dataframe th:nth-child(1) { width: 45px; text-align:center; }
          table.dataframe td:nth-child(2), table.dataframe th:nth-child(2) { width: 45px; text-align:center; }
          table.dataframe td:nth-child(3), table.dataframe th:nth-child(3) { width: 140px; }
          table.dataframe td:nth-child(4), table.dataframe th:nth-child(4) { width: 55px; text-align:center; }
          table.dataframe td:nth-child(5), table.dataframe th:nth-child(5) { width: 55px; text-align:center; }
          table.dataframe td:nth-child(n+6), table.dataframe th:nth-child(n+6) { width: 85px; text-align:center; }

          @media (prefers-color-scheme: dark) {
            table.dataframe { color: #f1f3f5; background: #1b1b1b; }
            table.dataframe th, table.dataframe td { color: #f1f3f5; background: #1b1b1b; border-bottom: 1px solid #333; }
            .race-val { color: #f1f3f5 !important; }
            .train-val { color: #a9b0b8 !important; }
          }
        </style>
        """
        html = view.to_html(index=False, escape=False)
        html = html.replace(
            "<table border=\"1\" class=\"dataframe\">",
            "<table class='dataframe' style='width:100%;border-collapse:collapse;'>",
        )
        html = f"<div style='overflow-x:auto;width:100%;'>{html}</div>"
        components.html(style + html, height=auto_height(view, row_h=38, min_h=200) + 120, scrolling=False)
        st.caption(f"{race_source_note} | {training_source_note}")
        if rider_selected != "Alle":
            st.caption("Farben: Rot = schneller als gewählter Rider, Grün = langsamer. Vergleich nur für Start-Zeiten.")
    else:
        start_df_simple = start_df.copy()
        if "name_short" in start_df_simple.columns:
            start_df_simple["Rider"] = start_df_simple["name_short"]
        if "name" in start_df_simple.columns:
            start_df_simple = start_df_simple.drop(columns=["name"])
        start_df_simple = start_df_simple.rename(columns={"bib": "Plate"})
        # drop any accidental duplicate columns
        start_df_simple = start_df_simple.loc[:, ~start_df_simple.columns.duplicated()]
        start_df_simple = start_df_simple[["nation", "Plate", "Rider", "pick_order", "chosen_lane"]]
        st.table(start_df_simple)

    # --- startlist tab: analysis tables in requested order ---
    df_hist_heat = df_hist_all.copy() if not df_hist_all.empty else pd.DataFrame()
    if not df_hist_heat.empty and not df_heat.empty:
        heat_riders_norm = set(df_heat["name_norm"].dropna().tolist())
        df_hist_heat = df_hist_heat[df_hist_heat["name_norm"].isin(heat_riders_norm)].copy()

    if not df_hist_heat.empty and not df_heat.empty:
        # Build pick_order map for sorting (current heat)
        po_map = (
            df_heat[["name_norm", "pick_order"]]
            .dropna()
            .drop_duplicates()
            .set_index("name_norm")["pick_order"]
            .to_dict()
        )

        # Lane distribution per rider (SECOND)
        st.markdown("**Lane-Verteilung (pick_order → chosen_lane) pro Rider:**")
        dist_df = lane_distribution(df_hist_heat)
        if dist_df.empty:
            st.info("Keine Verteilung berechenbar (chosen_lane / pick_order fehlen).")
        else:
            dist_df = dist_df.copy()
            dist_df["po_sort"] = dist_df["name"].map(lambda n: po_map.get(norm_name(n), 9999))

            def move_symbol(row):
                try:
                    po = int(row["pick_order"])
                    cl = int(row["chosen_lane"])
                except Exception:
                    return ""
                if cl == po:
                    return ""
                return "→" if cl > po else "←"

            dist_df["move"] = dist_df.apply(move_symbol, axis=1)

            def color_move(val):
                if val == "→":
                    return "color: #1f77b4; font-weight: 700;"
                if val == "←":
                    return "color: #2ca02c; font-weight: 700;"
                return ""

            dist_df = dist_df.sort_values(["po_sort", "name", "pick_order", "chosen_lane"], kind="stable")
            dist_view = dist_df[["name", "pick_order", "chosen_lane", "move", "count"]].copy()
            dist_view["name"] = dist_view["name"].apply(short_name)
            # alternate group shading by rider (keep default pandas table style)
            group_flag = []
            last_name = None
            toggle = False
            for n in dist_view["name"].tolist():
                if n != last_name:
                    toggle = not toggle
                    last_name = n
                group_flag.append(toggle)
            dist_view["_group"] = group_flag

            def _shade_row(row):
                if row["_group"]:
                    return ["background-color: #f3f5f7"] * len(row)
                return [""] * len(row)

            dist_out = dist_view.drop(columns=["_group"])
            styled = (
                dist_view.style
                .apply(_shade_row, axis=1)
                .applymap(color_move, subset=["move"])
                .hide(axis="index")
                .hide(axis="columns", subset=["_group"])
                .set_table_attributes('class="bmx-table"')
            )
            html = styled.to_html()
            render_html_table(dist_out, html=html, row_h=26, min_h=140)

        # Summary per rider (THIRD)
        st.markdown("**Zusammenfassung pro Rider (nur Fakten aus ausgewählten Events):**")
        sum_df = rider_summary(df_hist_heat)
        if sum_df.empty:
            st.info("Keine verwertbaren Picks (chosen_lane / pick_order fehlen).")
        else:
            sum_df = sum_df.copy()
            sum_df["po_sort"] = sum_df["name"].map(lambda n: po_map.get(norm_name(n), 9999))
            sum_df = sum_df.sort_values(["po_sort", "name"], kind="stable")
            st.caption("favorite_share = Anteil der häufigsten Lane-Wahl (Mode) an allen Picks des Riders (0–1).")
            sum_view = sum_df[["name", "picks_n", "mean_pick_order", "mean_chosen_lane", "fav_lane", "favorite_share", "taktik"]]
            sum_view["name"] = sum_view["name"].apply(short_name)
            render_html_table(sum_view, row_h=32, min_h=200)
    else:
        st.info("Keine Lane-/Zusammenfassung verfügbar (Heat-Auswahl oder Picks fehlen).")

with tab_rounds:
    mode_time = st.radio("Modus", ["Heat", "Athleten"], index=0, horizontal=True)
    if mode_time == "Athleten":
        # Use sidebar filters (Nation + Rider) instead of a separate filter
        base_df = df_event.copy()
        if nation:
            base_df = base_df[base_df["nation"].fillna("").str.upper() == nation].copy()

        if training_live:
            selected_riders_time = rider_live_selected[:]
        else:
            selected_riders_time = rider_selected_list[:]

        riders_pool = sorted(base_df["name"].dropna().unique().tolist())
        riders = selected_riders_time if selected_riders_time else riders_pool
        rk = None
        hid = None
        gid = None
        df_heat = base_df[base_df["name"].isin(riders)].copy()
    else:
        selected_riders_time = None
    # ----------------------------
    # Round-Start Matrix (selected heat riders, current event)
    # ----------------------------
    metric_options = {
        "Start to Bottom": "start",
        "Start to Turn 1": "t1",
        "Start to Turn 2": "t2",
        "Start to Turn 3": "t3",
        "Laptime": "time",
        "Split first Straight": "split_t1",
        "Split second straight": "split_t2",
        "Split third Straight": "split_t3",
        "Split last Straight": "split_time",
    }
    metric_label = st.selectbox("Segment anzeigen:", list(metric_options.keys()), index=0)
    metric_col = metric_options[metric_label]
    st.markdown(f"**{metric_label}-Zeiten pro Runde (aktuelles Event, Rider im Heat):**")
    def round_sort_key(df_in: pd.DataFrame) -> pd.Series:
        title = df_in["round_title"].fillna("").astype(str).str.lower()
        # preferred order for BMX rounds
        order_map = {
            "round 1": 1,
            "runde 1": 1,
            "lcq": 2,
            "last chance": 2,
            "1/8 final": 3,
            "1/8 finals": 3,
            "1/8 finale": 3,
            "1/4 final": 4,
            "1/4 finals": 4,
            "1/4 finale": 4,
            "1/2 final": 5,
            "1/2 finals": 5,
            "1/2 finale": 5,
            "final": 6,
        }
        return title.map(order_map).fillna(df_in["round_key"]).fillna(99)

    if mode_time == "Athleten":
        round_order = df_event[["round_key", "round_title"]].dropna().drop_duplicates().copy()
    else:
        round_order = df_event[df_event["group_id"] == gid][["round_key", "round_title"]].dropna().drop_duplicates().copy()

    round_order["_rk"] = round_sort_key(round_order)
    round_order = round_order.sort_values(["_rk", "round_key"], kind="stable")
    round_order = round_order.drop_duplicates(subset=["round_title"], keep="first").drop(columns=["_rk"])
    round_titles = round_order["round_title"].tolist()

    if round_titles:
        # Build matrix: rows=round_title, cols=riders
        if mode_time == "Athleten":
            riders = selected_riders_time if selected_riders_time else riders_pool
        else:
            riders = (
                df_heat.sort_values(["pick_order"], na_position="last", kind="stable")["name"]
                .dropna()
                .unique()
                .tolist()
            )
        if riders:
            if mode_time == "Athleten":
                df_round = df_event[df_event["name"].isin(riders)].copy()
            else:
                df_round = df_event[(df_event["name"].isin(riders)) & (df_event["group_id"] == gid)].copy()
            df_round["start_s"] = df_round["start"].apply(parse_time_to_seconds)
            df_round["t1_s"] = df_round["t1"].apply(parse_time_to_seconds)
            df_round["t2_s"] = df_round["t2"].apply(parse_time_to_seconds)
            df_round["t3_s"] = df_round["t3"].apply(parse_time_to_seconds)
            df_round["time_s"] = df_round["time"].apply(parse_time_to_seconds)
            df_round["split_t1"] = df_round["t1_s"] - df_round["start_s"]
            df_round["split_t2"] = df_round["t2_s"] - df_round["t1_s"]
            df_round["split_t3"] = df_round["t3_s"] - df_round["t2_s"]
            df_round["split_time"] = df_round["time_s"] - df_round["t3_s"]
            if metric_col in ["start", "t1", "t2", "t3", "time"]:
                df_round["metric_s"] = df_round[metric_col + "_s"]
            else:
                df_round["metric_s"] = df_round[metric_col]
            # best (min) per rider per round
            mat = (
                df_round.groupby(["round_title", "name"])["metric_s"]
                .min()
                .reset_index()
                .pivot(index="round_title", columns="name", values="metric_s")
            )
            # keep row order
            mat = mat.reindex(round_titles)
            mat = mat.reindex(columns=riders)
            # Keep fixed round order (do not sort by rider)
            mat = mat.round(3).reset_index().rename(columns={"round_title": "Round"})

            # Add per-rider rank (based on best metric in this event) and final rank (WM)
            extra_rows = []
            try:
                # Segment rank:
                # - Modus Heat: across whole event for the category
                # - Modus Athleten: only selected riders
                if mode_time == "Athleten":
                    df_rank_src = df_round.copy()
                else:
                    if gid is not None and "group_id" in df_event.columns:
                        df_rank_src = df_event[df_event["group_id"] == gid].copy()
                    else:
                        df_rank_src = df_event.copy()
                if metric_col in ["start", "t1", "t2", "t3", "time"]:
                    df_rank_src["metric_s"] = df_rank_src[metric_col].apply(parse_time_to_seconds)
                else:
                    df_rank_src["start_s"] = df_rank_src["start"].apply(parse_time_to_seconds)
                    df_rank_src["t1_s"] = df_rank_src["t1"].apply(parse_time_to_seconds)
                    df_rank_src["t2_s"] = df_rank_src["t2"].apply(parse_time_to_seconds)
                    df_rank_src["t3_s"] = df_rank_src["t3"].apply(parse_time_to_seconds)
                    df_rank_src["time_s"] = df_rank_src["time"].apply(parse_time_to_seconds)
                    df_rank_src["split_t1"] = df_rank_src["t1_s"] - df_rank_src["start_s"]
                    df_rank_src["split_t2"] = df_rank_src["t2_s"] - df_rank_src["t1_s"]
                    df_rank_src["split_t3"] = df_rank_src["t3_s"] - df_rank_src["t2_s"]
                    df_rank_src["split_time"] = df_rank_src["time_s"] - df_rank_src["t3_s"]
                    df_rank_src["metric_s"] = df_rank_src[metric_col]
                rider_best = df_rank_src.groupby("name")["metric_s"].min()
                if not rider_best.empty:
                    ranks = rider_best.rank(method="min", ascending=True).astype("Int64")
                    rank_row = {"Round": "Segment Rank"}
                    for r in riders:
                        rank_row[r] = ranks.get(r, pd.NA)
                    extra_rows.append(rank_row)
            except Exception:
                pass

            # Final rank from Master Results (WM / EC / EM / WC)
            master = load_master_results()
            if not master.empty:
                final_map_name = final_rank_map_for_event(event_id, gid, events, master)
                if final_map_name:
                    final_row = {"Round": "Final Rank"}
                    for r in riders:
                        final_row[r] = final_map_name.get(norm_name_key(r), pd.NA)
                    extra_rows.append(final_row)

            # Format per-round cells with rank in each field (e.g., "2.345 (1)")
            display = mat.copy()
            round_set = set(round_titles)
            for idx in range(len(display)):
                if display.loc[idx, "Round"] not in round_set:
                    continue
                row_vals = pd.to_numeric(display.loc[idx, riders], errors="coerce")
                row_ranks = row_vals.rank(method="min", ascending=True)
                formatted = {}
                for r in riders:
                    v = row_vals.get(r)
                    if pd.isna(v):
                        formatted[r] = None
                    else:
                        rk = row_ranks.get(r)
                        rk_txt = "" if pd.isna(rk) else str(int(rk))
                        formatted[r] = f"{v:.3f} ({rk_txt})"
                for r, v in formatted.items():
                    display.at[idx, r] = v

            if extra_rows:
                extra_df = pd.DataFrame(extra_rows)
                display = pd.concat([display, extra_df], ignore_index=True)
            # short names for columns (ensure unique)
            col_map = {r: short_name(r) for r in riders}
            display = display.rename(columns=col_map)
            # de-duplicate columns if short names collide
            cols = list(display.columns)
            seen = {}
            new_cols = []
            for c in cols:
                if c not in seen:
                    seen[c] = 1
                    new_cols.append(c)
                else:
                    seen[c] += 1
                    new_cols.append(f"{c} ({seen[c]})")
            display.columns = new_cols
            # round labels
            round_map = {
                "round 1": "R1",
                "runde 1": "R1",
                "lcq": "LCQ",
                "last chance": "LCQ",
                "1/8 final": "1/8",
                "1/8 finals": "1/8",
                "1/8 finale": "1/8",
                "1/4 final": "1/4",
                "1/4 finals": "1/4",
                "1/4 finale": "1/4",
                "1/2 final": "1/2",
                "1/2 finals": "1/2",
                "1/2 finale": "1/2",
                "final": "F",
            }
            def _map_round(v):
                if not isinstance(v, str):
                    return v
                tl = v.strip().lower()
                if tl == "segment rank":
                    return "S. Rank"
                if tl == "final rank":
                    return "F. Rank"
                return round_map.get(tl, v)
            display["Round"] = display["Round"].apply(_map_round)

            # render as dataframe (same style as before), no index, no scroll
            display = display.where(display.notna(), "")
            display = display.astype(str).replace({"nan": ""})
            height = auto_height(display, row_h=34, min_h=140)
            st.dataframe(display, use_container_width=True, height=height, hide_index=True)
            if mode_time == "Athleten":
                st.caption("Rundenmatrix: aktuelles Event, nur gewählte Athleten")
            else:
                st.caption("Rundenmatrix: aktuelles Event, Rider im gewählten Heat")
        else:
            st.info("Keine Rider im Heat für Rundentabelle gefunden.")
    else:
        st.info("Keine Rundendaten im aktuellen Event gefunden.")

    # Analyse (ausgewählte Events) in Time Analyse
    if not analysis_event_ids:
        st.info("Wähle links mindestens ein Analyse-Event aus.")
    else:
        df_hist = df_hist_all.copy() if not df_hist_all.empty else pd.DataFrame()
        if df_hist.empty:
            st.warning("Keine Picks für die ausgewählten Analyse-Events gefunden.")
        else:
            if mode_time == "Athleten" and selected_riders_time:
                df_hist = df_hist[df_hist["name"].isin(selected_riders_time)].copy()
            else:
                heat_riders_norm = set(df_heat["name_norm"].dropna().tolist())
                heat_bibs = set(df_heat["bib"].dropna().tolist()) if "bib" in df_heat.columns else set()
                df_hist = df_hist[
                    df_hist["name_norm"].isin(heat_riders_norm) | df_hist.get("bib", pd.Series([], dtype="Int64")).isin(heat_bibs)
                ].copy()
            if df_hist.empty:
                st.info("Keine Analyse-Daten für die Rider im gewählten Heat gefunden.")
            elif show_times:
                heat_name_keys = set()
                heat_short_map = {}
                if mode_time == "Heat" and not df_heat.empty:
                    heat_name_keys = set(df_heat["name_key"].dropna().tolist())
                    # map name_key -> short display (from current heat)
                    heat_short_map = (
                        df_heat[["name_key", "name_short"]]
                        .dropna()
                        .drop_duplicates(subset=["name_key"])
                        .set_index("name_key")["name_short"]
                        .to_dict()
                    )
                df_train = load_training_for_events(analysis_event_ids)
                if not df_train.empty and "name_key" in df_train.columns:
                    if mode_time == "Athleten" and selected_riders_time:
                        df_train = df_train[df_train["name"].isin(selected_riders_time)].copy()
                    else:
                        # filter strictly to riders in selected heat (fallback by bib)
                        heat_names = set(df_heat["name_key"].dropna().tolist())
                        heat_bibs = set(df_heat["bib"].dropna().tolist()) if "bib" in df_heat.columns else set()
                        df_train = df_train[
                            df_train["name_key"].isin(heat_names) | df_train.get("bib", pd.Series([], dtype="Int64")).isin(heat_bibs)
                        ].copy()
                        # also filter by category/gender when available
                        if gid in GROUP_MAP and "category" in df_train.columns:
                            cat_label = GROUP_MAP.get(gid, "")
                            if "Elite" in cat_label:
                                df_train = df_train[df_train["category"].str.contains("Elite", case=False, na=False)]
                            elif "U23" in cat_label:
                                df_train = df_train[df_train["category"].str.contains("U23", case=False, na=False)]
                            elif "Junior" in cat_label:
                                df_train = df_train[df_train["category"].str.contains("Junior", case=False, na=False)]
                if not df_train.empty:
                    st.markdown("**Training-Start/T1 (Best & Ø Top-3) + Konstanz-Score:**")
                    ts = training_stats(df_train)
                    ts_view = ts[["name", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "cons_score"]].rename(
                        columns={
                            "name": "Rider",
                            "best_start": "Best S",
                            "best_t1": "Best T1",
                            "avg_top3_start": "Ø3 S",
                            "avg_top3_t1": "Ø3 T1",
                            "cons_score": "Score",
                        }
                    )
                    # apply heat-based short names if available
                    ts_view["name_key"] = ts_view["Rider"].apply(norm_name_key)
                    if mode_time == "Heat" and heat_name_keys:
                        ts_view = ts_view[ts_view["name_key"].isin(heat_name_keys)].copy()
                        if heat_short_map:
                            ts_view["Rider"] = ts_view["name_key"].map(heat_short_map).fillna(ts_view["Rider"])
                        else:
                            ts_view["Rider"] = ts_view["Rider"].apply(short_name)
                    else:
                        ts_view["Rider"] = ts_view["Rider"].apply(short_name)
                    ts_view = ts_view.drop(columns=["name_key"])
                    ts_view = fmt_table(ts_view, time_cols=["Best S", "Best T1", "Ø3 S", "Ø3 T1"], score_cols=["Score"])
                    st.dataframe(ts_view, use_container_width=True, height=auto_height(ts_view), hide_index=True)
                    st.caption("Training-Zeiten: ausgewählte Analyse-Events (inkl. aktuelles Event)")
                # keep race stats within same category for heat mode
                if mode_time == "Heat" and gid is not None and "group_id" in df_hist.columns:
                    df_hist = df_hist[df_hist["group_id"] == gid].copy()
                rs = race_stats(df_hist)
                if not rs.empty:
                    st.markdown("**Race-Start/T1 (Best & Ø Top-3) + Konstanz-Score:**")
                    rs_view = rs[["name", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "cons_score"]].rename(
                        columns={
                            "name": "Rider",
                            "best_start": "Best S",
                            "best_t1": "Best T1",
                            "avg_top3_start": "Ø3 S",
                            "avg_top3_t1": "Ø3 T1",
                            "cons_score": "Score",
                        }
                    )
                    rs_view["name_key"] = rs_view["Rider"].apply(norm_name_key)
                    if mode_time == "Heat" and heat_name_keys:
                        rs_view = rs_view[rs_view["name_key"].isin(heat_name_keys)].copy()
                        if heat_short_map:
                            rs_view["Rider"] = rs_view["name_key"].map(heat_short_map).fillna(rs_view["Rider"])
                        else:
                            rs_view["Rider"] = rs_view["Rider"].apply(short_name)
                    else:
                        rs_view["Rider"] = rs_view["Rider"].apply(short_name)
                    rs_view = rs_view.drop(columns=["name_key"])
                    rs_view = fmt_table(rs_view, time_cols=["Best S", "Best T1", "Ø3 S", "Ø3 T1"], score_cols=["Score"])
                    st.dataframe(rs_view, use_container_width=True, height=auto_height(rs_view), hide_index=True)
                    st.caption("Race-Zeiten: ausgewählte Analyse-Events")
