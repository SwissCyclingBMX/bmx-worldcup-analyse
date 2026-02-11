import sqlite3
import unicodedata
from typing import Optional

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
    "round 1": 10,
    "moto": 10,
    "seeding": 10,
    "lcq": 20,
    "last chance": 20,
    "1/16": 30,
    "1/8": 40,
    "1/4": 50,
    "1/2": 60,
    "final": 70,
}


def infer_event_type(event_id: str) -> str:
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


def clean_spaces(s: str) -> str:
    return " ".join(str(s or "").strip().split())


def norm_uci_id(v) -> str:
    s = "".join(ch for ch in str(v or "").strip() if ch.isdigit())
    return s if len(s) >= 8 else ""


def norm_name_key(name: str) -> str:
    s = clean_spaces(name).lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(sorted(s.split()))


def short_name(name: str) -> str:
    n = clean_spaces(name)
    if not n:
        return ""
    p = n.split()
    if len(p) == 1:
        return p[0]
    return f"{p[0][0].upper()}. {p[-1].upper()}"


def parse_event_date(event_date: Optional[str], event_id: str) -> pd.Timestamp:
    s = clean_spaces(event_date or "")
    if s:
        dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
        if pd.notna(dt):
            return dt
    return pd.to_datetime(str(event_id)[:8], format="%Y%m%d", errors="coerce")


def round_sort(round_title: str, round_key) -> int:
    if pd.notna(round_key):
        try:
            return int(round_key)
        except Exception:
            pass
    t = str(round_title or "").lower()
    for k, v in ROUND_ORDER.items():
        if k in t:
            return v
    return 999


def classify_phase(round_title: str, round_sort_value: int) -> str:
    t = str(round_title or "").lower()
    if "final" in t:
        return "Final"
    if any(x in t for x in ["1/16", "1/8", "1/4", "1/2", "lcq", "last chance"]):
        return "KO"
    if any(x in t for x in ["round 1", "moto", "seeding"]) or round_sort_value <= 12:
        return "Early"
    return "KO"


def bin_pos(pos: float) -> str:
    if pd.isna(pos):
        return "NA"
    p = int(pos)
    if p <= 2:
        return "1-2"
    if p <= 4:
        return "3-4"
    if p <= 8:
        return "5-8"
    return "9+"


