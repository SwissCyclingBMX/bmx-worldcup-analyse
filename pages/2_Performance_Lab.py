import sqlite3
from typing import Optional
import unicodedata

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st


DB_PATH = "bmx.db"

GROUP_MAP = {
    91: ("Elite", "Men"),
    92: ("Elite", "Women"),
    93: ("Junior", "Men"),
    94: ("Junior", "Women"),
    95: ("U23", "Men"),
    96: ("U23", "Women"),
}

ROUND_ORDER = {
    "Round 1": 10,
    "LCQ": 20,
    "Last Chance": 20,
    "1/16 Finals": 30,
    "1/8 Finals": 40,
    "1/4 Finals": 50,
    "1/2 Finals": 60,
    "Final": 70,
    "Finals": 70,
}


def infer_series(event_id: str) -> str:
    e = str(event_id or "").lower()
    if "_euc_" in e:
        return "EC"
    if "_em_" in e:
        return "EM"
    if "_wch_" in e:
        return "WM"
    if e.endswith("_bmx"):
        return "WC"
    return "Other"


def parse_event_date(event_date: Optional[str], event_id: str) -> pd.Timestamp:
    s = str(event_date or "").strip()
    if s:
        dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
        if pd.notna(dt):
            return dt
    try:
        return pd.to_datetime(str(event_id)[:8], format="%Y%m%d", errors="coerce")
    except Exception:
        return pd.NaT


def short_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        return ""
    p = [x for x in name.strip().split() if x]
    if len(p) == 1:
        return p[0]
    first = p[0][0].upper()
    last = p[-1].upper()
    return f"{first}. {last}"


def clean_spaces(s: str) -> str:
    return " ".join(str(s or "").strip().split())


def norm_uci_id(v: str) -> str:
    s = "".join(ch for ch in str(v or "").strip() if ch.isdigit())
    return s if len(s) >= 8 else ""


def norm_name_key(name: str) -> str:
    s = clean_spaces(name).lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(sorted(s.split()))


