import sqlite3
import datetime as dt
import unicodedata
import pandas as pd
import streamlit as st

GROUP_ID_TO_CATEGORY = {
    91: "Elite Men",
    92: "Elite Women",
    93: "U23 Men",
    94: "U23 Women",
}


DB_PATH = "bmx.db"


# ----------------------------
# Normalization / Keys
# ----------------------------
def clean_spaces(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return " ".join(s.strip().split())


def display_name(name: str) -> str:
    # human-readable (keeps accents), but cleans spaces
    return clean_spaces(name)


def rider_key(name: str) -> str:
    """
    Stable matching key:
    - trim + collapse spaces
    - remove accents
    - uppercase
    """
    name = clean_spaces(name)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return name.upper()


def parse_time_str(t: str):
    """Parse 'HH:MM:SS.mmm' -> datetime (dummy date)."""
    if not t or not isinstance(t, str):
        return None
    try:
        return dt.datetime.strptime(t, "%H:%M:%S.%f")
    except Exception:
        return None


# ----------------------------
# DB loading
# ----------------------------
@st.cache_data(ttl=10)
def load_events() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT event_id, display_name, location, country, event_date
        FROM events
        ORDER BY event_id DESC
        """,
        conn,
    )
    conn.close()
    if df.empty:
        return df
    df["label"] = df["display_name"].fillna(df["event_id"])
    return df


@st.cache_data(ttl=10)
def load_picks_for_event(event_id: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM picks WHERE event_id = ?",
        conn,
        params=(event_id,),
    )
    conn.close()
    if df.empty:
        return df

    # types for stable sorting
    for c in ["group_id", "round_key", "heat_id", "bib", "pick_order", "lane_idx"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["name"] = df["name"].fillna("").map(display_name)
    df["rider_key"] = df["name"].map(rider_key)
    df["start_dt"] = df["start_time_string"].apply(parse_time_str)
    return df


@st.cache_data(ttl=10)
def load_picks_for_events(event_ids: list[str]) -> pd.DataFrame:
    if not event_ids:
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    qmarks = ",".join(["?"] * len(event_ids))
    df = pd.read_sql_query(
        f"""
        SELECT event_id, round_title, heat_title, name, nation, pick_order, lane_idx
        FROM picks
        WHERE event_id IN ({qmarks})
        """,
        conn,
        params=event_ids,
    )
    conn.close()

    if df.empty:
        return df

    for c in ["pick_order", "lane_idx"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["name"] = df["name"].fillna("").map(display_name)
    df["rider_key"] = df["name"].map(rider_key)
    return df


def build_heats(df_event: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "group_id",
        "round_key",
        "round_title",
        "heat_id",
        "heat_title",
        "heat_status",
        "start_time_string",
        "start_dt",
    ]
    heats = df_event[cols].drop_duplicates().copy()
    heats = heats.sort_values(["start_dt", "round_key", "heat_id"], na_position="last", kind="stable").reset_index(drop=True)
    return heats


def heat_label(row: pd.Series) -> str:
    category = GROUP_ID_TO_CATEGORY.get(int(row["group_id"]), "Unknown")
    stime = row["start_time_string"] or ""
    return f"{category} | {row['round_title']} | {row['heat_title']} | {stime}"


def classify_pick_style(group: pd.DataFrame) -> pd.Series:
    """
    Facts-only classification based on lane_idx distribution:
      - favorite lane if one lane >= 60%
      - else inside/middle/outside based on mean lane_idx
    """
    n = len(group)
    vc = group["lane_idx"].value_counts(dropna=True)
    top_lane = int(vc.index[0]) if len(vc) else None
    top_share = float(vc.iloc[0] / n) if len(vc) else 0.0
    mean_lane = float(group["lane_idx"].mean()) if group["lane_idx"].notna().any() else None

    if top_lane is not None and top_share >= 0.60:
        tactic = f"Lieblingslane ({top_lane})"
    elif mean_lane is None:
        tactic = "Unklar"
    elif mean_lane <= 3.0:
        tactic = "Innenbahn-orientiert"
    elif mean_lane >= 6.0:
        tactic = "Aussenbahn-orientiert"
    else:
        tactic = "Mitte"

    return pd.Series(
        {
            "picks_n": n,
            "mean_chosen_lane": round(mean_lane, 2) if mean_lane is not None else None,
            "favorite_lane": top_lane,
            "favorite_share": round(top_share, 2),
            "taktik": tactic,
        }
    )


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="BMX Heat Scout", layout="wide")
st.title("BMX Heat Scout")

events = load_events()
if events.empty:
    st.error("events-Tabelle ist leer. Bitte ingest.py für mindestens ein Event laufen lassen.")
    st.stop()

# ---- Sidebar: Event
event_label = st.sidebar.selectbox("Event", events["label"].tolist(), index=0)
event_id = events.loc[events["label"] == event_label, "event_id"].iloc[0]

# load current event picks (needed for rider autocomplete + heats)
df_event = load_picks_for_event(event_id)
if df_event.empty:
    st.warning(f"Keine Picks-Daten für {event_id}.")
    st.stop()

# ---- Sidebar: Filters
nation = st.sidebar.text_input("Nation Filter (z.B. SUI) – leer = alle", value="SUI").strip().upper()


only_upcoming = st.sidebar.checkbox("Nur anstehende Heats (Live)", value=True)

# ---- Sidebar: Analysis events (default current event)
analysis_event_labels = st.sidebar.multiselect(
    "Analyse: Events einbeziehen",
    options=events["label"].tolist(),
    default=[event_label],
)
analysis_event_ids = events.loc[events["label"].isin(analysis_event_labels), "event_id"].tolist()

# Rider autocomplete uses names from ANALYSIS events (so riders don't "disappear" when current event changes)
df_names_pool = load_picks_for_events(analysis_event_ids)
if df_names_pool.empty:
    all_names = sorted([n for n in df_event["name"].dropna().unique().tolist() if n])
else:
    all_names = sorted([n for n in df_names_pool["name"].dropna().unique().tolist() if n])

rider_names_selected = st.sidebar.multiselect(
    "Rider auswählen (Autocomplete) – optional",
    options=all_names,
    default=[],
)

# ----------------------------
# Heat list based on filters
# ----------------------------
df_filter = df_event.copy()

if nation:
    df_filter = df_filter[df_filter["nation"] == nation]

if rider_names_selected:
    # convert selected display names -> rider_key
    selected_keys = {rider_key(n) for n in rider_names_selected}
    df_filter = df_filter[df_filter["rider_key"].isin(selected_keys)]

if df_filter.empty:
    st.info("Keine Daten passend zu den Filtern (Nation / Rider-Auswahl).")
    st.stop()

# heats that contain filtered riders
heat_keys = df_filter[["round_key", "heat_id"]].drop_duplicates()
heats_all = build_heats(df_event)
heats = heats_all.merge(heat_keys, on=["round_key", "heat_id"], how="inner")

if only_upcoming:
    now = dt.datetime.now()
    upcoming = heats[
        (heats["heat_status"] != "Confirmed") |
        (heats["start_dt"].isna()) |
        (heats["start_dt"] > now)
    ].copy()

    if upcoming.empty:
        st.info("Hinweis: Keine 'anstehenden' Heats gefunden (Event vermutlich nicht live). Zeige stattdessen alle Heats.")
    else:
        heats = upcoming

heats = heats.sort_values(["start_dt", "round_key", "heat_id"], na_position="last", kind="stable").reset_index(drop=True)

# Schweizer Namen pro Heat (aus df_event, unabhängig von aktuellen Filtern)
sui_by_heat = (
    df_event[df_event["nation"] == "SUI"]
    .groupby(["round_key", "heat_id"])["name"]
    .apply(lambda s: ", ".join(sorted(pd.unique(s.dropna()))))
    .reset_index(name="SUI")
)

heats = heats.merge(sui_by_heat, on=["round_key", "heat_id"], how="left")
heats["SUI"] = heats["SUI"].fillna("")


st.subheader("Heats (nach Filter)")

heats_show = heats.copy()
heats_show["category"] = heats_show["group_id"].map(GROUP_ID_TO_CATEGORY).fillna("Unknown")

st.dataframe(
    heats_show[
        [
            "category",
            "round_title",
            "heat_title",
            "SUI",
            "heat_status",
            "start_time_string",
        ]
    ],
    use_container_width=True,
    height=260,
)

if heats.empty:
    st.warning("Keine Heats passend zu den Filtern.")
    st.stop()

# ---- Heat selection (manual)
heat_rows = list(heats.to_dict("records"))

GROUP_TO_CATEGORY = {
    91: "Elite Men",
    92: "Elite Women",
    93: "U23 Men",
    94: "U23 Women",
}

def heat_label_row(r: dict) -> str:
    gid = r.get("group_id")
    try:
        gid_int = int(gid) if gid is not None else None
    except Exception:
        gid_int = None

    category = GROUP_TO_CATEGORY.get(gid_int, f"G{gid}" if gid is not None else "Unknown")

    sui = (r.get("SUI") or "").strip()
    sui_part = f" | SUI: {sui}" if sui else ""

    return f"{category} | {r.get('round_title','')} | {r.get('heat_title','')} | {r.get('start_time_string','')}{sui_part}"

chosen = st.selectbox(
    "Heat auswählen",
    options=heat_rows,
    format_func=heat_label_row,
)

rk = int(chosen["round_key"])
hid = int(chosen["heat_id"])

# ----------------------------
# Start list (chosen heat)
# ----------------------------
startlist = df_event[(df_event["round_key"] == rk) & (df_event["heat_id"] == hid)].copy()
startlist = startlist.sort_values(["pick_order", "lane_idx", "bib"], kind="stable")

st.subheader("Startliste (PickOrder / Chosen Lane)")
startlist_show = startlist[["nation", "bib", "name", "pick_order", "lane_idx"]].rename(columns={"lane_idx": "chosen_lane"})
st.dataframe(startlist_show, use_container_width=True, height=420)

# ----------------------------
# Analysis across selected events (for riders in this heat)
# ----------------------------
st.subheader("Analyse: Lane-Picks der Rider im ausgewählten Heat (über gewählte Events)")

riders_in_heat = startlist[["rider_key", "name"]].drop_duplicates().copy()
if riders_in_heat.empty:
    st.info("Keine Rider im Heat gefunden.")
    st.stop()

if not analysis_event_ids:
    st.info("Keine Analyse-Events ausgewählt.")
    st.stop()

df_hist = load_picks_for_events(analysis_event_ids)
if df_hist.empty:
    st.info("Keine Picks-Daten in den ausgewählten Analyse-Events.")
    st.stop()

# filter to riders in heat using rider_key (accent-safe)
df_hist = df_hist[df_hist["rider_key"].isin(set(riders_in_heat["rider_key"]))].copy()
if df_hist.empty:
    st.info("Für die ausgewählten Events wurden keine passenden Picks für diese Rider gefunden.")
    st.stop()

# choose a display name per rider_key (most frequent in selected events, fallback to heat name)
disp = (
    pd.concat(
        [
            df_hist[["rider_key", "name"]],
            riders_in_heat.rename(columns={"name": "name"}),  # ensure present
        ],
        ignore_index=True,
    )
    .dropna()
)

display_map = (
    disp.groupby("rider_key")["name"]
    .agg(lambda s: s.value_counts().index[0] if len(s) else "")
    .to_dict()
)

df_hist["display_name"] = df_hist["rider_key"].map(display_map).fillna(df_hist["name"])

# ---- Summary per rider_key
summary = (
    df_hist.groupby("rider_key", dropna=False)
    .apply(classify_pick_style)
    .reset_index()
)
summary["display_name"] = summary["rider_key"].map(display_map).fillna(summary["rider_key"])
summary = summary[["display_name", "picks_n", "mean_chosen_lane", "favorite_lane", "favorite_share", "taktik"]]
summary = summary.sort_values(["taktik", "favorite_share", "picks_n"], ascending=[True, False, False], kind="stable")

st.markdown("**Zusammenfassung pro Rider (Fakten aus den ausgewählten Events):**")
st.dataframe(summary, use_container_width=True, height=280)

# ---- Distribution: pick_order -> chosen_lane
st.markdown("**Lane-Verteilung (Pick-Order → chosen_lane) pro Rider:**")
dist = (
    df_hist.groupby(["display_name", "pick_order", "lane_idx"], dropna=False)
    .size()
    .reset_index(name="count")
    .rename(columns={"lane_idx": "chosen_lane"})
    .sort_values(["display_name", "pick_order", "chosen_lane"], kind="stable")
)
st.dataframe(dist, use_container_width=True, height=380)
