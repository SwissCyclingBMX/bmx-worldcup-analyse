import sqlite3
import os
import zipfile
import requests
import unicodedata
import datetime
import re
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

DB_PATH = "bmx.db"
DB_URL_ZIP = "https://github.com/user-attachments/files/25094120/bmx_db.zip"
DB_PATH_CLOUD = "/tmp/bmx.db"

GROUP_MAP = {
    91: "Elite Men",
    92: "Elite Women",
    93: "U23 Men",
    94: "U23 Women",
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


    # Always assign sequential rounds per year (chronological by event_id)
    df["_event_day"] = pd.to_datetime(df["event_id"].astype(str).str.slice(0, 8), format="%Y%m%d", errors="coerce")
    df["round_num"] = pd.NA
    for yr, grp in df.sort_values(["_event_day", "event_id"]).groupby("year"):
        df.loc[grp.index, "round_num"] = range(1, len(grp) + 1)

    df["label_short"] = "ROUND " + df["round_num"].astype("Int64").astype(str) + " - " + loc_clean
    df["label_short"] = df["label_short"].str.strip()
    df["label_analysis"] = df["label_short"] + " - " + df["year"].astype(str)
    df = df.drop(columns=["_event_day", "round_num"])

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


def live_event_ids_today(events_df: pd.DataFrame) -> List[str]:
    if events_df is None or events_df.empty:
        return []
    if "event_date" not in events_df.columns:
        return []

    dates = pd.to_datetime(events_df["event_date"], errors="coerce").dt.date
    today = datetime.date.today()
    live_ids = events_df.loc[dates == today, "event_id"].dropna().unique().tolist()
    return sorted(live_ids)


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

    # timing fields + uci_id
    for c in ["uci_id", "start", "t1", "t2", "t3", "t4", "time"]:
        if c not in df.columns:
            df[c] = None

    # start_dt: parse if present
    if "start_dt" in df.columns:
        df["start_dt"] = pd.to_datetime(df["start_dt"], errors="coerce")
    else:
        df["start_dt"] = pd.NaT

    # group_id numeric
    df["group_id"] = pd.to_numeric(df["group_id"], errors="coerce").astype("Int64")

    # chosen_lane numeric (treat 0 as missing)
    df["chosen_lane"] = pd.to_numeric(df["chosen_lane"], errors="coerce").astype("Int64")
    df.loc[df["chosen_lane"] <= 0, "chosen_lane"] = pd.NA

    # pick_order numeric
    df["pick_order"] = pd.to_numeric(df["pick_order"], errors="coerce").astype("Int64")

    # category label
    df["category"] = df["group_id"].astype("Int64").map(GROUP_MAP).fillna(df["group_id"].astype(str))

    # Name normalization for analysis grouping
    df["name_norm"] = df["name"].apply(norm_name)
    df["name_key"] = df["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")

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
st.set_page_config(page_title="BMX Heat Scout", layout="wide")
st.title("BMX Heat Scout")

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

# Rider filter (only affects heat filtering)
all_names = sorted([n for n in df_event["name"].dropna().unique().tolist() if isinstance(n, str) and n.strip()])
if "rider_filter" not in st.session_state:
    st.session_state["rider_filter"] = []

options_riders = st.session_state["rider_filter"] if st.session_state["rider_filter"] else all_names
rider_selected_list = st.sidebar.multiselect(
    "Rider Filter (optional, leer = alle)",
    options=options_riders,
    key="rider_filter",
)
if len(rider_selected_list) > 1:
    st.sidebar.warning("Bitte nur einen Rider auswählen.")
    st.session_state["rider_filter"] = rider_selected_list[:1]
    rider_selected_list = rider_selected_list[:1]
rider_selected = rider_selected_list[0] if rider_selected_list else "Alle"

# Kategorie Filter
level_sel = st.sidebar.multiselect(
    "Kategorie",
    options=["Elite", "U23"],
    default=["Elite", "U23"],
)
gender_sel = st.sidebar.multiselect(
    "Geschlecht",
    options=["Men", "Women"],
    default=["Men", "Women"],
)

allowed_group_ids = []
if level_sel and gender_sel:
    for lvl in level_sel:
        for gen in gender_sel:
            label = f"{lvl} {gen}"
            for gid, cat in GROUP_MAP.items():
                if cat == label:
                    allowed_group_ids.append(gid)
else:
    # Empty selection means "show all"
    allowed_group_ids = []

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
# Heats table + selection
# ----------------------------
st.subheader("Heats (nach Filter)")

heats = build_heats(df_event)

# nation filter affects which heats shown (based on startlist membership)
df_filter = df_event.copy()
if nation:
    df_filter = df_filter[df_filter["nation"].fillna("").str.upper() == nation]
if rider_selected != "Alle":
    df_filter = df_filter[df_filter["name"] == rider_selected].copy()

heats_f = build_heats(df_filter)

# Add Swiss names column always (independent of nation filter)
heats_f = add_sui_names_column(heats_f, df_event, nation_filter="SUI")

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

# Startlist (PickOrder / Lane)
st.subheader("Startliste (PickOrder / Lane)")

df_heat = df_event[(df_event["round_key"] == rk) & (df_event["heat_id"] == hid)].copy()
if gid is not None:
    df_heat = df_heat[df_heat["group_id"] == gid].copy()
df_heat["name_norm"] = df_heat["name"].apply(norm_name)
df_heat["name_key"] = df_heat["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")

df_heat = df_heat.sort_values(["pick_order"], na_position="last", kind="stable")

start_cols = ["nation", "bib", "name", "pick_order", "chosen_lane"]
start_cols = [c for c in start_cols if c in df_heat.columns]
start_df = df_heat[start_cols].copy()
start_df["name_norm"] = start_df["name"].apply(norm_name)
start_df["name_key"] = start_df["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")

# Training stats for riders in heat
if show_times:
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
        # Use only the immediately previous selected event (by event_id date)
        prev_event_id = None
        if analysis_event_ids:
            try:
                current_date = int(str(event_id)[:8])
                candidates = [e for e in analysis_event_ids if e != event_id and str(e)[:8].isdigit()]
                candidates_sorted = sorted(candidates, key=lambda x: int(str(x)[:8]))
                prevs = [e for e in candidates_sorted if int(str(e)[:8]) < current_date]
                if prevs:
                    prev_event_id = prevs[-1]
            except Exception:
                prev_event_id = None
        if prev_event_id:
            df_race_hist = df_race_hist[df_race_hist["event_id"] == prev_event_id].copy()
        else:
            df_race_hist = df_race_hist.iloc[0:0].copy()
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
            return "#222"
        base = baseline[metric]
        better = v < base  # lower is better for start times
        return "#c0392b" if better else "#1e8449"

    def combined_cell(race_v, train_v, metric_race, metric_train, is_baseline):
        race_txt = fmt_val(race_v)
        train_txt = fmt_val(train_v)
        race_color = color_for(metric_race, race_v) if race_txt else "#222"
        train_color = color_for(metric_train, train_v) if train_txt else "#666"
        # Baseline rider should stay black
        if is_baseline:
            race_color = "#222"
            train_color = "#666"
        if not race_txt and train_txt:
            race_txt = "—"
            race_color = "#999"
        return (
            f"<div style='line-height:1.1'>"
            f"<div style='font-size:14px;color:{race_color};font-weight:600'>{race_txt}</div>"
            f"<div style='font-size:11px;color:{train_color}'>{train_txt}</div>"
            f"</div>"
        )

    view = start_df.copy()
    view = view.rename(columns={"bib": "Plate"})
    view["Best Start"] = view.apply(
        lambda r: combined_cell(
            r.get("race_best_start"),
            r.get("train_best_start"),
            "race_best_start",
            "train_best_start",
            r.get("name") == rider_selected,
        ),
        axis=1,
    )
    view["Ø3 Start"] = view.apply(
        lambda r: combined_cell(
            r.get("race_avg3_start"),
            r.get("train_avg3_start"),
            "race_avg3_start",
            "train_avg3_start",
            r.get("name") == rider_selected,
        ),
        axis=1,
    )
    view["Best T1"] = view.apply(
        lambda r: combined_cell(r.get("race_best_t1"), r.get("train_best_t1"), "", "", r.get("name") == rider_selected), axis=1
    )
    view["Ø3 T1"] = view.apply(
        lambda r: combined_cell(r.get("race_avg3_t1"), r.get("train_avg3_t1"), "", "", r.get("name") == rider_selected), axis=1
    )
    view["Score"] = view.apply(
        lambda r: combined_cell(r.get("race_cons_score"), r.get("train_cons_score"), "", "", r.get("name") == rider_selected), axis=1
    )

    show_cols = [
        "nation",
        "Plate",
        "name",
        "pick_order",
        "Best Start",
        "Ø3 Start",
        "Best T1",
        "Ø3 T1",
        "Score",
        "chosen_lane",
    ]
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
    </style>
    """
    html = view.to_html(index=False, escape=False)
    html = html.replace(
        "<table border=\"1\" class=\"dataframe\">",
        "<table class='dataframe' style='width:100%;border-collapse:collapse;'>",
    )
    components.html(style + html, height=360, scrolling=True)
else:
    start_df_simple = start_df.rename(columns={"bib": "Plate"})
    start_df_simple = start_df_simple[["nation", "Plate", "name", "pick_order", "chosen_lane"]]
    st.dataframe(start_df_simple, use_container_width=True, height=320, hide_index=True)

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
metric_label = st.selectbox("Rundenzeit anzeigen:", list(metric_options.keys()), index=0)
metric_col = metric_options[metric_label]
st.markdown(f"**{metric_label}-Zeiten pro Runde (aktuelles Event, Rider im Heat):**")
round_order = (
    df_event[df_event["group_id"] == gid][["round_key", "round_title"]]
    .dropna()
    .drop_duplicates()
    .sort_values(["round_key"], kind="stable")
)
round_titles = round_order["round_title"].tolist()

if round_titles:
    # Build matrix: rows=round_title, cols=riders
    riders = (
        df_heat.sort_values(["pick_order"], na_position="last", kind="stable")["name"]
        .dropna()
        .unique()
        .tolist()
    )
    if riders:
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
        # Optional: sort rows by selected rider's times (NaN last)
        if rider_selected != "Alle" and rider_selected in mat.columns:
            sort_col = mat[rider_selected]
            sort_key = sort_col.fillna(sort_col.max() if pd.notna(sort_col.max()) else 0) + 1e9 * sort_col.isna()
            mat = mat.assign(_sort_key=sort_key).sort_values("_sort_key").drop(columns=["_sort_key"])
        mat = mat.reset_index().rename(columns={"round_title": "Round"})
        st.dataframe(mat, use_container_width=True, height=240, hide_index=True)
    else:
        st.info("Keine Rider im Heat für Rundentabelle gefunden.")
else:
    st.info("Keine Rundendaten im aktuellen Event gefunden.")

# ----------------------------
# Analysis
# ----------------------------
st.divider()
st.subheader("Analyse (ausgewählte Events)")

if not analysis_event_ids:
    st.info("Wähle links mindestens ein Analyse-Event aus.")
    st.stop()

df_hist = df_hist_all.copy() if not df_hist_all.empty else pd.DataFrame()
if df_hist.empty:
    st.warning("Keine Picks für die ausgewählten Analyse-Events gefunden.")
    st.stop()

# df_hist_all already respects allowed_group_ids

# Nur Rider aus dem gewählten Heat analysieren
heat_riders_norm = set(df_heat["name_norm"].dropna().tolist())
df_hist = df_hist[df_hist["name_norm"].isin(heat_riders_norm)].copy()
if df_hist.empty:
    st.info("Keine Analyse-Daten für die Rider im gewählten Heat gefunden.")
    st.stop()

# Training stats summary (optional)
if show_times:
    df_train = load_training_for_events(analysis_event_ids)
    if not df_train.empty and "name_key" in df_train.columns:
        df_train = df_train[df_train["name_key"].isin(set(df_heat["name_key"].dropna().tolist()))].copy()
    if not df_train.empty:
        st.markdown("**Training-Start/T1 (Best & Ø Top-3) + Konstanz-Score:**")
        ts = training_stats(df_train)
        ts = ts.rename(
            columns={
                "best_start": "best_start",
                "best_t1": "best_t1",
                "avg_top3_start": "avg3_start",
                "avg_top3_t1": "avg3_t1",
                "cons_score": "cons_score",
            }
        )
        ts_view = ts[["name", "best_start", "best_t1", "avg3_start", "avg3_t1", "cons_score"]].rename(
            columns={
                "name": "Rider",
                "best_start": "Best S",
                "best_t1": "Best T1",
                "avg3_start": "Ø3 S",
                "avg3_t1": "Ø3 T1",
                "cons_score": "Score",
            }
        )
        st.dataframe(ts_view, use_container_width=True, height=240, hide_index=True)
    # Race stats summary
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
        st.dataframe(rs_view, use_container_width=True, height=240, hide_index=True)

# Summary per rider
st.markdown("**Zusammenfassung pro Rider (nur Fakten aus ausgewählten Events):**")
sum_df = rider_summary(df_hist)

if sum_df.empty:
    st.info("Keine verwertbaren Picks (chosen_lane / pick_order fehlen).")
else:
    # Explain favorite_share once
    st.caption("favorite_share = Anteil der häufigsten Lane-Wahl (Mode) an allen Picks des Riders (0–1).")
    st.dataframe(
        sum_df[["name", "picks_n", "mean_pick_order", "mean_chosen_lane", "fav_lane", "favorite_share", "taktik"]],
        use_container_width=True,
        height=320,
        hide_index=True,
    )

# Lane distribution per rider with pick_order before chosen_lane
st.markdown("**Lane-Verteilung (pick_order → chosen_lane) pro Rider:**")
dist_df = lane_distribution(df_hist)
if dist_df.empty:
    st.info("Keine Verteilung berechenbar (chosen_lane / pick_order fehlen).")
else:
    # Visual cue: pick_order -> chosen_lane
    def move_symbol(row):
        try:
            po = int(row["pick_order"])
            cl = int(row["chosen_lane"])
        except Exception:
            return ""
        if cl == po:
            return ""
        return "→" if cl > po else "←"

    dist_df = dist_df.copy()
    dist_df["move"] = dist_df.apply(move_symbol, axis=1)

    def color_move(val):
        if val == "→":
            return "color: #1f77b4; font-weight: 700;"  # blue
        if val == "←":
            return "color: #2ca02c; font-weight: 700;"  # green
        return ""

    styled = (
        dist_df[["name", "pick_order", "chosen_lane", "move", "count"]]
        .style.applymap(color_move, subset=["move"])
    )
    st.dataframe(styled, use_container_width=True, height=360, hide_index=True)
