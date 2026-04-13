import sqlite3
import unicodedata
from typing import Optional
import re
import json
import textwrap
import os
from io import BytesIO
from datetime import datetime

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
try:
    import plotly.graph_objects as go
except ImportError:
    go = None
try:
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
except ImportError:
    plt = None
    PdfPages = None


DB_PATH = "bmx.db"

GROUP_MAP = {
    91: ("Elite", "Men"),
    92: ("Elite", "Women"),
    93: ("U23", "Men"),
    94: ("U23", "Women"),
    95: ("Junior", "Men"),
    96: ("Junior", "Women"),
}

ROUND_ORDER = {
    "round 1": 10,
    "moto 1": 10,
    "moto 2": 11,
    "moto 3": 12,
    "moto": 10,
    "seeding": 10,
    "lcq": 20,
    "last chance": 20,
    "1/32": 25,
    "1/16": 30,
    "1/8": 40,
    "1/4": 50,
    "quarter final": 50,
    "quarter": 50,
    "1/2": 60,
    "semi final": 60,
    "semi": 60,
    "main m1": 70,
    "main m2": 71,
    "main m3": 72,
    "final": 70,
}


def infer_event_type(event_id: str) -> str:
    e = str(event_id or "").lower()
    if "_usap_" in e or "_usabmx_" in e:
        return "USABMX"
    if "_ffc_" in e:
        return "FFC"
    if "_scc_" in e:
        return "SCC"
    if "_other_" in e or "_sqorz_" in e:
        return "Other"
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


def _title_token(token: str) -> str:
    # Preserve separators like "-" and apostrophes while title-casing segments.
    if not token:
        return token
    if "-" in token:
        return "-".join(_title_token(t) for t in token.split("-"))
    if "'" in token:
        return "'".join(_title_token(t) for t in token.split("'"))
    if not any(ch.isalpha() for ch in token):
        return token
    return token[:1].upper() + token[1:].lower()


def pretty_name(name: str) -> str:
    n = clean_spaces(name)
    if not n:
        return ""
    parts = n.split()
    # If all alpha tokens are uppercase, normalize to title case.
    alpha_parts = [p for p in parts if any(ch.isalpha() for ch in p)]
    if alpha_parts and all(p == p.upper() for p in alpha_parts):
        return " ".join(_title_token(p) for p in parts)
    # Otherwise only fix tokens that are still all-uppercase (common mixed-source case).
    out = []
    for p in parts:
        if any(ch.isalpha() for ch in p) and p == p.upper() and len(p) > 1:
            out.append(_title_token(p))
        else:
            out.append(p)
    return " ".join(out)


def norm_uci_id(v) -> str:
    s = "".join(ch for ch in str(v or "").strip() if ch.isdigit())
    # Use only UCI-like IDs for identity stitching.
    # This avoids treating local federation/member IDs (e.g. 8-digit USABMX memberId)
    # as global rider identity across series.
    if len(s) >= 10 and s.startswith("100"):
        return s
    return ""


def norm_name_key(name: str) -> str:
    s = clean_spaces(name).lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    parts = [p for p in s.split() if p]
    # Ignore single-letter middle initials to unify
    # "SIMON M MARQUART" and "SIMON MARQUART".
    parts = [p for p in parts if len(p) > 1]
    return " ".join(sorted(parts))


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
        # Handle date ranges like "10-11 FEB 2024" robustly.
        m_range = re.match(r"^(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+([A-Z]{3})\s+(\d{4})$", s.upper())
        if m_range:
            d1 = int(m_range.group(1))
            d2 = int(m_range.group(2))
            mon = m_range.group(3)
            y = int(m_range.group(4))
            month_map = {
                "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
            }
            mm = month_map.get(mon)
            candidates = []
            if mm:
                for dd in [d1, d2]:
                    dt = pd.to_datetime(f"{y:04d}-{mm:02d}-{dd:02d}", errors="coerce")
                    if pd.notna(dt):
                        candidates.append(dt)
            if candidates:
                if pd.notna(event_id_dt):
                    candidates = sorted(candidates, key=lambda d: abs((d - event_id_dt).days))
                return candidates[0]

        # Robust handling for mixed numeric formats:
        # WC can be YYYY-MM-DD, while some sources may use YYYY-DD-MM.
        m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
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
        dt = pd.to_datetime(s, errors="coerce")
        if pd.notna(dt):
            return dt
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
    if " FFC " in f" {n} ":
        return "FFC"
    if " SCC " in f" {n} ":
        return "SCC"
    if "USA BMX" in n or "PRO CHAMPIONSHIP" in n:
        return "USABMX"
    if "WORLD CHAMPIONSHIP" in n or "WCH" in n:
        return "WCH"
    if "EUROPEAN CHAMPIONSHIP" in n or "ECH" in n:
        return "ECH"
    if "EUROPE CUP" in n or "EC" in n:
        return "EC"
    if "WORLD CUP" in n or " WC " in f" {n} ":
        return "WC"
    if event_type in {"WC", "WM", "EC", "EM", "USABMX", "FFC", "SCC", "Other"}:
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


def derive_location(display_name: str, location: str, event_type: str) -> str:
    loc = clean_spaces(location)
    if loc and loc.lower() != "unknown":
        return loc

    dn = clean_spaces(display_name)
    m = re.search(r"ROUND\\s*(\\d+)\\s*-\\s*([^,]+)", dn, flags=re.IGNORECASE)
    if m:
        rnd = int(m.group(1))
        place = clean_spaces(m.group(2))
        if event_type == "WC":
            return f"ROUND {rnd} - {place}"
        return place

    up = dn.upper()
    if "WORLD CHAMPIONSHIP" in up:
        return "World Championships"

    parts = [clean_spaces(p) for p in dn.split("-") if clean_spaces(p)]
    if parts:
        return parts[-1].split(",")[0].strip()
    return "Unknown"


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
    if any(x in t for x in ["1/32", "1/16", "1/8", "1/4", "1/2", "lcq", "last chance"]):
        return "KO"
    if any(x in t for x in ["round 1", "moto", "seeding"]) or round_sort_value <= 12:
        return "Early"
    return "KO"


def round_short_label(round_title: str) -> str:
    t = str(round_title or "").lower()
    if "moto 1" in t:
        return "M1"
    if "moto 2" in t:
        return "M2"
    if "moto 3" in t:
        return "M3"
    if "lcq" in t or "last chance" in t:
        return "LCQ"
    if "1/32" in t:
        return "1/32"
    if "1/16" in t:
        return "1/16"
    if "1/8" in t:
        return "1/8"
    if "quarter" in t:
        return "QF"
    if "1/4" in t:
        return "1/4"
    if "semi" in t:
        return "SF"
    if "1/2" in t:
        return "1/2"
    if "main m1" in t:
        return "F1"
    if "main m2" in t:
        return "F2"
    if "main m3" in t:
        return "F3"
    if "final" in t:
        return "F"
    if "round 1" in t or "moto" in t or "seeding" in t:
        return "R1"
    # Keep unknown rounds visible/filterable instead of collapsing to R1.
    return clean_spaces(str(round_title or "")) or "R1"


def segment_short_label(segment: str) -> str:
    m = {
        "BottomDelta": "Bottom",
        "T1Delta": "T1",
        "T2Delta": "T2",
        "T3Delta": "T3",
        "Bottom->T1Delta": "B->T1",
        "T1->T2Delta": "T1->T2",
        "T2->T3Delta": "T2->T3",
        "T3->FinishDelta": "T3->F",
        "Final Rank": "Final Rank",
        "LaptimeDelta": "Laptime",
        "FinishDelta": "Laptime",
    }
    return m.get(str(segment), str(segment))


def safe_float(v):
    x = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
    return float(x) if pd.notna(x) else np.nan


def format_rank_value(v) -> str:
    x = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
    if pd.isna(x):
        return "NA"
    x = float(x)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.1f}"