@st.cache_data(show_spinner=False)
def load_perf_data(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    picks = pd.read_sql_query(
        """
        SELECT
          p.event_id,
          p.group_id,
          p.round_title,
          p.heat_title,
          p.bib,
          p.name,
          p.nation,
          p.uci_id,
          p.pick_order,
          p.lane_idx,
          p.rank AS heat_rank,
          p.start,
          p.t1,
          p.t2,
          p.t3,
          p.time
        FROM picks p
        """,
        conn,
    )
    events = pd.read_sql_query(
        """
        SELECT event_id, display_name, event_date
        FROM events
        """,
        conn,
    )
    conn.close()

    if picks.empty:
        return picks

    df = picks.merge(events, on="event_id", how="left")
    df["series"] = df["event_id"].apply(infer_series)
    df["year"] = pd.to_numeric(df["event_id"].astype(str).str[:4], errors="coerce").astype("Int64")
    df["event_dt"] = [parse_event_date(ed, eid) for ed, eid in zip(df["event_date"], df["event_id"])]

    # Numeric conversions.
    for c in ["start", "t1", "t2", "t3", "time"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    cats = df["group_id"].map(lambda g: GROUP_MAP.get(int(g), ("Unknown", "Unknown")) if pd.notna(g) else ("Unknown", "Unknown"))
    df["category"] = [c[0] for c in cats]
    df["gender"] = [c[1] for c in cats]

    df["nation_u"] = df["nation"].fillna("").astype(str).str.upper().str.strip()
    df["name_clean"] = df["name"].fillna("").astype(str).apply(clean_spaces)
    df["name_key"] = df["name_clean"].apply(norm_name_key)
    df["uci_norm"] = df["uci_id"].fillna("").astype(str).apply(norm_uci_id)

    # Canonical rider identity:
    # 1) by UCI ID if available
    # 2) else by normalized name + nation (order/accents-insensitive)
    df["rider_id"] = np.where(
        df["uci_norm"] != "",
        "uci:" + df["uci_norm"],
        "name:" + df["name_key"] + "|" + df["nation_u"],
    )
    df["rider_label_raw"] = (df["name_clean"] + " (" + df["nation_u"] + ")").str.strip()

    # Choose one stable label per rider_id to avoid duplicate filter entries.
    label_counts = (
        df.groupby(["rider_id", "rider_label_raw"], as_index=False)
        .size()
        .rename(columns={"size": "cnt"})
    )
    label_counts["name_len"] = label_counts["rider_label_raw"].astype(str).str.len()
    label_counts = label_counts.sort_values(
        ["rider_id", "cnt", "name_len", "rider_label_raw"],
        ascending=[True, False, False, True],
    )
    label_map = label_counts.drop_duplicates(subset=["rider_id"]).set_index("rider_id")["rider_label_raw"].to_dict()
    df["rider_label"] = df["rider_id"].map(label_map).fillna(df["rider_label_raw"])

    name_part = df["rider_label"].str.replace(r"\s*\([A-Z]{2,3}\)\s*$", "", regex=True)
    df["rider_short"] = name_part.apply(short_name)
    return df


def round_sort_value(round_title: str) -> int:
    return ROUND_ORDER.get(str(round_title), 999)


st.title("Performance Lab")
st.caption("Mehrjahres-Analyse mit Segment-Ranking und Gap zum Schnellsten.")

df = load_perf_data()
if df.empty:
    st.warning("Keine Daten in picks/events gefunden.")
    st.stop()

series_opts = sorted([x for x in df["series"].dropna().unique().tolist() if x])
year_opts = sorted([int(x) for x in df["year"].dropna().unique().tolist()], reverse=True)
cat_opts = [x for x in ["Elite", "U23", "Junior"] if x in set(df["category"].dropna().unique().tolist())]
gender_opts = [x for x in ["Men", "Women"] if x in set(df["gender"].dropna().unique().tolist())]
nation_opts = sorted([x for x in df["nation"].dropna().astype(str).str.upper().unique().tolist() if x])

col_f1, col_f2, col_f3 = st.columns(3)
with col_f1:
    sel_series = st.multiselect("Serie", series_opts, default=series_opts)
with col_f2:
    sel_years = st.multiselect("Jahr", year_opts, default=year_opts)
with col_f3:
    segment_label = st.selectbox("Segment", ["Start", "T1", "Lap Time"], index=0)

col_f4, col_f5, col_f6 = st.columns(3)
with col_f4:
    sel_cat = st.multiselect("Kategorie", cat_opts, default=cat_opts)
with col_f5:
    sel_gender = st.multiselect("Geschlecht", gender_opts, default=gender_opts)
with col_f6:
    sel_nation = st.multiselect("Nation", nation_opts, default=[])

df_view = df.copy()
if sel_series:
    df_view = df_view[df_view["series"].isin(sel_series)]
if sel_years:
    df_view = df_view[df_view["year"].isin(sel_years)]
if sel_cat:
    df_view = df_view[df_view["category"].isin(sel_cat)]
if sel_gender:
    df_view = df_view[df_view["gender"].isin(sel_gender)]
if sel_nation:
    df_view = df_view[df_view["nation"].astype(str).str.upper().isin(sel_nation)]

rider_opts = sorted(df_view["rider_label"].dropna().unique().tolist())
sel_riders = st.multiselect("Rider (optional)", rider_opts, default=[])
if sel_riders:
    df_view = df_view[df_view["rider_label"].isin(sel_riders)]

seg_col = {"Start": "start", "T1": "t1", "Lap Time": "time"}[segment_label]
seg_name = {"Start": "Start", "T1": "T1", "Lap Time": "Laptime"}[segment_label]

df_seg = df_view.dropna(subset=[seg_col]).copy()
if df_seg.empty:
    st.warning(f"Keine Werte für Segment `{seg_name}` mit den aktuellen Filtern.")
    st.stop()

bucket_cols = ["event_id", "group_id", "round_title", "heat_title"]
df_seg["best_seg"] = df_seg.groupby(bucket_cols)[seg_col].transform("min")
df_seg["seg_rank"] = df_seg.groupby(bucket_cols)[seg_col].rank(method="min", ascending=True)
df_seg["gap_s"] = df_seg[seg_col] - df_seg["best_seg"]
df_seg["gap_pct"] = ((df_seg[seg_col] / df_seg["best_seg"]) - 1.0) * 100.0

rider_summary = (
    df_seg.groupby(["rider_id", "rider_label", "rider_short"], as_index=False)
    .agg(
        heats=("rider_id", "count"),
        events=("event_id", "nunique"),
        avg_gap_pct=("gap_pct", "mean"),
        med_gap_pct=("gap_pct", "median"),
        best_gap_pct=("gap_pct", "min"),
        avg_seg_rank=("seg_rank", "mean"),
        top3_rate=("seg_rank", lambda s: float((s <= 3).mean() * 100.0)),
        seg_std=("gap_pct", "std"),
    )
)
rider_summary["seg_std"] = rider_summary["seg_std"].fillna(0.0)
rider_summary["consistency"] = (100.0 - (rider_summary["seg_std"] * 12.0)).clip(lower=0.0, upper=100.0)
rider_summary["gap_index"] = (100.0 - (rider_summary["avg_gap_pct"] * 20.0)).clip(lower=0.0, upper=100.0)
rider_summary["selection_score"] = (
    0.45 * rider_summary["gap_index"] + 0.35 * rider_summary["consistency"] + 0.20 * rider_summary["top3_rate"]
)
rider_summary = rider_summary.sort_values(["selection_score", "heats"], ascending=[False, False])

metric_c1, metric_c2, metric_c3, metric_c4 = st.columns(4)
metric_c1.metric("Rider", f"{df_seg['rider_id'].nunique()}")
metric_c2.metric("Heats", f"{len(df_seg)}")
metric_c3.metric("Events", f"{df_seg['event_id'].nunique()}")
metric_c4.metric(f"Segment", seg_name)

st.markdown("### Selection Scoreboard")
board = rider_summary[
    ["rider_short", "heats", "events", "avg_gap_pct", "avg_seg_rank", "top3_rate", "consistency", "selection_score"]
].rename(
    columns={
        "rider_short": "Rider",
        "avg_gap_pct": "Avg Gap %",
        "avg_seg_rank": "Avg Seg Rank",
        "top3_rate": "Top3 %",
        "consistency": "Consistency",
        "selection_score": "Score",
    }
)
for c in ["Avg Gap %", "Avg Seg Rank", "Top3 %", "Consistency", "Score"]:
    board[c] = pd.to_numeric(board[c], errors="coerce").round(2)
st.dataframe(board, use_container_width=True, hide_index=True, height=min(620, 46 + 32 * (len(board) + 1)))

st.markdown("### Visuals")
top_n = st.slider("Max Rider in Charts", min_value=4, max_value=20, value=10, step=1)
chart_riders = rider_summary.head(top_n)["rider_id"].tolist()
df_chart = df_seg[df_seg["rider_id"].isin(chart_riders)].copy()
label_map = rider_summary.set_index("rider_id")["rider_short"].to_dict()
df_chart["rider_short"] = df_chart["rider_id"].map(label_map).fillna(df_chart["rider_short"])

col_v1, col_v2 = st.columns(2)
with col_v1:
    st.markdown(f"**1) Gap % zum Schnellsten ({seg_name})**")
    gap_bar = (
        alt.Chart(
            rider_summary.head(top_n).assign(rider_short=lambda d: d["rider_id"].map(label_map).fillna(d["rider_short"]))
        )
        .mark_bar()
        .encode(
            x=alt.X("avg_gap_pct:Q", title="Avg Gap %"),
            y=alt.Y("rider_short:N", sort="-x", title="Rider"),
            color=alt.Color("selection_score:Q", title="Score", scale=alt.Scale(scheme="tealblues")),
            tooltip=["rider_label:N", "avg_gap_pct:Q", "avg_seg_rank:Q", "top3_rate:Q", "selection_score:Q"],
        )
        .properties(height=380)
    )
    st.altair_chart(gap_bar, use_container_width=True)

with col_v2:
    st.markdown(f"**2) Rank vs Gap Scatter ({seg_name})**")
    scatter = (
        alt.Chart(rider_summary.head(top_n).assign(rider_short=lambda d: d["rider_id"].map(label_map).fillna(d["rider_short"])))
        .mark_circle(opacity=0.85)
        .encode(
            x=alt.X("avg_gap_pct:Q", title="Avg Gap %"),
            y=alt.Y("avg_seg_rank:Q", title="Avg Segment Rank"),
            size=alt.Size("heats:Q", title="Heats"),
            color=alt.Color("rider_short:N", title="Rider"),
            tooltip=["rider_label:N", "heats:Q", "events:Q", "avg_gap_pct:Q", "avg_seg_rank:Q", "selection_score:Q"],
        )
        .properties(height=380)
    )
    st.altair_chart(scatter, use_container_width=True)

st.markdown(f"**3) Trend über Events ({seg_name})**")
trend = (
    df_chart.groupby(["event_dt", "event_id", "display_name", "rider_id", "rider_short"], as_index=False)
    .agg(avg_gap_pct=("gap_pct", "mean"), avg_rank=("seg_rank", "mean"))
    .sort_values("event_dt")
)
if not trend.empty:
    trend["event_label"] = trend["event_dt"].dt.strftime("%Y-%m-%d").fillna("") + " | " + trend["display_name"].fillna("")
    trend_line = (
        alt.Chart(trend)
        .mark_line(point=True)
        .encode(
            x=alt.X("event_dt:T", title="Event Date"),
            y=alt.Y("avg_gap_pct:Q", title="Avg Gap %"),
            color=alt.Color("rider_short:N", title="Rider"),
            tooltip=["rider_short:N", "event_label:N", "avg_gap_pct:Q", "avg_rank:Q"],
        )
        .properties(height=340)
    )
    st.altair_chart(trend_line, use_container_width=True)

st.markdown(f"**4) Heatmap Runde x Rider ({seg_name}, Avg Gap %)**")
heat = (
    df_chart.groupby(["rider_id", "rider_short", "round_title"], as_index=False)
    .agg(avg_gap_pct=("gap_pct", "mean"))
)
if not heat.empty:
    heat["round_sort"] = heat["round_title"].apply(round_sort_value)
    heat = heat.sort_values(["round_sort", "round_title", "rider_short"])
    heatmap = (
        alt.Chart(heat)
        .mark_rect()
        .encode(
            x=alt.X("rider_short:N", title="Rider"),
            y=alt.Y("round_title:N", sort=alt.SortField(field="round_sort", order="ascending"), title="Round"),
            color=alt.Color("avg_gap_pct:Q", title="Avg Gap %", scale=alt.Scale(scheme="blues")),
            tooltip=["rider_short:N", "round_title:N", "avg_gap_pct:Q"],
        )
        .properties(height=340)
    )
    st.altair_chart(heatmap, use_container_width=True)

st.caption(
    "Hinweis: Der Vergleich über Events basiert auf Segment-Rang und Gap zum Schnellsten pro Heat "
    "(nicht auf rohen Sekunden zwischen unterschiedlichen Tracks)."
)
