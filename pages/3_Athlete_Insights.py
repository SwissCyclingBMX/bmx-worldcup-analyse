import sqlite3
import unicodedata
from typing import Optional
import re

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
    event_id_dt = pd.to_datetime(str(event_id)[:8], format="%Y%m%d", errors="coerce")
    if s:
        # Robust handling for mixed numeric formats:
        # WC can be YYYY-MM-DD, while some sources may use YYYY-DD-MM.
        m = re.match(r"^(\\d{4})[-/](\\d{1,2})[-/](\\d{1,2})$", s)
        if m:
            y, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            candidates = []
            # Prefer YYYY-MM-DD.
            if 1 <= a <= 12 and 1 <= b <= 31:
                dt = pd.to_datetime(f"{y:04d}-{a:02d}-{b:02d}", errors="coerce")
                if pd.notna(dt):
                    candidates.append(dt)
            # Fallback YYYY-DD-MM.
            if 1 <= b <= 12 and 1 <= a <= 31:
                dt = pd.to_datetime(f"{y:04d}-{b:02d}-{a:02d}", errors="coerce")
                if pd.notna(dt):
                    candidates.append(dt)
            # Resolve ambiguity using event_id date when available.
            if candidates:
                if pd.notna(event_id_dt):
                    candidates = sorted(candidates, key=lambda d: abs((d - event_id_dt).days))
                return candidates[0]
        # Generic fallback for text dates (e.g., 21 SEP 2025).
        dt = pd.to_datetime(s, errors="coerce", dayfirst=True)
        if pd.notna(dt):
            return dt
    return event_id_dt


def parse_round_code(display_name: str) -> str:
    m = re.search(r"ROUND\\s*(\\d+)", str(display_name or ""), flags=re.IGNORECASE)
    if m:
        return f"R{m.group(1)}"
    return ""


def parse_series_code(display_name: str, event_type: str) -> str:
    n = str(display_name or "").upper()
    if "WORLD CHAMPIONSHIP" in n or "WCH" in n:
        return "WCH"
    if "EUROPEAN CHAMPIONSHIP" in n or "ECH" in n:
        return "ECH"
    if "EUROPE CUP" in n or "EC" in n:
        return "EC"
    if "WORLD CUP" in n or " WC " in f" {n} ":
        return "WC"
    if event_type in {"WC", "WM", "EC", "EM"}:
        return event_type
    return "OTR"


def parse_location_short(display_name: str, location: str) -> str:
    loc = wc_location_clean(location)
    if loc and loc.lower() != "unknown":
        return loc
    n = str(display_name or "")
    m = re.search(r"ROUND\\s*\\d+\\s*-\\s*([^,]+)", n, flags=re.IGNORECASE)
    if m:
        return clean_spaces(m.group(1))
    parts = [clean_spaces(p) for p in n.split("-") if clean_spaces(p)]
    if parts:
        return parts[-1].split(",")[0].strip()
    return "Unknown"


def build_event_short(display_name: str, location: str, event_type: str, max_len: int = 30) -> str:
    loc = parse_location_short(display_name, location)
    series = parse_series_code(display_name, event_type)
    rnd = parse_round_code(display_name)
    bits = [b for b in [loc, series, rnd] if b]
    s = " ".join(bits).strip() or "Unknown"
    if len(s) > max_len:
        return s[: max_len - 1].rstrip() + "..."
    return s


def norm_location(s: str) -> str:
    s = clean_spaces(s)
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = " ".join(s.upper().split())
    return s


def wc_location_clean(location: str) -> str:
    loc = clean_spaces(location)
    loc = re.sub(r"^ROUND\\s*\\d+\\s*-\\s*", "", loc, flags=re.IGNORECASE)
    return loc


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


def round_short_label(round_title: str) -> str:
    t = str(round_title or "").lower()
    if "lcq" in t or "last chance" in t:
        return "LCQ"
    if "1/8" in t:
        return "1/8"
    if "1/4" in t:
        return "1/4"
    if "1/2" in t:
        return "1/2"
    if "final" in t:
        return "F"
    if "round 1" in t or "moto" in t or "seeding" in t:
        return "R1"
    return "R1"


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
    for c in ["start", "t1", "t2", "t3", "time"]:
        df.loc[df[c] <= 0, c] = np.nan

    df["finish"] = df["time"]
    df["event_type"] = df["event_id"].apply(infer_event_type)
    df["event_dt"] = [parse_event_date(ed, eid) for ed, eid in zip(df["event_date"], df["event_id"])]
    df["event_id_dt"] = pd.to_datetime(df["event_id"].astype(str).str[:8], format="%Y%m%d", errors="coerce")
    df["year"] = pd.to_numeric(df["event_id"].astype(str).str[:4], errors="coerce").astype("Int64")
    df["location"] = df["location"].fillna("Unknown").astype(str).apply(clean_spaces).replace("", "Unknown")
    df["event_short"] = [
        build_event_short(dn, loc, et)
        for dn, loc, et in zip(df["display_name"], df["location"], df["event_type"])
    ]
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
    df["round_short"] = df["round_title"].apply(round_short_label)
    df["phase"] = [classify_phase(rt, rs) for rt, rs in zip(df["round_title"], df["round_sort"])]
    return df


