import sqlite3
import unicodedata
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st

DB_PATH = "bmx.db"

GROUP_MAP = {
    91: "Elite Men",
    92: "Elite Women",
    93: "U23 Men",
    94: "U23 Women",
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
def load_events() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
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

    if df.empty:
        return df

    # Derive year from event_id (YYYYMMDD_...)
    df["year"] = df["event_id"].astype(str).str.slice(0, 4)

    # Create pretty label:
    # Prefer display_name; append location if missing; disambiguate duplicates by appending event_id.
    base = df["display_name"].fillna(df["event_id"]).astype(str).str.strip()

    # Make it shorter/cleaner
    base = base.str.replace(r"\s+", " ", regex=True)

    label_pretty = base.copy()

    # If location exists, append " - location" (simple & robust; no per-row regex)
    loc = df["location"].fillna("").astype(str).str.strip()
    mask = loc != ""
    label_pretty.loc[mask] = label_pretty.loc[mask] + " - " + loc.loc[mask]


    df["label_pretty"] = label_pretty

    # Disambiguate duplicates
    dup = df["label_pretty"].duplicated(keep=False)
    df["label"] = df["label_pretty"]
    df.loc[dup, "label"] = df.loc[dup, "label_pretty"] + " (" + df.loc[dup, "event_id"] + ")"

    return df


@st.cache_data(ttl=30)
def load_picks_for_event(event_id: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT * FROM picks WHERE event_id = ?",
            conn,
            params=[event_id],
        )
    finally:
        conn.close()
    return normalize_picks_df(df)


@st.cache_data(ttl=30)
def load_picks_for_events(event_ids: List[str]) -> pd.DataFrame:
    event_ids = [e for e in event_ids if e]
    if not event_ids:
        return pd.DataFrame()

    in_sql, params = safe_in_clause(event_ids)
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            f"SELECT * FROM picks WHERE event_id IN {in_sql}",
            conn,
            params=params,
        )
    finally:
        conn.close()
    return normalize_picks_df(df)


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

    # start_dt: parse if present
    if "start_dt" in df.columns:
        df["start_dt"] = pd.to_datetime(df["start_dt"], errors="coerce")
    else:
        df["start_dt"] = pd.NaT

    # group_id numeric
    df["group_id"] = pd.to_numeric(df["group_id"], errors="coerce").astype("Int64")

    # chosen_lane numeric
    df["chosen_lane"] = pd.to_numeric(df["chosen_lane"], errors="coerce").astype("Int64")

    # pick_order numeric
    df["pick_order"] = pd.to_numeric(df["pick_order"], errors="coerce").astype("Int64")

    # category label
    df["category"] = df["group_id"].astype("Int64").map(GROUP_MAP).fillna(df["group_id"].astype(str))

    # Name normalization for analysis grouping
    df["name_norm"] = df["name"].apply(norm_name)

    return df


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
    not_upcoming = status.isin(
        [
            "finished",
            "completed",
            "done",
            "ended",
            "official",
        ]
    )

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

    # only rows with actual picks
    dfp = df_hist.dropna(subset=["name_norm", "chosen_lane", "pick_order"]).copy()
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

events = load_events()
if events.empty:
    st.error("events-Tabelle ist leer. Bitte ingest.py für mindestens ein Event laufen lassen.")
    st.stop()

# Sidebar: Event Auswahl
st.sidebar.header("Event Auswahl")

mode = st.sidebar.radio("Modus", ["Live", "Archiv (Jahre)"], horizontal=True)

# Which set of events is shown in the CURRENT event dropdown?
if mode == "Live":
    # heuristic: newest year as "live pool"
    default_year = events["year"].iloc[0]
    df_current_pool = events[events["year"] == default_year].copy()
else:
    years = sorted(events["year"].dropna().unique().tolist(), reverse=True)
    year_sel = st.sidebar.selectbox("Jahr", years, index=0)
    df_current_pool = events[events["year"] == year_sel].copy()

df_current_pool = df_current_pool.sort_values("event_id", ascending=False)

event_label_current = st.sidebar.selectbox("Event", df_current_pool["label"].tolist(), index=0)
event_id = df_current_pool.loc[df_current_pool["label"] == event_label_current, "event_id"].iloc[0]
st.sidebar.caption(f"Aktives Event: {event_id}")

# Current event picks
df_event = load_picks_for_event(event_id)
if df_event.empty:
    st.warning(f"Keine Picks-Daten für {event_id}.")
    st.stop()

# Filters
nation = st.sidebar.text_input("Nation Filter (z.B. SUI) – leer = alle", value="SUI").strip().upper()
only_upcoming = st.sidebar.checkbox("Nur anstehende Heats (Live)", value=True)

# Analysis selection (independent from current)
st.sidebar.subheader("Analyse: Events einbeziehen")

analysis_years = st.sidebar.multiselect(
    "Jahre (Analyse)",
    options=sorted(events["year"].dropna().unique().tolist(), reverse=True),
    default=[events["year"].iloc[0]],
)