@st.cache_data(show_spinner=False)
def load_runs(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """
        SELECT
          p.event_id, p.group_id, p.round_key, p.round_title, p.heat_id, p.heat_title,
          p.name, p.nation, p.uci_id, p.rank,
          p.start, p.t1, p.t2, p.t3, p.time,
          e.display_name, e.location, e.country, e.event_date
        FROM picks p
        LEFT JOIN events e ON e.event_id = p.event_id
        """,
        conn,
    )
    conn.close()

    if df.empty:
        return df

    for c in ["start", "t1", "t2", "t3", "time", "rank", "heat_id", "round_key"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["finish"] = df["time"]
    df["event_type"] = df["event_id"].apply(infer_event_type)
    df["event_dt"] = [parse_event_date(ed, eid) for ed, eid in zip(df["event_date"], df["event_id"])]
    df["year"] = pd.to_numeric(df["event_id"].astype(str).str[:4], errors="coerce").astype("Int64")
    df["location"] = df["location"].fillna("Unknown").astype(str).apply(clean_spaces).replace("", "Unknown")
    df["nation"] = df["nation"].fillna("").astype(str).str.upper().str.strip()

    cats = df["group_id"].map(lambda g: GROUP_MAP.get(int(g), ("Unknown", "Unknown")) if pd.notna(g) else ("Unknown", "Unknown"))
    df["category"] = [c[0] for c in cats]
    df["gender"] = [c[1] for c in cats]

    df["name_clean"] = df["name"].fillna("").astype(str).apply(clean_spaces)
    df["name_key"] = df["name_clean"].apply(norm_name_key)
    df["uci_norm"] = df["uci_id"].apply(norm_uci_id)
    df["rider_id"] = np.where(df["uci_norm"] != "", "uci:" + df["uci_norm"], "name:" + df["name_key"] + "|" + df["nation"])
    df["rider_label_raw"] = df["name_clean"] + " (" + df["nation"] + ")"

    counts = (
        df.groupby(["rider_id", "rider_label_raw"], as_index=False)
        .size()
        .rename(columns={"size": "cnt"})
    )
    counts["len"] = counts["rider_label_raw"].astype(str).str.len()
    counts = counts.sort_values(["rider_id", "cnt", "len", "rider_label_raw"], ascending=[True, False, False, True])
    label_map = counts.drop_duplicates(subset=["rider_id"]).set_index("rider_id")["rider_label_raw"].to_dict()
    df["rider_label"] = df["rider_id"].map(label_map).fillna(df["rider_label_raw"])

    short_src = df["rider_label"].str.replace(r"\s*\([A-Z]{2,3}\)\s*$", "", regex=True)
    df["rider_short"] = short_src.apply(short_name)

    df["round_sort"] = [round_sort(rt, rk) for rt, rk in zip(df["round_title"], df["round_key"])]
    df["phase"] = [classify_phase(rt, rs) for rt, rs in zip(df["round_title"], df["round_sort"])]
    return df


def add_heat_relative_metrics(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    heat_cols = ["event_id", "group_id", "heat_id", "round_sort"]

    for seg in ["start", "t1", "t2", "t3", "finish"]:
        med_col = f"{seg}_median"
        delta_col = f"{seg}_delta"
        rank_col = f"pos_{seg}" if seg != "finish" else "pos_finish_est"
        pct_col = f"{seg}_pct"

        grp = out.groupby(heat_cols)[seg]
        out[med_col] = grp.transform("median")
        out[delta_col] = out[seg] - out[med_col]
        out[rank_col] = grp.rank(method="min", ascending=True)
        field = grp.transform("count")
        out[pct_col] = np.where(field > 1, (out[rank_col] - 1) / (field - 1), np.nan)

    # Prefer official rank for finish position if available.
    out["pos_finish"] = out["rank"].where(out["rank"].notna(), out["pos_finish_est"])
    return out


def apply_scope(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    if scope == "nur Finals":
        return df[df["phase"] == "Final"].copy()
    if scope == "nur KO":
        return df[df["phase"] == "KO"].copy()
    return df.copy()


def make_event_label(df: pd.DataFrame) -> pd.Series:
    return (
        df["event_dt"].dt.strftime("%Y-%m-%d").fillna(df["event_id"].astype(str))
        + " | "
        + df["location"].fillna("Unknown").astype(str)
    )


st.title("Athlete Insights")
st.caption("Trend, Segmente, Positionen, Druck, Track-Profile, Benchmark, Fatigue und Result-Trend.")

all_runs = load_runs()
if all_runs.empty:
    st.warning("Keine Daten gefunden.")
    st.stop()

event_type_opts = sorted([x for x in all_runs["event_type"].dropna().unique().tolist() if x])
year_opts = sorted([int(x) for x in all_runs["year"].dropna().unique().tolist()], reverse=True)
cat_opts = [x for x in ["Elite", "U23", "Junior"] if x in set(all_runs["category"].dropna().unique().tolist())]
gender_opts = [x for x in ["Men", "Women"] if x in set(all_runs["gender"].dropna().unique().tolist())]
loc_opts = sorted([x for x in all_runs["location"].dropna().unique().tolist() if x])
nation_opts = sorted([x for x in all_runs["nation"].dropna().unique().tolist() if x])

f1, f2, f3, f4, f5, f6 = st.columns(6)
with f1:
    sel_years = st.multiselect("Jahr", year_opts, default=year_opts)
with f2:
    sel_event_types = st.multiselect("Event Type", event_type_opts, default=event_type_opts)
with f3:
    sel_categories = st.multiselect("Kategorie", cat_opts, default=cat_opts)
with f4:
    sel_gender = st.multiselect("Geschlecht", gender_opts, default=gender_opts)
with f5:
    sel_nations = st.multiselect("Nation (Rider)", nation_opts, default=[])
with f6:
    sel_locations = st.multiselect("Location (optional)", loc_opts, default=[])

base_scope = all_runs.copy()
if sel_years:
    base_scope = base_scope[base_scope["year"].isin(sel_years)]
if sel_event_types:
    base_scope = base_scope[base_scope["event_type"].isin(sel_event_types)]
if sel_categories:
    base_scope = base_scope[base_scope["category"].isin(sel_categories)]
if sel_gender:
    base_scope = base_scope[base_scope["gender"].isin(sel_gender)]
if sel_locations:
    base_scope = base_scope[base_scope["location"].isin(sel_locations)]

rider_pool = base_scope.copy()
if sel_nations:
    rider_pool = rider_pool[rider_pool["nation"].isin(sel_nations)]

rider_opts_filtered = set(rider_pool["rider_label"].dropna().unique().tolist())
selected_prev = st.session_state.get("insight_riders", [])
rider_opts = sorted(rider_opts_filtered.union(set(selected_prev)))
if not rider_opts and not selected_prev:
    st.warning("Keine Rider fuer die aktuelle Filterauswahl.")
    st.stop()
sel_riders = st.multiselect("Rider Filter (optional, leer = alle)", rider_opts, key="insight_riders")

if sel_riders:
    selected_ids = rider_pool.loc[rider_pool["rider_label"].isin(sel_riders), "rider_id"].dropna().unique().tolist()
else:
    selected_ids = rider_pool["rider_id"].dropna().unique().tolist()

if not selected_ids:
    st.warning("Keine Rider fuer die aktuelle Auswahl.")
    st.stop()

base_rel = add_heat_relative_metrics(base_scope)
runs_sel = base_rel[base_rel["rider_id"].isin(selected_ids)].copy()
runs_sel = runs_sel.sort_values(["event_dt", "event_id", "round_sort", "heat_id"])
runs_sel["event_label"] = make_event_label(runs_sel)

tabs = st.tabs(
    [
        "Athlete Trend",
        "Segment Profile",
        "Positions & Overtakes",
        "Pressure Performance",
        "Track Profile",
        "Benchmark",
        "Fatigue",
        "Results Trend",
    ]
)

with tabs[0]:
    st.subheader("Athlete Trend")
    show_start = st.toggle("Start Delta anzeigen", value=True, key="trend_show_start")
    show_t1 = st.toggle("T1 Delta anzeigen", value=True, key="trend_show_t1")

    plot = runs_sel.copy()
    plot["order"] = plot.groupby("rider_id").cumcount()
    melt_cols = [("finish_delta", "Finish Delta")]
    if show_start and plot["start_delta"].notna().any():
        melt_cols.append(("start_delta", "Start Delta"))
    if show_t1 and plot["t1_delta"].notna().any():
        melt_cols.append(("t1_delta", "T1 Delta"))

    plot_long = pd.concat(
        [
            plot[["order", "event_label", "event_id", "round_title", "heat_id", "heat_title", "rank", "rider_short"]].assign(
                metric=label,
                delta=plot[col],
            )
            for col, label in melt_cols
        ],
        ignore_index=True,
    )
    plot_long = plot_long.dropna(subset=["delta"])

    if not plot_long.empty:
        trend_chart = (
            alt.Chart(plot_long)
            .mark_line(point=True)
            .encode(
                x=alt.X("order:Q", title="Chronologische Runs"),
                y=alt.Y("delta:Q", title="Delta vs Heat-Median (s)"),
                color=alt.Color("rider_short:N", title="Rider"),
                strokeDash=alt.StrokeDash("metric:N", title="Metrik"),
                tooltip=["rider_short:N", "event_label:N", "event_id:N", "round_title:N", "heat_title:N", "rank:Q", "metric:N", "delta:Q"],
            )
            .properties(height=360)
        )
        st.altair_chart(trend_chart, use_container_width=True)
    else:
        st.info("Keine verwertbaren Delta-Daten fuer die aktuelle Rider-Auswahl.")

    summary = (
        runs_sel.groupby(["year", "rider_short"], as_index=False)
        .agg(
            n_runs=("event_id", "count"),
            mean_finish_delta=("finish_delta", "mean"),
            std_finish_delta=("finish_delta", "std"),
            mean_start_delta=("start_delta", "mean"),
            mean_t1_delta=("t1_delta", "mean"),
            best_finish_delta=("finish_delta", "min"),
        )
        .sort_values("year", ascending=False)
    )
    for c in ["mean_finish_delta", "std_finish_delta", "mean_start_delta", "mean_t1_delta", "best_finish_delta"]:
        summary[c] = pd.to_numeric(summary[c], errors="coerce").round(4)
    st.dataframe(summary, use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Segment Strength Profile")
    scope = st.selectbox("Scope", ["alle ausgewaehlten Events", "nur Finals", "nur KO-Runden"], index=0, key="seg_scope")
    scope_key = {"alle ausgewaehlten Events": "alle", "nur Finals": "nur Finals", "nur KO-Runden": "nur KO"}[scope]
    df_scope = apply_scope(runs_sel, scope_key)
    min_n = st.slider("Min Messungen pro Segment", min_value=5, max_value=80, value=30, step=5)

    seg_defs = [("start", "Start"), ("t1", "T1"), ("t2", "T2"), ("t3", "T3"), ("finish", "Finish")]
    seg_rows = []
    for seg, label in seg_defs:
        d = df_scope[f"{seg}_delta"] if f"{seg}_delta" in df_scope.columns else pd.Series(dtype=float)
        p = df_scope[f"{seg}_pct"] if f"{seg}_pct" in df_scope.columns else pd.Series(dtype=float)
        n = int(d.notna().sum())
        seg_rows.append(
            {
                "Segment": label,
                "n": n,
                "mean_delta": d.mean(),
                "std_delta": d.std(),
                "best_delta": d.min(),
                "worst_delta": d.max(),
                "mean_percentile": p.mean(),
            }
        )
    seg_stats = pd.DataFrame(seg_rows)
    seg_ok = seg_stats[seg_stats["n"] >= min_n].copy()

    if not seg_ok.empty:
        bars = (
            alt.Chart(seg_ok)
            .mark_bar()
            .encode(
                x=alt.X("Segment:N", sort=None),
                y=alt.Y("mean_delta:Q", title="Mean Delta (s)"),
                tooltip=["Segment:N", "n:Q", "mean_delta:Q", "std_delta:Q", "mean_percentile:Q"],
            )
            .properties(height=300)
        )
        err = (
            alt.Chart(seg_ok.assign(y_low=lambda d: d["mean_delta"] - d["std_delta"], y_high=lambda d: d["mean_delta"] + d["std_delta"]))
            .mark_errorbar()
            .encode(x="Segment:N", y="y_low:Q", y2="y_high:Q")
        )
        st.altair_chart((bars + err), use_container_width=True)

        heat_long = pd.concat(
            [
                df_scope[["event_id", "event_dt", "location"]]
                .assign(Segment=label, value=df_scope[f"{seg}_delta"])
                for seg, label in seg_defs
                if f"{seg}_delta" in df_scope.columns
            ],
            ignore_index=True,
        )
        heat_long = heat_long.dropna(subset=["value"])
        if not heat_long.empty:
            event_seg = (
                heat_long.groupby(["event_id", "event_dt", "location", "Segment"], as_index=False)
                .agg(mean_delta=("value", "mean"))
            )
            event_seg["event_label"] = event_seg["event_dt"].dt.strftime("%Y-%m-%d").fillna(event_seg["event_id"]) + " | " + event_seg["location"]
            heat_chart = (
                alt.Chart(event_seg)
                .mark_rect()
                .encode(
                    x=alt.X("Segment:N", sort=None),
                    y=alt.Y("event_label:N", sort="-x", title="Event"),
                    color=alt.Color("mean_delta:Q", scale=alt.Scale(scheme="redblue")),
                    tooltip=["event_label:N", "Segment:N", "mean_delta:Q"],
                )
                .properties(height=320)
            )
            st.altair_chart(heat_chart, use_container_width=True)
    else:
        st.info("Zu wenig Segment-Daten fuer die aktuelle Auswahl.")

    disp = seg_stats.copy()
    for c in ["mean_delta", "std_delta", "best_delta", "worst_delta", "mean_percentile"]:
        disp[c] = pd.to_numeric(disp[c], errors="coerce").round(4)
    st.dataframe(disp, use_container_width=True, hide_index=True)

with tabs[2]:
    st.subheader("Positions & Overtakes")
    scope = st.selectbox("Scope", ["alle Laeufe", "nur KO", "nur Finals"], index=0, key="pos_scope")
    scope_key = {"alle Laeufe": "alle", "nur KO": "nur KO", "nur Finals": "nur Finals"}[scope]
    rider_scope = apply_scope(runs_sel, scope_key).copy()

    if rider_scope.empty:
        st.info("Keine Laeufe im gewaehlten Scope.")
    else:
        rider_scope["delta_start_to_t1"] = rider_scope["pos_start"] - rider_scope["pos_t1"]
        rider_scope["delta_t1_to_finish"] = rider_scope["pos_t1"] - rider_scope["pos_finish"]
        rider_scope["delta_start_to_finish"] = rider_scope["pos_start"] - rider_scope["pos_finish"]

        deltas = {
            "Start->T1": rider_scope["delta_start_to_t1"],
            "T1->Finish": rider_scope["delta_t1_to_finish"],
            "Start->Finish": rider_scope["delta_start_to_finish"],
        }
        bar_rows = []
        for k, s in deltas.items():
            if s.notna().sum() > 0:
                bar_rows.append({"phase": k, "mean_delta": s.mean(), "std": s.std(), "n": int(s.notna().sum())})
        if bar_rows:
            bdf = pd.DataFrame(bar_rows)
            b = (
                alt.Chart(bdf)
                .mark_bar()
                .encode(x="phase:N", y=alt.Y("mean_delta:Q", title="Durchschnittliche Positionsgewinne"), tooltip=["phase:N", "mean_delta:Q", "std:Q", "n:Q"])
                .properties(height=260)
            )
            st.altair_chart(b, use_container_width=True)

        scat = rider_scope.dropna(subset=["pos_start", "pos_finish"]).copy()
        if not scat.empty:
            scat_chart = (
                alt.Chart(scat)
                .mark_circle(opacity=0.8)
                .encode(
                    x=alt.X("pos_start:Q", title="Position Start"),
                    y=alt.Y("pos_finish:Q", title="Position Finish"),
                    color=alt.Color("round_title:N"),
                    tooltip=["event_id:N", "round_title:N", "heat_title:N", "pos_start:Q", "pos_finish:Q", "start:Q", "t1:Q", "finish:Q"],
                )
                .properties(height=300)
            )
            st.altair_chart(scat_chart, use_container_width=True)

            mat = pd.crosstab(scat["pos_start"].apply(bin_pos), scat["pos_finish"].apply(bin_pos)).reset_index().melt(
                id_vars="pos_start", var_name="finish_bin", value_name="count"
            )
            mat = mat.rename(columns={"pos_start": "start_bin"})
            hm = (
                alt.Chart(mat)
                .mark_rect()
                .encode(
                    x="finish_bin:N",
                    y="start_bin:N",
                    color=alt.Color("count:Q", scale=alt.Scale(scheme="blues")),
                    tooltip=["start_bin:N", "finish_bin:N", "count:Q"],
                )
                .properties(height=220)
            )
            st.altair_chart(hm, use_container_width=True)

        n_heats = len(rider_scope)
        delta_sf = rider_scope["delta_start_to_finish"]
        summary = pd.DataFrame(
            [
                {
                    "n_heats": n_heats,
                    "mean_delta_start_to_finish": delta_sf.mean(),
                    "%gained": (delta_sf > 0).mean() * 100.0,
                    "%lost": (delta_sf < 0).mean() * 100.0,
                    "%neutral": (delta_sf == 0).mean() * 100.0,
                }
            ]
        )
        st.dataframe(summary.round(4), use_container_width=True, hide_index=True)

with tabs[3]:
    st.subheader("Pressure Performance")
    scope = st.selectbox("Scope", ["alle", "nur KO", "nur Finals"], index=0, key="press_scope")
    rider_scope = apply_scope(runs_sel, scope)
    if rider_scope.empty:
        st.info("Keine Daten im Scope.")
    else:
        phase_tbl = (
            rider_scope.groupby("phase", as_index=False)
            .agg(
                n=("event_id", "count"),
                mean_finish_delta=("finish_delta", "mean"),
                std_finish_delta=("finish_delta", "std"),
                mean_start_delta=("start_delta", "mean"),
                mean_t1_delta=("t1_delta", "mean"),
            )
            .sort_values("phase")
        )
        st.dataframe(phase_tbl.round(4), use_container_width=True, hide_index=True)

        pmap = phase_tbl.set_index("phase")
        if "Final" in pmap.index and "Early" in pmap.index:
            drop_finish = pmap.loc["Final", "mean_finish_delta"] - pmap.loc["Early", "mean_finish_delta"]
            drop_start = pmap.loc["Final", "mean_start_delta"] - pmap.loc["Early", "mean_start_delta"]
            drop_t1 = pmap.loc["Final", "mean_t1_delta"] - pmap.loc["Early", "mean_t1_delta"]
            st.metric("Pressure Dropoff Finish (Final - Early)", f"{drop_finish:.4f} s")
            st.caption(f"Start Dropoff: {drop_start:.4f} s | T1 Dropoff: {drop_t1:.4f} s")
        else:
            st.caption("Final oder Early nicht ausreichend vorhanden, Dropoff nicht berechnet.")

        box_df = rider_scope[["phase", "finish_delta"]].dropna()
        if not box_df.empty:
            box = alt.Chart(box_df).mark_boxplot().encode(x="phase:N", y=alt.Y("finish_delta:Q", title="Finish Delta (s)"))
            st.altair_chart(box, use_container_width=True)

with tabs[4]:
    st.subheader("Track Profile")
    top_n = st.slider("Top N Locations nach Runs", min_value=3, max_value=20, value=8, step=1, key="track_topn")
    show_unknown = st.toggle("Unknown anzeigen", value=False, key="track_unknown")
    tr = runs_sel.copy()
    if not show_unknown:
        tr = tr[tr["location"].str.lower() != "unknown"]

    agg = (
        tr.groupby("location", as_index=False)
        .agg(
            n_runs=("event_id", "count"),
            mean_finish_delta=("finish_delta", "mean"),
            std_finish_delta=("finish_delta", "std"),
            mean_start_delta=("start_delta", "mean"),
            mean_t1_delta=("t1_delta", "mean"),
            median_rank=("rank", "median"),
            finals_pct=("phase", lambda s: (s == "Final").mean() * 100.0),
        )
        .sort_values("n_runs", ascending=False)
    )
    agg = agg.head(top_n)
    if agg.empty:
        st.info("Keine Track-Daten vorhanden.")
    else:
        bar = alt.Chart(agg).mark_bar().encode(
            x=alt.X("mean_finish_delta:Q", title="Mean Finish Delta (s)"),
            y=alt.Y("location:N", sort="-x"),
            tooltip=["location:N", "n_runs:Q", "mean_finish_delta:Q", "std_finish_delta:Q", "mean_start_delta:Q", "mean_t1_delta:Q"],
        )
        st.altair_chart(bar.properties(height=280), use_container_width=True)

        sc = alt.Chart(agg).mark_circle(opacity=0.8).encode(
            x=alt.X("mean_start_delta:Q", title="Mean Start Delta"),
            y=alt.Y("mean_finish_delta:Q", title="Mean Finish Delta"),
            size=alt.Size("n_runs:Q"),
            color=alt.Color("location:N", legend=None),
            tooltip=["location:N", "n_runs:Q", "mean_start_delta:Q", "mean_finish_delta:Q", "finals_pct:Q"],
        )
        st.altair_chart(sc.properties(height=280), use_container_width=True)
        st.dataframe(agg.round(4), use_container_width=True, hide_index=True)

with tabs[5]:
    st.subheader("Benchmark")
    mode = st.selectbox("Benchmark Mode", ["Gap to Winner", "Gap to Top3 mean", "Percentile in Heat"], index=0, key="bench_mode")
    b = base_rel.copy()
    heat_cols = ["event_id", "group_id", "heat_id", "round_sort"]
    b["winner_finish"] = b.groupby(heat_cols)["finish"].transform("min")
    b["top3_mean"] = b.groupby(heat_cols)["finish"].transform(lambda s: s.nsmallest(3).mean() if s.notna().sum() >= 3 else np.nan)
    b["finish_pctile"] = b["finish_pct"]

    rr = b[b["rider_id"].isin(selected_ids)].copy()
    rr["gap_winner"] = rr["finish"] - rr["winner_finish"]
    rr["gap_top3"] = rr["finish"] - rr["top3_mean"]
    rr["event_label"] = make_event_label(rr)
    rr = rr.sort_values(["event_dt", "event_id", "round_sort", "heat_id"])
    rr["order"] = rr.groupby("rider_id").cumcount()

    metric_col = {"Gap to Winner": "gap_winner", "Gap to Top3 mean": "gap_top3", "Percentile in Heat": "finish_pctile"}[mode]
    mm = rr.dropna(subset=[metric_col]).copy()
    if mm.empty:
        st.info("Keine Daten fuer diesen Benchmark-Modus.")
    else:
        line = alt.Chart(mm).mark_line(point=True).encode(
            x=alt.X("order:Q", title="Chronologische Runs"),
            y=alt.Y(f"{metric_col}:Q", title=mode),
            color=alt.Color("rider_short:N", title="Rider"),
            strokeDash=alt.StrokeDash("phase:N", title="Phase"),
            tooltip=["rider_short:N", "event_label:N", "round_title:N", "heat_title:N", "rank:Q", f"{metric_col}:Q"],
        )
        st.altair_chart(line.properties(height=320), use_container_width=True)

        hist = alt.Chart(mm).mark_bar().encode(x=alt.X(f"{metric_col}:Q", bin=True), y="count()")
        st.altair_chart(hist.properties(height=220), use_container_width=True)

        tcols = ["event_id", "event_dt", "location", "round_title", "heat_title", "rank", metric_col]
        view = mm[tcols].copy()
        st.dataframe(view.round(4), use_container_width=True, hide_index=True)

with tabs[6]:
    st.subheader("Fatigue / Day Progression")
    ev = runs_sel.copy()
    ev = ev.sort_values(["rider_id", "event_id", "round_sort", "heat_id"])
    agg_rows = []
    for (rid, event_id), g in ev.groupby(["rider_id", "event_id"]):
        g = g.dropna(subset=["finish_delta"])
        if len(g) < 2:
            continue
        first = g.iloc[0]
        last = g.iloc[-1]
        agg_rows.append(
            {
                "event_id": event_id,
                "rider_id": rid,
                "rider_short": first["rider_short"],
                "event_dt": first["event_dt"],
                "location": first["location"],
                "n_runs": len(g),
                "first_finish_delta": first["finish_delta"],
                "last_finish_delta": last["finish_delta"],
                "dropoff": last["finish_delta"] - first["finish_delta"],
                "first_start_delta": first["start_delta"],
                "last_start_delta": last["start_delta"],
                "first_t1_delta": first["t1_delta"],
                "last_t1_delta": last["t1_delta"],
            }
        )
    prog = pd.DataFrame(agg_rows)
    if prog.empty:
        st.info("Zu wenig Events mit >=2 Runden.")
    else:
        prog["event_label"] = (
            prog["event_dt"].dt.strftime("%Y-%m-%d").fillna(prog["event_id"])
            + " | "
            + prog["location"]
            + " | "
            + prog["rider_short"]
        )
        bar = alt.Chart(prog).mark_bar().encode(
            x=alt.X("event_label:N", sort=None, title="Event"),
            y=alt.Y("dropoff:Q", title="Dropoff Finish Delta (last - first)"),
            color=alt.Color("rider_short:N", title="Rider"),
            tooltip=["event_label:N", "dropoff:Q", "n_runs:Q"],
        )
        st.altair_chart(bar.properties(height=300), use_container_width=True)

        scat = alt.Chart(prog).mark_circle(size=80).encode(
            x=alt.X("n_runs:Q", title="Runs im Event"),
            y=alt.Y("dropoff:Q", title="Dropoff"),
            color=alt.Color("rider_short:N", title="Rider"),
            tooltip=["event_label:N", "n_runs:Q", "dropoff:Q"],
        )
        st.altair_chart(scat.properties(height=220), use_container_width=True)

        summary = pd.DataFrame(
            [
                {
                    "mean_dropoff": prog["dropoff"].mean(),
                    "%events_improved": (prog["dropoff"] < 0).mean() * 100.0,
                    "%events_worse": (prog["dropoff"] > 0).mean() * 100.0,
                }
            ]
        )
        st.dataframe(summary.round(4), use_container_width=True, hide_index=True)

with tabs[7]:
    st.subheader("Results Trend")
    rr = runs_sel.copy()
    rr = rr.sort_values(["rider_id", "event_dt", "event_id", "round_sort", "heat_id"])

    rows = []
    for (rid, event_id), g in rr.groupby(["rider_id", "event_id"]):
        g = g.copy().sort_values(["round_sort", "heat_id"])
        reached_phase = "Early"
        if (g["phase"] == "Final").any():
            reached_phase = "Final"
        elif (g["phase"] == "KO").any():
            reached_phase = "KO"

        est = False
        rank_val = np.nan
        finals = g[g["phase"] == "Final"]
        if not finals.empty and finals["rank"].notna().any():
            rank_val = finals["rank"].min()
        elif g["rank"].notna().any():
            # Best rank in latest reached round.
            max_rs = g["round_sort"].max()
            gg = g[g["round_sort"] == max_rs]
            if gg["rank"].notna().any():
                rank_val = gg["rank"].min()
            else:
                rank_val = g["rank"].min()
        elif g["finish"].notna().any():
            # Fallback estimate from finish position.
            rank_val = g["finish"].rank(method="min", ascending=True).min()
            est = True

        rows.append(
            {
                "event_id": event_id,
                "rider_id": rid,
                "rider_short": g["rider_short"].iloc[0],
                "event_dt": g["event_dt"].iloc[0],
                "location": g["location"].iloc[0],
                "year": g["year"].iloc[0],
                "final_rank": rank_val,
                "reached_phase": reached_phase,
                "n_runs": len(g),
                "estimated": est,
            }
        )

    res = pd.DataFrame(rows).sort_values(["event_dt", "event_id"])
    if res.empty:
        st.info("Keine Event-Ergebnisse fuer die aktuelle Rider-Auswahl.")
    else:
        res["event_label"] = (
            res["event_dt"].dt.strftime("%Y-%m-%d").fillna(res["event_id"])
            + " | "
            + res["location"]
            + " | "
            + res["rider_short"]
        )
        line = alt.Chart(res.dropna(subset=["final_rank"])).mark_line(point=True).encode(
            x=alt.X("event_dt:T", title="Event Date"),
            y=alt.Y("final_rank:Q", title="Final Rank", scale=alt.Scale(reverse=True)),
            color=alt.Color("rider_short:N", title="Rider"),
            strokeDash=alt.StrokeDash("reached_phase:N", title="Phase"),
            tooltip=["event_label:N", "final_rank:Q", "reached_phase:N", "n_runs:Q", "estimated:N"],
        )
        st.altair_chart(line.properties(height=320), use_container_width=True)

        phase_counts = res.groupby(["year", "reached_phase"], as_index=False).size().rename(columns={"size": "count"})
        bars = alt.Chart(phase_counts).mark_bar().encode(
            x=alt.X("year:O"),
            y=alt.Y("count:Q"),
            color=alt.Color("reached_phase:N"),
            tooltip=["year:O", "reached_phase:N", "count:Q"],
        )
        st.altair_chart(bars.properties(height=260), use_container_width=True)

        yearly = (
            res.groupby("year", as_index=False)
            .agg(
                n_events=("event_id", "count"),
                avg_final_rank=("final_rank", "mean"),
                finals_count=("reached_phase", lambda s: int((s == "Final").sum())),
                top8_count=("final_rank", lambda s: int((s <= 8).sum())),
                dnq_count=("reached_phase", lambda s: int((s == "Early").sum())),
            )
            .sort_values("year", ascending=False)
        )
        st.dataframe(yearly.round(3), use_container_width=True, hide_index=True)

st.caption(
    "Alle Deltas werden heat-relativ berechnet (Zeit des Riders minus Heat-Median), "
    "um Track-/Tages-Effekte zu reduzieren."
)