@st.cache_data(show_spinner=False)
def load_master_results(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    mr = pd.read_sql_query("SELECT * FROM master_results", conn)
    conn.close()
    if mr.empty:
        return mr
    mr["rank"] = pd.to_numeric(mr["rank"], errors="coerce")
    mr["year"] = pd.to_numeric(mr["year"], errors="coerce").astype("Int64")
    mr["uci_norm"] = mr["uci_id"].apply(norm_uci_id)
    mr["name_key"] = (mr["first_name"].fillna("").astype(str) + " " + mr["last_name"].fillna("").astype(str)).apply(norm_name_key)
    mr["gender"] = mr["gender"].fillna("").astype(str).str.upper().str.strip()
    mr["category"] = mr["category"].fillna("").astype(str).str.strip()
    mr["location_norm"] = mr["location"].fillna("").astype(str).apply(norm_location)
    mr["uci_event_id"] = mr["uci_event_id"].fillna("").astype(str).str.strip()
    mr["master_dt"] = pd.to_datetime(mr["date"], errors="coerce")
    return mr


def add_heat_relative_metrics(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    heat_cols = ["event_id", "group_id", "heat_id", "round_sort"]
    base_segments = ["start", "t1", "t2", "t3", "finish"]

    for seg in base_segments:
        med_col = f"{seg}_median"
        rank_col = f"pos_{seg}" if seg != "finish" else "pos_finish_est"
        pct_col = f"{seg}_pct"

        grp = out.groupby(heat_cols)[seg]
        out[med_col] = grp.transform("median")
        out[f"{seg}_winner"] = grp.transform("min")
        out[rank_col] = grp.rank(method="min", ascending=True)
        field = grp.transform("count")
        out[pct_col] = np.where(field > 1, (out[rank_col] - 1) / (field - 1), np.nan)

    out["rank_bottom"] = out["pos_start"]
    out["rank_t1"] = out["pos_t1"]
    out["rank_t2"] = out["pos_t2"]
    out["rank_t3"] = out["pos_t3"]

    # Prefer official rank for finish position if available.
    out["pos_finish"] = out["rank"].where(out["rank"].notna(), out["pos_finish_est"])
    out["rank_finish"] = out["pos_finish"]

    # Rank-4 reference by segment time (4th fastest valid time in the heat).
    # DNFs are ignored by dropping non-numeric/NaN times.
    for seg in base_segments:
        rank4 = (
            out.groupby(heat_cols, dropna=False)
            .apply(lambda g: g[seg].dropna().nsmallest(4).iloc[-1] if g[seg].notna().sum() >= 4 else np.nan)
            .reset_index(name=f"{seg}_rank4_ref")
        )
        out = out.merge(rank4, on=heat_cols, how="left")

        out[f"{seg}_delta_heat_median"] = out[seg] - out[f"{seg}_median"]
        out[f"{seg}_delta_rank4"] = out[seg] - out[f"{seg}_rank4_ref"]
        out[f"{seg}_delta_winner"] = out[seg] - out[f"{seg}_winner"]

    # Backward-compatible default reference.
    out["start_delta"] = out["start_delta_heat_median"]
    out["t1_delta"] = out["t1_delta_heat_median"]
    out["t2_delta"] = out["t2_delta_heat_median"]
    out["t3_delta"] = out["t3_delta_heat_median"]
    out["finish_delta"] = out["finish_delta_heat_median"]

    # Additional requested metrics.
    out["delta_vs_winner"] = out["finish_delta_winner"]
    out["delta_post_start"] = out["finish_delta"] - out["start_delta"]
    out["delta_post_t1"] = out["finish_delta"] - out["t1_delta"]
    out["delta_post_t2"] = out["finish_delta"] - out["t2_delta"]
    out["delta_post_t3"] = out["finish_delta"] - out["t3_delta"]

    # Split ranks per heat (smaller split duration = better rank).
    out["split_bottom_t1"] = out["t1"] - out["start"]
    out["split_t1_t2"] = out["t2"] - out["t1"]
    out["split_t2_t3"] = out["t3"] - out["t2"]
    out["split_t3_finish"] = out["finish"] - out["t3"]
    split_segments = ["split_bottom_t1", "split_t1_t2", "split_t2_t3", "split_t3_finish"]
    for seg in split_segments:
        grp = out.groupby(heat_cols)[seg]
        out[f"{seg}_median"] = grp.transform("median")
        out[f"{seg}_winner"] = grp.transform("min")
        rank4 = (
            out.groupby(heat_cols, dropna=False)
            .apply(lambda g: g[seg].dropna().nsmallest(4).iloc[-1] if g[seg].notna().sum() >= 4 else np.nan)
            .reset_index(name=f"{seg}_rank4_ref")
        )
        out = out.merge(rank4, on=heat_cols, how="left")
        out[f"{seg}_delta_heat_median"] = out[seg] - out[f"{seg}_median"]
        out[f"{seg}_delta_rank4"] = out[seg] - out[f"{seg}_rank4_ref"]
        out[f"{seg}_delta_winner"] = out[seg] - out[f"{seg}_winner"]

    for split_col, rank_col in [
        ("split_bottom_t1", "rank_bottom_t1"),
        ("split_t1_t2", "rank_t1_t2"),
        ("split_t2_t3", "rank_t2_t3"),
        ("split_t3_finish", "rank_t3_finish"),
    ]:
        out[rank_col] = out.groupby(heat_cols)[split_col].rank(method="min", ascending=True)
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


def apply_reference(
    df: pd.DataFrame,
    ref_key: str,
    event_top_n: int = 4,
    event_ko_final_only: bool = True,
    reference_source: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    out = df.copy()
    if event_top_n < 1:
        event_top_n = 4
    all_segments = ["start", "t1", "t2", "t3", "finish", "split_bottom_t1", "split_t1_t2", "split_t2_t3", "split_t3_finish"]

    # Event-level references (per event + category/group) from absolute segment times.
    if ref_key in {"event_top4", "event_best"}:
        src = reference_source.copy() if reference_source is not None else out.copy()
        if event_ko_final_only and "phase" in src.columns:
            src = src[src["phase"].isin(["KO", "Final"])].copy()
        group_cols = ["event_id", "group_id"]
        for seg in all_segments:
            if seg not in src.columns:
                continue
            ref_df = (
                src.groupby(group_cols, dropna=False)
                .apply(
                    lambda g: pd.Series(
                        {
                            f"{seg}_event_topn_ref": g[seg].dropna().nsmallest(max(1, event_top_n)).median()
                            if g[seg].notna().sum() > 0
                            else np.nan,
                            f"{seg}_event_best_ref": g[seg].dropna().min() if g[seg].notna().sum() > 0 else np.nan,
                        }
                    )
                )
                .reset_index()
            )
            out = out.merge(ref_df, on=group_cols, how="left")
            out[f"{seg}_delta_event_top4"] = out[seg] - out.get(f"{seg}_event_topn_ref")
            out[f"{seg}_delta_event_best"] = out[seg] - out.get(f"{seg}_event_best_ref")

    map_suffix = {
        "rank4": "rank4",
        "winner": "winner",
        "event_top4": "event_top4",
        "event_best": "event_best",
    }
    suffix = map_suffix.get(ref_key, "rank4")
    for seg in all_segments:
        src = f"{seg}_delta_{suffix}"
        if src in out.columns:
            out[f"{seg}_delta"] = out[src]

    out["delta_vs_winner"] = out["finish_delta_winner"]
    out["delta_post_start"] = out["finish_delta"] - out["start_delta"]
    out["delta_post_t1"] = out["finish_delta"] - out["t1_delta"]
    out["delta_post_t2"] = out["finish_delta"] - out["t2_delta"]
    out["delta_post_t3"] = out["finish_delta"] - out["t3_delta"]
    if "split_bottom_t1_delta" in out.columns:
        out["delta_bottom_t1"] = out["split_bottom_t1_delta"]
    if "split_t1_t2_delta" in out.columns:
        out["delta_t1_t2"] = out["split_t1_t2_delta"]
    if "split_t2_t3_delta" in out.columns:
        out["delta_t2_t3"] = out["split_t2_t3_delta"]
    if "split_t3_finish_delta" in out.columns:
        out["delta_t3_finish"] = out["split_t3_finish_delta"]
    return out


def add_robust_outlier_flags_and_winsor(
    df: pd.DataFrame,
    reference_df: pd.DataFrame,
    group_cols: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Winsorize upper-tail extremes per (category, gender, segment) using Q3 + 2*IQR.
    Keeps all rows; adds *_w and *_is_extreme columns for delta metrics.
    """
    out = df.copy()
    if out.empty:
        return out
    if group_cols is None:
        group_cols = ["category", "gender"]

    delta_cols = [
        "finish_delta",
        "start_delta",
        "t1_delta",
        "t2_delta",
        "t3_delta",
        "delta_bottom_t1",
        "delta_t1_t2",
        "delta_t2_t3",
        "delta_t3_finish",
        "delta_vs_winner",
        "delta_post_start",
        "delta_post_t1",
        "delta_post_t2",
        "delta_post_t3",
    ]
    present_cols = [c for c in delta_cols if c in out.columns and c in reference_df.columns]
    if not present_cols:
        return out

    ref = reference_df.copy()
    for col in present_cols:
        ref[col] = pd.to_numeric(ref[col], errors="coerce")
        qstats = (
            ref.groupby(group_cols, dropna=False)[col]
            .agg(
                n_valid=lambda s: s.notna().sum(),
                q1=lambda s: s.quantile(0.25),
                q3=lambda s: s.quantile(0.75),
                median=lambda s: s.median(),
                mad=lambda s: (s - s.median()).abs().median(),
            )
            .reset_index()
        )
        qstats["iqr"] = qstats["q3"] - qstats["q1"]
        qstats[f"{col}_threshold_iqr"] = qstats["q3"] + 2.0 * qstats["iqr"]
        qstats[f"{col}_threshold_mad"] = qstats["median"] + 6.0 * qstats["mad"]
        qstats[f"{col}_upper"] = np.where(
            qstats["n_valid"] < 20,
            qstats[f"{col}_threshold_mad"],
            qstats[f"{col}_threshold_iqr"],
        )
        # If fallback cannot be computed (e.g. MAD NaN), fall back to IQR threshold.
        qstats[f"{col}_upper"] = np.where(
            pd.to_numeric(qstats[f"{col}_upper"], errors="coerce").notna(),
            qstats[f"{col}_upper"],
            qstats[f"{col}_threshold_iqr"],
        )
        qstats = qstats.rename(
            columns={
                "q1": f"{col}_q1",
                "q3": f"{col}_q3",
                "iqr": f"{col}_iqr",
                "median": f"{col}_median",
                "mad": f"{col}_mad",
                "n_valid": f"{col}_n_valid",
            }
        )
        qstats = qstats[
            group_cols
            + [
                f"{col}_q1",
                f"{col}_q3",
                f"{col}_iqr",
                f"{col}_median",
                f"{col}_mad",
                f"{col}_n_valid",
                f"{col}_threshold_iqr",
                f"{col}_threshold_mad",
                f"{col}_upper",
            ]
        ]
        out = out.merge(qstats, on=group_cols, how="left")

        raw = pd.to_numeric(out[col], errors="coerce")
        upper = pd.to_numeric(out[f"{col}_upper"], errors="coerce")
        is_extreme = raw.notna() & upper.notna() & (raw > upper)

        out[f"{col}_is_extreme"] = is_extreme
        out[f"{col}_w"] = np.where(is_extreme, upper, raw)
    return out


def attach_final_rank_event(df: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["final_rank_event"] = np.nan
    if out.empty or master.empty:
        return out

    out["location_norm"] = out["location"].apply(lambda x: norm_location(wc_location_clean(x)))
    out["event_id_dt"] = pd.to_datetime(out["event_id"].astype(str).str[:8], format="%Y%m%d", errors="coerce")
    out["event_day"] = out["event_dt"].where(out["event_dt"].notna(), out["event_id_dt"]).dt.normalize()

    mr = master.copy()
    mr["master_day"] = pd.to_datetime(mr["master_dt"], errors="coerce").dt.normalize()

    def make_map(df_src: pd.DataFrame, key_cols: list[str], id_col: str):
        d = df_src[df_src[id_col] != ""].copy()
        if d.empty:
            return {}
        return d.groupby(key_cols + [id_col])["rank"].min().to_dict()

    # EC/EM: exact event_id mapping
    mr_ec = mr[mr["klasse"].isin(["EC", "EM"])].copy()
    ec_uci_map = make_map(mr_ec, ["uci_event_id"], "uci_norm")
    ec_name_map = make_map(mr_ec, ["uci_event_id"], "name_key")

    # WC: exact day + location + category + gender mapping
    mr_wc = mr[mr["klasse"] == "CDM"].copy()
    wc_uci_map = make_map(mr_wc, ["master_day", "location_norm", "category", "gender"], "uci_norm")
    wc_name_map = make_map(mr_wc, ["master_day", "location_norm", "category", "gender"], "name_key")
    # controlled fallback for minor location mismatches: day + category + gender
    wc_uci_fallback_map = make_map(mr_wc, ["master_day", "category", "gender"], "uci_norm")
    wc_name_fallback_map = make_map(mr_wc, ["master_day", "category", "gender"], "name_key")

    # WM: year + category + gender mapping
    mr_wm = mr[mr["klasse"] == "CM"].copy()
    wm_uci_map = make_map(mr_wm, ["year", "category", "gender"], "uci_norm")
    wm_name_map = make_map(mr_wm, ["year", "category", "gender"], "name_key")

    def get_rank_for_row(r: dict):
        event_id = str(r["event_id"])
        event_type = str(r["event_type"])
        year = r["year"]
        loc_norm = str(r["location_norm"] or "")
        cat = str(r["category"] or "")
        gen = "M" if str(r["gender"]) == "Men" else "W" if str(r["gender"]) == "Women" else ""
        day = pd.to_datetime(r.get("event_day"), errors="coerce")
        uci = str(r.get("uci_norm") or "")
        nk = str(r.get("name_key") or "")

        if event_type in {"EC", "EM"}:
            if uci:
                val = ec_uci_map.get((event_id, uci), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            if nk:
                val = ec_name_map.get((event_id, nk), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            return np.nan

        if event_type == "WC":
            if pd.isna(day) or not cat or not gen:
                return np.nan
            if uci and loc_norm:
                val = wc_uci_map.get((day, loc_norm, cat, gen, uci), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            if nk and loc_norm:
                val = wc_name_map.get((day, loc_norm, cat, gen, nk), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            if uci:
                val = wc_uci_fallback_map.get((day, cat, gen, uci), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            if nk:
                val = wc_name_fallback_map.get((day, cat, gen, nk), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            return np.nan

        if event_type == "WM":
            if pd.isna(year) or not cat or not gen:
                return np.nan
            y = int(year)
            if uci:
                val = wm_uci_map.get((y, cat, gen, uci), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            if nk:
                val = wm_name_map.get((y, cat, gen, nk), np.nan)
                if pd.notna(val) and float(val) > 0:
                    return val
            return np.nan

        return np.nan

    ranks = [
        get_rank_for_row(r)
        for r in out[
            ["event_id", "event_type", "year", "event_day", "location_norm", "category", "gender", "uci_norm", "name_key"]
        ].to_dict("records")
    ]
    out["final_rank_event"] = pd.to_numeric(pd.Series(ranks), errors="coerce")
    out["final_rank_event_display"] = np.where(
        out["final_rank_event"].notna(), out["final_rank_event"].astype("Int64").astype(str), "NA"
    )
    return out


st.title("Athlete Insights")
st.caption("Trend, Segmente, Positionen, Druck, Track-Profile, Benchmark, Fatigue und Result-Trend.")

all_runs = load_runs()
if all_runs.empty:
    st.warning("Keine Daten gefunden.")
    st.stop()
master_results = load_master_results()

event_type_opts = sorted([x for x in all_runs["event_type"].dropna().unique().tolist() if x])
year_opts = sorted([int(x) for x in all_runs["year"].dropna().unique().tolist()], reverse=True)
cat_opts = [x for x in ["Elite", "U23", "Junior"] if x in set(all_runs["category"].dropna().unique().tolist())]
gender_opts = [x for x in ["Men", "Women"] if x in set(all_runs["gender"].dropna().unique().tolist())]
nation_opts = sorted([x for x in all_runs["nation"].dropna().unique().tolist() if x])
default_years = [y for y in [2025, 2024, 2023] if y in year_opts]
if not default_years:
    default_years = year_opts
default_nations = ["SUI"] if "SUI" in nation_opts else []

f1, f2, f3, f4, f5 = st.columns(5)
with f1:
    sel_years = st.multiselect("Jahr", year_opts, default=default_years)
with f2:
    sel_event_types = st.multiselect("Event Type", event_type_opts, default=event_type_opts)
with f3:
    sel_categories = st.multiselect("Kategorie", cat_opts, default=cat_opts)
with f4:
    sel_gender = st.multiselect("Geschlecht", gender_opts, default=gender_opts)
with f5:
    sel_nations = st.multiselect("Nation (Rider)", nation_opts, default=default_nations)

loc_scope = all_runs.copy()
if sel_years:
    loc_scope = loc_scope[loc_scope["year"].isin(sel_years)]
if sel_event_types:
    loc_scope = loc_scope[loc_scope["event_type"].isin(sel_event_types)]
if sel_categories:
    loc_scope = loc_scope[loc_scope["category"].isin(sel_categories)]
if sel_gender:
    loc_scope = loc_scope[loc_scope["gender"].isin(sel_gender)]
if sel_nations:
    loc_scope = loc_scope[loc_scope["nation"].isin(sel_nations)]
loc_opts = sorted([x for x in loc_scope["location"].dropna().unique().tolist() if x])

g1, g2 = st.columns(2)
with g1:
    sel_locations = st.multiselect("Location (optional)", loc_opts, default=[])
with g2:
    round_opts = [x for x in ["R1", "LCQ", "1/8", "1/4", "1/2", "F"] if x in set(loc_scope["round_short"].dropna().unique().tolist())]
    sel_rounds = st.multiselect("Runde (optional)", round_opts, default=round_opts)

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
if sel_rounds:
    base_scope = base_scope[base_scope["round_short"].isin(sel_rounds)]

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

ref_label = st.radio(
    "Referenz",
    ["Event Top4 (robust)", "Heat Rank 4 (Qualification Cut)", "Heat Rank 1 (Winner)"],
    horizontal=True,
    index=0,
)
event_top_n = 4
event_ko_final_only = True
use_event_best = False
if ref_label == "Event Top4 (robust)":
    c1, c2, c3 = st.columns(3)
    with c1:
        event_top_n = st.selectbox("Event Top N", [4, 8], index=0, key="event_top_n")
    with c2:
        event_ko_final_only = st.toggle("Event-Referenz nur KO+Final", value=True, key="event_ref_ko_final")
    with c3:
        use_event_best = st.toggle("Event Best (Ceiling) statt Event Top N", value=False, key="event_ref_best")

ref_key = "rank4"
if ref_label == "Event Top4 (robust)":
    ref_key = "event_best" if use_event_best else "event_top4"
elif ref_label == "Heat Rank 4 (Qualification Cut)":
    ref_key = "rank4"
elif ref_label == "Heat Rank 1 (Winner)":
    ref_key = "winner"

ref_caption = ref_label
if ref_label == "Event Top4 (robust)":
    base_ref = "Event Best" if use_event_best else f"Event Top{event_top_n}"
    scope_ref = "KO+Final" if event_ko_final_only else "alle Runden"
    ref_caption = f"{base_ref} ({scope_ref})"
st.caption(f"Aktive Delta-Referenz: {ref_caption}")

base_rel = add_heat_relative_metrics(base_scope)
base_rel_ref = apply_reference(
    base_rel,
    ref_key,
    event_top_n=event_top_n,
    event_ko_final_only=event_ko_final_only,
    reference_source=base_rel,
)
runs_sel = base_rel_ref[base_rel_ref["rider_id"].isin(selected_ids)].copy()
runs_sel = runs_sel.sort_values(["event_dt", "event_id", "round_sort", "heat_id"])
runs_sel = add_robust_outlier_flags_and_winsor(
    runs_sel,
    reference_df=base_rel_ref,
    group_cols=["category", "gender"],
)
runs_sel = attach_final_rank_event(runs_sel, master_results)
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
    plot = runs_sel.copy()
    plot["round_short"] = plot["round_title"].apply(round_short_label)
    round_order_map = {"R1": 1, "LCQ": 2, "1/8": 3, "1/4": 4, "1/2": 5, "F": 6}
    plot["round_order"] = plot["round_short"].map(round_order_map).fillna(99)
    plot["heat_sort"] = pd.to_numeric(plot["heat_id"], errors="coerce").fillna(99999)
    plot["event_label_full"] = plot["display_name"].fillna(plot["event_label"])
    plot["x_key"] = plot["event_label"] + " • " + plot["round_short"]
    plot["x_base_short"] = plot["event_short"].fillna("Unknown") + " • " + plot["round_short"]
    dup_short = plot.groupby("x_base_short")["event_id"].transform("nunique") > 1
    plot["x_label_short"] = np.where(
        dup_short,
        plot["x_base_short"] + " (" + plot["event_id"].astype(str).str[:8] + ")",
        plot["x_base_short"],
    )
    plot["x_base_short_best"] = plot["event_short"].fillna("Unknown")
    dup_short_best = plot.groupby("x_base_short_best")["event_id"].transform("nunique") > 1
    plot["x_label_short_best"] = np.where(
        dup_short_best,
        plot["x_base_short_best"] + " (" + plot["event_id"].astype(str).str[:8] + ")",
        plot["x_base_short_best"],
    )
    plot["x_label_long"] = plot["event_label_full"] + " • " + plot["round_short"]
    plot["rank_num"] = pd.to_numeric(plot["rank"], errors="coerce")
    plot["rank_display"] = np.where(plot["rank_num"].fillna(0) > 0, plot["rank_num"].astype("Int64").astype(str), "unknown")
    plot["reference_type"] = ref_caption
    plot["event_date_display"] = plot["event_dt"].dt.strftime("%Y-%m-%d")
    for rc in [
        "rank_bottom",
        "rank_t1",
        "rank_t2",
        "rank_t3",
        "rank_finish",
        "rank_bottom_t1",
        "rank_t1_t2",
        "rank_t2_t3",
        "rank_t3_finish",
    ]:
        if rc in plot.columns:
            plot[f"{rc}_display"] = np.where(
                pd.to_numeric(plot[rc], errors="coerce").notna(),
                pd.to_numeric(plot[rc], errors="coerce").astype("Int64").astype(str),
                "NA",
            )

    # Dynamic split deltas (consistent sign: positive = slower than reference).
    if "split_bottom_t1_delta" in plot.columns and plot["split_bottom_t1_delta"].notna().any():
        plot["delta_bottom_t1"] = plot["split_bottom_t1_delta"]
    else:
        plot["delta_bottom_t1"] = plot["t1_delta"] - plot["start_delta"]
    if "split_t1_t2_delta" in plot.columns and plot["split_t1_t2_delta"].notna().any():
        plot["delta_t1_t2"] = plot["split_t1_t2_delta"]
    else:
        plot["delta_t1_t2"] = plot["t2_delta"] - plot["t1_delta"]
    if "split_t2_t3_delta" in plot.columns and plot["split_t2_t3_delta"].notna().any():
        plot["delta_t2_t3"] = plot["split_t2_t3_delta"]
    else:
        plot["delta_t2_t3"] = plot["t3_delta"] - plot["t2_delta"]
    if "split_t3_finish_delta" in plot.columns and plot["split_t3_finish_delta"].notna().any():
        plot["delta_t3_finish"] = plot["split_t3_finish_delta"]
    else:
        plot["delta_t3_finish"] = plot["finish_delta"] - plot["t3_delta"]

    metric_defs = [
        ("Finish Delta", "finish_delta"),
        ("Bottom Delta", "start_delta"),
        ("T1 Delta", "t1_delta"),
        ("T2 Delta", "t2_delta"),
        ("T3 Delta", "t3_delta"),
        ("Bottom->T1 Delta", "delta_bottom_t1"),
        ("T1->T2 Delta", "delta_t1_t2"),
        ("T2->T3 Delta", "delta_t2_t3"),
        ("T3->Finish Delta", "delta_t3_finish"),
    ]
    available_metrics = [m for m in metric_defs if m[1] in plot.columns and plot[m[1]].notna().any()]
    available_labels = [m[0] for m in available_metrics]

    default_metrics = ["Finish Delta"] if "Finish Delta" in available_labels else (available_labels[:1] if available_labels else [])
    selected_metric_labels = st.multiselect(
        "Delta-Metriken auswaehlen",
        options=available_labels,
        default=default_metrics,
        key="trend_metrics",
    )
    best_of_day_mode = st.toggle(
        "Tagesbestwert pro Event (je Rider) anzeigen",
        value=False,
        key="trend_best_of_day",
        help="Pro Rider und Event wird nur der beste Delta-Wert des Tages angezeigt.",
    )
    metric_map = dict(available_metrics)
    metric_plot_map = {label: (f"{col}_w" if f"{col}_w" in plot.columns else col) for label, col in available_metrics}
    metric_extreme_map = {label: (f"{col}_is_extreme" if f"{col}_is_extreme" in plot.columns else None) for label, col in available_metrics}
    metric_q1_map = {label: (f"{col}_q1" if f"{col}_q1" in plot.columns else None) for label, col in available_metrics}
    metric_q3_map = {label: (f"{col}_q3" if f"{col}_q3" in plot.columns else None) for label, col in available_metrics}
    metric_iqr_map = {label: (f"{col}_iqr" if f"{col}_iqr" in plot.columns else None) for label, col in available_metrics}
    metric_cap_map = {label: (f"{col}_upper" if f"{col}_upper" in plot.columns else None) for label, col in available_metrics}
    metric_thr_map = {
        label: (f"{col}_threshold_iqr" if f"{col}_threshold_iqr" in plot.columns else None)
        for label, col in available_metrics
    }
    x_axis_col = "x_label_short_best" if best_of_day_mode else "x_label_short"

    plot_frames = []
    for metric_label in selected_metric_labels:
        metric_col_raw = metric_map.get(metric_label)
        metric_col = metric_plot_map.get(metric_label)
        metric_extreme_col = metric_extreme_map.get(metric_label)
        metric_q1_col = metric_q1_map.get(metric_label)
        metric_q3_col = metric_q3_map.get(metric_label)
        metric_iqr_col = metric_iqr_map.get(metric_label)
        metric_cap_col = metric_cap_map.get(metric_label)
        metric_thr_col = metric_thr_map.get(metric_label)
        if not metric_col_raw or not metric_col:
            continue
        frame_cols = [
                "year",
                "event_dt",
                "event_id_dt",
                "event_label",
                "event_label_full",
                "location",
                "event_date_display",
                "event_id",
                "rider_id",
                "round_title",
                "round_short",
                "round_order",
                "heat_id",
                "heat_sort",
                "heat_title",
                "rank_display",
                "rider_short",
                "reference_type",
                "x_key",
                "x_label_short",
                "x_label_short_best",
                "x_label_long",
                "rank_bottom_display",
                "rank_t1_display",
                "rank_t2_display",
                "rank_t3_display",
                "rank_finish_display",
                "rank_bottom_t1_display",
                "rank_t1_t2_display",
                "rank_t2_t3_display",
                "rank_t3_finish_display",
                "final_rank_event_display",
                metric_col_raw,
            ]
        if metric_col != metric_col_raw:
            frame_cols.append(metric_col)
        for extra_col in [metric_q1_col, metric_q3_col, metric_iqr_col, metric_cap_col, metric_thr_col]:
            if extra_col and extra_col in plot.columns:
                frame_cols.append(extra_col)
        frame = plot[frame_cols].copy()
        if metric_col == metric_col_raw:
            frame = frame.rename(columns={metric_col_raw: "delta"})
            frame["delta_raw"] = frame["delta"]
        else:
            frame = frame.rename(columns={metric_col_raw: "delta_raw", metric_col: "delta"})
        if metric_extreme_col and metric_extreme_col in plot.columns:
            frame["is_extreme"] = plot[metric_extreme_col].astype(bool)
        else:
            frame["is_extreme"] = False
        frame["q1"] = pd.to_numeric(plot[metric_q1_col], errors="coerce") if metric_q1_col and metric_q1_col in plot.columns else np.nan
        frame["q3"] = pd.to_numeric(plot[metric_q3_col], errors="coerce") if metric_q3_col and metric_q3_col in plot.columns else np.nan
        frame["iqr"] = pd.to_numeric(plot[metric_iqr_col], errors="coerce") if metric_iqr_col and metric_iqr_col in plot.columns else np.nan
        frame["upper_cap"] = pd.to_numeric(plot[metric_cap_col], errors="coerce") if metric_cap_col and metric_cap_col in plot.columns else np.nan
        frame["extreme_threshold"] = pd.to_numeric(plot[metric_thr_col], errors="coerce") if metric_thr_col and metric_thr_col in plot.columns else np.nan
        frame["group_key"] = (
            plot["category"].fillna("Unknown").astype(str)
            + " "
            + plot["gender"].fillna("Unknown").astype(str)
            + " × "
            + metric_label
        )
        frame["metric"] = metric_label
        if best_of_day_mode:
            # Keep one row per rider/event/metric: the best (smallest) delta.
            frame = frame.sort_values(["delta", "round_order", "heat_sort"], na_position="last")
            frame = frame.dropna(subset=["delta"])
            if not frame.empty:
                idx = frame.groupby(["rider_id", "event_id", "metric"], dropna=False)["delta"].idxmin()
                frame = frame.loc[idx].copy()
                frame["round_short"] = "BEST"
                frame["round_title"] = "Best of day"
                frame["heat_title"] = "Best run in event"
        plot_frames.append(frame)

    plot_long = pd.concat(plot_frames, ignore_index=True) if plot_frames else pd.DataFrame()
    if not plot_long.empty:
        plot_long = plot_long.dropna(subset=["delta"])
        plot_long["series_label"] = plot_long["rider_short"] + " - " + plot_long["metric"]
        x_order_df = (
            plot_long[[x_axis_col, "event_id_dt", "event_dt", "round_order", "heat_sort"]]
            .groupby(x_axis_col, as_index=False)
            .agg(
                sort_event_id_dt=("event_id_dt", "min"),
                sort_event_dt=("event_dt", "min"),
                sort_round=("round_order", "min"),
                sort_heat=("heat_sort", "min"),
            )
            .sort_values(["sort_event_id_dt", "sort_event_dt", "sort_round", "sort_heat", x_axis_col], na_position="last")
        )
        x_order = x_order_df[x_axis_col].tolist()
        x_rank_map = {k: i for i, k in enumerate(x_order)}
        plot_long["x_order"] = plot_long[x_axis_col].map(x_rank_map)

        # Build a complete x-grid per rider/metric series to force visual gaps
        # when a competition/round is missing for that specific series.
        series_keys = plot_long[["series_label", "rider_short", "metric", "reference_type"]].drop_duplicates()
        x_keys = pd.DataFrame({x_axis_col: x_order, "x_order": list(range(len(x_order)))})
        full_grid = series_keys.assign(_k=1).merge(x_keys.assign(_k=1), on="_k").drop(columns="_k")

        meta_cols = [
            "series_label",
            "rider_short",
            "metric",
            "reference_type",
            x_axis_col,
            "x_order",
            "delta",
            "delta_raw",
            "is_extreme",
            "q1",
            "q3",
            "iqr",
            "upper_cap",
            "extreme_threshold",
            "group_key",
            "event_label_full",
            "event_date_display",
            "location",
            "round_title",
            "round_short",
            "heat_title",
            "rank_display",
            "rank_bottom_display",
            "rank_t1_display",
            "rank_t2_display",
            "rank_t3_display",
            "rank_finish_display",
            "rank_bottom_t1_display",
            "rank_t1_t2_display",
            "rank_t2_t3_display",
            "rank_t3_finish_display",
            "final_rank_event_display",
        ]
        meta_cols = [c for c in meta_cols if c in plot_long.columns]
        plot_long = full_grid.merge(
            plot_long[meta_cols],
            on=[c for c in ["series_label", "rider_short", "metric", "reference_type", x_axis_col, "x_order"] if c in meta_cols],
            how="left",
        )
    else:
        x_order = []

    if not plot_long.empty:
        trend_chart = (
            alt.Chart(plot_long)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    f"{x_axis_col}:N",
                    title="Event" if best_of_day_mode else "Event • Runde",
                    sort=x_order,
                    axis=alt.Axis(labelAngle=-55, labelLimit=280, labelOverlap=False),
                ),
                y=alt.Y("delta:Q", title="Delta (s)"),
                color=alt.Color("series_label:N", title="Rider - Metrik"),
                detail="series_label:N",
                order=alt.Order("x_order:Q", sort="ascending"),
                tooltip=[
                    "series_label:N",
                    alt.Tooltip("event_label_full:N", title="event_label"),
                    alt.Tooltip("event_date_display:N", title="date"),
                    "location:N",
                    "round_title:N",
                    "round_short:N",
                    "heat_title:N",
                    alt.Tooltip("rank_display:N", title="rank"),
                    alt.Tooltip("delta_raw:Q", title="raw_delta", format=".4f"),
                    alt.Tooltip("delta:Q", title="winsorized_delta", format=".4f"),
                    alt.Tooltip("upper_cap:Q", title="upper_cap", format=".4f"),
                    alt.Tooltip("q1:Q", title="Q1", format=".4f"),
                    alt.Tooltip("q3:Q", title="Q3", format=".4f"),
                    alt.Tooltip("iqr:Q", title="IQR", format=".4f"),
                    alt.Tooltip("extreme_threshold:Q", title="extreme_threshold", format=".4f"),
                    alt.Tooltip("group_key:N", title="group_key"),
                    alt.Tooltip("is_extreme:N", title="is_extreme"),
                    "reference_type:N",
                    alt.Tooltip("rank_bottom_display:N", title="rank_bottom"),
                    alt.Tooltip("rank_t1_display:N", title="rank_t1"),
                    alt.Tooltip("rank_t2_display:N", title="rank_t2"),
                    alt.Tooltip("rank_t3_display:N", title="rank_t3"),
                    alt.Tooltip("rank_finish_display:N", title="rank_finish"),
                    alt.Tooltip("rank_bottom_t1_display:N", title="rank_bottom_t1"),
                    alt.Tooltip("rank_t1_t2_display:N", title="rank_t1_t2"),
                    alt.Tooltip("rank_t2_t3_display:N", title="rank_t2_t3"),
                    alt.Tooltip("rank_t3_finish_display:N", title="rank_t3_finish"),
                    alt.Tooltip("final_rank_event_display:N", title="final_rank_event"),
                ],
            )
            .properties(height=430, padding={"bottom": 130, "left": 5, "right": 5, "top": 10})
            .configure_axis(labelFontSize=11, titleFontSize=12)
        )
        st.altair_chart(trend_chart, use_container_width=True)
    else:
        st.info("Keine verwertbaren Daten fuer die gewaehlten Delta-Metriken in der aktuellen Rider-Auswahl.")

    # Summary: use Finish Delta by default; fallback to first selected metric.
    summary_label = "Finish Delta" if "Finish Delta" in selected_metric_labels else (selected_metric_labels[0] if selected_metric_labels else None)
    summary_col = metric_plot_map.get(summary_label) if summary_label else None
    summary_src = pd.DataFrame()
    if summary_col and summary_col in plot.columns:
        summary_src = plot[
            ["year", "rider_short", "event_id", summary_col]
        ].rename(columns={summary_col: "delta"}).dropna(subset=["delta"])

    summary = (
        summary_src.groupby(["year", "rider_short"], as_index=False)
        .agg(
            n_runs=("event_id", "count"),
            mean_metric=("delta", "mean"),
            median_metric=("delta", "median"),
            mad_metric=("delta", lambda s: (s - s.median()).abs().median()),
            best_metric=("delta", "min"),
        )
        .sort_values("year", ascending=False)
    ) if not summary_src.empty else pd.DataFrame(columns=["year", "rider_short", "n_runs", "mean_metric", "median_metric", "mad_metric", "best_metric"])

    summary_name = summary_label if summary_label else "metric"
    summary = summary.rename(
        columns={
            "mean_metric": f"mean_{summary_name}",
            "median_metric": f"median_{summary_name}",
            "mad_metric": f"mad_{summary_name}",
            "best_metric": f"best_{summary_name}",
        }
    )
    for c in [f"mean_{summary_name}", f"median_{summary_name}", f"mad_{summary_name}", f"best_{summary_name}"]:
        summary[c] = pd.to_numeric(summary[c], errors="coerce").round(4)
    st.dataframe(summary, use_container_width=True, hide_index=True)

    st.markdown("**Segment Contribution**")
    contrib_src = runs_sel.copy()
    seg_candidates = [
        ("BottomDelta", "start_delta"),
        ("PostStartDelta", "delta_post_start"),
        ("PostT1Delta", "delta_post_t1"),
        ("PostT2Delta", "delta_post_t2"),
        ("PostT3Delta", "delta_post_t3"),
        ("Split Bottom->T1 Delta", "delta_bottom_t1"),
        ("Split T1->T2 Delta", "delta_t1_t2"),
        ("Split T2->T3 Delta", "delta_t2_t3"),
        ("Split T3->Finish Delta", "delta_t3_finish"),
    ]
    available_seg_labels = [label for label, col in seg_candidates if col in contrib_src.columns and contrib_src[col].notna().any()]
    selected_seg_labels = st.multiselect(
        "Segmente anzeigen",
        options=available_seg_labels,
        default=available_seg_labels,
        key="trend_contrib_segments",
    )
    seg_frames = []
    for label, col in seg_candidates:
        if label not in selected_seg_labels:
            continue
        value_col = f"{col}_w" if f"{col}_w" in contrib_src.columns else col
        if value_col not in contrib_src.columns:
            continue
        vals = pd.to_numeric(contrib_src[value_col], errors="coerce")
        if vals.notna().sum() == 0:
            continue
        seg_frames.append(contrib_src[["rider_short"]].assign(metric=label, value=vals))
    contrib_long = pd.concat(seg_frames, ignore_index=True).dropna(subset=["value"]) if seg_frames else pd.DataFrame()
    if not contrib_long.empty:
        contrib_agg = contrib_long.groupby(["rider_short", "metric"], as_index=False).agg(mean_value=("value", "mean"))
        # Share of total loss (informative): positive deltas only.
        contrib_agg["loss_pos"] = contrib_agg["mean_value"].clip(lower=0)
        denom = contrib_agg.groupby("rider_short")["loss_pos"].transform("sum")
        contrib_agg["loss_share_pct"] = np.where(denom > 0, (contrib_agg["loss_pos"] / denom) * 100.0, np.nan)

        bottom_df = contrib_agg[contrib_agg["metric"] == "BottomDelta"].copy()
        other_df = contrib_agg[contrib_agg["metric"] != "BottomDelta"].copy()

        if not bottom_df.empty:
            st.markdown("Bottom Delta")
            cbar_bottom = (
                alt.Chart(bottom_df)
                .mark_bar()
                .encode(
                    x=alt.X("metric:N", title="Metrik"),
                    y=alt.Y("mean_value:Q", title="Mean Delta (s)"),
                    color=alt.Color("rider_short:N", title="Rider"),
                    tooltip=["rider_short:N", "metric:N", "mean_value:Q", alt.Tooltip("loss_share_pct:Q", title="Loss Share %", format=".1f")],
                )
                .properties(height=180)
            )
            st.altair_chart(cbar_bottom, use_container_width=True)

        if not other_df.empty:
            st.markdown("Andere Segmente")
            cbar_other = (
                alt.Chart(other_df)
                .mark_bar()
                .encode(
                    x=alt.X("metric:N", title="Metrik"),
                    y=alt.Y("mean_value:Q", title="Mean Delta (s)"),
                    color=alt.Color("rider_short:N", title="Rider"),
                    tooltip=["rider_short:N", "metric:N", "mean_value:Q", alt.Tooltip("loss_share_pct:Q", title="Loss Share %", format=".1f")],
                )
                .properties(height=280)
            )
            st.altair_chart(cbar_other, use_container_width=True)

        table = contrib_agg[["rider_short", "metric", "mean_value", "loss_share_pct"]].copy()
        table = table.rename(columns={"rider_short": "Rider", "metric": "Segment", "mean_value": "Delta (s)", "loss_share_pct": "Loss Share %"})
        table["Delta (s)"] = pd.to_numeric(table["Delta (s)"], errors="coerce").round(4)
        table["Loss Share %"] = pd.to_numeric(table["Loss Share %"], errors="coerce").round(1)
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.caption("Vorzeichen: positiv = langsamer als Referenz, negativ = schneller als Referenz.")

    st.markdown("**Start Delta vs Finish Delta**")
    scat = runs_sel.dropna(subset=["start_delta", "finish_delta"]).copy()
    if not scat.empty:
        mn = float(min(scat["start_delta"].min(), scat["finish_delta"].min()))
        mx = float(max(scat["start_delta"].max(), scat["finish_delta"].max()))
        diag = pd.DataFrame({"x": [mn, mx], "y": [mn, mx]})
        scatter = (
            alt.Chart(scat)
            .mark_circle(opacity=0.75)
            .encode(
                x=alt.X("start_delta:Q", title="Start Delta (s)"),
                y=alt.Y("finish_delta:Q", title="Finish Delta (s)"),
                color=alt.Color("rider_short:N", title="Rider"),
                tooltip=["rider_short:N", "event_label:N", "round_title:N", "heat_title:N", "start_delta:Q", "finish_delta:Q"],
            )
            .properties(height=320)
        )
        diag_line = alt.Chart(diag).mark_line(strokeDash=[4, 4], color="gray").encode(x="x:Q", y="y:Q")
        st.altair_chart(scatter + diag_line, use_container_width=True)

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
    b = runs_sel.copy()
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
            tooltip=["rider_short:N", "event_label:N", "round_title:N", "heat_title:N", "rank:Q", "final_rank_event_display:N", f"{metric_col}:Q"],
        )
        st.altair_chart(line.properties(height=320), use_container_width=True)

        hist = alt.Chart(mm).mark_bar().encode(x=alt.X(f"{metric_col}:Q", bin=True), y="count()")
        st.altair_chart(hist.properties(height=220), use_container_width=True)

        tcols = ["event_id", "event_dt", "location", "round_title", "heat_title", "rank", "final_rank_event_display", metric_col]
        view = mm[tcols].copy()
        view = view.rename(columns={"final_rank_event_display": "final_rank_event"})
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
                "final_rank_event": first.get("final_rank_event", np.nan),
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
            tooltip=["event_label:N", "dropoff:Q", "n_runs:Q", "final_rank_event:Q"],
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
    rr = runs_sel.copy().sort_values(["rider_id", "event_dt", "event_id", "round_sort", "heat_id"])

    # One row per rider+event to map overall/final classification.
    rider_event = (
        rr.groupby(["rider_id", "event_id"], as_index=False)
        .agg(
            rider_short=("rider_short", "first"),
            rider_label=("rider_label", "first"),
            event_short=("event_short", "first"),
            category=("category", lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]),
            gender=("gender", lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]),
            event_dt=("event_dt", "first"),
            location=("location", "first"),
            year=("year", "first"),
        )
    )
    event_rank_map = (
        rr[["rider_id", "event_id", "final_rank_event"]]
        .drop_duplicates(subset=["rider_id", "event_id"])
        .copy()
    )
    rider_event = rider_event.merge(event_rank_map, on=["rider_id", "event_id"], how="left")
    rider_event["final_rank"] = pd.to_numeric(rider_event["final_rank_event"], errors="coerce")
    rider_event = rider_event.sort_values(["event_dt", "event_id", "rider_short"])

    if rider_event.empty:
        st.info("Keine Event-Ergebnisse fuer die aktuelle Rider-Auswahl.")
    else:
        plot_df = rider_event.dropna(subset=["final_rank"]).copy()
        if plot_df.empty:
            st.info("Keine Final Classification in master_results fuer die aktuelle Auswahl gefunden.")
        else:
            plot_df["event_id_dt"] = pd.to_datetime(
                plot_df["event_id"].astype(str).str[:8], format="%Y%m%d", errors="coerce"
            )
            plot_df["x_base_short"] = plot_df["event_short"].fillna("Unknown")
            dup_short = plot_df.groupby("x_base_short")["event_id"].transform("nunique") > 1
            plot_df["x_label_short"] = np.where(
                dup_short,
                plot_df["x_base_short"] + " (" + plot_df["event_id"].astype(str).str[:8] + ")",
                plot_df["x_base_short"],
            )
            x_order_df = (
                plot_df[["x_label_short", "event_id_dt", "event_dt", "event_id"]]
                .drop_duplicates(subset=["x_label_short", "event_id"])
                .sort_values(["event_id_dt", "event_dt", "event_id"], ascending=[True, True, True], na_position="last")
            )
            x_order = x_order_df["x_label_short"].drop_duplicates().tolist()
            x_rank_map = {k: i for i, k in enumerate(x_order)}
            plot_df["x_order"] = plot_df["x_label_short"].map(x_rank_map)
            plot_df["final_rank_plot"] = pd.to_numeric(plot_df["final_rank"], errors="coerce").clip(lower=1, upper=32)
            plot_df["final_rank_over32_label"] = np.where(
                pd.to_numeric(plot_df["final_rank"], errors="coerce") > 32,
                pd.to_numeric(plot_df["final_rank"], errors="coerce").astype("Int64").astype(str),
                "",
            )
            plot_df["event_label"] = (
                plot_df["event_dt"].dt.strftime("%Y-%m-%d").fillna(plot_df["event_id"])
                + " | "
                + plot_df["location"]
                + " | "
                + plot_df["rider_short"]
            )
            base = alt.Chart(plot_df).encode(
                x=alt.X(
                    "x_label_short:N",
                    title="Event",
                    sort=x_order,
                    axis=alt.Axis(labelAngle=-55, labelLimit=280, labelOverlap=False),
                ),
                y=alt.Y(
                    "final_rank_plot:Q",
                    title="Final Rank",
                    scale=alt.Scale(domain=[1, 32], domainMin=1, domainMax=32, reverse=True, nice=False),
                    axis=alt.Axis(values=[1, 2, 3, 8, 16, 24, 32]),
                ),
                color=alt.Color("rider_short:N", title="Rider"),
                detail="rider_short:N",
                order=alt.Order("x_order:Q", sort="ascending"),
                tooltip=["event_label:N", "final_rank:Q", "category:N", "gender:N", "event_id:N"],
            )
            line = base.mark_line()
            points = base.mark_point()
            over32_text = (
                base.transform_filter(alt.datum.final_rank > 32)
                .mark_text(dy=-8, fontSize=10)
                .encode(text="final_rank_over32_label:N")
            )
            st.altair_chart(
                (line + points + over32_text).properties(
                    height=460, padding={"bottom": 110, "left": 5, "right": 5, "top": 10}
                ),
                use_container_width=True,
            )

        st.markdown("**Final Rank pro Event (master_results)**")
        final_rank_tbl = rider_event.copy()
        final_rank_tbl["event_id_dt"] = pd.to_datetime(
            final_rank_tbl["event_id"].astype(str).str[:8], format="%Y%m%d", errors="coerce"
        )
        final_rank_tbl = final_rank_tbl.sort_values(
            ["event_id_dt", "event_dt", "event_id", "rider_short"],
            ascending=[True, True, True, True],
            na_position="last",
        )
        final_rank_tbl["Date"] = final_rank_tbl["event_dt"].dt.strftime("%Y-%m-%d")
        final_rank_tbl["Final Rank"] = pd.to_numeric(final_rank_tbl["final_rank"], errors="coerce")
        final_rank_tbl["Final Rank"] = np.where(
            final_rank_tbl["Final Rank"].notna(),
            final_rank_tbl["Final Rank"].astype("Int64").astype(str),
            "NA",
        )
        final_rank_tbl = final_rank_tbl.rename(
            columns={
                "event_id": "Event",
                "location": "Location",
                "rider_short": "Rider",
            }
        )
        st.dataframe(
            final_rank_tbl[["Date", "Event", "Location", "Rider", "Final Rank"]],
            use_container_width=True,
            hide_index=True,
        )

        # Requested summary metrics.
        summary = (
            rider_event.groupby("rider_short", as_index=False)
            .agg(
                n_events=("event_id", "nunique"),
                avg_final_rank=("final_rank", "mean"),
                finals_count=("final_rank", lambda s: int((pd.to_numeric(s, errors="coerce") <= 8).sum())),
                top4_count=("final_rank", lambda s: int((pd.to_numeric(s, errors="coerce") <= 4).sum())),
                dnq_count=("final_rank", lambda s: int(pd.to_numeric(s, errors="coerce").isna().sum())),
            )
            .sort_values("avg_final_rank", ascending=True, na_position="last")
        )
        st.dataframe(summary.round(3), use_container_width=True, hide_index=True)

st.caption(
    f"Alle Deltas verwenden die aktive Referenz: {ref_caption}."
)