def style_pdf_table(table, n_cols: int):
    """Improve readability of matplotlib table headers with wrapped labels."""
    # Slightly smaller body text, but clearer header rows.
    table.auto_set_font_size(False)
    table.set_fontsize(6.2)
    # Base cell scaling; make rows taller for wrapped headers/body.
    table.scale(1, 1.42)
    # Force wrap for all cells.
    for (_, _), cell in table.get_celld().items():
        cell.get_text().set_wrap(True)
    # Keep columns readable on A4 portrait.
    if n_cols > 0:
        col_w = min(0.98 / n_cols, 0.12)
        for c in range(n_cols):
            for r in range(0, max(k[0] for k in table.get_celld().keys()) + 1):
                if (r, c) in table.get_celld():
                    table[(r, c)].set_width(col_w)
    for c in range(n_cols):
        hcell = table[(0, c)]
        hcell.set_text_props(weight="bold", fontsize=5.8, ha="center", va="center")
        # Increase header row height to make 2-line headers readable.
        hcell.set_height(hcell.get_height() * 1.90)


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
    df["location"] = [
        derive_location(dn, loc, et)
        for dn, loc, et in zip(df["display_name"], df["location"], df["event_type"])
    ]
    df["event_short"] = [
        build_event_short(dn, loc, et)
        for dn, loc, et in zip(df["display_name"], df["location"], df["event_type"])
    ]
    df["nation"] = df["nation"].fillna("").astype(str).str.upper().str.strip()

    cats = df["group_id"].map(lambda g: GROUP_MAP.get(int(g), ("Unknown", "Unknown")) if pd.notna(g) else ("Unknown", "Unknown"))
    df["category"] = [c[0] for c in cats]
    df["gender"] = [c[1] for c in cats]

    df["name_clean"] = df["name"].fillna("").astype(str).apply(clean_spaces)
    df["name_pretty"] = df["name_clean"].apply(pretty_name)
    df["name_key"] = df["name_clean"].apply(norm_name_key)
    df["uci_norm"] = df["uci_id"].apply(norm_uci_id)
    df["name_nat_key"] = df["name_key"] + "|" + df["nation"]

    # Stitch source rows without UCI ID to the known UCI ID of the same normalized
    # rider name+nation when available. This avoids duplicate riders in selectors.
    uci_by_name_nat = (
        df[df["uci_norm"] != ""]
        .groupby("name_nat_key")["uci_norm"]
        .agg(lambda s: s.value_counts().index[0] if not s.empty else "")
        .to_dict()
    )
    df["uci_norm_stitched"] = df["uci_norm"]
    missing_uci = df["uci_norm_stitched"] == ""
    df.loc[missing_uci, "uci_norm_stitched"] = (
        df.loc[missing_uci, "name_nat_key"].map(uci_by_name_nat).fillna("")
    )

    df["rider_id"] = np.where(
        df["uci_norm_stitched"] != "",
        "uci:" + df["uci_norm_stitched"],
        "name:" + df["name_nat_key"],
    )
    df["rider_label_raw"] = df["name_pretty"] + " (" + df["nation"] + ")"

    counts = (
        df.groupby(["rider_id", "rider_label_raw"], as_index=False)
        .size()
        .rename(columns={"size": "cnt"})
    )
    counts["len"] = counts["rider_label_raw"].astype(str).str.len()
    # Prefer non-all-caps labels for display when counts tie.
    counts["name_part"] = counts["rider_label_raw"].str.replace(r"\s*\([A-Z]{2,3}\)\s*$", "", regex=True)
    counts["is_all_caps"] = counts["name_part"].fillna("").apply(
        lambda s: int(bool(s) and any(ch.isalpha() for ch in s) and s == s.upper())
    )
    counts = counts.sort_values(
        ["rider_id", "cnt", "is_all_caps", "len", "rider_label_raw"],
        ascending=[True, False, True, False, True],
    )
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
    event_top_n: int = 1,
    event_ko_final_only: bool = True,
    reference_source: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    out = df.copy()
    if event_top_n < 1:
        event_top_n = 1
    all_segments = ["start", "t1", "t2", "t3", "finish", "split_bottom_t1", "split_t1_t2", "split_t2_t3", "split_t3_finish"]

    # Event-level references (per event + category/group) from absolute segment times.
    if ref_key in {"event_topn", "event_top4", "event_best"}:
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
                            f"{seg}_event_topn_ref": g[seg].dropna().nsmallest(max(1, event_top_n)).mean()
                            if g[seg].notna().sum() > 0
                            else np.nan,
                            f"{seg}_event_best_ref": g[seg].dropna().min() if g[seg].notna().sum() > 0 else np.nan,
                        }
                    )
                )
                .reset_index()
            )
            out = out.merge(ref_df, on=group_cols, how="left")
            out[f"{seg}_delta_event_topn"] = out[seg] - out.get(f"{seg}_event_topn_ref")
            # Backward-compatible alias
            out[f"{seg}_delta_event_top4"] = out[f"{seg}_delta_event_topn"]
            out[f"{seg}_delta_event_best"] = out[seg] - out.get(f"{seg}_event_best_ref")

    map_suffix = {
        "rank4": "rank4",
        "winner": "winner",
        "event_topn": "event_topn",
        "event_top4": "event_topn",
        "event_best": "event_topn",
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
            # WC event_id encodes the exact race day reliably (YYYYMMDD).
            # Prefer this over parsed event_date text to avoid range/date-format drift.
            day_wc = pd.to_datetime(r.get("event_id_dt"), errors="coerce")
            if pd.notna(day_wc):
                day = day_wc.normalize()
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


def safe_sidebar_page_link(script_path: str, label: str) -> None:
    if os.path.exists(script_path):
        st.sidebar.page_link(script_path, label=label)


safe_sidebar_page_link("app.py", "Heat Analyser")
safe_sidebar_page_link("pages/3_Athlete_Insights.py", "Athlete Insights")
safe_sidebar_page_link("pages/4_Live_Polling.py", "Live Polling")
safe_sidebar_page_link("pages/9_CoachNow_Automation.py", "CoachNow Automation")
st.sidebar.divider()

st.title("Athlete Insights")
st.caption("Trend, Segmente, Positionen, Druck, Track-Profile, Benchmark, Fatigue und Result-Trend.")

# Keep Vega/Altair tooltips anchored at top-center for better readability on dense charts.
st.markdown(
    """
    <style>
      .vg-tooltip,
      .vega-tooltip {
        position: fixed !important;
        top: 10px !important;
        left: 50% !important;
        transform: translateX(-50%) !important;
        max-width: min(95vw, 1200px) !important;
        max-height: 70vh !important;
        overflow-y: auto !important;
        white-space: pre-line !important;
        z-index: 999999 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

all_runs = load_runs()
if all_runs.empty:
    st.warning("Keine Daten gefunden.")
    st.stop()
master_results = load_master_results()

rider_nation_opts = sorted([x for x in all_runs["nation"].dropna().unique().tolist() if x])

nf1, nf2 = st.columns([1, 3])
with nf1:
    sel_nations = st.multiselect("Nation (Rider) – leer = alle", rider_nation_opts, default=[])
with nf2:
    rider_pool_for_select = all_runs.copy()
    if sel_nations:
        rider_pool_for_select = rider_pool_for_select[rider_pool_for_select["nation"].isin(sel_nations)].copy()
    rider_opts = sorted([x for x in rider_pool_for_select["rider_label"].dropna().unique().tolist() if x])
    sel_riders = st.multiselect("Athlete (leer = keinen anzeigen)", rider_opts, default=[], key="insight_riders")

rider_mode = "athlete" if sel_riders else ("nation" if sel_nations else "none")
if rider_mode == "none":
    st.info("Bitte mindestens eine Nation oder einen Athleten auswaehlen.")
    st.stop()

rider_scope = all_runs.copy()
if rider_mode == "athlete":
    selected_ids_seed = (
        all_runs.loc[all_runs["rider_label"].isin(sel_riders), "rider_id"]
        .dropna()
        .unique()
        .tolist()
    )
    rider_scope = rider_scope[rider_scope["rider_id"].isin(selected_ids_seed)].copy()
else:
    rider_scope = rider_scope[rider_scope["nation"].isin(sel_nations)].copy()
    selected_ids_seed = (
        rider_scope["rider_id"].dropna().unique().tolist()
    )

if rider_scope.empty:
    st.warning("Keine Daten fuer die aktuelle Athleten-Auswahl.")
    st.stop()

event_type_opts = sorted([x for x in rider_scope["event_type"].dropna().unique().tolist() if x])
year_opts = sorted([int(x) for x in rider_scope["year"].dropna().unique().tolist()], reverse=True)
cat_opts = [x for x in ["Elite", "U23", "Junior"] if x in set(rider_scope["category"].dropna().unique().tolist())]
gender_opts = [x for x in ["Men", "Women"] if x in set(rider_scope["gender"].dropna().unique().tolist())]
default_years = [y for y in [2025, 2024, 2023] if y in year_opts]
if not default_years:
    default_years = year_opts

f1, f2, f3, f4 = st.columns(4)
with f1:
    sel_years = st.multiselect("Jahr", year_opts, default=default_years)
with f2:
    sel_event_types = st.multiselect("Event Type", event_type_opts, default=event_type_opts)
with f3:
    if rider_mode == "nation":
        sel_categories = st.multiselect("Kategorie", cat_opts, default=[])
    else:
        sel_categories = cat_opts
        st.multiselect("Kategorie", cat_opts, default=cat_opts, disabled=True, key="ai_cat_disabled")
with f4:
    if rider_mode == "nation":
        sel_gender = st.multiselect("Geschlecht", gender_opts, default=[])
    else:
        sel_gender = gender_opts
        st.multiselect("Geschlecht", gender_opts, default=gender_opts, disabled=True, key="ai_gender_disabled")

if rider_mode == "nation" and (not sel_categories or not sel_gender):
    st.info("Bei Nation-Auswahl bitte Kategorie und Geschlecht angeben.")
    st.stop()

loc_scope = rider_scope.copy()
if sel_years:
    loc_scope = loc_scope[loc_scope["year"].isin(sel_years)]
if sel_event_types:
    loc_scope = loc_scope[loc_scope["event_type"].isin(sel_event_types)]
if rider_mode == "nation" and sel_categories:
    loc_scope = loc_scope[loc_scope["category"].isin(sel_categories)]
if rider_mode == "nation" and sel_gender:
    loc_scope = loc_scope[loc_scope["gender"].isin(sel_gender)]
loc_opts = sorted([x for x in loc_scope["location"].dropna().unique().tolist() if x])

g1, g2 = st.columns(2)
with g1:
    sel_locations = st.multiselect("Location (optional)", loc_opts, default=[])
with g2:
    round_order_pref = ["R1", "LCQ", "1/32", "1/16", "1/8", "1/4", "1/2", "F", "M1", "M2", "M3", "QF", "SF", "F1", "F2", "F3"]
    round_seen = [x for x in loc_scope["round_short"].dropna().astype(str).unique().tolist() if clean_spaces(x)]
    round_opts = [x for x in round_order_pref if x in set(round_seen)] + [x for x in sorted(round_seen) if x not in round_order_pref]
    # New round families (USABMX etc.) are available but intentionally not default-selected.
    round_defaults = [x for x in ["R1", "LCQ", "1/32", "1/16", "1/8", "1/4", "1/2", "F"] if x in set(round_opts)]
    sel_rounds = st.multiselect("Runde (optional)", round_opts, default=round_defaults)

# Comparison/reference pool must stay on full field for selected filters.
base_scope = all_runs.copy()
if rider_mode == "nation" and sel_nations:
    base_scope = base_scope[base_scope["nation"].isin(sel_nations)]
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

if rider_mode == "athlete":
    selected_ids = (
        all_runs.loc[all_runs["rider_label"].isin(sel_riders), "rider_id"]
        .dropna()
        .unique()
        .tolist()
    )
else:
    selected_ids = (
        rider_scope["rider_id"]
        .dropna()
        .unique()
        .tolist()
    )

if not selected_ids:
    st.warning("Keine Rider fuer die aktuelle Auswahl.")
    st.stop()

ref_label = st.radio(
    "Referenz",
    ["Event Top N (robust)", "Heat Rank 4 (Qualification Cut)", "Heat Rank 1 (Winner)"],
    horizontal=True,
    index=0,
)
event_top_n = 1
event_ko_final_only = False
if ref_label == "Event Top N (robust)":
    with st.container():
        if "event_top_n" in st.session_state and st.session_state["event_top_n"] not in [1, 3, 8]:
            st.session_state["event_top_n"] = 1
        event_top_n = st.selectbox("Event Top N", [1, 3, 8], index=0, key="event_top_n")

ref_key = "rank4"
if ref_label == "Event Top N (robust)":
    ref_key = "event_topn"
elif ref_label == "Heat Rank 4 (Qualification Cut)":
    ref_key = "rank4"
elif ref_label == "Heat Rank 1 (Winner)":
    ref_key = "winner"

ref_caption = ref_label
if ref_label == "Event Top N (robust)":
    base_ref = f"Event Top{event_top_n}"
    ref_caption = f"{base_ref} (gefilterte Runden)"
st.caption(f"Aktive Delta-Referenz: {ref_caption}")

base_rel = add_heat_relative_metrics(base_scope)
base_rel_ref = apply_reference(
    base_rel,
    ref_key,
    event_top_n=event_top_n,
    event_ko_final_only=event_ko_final_only,
    reference_source=base_rel,
)
# Pool with all riders from current filters (without rider filter),
# used for robust thresholds and ranking against full category field.
pool_rel = add_robust_outlier_flags_and_winsor(
    base_rel_ref.copy(),
    reference_df=base_rel_ref,
    group_cols=["category", "gender"],
)
runs_sel = pool_rel[pool_rel["rider_id"].isin(selected_ids)].copy()
runs_sel = runs_sel.sort_values(["event_dt", "event_id", "round_sort", "heat_id"])
runs_sel = attach_final_rank_event(runs_sel, master_results)
ranks_pool_all = attach_final_rank_event(pool_rel.copy(), master_results)
if not ranks_pool_all.empty:
    rank_fb = (
        ranks_pool_all[["rider_id", "event_id", "final_rank_event"]]
        .copy()
    )
    rank_fb["final_rank_event"] = pd.to_numeric(rank_fb["final_rank_event"], errors="coerce")
    rank_fb = (
        rank_fb.dropna(subset=["final_rank_event"])
        .groupby(["rider_id", "event_id"], as_index=False)["final_rank_event"]
        .min()
        .rename(columns={"final_rank_event": "final_rank_event_fb"})
    )
    runs_sel = runs_sel.merge(rank_fb, on=["rider_id", "event_id"], how="left")
    runs_sel["final_rank_event"] = pd.to_numeric(runs_sel["final_rank_event"], errors="coerce")
    runs_sel["final_rank_event_fb"] = pd.to_numeric(runs_sel["final_rank_event_fb"], errors="coerce")
    runs_sel["final_rank_event"] = runs_sel["final_rank_event"].where(
        runs_sel["final_rank_event"].notna(), runs_sel["final_rank_event_fb"]
    )
    runs_sel = runs_sel.drop(columns=["final_rank_event_fb"], errors="ignore")
runs_sel["final_rank_event_display"] = np.where(
    pd.to_numeric(runs_sel["final_rank_event"], errors="coerce").notna(),
    pd.to_numeric(runs_sel["final_rank_event"], errors="coerce").astype("Int64").astype(str),
    "NA",
)
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
    round_order_map = {
        "R1": 1, "M1": 1, "M2": 2, "M3": 3,
        "LCQ": 4, "1/32": 5, "1/16": 6, "1/8": 7, "QF": 8, "1/4": 8,
        "SF": 9, "1/2": 9, "F1": 10, "F2": 11, "F3": 12, "F": 13,
    }
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
    plot_long_base = pd.DataFrame()
    if not plot_long.empty:
        plot_long = plot_long.dropna(subset=["delta"])
        plot_long_base = plot_long.copy()
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
    summary_src = pd.DataFrame()
    if summary_label and not plot_long_base.empty:
        summary_src = (
            plot_long_base[plot_long_base["metric"] == summary_label][["year", "rider_short", "event_id", "delta"]]
            .dropna(subset=["delta"])
            .copy()
        )

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

    st.markdown("**Peak Segment Profile**")
    contrib_src = runs_sel.copy()
    rank_pool_src = pool_rel.copy()
    ref_suffix_map = {
        "rank4": "rank4_ref",
        "winner": "winner",
        "event_topn": "event_topn_ref",
        "event_top4": "event_topn_ref",
        "event_best": "event_topn_ref",
    }
    ref_suffix = ref_suffix_map.get(ref_key, "rank4_ref")
    seg_defs = [
        {"label": "BottomDelta", "delta_col": "start_delta", "time_col": "start", "seg_base": "start"},
        {"label": "T1Delta", "delta_col": "t1_delta", "time_col": "t1", "seg_base": "t1"},
        {"label": "T2Delta", "delta_col": "t2_delta", "time_col": "t2", "seg_base": "t2"},
        {"label": "T3Delta", "delta_col": "t3_delta", "time_col": "t3", "seg_base": "t3"},
        {"label": "LaptimeDelta", "delta_col": "finish_delta", "time_col": "finish", "seg_base": "finish"},
        {"label": "Bottom->T1Delta", "delta_col": "split_bottom_t1_delta", "time_col": "split_bottom_t1", "seg_base": "split_bottom_t1"},
        {"label": "T1->T2Delta", "delta_col": "split_t1_t2_delta", "time_col": "split_t1_t2", "seg_base": "split_t1_t2"},
        {"label": "T2->T3Delta", "delta_col": "split_t2_t3_delta", "time_col": "split_t2_t3", "seg_base": "split_t2_t3"},
        {"label": "T3->FinishDelta", "delta_col": "split_t3_finish_delta", "time_col": "split_t3_finish", "seg_base": "split_t3_finish"},
    ]
    available_defs = []
    for sd in seg_defs:
        delta_col = sd["delta_col"]
        time_col = sd["time_col"]
        ref_col = f"{sd['seg_base']}_{ref_suffix}"
        delta_use_col = f"{delta_col}_w" if f"{delta_col}_w" in contrib_src.columns else delta_col
        if delta_use_col not in contrib_src.columns:
            continue
        if time_col not in contrib_src.columns:
            continue
        if ref_col not in contrib_src.columns:
            continue
        if pd.to_numeric(contrib_src[delta_use_col], errors="coerce").notna().sum() == 0:
            continue
        sd2 = sd.copy()
        sd2["delta_use_col"] = delta_use_col
        sd2["ref_col"] = ref_col
        available_defs.append(sd2)

    available_seg_labels = [sd["label"] for sd in available_defs]
    segment_options = ["Final Rank"] + [x for x in available_seg_labels if x != "Final Rank"]
    default_segments = [
        "Final Rank",
        "BottomDelta",
        "Bottom->T1Delta",
        "T1->T2Delta",
        "T2->T3Delta",
        "T3->FinishDelta",
    ]
    default_segment_selection = [x for x in default_segments if x in segment_options]
    if not default_segment_selection:
        default_segment_selection = segment_options
    selected_seg_labels = st.multiselect(
        "Segmente anzeigen",
        options=segment_options,
        default=default_segment_selection,
        key="peak_seg_segments",
    )
    peak_mode = st.selectbox(
        "Peak Selection",
        ["All", "Best Run", "Best 3 Runs", "Best 5 Runs", "Best 10%", "Best 20%"],
        index=5,
        key="peak_seg_mode",
    )
    peak_per_location = st.checkbox(
        "Peak per Location",
        value=True,
        key="peak_seg_per_location",
        help="Wenn aktiv: pro Location wird zuerst nur der beste Run genommen, dann der Peak daraus berechnet.",
    )
    # For Event Top1 only: reference rider row can be compared vs Rank 2 for visibility.
    show_delta_vs_rank2 = ref_key in {"event_topn", "event_top4", "event_best"} and int(event_top_n) == 1
    show_overall_median = st.toggle(
        "Show Overall Median (Reality Check)",
        value=False,
        key="peak_seg_show_overall",
    )
    if show_delta_vs_rank2:
        st.caption("Bei Event Top 1 wird nur fuer den Referenz-Rider Delta gegen Rank 2 berechnet.")

    def _take_n(n_rows: int, mode: str) -> int:
        if n_rows <= 0:
            return 0
        if mode == "All":
            return n_rows
        if mode == "Best Run":
            return 1
        if mode == "Best 3 Runs":
            return min(3, n_rows)
        if mode == "Best 5 Runs":
            return min(5, n_rows)
        if mode == "Best 10%":
            return max(1, int(np.ceil(n_rows * 0.10)))
        return max(1, int(np.ceil(n_rows * 0.20)))

    def _pick_peak_rows(g_in: pd.DataFrame) -> pd.DataFrame:
        g_base = g_in.copy()
        g_peak_base = g_base.drop_duplicates(subset=["location"], keep="first") if peak_per_location else g_base
        if peak_mode == "All":
            return g_peak_base.copy()
        if peak_mode == "Best Run":
            if peak_per_location:
                return g_peak_base.copy()
            return g_peak_base.nsmallest(1, "delta_display").copy()
        if g_peak_base.empty:
            return g_peak_base
        k = _take_n(len(g_peak_base), peak_mode)
        return g_peak_base.nsmallest(k, "delta_display").copy()

    peak_rows = []
    coverage_rows = []
    for sd in available_defs:
        if sd["label"] not in selected_seg_labels:
            continue
        dcol = sd["delta_use_col"]
        dcol_raw = sd["delta_col"]
        tcol = sd["time_col"]
        rcol = sd["ref_col"]

        seg_cols = [
            "rider_short",
            "category",
            "gender",
            "display_name",
            "location",
            "round_short",
            "round_title",
            "event_dt",
            "event_id",
            "group_id",
            dcol,
            dcol_raw,
            tcol,
            rcol,
            "round_sort",
            "heat_id",
        ]
        seg_cols = list(dict.fromkeys(seg_cols))
        seg_df = contrib_src[seg_cols].copy()
        rename_map = {
            tcol: "segment_time",
            rcol: "reference_time",
        }
        if dcol == dcol_raw:
            rename_map[dcol] = "delta_value"
            seg_df = seg_df.rename(columns=rename_map)
            seg_df["delta_raw"] = seg_df["delta_value"]
        else:
            rename_map[dcol] = "delta_value"
            rename_map[dcol_raw] = "delta_raw"
            seg_df = seg_df.rename(columns=rename_map)
        seg_df["delta_value"] = pd.to_numeric(seg_df["delta_value"], errors="coerce")
        seg_df["delta_raw"] = pd.to_numeric(seg_df["delta_raw"], errors="coerce")
        seg_df["segment_time"] = pd.to_numeric(seg_df["segment_time"], errors="coerce")
        seg_df["reference_time"] = pd.to_numeric(seg_df["reference_time"], errors="coerce")
        seg_df["rank2_ref_heat"] = seg_df.groupby(
            ["event_id", "group_id", "heat_id", "round_sort"], dropna=False
        )["segment_time"].transform(
            lambda s: s.dropna().nsmallest(2).iloc[-1] if s.notna().sum() >= 2 else np.nan
        )
        seg_df["rank2_ref_event"] = seg_df.groupby(
            ["event_id", "group_id"], dropna=False
        )["segment_time"].transform(
            lambda s: s.dropna().nsmallest(2).iloc[-1] if s.notna().sum() >= 2 else np.nan
        )
        seg_df["ref_time_display"] = seg_df["reference_time"]
        seg_df["ref_display_type"] = "Aktive Referenz"
        seg_df["delta_display"] = seg_df["delta_value"]
        if show_delta_vs_rank2:
            is_best_row = (
                seg_df["segment_time"].notna()
                & seg_df["reference_time"].notna()
                & np.isclose(seg_df["segment_time"], seg_df["reference_time"], atol=1e-6)
            )
            use_rank2 = is_best_row & seg_df["rank2_ref_event"].notna()
            seg_df.loc[use_rank2, "ref_time_display"] = seg_df.loc[use_rank2, "rank2_ref_event"]
            seg_df.loc[use_rank2, "ref_display_type"] = "Rank 2 (Event, nur Referenz-Rider)"
            seg_df.loc[use_rank2, "delta_display"] = (
                seg_df.loc[use_rank2, "segment_time"] - seg_df.loc[use_rank2, "rank2_ref_event"]
            )
        seg_df["is_valid_peak_row"] = (
            seg_df["delta_display"].notna() & seg_df["segment_time"].notna() & seg_df["ref_time_display"].notna()
        )
        cov = (
            seg_df.groupby("rider_short", as_index=False)
            .agg(n_total=("event_id", "count"), n_valid=("is_valid_peak_row", "sum"))
        )
        cov["Segment"] = sd["label"]
        coverage_rows.extend(cov.to_dict("records"))
        seg_df = seg_df[seg_df["is_valid_peak_row"]].copy()
        if seg_df.empty:
            continue
        seg_df = seg_df.sort_values(["delta_display", "event_dt", "event_id", "round_sort", "heat_id"], na_position="last")

        for rider, g in seg_df.groupby("rider_short", dropna=False):
            g_all = g.copy()
            peak_sel = _pick_peak_rows(g_all)
            if peak_sel.empty:
                continue

            peak_delta = float(peak_sel["delta_display"].median())
            peak_time = float(peak_sel["segment_time"].median())
            peak_ref = float(peak_sel["ref_time_display"].median())
            peak_pct = (peak_delta / peak_ref * 100.0) if pd.notna(peak_ref) and peak_ref != 0 else np.nan
            peak_runs_text = " | ".join(
                [
                    f"{clean_spaces(ev)} | {clean_spaces(loc)} | {clean_spaces(rsh)} | {stm:.3f}s"
                    for ev, loc, rsh, stm in zip(
                        peak_sel["display_name"].fillna(peak_sel["event_id"]).astype(str),
                        peak_sel["location"].fillna("Unknown").astype(str),
                        peak_sel["round_short"].fillna(peak_sel["round_title"]).astype(str),
                        peak_sel["segment_time"],
                    )
                ]
            )
            peak_runs_meta_lines = [
                f"{int(y) if pd.notna(y) else 'NA'} | {clean_spaces(loc)} | {clean_spaces(rsh)} | {clean_spaces(cat)}"
                for y, loc, rsh, cat in zip(
                    peak_sel["event_dt"].dt.year,
                    peak_sel["location"].fillna("Unknown").astype(str),
                    peak_sel["round_short"].fillna(peak_sel["round_title"]).astype(str),
                    peak_sel["category"].fillna("Unknown").astype(str),
                )
            ]
            peak_runs_meta_text = "\n".join(peak_runs_meta_lines)
            peak_runs_meta_html = "<br>".join(peak_runs_meta_lines)

            peak_rows.append(
                {
                    "Rider": rider,
                    "Segment": sd["label"],
                    "Category": peak_sel["category"].mode().iloc[0] if "category" in peak_sel.columns and not peak_sel["category"].mode().empty else "Unknown",
                    "Gender": peak_sel["gender"].mode().iloc[0] if "gender" in peak_sel.columns and not peak_sel["gender"].mode().empty else "Unknown",
                    "Profile": "Peak",
                    "Delta (s)": peak_delta,
                    "Delta (% ref)": peak_pct,
                    "Rider Segment Time (s)": peak_time,
                    "Reference Segment Time (s)": peak_ref,
                    "Reference Mode": peak_sel["ref_display_type"].iloc[0],
                    "Active Reference": ref_caption,
                    "Runs Used (n)": int(len(peak_sel)),
                    "Locations Used (n)": int(peak_sel["location"].nunique(dropna=True)),
                    "Peak Runs": peak_runs_text,
                    "Peak Runs (meta)": peak_runs_meta_text,
                    "Peak Runs (meta html)": peak_runs_meta_html,
                }
            )

            if show_overall_median:
                ov_delta = float(g_all["delta_display"].median())
                ov_time = float(g_all["segment_time"].median())
                ov_ref = float(g_all["ref_time_display"].median())
                ov_pct = (ov_delta / ov_ref * 100.0) if pd.notna(ov_ref) and ov_ref != 0 else np.nan
                peak_rows.append(
                    {
                        "Rider": rider,
                        "Segment": sd["label"],
                        "Category": g_all["category"].mode().iloc[0] if "category" in g_all.columns and not g_all["category"].mode().empty else "Unknown",
                        "Gender": g_all["gender"].mode().iloc[0] if "gender" in g_all.columns and not g_all["gender"].mode().empty else "Unknown",
                        "Profile": "Overall Median",
                        "Delta (s)": ov_delta,
                        "Delta (% ref)": ov_pct,
                        "Rider Segment Time (s)": ov_time,
                        "Reference Segment Time (s)": ov_ref,
                        "Reference Mode": g_all["ref_display_type"].iloc[0],
                        "Active Reference": ref_caption,
                        "Runs Used (n)": int(len(g_all)),
                        "Locations Used (n)": int(g_all["location"].nunique(dropna=True)),
                        "Peak Runs": "",
                        "Peak Runs (meta)": "",
                        "Peak Runs (meta html)": "",
                    }
                )

    peak_df = pd.DataFrame(peak_rows)
    if not peak_df.empty:
        # Segment ranks are computed against full field (same filters, without rider filter),
        # grouped by Segment + Category + Gender.
        rank_rows = []
        for sd in available_defs:
            if sd["label"] not in selected_seg_labels:
                continue
            dcol = sd["delta_use_col"]
            dcol_raw = sd["delta_col"]
            tcol = sd["time_col"]
            rcol = sd["ref_col"]

            rseg_cols = [
                "rider_short",
                "category",
                "gender",
                "location",
                "event_dt",
                "event_id",
                "group_id",
                dcol,
                dcol_raw,
                tcol,
                rcol,
                "round_sort",
                "heat_id",
            ]
            rseg_cols = list(dict.fromkeys(rseg_cols))
            rseg = rank_pool_src[rseg_cols].copy()
            rrename = {tcol: "segment_time", rcol: "reference_time"}
            if dcol == dcol_raw:
                rrename[dcol] = "delta_value"
                rseg = rseg.rename(columns=rrename)
                rseg["delta_raw"] = rseg["delta_value"]
            else:
                rrename[dcol] = "delta_value"
                rrename[dcol_raw] = "delta_raw"
                rseg = rseg.rename(columns=rrename)

            rseg["delta_value"] = pd.to_numeric(rseg["delta_value"], errors="coerce")
            rseg["segment_time"] = pd.to_numeric(rseg["segment_time"], errors="coerce")
            rseg["reference_time"] = pd.to_numeric(rseg["reference_time"], errors="coerce")
            rseg["rank2_ref_event"] = rseg.groupby(["event_id", "group_id"], dropna=False)["segment_time"].transform(
                lambda s: s.dropna().nsmallest(2).iloc[-1] if s.notna().sum() >= 2 else np.nan
            )
            rseg["ref_time_display"] = rseg["reference_time"]
            rseg["delta_display"] = rseg["delta_value"]
            if show_delta_vs_rank2:
                is_best_row = (
                    rseg["segment_time"].notna()
                    & rseg["reference_time"].notna()
                    & np.isclose(rseg["segment_time"], rseg["reference_time"], atol=1e-6)
                )
                use_rank2 = is_best_row & rseg["rank2_ref_event"].notna()
                rseg.loc[use_rank2, "ref_time_display"] = rseg.loc[use_rank2, "rank2_ref_event"]
                rseg.loc[use_rank2, "delta_display"] = (
                    rseg.loc[use_rank2, "segment_time"] - rseg.loc[use_rank2, "rank2_ref_event"]
                )
            rseg = rseg.dropna(subset=["delta_display", "segment_time", "ref_time_display"])
            if rseg.empty:
                continue
            rseg = rseg.sort_values(["delta_display", "event_dt", "event_id", "round_sort", "heat_id"], na_position="last")

            for rider, g in rseg.groupby("rider_short", dropna=False):
                peak_sel = _pick_peak_rows(g)
                if peak_sel.empty:
                    continue
                rank_rows.append(
                    {
                        "Rider": rider,
                        "Segment": sd["label"],
                        "Category": peak_sel["category"].mode().iloc[0] if not peak_sel["category"].mode().empty else "Unknown",
                        "Gender": peak_sel["gender"].mode().iloc[0] if not peak_sel["gender"].mode().empty else "Unknown",
                        "Delta (s) rank_base": float(peak_sel["delta_display"].median()),
                    }
                )

        rank_pool_df = pd.DataFrame(rank_rows)
        if not rank_pool_df.empty:
            rank_pool_df["Segment Rank"] = rank_pool_df.groupby(["Segment", "Category", "Gender"])["Delta (s) rank_base"].rank(method="min", ascending=True)
            rank_pool_df["Field Size"] = rank_pool_df.groupby(["Segment", "Category", "Gender"])["Rider"].transform("nunique")
            rank_pool_df["Rank %"] = np.where(
                rank_pool_df["Field Size"] > 0,
                (rank_pool_df["Segment Rank"] / rank_pool_df["Field Size"]) * 100.0,
                np.nan,
            )
            rank_cols = rank_pool_df[
                ["Rider", "Segment", "Category", "Gender", "Segment Rank", "Field Size", "Rank %"]
            ].drop_duplicates(subset=["Rider", "Segment", "Category", "Gender"])
            peak_df = peak_df.merge(rank_cols, on=["Rider", "Segment", "Category", "Gender"], how="left")
        else:
            peak_df["Segment Rank"] = np.nan
            peak_df["Field Size"] = np.nan
            peak_df["Rank %"] = np.nan
        peak_df.loc[peak_df["Profile"] != "Peak", ["Segment Rank", "Field Size", "Rank %"]] = np.nan

        rider_fr = (
            runs_sel[["rider_short", "event_id", "final_rank_event"]]
            .drop_duplicates(subset=["rider_short", "event_id"])
            .copy()
        )
        rider_fr["final_rank_event"] = pd.to_numeric(rider_fr["final_rank_event"], errors="coerce")
        rider_fr_med = rider_fr.groupby("rider_short", as_index=False)["final_rank_event"].median()
        fr_map = rider_fr_med.set_index("rider_short")["final_rank_event"].to_dict()
        peak_df["Final Rank (median)"] = peak_df["Rider"].map(fr_map)
        peak_df["Final Rank (median) display"] = np.where(
            pd.to_numeric(peak_df["Final Rank (median)"], errors="coerce").notna(),
            pd.to_numeric(peak_df["Final Rank (median)"], errors="coerce").round(1).astype(str),
            "NA",
        )

        # Add one synthetic radar metric for Final Rank per rider (single value).
        pool_fr = attach_final_rank_event(pool_rel.copy(), master_results)
        pool_fr = (
            pool_fr[["rider_short", "category", "gender", "event_id", "location", "final_rank_event"]]
            .drop_duplicates(subset=["rider_short", "category", "gender", "event_id"])
            .copy()
        )
        pool_fr["final_rank_event"] = pd.to_numeric(pool_fr["final_rank_event"], errors="coerce")
        pool_fr_med = (
            pool_fr.groupby(["rider_short", "category", "gender"], as_index=False)["final_rank_event"].median()
        )
        pool_field = (
            pool_fr_med.dropna(subset=["final_rank_event"])
            .groupby(["category", "gender"], as_index=False)
            .agg(field_size=("rider_short", "nunique"))
        )
        rider_meta = (
            runs_sel.groupby("rider_short", as_index=False)
            .agg(
                category=("category", lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]),
                gender=("gender", lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]),
                runs_used=("event_id", "nunique"),
                locations_used=("location", "nunique"),
            )
        )
        fr_rows = rider_meta.merge(pool_fr_med, on=["rider_short", "category", "gender"], how="left")
        fr_rows = fr_rows.merge(pool_field, on=["category", "gender"], how="left")
        fr_rows = fr_rows.dropna(subset=["final_rank_event"]).copy()
        if not fr_rows.empty:
            fr_rows["Segment Rank"] = pd.to_numeric(fr_rows["final_rank_event"], errors="coerce")
            fr_rows["Field Size"] = pd.to_numeric(fr_rows["field_size"], errors="coerce")
            fr_rows["Rank %"] = np.where(
                fr_rows["Field Size"] > 0,
                (fr_rows["Segment Rank"] / fr_rows["Field Size"]) * 100.0,
                np.nan,
            )
            fr_rows["Final Rank (median)"] = fr_rows["Segment Rank"]
            fr_rows["Final Rank (median) display"] = fr_rows["Segment Rank"].round(1).astype(str)
            fr_rows["Profile"] = "Peak"
            fr_rows["Segment"] = "Final Rank"
            fr_rows["Delta (s)"] = np.nan
            fr_rows["Delta (% ref)"] = np.nan
            fr_rows["Rider Segment Time (s)"] = np.nan
            fr_rows["Reference Segment Time (s)"] = np.nan
            fr_rows["Reference Mode"] = "Final Classification"
            fr_rows["Active Reference"] = ref_caption
            fr_rows["Runs Used (n)"] = fr_rows["runs_used"].astype(int)
            fr_rows["Locations Used (n)"] = fr_rows["locations_used"].astype(int)
            fr_rows["Peak Runs"] = ""
            fr_rows["Peak Runs (meta)"] = ""
            fr_rows["Peak Runs (meta html)"] = ""
            fr_rows["Rider"] = fr_rows["rider_short"]
            fr_rows["Category"] = fr_rows["category"]
            fr_rows["Gender"] = fr_rows["gender"]
            add_cols = [
                "Rider",
                "Segment",
                "Category",
                "Gender",
                "Profile",
                "Delta (s)",
                "Delta (% ref)",
                "Rider Segment Time (s)",
                "Reference Segment Time (s)",
                "Reference Mode",
                "Active Reference",
                "Runs Used (n)",
                "Locations Used (n)",
                "Peak Runs",
                "Segment Rank",
                "Field Size",
                "Rank %",
                "Final Rank (median)",
                "Final Rank (median) display",
            ]
            peak_df = pd.concat([peak_df, fr_rows[add_cols]], ignore_index=True)

        # Ensure every peak row carries the same rider-level final-rank value
        # that is used for the synthetic "Final Rank" segment.
        fr_row_map = (
            peak_df[peak_df["Segment"] == "Final Rank"][["Rider", "Final Rank (median)"]]
            .dropna(subset=["Final Rank (median)"])
            .drop_duplicates(subset=["Rider"], keep="first")
            .set_index("Rider")["Final Rank (median)"]
            .to_dict()
        )
        if fr_row_map:
            peak_df["Final Rank (median)"] = np.where(
                pd.to_numeric(peak_df["Final Rank (median)"], errors="coerce").notna(),
                pd.to_numeric(peak_df["Final Rank (median)"], errors="coerce"),
                peak_df["Rider"].map(fr_row_map),
            )
            peak_df["Final Rank (median) display"] = np.where(
                pd.to_numeric(peak_df["Final Rank (median)"], errors="coerce").notna(),
                pd.to_numeric(peak_df["Final Rank (median)"], errors="coerce").round(1).astype(str),
                "NA",
            )

        tt = [
            alt.Tooltip("Rider:N"),
            alt.Tooltip("Segment:N"),
            alt.Tooltip("Profile:N"),
            alt.Tooltip("Segment Rank:Q", format=".0f"),
            alt.Tooltip("Field Size:Q", format=".0f"),
            alt.Tooltip("Rank %:Q", format=".1f"),
            alt.Tooltip("Delta (s):Q", format=".4f"),
            alt.Tooltip("Delta (% ref):Q", format=".2f"),
            alt.Tooltip("Rider Segment Time (s):Q", format=".4f"),
            alt.Tooltip("Reference Segment Time (s):Q", format=".4f"),
                alt.Tooltip("Reference Mode:N"),
                alt.Tooltip("Active Reference:N"),
                alt.Tooltip("Final Rank (median) display:N", title="Final Rank"),
                alt.Tooltip("Runs Used (n):Q"),
                alt.Tooltip("Locations Used (n):Q"),
                alt.Tooltip("Peak Runs (meta):N", title="Runs (Year | Location | Round | Category)"),
                alt.Tooltip("Peak Runs:N"),
            ]
        bottom_peak = peak_df[peak_df["Segment"] == "BottomDelta"].copy()
        other_peak = peak_df[(peak_df["Segment"] != "BottomDelta") & (peak_df["Segment"] != "Final Rank")].copy()

        rider_domain = sorted(peak_df["Rider"].dropna().unique().tolist())
        rider_color = alt.Color("Rider:N", title="Rider", scale=alt.Scale(domain=rider_domain))

        c_left, c_right = st.columns([14, 36])
        with c_left:
            if not bottom_peak.empty:
                bmin = float(pd.to_numeric(bottom_peak["Delta (s)"], errors="coerce").min())
                bmax = float(pd.to_numeric(bottom_peak["Delta (s)"], errors="coerce").max())
                by_min = min(-0.05, bmin - 0.01)
                by_max = max(0.2, bmax + 0.01)
                c_bottom = (
                    alt.Chart(bottom_peak)
                    .mark_bar()
                    .encode(
                        x=alt.X("Rider:N", title="Rider", axis=alt.Axis(labelAngle=-90, labelLimit=180)),
                        xOffset=alt.XOffset("Profile:N"),
                        y=alt.Y("Delta (s):Q", title="Bottom Delta (s)", scale=alt.Scale(domain=[by_min, by_max], nice=False)),
                        color=alt.Color("Rider:N", scale=alt.Scale(domain=rider_domain), legend=None),
                        tooltip=tt,
                    )
                    .properties(height=320)
                )
                st.altair_chart(c_bottom, use_container_width=True)
            else:
                st.info("Kein BottomDelta fuer aktuelle Filter.")
        with c_right:
            if not other_peak.empty:
                other_peak_plot = other_peak.copy()
                # Show rider bars side-by-side (not stacked). If both profiles are shown,
                # include profile in the offset key to avoid overlap.
                if show_overall_median:
                    other_peak_plot["offset_key"] = other_peak_plot["Rider"] + " | " + other_peak_plot["Profile"]
                else:
                    other_peak_plot["offset_key"] = other_peak_plot["Rider"]
                c_other = (
                    alt.Chart(other_peak_plot)
                    .mark_bar()
                    .encode(
                        x=alt.X("Segment:N", sort=[x for x in selected_seg_labels if x != "BottomDelta"]),
                        xOffset=alt.XOffset("offset_key:N", title=None),
                        y=alt.Y("Delta (s):Q", title="Delta (s)", stack=None),
                        color=rider_color,
                        tooltip=tt,
                    )
                    .properties(height=320)
                )
                st.altair_chart(c_other, use_container_width=True)
            else:
                st.info("Keine weiteren Segment-Deltas fuer aktuelle Filter.")

        st.markdown("**Peak Segment Radar (Rank)**")
        radar_df = peak_df[peak_df["Profile"] == "Peak"].copy()
        radar_df["Segment Rank"] = pd.to_numeric(radar_df["Segment Rank"], errors="coerce")
        radar_df["Field Size"] = pd.to_numeric(radar_df["Field Size"], errors="coerce")
        radar_df = radar_df.dropna(subset=["Segment Rank", "Field Size"])
        if not radar_df.empty:
            seg_order = [x for x in selected_seg_labels if x in radar_df["Segment"].unique().tolist()]
            radar_df["Segment Short"] = radar_df["Segment"].apply(segment_short_label)
            seg_counts = radar_df.groupby("Rider")["Segment"].nunique()
            riders_ok = seg_counts[seg_counts >= 2].index.tolist()
            hidden = sorted(set(radar_df["Rider"]) - set(riders_ok))
            if hidden:
                st.caption("Radar blendet Rider mit <2 verfuegbaren Segmenten aus: " + ", ".join(hidden))
            radar_df = radar_df[radar_df["Rider"].isin(riders_ok)].copy()
            if radar_df["Rider"].nunique() > 4:
                rider_top = (
                    radar_df.groupby("Rider", as_index=False)["Segment Rank"]
                    .mean()
                    .sort_values(["Segment Rank", "Rider"], ascending=[True, True])
                )
                keep_riders = rider_top["Rider"].head(4).tolist()
                st.warning("Mehr als 4 Rider im Radar. Es werden automatisch die Top 4 (bester mittlerer Segment Rank) angezeigt.")
                radar_df = radar_df[radar_df["Rider"].isin(keep_riders)].copy()
            fr_scope = pd.to_numeric(runs_sel.get("final_rank_event"), errors="coerce")
            has_top8 = fr_scope.notna().any() and (fr_scope <= 8).any()
            fixed_radar_max_rank = 48 if has_top8 else 80
            radar_df["Segment Rank Plot"] = pd.to_numeric(radar_df["Segment Rank"], errors="coerce").clip(lower=1, upper=fixed_radar_max_rank)
            if not radar_df.empty and len(seg_order) >= 2:
                seg_order_short = [segment_short_label(x) for x in seg_order]
                radar_df["Rank Top %"] = np.where(
                    radar_df["Field Size"] > 0,
                    (radar_df["Segment Rank"] / radar_df["Field Size"]) * 100.0,
                    np.nan,
                )
                radar_df["Segment Rank Text"] = radar_df.apply(
                    lambda r: f"{format_rank_value(r.get('Segment Rank'))}/{format_rank_value(r.get('Field Size'))}",
                    axis=1,
                )
                if go is None:
                    st.warning("Radar benoetigt `plotly`. Fallback-Ansicht wird angezeigt.")
                    fallback = (
                        alt.Chart(radar_df)
                        .mark_line(point=True)
                        .encode(
                            x=alt.X("Segment Short:N", sort=seg_order_short),
                            y=alt.Y(
                                "Segment Rank Plot:Q",
                                title="Segment Rank (1=best)",
                                scale=alt.Scale(domain=[1, fixed_radar_max_rank], reverse=False),
                            ),
                            color=alt.Color("Rider:N", title="Rider"),
                            detail="Rider:N",
                            tooltip=[
                                alt.Tooltip("Rider:N"),
                                alt.Tooltip("Segment Short:N", title="Segment"),
                                alt.Tooltip("Segment Rank Text:N", title="Segment Rank"),
                                alt.Tooltip("Rank Top %:Q", title="Rank %", format=".1f"),
                                alt.Tooltip("Delta (s):Q", format=".4f"),
                                alt.Tooltip("Runs Used (n):Q", format=".0f"),
                                alt.Tooltip("Reference Mode:N"),
                                alt.Tooltip("Active Reference:N"),
                                alt.Tooltip("Final Rank (median) display:N", title="Final Rank"),
                                alt.Tooltip("Peak Runs (meta):N", title="Runs (Year | Location | Round | Category)"),
                            ],
                        )
                        .properties(height=420)
                    )
                    st.altair_chart(fallback, use_container_width=True)
                else:
                    fig = go.Figure()
                    for rider, g in radar_df.groupby("Rider", dropna=False):
                        gm = g.set_index("Segment Short")
                        r_vals = []
                        custom = []
                        for seg in seg_order_short:
                            if seg in gm.index:
                                row = gm.loc[seg]
                                if isinstance(row, pd.DataFrame):
                                    row = row.iloc[0]
                                r_vals.append(float(row["Segment Rank Plot"]))
                                custom.append(
                                    [
                                        row.get("Segment Rank Text", "NA/NA"),
                                        row.get("Rank Top %", np.nan),
                                        row.get("Delta (s)", np.nan),
                                        row.get("Runs Used (n)", np.nan),
                                        row.get("Reference Mode", ""),
                                        row.get("Active Reference", ""),
                                        row.get("Final Rank (median) display", "NA"),
                                        row.get("Peak Runs (meta html)", ""),
                                    ]
                                )
                            else:
                                continue
                        if len(r_vals) < 2:
                            continue
                        theta_vals = [s for s in seg_order_short if s in gm.index]
                        r_plot = r_vals + [r_vals[0]]
                        theta_plot = theta_vals + [theta_vals[0]]
                        custom_plot = custom + [custom[0]]
                        fig.add_trace(
                            go.Scatterpolar(
                                r=r_plot,
                                theta=theta_plot,
                                mode="lines+markers",
                                name=str(rider),
                                fill="none",
                                customdata=custom_plot,
                                hovertemplate=(
                                    "Rider: %{fullData.name}<br>"
                                    "Segment: %{theta}<br>"
                                    "Segment Rank: %{customdata[0]}<br>"
                                    "Rank %%: %{customdata[1]:.1f}%<br>"
                                    "Delta (s): %{customdata[2]:.4f}<br>"
                                    "Runs Used (n): %{customdata[3]:.0f}<br>"
                                    "Reference Mode: %{customdata[4]}<br>"
                                    "Active Reference: %{customdata[5]}<br>"
                                    "Final Rank: %{customdata[6]}<br>"
                                    "Runs (Y|Loc|Rnd|Cat):<br>%{customdata[7]}<extra></extra>"
                                ),
                            )
                        )
                    ring_vals = [48, 32, 16, 8, 1] if fixed_radar_max_rank == 48 else [80, 32, 16, 8, 1]
                    fig.update_layout(
                        height=560,
                        showlegend=True,
                        margin=dict(l=10, r=10, t=10, b=20),
                        legend=dict(orientation="h", y=-0.12, x=0.5, xanchor="center", yanchor="top"),
                        polar=dict(
                            domain=dict(x=[0.08, 0.92], y=[0.06, 0.98]),
                            radialaxis=dict(
                                autorange=False,
                                range=[fixed_radar_max_rank, 1],
                                tickmode="array",
                                tickvals=ring_vals,
                                ticktext=[str(v) for v in ring_vals],
                                showticklabels=True,
                                ticks="outside",
                                tickfont=dict(size=11),
                            ),
                            angularaxis=dict(categoryorder="array", categoryarray=seg_order_short),
                        ),
                    )
                    st.plotly_chart(fig, use_container_width=True)
                    st.caption(
                        "Segment Rank (1=best). Referenzringe (fix): "
                        + ("1, 8, 16, 32, 48." if fixed_radar_max_rank == 48 else "1, 8, 16, 32, 80.")
                    )
            else:
                st.info("Nicht genug Daten fuer Peak Segment Radar.")
        else:
            st.info("Keine Peak-Ranks verfuegbar fuer Radar.")

        # PDF export for Peak Segment Radar report (per-event + overall).
        if plt is None or PdfPages is None:
            st.caption("PDF-Export verfuegbar nach Installation von `matplotlib`.")
        else:
            export_segment_order = [
                "BottomDelta",
                "Bottom->T1Delta",
                "T1Delta",
                "T1->T2Delta",
                "T2Delta",
                "T2->T3Delta",
                "T3->FinishDelta",
                "LaptimeDelta",
                "Final Rank",
            ]
            export_segment_order = [
                s
                for s in export_segment_order
                if (s == "Final Rank") or (s in [d["label"] for d in available_defs])
            ]
            export_seg_idx = {s: i for i, s in enumerate(export_segment_order)}

            def _compute_event_peak_df(event_slice: pd.DataFrame, rank_pool_slice: pd.DataFrame) -> pd.DataFrame:
                rows = []
                seg_defs_for_export = [d for d in available_defs if d["label"] in export_segment_order]
                for sd in seg_defs_for_export:
                    if sd["label"] not in export_segment_order:
                        continue
                    dcol = sd["delta_use_col"]
                    dcol_raw = sd["delta_col"]
                    tcol = sd["time_col"]
                    rcol = sd["ref_col"]
                    # Selected riders view
                    cols = [
                        "rider_short", "category", "gender", "location", "display_name",
                        "round_short", "round_title", "heat_title", "event_id", "event_dt",
                        "group_id", "round_sort", "heat_id", dcol, dcol_raw, tcol, rcol,
                    ]
                    cols = list(dict.fromkeys(cols))
                    s = event_slice[cols].copy()
                    rnm = {tcol: "segment_time", rcol: "reference_time"}
                    if dcol == dcol_raw:
                        rnm[dcol] = "delta_value"
                        s = s.rename(columns=rnm)
                        s["delta_raw"] = s["delta_value"]
                    else:
                        rnm[dcol] = "delta_value"
                        rnm[dcol_raw] = "delta_raw"
                        s = s.rename(columns=rnm)
                    s["delta_value"] = pd.to_numeric(s["delta_value"], errors="coerce")
                    s["segment_time"] = pd.to_numeric(s["segment_time"], errors="coerce")
                    s["reference_time"] = pd.to_numeric(s["reference_time"], errors="coerce")
                    s["rank2_ref_event"] = s.groupby(["event_id", "group_id"], dropna=False)["segment_time"].transform(
                        lambda q: q.dropna().nsmallest(2).iloc[-1] if q.notna().sum() >= 2 else np.nan
                    )
                    s["delta_display"] = s["delta_value"]
                    if show_delta_vs_rank2:
                        is_best_row = (
                            s["segment_time"].notna()
                            & s["reference_time"].notna()
                            & np.isclose(s["segment_time"], s["reference_time"], atol=1e-6)
                        )
                        use_rank2 = is_best_row & s["rank2_ref_event"].notna()
                        s.loc[use_rank2, "delta_display"] = (
                            s.loc[use_rank2, "segment_time"] - s.loc[use_rank2, "rank2_ref_event"]
                        )
                    s = s.dropna(subset=["delta_display", "segment_time", "reference_time"])
                    if s.empty:
                        continue
                    s = s.sort_values(["delta_display", "round_sort", "heat_id"], na_position="last")

                    # Full field rank pool for this event/segment.
                    rp = rank_pool_slice[cols].copy()
                    rp = rp.rename(columns=rnm)
                    rp["delta_value"] = pd.to_numeric(rp["delta_value"], errors="coerce")
                    rp["segment_time"] = pd.to_numeric(rp["segment_time"], errors="coerce")
                    rp["reference_time"] = pd.to_numeric(rp["reference_time"], errors="coerce")
                    rp["rank2_ref_event"] = rp.groupby(["event_id", "group_id"], dropna=False)["segment_time"].transform(
                        lambda q: q.dropna().nsmallest(2).iloc[-1] if q.notna().sum() >= 2 else np.nan
                    )
                    rp["delta_display"] = rp["delta_value"]
                    if show_delta_vs_rank2:
                        is_best_row = (
                            rp["segment_time"].notna()
                            & rp["reference_time"].notna()
                            & np.isclose(rp["segment_time"], rp["reference_time"], atol=1e-6)
                        )
                        use_rank2 = is_best_row & rp["rank2_ref_event"].notna()
                        rp.loc[use_rank2, "delta_display"] = (
                            rp.loc[use_rank2, "segment_time"] - rp.loc[use_rank2, "rank2_ref_event"]
                        )
                    rp = rp.dropna(subset=["delta_display", "segment_time", "reference_time"])
                    if rp.empty:
                        continue
                    rp = rp.sort_values(["delta_display", "round_sort", "heat_id"], na_position="last")

                    # Peak per rider in selected view.
                    for rider, g in s.groupby("rider_short", dropna=False):
                        gsel = _pick_peak_rows(g)
                        if gsel.empty:
                            continue
                        peak_delta = float(gsel["delta_display"].median())
                        peak_time = float(gsel["segment_time"].median())
                        peak_ref = float(gsel["reference_time"].median())
                        pct = (peak_delta / peak_ref * 100.0) if pd.notna(peak_ref) and peak_ref != 0 else np.nan
                        peak_runs = " | ".join(
                            [
                                f"{clean_spaces(ev)} | {clean_spaces(rsh)} | {clean_spaces(hh)} | {stm:.3f}s"
                                for ev, rsh, hh, stm in zip(
                                    gsel["display_name"].fillna(gsel["event_id"]).astype(str),
                                    gsel["round_short"].fillna(gsel["round_title"]).astype(str),
                                    gsel["heat_title"].fillna("").astype(str),
                                    gsel["segment_time"],
                                )
                            ]
                        )
                        round_list = " | ".join(
                            list(
                                dict.fromkeys(
                                    gsel["round_short"].fillna(gsel["round_title"]).astype(str).tolist()
                                )
                            )
                        )

                        # Segment rank in full field.
                        rr = []
                        for rid2, g2 in rp.groupby("rider_short", dropna=False):
                            g2sel = _pick_peak_rows(g2)
                            if g2sel.empty:
                                continue
                            rr.append({"Rider": rid2, "rank_base": float(g2sel["delta_display"].median())})
                        rrdf = pd.DataFrame(rr)
                        seg_rank = np.nan
                        field_size = np.nan
                        rank_pct = np.nan
                        if not rrdf.empty:
                            rrdf["seg_rank"] = rrdf["rank_base"].rank(method="min", ascending=True)
                            field_size = int(rrdf["Rider"].nunique())
                            m = rrdf[rrdf["Rider"] == rider]
                            if not m.empty:
                                seg_rank = float(m["seg_rank"].iloc[0])
                                rank_pct = (seg_rank / field_size) * 100.0 if field_size > 0 else np.nan

                        rows.append(
                            {
                                "Rider": rider,
                                "Segment": sd["label"],
                                "Segment Short": segment_short_label(sd["label"]),
                                "Delta (s)": peak_delta,
                                "Delta (% ref)": pct,
                                "Rider Segment Time (s)": peak_time,
                                "Reference Segment Time (s)": peak_ref,
                                "Reference Mode": ref_caption,
                                "Runs Used (n)": int(len(gsel)),
                                "Peak Runs": peak_runs,
                                "Rounds": round_list,
                                "Segment Rank": seg_rank,
                                "Field Size": field_size,
                                "Rank %": rank_pct,
                            }
                        )

                # Add event-level final classification as synthetic segment.
                ev = event_slice.copy()
                rp_ev = rank_pool_slice.copy()
                if "final_rank_event" not in ev.columns:
                    ev = attach_final_rank_event(ev, master_results)
                if "final_rank_event" not in rp_ev.columns:
                    rp_ev = attach_final_rank_event(rp_ev, master_results)
                ev["final_rank_event"] = pd.to_numeric(ev.get("final_rank_event"), errors="coerce")
                rp_ev["final_rank_event"] = pd.to_numeric(rp_ev.get("final_rank_event"), errors="coerce")
                ev_fr = (
                    ev[["rider_short", "category", "gender", "final_rank_event"]]
                    .dropna(subset=["final_rank_event"])
                    .sort_values(["rider_short", "final_rank_event"], na_position="last")
                    .drop_duplicates(subset=["rider_short"], keep="first")
                    .copy()
                )
                if not ev_fr.empty:
                    pool_fr = (
                        rp_ev[["rider_short", "category", "gender", "final_rank_event"]]
                        .dropna(subset=["final_rank_event"])
                        .sort_values(["rider_short", "final_rank_event"], na_position="last")
                        .drop_duplicates(subset=["rider_short"], keep="first")
                        .copy()
                    )
                    if not pool_fr.empty:
                        for _, rr in ev_fr.iterrows():
                            cat = rr.get("category")
                            gen = rr.get("gender")
                            rider = rr.get("rider_short")
                            rnk = float(rr.get("final_rank_event"))
                            field = pool_fr[
                                (pool_fr["category"] == cat) & (pool_fr["gender"] == gen)
                            ].copy()
                            seg_rank = np.nan
                            field_size = np.nan
                            rank_pct = np.nan
                            if not field.empty:
                                field = field.sort_values("final_rank_event", ascending=True, na_position="last")
                                field["seg_rank"] = field["final_rank_event"].rank(method="min", ascending=True)
                                field_size = int(field["rider_short"].nunique())
                                one = field[field["rider_short"] == rider]
                                if not one.empty:
                                    seg_rank = float(one["seg_rank"].iloc[0])
                                    rank_pct = (seg_rank / field_size) * 100.0 if field_size > 0 else np.nan
                            rows.append(
                                {
                                    "Rider": rider,
                                    "Segment": "Final Rank",
                                    "Segment Short": segment_short_label("Final Rank"),
                                    "Delta (s)": np.nan,
                                    "Delta (% ref)": np.nan,
                                    "Rider Segment Time (s)": np.nan,
                                    "Reference Segment Time (s)": np.nan,
                                    "Reference Mode": "Final Classification",
                                    "Runs Used (n)": 1,
                                    "Peak Runs": "",
                                    "Rounds": "Final classification",
                                    "Segment Rank": seg_rank,
                                    "Field Size": field_size,
                                    "Rank %": rank_pct,
                                }
                            )
                return pd.DataFrame(rows)

            def _draw_radar(ax, one_rider_df: pd.DataFrame, title: str):
                if one_rider_df.empty:
                    return
                d = one_rider_df.copy()
                if "Segment Short" not in d.columns:
                    if "Segment" in d.columns:
                        d["Segment Short"] = d["Segment"].apply(segment_short_label)
                    else:
                        return
                d["Segment Rank"] = pd.to_numeric(d["Segment Rank"], errors="coerce")
                d["seg_idx"] = d["Segment"].map(export_seg_idx)
                d = d.sort_values("seg_idx", na_position="last")
                if d.empty:
                    return
                labels = [segment_short_label(x) for x in export_segment_order]
                theta = np.linspace(0, 2 * np.pi, len(export_segment_order), endpoint=False)
                rank_map = d.set_index("Segment")["Segment Rank"].to_dict()
                rank_vals = np.array([safe_float(rank_map.get(seg)) for seg in export_segment_order], dtype=float)
                theta = np.concatenate([theta, [theta[0]]])
                # Dynamic, page-stable max rank: use field size of this page/rider view.
                field_max = int(pd.to_numeric(d["Field Size"], errors="coerce").max()) if d["Field Size"].notna().any() else 0
                finite_ranks = rank_vals[np.isfinite(rank_vals)]
                rank_max = int(finite_ranks.max()) if finite_ranks.size > 0 else 0
                max_rank = max(16, field_max, rank_max)
                # Keep axis non-inverted; transform rank so Rank 1 is at the outer ring.
                # r_plot = max_rank + 1 - rank
                r_plot_vals = (max_rank + 1.0) - rank_vals
                r = np.concatenate([r_plot_vals, [r_plot_vals[0]]])
                ax.plot(theta, r, linewidth=2)
                valid_mask = ~np.isnan(r_plot_vals)
                ax.scatter(theta[:-1][valid_mask], r[:-1][valid_mask], s=20)
                ax.set_title(title, fontsize=10, pad=18)
                ax.set_xticks(theta[:-1])
                ax.set_xticklabels(labels, fontsize=8)
                ax.set_ylim(0.5, max_rank + 0.5)
                ring_ranks = [x for x in [1, 8, 16, 32] if x <= max_rank]
                ring_pos = [(max_rank + 1) - x for x in ring_ranks]
                ax.set_yticks(ring_pos)
                ax.set_yticklabels([str(x) for x in ring_ranks], fontsize=7)
                ax.set_theta_offset(np.pi / 2)
                ax.set_theta_direction(-1)

            export_disabled = radar_df.empty if "radar_df" in locals() else True
            if st.button("Export Peak Radar PDF", disabled=export_disabled, key="export_peak_radar_pdf"):
                if export_disabled:
                    st.warning("Keine Radar-Daten fuer Export.")
                else:
                    pdf_buffer = BytesIO()
                    rider_list = sorted(radar_df["Rider"].dropna().unique().tolist())
                    # Event order by normalized date.
                    event_order = (
                        runs_sel[["event_id", "event_dt", "display_name", "location"]]
                        .drop_duplicates("event_id")
                        .sort_values(["event_dt", "event_id"], na_position="last")
                    )
                    with PdfPages(pdf_buffer) as pdf:
                        for rider in rider_list:
                            rider_event_peaks: list[pd.DataFrame] = []
                            # Header page
                            fig = plt.figure(figsize=(8.27, 11.69))
                            fig.text(0.08, 0.94, f"Peak Segment Radar Report - {rider}", fontsize=14, weight="bold")
                            fig.text(0.08, 0.90, f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", fontsize=9)
                            fig.text(0.08, 0.87, f"Reference: {ref_caption}", fontsize=9)
                            fig.text(0.08, 0.84, f"Peak Selection: {peak_mode} | Peak per Location: {'ON' if peak_per_location else 'OFF'}", fontsize=9)
                            fig.text(0.08, 0.81, "Further details per event on following pages.", fontsize=9)
                            plt.axis("off")
                            pdf.savefig(fig, bbox_inches="tight")
                            plt.close(fig)

                            # Event pages
                            for _, erow in event_order.iterrows():
                                eid = erow["event_id"]
                                eview = runs_sel[runs_sel["event_id"] == eid].copy()
                                erank = rank_pool_src[rank_pool_src["event_id"] == eid].copy()
                                evt_peak = _compute_event_peak_df(eview, erank)
                                evt_peak = evt_peak[evt_peak["Rider"] == rider].copy()
                                if evt_peak.empty:
                                    continue
                                evt_peak["event_id"] = eid
                                evt_peak["event_location"] = clean_spaces(str(erow["location"]))
                                rider_event_peaks.append(evt_peak.copy())
                                fig = plt.figure(figsize=(8.27, 11.69))
                                ax = plt.subplot(211, projection="polar")
                                event_title = f"{clean_spaces(str(erow['display_name']))} | {clean_spaces(str(erow['location']))}"
                                event_title_wrapped = textwrap.fill(event_title, width=78, max_lines=2, placeholder="...")
                                _draw_radar(ax, evt_peak, event_title_wrapped)
                                fig.text(0.08, 0.50, f"Rider: {rider}", fontsize=9, weight="bold")
                                fig.text(0.08, 0.48, "Aussen = besser (Rank 1).", fontsize=8)
                                t_ax = plt.subplot(212)
                                t_ax.axis("off")
                                tbl = evt_peak[[
                                    "Segment Short",
                                    "Segment Rank",
                                    "Field Size",
                                    "Rank %",
                                    "Delta (s)",
                                    "Delta (% ref)",
                                    "Rider Segment Time (s)",
                                    "Runs Used (n)",
                                    "Rounds",
                                ]].copy()
                                tbl["seg_idx"] = tbl["Segment Short"].map(
                                    {segment_short_label(k): i for i, k in enumerate(export_segment_order)}
                                )
                                tbl = tbl.sort_values("seg_idx", na_position="last").drop(columns=["seg_idx"])
                                base_segments = pd.DataFrame(
                                    {"Segment": [segment_short_label(k) for k in export_segment_order]}
                                )
                                tbl = tbl.rename(
                                    columns={
                                        "Segment Short": "Segment",
                                        "Segment Rank": "Rank",
                                        "Field Size": "Field",
                                        "Rank %": "Rank %",
                                        "Delta (s)": "Delta\n(s)",
                                        "Delta (% ref)": "Delta\n(% ref)",
                                        "Rider Segment Time (s)": "Rider Time\n(s)",
                                        "Runs Used (n)": "Runs",
                                        "Rounds": "Round(s)",
                                    }
                                )
                                tbl = base_segments.merge(tbl, on="Segment", how="left")
                                tbl["Rank"] = tbl["Rank"].apply(format_rank_value)
                                for c in ["Field", "Runs"]:
                                    tbl[c] = pd.to_numeric(tbl[c], errors="coerce").round(0).astype("Int64").astype(str)
                                for c in ["Rank %", "Delta\n(s)", "Delta\n(% ref)", "Rider Time\n(s)"]:
                                    tbl[c] = pd.to_numeric(tbl[c], errors="coerce").round(4)
                                table = t_ax.table(
                                    cellText=tbl.values,
                                    colLabels=tbl.columns,
                                    loc="center",
                                )
                                style_pdf_table(table, len(tbl.columns))
                                pdf.savefig(fig, bbox_inches="tight")
                                plt.close(fig)

                            # Overall page for rider
                            ov = pd.DataFrame()
                            if rider_event_peaks:
                                ov_all = pd.concat(rider_event_peaks, ignore_index=True)
                                num_cols = [
                                    "Segment Rank",
                                    "Field Size",
                                    "Rank %",
                                    "Delta (s)",
                                    "Delta (% ref)",
                                    "Rider Segment Time (s)",
                                    "Runs Used (n)",
                                ]
                                for c in num_cols:
                                    ov_all[c] = pd.to_numeric(ov_all[c], errors="coerce")
                                grp_cols = ["Rider", "Segment", "Segment Short"]
                                ov = ov_all.groupby(grp_cols, dropna=False)[
                                    ["Segment Rank", "Field Size", "Rank %", "Delta (s)", "Delta (% ref)", "Rider Segment Time (s)"]
                                ].mean(numeric_only=True).reset_index()
                                ov_ref_mode = (
                                    ov_all.groupby(grp_cols, dropna=False)["Reference Mode"]
                                    .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else clean_spaces(str(s.iloc[0])) if len(s) else "")
                                    .reset_index(name="Reference Mode")
                                )
                                ov_runs = (
                                    ov_all.groupby(grp_cols, dropna=False)["Runs Used (n)"]
                                    .sum(min_count=1)
                                    .reset_index(name="Runs Used (n)")
                                )
                                ov_var = (
                                    ov_all.groupby(grp_cols, dropna=False)["Rider Segment Time (s)"]
                                    .agg(lambda s: (s.max() - s.min()) / 2 if s.notna().sum() > 1 else 0.0)
                                    .reset_index(name="Rider Segment Variation (±s)")
                                )
                                ev_used = (
                                    ov_all.groupby(grp_cols, dropna=False)["event_id"]
                                    .nunique()
                                    .reset_index(name="Events Used (n)")
                                )
                                loc_used = (
                                    ov_all.groupby(grp_cols, dropna=False)["event_location"]
                                    .nunique()
                                    .reset_index(name="Locations Used (n)")
                                )
                                ov = (
                                    ov.merge(ov_ref_mode, on=grp_cols, how="left")
                                    .merge(ov_runs, on=grp_cols, how="left")
                                    .merge(ov_var, on=grp_cols, how="left")
                                    .merge(ev_used, on=grp_cols, how="left")
                                    .merge(loc_used, on=grp_cols, how="left")
                                )
                                ov = ov.sort_values(
                                    "Segment",
                                    key=lambda s: s.map(export_seg_idx).fillna(999),
                                )
                            if not ov.empty:
                                fig = plt.figure(figsize=(8.27, 11.69))
                                ax = plt.subplot(211, projection="polar")
                                _draw_radar(ax, ov.rename(columns={"Segment": "Segment", "Segment Rank": "Segment Rank"}), "Overall Peak Segment Radar")
                                fig.text(0.08, 0.50, f"Rider: {rider}", fontsize=9, weight="bold")
                                fig.text(0.08, 0.48, "Aussen = besser (Rank 1).", fontsize=8)
                                t_ax = plt.subplot(212)
                                t_ax.axis("off")
                                ov_tbl = ov[[
                                    "Segment",
                                    "Segment Rank",
                                    "Field Size",
                                    "Rank %",
                                    "Delta (s)",
                                    "Delta (% ref)",
                                    "Rider Segment Variation (±s)",
                                    "Runs Used (n)",
                                    "Locations Used (n)",
                                    "Events Used (n)",
                                ]].copy()
                                ov_tbl["seg_idx"] = ov_tbl["Segment"].map(export_seg_idx)
                                ov_tbl = ov_tbl.sort_values("seg_idx", na_position="last").drop(columns=["seg_idx"])
                                base_segments = pd.DataFrame({"Segment": export_segment_order})
                                ov_tbl = base_segments.merge(ov_tbl, on="Segment", how="left")
                                ov_tbl["Segment"] = ov_tbl["Segment"].apply(segment_short_label)
                                ov_tbl = ov_tbl.rename(
                                    columns={
                                        "Segment Rank": "Rank",
                                        "Field Size": "Field",
                                        "Rank %": "Rank %",
                                        "Delta (s)": "Delta\n(s)",
                                        "Delta (% ref)": "Delta\n(% ref)",
                                        "Rider Segment Variation (±s)": "Rider Var\n(±s)",
                                        "Runs Used (n)": "Runs",
                                        "Locations Used (n)": "Locs",
                                        "Events Used (n)": "Events",
                                    }
                                )
                                ov_tbl["Rank"] = ov_tbl["Rank"].apply(format_rank_value)
                                for c in ["Field", "Runs", "Locs", "Events"]:
                                    ov_tbl[c] = pd.to_numeric(ov_tbl[c], errors="coerce").round(0).astype("Int64").astype(str)
                                for c in ["Rank %", "Delta\n(s)", "Delta\n(% ref)", "Rider Var\n(±s)"]:
                                    ov_tbl[c] = pd.to_numeric(ov_tbl[c], errors="coerce").round(4)
                                table = t_ax.table(cellText=ov_tbl.values, colLabels=ov_tbl.columns, loc="center")
                                style_pdf_table(table, len(ov_tbl.columns))
                                pdf.savefig(fig, bbox_inches="tight")
                                plt.close(fig)
                    pdf_bytes = pdf_buffer.getvalue()
                    y_lbl = "-".join(str(y) for y in sorted(sel_years)) if sel_years else "all"
                    c_lbl = "_".join(x.lower() for x in sel_categories) if sel_categories else "all"
                    g_lbl = "_".join(x.lower() for x in sel_gender) if sel_gender else "all"
                    out_name = f"PeakRadar_{c_lbl}_{g_lbl}_{y_lbl}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                    st.download_button(
                        label="Download Peak Segment Radar PDF",
                        data=pdf_bytes,
                        file_name=out_name,
                        mime="application/pdf",
                        key="dl_peak_radar_pdf",
                    )

        peak_table = peak_df.copy()
        for col in ["Delta (s)", "Delta (% ref)", "Rider Segment Time (s)", "Reference Segment Time (s)", "Rank %"]:
            peak_table[col] = pd.to_numeric(peak_table[col], errors="coerce").round(4)
        peak_table["Segment Rank"] = pd.to_numeric(peak_table["Segment Rank"], errors="coerce").round(2)
        for col in ["Field Size", "Runs Used (n)", "Locations Used (n)"]:
            peak_table[col] = pd.to_numeric(peak_table[col], errors="coerce").round(0).astype("Int64")
        st.dataframe(peak_table, use_container_width=True, hide_index=True)
        cov_df = pd.DataFrame(coverage_rows)
        if not cov_df.empty:
            cov_df["missing_reason"] = np.where(cov_df["n_valid"] == 0, "Keine gueltigen Kombinationen aus Delta+SegmentTime+Reference", "")
            st.dataframe(
                cov_df.sort_values(["Segment", "rider_short"] if "rider_short" in cov_df.columns else ["Segment"]),
                use_container_width=True,
                hide_index=True,
            )
        st.caption("Peak wird pro Rider × Segment unabhaengig berechnet (Top-Auswahl je Segment, danach Median).")
    else:
        st.info("Keine Daten fuer Peak Segment Profile in der aktuellen Auswahl.")

    st.markdown("**Start Delta vs Finish Delta**")
    scat = runs_sel.dropna(subset=["start_delta", "finish_delta"]).copy()
    if not scat.empty:
        # Keep axes readable for coaching use: fixed windows.
        x_min, x_max = 0.0, 0.5
        y_min, y_max = 0.0, 3.0
        scat_plot = scat[
            (scat["start_delta"] >= x_min)
            & (scat["start_delta"] <= x_max)
            & (scat["finish_delta"] >= y_min)
            & (scat["finish_delta"] <= y_max)
        ].copy()
        if scat_plot.empty:
            st.info("Keine Punkte im Bereich Start 0.0-0.5s und Finish 0.0-3.0s.")
        else:
            diag = pd.DataFrame({"x": [x_min, x_max], "y": [x_min, x_max]})
            scatter = (
                alt.Chart(scat_plot)
                .mark_circle(opacity=0.75)
                .encode(
                    x=alt.X("start_delta:Q", title="Start Delta (s)", scale=alt.Scale(domain=[x_min, x_max])),
                    y=alt.Y("finish_delta:Q", title="Finish Delta (s)", scale=alt.Scale(domain=[y_min, y_max])),
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
            scat["pos_start"] = pd.to_numeric(scat["pos_start"], errors="coerce")
            scat["pos_finish"] = pd.to_numeric(scat["pos_finish"], errors="coerce")
            scat = scat.dropna(subset=["pos_start", "pos_finish"]).copy()
            scat["delta_start_to_finish"] = pd.to_numeric(scat["delta_start_to_finish"], errors="coerce")
            scat["move_state"] = np.where(
                scat["delta_start_to_finish"] > 0,
                "Plätze gewonnen",
                np.where(scat["delta_start_to_finish"] < 0, "Plätze verloren", "Neutral"),
            )
            total_points = len(scat)
            # BMX gates are 1..8; hide out-of-range points from this view.
            scat = scat[
                (scat["pos_start"] >= 1)
                & (scat["pos_start"] <= 8)
                & (scat["pos_finish"] >= 1)
                & (scat["pos_finish"] <= 8)
            ].copy()
            removed_points = total_points - len(scat)
        if not scat.empty:
            axis_x = alt.X(
                "pos_start:Q",
                title="Position Start",
                scale=alt.Scale(domain=[1, 8], clamp=True),
                axis=alt.Axis(values=[1, 2, 3, 4, 5, 6, 7, 8], format="d"),
            )
            axis_y = alt.Y(
                "pos_finish:Q",
                title="Position Finish",
                scale=alt.Scale(domain=[1, 8], clamp=True),
                axis=alt.Axis(values=[1, 2, 3, 4, 5, 6, 7, 8], format="d"),
            )
            bg_df = pd.DataFrame({"x": [1, 8], "diag": [1, 8], "top": [8, 8], "bottom": [1, 1]})
            red_upper = (
                alt.Chart(bg_df)
                .mark_area(opacity=0.12, color="#ef8a8a")
                .encode(x="x:Q", y="top:Q", y2="diag:Q")
            )
            green_lower = (
                alt.Chart(bg_df)
                .mark_area(opacity=0.12, color="#74c476")
                .encode(x="x:Q", y="diag:Q", y2="bottom:Q")
            )
            diag_line = (
                alt.Chart(pd.DataFrame({"x": [1, 8], "y": [1, 8]}))
                .mark_line(color="#808080", strokeWidth=2)
                .encode(x="x:Q", y="y:Q")
            )
            points = (
                alt.Chart(scat)
                .mark_circle(opacity=0.9, size=110)
                .encode(
                    x=axis_x,
                    y=axis_y,
                    color=alt.Color("rider_short:N", title="Rider"),
                    tooltip=[
                        "rider_short:N",
                        "event_id:N",
                        "round_title:N",
                        "heat_title:N",
                        "pos_start:Q",
                        "pos_finish:Q",
                        "delta_start_to_finish:Q",
                        "move_state:N",
                        "start:Q",
                        "t1:Q",
                        "finish:Q",
                    ],
                )
                .properties(height=300)
            )
            st.altair_chart(green_lower + red_upper + diag_line + points, use_container_width=True)
            if removed_points > 0:
                st.caption(f"Hinweis: {removed_points} Punkte ausserhalb Gate-Range 1-8 wurden im Scatter ausgeblendet.")

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
    trend_chart_mode = st.toggle(
        "Boxplot statt Liniengrafik",
        value=False,
        key="results_trend_boxplot_mode",
    )
    show_dnq_labels = st.toggle("Show DNQ labels (>32)", value=True, key="results_trend_show_dnq_labels")
    rr = runs_sel.copy().sort_values(["rider_id", "event_dt", "event_id", "round_sort", "heat_id"])

    # One row per rider+event to map overall/final classification.
    rider_event = (
        rr.groupby(["rider_id", "event_id"], as_index=False)
        .agg(
            rider_short=("rider_short", "first"),
            rider_label=("rider_label", "first"),
            event_short=("event_short", "first"),
            event_label_full=("display_name", "first"),
            category=("category", lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]),
            gender=("gender", lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]),
            reached_phase=("phase", lambda s: "Final" if (s == "Final").any() else ("KO" if (s == "KO").any() else "Early")),
            event_dt=("event_dt", "first"),
            location=("location", "first"),
            year=("year", "first"),
        )
    )
    event_rank_map = rr[["rider_id", "event_id", "final_rank_event"]].copy()
    event_rank_map["final_rank_event"] = pd.to_numeric(event_rank_map["final_rank_event"], errors="coerce")
    event_rank_map = (
        event_rank_map.dropna(subset=["final_rank_event"])
        .groupby(["rider_id", "event_id"], as_index=False)["final_rank_event"]
        .min()
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
            plot_df["x_index"] = plot_df["x_order"].astype(float)
            plot_df["final_rank_num"] = pd.to_numeric(plot_df["final_rank"], errors="coerce")
            plot_df["final_rank_raw"] = plot_df["final_rank_num"]
            plot_df["is_overflow"] = plot_df["final_rank_raw"] > 32
            plot_df["final_rank_plot"] = np.where(
                plot_df["is_overflow"],
                33.0,
                plot_df["final_rank_raw"],
            )
            plot_df["line_rank_plot"] = plot_df["final_rank_raw"].clip(upper=32)
            plot_df["overflow_clamped"] = np.where(plot_df["is_overflow"], "yes", "no")
            plot_df["final_rank_over32_label"] = np.where(
                plot_df["is_overflow"],
                plot_df["final_rank_raw"].astype("Int64").astype(str),
                "",
            )
            plot_df["event_label"] = (
                plot_df["event_label_full"].fillna(plot_df["event_short"]).fillna(plot_df["event_id"])
            )
            plot_df["event_date_display"] = (
                plot_df["event_dt"].dt.strftime("%Y-%m-%d").fillna(plot_df["event_id"])
                + " | "
                + plot_df["location"]
                + " | "
                + plot_df["rider_short"]
            )
            max_rank = max(32.0, float(plot_df["final_rank_raw"].max()) if plot_df["final_rank_raw"].notna().any() else 32.0)
            y_axis = alt.Y(
                "line_rank_plot:Q",
                title="Final Rank",
                scale=alt.Scale(domain=[1, 33.5], domainMin=1, domainMax=33.5, reverse=True, nice=False),
                axis=alt.Axis(values=[1, 4, 8, 16, 32]),
            )
            zone_bands = [
                {"y0": 1, "y1": 3, "zone_color": "#2ca02c"},
                {"y0": 4, "y1": 8, "zone_color": "#f1c40f"},
                {"y0": 9, "y1": 16, "zone_color": "#e67e22"},
                {"y0": 17, "y1": 32, "zone_color": "#e74c3c"},
            ]
            zone_layers = []
            for z in zone_bands:
                zdf = pd.DataFrame([z])
                zlayer = alt.Chart(zdf).mark_rect(color=z["zone_color"], opacity=0.12).encode(
                    y=alt.Y("y0:Q", scale=alt.Scale(domain=[1, 33.5], reverse=True, nice=False), title=None),
                    y2="y1:Q",
                )
                zone_layers.append(zlayer)

            if trend_chart_mode:
                box_y = alt.Y(
                    "final_rank_raw:Q",
                    title="Final Rank",
                    scale=alt.Scale(domain=[1, max_rank], reverse=True, nice=False),
                    axis=alt.Axis(values=[1, 4, 8, 16, 32] + ([int(max_rank)] if max_rank > 32 else [])),
                )
                box_base = alt.Chart(plot_df)
                box = box_base.mark_boxplot(extent="min-max").encode(
                    x=alt.X("rider_short:N", title="Rider"),
                    y=box_y,
                    color=alt.Color("rider_short:N", title="Rider", legend=None),
                    tooltip=[
                        alt.Tooltip("rider_short:N", title="Rider"),
                        alt.Tooltip("final_rank_raw:Q", title="Final Rank"),
                        alt.Tooltip("event_label:N", title="Event label"),
                        alt.Tooltip("event_dt:T", title="Date"),
                        alt.Tooltip("location:N", title="Location"),
                        alt.Tooltip("reached_phase:N", title="Phase"),
                    ],
                )
                points = box_base.mark_point(size=60, opacity=0.55, filled=True).encode(
                    x=alt.X("rider_short:N", title="Rider"),
                    y=box_y,
                    color=alt.Color("rider_short:N", title="Rider"),
                    tooltip=[
                        alt.Tooltip("rider_short:N", title="Rider"),
                        alt.Tooltip("final_rank_raw:Q", title="Final Rank"),
                        alt.Tooltip("event_label:N", title="Event label"),
                        alt.Tooltip("event_dt:T", title="Date"),
                        alt.Tooltip("location:N", title="Location"),
                        alt.Tooltip("reached_phase:N", title="Phase"),
                    ],
                )
                trend_chart = alt.layer(box, points).properties(height=460)
                st.altair_chart(trend_chart, use_container_width=True)
            else:
                line_df = plot_df.dropna(subset=["line_rank_plot", "x_index"]).copy()
                line_df = line_df.sort_values(["rider_short", "x_order"])
                line_df["x_plot"] = line_df["x_index"]
                overflow_df = plot_df[plot_df["is_overflow"]].copy()
                overflow_df = overflow_df.sort_values(["x_order", "final_rank_raw", "rider_short"])
                if not overflow_df.empty:
                    overflow_df["overflow_pos"] = overflow_df.groupby("x_order").cumcount().astype(float)
                    overflow_df["overflow_n"] = overflow_df.groupby("x_order")["rider_short"].transform("size").astype(float)
                    overflow_df["overflow_offset"] = (overflow_df["overflow_pos"] - (overflow_df["overflow_n"] - 1.0) / 2.0) * 0.08
                    overflow_df["x_plot"] = overflow_df["x_index"] + overflow_df["overflow_offset"]
                else:
                    overflow_df["x_plot"] = overflow_df["x_index"]

                axis_labels_json = json.dumps(x_order, ensure_ascii=False)
                x_axis = alt.X(
                    "x_plot:Q",
                    title="Event",
                    scale=alt.Scale(domain=[-0.5, max(len(x_order) - 0.5, 0.5)]),
                    axis=alt.Axis(
                        values=list(range(len(x_order))),
                        labelExpr=f"{axis_labels_json}[datum.value]",
                        labelAngle=-55,
                        labelLimit=280,
                        labelOverlap=False,
                    ),
                )

                base_line = alt.Chart(line_df).encode(
                    x=x_axis,
                    y=y_axis,
                    color=alt.Color("rider_short:N", title="Rider"),
                    detail="rider_short:N",
                    order=alt.Order("x_order:Q", sort="ascending"),
                    tooltip=[
                        alt.Tooltip("event_label:N", title="Event label"),
                        alt.Tooltip("event_dt:T", title="Date"),
                        alt.Tooltip("location:N", title="Location"),
                        alt.Tooltip("rider_short:N", title="Rider"),
                        alt.Tooltip("reached_phase:N", title="Phase"),
                        alt.Tooltip("final_rank_raw:Q", title="Final Rank"),
                        alt.Tooltip("overflow_clamped:N", title="Overflow clamped"),
                    ],
                )

                line = base_line.mark_line()
                points = base_line.transform_filter("datum.is_overflow == false").mark_point(size=65)

                overflow_base = alt.Chart(overflow_df).encode(
                    x=x_axis,
                    y=alt.Y(
                        "final_rank_plot:Q",
                        scale=alt.Scale(domain=[1, 33.5], domainMin=1, domainMax=33.5, reverse=True, nice=False),
                        title="Final Rank",
                    ),
                    color=alt.Color("rider_short:N", title="Rider"),
                    detail="rider_short:N",
                    tooltip=[
                        alt.Tooltip("event_label:N", title="Event label"),
                        alt.Tooltip("event_dt:T", title="Date"),
                        alt.Tooltip("location:N", title="Location"),
                        alt.Tooltip("rider_short:N", title="Rider"),
                        alt.Tooltip("reached_phase:N", title="Phase"),
                        alt.Tooltip("final_rank_raw:Q", title="Final Rank"),
                        alt.Tooltip("overflow_clamped:N", title="Overflow clamped"),
                    ],
                )
                overflow_points = overflow_base.mark_point(shape="triangle-up", size=45, opacity=0.95)
                layers = [*zone_layers, line, points, overflow_points]
                if show_dnq_labels:
                    over32_text = (
                        overflow_base.mark_text(dy=-8, fontSize=10)
                        .encode(text="final_rank_over32_label:N")
                    )
                    layers.append(over32_text)
                trend_chart = alt.layer(*layers).properties(
                    height=460, padding={"bottom": 110, "left": 5, "right": 5, "top": 10}
                )
                st.altair_chart(trend_chart, use_container_width=True)

        st.markdown("**Final Rank pro Event (master_results)**")
        final_rank_tbl = rider_event.copy()
        final_rank_tbl["event_id_dt"] = pd.to_datetime(
            final_rank_tbl["event_id"].astype(str).str[:8], format="%Y%m%d", errors="coerce"
        )
        final_rank_tbl["event_dt_sort"] = pd.to_datetime(final_rank_tbl["event_dt"], errors="coerce")
        final_rank_tbl["event_sort"] = final_rank_tbl["event_dt_sort"].where(final_rank_tbl["event_dt_sort"].notna(), final_rank_tbl["event_id_dt"])
        final_rank_tbl = final_rank_tbl.sort_values(
            ["event_sort", "rider_short"],
            ascending=[True, True],
            na_position="last",
        )
        final_rank_tbl["Date"] = final_rank_tbl["event_sort"].dt.strftime("%Y-%m-%d")
        final_rank_tbl["Final Rank"] = pd.to_numeric(final_rank_tbl["final_rank"], errors="coerce")
        final_rank_tbl["Final Rank"] = np.where(
            final_rank_tbl["Final Rank"].notna(),
            final_rank_tbl["Final Rank"].astype("Int64").astype(str),
            "NA",
        )
        final_rank_tbl["Event Label"] = final_rank_tbl["event_label_full"].fillna(final_rank_tbl["event_short"]).fillna("Unknown")
        final_rank_tbl = final_rank_tbl.rename(
            columns={
                "location": "Location",
                "rider_short": "Rider",
            }
        )
        st.dataframe(
            final_rank_tbl[["Date", "Event Label", "Location", "Rider", "Final Rank"]],
            use_container_width=True,
            hide_index=True,
        )

        # Requested summary metrics.
        summary = (
            rider_event.groupby("rider_short", as_index=False)
            .agg(
                n_events=("event_id", "nunique"),
                avg_final_rank=("final_rank", "mean"),
                median_final_rank=("final_rank", "median"),
                best_final_rank=("final_rank", "min"),
                worst_final_rank=("final_rank", "max"),
                top16_count=("final_rank", lambda s: int((pd.to_numeric(s, errors="coerce") <= 16).sum())),
                top8_count=("final_rank", lambda s: int((pd.to_numeric(s, errors="coerce") <= 8).sum())),
                top3_count=("final_rank", lambda s: int((pd.to_numeric(s, errors="coerce") <= 3).sum())),
                variability_final_rank=("final_rank", lambda s: (pd.to_numeric(s, errors="coerce") - pd.to_numeric(s, errors="coerce").median()).abs().median()),
            )
            .sort_values("rider_short", ascending=True, na_position="last")
        )
        st.dataframe(summary.round(3), use_container_width=True, hide_index=True)

st.caption(
    f"Alle Deltas verwenden die aktive Referenz: {ref_caption}."
)