df_analysis_pool = events[events["year"].isin(analysis_years)].copy()
df_analysis_pool = df_analysis_pool.sort_values("event_id", ascending=False)

analysis_locations = st.sidebar.multiselect(
    "Orte (Analyse)",
    options=sorted([x for x in df_analysis_pool["location"].dropna().unique().tolist() if str(x).strip()]),
    default=[],
)

if analysis_locations:
    df_analysis_pool = df_analysis_pool[df_analysis_pool["location"].isin(analysis_locations)].copy()

analysis_event_labels = st.sidebar.multiselect(
    "Events (Analyse) – frei kombinierbar",
    options=df_analysis_pool["label"].tolist(),
    default=[event_label_current] if event_label_current in df_analysis_pool["label"].tolist() else [],
)

analysis_event_ids = events.loc[events["label"].isin(analysis_event_labels), "event_id"].tolist()

# Rider autocomplete based on ANALYSIS pool (so riders don't disappear)
df_names_pool = load_picks_for_events(analysis_event_ids) if analysis_event_ids else pd.DataFrame()
if df_names_pool.empty:
    all_names = sorted([n for n in df_event["name"].dropna().unique().tolist() if isinstance(n, str) and n.strip()])
else:
    all_names = sorted([n for n in df_names_pool["name"].dropna().unique().tolist() if isinstance(n, str) and n.strip()])

rider_names_selected = st.sidebar.multiselect(
    "Rider auswählen (Autocomplete) – optional",
    options=all_names,
    default=[],
)

# ----------------------------
# Heats table + selection
# ----------------------------
st.subheader("Heats (nach Filter)")

heats = build_heats(df_event)

# nation filter affects which heats shown (based on startlist membership)
df_filter = df_event.copy()
if nation:
    df_filter = df_filter[df_filter["nation"].fillna("").str.upper() == nation]

heats_f = build_heats(df_filter)

# Add Swiss names column always (independent of nation filter)
heats_f = add_sui_names_column(heats_f, df_event, nation_filter="SUI")

# Upcoming filter
if only_upcoming:
    tmp = filter_upcoming_heats(heats_f)
    if tmp.empty:
        st.info("Hinweis: Keine 'anstehenden' Heats gefunden (Event vermutlich nicht live). Zeige stattdessen alle Heats.")
    else:
        heats_f = tmp

# Show heats table
show_cols = ["category", "round_title", "heat_title", "SUI", "heat_status", "start_time_string", "round_key", "heat_id"]
show_cols = [c for c in show_cols if c in heats_f.columns]
st.dataframe(heats_f[show_cols], use_container_width=True, height=280)

# Heat selectbox with Swiss names embedded
if heats_f.empty:
    st.warning("Keine Heats passend zu den Filtern.")
    st.stop()

options = [heat_label_row(r) for _, r in heats_f.iterrows()]
choice = st.selectbox("Heat auswählen", options, index=0)
chosen = heats_f.iloc[options.index(choice)]
rk = int(chosen["round_key"])
hid = int(chosen["heat_id"])

# Startlist (PickOrder / Lane)
st.subheader("Startliste (PickOrder / Lane)")

df_heat = df_event[(df_event["round_key"] == rk) & (df_event["heat_id"] == hid)].copy()

# optional: rider filter (names)
if rider_names_selected:
    df_heat = df_heat[df_heat["name"].isin(rider_names_selected)].copy()

df_heat = df_heat.sort_values(["pick_order"], na_position="last", kind="stable")

start_cols = ["nation", "bib", "name", "pick_order", "chosen_lane"]
start_cols = [c for c in start_cols if c in df_heat.columns]
st.dataframe(df_heat[start_cols], use_container_width=True, height=320)

# ----------------------------
# Analysis
# ----------------------------
st.divider()
st.subheader("Analyse (ausgewählte Events)")

if not analysis_event_ids:
    st.info("Wähle links mindestens ein Analyse-Event aus.")
    st.stop()

df_hist = load_picks_for_events(analysis_event_ids)
if df_hist.empty:
    st.warning("Keine Picks für die ausgewählten Analyse-Events gefunden.")
    st.stop()

# Optional: nation filter on analysis? (use same nation field if set)
# (Du kannst das später separat machen – ich lasse es hier bewusst einfach.)
if rider_names_selected:
    df_hist = df_hist[df_hist["name"].isin(rider_names_selected)].copy()

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
    )

# Lane distribution per rider with pick_order before chosen_lane
st.markdown("**Lane-Verteilung (pick_order → chosen_lane) pro Rider:**")
dist_df = lane_distribution(df_hist)
if dist_df.empty:
    st.info("Keine Verteilung berechenbar (chosen_lane / pick_order fehlen).")
else:
    st.dataframe(
        dist_df[["name", "pick_order", "chosen_lane", "count"]],
        use_container_width=True,
        height=360,
    )
