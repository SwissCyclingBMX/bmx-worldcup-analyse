import sqlite3
import unicodedata
from typing import Any, Dict, List, Optional
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
from access_control import render_sidebar_nav, require_page_access
from ui_prefs import load_page_prefs, update_page_prefs
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

alt.data_transformers.disable_max_rows()


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(APP_DIR), "bmx.db")

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


def infer_event_type(event_id: str, display_name: str = "") -> str:
    raw = str(event_id or "").strip()
    display = str(display_name or "").lower().strip()
    if raw.upper() in {"WC", "WM", "EC", "EM", "USABMX", "FFC", "SCC", "OTHER"}:
        return "Other" if raw.upper() == "OTHER" else raw.upper()
    if any(token in display for token in ["bundesliga", "championnat", "training"]) or display == "tmp":
        return "Other"
    if any(token in display for token in ["winterthur", " scc ", "scc -", "- scc"]):
        return "SCC"
    if any(token in display for token in ["lone star", "usa bmx", "pro championship", "day 1", "day 2", "day 3"]):
        return "USABMX"
    if "european bmx cup" in display or "european cup" in display:
        return "EC"
    if "european championship" in display or "european championships" in display:
        return "EM"
    if "world championship" in display or "world championships" in display:
        return "WM"
    e = raw.lower()
    if "_usap_" in e or "_usabmx_" in e:
        return "USABMX"
    if "_ffc_" in e:
        return "FFC"
    if "_scc_" in e:
        return "SCC"
    if "_other_" in e or "_sqorz_" in e or "tmp" in e:
        return "Other"
    if "_euc_" in e or "_uec_" in e:
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


def parse_time_to_seconds(val) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return float("nan")
    s = str(val).strip()
    if not s or s in {"-", "None", "nan"}:
        return float("nan")
    try:
        td = pd.to_timedelta(s)
        return td.total_seconds()
    except Exception:
        try:
            return float(s)
        except Exception:
            return float("nan")


def format_seconds_3(v) -> str:
    x = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
    if pd.isna(x):
        return ""
    return f"{float(x):.3f}"


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


@st.cache_data(show_spinner=False, ttl=30)
def load_runs(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            """
            SELECT
              p.event_id, p.group_id, p.round_key, p.round_title, p.heat_id, p.heat_title,
              p.name, p.nation, p.uci_id, p.rank,
              p.start, p.t1, p.t2, p.t3, p.time,
              e.display_name, e.location, e.country, e.event_date, e.event_type
            FROM picks p
            LEFT JOIN events e ON e.event_id = p.event_id
            """,
            conn,
        )
    except Exception:
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
        df["event_type"] = ""
    conn.close()

    if df.empty:
        return df

    for c in ["start", "t1", "t2", "t3", "time"]:
        df[c] = df[c].map(parse_time_to_seconds)
        df.loc[df[c] <= 0, c] = np.nan
    for c in ["rank", "heat_id", "round_key"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["finish"] = df["time"]
    inferred_event_type = [
        infer_event_type(eid, dn)
        for eid, dn in zip(df["event_id"], df["display_name"])
    ]
    df["event_type"] = df["event_type"].where(
        df["event_type"].notna() & (df["event_type"].astype(str).str.strip() != ""),
        inferred_event_type,
    )
    df["event_type"] = [infer_event_type(et, dn) for et, dn in zip(df["event_type"], df["display_name"])]
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


@st.cache_data(show_spinner=False, ttl=60)
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
    invalid_bottom_t1 = (
        out["start"].notna()
        & out["t1"].notna()
        & (out["t1"] < out["start"])
    )
    invalid_t1_t2 = (
        out["t1"].notna()
        & out["t2"].notna()
        & (out["t2"] < out["t1"])
    )
    invalid_t2_t3 = (
        out["t2"].notna()
        & out["t3"].notna()
        & (out["t3"] < out["t2"])
    )
    invalid_t3_finish = (
        out["t3"].notna()
        & out["finish"].notna()
        & (out["finish"] < out["t3"])
    )
    out.loc[invalid_bottom_t1, "split_bottom_t1"] = np.nan
    out.loc[invalid_t1_t2, "split_t1_t2"] = np.nan
    out.loc[invalid_t2_t3, "split_t2_t3"] = np.nan
    out.loc[invalid_t3_finish, "split_t3_finish"] = np.nan
    for split_col in ["split_bottom_t1", "split_t1_t2", "split_t2_t3", "split_t3_finish"]:
        out.loc[out[split_col] <= 0, split_col] = np.nan
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


def build_second_best_reference(
    df: pd.DataFrame,
    *,
    time_col: str = "segment_time",
    group_cols: Optional[List[str]] = None,
    rider_col: Optional[str] = None,
    value_name: str = "rank2_ref_event",
) -> pd.DataFrame:
    if group_cols is None:
        group_cols = ["event_id", "group_id"]
    if df.empty or time_col not in df.columns:
        return pd.DataFrame(columns=group_cols + [value_name])

    work = df.copy()
    work[time_col] = pd.to_numeric(work[time_col], errors="coerce")
    work = work.dropna(subset=[time_col]).copy()
    if rider_col is not None:
        if rider_col not in work.columns:
            return pd.DataFrame(columns=group_cols + [value_name])
        work = (
            work.dropna(subset=[rider_col])
            .groupby(group_cols + [rider_col], dropna=False, as_index=False)[time_col]
            .min()
        )
    if work.empty:
        return pd.DataFrame(columns=group_cols + [value_name])

    return (
        work.groupby(group_cols, dropna=False)[time_col]
        .apply(lambda s: s.nsmallest(2).iloc[-1] if s.notna().sum() >= 2 else np.nan)
        .rename(value_name)
        .reset_index()
    )


def apply_event_top1_adjustment(
    target_df: pd.DataFrame,
    reference_df: pd.DataFrame,
    *,
    time_col: str = "segment_time",
    reference_time_col: str = "reference_time",
    delta_col: str = "delta_display",
    group_cols: Optional[List[str]] = None,
    rider_col: str = "rider_id",
    use_rider_best: bool = False,
    ref_display_col: Optional[str] = None,
) -> pd.DataFrame:
    if group_cols is None:
        group_cols = ["event_id", "group_id"]
    if target_df.empty:
        return target_df

    second_ref = build_second_best_reference(
        reference_df,
        time_col=time_col,
        group_cols=group_cols,
        rider_col=(rider_col if use_rider_best else None),
        value_name="rank2_ref_event",
    )
    out = target_df.drop(columns=["rank2_ref_event"], errors="ignore").merge(second_ref, on=group_cols, how="left")
    is_best_row = (
        out[time_col].notna()
        & out[reference_time_col].notna()
        & np.isclose(out[time_col], out[reference_time_col], atol=1e-6)
    )
    use_rank2 = is_best_row & out["rank2_ref_event"].notna()
    if ref_display_col:
        out.loc[use_rank2, ref_display_col] = out.loc[use_rank2, "rank2_ref_event"]
    out.loc[use_rank2, delta_col] = out.loc[use_rank2, time_col] - out.loc[use_rank2, "rank2_ref_event"]
    return out


def get_plotly_selected_point_ids(event, point_id_idx: int = -1) -> List[str]:
    if event is None:
        return []
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection")
    if selection is None:
        return []
    points = getattr(selection, "points", None)
    if points is None and isinstance(selection, dict):
        points = selection.get("points")
    if not points:
        return []
    out: List[str] = []
    for point in points:
        customdata = None
        if isinstance(point, dict):
            customdata = point.get("customdata")
        else:
            customdata = getattr(point, "customdata", None)
        if customdata is None:
            continue
        try:
            point_id = customdata[point_id_idx]
        except Exception:
            continue
        if point_id is None:
            continue
        point_id = str(point_id)
        if point_id and point_id not in out:
            out.append(point_id)
    return out


def sync_exclusion_state(state_key: str, view_signature: str) -> dict:
    state = st.session_state.get(state_key)
    if not isinstance(state, dict) or state.get("view_signature") != view_signature:
        state = {
            "view_signature": view_signature,
            "excluded_ids": [],
            "undo_stack": [],
        }
        st.session_state[state_key] = state
    else:
        state.setdefault("excluded_ids", [])
        state.setdefault("undo_stack", [])
    return state


def excluded_id_set(state: dict) -> set[str]:
    return {str(x) for x in state.get("excluded_ids", []) if str(x)}


def apply_point_exclusions(df: pd.DataFrame, state: dict, point_col: str = "point_id") -> pd.DataFrame:
    if df.empty or point_col not in df.columns:
        return df
    excluded = excluded_id_set(state)
    if not excluded:
        return df
    return df[~df[point_col].astype(str).isin(excluded)].copy()


def build_view_signature(name: str, payload: dict) -> str:
    return json.dumps({"name": name, "payload": payload}, sort_keys=True, default=str)


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
    # Preserve the filtered frame's index; otherwise pandas aligns the default
    # RangeIndex from pd.Series(ranks) onto scattered row indices and mixes
    # final-event ranks between riders in multi-athlete views.
    out["final_rank_event"] = pd.to_numeric(pd.Series(ranks, index=out.index), errors="coerce")
    out["final_rank_event_display"] = np.where(
        out["final_rank_event"].notna(), out["final_rank_event"].astype("Int64").astype(str), "NA"
    )
    return out


def ensure_training_alias_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS training_name_aliases (
          source_name_key TEXT NOT NULL,
          source_name TEXT NOT NULL,
          source_nation TEXT NOT NULL DEFAULT '',
          source_bib INTEGER,
          target_rider_id TEXT NOT NULL,
          target_uci_id TEXT,
          target_name TEXT NOT NULL,
          target_nation TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (source_name_key, source_nation, source_bib)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_training_alias_key ON training_name_aliases(source_name_key)")


def _select_col(existing: set[str], table_alias: str, col: str, alias: Optional[str] = None) -> str:
    out = alias or col
    if col in existing:
        return f"{table_alias}.{col} AS {out}"
    return f"NULL AS {out}"


def save_training_alias(source_name: str, source_nation: str, source_bib: Any, target: Dict[str, Any]) -> None:
    source_name = clean_spaces(source_name)
    source_nation = clean_spaces(source_nation).upper()
    bib_val = pd.to_numeric(pd.Series([source_bib]), errors="coerce").iloc[0]
    source_bib_int = int(bib_val) if pd.notna(bib_val) else None
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_training_alias_table(conn)
        if source_bib_int is None:
            conn.execute(
                """
                DELETE FROM training_name_aliases
                WHERE source_name_key=? AND source_nation=? AND source_bib IS NULL
                """,
                (norm_name_key(source_name), source_nation),
            )
        conn.execute(
            """
            INSERT INTO training_name_aliases (
              source_name_key, source_name, source_nation, source_bib,
              target_rider_id, target_uci_id, target_name, target_nation,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name_key, source_nation, source_bib) DO UPDATE SET
              source_name=excluded.source_name,
              target_rider_id=excluded.target_rider_id,
              target_uci_id=excluded.target_uci_id,
              target_name=excluded.target_name,
              target_nation=excluded.target_nation,
              updated_at=excluded.updated_at
            """,
            (
                norm_name_key(source_name),
                source_name,
                source_nation,
                source_bib_int,
                str(target.get("rider_id") or ""),
                str(target.get("uci_norm_stitched") or target.get("uci_norm") or ""),
                clean_spaces(str(target.get("name_pretty") or target.get("name_clean") or "")),
                clean_spaces(str(target.get("nation") or "")).upper(),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_training_athlete_targets(all_runs: pd.DataFrame, master_results: pd.DataFrame) -> pd.DataFrame:
    cols = ["rider_id", "rider_label", "rider_short", "name_pretty", "name_clean", "nation", "uci_norm_stitched", "uci_norm"]
    frames = []
    if not all_runs.empty:
        frames.append(all_runs[[c for c in cols if c in all_runs.columns]].copy())
    if not master_results.empty:
        mr = master_results.copy()
        mr["name_clean"] = (mr["first_name"].fillna("").astype(str) + " " + mr["last_name"].fillna("").astype(str)).apply(clean_spaces)
        mr["name_pretty"] = mr["name_clean"].apply(pretty_name)
        mr["nation"] = ""
        mr["uci_norm_stitched"] = mr["uci_norm"] if "uci_norm" in mr.columns else mr["uci_id"].apply(norm_uci_id)
        mr["rider_id"] = np.where(mr["uci_norm_stitched"] != "", "uci:" + mr["uci_norm_stitched"], "name:" + mr["name_clean"].apply(norm_name_key))
        mr["rider_label"] = mr["name_pretty"]
        mr["rider_short"] = mr["name_pretty"].apply(short_name)
        mr["uci_norm"] = mr["uci_norm_stitched"]
        frames.append(mr[cols].copy())
    if not frames:
        return pd.DataFrame(columns=cols + ["target_label"])
    out = pd.concat(frames, ignore_index=True, sort=False)
    for c in cols:
        if c not in out.columns:
            out[c] = ""
    out = out.fillna("")
    out["target_label"] = out["rider_label"].where(out["rider_label"].astype(str).str.strip() != "", out["name_pretty"])
    return out.sort_values(["target_label", "rider_id"], kind="stable").drop_duplicates(subset=["rider_id"])


def _parse_training_clock(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?", text)
        if m:
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return ""


def _parse_training_day(event_id: Any, event_date: Any, *values: Any) -> pd.Timestamp:
    event_id_dt = pd.to_datetime(str(event_id or "")[:8], format="%Y%m%d", errors="coerce")
    year = int(event_id_dt.year) if pd.notna(event_id_dt) else datetime.now().year
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.", text)
        if m:
            dt = pd.to_datetime(f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}", errors="coerce")
            if pd.notna(dt):
                return dt.normalize()
    return parse_event_date(str(event_date or ""), str(event_id or "")).normalize()


def derive_training_datetime(row: pd.Series) -> pd.Timestamp:
    label = str(row.get("training_block_label") or "").strip()
    block_time = str(row.get("training_block_time") or "").strip()
    gate = str(row.get("gate") or "").strip()
    source_file = str(row.get("source_file") or "").strip()
    ingested_at = pd.to_datetime(row.get("ingested_at"), errors="coerce")
    day = _parse_training_day(row.get("event_id"), row.get("event_date"), label, gate, block_time, source_file)
    clock = _parse_training_clock(block_time, label, gate, source_file)
    if not clock and pd.notna(ingested_at):
        clock = ingested_at.strftime("%H:%M:%S")
    if pd.notna(day) and clock:
        dt = pd.to_datetime(f"{day.strftime('%Y-%m-%d')} {clock}", errors="coerce")
        if pd.notna(dt):
            return dt
    if pd.notna(ingested_at):
        return ingested_at
    return day


def add_training_sessions(df: pd.DataFrame, gap_minutes: int = 120) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        out["session_id"] = ""
        out["session_label"] = ""
        return out
    out["session_group_key"] = out["training_location"].fillna("").astype(str) + "|" + out["source_file"].fillna("").astype(str)
    out = out.sort_values(["session_group_key", "training_datetime", "source_name", "bib"], na_position="last", kind="stable")
    gap = pd.Timedelta(minutes=gap_minutes)
    session_nums = pd.Series(index=out.index, dtype="Int64")
    for _, grp in out.groupby("session_group_key", dropna=False, sort=False):
        prev_dt = None
        session_num = 0
        for idx, dt_val in grp["training_datetime"].items():
            if prev_dt is None or pd.isna(prev_dt) or pd.isna(dt_val) or (dt_val - prev_dt) > gap:
                session_num += 1
            session_nums.loc[idx] = session_num
            prev_dt = dt_val
    out["session_num"] = session_nums.astype("Int64")
    out["session_day"] = pd.to_datetime(out["training_datetime"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("Unknown")
    out["session_id"] = out["session_group_key"] + "|" + out["session_day"] + "|" + out["session_num"].astype(str)
    meta = (
        out.groupby("session_id", as_index=False, dropna=False)
        .agg(session_start=("training_datetime", "min"), training_location=("training_location", "first"))
    )
    meta["session_label"] = (
        pd.to_datetime(meta["session_start"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M").fillna("Unknown")
        + " | "
        + meta["training_location"].fillna("Unknown").astype(str)
    )
    return out.merge(meta[["session_id", "session_start", "session_label"]], on="session_id", how="left")


@st.cache_data(show_spinner=False, ttl=300)
def load_training_locations(db_path: str = DB_PATH, db_mtime: float = 0.0) -> List[str]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        loc_expr = _select_col(event_cols, "e", "location", "event_location")
        display_expr = _select_col(event_cols, "e", "display_name")
        df = pd.read_sql_query(
            f"""
            SELECT DISTINCT {loc_expr}, {display_expr}
            FROM training_times t
            LEFT JOIN events e ON e.event_id = t.event_id
            """,
            conn,
        )
    except Exception:
        return []
    finally:
        conn.close()
    if df.empty:
        return []
    for c in ["event_location", "display_name"]:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str)
    loc = df["event_location"].apply(wc_location_clean)
    loc = loc.where(loc.astype(str).str.strip() != "", df["display_name"].astype(str).str.strip())
    loc = loc.where(loc.astype(str).str.strip() != "", "Unknown")
    return sorted([x for x in loc.dropna().astype(str).unique().tolist() if x])


@st.cache_data(show_spinner=False, ttl=300)
def load_training_data(
    db_path: str = DB_PATH,
    db_mtime: float = 0.0,
    location_filter: tuple[str, ...] = (),
) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    conn = sqlite3.connect(db_path)
    try:
        ensure_training_alias_table(conn)
        train_cols = {row[1] for row in conn.execute("PRAGMA table_info(training_times)").fetchall()}
        if not train_cols:
            return pd.DataFrame()
        event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        train_select = [
            _select_col(train_cols, "t", "event_id"),
            _select_col(train_cols, "t", "category"),
            _select_col(train_cols, "t", "bib"),
            _select_col(train_cols, "t", "name", "source_name"),
            _select_col(train_cols, "t", "nation", "source_nation"),
            _select_col(train_cols, "t", "gate"),
            _select_col(train_cols, "t", "source_file"),
            _select_col(train_cols, "t", "ingested_at"),
        ] + [
            _select_col(train_cols, "t", c)
            for c in [
                "kink", "bottom", "interim", "t1_in", "start", "t1", "total",
                "split_count", "split_cumulative", "split_deltas",
                "training_block_id", "training_block_label", "training_block_time", "source_kind",
            ]
        ]
        event_select = [
            _select_col(event_cols, "e", "display_name"),
            _select_col(event_cols, "e", "location", "event_location"),
            _select_col(event_cols, "e", "country", "event_country"),
            _select_col(event_cols, "e", "event_date"),
            _select_col(event_cols, "e", "event_type"),
        ]
        where_sql = ""
        params: List[Any] = []
        if location_filter:
            event_id_rows = conn.execute(
                "SELECT event_id, display_name, location FROM events"
            ).fetchall()
            event_ids = []
            wanted_locations = set(location_filter)
            for event_id, display_name, location in event_id_rows:
                loc = wc_location_clean(location or "")
                if not loc:
                    loc = str(display_name or "").strip() or "Unknown"
                if loc in wanted_locations:
                    event_ids.append(str(event_id))
            if not event_ids:
                return pd.DataFrame()
            placeholders = ",".join("?" for _ in event_ids)
            where_sql = f"WHERE t.event_id IN ({placeholders})"
            params = event_ids
        df = pd.read_sql_query(
            f"""
            SELECT {", ".join(train_select + event_select)}
            FROM training_times t
            LEFT JOIN events e ON e.event_id = t.event_id
            {where_sql}
            """,
            conn,
            params=params,
        )
        aliases = pd.read_sql_query("SELECT * FROM training_name_aliases", conn)
        conn.commit()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()
    if df.empty:
        return df
    for c in [
        "category", "source_name", "source_nation", "gate", "source_file", "ingested_at",
        "training_block_id", "training_block_label", "training_block_time", "source_kind",
        "display_name", "event_location", "event_country", "event_date", "event_type",
    ]:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str)
    df["source_name"] = df["source_name"].apply(clean_spaces)
    df["source_name_key"] = df["source_name"].apply(norm_name_key)
    df["source_nation"] = df["source_nation"].fillna("").astype(str).str.upper().str.strip()
    df["bib"] = pd.to_numeric(df["bib"], errors="coerce").astype("Int64")
    for col in ["kink", "bottom", "interim", "t1_in", "start", "t1", "total"]:
        if col not in df.columns:
            df[col] = ""
        df[f"{col}_s"] = df[col].apply(parse_time_to_seconds)
        df.loc[df[f"{col}_s"] <= 0, f"{col}_s"] = np.nan
    for split_base in ["split_cumulative", "split_deltas"]:
        if split_base not in df.columns:
            df[split_base] = ""
        split_lists = df[split_base].fillna("").astype(str).str.split(",")
        max_splits = min(12, max((len([x for x in vals if str(x).strip()]) for vals in split_lists), default=0))
        for idx in range(max_splits):
            col = f"{split_base}_{idx + 1}_s"
            df[col] = split_lists.apply(lambda vals, i=idx: parse_time_to_seconds(vals[i]) if i < len(vals) else np.nan)
            df.loc[df[col] <= 0, col] = np.nan
    df["training_datetime"] = pd.to_datetime(df.apply(derive_training_datetime, axis=1), errors="coerce")
    df["training_location"] = df["event_location"].apply(wc_location_clean)
    df.loc[df["training_location"].astype(str).str.strip() == "", "training_location"] = df["display_name"].fillna("").astype(str).str.strip()
    df.loc[df["training_location"].astype(str).str.strip() == "", "training_location"] = "Unknown"
    df["source_kind"] = df["source_kind"].where(df["source_kind"].str.strip() != "", df["source_file"].str.extract(r"([^/?]+)", expand=False).fillna(""))

    for c in ["target_rider_id", "target_uci_id", "target_name", "target_nation"]:
        df[c] = ""
    if not aliases.empty:
        aliases = aliases.copy()
        aliases["source_name_key"] = aliases["source_name_key"].fillna("").astype(str)
        aliases["source_nation"] = aliases["source_nation"].fillna("").astype(str).str.upper().str.strip()
        aliases["source_bib"] = pd.to_numeric(aliases["source_bib"], errors="coerce").astype("Int64")
        specific = aliases.dropna(subset=["source_bib"]).rename(columns={"source_bib": "bib"})
        df = df.merge(
            specific[["source_name_key", "source_nation", "bib", "target_rider_id", "target_uci_id", "target_name", "target_nation"]],
            on=["source_name_key", "source_nation", "bib"],
            how="left",
            suffixes=("", "_specific"),
        )
        for c in ["target_rider_id", "target_uci_id", "target_name", "target_nation"]:
            specific_col = f"{c}_specific"
            if specific_col in df.columns:
                df[c] = df[specific_col].where(df[specific_col].fillna("").astype(str).str.strip() != "", df[c])
                df = df.drop(columns=[specific_col])
        broad = (
            aliases[aliases["source_bib"].isna()]
            .sort_values(["source_name_key", "source_nation", "updated_at"], kind="stable")
            .drop_duplicates(subset=["source_name_key", "source_nation"], keep="last")
        )
        df = df.merge(
            broad[["source_name_key", "source_nation", "target_rider_id", "target_uci_id", "target_name", "target_nation"]].rename(
                columns={c: f"{c}_broad" for c in ["target_rider_id", "target_uci_id", "target_name", "target_nation"]}
            ),
            on=["source_name_key", "source_nation"],
            how="left",
        )
        for c in ["target_rider_id", "target_uci_id", "target_name", "target_nation"]:
            broad_col = f"{c}_broad"
            df[c] = df[c].where(df[c].fillna("").astype(str).str.strip() != "", df[broad_col])
            df = df.drop(columns=[broad_col], errors="ignore")

    df["target_name"] = df["target_name"].fillna("").astype(str).apply(clean_spaces)
    df["target_nation"] = df["target_nation"].fillna("").astype(str).str.upper().str.strip()
    df["mapped"] = df["target_rider_id"].fillna("").astype(str).str.strip() != ""
    df["name_clean"] = df["target_name"].where(df["mapped"], df["source_name"])
    df["name_pretty"] = df["name_clean"].apply(pretty_name)
    df["nation"] = df["target_nation"].where(df["mapped"] & (df["target_nation"] != ""), df["source_nation"])
    df["name_key"] = df["name_clean"].apply(norm_name_key)
    df["rider_id"] = df["target_rider_id"].where(
        df["mapped"],
        "training:" + df["source_name_key"] + "|" + df["source_nation"].fillna("").astype(str),
    )
    df["rider_label"] = df["name_pretty"] + np.where(df["nation"].astype(str).str.strip() != "", " (" + df["nation"] + ")", "")
    df["rider_short"] = df["name_pretty"].apply(short_name)
    return add_training_sessions(df, gap_minutes=120)


def flag_training_metric_outliers(
    df_train: pd.DataFrame,
    metric_col: str,
    category_col: str = "training_location",
    athlete_col: str = "rider_id",
    absolute_lower: Optional[float] = None,
    absolute_upper: Optional[float] = None,
) -> pd.DataFrame:
    if df_train.empty or metric_col not in df_train.columns:
        out = df_train.copy()
        out["measurement_flagged"] = False
        out["measurement_flag_reason"] = ""
        return out

    df_flagged = df_train.copy()
    metric_all = pd.to_numeric(df_flagged[metric_col], errors="coerce")
    df_flagged["measurement_flagged"] = metric_all.notna() & (metric_all <= 0)
    df_flagged["measurement_flag_reason"] = np.where(df_flagged["measurement_flagged"], "non_positive", "")

    if absolute_lower is not None:
        too_low_abs = metric_all.notna() & (metric_all < float(absolute_lower))
        df_flagged.loc[too_low_abs, "measurement_flagged"] = True
        df_flagged.loc[too_low_abs, "measurement_flag_reason"] = df_flagged.loc[too_low_abs, "measurement_flag_reason"].replace("", "absolute_low")
    if absolute_upper is not None:
        too_high_abs = metric_all.notna() & (metric_all > float(absolute_upper))
        df_flagged.loc[too_high_abs, "measurement_flagged"] = True
        df_flagged.loc[too_high_abs, "measurement_flag_reason"] = df_flagged.loc[too_high_abs, "measurement_flag_reason"].replace("", "absolute_high")

    if category_col in df_flagged.columns:
        for _, grp in df_flagged.groupby(category_col, dropna=False):
            metric_vals = pd.to_numeric(grp[metric_col], errors="coerce")
            metric_vals = metric_vals[np.isfinite(metric_vals) & (metric_vals > 0)]
            unique_vals = np.sort(metric_vals.unique())
            if unique_vals.size < 5:
                continue

            q10, q25, q50, q75 = np.percentile(unique_vals, [10, 25, 50, 75])
            iqr = max(float(q75 - q25), 0.001)
            dense_gap = max(0.02, min(0.08, 0.75 * max(float(q50 - q25), 0.01)))
            lower_bound = float(q10 - max(0.12, 1.5 * iqr, 0.06 * max(float(q10), 1.0)))

            cluster_start = None
            for i in range(max(0, unique_vals.size - 3)):
                window = unique_vals[i : i + 4]
                if window.size < 3:
                    break
                gaps = np.diff(window)
                if gaps.size >= 2 and float(np.max(gaps)) <= dense_gap:
                    cluster_start = float(window[0])
                    break
            if cluster_start is not None and cluster_start <= float(q50):
                lower_bound = max(lower_bound, cluster_start)

            too_fast = pd.to_numeric(grp[metric_col], errors="coerce") < lower_bound
            flagged_idx = grp.index[too_fast.fillna(False)]
            df_flagged.loc[flagged_idx, "measurement_flagged"] = True
            df_flagged.loc[
                flagged_idx,
                "measurement_flag_reason",
            ] = df_flagged.loc[flagged_idx, "measurement_flag_reason"].replace("", "track_fast")

    if athlete_col in df_flagged.columns:
        for _, grp in df_flagged.groupby(athlete_col, dropna=False):
            metric_vals = pd.to_numeric(grp[metric_col], errors="coerce")
            metric_vals = metric_vals[np.isfinite(metric_vals) & (metric_vals > 0)]
            unique_vals = np.sort(metric_vals.unique())
            if unique_vals.size < 5:
                continue
            aq25, aq50, aq75 = np.percentile(unique_vals, [25, 50, 75])
            aiqr = max(float(aq75 - aq25), 0.001)
            lower_bound = float(aq25 - max(0.08, 1.5 * aiqr, 0.04 * max(float(aq50), 1.0)))
            upper_bound = float(aq75 + max(0.12, 1.5 * aiqr, 0.06 * max(float(aq50), 1.0)))
            grp_metric = pd.to_numeric(grp[metric_col], errors="coerce")
            fast_idx = grp.index[(grp_metric < lower_bound).fillna(False)]
            slow_idx = grp.index[(grp_metric > upper_bound).fillna(False)]
            df_flagged.loc[fast_idx, "measurement_flagged"] = True
            df_flagged.loc[slow_idx, "measurement_flagged"] = True
            df_flagged.loc[
                fast_idx,
                "measurement_flag_reason",
            ] = df_flagged.loc[fast_idx, "measurement_flag_reason"].replace("", "athlete_fast")
            df_flagged.loc[
                slow_idx,
                "measurement_flag_reason",
            ] = df_flagged.loc[slow_idx, "measurement_flag_reason"].replace("", "athlete_slow")

    return df_flagged


def render_training_insights(page_prefs: dict) -> None:
    st.subheader("Training")
    db_mtime = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0.0
    location_opts = load_training_locations(db_mtime=db_mtime)
    if not location_opts:
        st.info("Keine Trainingsdaten gefunden.")
        return

    saved_locations = [x for x in page_prefs.get("training_locations", []) if x in location_opts]
    default_location = saved_locations[0] if len(saved_locations) == 1 else ""
    if not default_location and "Aigle" in location_opts:
        default_location = "Aigle"
    if not default_location:
        default_location = location_opts[0]
    default_location_index = location_opts.index(default_location) if default_location in location_opts else 0
    loc_col, date_col_1, date_col_2 = st.columns([2, 1, 1])
    with loc_col:
        selected_training_location = st.selectbox(
            "Strecke / Ort",
            location_opts,
            index=default_location_index,
            key="ai_training_location_single_v2",
        )

    if not selected_training_location:
        st.info("Bitte mindestens eine Strecke / einen Ort auswaehlen.")
        return
    sel_training_locations = [selected_training_location]

    df_train = load_training_data(
        db_mtime=db_mtime,
        location_filter=tuple(sel_training_locations),
    )
    if df_train.empty:
        st.info("Keine Trainingsdaten gefunden.")
        return

    metric_defs = {
        "Start to Kink": "kink_s",
        "Split Kink to Bottom": "bottom_s",
        "Interim": "interim_s",
        "Split first Straight Bottom to T1": "t1_in_s",
        "Start": "start_s",
        "T1": "t1_s",
        "Total": "total_s",
    }
    for idx in range(1, 13):
        cum_col = f"split_cumulative_{idx}_s"
        delta_col = f"split_deltas_{idx}_s"
        if cum_col in df_train.columns:
            metric_defs[f"BMX-Racer Split {idx}"] = cum_col
        if delta_col in df_train.columns:
            metric_defs[f"BMX-Racer Delta {idx}"] = delta_col
    metric_options = [label for label, col in metric_defs.items() if col in df_train.columns and df_train[col].notna().any()]
    if not metric_options:
        st.info("Keine verwertbaren Trainings-Splitzeiten vorhanden.")
        return

    min_dt = pd.to_datetime(df_train["training_datetime"], errors="coerce").min()
    max_dt = pd.to_datetime(df_train["training_datetime"], errors="coerce").max()
    if pd.isna(min_dt) or pd.isna(max_dt):
        st.info("Keine verwertbaren Trainings-Zeitstempel vorhanden.")
        return
    default_start = pd.to_datetime(page_prefs.get("training_start_date"), errors="coerce")
    default_end = pd.to_datetime(page_prefs.get("training_end_date"), errors="coerce")
    if pd.isna(default_start):
        default_start = max_dt - pd.Timedelta(days=60)
    if pd.isna(default_end):
        default_end = max_dt

    with date_col_1:
        start_date = st.date_input("Von", value=default_start.date(), format="DD/MM/YYYY", key="ai_training_start_date")
    with date_col_2:
        end_date = st.date_input("Bis", value=default_end.date(), format="DD/MM/YYYY", key="ai_training_end_date")

    scope = df_train.copy()
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    scope = scope[(scope["training_datetime"] >= start_ts) & (scope["training_datetime"] <= end_ts)].copy()
    if sel_training_locations:
        scope = scope[scope["training_location"].isin(sel_training_locations)].copy()

    c1, c2, c4 = st.columns(3)
    with c1:
        nation_opts = sorted([x for x in scope["nation"].dropna().astype(str).unique().tolist() if x])
        sel_train_nations = st.multiselect(
            "Nation (optional)",
            nation_opts,
            default=[x for x in page_prefs.get("training_nations", []) if x in nation_opts],
            key="ai_training_nations",
        )
    if sel_train_nations:
        scope = scope[scope["nation"].isin(sel_train_nations)].copy()
    with c2:
        rider_opts = sorted([x for x in scope["rider_label"].dropna().astype(str).unique().tolist() if x])
        sel_train_riders = st.multiselect(
            "Athlet (optional)",
            rider_opts,
            default=[x for x in page_prefs.get("training_riders", []) if x in rider_opts],
            key="ai_training_riders",
        )
    if sel_train_riders:
        scope = scope[scope["rider_label"].isin(sel_train_riders)].copy()
    with c4:
        selected_metric_labels = st.multiselect(
            "Split-Metriken",
            metric_options,
            default=[x for x in page_prefs.get("training_metric_labels", metric_options[:1]) if x in metric_options] or metric_options[:1],
            key="ai_training_metric_labels",
        )

    update_page_prefs("athlete_insights", {
        "ai_active_section": "Training",
        "training_start_date": str(start_date),
        "training_end_date": str(end_date),
        "training_locations": sel_training_locations,
        "training_nations": sel_train_nations,
        "training_riders": sel_train_riders,
        "training_metric_labels": selected_metric_labels,
    })

    if scope.empty:
        st.info("Keine Trainingsdaten fuer die aktuelle Auswahl.")
        return

    with st.expander("Timing-Namen zu DB-Athleten zuordnen", expanded=False):
        edit_aliases = st.checkbox("Zuordnungen bearbeiten", value=False, key="ai_training_alias_edit")
        unmapped = (
            scope[~scope["mapped"].fillna(False)]
            .groupby(["source_name_key", "source_name", "source_nation"], as_index=False, dropna=False)
            .agg(
                runs=("source_name", "size"),
                first_seen=("training_datetime", "min"),
                bibs=("bib", lambda s: ", ".join([str(int(x)) for x in pd.to_numeric(s, errors="coerce").dropna().unique()[:8]])),
            )
            .sort_values(["runs", "source_name"], ascending=[False, True], kind="stable")
        )
        if unmapped.empty:
            st.caption("Alle Timing-Namen in der aktuellen Auswahl sind zugeordnet oder verwenden bereits ihren Roh-Namen.")
        elif not edit_aliases:
            st.caption(f"{len(unmapped)} Timing-Namen ohne feste DB-Zuordnung. Zum Bearbeiten aktivieren.")
        else:
            all_runs = load_runs()
            master_results = load_master_results()
            targets = build_training_athlete_targets(all_runs, master_results)
            if targets.empty:
                st.warning("Keine bestehenden DB-Athleten fuer die Zuordnung gefunden.")
                targets = pd.DataFrame()
        if not unmapped.empty and edit_aliases and not targets.empty:
            unmapped["option_label"] = (
                unmapped["source_name"].astype(str)
                + np.where(unmapped["source_nation"].astype(str).str.strip() != "", " (" + unmapped["source_nation"].astype(str) + ")", "")
                + " | "
                + unmapped["runs"].astype(str)
                + " Laeufe"
            )
            source_label = st.selectbox("Timing-Name", unmapped["option_label"].tolist(), key="ai_training_alias_source")
            source_row = unmapped[unmapped["option_label"] == source_label].iloc[0].to_dict()
            target_label = st.selectbox("DB-Athlet", targets["target_label"].tolist(), key="ai_training_alias_target")
            target_row = targets[targets["target_label"] == target_label].iloc[0].to_dict()
            bib_specific = st.checkbox(
                "Nur fuer diese Startnummer speichern",
                value=False,
                help="Leer lassen, wenn der Timing-Name generell diesem Athleten entsprechen soll.",
                key="ai_training_alias_bib_specific",
            )
            bib_for_alias = None
            if bib_specific:
                bib_vals = pd.to_numeric(
                    scope.loc[scope["source_name_key"] == source_row["source_name_key"], "bib"],
                    errors="coerce",
                ).dropna().astype(int).unique().tolist()
                if bib_vals:
                    bib_for_alias = st.selectbox("Startnummer", sorted(bib_vals), key="ai_training_alias_bib")
            if st.button("Zuordnung speichern", key="ai_training_alias_save"):
                save_training_alias(
                    source_row["source_name"],
                    source_row["source_nation"],
                    bib_for_alias,
                    target_row,
                )
                load_training_data.clear()
                st.success("Zuordnung gespeichert.")
                st.rerun()

    metric_rows = []
    for metric_label in selected_metric_labels:
        metric_col = metric_defs.get(metric_label)
        if not metric_col or metric_col not in scope.columns:
            continue
        frame = scope.dropna(subset=[metric_col]).copy()
        if frame.empty:
            continue
        frame["metric"] = metric_label
        frame["metric_value"] = pd.to_numeric(frame[metric_col], errors="coerce")
        frame = flag_training_metric_outliers(
            frame,
            "metric_value",
            category_col="training_location",
            athlete_col="rider_id",
        )
        metric_rows.append(frame)
    plot_src = pd.concat(metric_rows, ignore_index=True, sort=False) if metric_rows else pd.DataFrame()
    if plot_src.empty:
        st.info("Keine Daten fuer die gewaehlten Split-Metriken.")
        return
    flagged_count = int(plot_src["measurement_flagged"].fillna(False).sum()) if "measurement_flagged" in plot_src.columns else 0
    if flagged_count:
        st.caption(f"Fehlmessungen automatisch ausgeschlossen: {flagged_count}")
    plot_src_valid = plot_src.loc[~plot_src["measurement_flagged"].fillna(False)].copy()
    if plot_src_valid.empty:
        st.info("Nach dem Filtern von Fehlmessungen bleiben keine verwertbaren Trainingsdaten fuer die gewaehlten Metriken.")
        return

    t1, t2, t3 = st.columns(3)
    with t1:
        trend_mode = st.radio("Verlauf", ["Alle Laeufe", "Bester pro Session"], index=1, horizontal=True, key="ai_training_trend_mode")
    with t2:
        value_mode = st.radio("Wert", ["Rohzeit", "Delta pro Strecke"], horizontal=True, key="ai_training_value_mode")
    with t3:
        show_points = st.toggle("Punkte anzeigen", value=False, key="ai_training_show_points")

    if trend_mode == "Bester pro Session":
        plot_src_valid = (
            plot_src_valid.sort_values(["metric_value", "training_datetime"], ascending=[True, True], kind="stable")
            .drop_duplicates(subset=["rider_id", "training_location", "session_id", "metric"], keep="first")
            .copy()
        )
    if value_mode == "Delta pro Strecke":
        ref = plot_src_valid.groupby(["training_location", "metric"], dropna=False)["metric_value"].transform("min")
        plot_src_valid["plot_value"] = plot_src_valid["metric_value"] - ref
        y_title = "Delta zum Bestwert je Strecke/Metrik im Zeitraum (s)"
        st.caption("Delta pro Strecke: Referenz ist der schnellste Wert je Strecke und Metrik im aktuell gewaehlten Zeitraum.")
    else:
        plot_src_valid["plot_value"] = plot_src_valid["metric_value"]
        y_title = "Zeit (s)"
    plot_src_valid["series_label"] = plot_src_valid["rider_short"].fillna(plot_src_valid["rider_label"]) + " - " + plot_src_valid["metric"]
    plot_src_valid["datetime_label"] = plot_src_valid["training_datetime"].dt.strftime("%d/%m/%y %H:%M")
    plot_src_valid["session_datetime_label"] = pd.to_datetime(plot_src_valid["session_start"], errors="coerce").dt.strftime("%d/%m/%y %H:%M")
    if trend_mode == "Bester pro Session":
        plot_src_valid["x_dt"] = pd.to_datetime(plot_src_valid["session_start"], errors="coerce")
        plot_src_valid["x_dt"] = plot_src_valid["x_dt"].where(plot_src_valid["x_dt"].notna(), plot_src_valid["training_datetime"])
    else:
        plot_src_valid["x_dt"] = plot_src_valid["training_datetime"]
    vals = pd.to_numeric(plot_src_valid["plot_value"], errors="coerce").dropna()
    y_scale = alt.Scale(zero=False)
    if not vals.empty:
        y_min = float(vals.min())
        y_max = float(vals.max())
        if np.isfinite(y_min) and np.isfinite(y_max):
            pad = max((y_max - y_min) * 0.12, 0.01)
            if abs(y_max - y_min) < 1e-9:
                pad = max(abs(y_max) * 0.05, 0.01)
            y_scale = alt.Scale(domain=[y_min - pad, y_max + pad], zero=False, nice=False)

    base = alt.Chart(plot_src_valid).encode(
        x=alt.X(
            "x_dt:T",
            title="Datum / Uhrzeit",
            axis=alt.Axis(labelAngle=-35, labelLimit=120),
        ),
        y=alt.Y("plot_value:Q", title=y_title, scale=y_scale),
        color=alt.Color("training_location:N", title="Strecke"),
        detail="series_label:N",
    )
    point_tooltip = [
        alt.Tooltip("datetime_label:N", title="Datum/Zeit"),
        alt.Tooltip("session_label:N", title="Session"),
        alt.Tooltip("training_location:N", title="Strecke"),
        alt.Tooltip("rider_label:N", title="Athlet"),
        alt.Tooltip("metric:N", title="Metrik"),
        alt.Tooltip("metric_value:Q", title="Rohzeit", format=".3f"),
        alt.Tooltip("plot_value:Q", title="Chart-Wert", format=".3f"),
    ]
    layers = [base.mark_line()]
    if show_points:
        layers.append(
            base.mark_point(size=55, opacity=0.85)
            .encode(
                shape=alt.Shape("rider_short:N", title="Athlet"),
                tooltip=point_tooltip,
            )
        )
    st.altair_chart(alt.layer(*layers).properties(height=430), use_container_width=True)

    def avg_top3(series: pd.Series) -> float:
        s = pd.to_numeric(series, errors="coerce").dropna().sort_values()
        return float(s.head(3).mean()) if not s.empty else np.nan

    summary = (
        plot_src_valid.groupby(["rider_label", "training_location", "metric"], as_index=False)
        .agg(
            runs=("metric_value", "count"),
            sessions=("session_id", "nunique"),
            best=("metric_value", "min"),
            avg_top3=("metric_value", avg_top3),
            median=("metric_value", "median"),
            std=("metric_value", "std"),
        )
        .sort_values(["rider_label", "training_location", "metric"], kind="stable")
    )
    for c in ["best", "avg_top3", "median", "std"]:
        summary[c] = pd.to_numeric(summary[c], errors="coerce").round(3)
    st.markdown("**Summary pro Athlet, Strecke und Metrik**")
    st.dataframe(
        summary.rename(
            columns={
                "rider_label": "Athlet",
                "training_location": "Strecke",
                "metric": "Metrik",
                "runs": "Laeufe",
                "sessions": "Sessions",
                "best": "Best",
                "avg_top3": "Ø Top 3",
                "median": "Median",
                "std": "Std",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    flag_cols = [
        "rider_id",
        "training_location",
        "session_id",
        "training_datetime",
        "source_name",
        "bib",
        "metric",
        "metric_value",
        "measurement_flagged",
        "measurement_flag_reason",
    ]
    flags = plot_src[[c for c in flag_cols if c in plot_src.columns]].copy()
    detail = scope.sort_values(["training_datetime", "training_location", "rider_label"], kind="stable").copy()
    detail["Datum/Zeit"] = detail["training_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    if not flags.empty:
        flagged_detail = (
            flags[flags["measurement_flagged"].fillna(False)]
            .groupby(["rider_id", "training_location", "session_id", "training_datetime", "source_name", "bib"], as_index=False, dropna=False)
            .agg(
                Fehlmessung=("metric", lambda s: ", ".join(sorted(set(str(x) for x in s if str(x))))),
                Fehlergrund=("measurement_flag_reason", lambda s: ", ".join(sorted(set(str(x) for x in s if str(x))))),
            )
        )
        detail = detail.merge(
            flagged_detail,
            on=["rider_id", "training_location", "session_id", "training_datetime", "source_name", "bib"],
            how="left",
        )
    if "Fehlmessung" not in detail.columns:
        detail["Fehlmessung"] = ""
    if "Fehlergrund" not in detail.columns:
        detail["Fehlergrund"] = ""
    for label, col in metric_defs.items():
        if col in detail.columns:
            detail[label] = detail[col].apply(format_seconds_3)
    detail_cols = [
        "Datum/Zeit", "session_label", "training_location", "rider_label", "source_name", "source_nation", "Fehlmessung", "Fehlergrund",
        "bib", "source_file", "gate",
    ] + [label for label in metric_defs if label in detail.columns]
    st.markdown("**Trainingslaeufe**")
    st.dataframe(
        detail[detail_cols].rename(
            columns={
                "session_label": "Session",
                "training_location": "Strecke",
                "rider_label": "Athlet",
                "source_name": "Timing-Name",
                "source_nation": "Timing-Nation",
                "bib": "Bib",
                "source_file": "Quelle",
                "gate": "Gate/Block",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )


require_page_access(["admin", "coach"], "Athlete Insights")
render_sidebar_nav()
page_prefs = load_page_prefs("athlete_insights")

st.title("Athlete Insights")
st.caption("Trend, Segment Profile, Results Trend und Training.")

# Disable Vega/Altair HTML tooltips on this page. Their browser-level overlay can
# survive Streamlit reruns/navigation and cover the next Athlete Insights screen.
st.markdown(
    """
    <style>
      .vg-tooltip,
      .vega-tooltip {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

section_options = ["Athlete Trend", "Segment Profile", "Results Trend", "Training"]
section_default = page_prefs.get("ai_active_section", section_options[0])
if section_default not in section_options:
    section_default = section_options[0]
ai_active_section = st.segmented_control(
    "Ansicht",
    section_options,
    default=section_default,
    key="ai_active_section",
)
update_page_prefs("athlete_insights", {"ai_active_section": ai_active_section})

if ai_active_section == "Training":
    render_training_insights(page_prefs)
    st.stop()

all_runs = load_runs()
master_results = load_master_results()

if all_runs.empty:
    st.warning("Keine Daten gefunden.")
    st.stop()

rider_nation_opts = sorted([x for x in all_runs["nation"].dropna().unique().tolist() if x])

nf1, nf2 = st.columns([1, 3])
with nf1:
    sel_nations = st.multiselect(
        "Nation (Rider) – leer = alle",
        rider_nation_opts,
        default=[x for x in page_prefs.get("sel_nations", []) if x in rider_nation_opts],
        key="ai_sel_nations",
    )
with nf2:
    rider_pool_for_select = all_runs.copy()
    if sel_nations:
        rider_pool_for_select = rider_pool_for_select[rider_pool_for_select["nation"].isin(sel_nations)].copy()
    rider_opts = sorted([x for x in rider_pool_for_select["rider_label"].dropna().unique().tolist() if x])
    sel_riders = st.multiselect(
        "Athlete (leer = keinen anzeigen)",
        rider_opts,
        default=[x for x in page_prefs.get("sel_riders", []) if x in rider_opts],
        key="insight_riders",
    )

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
    sel_years = st.multiselect("Jahr", year_opts, default=[x for x in page_prefs.get("sel_years", default_years) if x in year_opts] or default_years, key="ai_sel_years")
with f2:
    sel_event_types = st.multiselect("Event Type", event_type_opts, default=[x for x in page_prefs.get("sel_event_types", event_type_opts) if x in event_type_opts] or event_type_opts, key="ai_sel_event_types")
with f3:
    if rider_mode == "nation":
        sel_categories = st.multiselect("Kategorie", cat_opts, default=[x for x in page_prefs.get("sel_categories", []) if x in cat_opts], key="ai_sel_categories")
    else:
        sel_categories = cat_opts
        st.multiselect("Kategorie", cat_opts, default=cat_opts, disabled=True, key="ai_cat_disabled")
with f4:
    if rider_mode == "nation":
        sel_gender = st.multiselect("Geschlecht", gender_opts, default=[x for x in page_prefs.get("sel_gender", []) if x in gender_opts], key="ai_sel_gender")
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
    sel_locations = st.multiselect("Location (optional)", loc_opts, default=[x for x in page_prefs.get("sel_locations", []) if x in loc_opts], key="ai_sel_locations")
with g2:
    round_order_pref = ["R1", "LCQ", "1/32", "1/16", "1/8", "1/4", "1/2", "F", "M1", "M2", "M3", "QF", "SF", "F1", "F2", "F3"]
    round_seen = [x for x in loc_scope["round_short"].dropna().astype(str).unique().tolist() if clean_spaces(x)]
    round_opts = [x for x in round_order_pref if x in set(round_seen)] + [x for x in sorted(round_seen) if x not in round_order_pref]
    # New round families (USABMX etc.) are available but intentionally not default-selected.
    round_defaults = [x for x in ["R1", "LCQ", "1/32", "1/16", "1/8", "1/4", "1/2", "F"] if x in set(round_opts)]
    sel_rounds = st.multiselect("Runde (optional)", round_opts, default=[x for x in page_prefs.get("sel_rounds", round_defaults) if x in round_opts] or round_defaults, key="ai_sel_rounds")

# Comparison/reference pool must stay on the full field for the active
# event/category/gender filters. Nation and athlete filters only define which
# riders are displayed, not who they are ranked against.
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
    index=(["Event Top N (robust)", "Heat Rank 4 (Qualification Cut)", "Heat Rank 1 (Winner)"].index(page_prefs.get("ref_label")) if page_prefs.get("ref_label") in ["Event Top N (robust)", "Heat Rank 4 (Qualification Cut)", "Heat Rank 1 (Winner)"] else 0),
    key="ai_ref_label",
)
event_top_n = 1
event_ko_final_only = False
if ref_label == "Event Top N (robust)":
    with st.container():
        if "event_top_n" in st.session_state and st.session_state["event_top_n"] not in [1, 3, 8]:
            st.session_state["event_top_n"] = 1
        event_top_n = st.selectbox("Event Top N", [1, 3, 8], index=([1, 3, 8].index(page_prefs.get("event_top_n")) if page_prefs.get("event_top_n") in [1, 3, 8] else 0), key="event_top_n")

update_page_prefs("athlete_insights", {
    "sel_nations": sel_nations,
    "sel_riders": sel_riders,
    "sel_years": sel_years,
    "sel_event_types": sel_event_types,
    "sel_categories": sel_categories if rider_mode == "nation" else [],
    "sel_gender": sel_gender if rider_mode == "nation" else [],
    "sel_locations": sel_locations,
    "sel_rounds": sel_rounds,
    "ref_label": ref_label,
    "event_top_n": event_top_n,
})

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

if ai_active_section == "Athlete Trend":
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

elif ai_active_section == "Segment Profile":
    st.subheader("Segment Profile")

    segment_profile_src = runs_sel.copy()
    segment_profile_rank_src = pool_rel.copy()
    segment_ref_suffix_map = {
        "rank4": "rank4_ref",
        "winner": "winner",
        "event_topn": "event_topn_ref",
        "event_top4": "event_topn_ref",
        "event_best": "event_topn_ref",
    }
    segment_ref_suffix = segment_ref_suffix_map.get(ref_key, "rank4_ref")
    segment_profile_defs = [
        {"label": "BottomDelta", "delta_col": "start_delta", "time_col": "start", "seg_base": "start", "rank_col": "rank_bottom"},
        {"label": "T1Delta", "delta_col": "t1_delta", "time_col": "t1", "seg_base": "t1", "rank_col": "rank_t1"},
        {"label": "T2Delta", "delta_col": "t2_delta", "time_col": "t2", "seg_base": "t2", "rank_col": "rank_t2"},
        {"label": "T3Delta", "delta_col": "t3_delta", "time_col": "t3", "seg_base": "t3", "rank_col": "rank_t3"},
        {"label": "LaptimeDelta", "delta_col": "finish_delta", "time_col": "finish", "seg_base": "finish", "rank_col": "rank_finish"},
        {"label": "Bottom->T1Delta", "delta_col": "split_bottom_t1_delta", "time_col": "split_bottom_t1", "seg_base": "split_bottom_t1", "rank_col": "rank_bottom_t1"},
        {"label": "T1->T2Delta", "delta_col": "split_t1_t2_delta", "time_col": "split_t1_t2", "seg_base": "split_t1_t2", "rank_col": "rank_t1_t2"},
        {"label": "T2->T3Delta", "delta_col": "split_t2_t3_delta", "time_col": "split_t2_t3", "seg_base": "split_t2_t3", "rank_col": "rank_t2_t3"},
        {"label": "T3->FinishDelta", "delta_col": "split_t3_finish_delta", "time_col": "split_t3_finish", "seg_base": "split_t3_finish", "rank_col": "rank_t3_finish"},
    ]

    segment_profile_available = []
    for sd in segment_profile_defs:
        delta_col = sd["delta_col"]
        time_col = sd["time_col"]
        ref_col = f"{sd['seg_base']}_{segment_ref_suffix}"
        delta_use_col = f"{delta_col}_w" if f"{delta_col}_w" in segment_profile_src.columns else delta_col
        if delta_use_col not in segment_profile_src.columns or time_col not in segment_profile_src.columns or ref_col not in segment_profile_src.columns:
            continue
        if pd.to_numeric(segment_profile_src[delta_use_col], errors="coerce").notna().sum() == 0:
            continue
        sd2 = sd.copy()
        sd2["delta_use_col"] = delta_use_col
        sd2["ref_col"] = ref_col
        segment_profile_available.append(sd2)

    if not segment_profile_available:
        st.info("Keine Daten fuer Segment Profile in der aktuellen Auswahl.")
    else:
        segment_profile_label_map = {sd["label"]: segment_short_label(sd["label"]) for sd in segment_profile_available}
        segment_profile_options = [segment_profile_label_map[sd["label"]] for sd in segment_profile_available]
        bottom_default = segment_profile_label_map.get("BottomDelta")
        segment_profile_default = [bottom_default] if bottom_default else segment_profile_options[:1]

        rider_meta = (
            segment_profile_src[["rider_id", "rider_label", "rider_short"]]
            .dropna(subset=["rider_id"])
            .drop_duplicates(subset=["rider_id"])
            .copy()
        )
        rider_meta["rider_label"] = rider_meta["rider_label"].fillna(rider_meta["rider_short"])
        rider_meta["rider_label"] = rider_meta["rider_label"].fillna(rider_meta["rider_id"].astype(str))
        rider_options = rider_meta["rider_label"].dropna().astype(str).tolist()
        rider_label_to_ids = (
            rider_meta.groupby("rider_label")["rider_id"]
            .apply(lambda s: [str(x) for x in s.dropna().tolist()])
            .to_dict()
        )
        rider_id_to_short = (
            rider_meta.assign(rider_id_str=rider_meta["rider_id"].astype(str))
            .drop_duplicates(subset=["rider_id_str"])
            .set_index("rider_id_str")["rider_short"]
            .to_dict()
        )

        profile_ids_key = "segment_profile_profile_ids"
        if profile_ids_key not in st.session_state or not st.session_state[profile_ids_key]:
            st.session_state[profile_ids_key] = [1]
        profile_ids = list(st.session_state[profile_ids_key])

        def _profile_key(profile_id: int, suffix: str) -> str:
            return f"segment_profile_{suffix}_{profile_id}"

        def _profile_defaults() -> dict:
            return {
                "riders": list(rider_options),
                "segments": list(segment_profile_default),
                "comparison_mode": "Athlet",
                "metric": "Segment Delta",
                "delta_mode": "Alle Werte",
            }

        def _ensure_profile_state(profile_id: int, source_profile_id=None) -> None:
            defaults = _profile_defaults()
            if source_profile_id is not None:
                defaults = {
                    "riders": list(st.session_state.get(_profile_key(source_profile_id, "riders"), defaults["riders"])),
                    "segments": list(st.session_state.get(_profile_key(source_profile_id, "segments"), defaults["segments"])),
                    "comparison_mode": st.session_state.get(_profile_key(source_profile_id, "comparison_mode"), defaults["comparison_mode"]),
                    "metric": st.session_state.get(_profile_key(source_profile_id, "metric"), defaults["metric"]),
                    "delta_mode": st.session_state.get(_profile_key(source_profile_id, "delta_mode"), defaults["delta_mode"]),
                }
            for field, value in defaults.items():
                key = _profile_key(profile_id, field)
                if key not in st.session_state:
                    st.session_state[key] = value

        def _clear_profile_state(profile_id: int) -> None:
            for suffix in ["riders", "segments", "comparison_mode", "metric", "delta_mode"]:
                st.session_state.pop(_profile_key(profile_id, suffix), None)
            st.session_state.pop(f"ai_segment_profile_exclusions_{profile_id}", None)

        first_profile_id = profile_ids[0]
        _ensure_profile_state(first_profile_id)

        add_col, spacer_col = st.columns([1, 5])
        if add_col.button("Weiteres Profil hinzufuegen", key="segment_profile_add_profile"):
            new_profile_id = max(profile_ids) + 1
            _ensure_profile_state(new_profile_id, source_profile_id=first_profile_id)
            st.session_state[profile_ids_key] = profile_ids + [new_profile_id]
            st.rerun()

        def _segment_profile_round_value(df: pd.DataFrame) -> pd.Series:
            base = df["round_short"].fillna("").astype(str).str.strip()
            fallback = df["round_title"].fillna("").astype(str).str.strip()
            return base.where(base != "", fallback).replace("", "Unknown")

        def _segment_profile_event_value(df: pd.DataFrame) -> pd.Series:
            base = df["event_short"].fillna(df["location"]).fillna("Unknown").astype(str)
            year_txt = pd.to_numeric(df["year"], errors="coerce").astype("Int64").astype(str)
            if len(sel_years) == 1:
                event_val = base
            else:
                event_val = base + " " + year_txt.replace("<NA>", "")
            dup = event_val.groupby(event_val).transform("size") > 1
            date_txt = pd.to_datetime(df["event_dt"], errors="coerce").dt.strftime("%Y-%m-%d").fillna(df["event_id"].astype(str))
            return np.where(dup, event_val + " " + date_txt, event_val)

        def _apply_segment_grouping(df: pd.DataFrame, comparison_mode: str, metric_label: str) -> pd.DataFrame:
            out = df.copy()
            if out.empty:
                out["group_value"] = pd.Series(dtype="object")
                out["group_sort"] = pd.Series(dtype="float")
                out["x_label"] = pd.Series(dtype="object")
                return out
            effective_mode = comparison_mode
            if metric_label == "Segment Rank" and comparison_mode == "Runde":
                effective_mode = "Event"
            if effective_mode == "Athlet":
                out["group_value"] = out["rider_short"].fillna("Unknown")
                out["group_sort"] = 0.0
            elif effective_mode == "Runde":
                out["group_value"] = out["rider_short"].fillna("Unknown") + " / " + _segment_profile_round_value(out)
                out["group_sort"] = pd.to_numeric(out["round_sort"], errors="coerce").fillna(999)
            elif effective_mode == "Jahr":
                year_num = pd.to_numeric(out["year"], errors="coerce")
                year_txt = year_num.astype("Int64").astype(str).replace("<NA>", "Unknown")
                out["group_value"] = out["rider_short"].fillna("Unknown") + " / " + year_txt
                out["group_sort"] = year_num.fillna(9999)
            else:
                out["group_value"] = out["rider_short"].fillna("Unknown") + " / " + _segment_profile_event_value(out)
                out["group_sort"] = pd.to_datetime(out["event_dt"], errors="coerce").view("int64").astype(float)
                out.loc[pd.isna(pd.to_datetime(out["event_dt"], errors="coerce")), "group_sort"] = np.inf
            out["x_label"] = out["group_value"].astype(str) + " / " + out["Segment Short"].astype(str)
            return out

        common_cols = [
            "rider_id",
            "rider_short",
            "display_name",
            "event_id",
            "event_dt",
            "event_short",
            "location",
            "round_sort",
            "round_short",
            "round_title",
            "heat_title",
            "group_id",
            "category",
            "gender",
            "year",
        ]

        for idx, profile_id in enumerate(profile_ids, start=1):
            _ensure_profile_state(profile_id, source_profile_id=first_profile_id if profile_id != first_profile_id else None)
            riders_key = _profile_key(profile_id, "riders")
            segments_key = _profile_key(profile_id, "segments")
            comparison_key = _profile_key(profile_id, "comparison_mode")
            metric_key = _profile_key(profile_id, "metric")
            delta_key = _profile_key(profile_id, "delta_mode")

            header_cols = st.columns([6, 1])
            header_cols[0].markdown(f"**Segment Profile {idx}**")
            remove_clicked = False
            if profile_id != first_profile_id:
                remove_clicked = header_cols[1].button("Entfernen", key=f"segment_profile_remove_{profile_id}")
            if remove_clicked:
                updated_ids = [pid for pid in profile_ids if pid != profile_id]
                st.session_state[profile_ids_key] = updated_ids or [first_profile_id]
                _clear_profile_state(profile_id)
                st.rerun()

            with st.expander(f"Filter Segment Profile {idx}", expanded=(idx == 1)):
                selected_profile_riders = st.multiselect(
                    "Athleten",
                    options=rider_options,
                    default=st.session_state[riders_key],
                    key=riders_key,
                )
                selected_profile_segments = st.multiselect(
                    "Segmente anzeigen",
                    options=segment_profile_options,
                    default=st.session_state[segments_key],
                    key=segments_key,
                )
                profile_comparison_mode = st.selectbox(
                    "Vergleichsmodus",
                    ["Athlet", "Runde", "Jahr", "Event"],
                    index=["Athlet", "Runde", "Jahr", "Event"].index(st.session_state[comparison_key]),
                    key=comparison_key,
                )
                profile_metric = st.selectbox(
                    "Boxplot-Wert",
                    ["Segment Delta", "Segment Rank"],
                    index=["Segment Delta", "Segment Rank"].index(st.session_state[metric_key]),
                    key=metric_key,
                )
                profile_delta_mode = None
                if profile_metric == "Segment Delta":
                    profile_delta_mode = st.selectbox(
                        "Delta-Werte",
                        ["Alle Werte", "Nur bester Wert pro Event"],
                        index=["Alle Werte", "Nur bester Wert pro Event"].index(st.session_state[delta_key]),
                        key=delta_key,
                    )
                else:
                    st.session_state[delta_key] = "Alle Werte"
                if profile_metric == "Segment Rank" and profile_comparison_mode == "Runde":
                    st.caption("Segment Rank bleibt eventbasiert (Best of the Day pro Event), auch wenn Vergleichsmodus `Runde` gewaehlt ist.")

            selected_profile_riders = st.session_state.get(riders_key, list(rider_options))
            selected_profile_segments = st.session_state.get(segments_key, list(segment_profile_default))
            profile_comparison_mode = st.session_state.get(comparison_key, "Athlet")
            profile_metric = st.session_state.get(metric_key, "Segment Delta")
            profile_delta_mode = st.session_state.get(delta_key, "Alle Werte") if profile_metric == "Segment Delta" else None

            selected_profile_ids = []
            for rider_label in selected_profile_riders:
                selected_profile_ids.extend(rider_label_to_ids.get(rider_label, []))
            selected_profile_ids = list(dict.fromkeys(selected_profile_ids))
            if not selected_profile_ids:
                st.info(f"Segment Profile {idx}: Bitte mindestens einen Athleten auswaehlen.")
                continue

            segment_profile_rows = []
            seg_display_order = [x for x in selected_profile_segments if x in segment_profile_options]
            if not seg_display_order:
                st.info(f"Segment Profile {idx}: Bitte mindestens ein Segment auswaehlen.")
                continue

            segment_profile_src_local = segment_profile_src[
                segment_profile_src["rider_id"].astype(str).isin(selected_profile_ids)
            ].copy()
            if segment_profile_src_local.empty:
                st.info(f"Segment Profile {idx}: Keine Daten fuer die gewaehlteten Athleten in der aktuellen Auswahl.")
                continue

            for sd in segment_profile_available:
                seg_short = segment_profile_label_map[sd["label"]]
                if seg_short not in selected_profile_segments:
                    continue

                if profile_metric == "Segment Delta":
                    seg_df = segment_profile_src_local[
                        common_cols + [sd["delta_use_col"], sd["time_col"], sd["ref_col"]]
                    ].copy()
                    ref_seg_df = segment_profile_rank_src[
                        common_cols + [sd["delta_use_col"], sd["time_col"], sd["ref_col"]]
                    ].copy()
                    seg_df["source_row_id"] = seg_df.index.astype(str)
                    seg_df = seg_df.rename(columns={
                        sd["delta_use_col"]: "metric_value",
                        sd["time_col"]: "segment_time",
                        sd["ref_col"]: "reference_time",
                    })
                    ref_seg_df = ref_seg_df.rename(columns={
                        sd["delta_use_col"]: "metric_value",
                        sd["time_col"]: "segment_time",
                        sd["ref_col"]: "reference_time",
                    })
                    seg_df["metric_value"] = pd.to_numeric(seg_df["metric_value"], errors="coerce")
                    seg_df["segment_time"] = pd.to_numeric(seg_df["segment_time"], errors="coerce")
                    seg_df["reference_time"] = pd.to_numeric(seg_df["reference_time"], errors="coerce")
                    ref_seg_df["metric_value"] = pd.to_numeric(ref_seg_df["metric_value"], errors="coerce")
                    ref_seg_df["segment_time"] = pd.to_numeric(ref_seg_df["segment_time"], errors="coerce")
                    ref_seg_df["reference_time"] = pd.to_numeric(ref_seg_df["reference_time"], errors="coerce")
                    if ref_key in {"event_topn", "event_top4", "event_best"} and int(event_top_n) == 1:
                        seg_df = apply_event_top1_adjustment(
                            seg_df,
                            ref_seg_df,
                            time_col="segment_time",
                            reference_time_col="reference_time",
                            delta_col="metric_value",
                            group_cols=["event_id", "group_id"],
                            rider_col="rider_id",
                            use_rider_best=(profile_delta_mode == "Nur bester Wert pro Event"),
                        )
                    seg_df = seg_df.dropna(subset=["metric_value"]).copy()
                    if seg_df.empty:
                        continue
                    seg_df["Segment"] = sd["label"]
                    seg_df["Segment Short"] = seg_short
                    seg_df = _apply_segment_grouping(seg_df, profile_comparison_mode, profile_metric)
                    if profile_delta_mode == "Nur bester Wert pro Event":
                        best_keys = ["rider_id", "event_id", "Segment Short"]
                        if profile_comparison_mode == "Runde":
                            best_keys.append("group_value")
                        seg_df = seg_df.sort_values(["metric_value", "event_dt", "round_sort"], ascending=[True, True, True])
                        seg_df = seg_df.drop_duplicates(subset=best_keys, keep="first")
                else:
                    seg_df = segment_profile_rank_src[
                        common_cols + [sd["time_col"]]
                    ].copy()
                    seg_df["source_row_id"] = seg_df.index.astype(str)
                    seg_df = seg_df.rename(columns={sd["time_col"]: "segment_time"})
                    seg_df["segment_time"] = pd.to_numeric(seg_df["segment_time"], errors="coerce")
                    seg_df = seg_df.dropna(subset=["segment_time"]).copy()
                    if seg_df.empty:
                        continue
                    seg_df = seg_df.sort_values(["segment_time", "event_dt", "round_sort"], ascending=[True, True, True])
                    seg_df = seg_df.drop_duplicates(subset=["rider_id", "event_id"], keep="first")
                    seg_df["metric_value"] = seg_df.groupby(
                        ["event_id", "category", "gender"], dropna=False
                    )["segment_time"].rank(method="min", ascending=True)
                    seg_df = seg_df[seg_df["rider_id"].astype(str).isin(selected_profile_ids)].copy()
                    if seg_df.empty:
                        continue
                    seg_df["Segment"] = sd["label"]
                    seg_df["Segment Short"] = seg_short
                    seg_df["reference_time"] = np.nan
                    seg_df = _apply_segment_grouping(seg_df, profile_comparison_mode, profile_metric)
                    seg_df = seg_df.dropna(subset=["metric_value"]).copy()

                if seg_df.empty:
                    continue
                seg_df["point_id"] = (
                    seg_df["rider_id"].astype(str)
                    + "|"
                    + seg_df["event_id"].astype(str)
                    + "|"
                    + seg_df["Segment Short"].astype(str)
                    + "|"
                    + seg_df["source_row_id"].astype(str)
                )
                segment_profile_rows.append(seg_df)

            segment_profile_df = pd.concat(segment_profile_rows, ignore_index=True) if segment_profile_rows else pd.DataFrame()
            if segment_profile_df.empty:
                st.info(f"Segment Profile {idx}: Keine Daten fuer die aktuelle Auswahl.")
                continue
            if go is None:
                st.warning("Plotly ist fuer den Segment-Boxplot nicht verfuegbar.")
                continue

            segment_profile_view_signature = build_view_signature(
                f"segment_profile_{profile_id}",
                {
                    "selected_ids": sorted(selected_profile_ids),
                    "segments": seg_display_order,
                    "comparison_mode": profile_comparison_mode,
                    "metric": profile_metric,
                    "delta_mode": profile_delta_mode,
                    "ref_key": ref_key,
                    "event_top_n": event_top_n,
                    "years": sel_years,
                    "event_types": sel_event_types,
                    "categories": sel_categories,
                    "gender": sel_gender,
                    "locations": sel_locations,
                    "rounds": sel_rounds,
                },
            )
            exclusions_state_key = f"ai_segment_profile_exclusions_{profile_id}"
            segment_profile_exclusion_state = sync_exclusion_state(
                exclusions_state_key, segment_profile_view_signature
            )
            segment_profile_df = apply_point_exclusions(segment_profile_df, segment_profile_exclusion_state)
            if segment_profile_df.empty:
                st.info(f"Segment Profile {idx}: Alle Punkte der aktuellen Ansicht sind ausgeschlossen.")
                seg_cols = st.columns([1, 1, 1, 2])
                if seg_cols[2].button(
                    "Alle Ausschluesse zuruecksetzen",
                    key=f"segment_profile_reset_exclusions_empty_{profile_id}",
                ):
                    segment_profile_exclusion_state["excluded_ids"] = []
                    segment_profile_exclusion_state["undo_stack"] = []
                    st.session_state[exclusions_state_key] = segment_profile_exclusion_state
                    st.rerun()
                st.caption(
                    f"Aktuell ausgeschlossen: {len(excluded_id_set(segment_profile_exclusion_state))} Punkt(e)."
                )
                continue

            selected_short_order = []
            for rider_label in selected_profile_riders:
                ids_for_label = rider_label_to_ids.get(rider_label, [])
                for rider_id in ids_for_label:
                    short_label = rider_id_to_short.get(str(rider_id))
                    if short_label and short_label not in selected_short_order:
                        selected_short_order.append(short_label)
            if not selected_short_order:
                selected_short_order = sorted(segment_profile_df["rider_short"].dropna().unique().tolist())

            def _group_sort_key(g: str):
                sdf = segment_profile_df[segment_profile_df["group_value"] == g]
                rider = str(sdf["rider_short"].dropna().iloc[0]) if not sdf["rider_short"].dropna().empty else ""
                rider_idx = selected_short_order.index(rider) if rider in selected_short_order else len(selected_short_order)
                group_sort = pd.to_numeric(sdf["group_sort"], errors="coerce")
                gsort = float(group_sort.min()) if group_sort.notna().any() else float("inf")
                return (rider_idx, gsort, g)

            group_value_order = sorted(segment_profile_df["group_value"].dropna().unique().tolist(), key=_group_sort_key)
            x_order = []
            for g in group_value_order:
                for seg_short in seg_display_order:
                    x_label = f"{g} / {seg_short}"
                    if x_label in segment_profile_df["x_label"].values:
                        x_order.append(x_label)
            x_order = list(dict.fromkeys(x_order))

            fig = go.Figure()
            for x_label in x_order:
                rdf = segment_profile_df[segment_profile_df["x_label"] == x_label].copy()
                if rdf.empty:
                    continue
                customdata = np.column_stack([
                    rdf["display_name"].fillna(rdf["event_id"]).to_numpy(),
                    rdf["event_dt"].dt.strftime("%Y-%m-%d").fillna("").to_numpy(),
                    rdf["location"].fillna("").to_numpy(),
                    rdf["round_short"].fillna(rdf["round_title"]).to_numpy(),
                    rdf["heat_title"].fillna("").to_numpy(),
                    rdf["Segment Short"].to_numpy(),
                    rdf["group_value"].fillna("").to_numpy(),
                    rdf["point_id"].astype(str).to_numpy(),
                ])
                fig.add_trace(
                    go.Box(
                        y=rdf["metric_value"],
                        x=[x_label] * len(rdf),
                        name=x_label,
                        boxpoints="all",
                        jitter=0.42,
                        pointpos=0,
                        width=0.42,
                        whiskerwidth=0.8,
                        marker=dict(
                            size=10,
                            color="rgba(0,0,0,0)",
                            line=dict(color="rgba(120,120,120,0.85)", width=1.4),
                        ),
                        line=dict(color="rgba(25,25,25,0.95)", width=1.8),
                        fillcolor="rgba(0,0,0,0)",
                        customdata=customdata,
                        hovertemplate=(
                            "Rider: %{customdata[0]}<br>"
                            "Group: %{customdata[6]}<br>"
                            "Segment: %{customdata[5]}<br>"
                            + ("Delta (s): %{y:.4f}<br>" if profile_metric == "Segment Delta" else "Segment Rank: %{y:.0f}<br>")
                            + "Date: %{customdata[1]}<br>"
                            + "Location: %{customdata[2]}<br>"
                            + "Round: %{customdata[3]}<br>"
                            + "Heat: %{customdata[4]}<extra></extra>"
                        ),
                        showlegend=False,
                    )
                )
            yaxis_range = None
            yaxis_tickvals = None
            metric_vals = pd.to_numeric(segment_profile_df["metric_value"], errors="coerce").dropna()
            if profile_metric == "Segment Rank":
                if not metric_vals.empty:
                    rank_cap = int(np.ceil(metric_vals.quantile(0.95)))
                    rank_cap = max(8, min(48, rank_cap))
                    yaxis_range = [rank_cap + 0.5, 0.5]
                    yaxis_tickvals = sorted(set([x for x in [1, 4, 8, 16, 32, 48] if x <= rank_cap]))
            elif not metric_vals.empty:
                upper_cap = float(metric_vals.quantile(0.95))
                lower_bound = float(metric_vals.min())
                if np.isfinite(upper_cap) and np.isfinite(lower_bound):
                    if upper_cap <= lower_bound:
                        upper_cap = lower_bound + 1e-6
                    pad = max((upper_cap - lower_bound) * 0.05, 1e-6)
                    yaxis_range = [lower_bound - pad, upper_cap + pad]
            fig.update_layout(
                height=620,
                margin=dict(l=40, r=20, t=10, b=40),
                plot_bgcolor="white",
                paper_bgcolor="white",
                boxmode="overlay",
                xaxis=dict(title="Athlet / Gruppe / Segment", tickangle=-90, categoryorder="array", categoryarray=x_order),
                yaxis=dict(
                    title="Delta (s)" if profile_metric == "Segment Delta" else "Segment Rank",
                    gridcolor="#e5e7eb",
                    zeroline=(profile_metric == "Segment Delta"),
                    zerolinecolor="#cbd5e1",
                    range=yaxis_range,
                    tickvals=yaxis_tickvals,
                ),
            )
            segment_profile_event = st.plotly_chart(
                fig,
                use_container_width=True,
                key=f"ai_segment_profile_boxplot_{profile_id}",
                on_select="rerun",
                selection_mode=("points", "box", "lasso"),
            )
            selected_segment_profile_ids = get_plotly_selected_point_ids(segment_profile_event)
            seg_cols = st.columns([1, 1, 1, 2])
            exclude_clicked = seg_cols[0].button(
                "Ausgewaehlte Punkte ausschliessen",
                key=f"segment_profile_exclude_selected_{profile_id}",
                disabled=not selected_segment_profile_ids,
            )
            undo_clicked = seg_cols[1].button(
                "Letzten Ausschluss rueckgaengig",
                key=f"segment_profile_undo_exclusion_{profile_id}",
                disabled=not segment_profile_exclusion_state.get("undo_stack"),
            )
            reset_clicked = seg_cols[2].button(
                "Alle Ausschluesse zuruecksetzen",
                key=f"segment_profile_reset_exclusions_{profile_id}",
                disabled=not excluded_id_set(segment_profile_exclusion_state),
            )
            if exclude_clicked and selected_segment_profile_ids:
                excluded = excluded_id_set(segment_profile_exclusion_state)
                new_ids = [pid for pid in selected_segment_profile_ids if pid not in excluded]
                if new_ids:
                    segment_profile_exclusion_state["excluded_ids"] = sorted(excluded.union(new_ids))
                    segment_profile_exclusion_state.setdefault("undo_stack", []).append(new_ids)
                    st.session_state[exclusions_state_key] = segment_profile_exclusion_state
                    st.rerun()
            if undo_clicked and segment_profile_exclusion_state.get("undo_stack"):
                last_batch = segment_profile_exclusion_state["undo_stack"].pop()
                remaining = [pid for pid in segment_profile_exclusion_state.get("excluded_ids", []) if pid not in set(last_batch)]
                segment_profile_exclusion_state["excluded_ids"] = remaining
                st.session_state[exclusions_state_key] = segment_profile_exclusion_state
                st.rerun()
            if reset_clicked:
                segment_profile_exclusion_state["excluded_ids"] = []
                segment_profile_exclusion_state["undo_stack"] = []
                st.session_state[exclusions_state_key] = segment_profile_exclusion_state
                st.rerun()
            st.caption(
                f"Aktuell ausgeschlossen: {len(excluded_id_set(segment_profile_exclusion_state))} Punkt(e)."
            )
elif ai_active_section == "Results Trend":
    st.subheader("Results Trend")
    show_boxplot = st.toggle("Boxplot statt Liniengrafik", value=False, key="results_trend_show_boxplot")
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
    rider_event["point_id"] = rider_event["rider_id"].astype(str) + "|" + rider_event["event_id"].astype(str)
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

            if show_boxplot:
                box_df = plot_df.dropna(subset=["final_rank_raw", "rider_short"]).copy()
                box_df["point_id"] = box_df["rider_id"].astype(str) + "|" + box_df["event_id"].astype(str)
                results_trend_view_signature = build_view_signature(
                    "results_trend",
                    {
                        "selected_ids": sorted(str(x) for x in selected_ids),
                        "years": sel_years,
                        "event_types": sel_event_types,
                        "categories": sel_categories,
                        "gender": sel_gender,
                        "locations": sel_locations,
                        "rounds": sel_rounds,
                        "show_boxplot": show_boxplot,
                    },
                )
                results_trend_exclusion_state = sync_exclusion_state(
                    "ai_results_trend_exclusions", results_trend_view_signature
                )
                box_df = apply_point_exclusions(box_df, results_trend_exclusion_state)
                rider_order = sorted(box_df["rider_short"].dropna().unique().tolist())
                if go is None:
                    st.warning("Plotly ist fuer den Boxplot nicht verfuegbar.")
                elif box_df.empty:
                    rider_event = rider_event.iloc[0:0].copy()
                    st.info("Alle Punkte der aktuellen Results-Trend-Ansicht sind ausgeschlossen.")
                    rt_cols = st.columns([1, 1, 1, 2])
                    if rt_cols[2].button(
                        "Alle Ausschluesse zuruecksetzen",
                        key="results_trend_reset_exclusions_empty",
                    ):
                        results_trend_exclusion_state["excluded_ids"] = []
                        results_trend_exclusion_state["undo_stack"] = []
                        st.session_state["ai_results_trend_exclusions"] = results_trend_exclusion_state
                        st.rerun()
                    st.caption(
                        f"Aktuell ausgeschlossen: {len(excluded_id_set(results_trend_exclusion_state))} Punkt(e)."
                    )
                else:
                    rider_colors = [
                        "#1f77b4", "#aec7e8", "#ff7f0e", "#ffbb78", "#2ca02c",
                        "#98df8a", "#d62728", "#ff9896", "#9467bd", "#c5b0d5",
                    ]
                    color_map = {r: rider_colors[i % len(rider_colors)] for i, r in enumerate(rider_order)}
                    fig = go.Figure()
                    for rider in rider_order:
                        rdf = box_df[box_df["rider_short"] == rider].copy()
                        customdata = np.column_stack([
                            rdf["event_label"].fillna("").to_numpy(),
                            rdf["event_dt"].dt.strftime("%Y-%m-%d").fillna("").to_numpy(),
                            rdf["location"].fillna("").to_numpy(),
                            rdf["reached_phase"].fillna("").to_numpy(),
                            rdf["overflow_clamped"].fillna("no").to_numpy(),
                            rdf["point_id"].astype(str).to_numpy(),
                        ])
                        fig.add_trace(
                            go.Box(
                                y=rdf["final_rank_raw"],
                                x=[rider] * len(rdf),
                                name=rider,
                                boxpoints="all",
                                jitter=0.42,
                                pointpos=0,
                                width=0.42,
                                whiskerwidth=0.8,
                                marker=dict(size=11, color="rgba(0,0,0,0)", line=dict(color="rgba(120,120,120,0.85)", width=1.4)),
                                line=dict(color="black", width=1.4),
                                fillcolor="rgba(0,0,0,0)",
                                showlegend=False,
                                customdata=customdata,
                                hovertemplate=(
                                    "Rider: %{x}<br>"
                                    "Final Rank: %{y}<br>"
                                    "Event label: %{customdata[0]}<br>"
                                    "Date: %{customdata[1]}<br>"
                                    "Location: %{customdata[2]}<br>"
                                    "Phase: %{customdata[3]}<br>"
                                    "Overflow clamped: %{customdata[4]}<extra></extra>"
                                ),
                            )
                        )
                    for y0, y1, color in [
                        (1, 3, "rgba(44,160,44,0.12)"),
                        (4, 8, "rgba(241,196,15,0.12)"),
                        (9, 16, "rgba(230,126,34,0.12)"),
                        (17, 32, "rgba(231,76,60,0.12)"),
                    ]:
                        fig.add_hrect(y0=y0, y1=y1, fillcolor=color, line_width=0, layer="below")
                    fig.update_layout(
                        height=520,
                        margin=dict(l=40, r=20, t=10, b=40),
                        plot_bgcolor="white",
                        paper_bgcolor="white",
                        boxmode="group",
                        xaxis=dict(title="Rider", tickangle=-90, categoryorder="array", categoryarray=rider_order, tickson="boundaries"),
                        yaxis=dict(title="Final Rank", range=[48, 1], tickmode="array", tickvals=[1, 4, 8, 16, 32, 48], gridcolor="#e5e7eb"),
                    )
                    results_trend_event = st.plotly_chart(
                        fig,
                        use_container_width=True,
                        key="ai_results_trend_boxplot",
                        on_select="rerun",
                        selection_mode=("points", "box", "lasso"),
                    )
                    selected_results_ids = get_plotly_selected_point_ids(results_trend_event)
                    rt_cols = st.columns([1, 1, 1, 2])
                    exclude_clicked = rt_cols[0].button(
                        "Ausgewaehlte Punkte ausschliessen",
                        key="results_trend_exclude_selected",
                        disabled=not selected_results_ids,
                    )
                    undo_clicked = rt_cols[1].button(
                        "Letzten Ausschluss rueckgaengig",
                        key="results_trend_undo_exclusion",
                        disabled=not results_trend_exclusion_state.get("undo_stack"),
                    )
                    reset_clicked = rt_cols[2].button(
                        "Alle Ausschluesse zuruecksetzen",
                        key="results_trend_reset_exclusions",
                        disabled=not excluded_id_set(results_trend_exclusion_state),
                    )
                    if exclude_clicked and selected_results_ids:
                        excluded = excluded_id_set(results_trend_exclusion_state)
                        new_ids = [pid for pid in selected_results_ids if pid not in excluded]
                        if new_ids:
                            results_trend_exclusion_state["excluded_ids"] = sorted(excluded.union(new_ids))
                            results_trend_exclusion_state.setdefault("undo_stack", []).append(new_ids)
                            st.session_state["ai_results_trend_exclusions"] = results_trend_exclusion_state
                            st.rerun()
                    if undo_clicked and results_trend_exclusion_state.get("undo_stack"):
                        last_batch = results_trend_exclusion_state["undo_stack"].pop()
                        remaining = [pid for pid in results_trend_exclusion_state.get("excluded_ids", []) if pid not in set(last_batch)]
                        results_trend_exclusion_state["excluded_ids"] = remaining
                        st.session_state["ai_results_trend_exclusions"] = results_trend_exclusion_state
                        st.rerun()
                    if reset_clicked:
                        results_trend_exclusion_state["excluded_ids"] = []
                        results_trend_exclusion_state["undo_stack"] = []
                        st.session_state["ai_results_trend_exclusions"] = results_trend_exclusion_state
                        st.rerun()
                    st.caption(
                        f"Aktuell ausgeschlossen: {len(excluded_id_set(results_trend_exclusion_state))} Punkt(e)."
                    )
                    rider_event = rider_event[~rider_event["point_id"].astype(str).isin(excluded_id_set(results_trend_exclusion_state))].copy()
            else:
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
