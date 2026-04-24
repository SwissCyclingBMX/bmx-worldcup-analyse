import sqlite3
import os
import subprocess
import shutil
import zipfile
import requests
import unicodedata
import datetime
import json
import html as html_lib
import re
from typing import List, Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from access_control import render_sidebar_nav, require_page_access
from ui_prefs import load_page_prefs, update_page_prefs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "bmx.db")
DB_URL_ZIP = "https://github.com/SwissCyclingBMX/bmx-worldcup-analyse/releases/download/db-latest/bmx_db.zip"
DB_PATH_CLOUD = "/tmp/bmx.db"
ACTIVE_HEAT_ANALYZER_EVENT_PATH = os.environ.get(
    "ACTIVE_HEAT_ANALYZER_EVENT_PATH",
    os.path.join(APP_DIR, "state", "active_heat_analyzer_event.json"),
)

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


def persist_active_heat_analyzer_event(event_id: str, event_label: str, event_date: Any = "") -> None:
    event_id_str = str(event_id or "").strip()
    if not event_id_str:
        return
    event_date_compact = ""
    raw_event_date = str(event_date or "").strip()
    if raw_event_date:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_event_date):
            event_date_compact = raw_event_date.replace("-", "")
        elif re.fullmatch(r"\d{8}", raw_event_date):
            event_date_compact = raw_event_date
    if not event_date_compact and len(event_id_str) >= 8 and event_id_str[:8].isdigit():
        event_date_compact = event_id_str[:8]
    payload = {
        "event_id": event_id_str,
        "event_label": str(event_label or "").strip(),
        "event_date": event_date_compact,
        "updated_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "heat_analyzer_sidebar",
    }
    try:
        os.makedirs(os.path.dirname(ACTIVE_HEAT_ANALYZER_EVENT_PATH), exist_ok=True)
        tmp_path = ACTIVE_HEAT_ANALYZER_EVENT_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, ACTIVE_HEAT_ANALYZER_EVENT_PATH)
    except Exception:
        pass


def norm_name_key(s: str) -> str:
    """Order-insensitive name key (handles 'LAST First' vs 'First LAST')."""
    base = norm_name(s)
    if not base:
        return ""
    tokens = [t for t in base.split() if len(t) > 1]
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
    if len(s) >= 10 and s.startswith("100"):
        return s
    return ""


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
# Poller service helpers
# ----------------------------
POLLER_ENV_DIR = "/etc/bmx-pollers"
POLLER_UNIT_TEMPLATE = "/etc/systemd/system/bmx-poller@.service"


def running_on_systemd_host() -> bool:
    return os.path.isdir("/run/systemd/system")


def systemctl_available() -> bool:
    return os.path.exists("/bin/systemctl") or os.path.exists("/usr/bin/systemctl")


def systemctl_bin() -> str:
    return "/bin/systemctl" if os.path.exists("/bin/systemctl") else "/usr/bin/systemctl"


def journalctl_bin() -> str:
    return "/bin/journalctl" if os.path.exists("/bin/journalctl") else "/usr/bin/journalctl"


def poller_instance_slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:64]


def sqorz_event_id_from_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    m = re.search(r"/event/([a-f0-9]{24})", u, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/json/event/([a-f0-9]{24})", u, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def run_cmd(args: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def write_poller_env(instance: str, values: Dict[str, str]) -> str:
    os.makedirs(POLLER_ENV_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(POLLER_ENV_DIR, f"{instance}.env")
    lines = []
    for k, v in values.items():
        val = str(v if v is not None else "")
        val = val.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{k}="{val}"')
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def poller_service_name(instance: str) -> str:
    return f"bmx-poller@{instance}.service"


def poller_status(instance: str) -> Dict[str, str]:
    unit = poller_service_name(instance)
    rc, out, err = run_cmd(
        [
            systemctl_bin(),
            "show",
            unit,
            "--property=ActiveState,SubState,Result,ExecMainStartTimestamp",
            "--value",
        ]
    )
    if rc != 0:
        return {"unit": unit, "active": "unknown", "sub": "", "result": err or "not found", "started": ""}
    vals = out.splitlines()
    while len(vals) < 4:
        vals.append("")
    return {
        "unit": unit,
        "active": vals[0],
        "sub": vals[1],
        "result": vals[2],
        "started": vals[3],
    }


def list_poller_units() -> List[Dict[str, str]]:
    cmd = [systemctl_bin()]
    rc, out, _ = run_cmd(cmd + ["list-units", "--type=service", "--all", "bmx-poller@*.service", "--no-legend"])
    rows: List[Dict[str, str]] = []
    if rc != 0 or not out:
        return rows
    for ln in out.splitlines():
        parts = ln.split()
        if not parts:
            continue
        unit = parts[0]
        load = parts[1] if len(parts) > 1 else ""
        active = parts[2] if len(parts) > 2 else ""
        sub = parts[3] if len(parts) > 3 else ""
        rows.append({"unit": unit, "load": load, "active": active, "sub": sub})
    return rows


def tail_poller_logs(instance: str, lines: int = 30) -> str:
    unit = poller_service_name(instance)
    cmd = [journalctl_bin()]
    rc, out, err = run_cmd(cmd + ["-u", unit, "-n", str(lines), "--no-pager"])
    if rc != 0:
        return err or "Keine Logs verfügbar."
    return out or "Keine Logs verfügbar."


def ensure_poller_template_installed() -> Tuple[bool, str]:
    if os.path.exists(POLLER_UNIT_TEMPLATE):
        return True, "Service-Template vorhanden."

    app_dir = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(app_dir, "deploy", "bmx-poller@.service.example")
    if not os.path.exists(src):
        return False, f"Template-Datei fehlt: {src}"

    try:
        shutil.copyfile(src, POLLER_UNIT_TEMPLATE)
        run_cmd([systemctl_bin(), "daemon-reload"])
        return True, f"Template installiert: {POLLER_UNIT_TEMPLATE}"
    except Exception as e:
        return False, f"Template konnte nicht installiert werden: {e}"


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
            try:
                df = pd.read_sql_query(
                    """
                    SELECT event_id, display_name, location, country, event_type, event_date, last_seen
                    FROM events
                    ORDER BY event_id DESC
                    """,
                    conn,
                )
            except Exception:
                df = pd.read_sql_query(
                    """
                    SELECT event_id, display_name, location, country, event_date, last_seen
                    FROM events
                    ORDER BY event_id DESC
                    """,
                    conn,
                )
                df["event_type"] = ""
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return df

    # Derive year:
    # 1) from event_id prefix (YYYY....) when valid
    # 2) fallback from event_date
    year_from_id = pd.to_numeric(
        df["event_id"].astype(str).str.extract(r"^(\d{4})", expand=False),
        errors="coerce",
    ).astype("Int64")
    year_from_date = pd.to_datetime(df["event_date"], errors="coerce").dt.year.astype("Int64")
    df["year"] = year_from_id.fillna(year_from_date).astype("Int64").astype(str)

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


    # Series detection: prefer explicit event_type, fall back to legacy inference only when missing.
    event_type_norm = df["event_type"].fillna("").astype(str).str.strip().str.upper()
    name_norm = df["display_name"].fillna("").astype(str)
    event_id_norm = df["event_id"].astype(str)

    is_wch = event_type_norm.eq("WM")
    is_em = event_type_norm.eq("EM")
    is_euc = event_type_norm.eq("EC")
    is_usap = event_type_norm.eq("USABMX")
    is_ffc = event_type_norm.eq("FFC")
    is_scc = event_type_norm.eq("SCC")
    is_other_series = event_type_norm.eq("OTHER")
    is_wc = event_type_norm.eq("WC")

    missing_type = event_type_norm.eq("")
    is_wch = is_wch | (missing_type & (
        event_id_norm.str.contains("wch", case=False, regex=False)
        | name_norm.str.contains("world championship", case=False, regex=False)
        | name_norm.str.contains("world championships", case=False, regex=False)
    ))
    is_em = is_em | (missing_type & (
        event_id_norm.str.contains("_em_", case=False, regex=False)
        | name_norm.str.contains("european championship", case=False, regex=False)
        | name_norm.str.contains("european championships", case=False, regex=False)
    ))
    is_euc = is_euc | (missing_type & (
        event_id_norm.str.contains("_euc_", case=False, regex=False)
        | name_norm.str.contains("european cup", case=False, regex=False)
        | name_norm.str.contains("european bmx cup", case=False, regex=False)
    ))
    is_usap = is_usap | (missing_type & (
        event_id_norm.str.contains("_usap_", case=False, regex=False)
        | event_id_norm.str.contains("_usabmx_", case=False, regex=False)
        | name_norm.str.contains("usa bmx", case=False, regex=False)
        | name_norm.str.contains("pro championship", case=False, regex=False)
        | name_norm.str.contains("lone star", case=False, regex=False)
        | name_norm.str.contains("day 1", case=False, regex=False)
        | name_norm.str.contains("day 2", case=False, regex=False)
        | name_norm.str.contains("day 3", case=False, regex=False)
    ))
    is_ffc = is_ffc | (missing_type & (
        event_id_norm.str.contains("_ffc_", case=False, regex=False)
        | name_norm.str.contains(r"\bffc\b", case=False, regex=True)
    ))
    is_scc = is_scc | (missing_type & (
        event_id_norm.str.contains("_scc_", case=False, regex=False)
        | name_norm.str.contains(r"\bscc\b", case=False, regex=True)
        | name_norm.str.contains("winterthur", case=False, regex=False)
    ))
    is_other_series = is_other_series | (missing_type & (
        event_id_norm.str.contains("_other_", case=False, regex=False)
        | event_id_norm.str.contains("_sqorz_", case=False, regex=False)
        | event_id_norm.str.contains("tmp", case=False, regex=False)
        | name_norm.str.fullmatch(r"\s*tmp\s*", case=False)
        | name_norm.str.contains("bundesliga", case=False, regex=False)
        | name_norm.str.contains("championnat", case=False, regex=False)
        | name_norm.str.contains("training", case=False, regex=False)
    ))
    is_wc = is_wc | (missing_type & name_norm.str.contains("world cup", case=False, regex=False))

    df["series"] = np.where(
        is_wch,
        "wch",
        np.where(
            is_em,
            "em",
            np.where(
                is_euc,
                "euc",
                np.where(
                    is_usap,
                    "usap",
                    np.where(
                        is_ffc,
                        "ffc",
                        np.where(is_scc, "scc", np.where(is_other_series, "other", "wc")),
                    ),
                ),
            ),
        ),
    )

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

    # Assign sequential rounds only for real WC / EC race weekends. Other series keep display names.
    df["_event_day"] = pd.to_datetime(df["event_id"].astype(str).str.slice(0, 8), format="%Y%m%d", errors="coerce")
    df["round_num"] = pd.NA
    mask_round = df["series"].isin(["wc", "euc"])
    if race_event_ids:
        mask_round = mask_round & df["event_id"].isin(race_event_ids)
    for (yr, series), grp in df.loc[mask_round].sort_values(["_event_day", "event_id"]).groupby(["year", "series"]):
        df.loc[grp.index, "round_num"] = range(1, len(grp) + 1)

    round_label = "ROUND " + df["round_num"].astype("Int64").astype(str) + " - " + loc_clean
    df["label_short"] = base
    df.loc[mask_round & df["round_num"].notna(), "label_short"] = round_label.loc[mask_round & df["round_num"].notna()]
    df.loc[(df["series"] == "euc") & df["round_num"].notna(), "label_short"] = (
        "EC-" + round_label.loc[(df["series"] == "euc") & df["round_num"].notna()]
    )
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


@st.cache_data(ttl=10)
def load_event_pick_counts(event_ids: List[str]) -> Dict[str, int]:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        return {}
    event_ids = [e for e in event_ids if e]
    if not event_ids:
        return {}

    in_sql, params = safe_in_clause(event_ids)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT event_id, COUNT(*) AS n FROM picks WHERE event_id IN {in_sql} GROUP BY event_id",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {str(eid): int(n or 0) for eid, n in rows}


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
    near_days = {today, today - datetime.timedelta(days=1), today + datetime.timedelta(days=1)}
    date_mask = dates.isin(near_days)

    # Fallback for timezone drift or late-night sessions:
    # treat events seen in the last 10 hours as live, even if event_date differs by one day.
    recent_mask = pd.Series(False, index=events_df.index)
    if "last_seen" in events_df.columns:
        last_seen_ts = pd.to_datetime(events_df["last_seen"], errors="coerce")
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=10)
        recent_mask = last_seen_ts >= cutoff

    live_ids = events_df.loc[date_mask | recent_mask, "event_id"].dropna().unique().tolist()
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


def coachnow_athlete_tag(name: str) -> str:
    """Format athlete name for one-tag copy: first name + last name (ASCII, no spaces)."""
    if not isinstance(name, str):
        return ""
    s = name.strip()
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    tokens = re.findall(r"[A-Za-z0-9]+", s)
    if not tokens:
        return ""

    def _cap(tok: str) -> str:
        return tok[:1].upper() + tok[1:].lower() if tok else ""

    if len(tokens) == 1:
        return _cap(tokens[0])
    return _cap(tokens[0]) + _cap(tokens[-1])


def normalize_picks_df(df: pd.DataFrame) -> pd.DataFrame:
    """Make columns consistent across historical schema changes."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # Ensure required columns exist
    for c in ["group_id", "round_key", "round_title", "heat_id", "heat_title", "heat_status", "start_time_string", "start_time_first_seen_at", "start_time_last_seen_at"]:
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


def format_seconds_3(val: Any) -> str:
    try:
        num = float(val)
    except Exception:
        return ""
    if pd.isna(num):
        return ""
    return f"{num:.3f}"


def format_rank_tag(rank: Any) -> str:
    try:
        rank_i = int(rank)
    except Exception:
        return ""
    return f"#{rank_i}"


def extract_clock_time(*values: Any) -> str:
    for value in values:
        s = str(value or "").strip()
        if not s:
            continue
        m = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", s)
        if m:
            return m.group(1)
    return ""


def build_training_tag_payload(df_block: pd.DataFrame) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    athlete_tags: List[Dict[str, str]] = []
    if df_block is not None and not df_block.empty and "name" in df_block.columns:
        tag_src = df_block.copy()
        tag_src["name_clean"] = tag_src["name"].fillna("").astype(str).str.strip()
        tag_src = tag_src[tag_src["name_clean"] != ""].copy()
        if not tag_src.empty:
            nation_src = (
                tag_src["nation"]
                if "nation" in tag_src.columns
                else pd.Series([""] * len(tag_src), index=tag_src.index)
            )
            tag_src["nation_clean"] = nation_src.fillna("").astype(str).str.upper()
            tag_src["is_sui"] = (tag_src["nation_clean"] == "SUI").astype(int)
            tag_src = tag_src.reset_index(drop=True)
            tag_src["ord"] = range(len(tag_src))
            tag_src = (
                tag_src.sort_values(["is_sui", "ord"], ascending=[False, True], kind="stable")
                .drop_duplicates(subset=["name_clean"], keep="first")
            )
        names = tag_src["name_clean"].tolist() if not tag_src.empty else []
        seen_tags = set()
        for n in names:
            tag_val = coachnow_athlete_tag(n)
            if not tag_val or tag_val in seen_tags:
                continue
            seen_tags.add(tag_val)
            athlete_tags.append({"label": tag_val, "value": tag_val})

    meta_tags = [
        {"label": "Training", "value": "Training"},
        {"label": "Gate", "value": "Gate"},
    ]
    return athlete_tags, meta_tags


def training_category_label(category: Any, gate: Any = "") -> str:
    code = str(category or "").strip().upper()
    gate_text = str(gate or "").strip()
    gate_label = re.sub(r"^Race\s+\d+\s+", "", gate_text, flags=re.IGNORECASE).strip()
    code_map = {
        "ME": "Elite Men",
        "WE": "Elite Women",
        "MU": "U23 Men",
        "WU": "U23 Women",
        "MJ": "Junior Men",
        "WJ": "Junior Women",
    }
    if code in code_map:
        return code_map[code]
    inferred = infer_training_group_label(category, gate)
    if inferred:
        return inferred
    if gate_label:
        return gate_label
    return code


def infer_training_group_label(category: Any, gate: Any = "") -> str:
    code = str(category or "").strip().upper()
    exact_map = {
        "ME": "Elite Men",
        "WE": "Elite Women",
        "MU": "U23 Men",
        "WU": "U23 Women",
        "MJ": "Junior Men",
        "WJ": "Junior Women",
        "W00": "Elite Women",
    }
    if code in exact_map:
        return exact_map[code]

    gate_text = str(gate or "").strip().lower()
    patterns = [
        ("men elite", "Elite Men"),
        ("women elite", "Elite Women"),
        ("women championship", "Elite Women"),
        ("men u23", "U23 Men"),
        ("women u23", "U23 Women"),
        ("men junior", "Junior Men"),
        ("women junior", "Junior Women"),
    ]
    for token, label in patterns:
        if token in gate_text:
            return label
    return ""


def filter_training_by_allowed_groups(df_train: pd.DataFrame, allowed_group_ids: List[int]) -> pd.DataFrame:
    if df_train.empty or not allowed_group_ids:
        return df_train
    allowed_labels = {GROUP_MAP.get(int(gid), "") for gid in allowed_group_ids if int(gid) in GROUP_MAP}
    allowed_labels.discard("")
    if not allowed_labels or "training_group_label" not in df_train.columns:
        return df_train
    return df_train[df_train["training_group_label"].isin(allowed_labels)].copy()


def filter_training_metric_outliers(
    df_train: pd.DataFrame,
    metric_col: str,
    category_col: str = "category_label",
    absolute_lower: Optional[float] = None,
    absolute_upper: Optional[float] = None,
) -> Tuple[pd.DataFrame, Dict[str, Dict[str, float]]]:
    flagged_df, stats = flag_training_metric_outliers(
        df_train,
        metric_col,
        category_col=category_col,
        absolute_lower=absolute_lower,
        absolute_upper=absolute_upper,
    )
    if flagged_df.empty or "measurement_flagged" not in flagged_df.columns:
        return flagged_df, stats
    return flagged_df.loc[~flagged_df["measurement_flagged"]].copy(), stats


def flag_training_metric_outliers(
    df_train: pd.DataFrame,
    metric_col: str,
    category_col: str = "category_label",
    athlete_col: str = "name",
    absolute_lower: Optional[float] = None,
    absolute_upper: Optional[float] = None,
) -> Tuple[pd.DataFrame, Dict[str, Dict[str, float]]]:
    if df_train.empty or metric_col not in df_train.columns or category_col not in df_train.columns:
        return df_train, {}

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

    stats: Dict[str, Dict[str, float]] = {}

    for category_label, grp in df_flagged.groupby(category_col, dropna=False):
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
        ] = df_flagged.loc[flagged_idx, "measurement_flag_reason"].replace("", "category_fast")
        stats[str(category_label or "")] = {
            "lower_bound": lower_bound,
            "removed": float(too_fast.fillna(False).sum()),
        }

    if athlete_col in df_flagged.columns:
        for athlete_name, grp in df_flagged.groupby(athlete_col, dropna=False):
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
            too_fast = grp_metric < lower_bound
            too_slow = grp_metric > upper_bound
            fast_idx = grp.index[too_fast.fillna(False)]
            slow_idx = grp.index[too_slow.fillna(False)]
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
            stats.setdefault("__athlete_bounds__", {})
            stats["__athlete_bounds__"][f"{str(athlete_name or '')}::lower_bound"] = lower_bound
            stats["__athlete_bounds__"][f"{str(athlete_name or '')}::upper_bound"] = upper_bound

    return df_flagged, stats


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
def load_training_for_events(event_ids: List[str], rider_names: Optional[List[str]] = None) -> pd.DataFrame:
    db_path = DB_PATH if os.path.exists(DB_PATH) else DB_PATH_CLOUD
    if not os.path.exists(db_path):
        return pd.DataFrame()
    event_ids = [e for e in event_ids if e]
    if not event_ids:
        return pd.DataFrame()
    rider_names = [n for n in (rider_names or []) if n]

    in_sql, params = safe_in_clause(event_ids)
    where_parts = [f"event_id IN {in_sql}"]
    if rider_names:
        rider_in_sql, rider_params = safe_in_clause(rider_names)
        where_parts.append(f"name IN {rider_in_sql}")
        params = params + rider_params
    where_sql = " AND ".join(where_parts)
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            f"""
            SELECT DISTINCT
              event_id, category, bib, name, nation, gate,
              kink, bottom, interim, t1_in, total,
              training_block_id, training_block_label, training_block_time,
              source_kind, source_file, start, t1, ingested_at
            FROM training_times
            WHERE {where_sql}
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
    if "kink" in df.columns:
        df["kink_s"] = df["kink"].apply(parse_time_to_seconds)
    if "bottom" in df.columns:
        df["bottom_s"] = df["bottom"].apply(parse_time_to_seconds)
    if "interim" in df.columns:
        df["interim_s"] = df["interim"].apply(parse_time_to_seconds)
    if "t1_in" in df.columns:
        df["t1_in_s"] = df["t1_in"].apply(parse_time_to_seconds)
    if "total" in df.columns:
        df["total_s"] = df["total"].apply(parse_time_to_seconds)
    df["category_label"] = df.apply(lambda r: training_category_label(r.get("category"), r.get("gate")), axis=1)
    df["training_group_label"] = df.apply(lambda r: infer_training_group_label(r.get("category"), r.get("gate")), axis=1)
    if "training_block_id" not in df.columns:
        df["training_block_id"] = ""
    if "training_block_label" not in df.columns:
        df["training_block_label"] = ""
    if "training_block_time" not in df.columns:
        df["training_block_time"] = ""
    if "ingested_at" not in df.columns:
        df["ingested_at"] = ""

    df["training_block_time"] = df["training_block_time"].fillna("").astype(str).str.strip()
    df["training_block_label"] = df["training_block_label"].fillna("").astype(str).str.strip()
    df["training_block_id"] = df["training_block_id"].fillna("").astype(str).str.strip()
    first_seen_key = df["training_block_id"].where(
        df["training_block_id"].str.strip().ne(""),
        (
            df["event_id"].fillna("").astype(str)
            + "|"
            + df["source_file"].fillna("").astype(str)
            + "|"
            + df["training_block_label"].fillna("").astype(str)
            + "|"
            + df["gate"].fillna("").astype(str)
        ),
    )
    first_seen_ts = pd.to_datetime(df["ingested_at"], errors="coerce")
    first_seen_ts_group = first_seen_ts.groupby(first_seen_key, sort=False).transform("min")
    first_seen_clock = first_seen_ts_group.dt.strftime("%H:%M:%S").fillna("")
    event_day = pd.to_datetime(
        df["event_id"].fillna("").astype(str).str.extract(r"^(\d{8})", expand=False),
        format="%Y%m%d",
        errors="coerce",
    )
    first_seen_day = first_seen_ts_group.dt.strftime("%d.%m.").fillna("")
    event_day_label = event_day.dt.strftime("%d.%m.").fillna("")
    first_seen_day = first_seen_day.where(first_seen_day.ne(""), event_day_label)
    first_seen_label = (
        first_seen_day.str.strip()
        + np.where(
            (first_seen_day.str.strip() != "") & (first_seen_clock.str.strip() != ""),
            " | ",
            "",
        )
        + first_seen_clock.str.strip()
    )
    fallback_time = df.apply(
        lambda r: extract_clock_time(
            r.get("training_block_time"),
            r.get("training_block_label"),
            r.get("gate"),
            r.get("source_file"),
            r.get("start"),
            r.get("t1"),
        ),
        axis=1,
    )
    df.loc[df["training_block_time"] == "", "training_block_time"] = fallback_time[df["training_block_time"] == ""]
    missing_block_time = df["training_block_time"].fillna("").astype(str).str.strip() == ""
    df.loc[missing_block_time, "training_block_time"] = first_seen_clock[missing_block_time]
    df["first_seen_clock"] = first_seen_clock
    df["first_seen_label"] = first_seen_label.where(first_seen_label.str.strip() != "", first_seen_clock)
    fallback_label = df["training_block_label"].copy()
    fallback_label = fallback_label.where(fallback_label.str.strip().ne(""), df["gate"].fillna("").astype(str).str.strip())
    fallback_label = fallback_label.where(fallback_label.str.strip().ne(""), df["training_block_time"].fillna("").astype(str).str.strip())
    fallback_label = fallback_label.where(fallback_label.str.strip().ne(""), "Training")
    df["training_block_label"] = fallback_label
    fallback_id = (
        "train|"
        + df["event_id"].fillna("").astype(str)
        + "|"
        + df["source_file"].fillna("").astype(str)
        + "|"
        + df["training_block_label"].fillna("").astype(str)
        + "|"
        + df["training_block_time"].fillna("").astype(str)
    )
    df.loc[df["training_block_id"] == "", "training_block_id"] = fallback_id[df["training_block_id"] == ""]
    return df


def training_stats(df_train: pd.DataFrame) -> pd.DataFrame:
    empty_cols = ["name_key", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "name", "cons_score"]
    if df_train.empty:
        return pd.DataFrame(columns=empty_cols)

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
    if out.empty:
        return pd.DataFrame(columns=empty_cols)
    # round averages for display
    for c in ["avg_top3_start", "avg_top3_t1", "best_start", "best_t1"]:
        out[c] = out[c].round(3)
    return out


def race_stats(df_race: pd.DataFrame) -> pd.DataFrame:
    """
    Compute best/avg3/consistency for race data (start/t1 from picks).
    """
    empty_cols = ["name_norm", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "name", "cons_score"]
    if df_race.empty:
        return pd.DataFrame(columns=empty_cols)

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
    if out.empty:
        return pd.DataFrame(columns=empty_cols)
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
    # also treat heats with already available result/timing as not upcoming
    if "has_result" in heats.columns:
        not_upcoming = not_upcoming | heats["has_result"].fillna(False).astype(bool)

    # keep "confirmed", "upcoming", "scheduled", plus anything unknown (to not hide data)
    out = heats[~not_upcoming].copy()
    # JSTiming can expose future-round placeholder heats without riders yet (e.g. empty LCQ shells).
    # Those are not actionable for the live view and should not replace real upcoming startlists.
    if "has_named_rows" in out.columns:
        out = out[out["has_named_rows"].fillna(False).astype(bool)].copy()
    return out


def add_heat_result_flags(heats: pd.DataFrame, df_rows: pd.DataFrame) -> pd.DataFrame:
    """Annotate each heat with whether any result/timing row already exists."""
    if heats.empty:
        return heats
    out = heats.copy()
    if df_rows is None or df_rows.empty:
        out["has_result"] = False
        out["has_named_rows"] = False
        return out

    tmp = df_rows.copy()
    time_cols = [c for c in ["time", "t1", "t2", "t3", "t4"] if c in tmp.columns]
    has_time = pd.Series(False, index=tmp.index)
    for c in time_cols:
        has_time = has_time | tmp[c].apply(parse_time_to_seconds).notna()

    if "rank" in tmp.columns:
        rank_num = pd.to_numeric(tmp["rank"], errors="coerce")
        # can be nullable boolean depending on dtype; force missing -> False
        has_rank = rank_num.between(1, 8, inclusive="both").fillna(False)
    else:
        has_rank = pd.Series(False, index=tmp.index, dtype=bool)

    tmp["has_result"] = (has_time.fillna(False) | has_rank.fillna(False)).astype(bool)
    if "name" in tmp.columns:
        tmp["has_named_rows"] = tmp["name"].fillna("").astype(str).str.strip().ne("")
    else:
        tmp["has_named_rows"] = False

    key_cols = [c for c in ["group_id", "round_key", "round_title", "heat_id", "heat_title"] if c in out.columns and c in tmp.columns]
    if not key_cols:
        out["has_result"] = False
        out["has_named_rows"] = False
        return out

    flags = tmp.groupby(key_cols, as_index=False).agg(
        has_result=("has_result", "any"),
        has_named_rows=("has_named_rows", "any"),
    )
    out = out.merge(flags, on=key_cols, how="left")
    out["has_result"] = out["has_result"].fillna(False).astype(bool)
    out["has_named_rows"] = out["has_named_rows"].fillna(False).astype(bool)
    return out


def heat_label_row(r: pd.Series) -> str:
    cat = r.get("category", "")
    rt = r.get("round_filter_label", r.get("round_title", ""))
    ht = r.get("heat_title", "")
    stt = r.get("start_time_string", "")
    sui = r.get("SUI", "")
    sui_part = f" | SUI: {sui}" if isinstance(sui, str) and sui.strip() else ""
    time_part = f" | {stt}" if isinstance(stt, str) and stt.strip() else ""
    return f"{cat} | {rt} | {ht}{time_part}{sui_part}"


def class_tag_from_group_id(group_id: Optional[int]) -> Optional[str]:
    mapping = {
        91: "EliteMen",
        92: "EliteWomen",
        93: "U23Men",
        94: "U23Women",
        95: "JuniorMen",
        96: "JuniorWomen",
    }
    if group_id is None:
        return None
    try:
        return mapping.get(int(group_id))
    except Exception:
        return None


def round_tag_from_title(round_title: Optional[str]) -> Optional[str]:
    if not isinstance(round_title, str):
        return None
    rt = round_title.strip()
    if not rt:
        return None
    rt_lower = rt.lower()
    if rt_lower in {"lcq", "last chance", "last chance qualifier"}:
        return "LCQ"
    if "1/32" in rt_lower:
        return "1/32Final"
    if "1/16" in rt_lower:
        return "1/16Final"
    if "1/8" in rt_lower:
        return "1/8Final"
    if "1/4" in rt_lower:
        return "1/4Final"
    if "1/2" in rt_lower:
        return "1/2Final"
    if rt_lower == "final" or rt_lower.endswith(" final") or rt_lower.endswith(" finale"):
        return "Final"
    m = re.search(r"\b(?:round|runde)\s*(\d+)\b", rt_lower)
    if m:
        return f"Round{m.group(1)}"
    return rt.replace(" ", "")


def canonical_round_label(round_title: Optional[str]) -> str:
    """Normalize noisy/duplicated round labels for filters (e.g. multilingual variants)."""
    rt = " ".join(str(round_title or "").strip().split())
    tl = rt.lower()
    if not tl:
        return ""
    if "lcq" in tl or "last chance" in tl:
        return "LCQ"
    if "1/32" in tl:
        return "1/32 Finals"
    if "1/16" in tl:
        return "1/16 Finals"
    if "1/8" in tl:
        return "1/8 Finals"
    if "1/4" in tl or "quarter" in tl:
        return "1/4 Finals"
    if "1/2" in tl or "semi" in tl:
        return "1/2 Finals"
    if tl == "final" or " final" in tl or "finale" in tl or "main" in tl:
        return "Final"
    if (
        tl.startswith("round 1")
        or tl.startswith("runde 1")
        or re.match(r"^round\s*1\b", tl)
        or re.match(r"^r\s*1\b", tl)
        or "moto" in tl
        or "manche" in tl
    ):
        return "Round 1"
    return rt


def heat_tag_from_context(heat_title: Optional[str], heat_id: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """Return (label, copy_value) as Heat N / HeatN."""
    title = str(heat_title or "").strip()
    n = None
    if title:
        m = re.search(r"(\d+)", title)
        if m:
            try:
                n = int(m.group(1))
            except Exception:
                n = None
    # Fallback only for small explicit IDs (avoid values like 910102)
    if n is None and heat_id is not None:
        try:
            hid_int = int(heat_id)
            if 1 <= hid_int <= 64:
                n = hid_int
        except Exception:
            n = None
    if n is None:
        return None, None
    return f"Heat {n}", f"Heat{n}"


def parse_clock_hms(value: Any) -> Optional[int]:
    s = str(value or "").strip()
    if not s:
        return None
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", s)
    if not m:
        return None
    try:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ss = int(m.group(3) or 0)
    except Exception:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh * 3600 + mm * 60 + ss


def format_match_delta(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    try:
        sec = abs(int(seconds))
    except Exception:
        return "-"
    mm, ss = divmod(sec, 60)
    hh, mm = divmod(mm, 60)
    if hh:
        return f"{hh}:{mm:02d}:{ss:02d}"
    return f"{mm}:{ss:02d}"


def parse_tag_csv(value: Any) -> set:
    raw = str(value or "").strip()
    if not raw:
        return set()
    parts = re.split(r"[,;\n]+", raw)
    return {str(part).strip().lower() for part in parts if str(part).strip()}


def build_heat_context(df_event: pd.DataFrame, heat_row: pd.Series) -> pd.DataFrame:
    rk = int(heat_row["round_key"])
    hid = int(heat_row["heat_id"])
    gid = int(heat_row["group_id"]) if pd.notna(heat_row.get("group_id")) else None
    chosen_round_title = heat_row.get("round_title")
    chosen_heat_title = heat_row.get("heat_title")

    df_heat_ctx = df_event[(df_event["round_key"] == rk) & (df_event["heat_id"] == hid)].copy()
    if gid is not None:
        df_heat_ctx = df_heat_ctx[df_heat_ctx["group_id"] == gid].copy()
    if chosen_round_title:
        df_heat_ctx = df_heat_ctx[df_heat_ctx["round_title"] == chosen_round_title].copy()
    if chosen_heat_title:
        df_heat_ctx = df_heat_ctx[df_heat_ctx["heat_title"] == chosen_heat_title].copy()
    if "name" in df_heat_ctx.columns:
        df_heat_ctx = df_heat_ctx[df_heat_ctx["name"].fillna("").astype(str).str.strip() != ""].copy()
    if "pick_order" in df_heat_ctx.columns:
        df_heat_ctx = df_heat_ctx.sort_values(["pick_order"], na_position="last", kind="stable")
    return df_heat_ctx


def build_heat_tag_payload(df_event: pd.DataFrame, heat_row: pd.Series) -> Tuple[pd.DataFrame, List[Dict[str, str]], List[Dict[str, str]]]:
    df_heat_ctx = build_heat_context(df_event, heat_row)

    athlete_tags: List[Dict[str, str]] = []
    if not df_heat_ctx.empty and "name" in df_heat_ctx.columns:
        tag_src = df_heat_ctx.copy()
        tag_src["name_clean"] = tag_src["name"].fillna("").astype(str).str.strip()
        tag_src = tag_src[tag_src["name_clean"] != ""].copy()
        if not tag_src.empty:
            nation_src = (
                tag_src["nation"]
                if "nation" in tag_src.columns
                else pd.Series([""] * len(tag_src), index=tag_src.index)
            )
            tag_src["nation_clean"] = nation_src.fillna("").astype(str).str.upper()
            tag_src["is_sui"] = (tag_src["nation_clean"] == "SUI").astype(int)
            tag_src = tag_src.reset_index(drop=True)
            tag_src["ord"] = range(len(tag_src))
            tag_src = (
                tag_src.sort_values(["is_sui", "ord"], ascending=[False, True], kind="stable")
                .drop_duplicates(subset=["name_clean"], keep="first")
            )
        names = tag_src["name_clean"].tolist() if not tag_src.empty else []
        seen_tags = set()
        for n in names:
            tag_val = coachnow_athlete_tag(n)
            if not tag_val or tag_val in seen_tags:
                continue
            seen_tags.add(tag_val)
            athlete_tags.append({"label": tag_val, "value": tag_val})

    meta_tags: List[Dict[str, str]] = []
    round_tag = round_tag_from_title(heat_row.get("round_title"))
    if round_tag:
        meta_tags.append({"label": round_tag, "value": round_tag})

    heat_tag_label, heat_tag_value = heat_tag_from_context(
        heat_row.get("heat_title"),
        heat_row.get("heat_id"),
    )
    if heat_tag_value:
        meta_tags.append({"label": heat_tag_label, "value": heat_tag_value})

    class_tag = class_tag_from_group_id(
        int(heat_row["group_id"]) if pd.notna(heat_row.get("group_id")) else None
    )
    if class_tag:
        meta_tags.append({"label": class_tag, "value": class_tag})

    return df_heat_ctx, athlete_tags, meta_tags


def build_heat_match_candidates(
    heats_f: pd.DataFrame,
    df_event: pd.DataFrame,
    video_time: Any,
    existing_tags_csv: Any = "",
    time_source: str = "start_time_string",
) -> List[Dict[str, Any]]:
    target_seconds = parse_clock_hms(video_time)
    if target_seconds is None or heats_f is None or heats_f.empty:
        return []

    existing_tags = parse_tag_csv(existing_tags_csv)
    candidates: List[Dict[str, Any]] = []
    for _, row in heats_f.iterrows():
        time_source_key = "start_time_first_seen_at" if str(time_source or "").strip() == "start_time_first_seen_at" else "start_time_string"
        start_time = str(row.get(time_source_key) or "").strip()[:8]
        if not start_time:
            start_time = str(row.get("start_time_string") or "").strip()[:8]
        start_seconds = parse_clock_hms(start_time)
        if start_seconds is None:
            continue
        _, athlete_tags, meta_tags = build_heat_tag_payload(df_event, row)
        tag_values = [str(t.get("value", "")).strip() for t in athlete_tags + meta_tags if str(t.get("value", "")).strip()]
        overlap = len({t.lower() for t in tag_values} & existing_tags) if existing_tags else 0
        diff_seconds = abs(start_seconds - target_seconds)
        candidates.append(
            {
                "option": heat_label_row(row),
                "label": heat_label_row(row),
                "start_time": start_time,
                "time_source": time_source_key,
                "diff_seconds": diff_seconds,
                "tag_overlap": overlap,
                "tag_values": tag_values,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["diff_seconds"],
            -item["tag_overlap"],
            item["label"],
        )
    )
    return candidates


def has_lane_pick_data(event_id: str) -> bool:
    """
    Lane-pick data is currently reliable only for World Cup event feed.
    SQORZ/USABMX and other series should not show lane-pick metrics.
    """
    e = str(event_id or "").lower()
    if (
        "_euc_" in e
        or "_em_" in e
        or "_wch_" in e
        or "_usap_" in e
        or "_usabmx_" in e
        or "_sqorz_" in e
        or "_ffc_" in e
        or "_scc_" in e
        or "_other_" in e
    ):
        return False
    if e.endswith("_bmx"):
        return True
    return False


def render_copy_buttons(
    title: str,
    tags: List[Dict[str, str]],
    section_style: str = "default",
    columns: int = 1,
    show_title: bool = True,
    show_last_copied: bool = True,
) -> None:
    if not tags:
        return
    payload_json = json.dumps(tags, ensure_ascii=False)
    title_html = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    section_class = "athlete" if section_style == "athlete" else "meta"
    columns = max(1, int(columns))
    rows = (len(tags) + columns - 1) // columns
    base_h = 18
    if show_title:
        base_h += 28
    if show_last_copied:
        base_h += 24
    comp_height = max(90, base_h + rows * 56)
    components.html(
        f"""
        <div class="tag-section {section_class}">
          {"<div class='tag-title'>" + title_html + "</div>" if show_title else ""}
          {"<div class='last-copied' id='lastcopied'>Last copied: -</div>" if show_last_copied else ""}
          <div class="tag-grid" id="grid"></div>
          <div class="toast" id="toast"></div>
        </div>
        <script>
          const tags = {payload_json};
          const root = document.currentScript.previousElementSibling;
          const grid = root.querySelector("#grid");
          const toast = root.querySelector("#toast");
          const lastCopied = root.querySelector("#lastcopied");

          const showToast = (txt) => {{
            toast.textContent = txt;
            toast.classList.add("show");
            setTimeout(() => toast.classList.remove("show"), 750);
          }};

          const fallbackCopy = (text) => {{
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.setAttribute("readonly", "");
            ta.style.position = "fixed";
            ta.style.top = "-1000px";
            ta.style.left = "-1000px";
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            ta.setSelectionRange(0, ta.value.length);
            let ok = false;
            try {{
              ok = document.execCommand("copy");
            }} catch (e) {{
              ok = false;
            }}
            document.body.removeChild(ta);
            return ok;
          }};

          const copyText = async (text) => {{
            if (!text) return false;
            if (navigator.clipboard && window.isSecureContext) {{
              try {{
                await navigator.clipboard.writeText(text);
                return true;
              }} catch (e) {{
                // fallback below
              }}
            }}
            return fallbackCopy(text);
          }};

          tags.forEach((t) => {{
            const b = document.createElement("button");
            b.className = "tagbtn";
            b.type = "button";
            b.textContent = t.label || t.value || "";
            b.onclick = async () => {{
              const val = t.value || "";
              if (!val) return;
              const ok = await copyText(val);
              if (ok) {{
                if (lastCopied) {{
                  lastCopied.textContent = "Last copied: " + val;
                }}
                showToast("Copied");
              }} else {{
                showToast("Copy failed");
              }}
            }};
            grid.appendChild(b);
          }});
        </script>
        <style>
          .tag-section {{ width: 100%; }}
          .tag-title {{
            font-size: 18px;
            font-weight: 700;
            margin: 0 0 6px 0;
          }}
          .last-copied {{
            font-size: 13px;
            opacity: 0.85;
            margin: 0 0 8px 0;
          }}
          .tag-grid {{
            display: grid;
            grid-template-columns: repeat({columns}, minmax(0, 1fr));
            gap: 8px;
          }}
          .tagbtn {{
            min-height: 48px;
            width: 100%;
            border: 1px solid rgba(120, 120, 120, 0.35);
            border-radius: 10px;
            font-size: 18px;
            font-weight: 600;
            padding: 10px 14px;
            text-align: left;
            background: #fff;
            color: #111;
            cursor: pointer;
          }}
          .meta .tagbtn {{
            font-size: 17px;
            text-align: center;
          }}
          .tagbtn:active {{
            transform: scale(0.995);
          }}
          .toast {{
            position: fixed;
            bottom: 18px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(0,0,0,0.86);
            color: #fff;
            padding: 9px 14px;
            border-radius: 9px;
            font-size: 14px;
            opacity: 0;
            transition: opacity 120ms ease;
            pointer-events: none;
            z-index: 9999;
          }}
          .toast.show {{
            opacity: 1;
          }}
          @media (prefers-color-scheme: dark) {{
            .tagbtn {{
              background: #1f2329;
              color: #f2f4f8;
              border-color: #3a3f46;
            }}
          }}
        </style>
        """,
        height=comp_height,
        scrolling=False,
    )


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
st.set_page_config(page_title="Heat Analyser", layout="wide", initial_sidebar_state="expanded")
require_page_access(["admin", "coach"], "Heat Analyser")

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
st.title("Heat Analyser")
st.caption("Live-Ansicht aktualisiert sich bei Interaktionen (kein Auto-Refresh).")

if "cache_bust" not in st.session_state:
    st.session_state["cache_bust"] = 0

events = load_events(cache_bust=st.session_state["cache_bust"])
if events.empty:
    st.error("events-Tabelle ist leer. Bitte ingest.py für mindestens ein Event laufen lassen.")
    st.stop()

# Robust series code for sidebar filtering (works even if cached events lacks 'series').
events_work = events.copy()
event_type_l = events_work.get("event_type", pd.Series(index=events_work.index, dtype="object")).fillna("").astype(str).str.strip().str.upper()
series_from_type = np.where(
    event_type_l.eq("WM"), "wch",
    np.where(
        event_type_l.eq("EC"), "euc",
        np.where(
            event_type_l.eq("EM"), "em",
            np.where(
                event_type_l.eq("USABMX"), "usap",
                np.where(
                    event_type_l.eq("FFC"), "ffc",
                    np.where(
                        event_type_l.eq("SCC"), "scc",
                        np.where(event_type_l.eq("OTHER"), "other", np.where(event_type_l.eq("WC"), "wc", "")),
                    ),
                ),
            ),
        ),
    ),
)
eid_l = events_work["event_id"].astype(str).str.lower()
name_l = events_work["display_name"].fillna("").astype(str).str.lower()
series_fallback = np.where(
    name_l.str.contains("bundesliga", regex=False)
    | name_l.str.contains("championnat", regex=False),
    "other",
    np.where(
    eid_l.str.contains("wch", regex=False) | name_l.str.contains("world championship", regex=False),
    "wch",
    np.where(
        eid_l.str.contains("_em_", regex=False) | name_l.str.contains("european championship", regex=False),
        "em",
        np.where(
            eid_l.str.contains("_euc_", regex=False) | name_l.str.contains("european cup", regex=False),
            "euc",
            np.where(
                eid_l.str.contains("_ffc_", regex=False)
                | name_l.str.contains(r"\bffc\b", regex=True),
                "ffc",
                np.where(
                    eid_l.str.contains("_scc_", regex=False)
                    | name_l.str.contains(r"\bscc\b", regex=True),
                    "scc",
                    np.where(
                        eid_l.str.contains("_other_", regex=False)
                        | eid_l.str.contains("_sqorz_", regex=False),
                        "other",
                        np.where(
                            eid_l.str.contains("_usap_", regex=False)
                            | eid_l.str.contains("_usabmx_", regex=False)
                            | name_l.str.contains("usa bmx", regex=False)
                            | name_l.str.contains("pro championship", regex=False),
                            "usap",
                            "wc",
                        ),
                    ),
                ),
            ),
        ),
    )),
)
series_code = np.where(pd.Series(series_from_type, index=events_work.index).astype(str).str.strip().ne(""), series_from_type, series_fallback)
events_work["_series_code"] = pd.Series(series_code, index=events_work.index)

# Sidebar: Event Auswahl
render_sidebar_nav()
st.sidebar.header("Event Auswahl")
page_prefs = load_page_prefs("heat_analyzer")

# Live if there is something with event_date == today.
# Do not restrict by derived year to avoid hiding valid events with legacy IDs.
live_ids = live_event_ids_today(events)

if live_ids:
    mode_default = page_prefs.get("mode", "Live" if live_ids else "Archiv (Jahre)")
    mode = st.sidebar.radio("Modus", ["Live", "Archiv (Jahre)"], horizontal=True, index=(["Live", "Archiv (Jahre)"].index(mode_default) if mode_default in ["Live", "Archiv (Jahre)"] else 0), key="ha_mode")
else:
    st.sidebar.caption("Kein Live-Event erkannt – Modus bleibt auf Archiv.")
    mode = "Archiv (Jahre)"

# Which set of events is shown in the CURRENT event dropdown?
if mode == "Live":
    df_current_pool = events_work[events_work["event_id"].isin(live_ids)].copy()
else:
    years = sorted(events_work["year"].dropna().unique().tolist(), reverse=True)
    year_default = [y for y in page_prefs.get("year_sel", []) if y in years] or ([years[0]] if years else [])
    year_sel = st.sidebar.multiselect("Jahr", years, default=year_default, key="ha_year_sel")
    code_to_label = {
        "wc": "WC",
        "wch": "WM",
        "euc": "EC",
        "em": "EM",
        "usap": "USABMX",
        "ffc": "FFC",
        "scc": "SCC",
        "other": "Other",
    }
    label_to_code = {v: k for k, v in code_to_label.items()}
    available_codes = set(events_work["_series_code"].dropna().astype(str).tolist())
    type_opts = [code_to_label[c] for c in ["wc", "wch", "euc", "em", "usap", "ffc", "scc", "other"] if c in available_codes]
    type_default = [t for t in page_prefs.get("type_sel", []) if t in type_opts] or type_opts
    type_sel = st.sidebar.multiselect("Wettkampftyp", type_opts, default=type_default, key="ha_type_sel")
    df_current_pool = events_work.copy()
    if year_sel:
        df_current_pool = df_current_pool[df_current_pool["year"].isin(year_sel)].copy()
    if type_sel:
        sel_codes = [label_to_code[t] for t in type_sel if t in label_to_code]
        df_current_pool = df_current_pool[df_current_pool["_series_code"].isin(sel_codes)].copy()

df_current_pool = df_current_pool.sort_values("event_id", ascending=False)
if df_current_pool.empty:
    st.warning("Keine Events für die aktuelle Auswahl (Jahr/Wettkampftyp).")
    st.stop()

event_options = df_current_pool["label_analysis"].tolist()
default_event_index = 0
try:
    pick_counts = load_event_pick_counts(df_current_pool["event_id"].astype(str).tolist())
    for i, eid in enumerate(df_current_pool["event_id"].astype(str).tolist()):
        if pick_counts.get(eid, 0) > 0:
            default_event_index = i
            break
except Exception:
    default_event_index = 0

event_default = page_prefs.get("event_label_current")
event_index = event_options.index(event_default) if event_default in event_options else default_event_index
event_label_current = st.sidebar.selectbox("Event", event_options, index=event_index, key="ha_event_current")
selected_event_row = df_current_pool.loc[df_current_pool["label_analysis"] == event_label_current].iloc[0]
event_id = selected_event_row["event_id"]
st.sidebar.caption(f"Aktives Event: {event_id}")
persist_active_heat_analyzer_event(
    str(event_id),
    str(event_label_current),
    selected_event_row.get("event_date", ""),
)

# Analyse selection (directly under Event)
default_analysis_labels = []
if event_id in events_work["event_id"].tolist():
    default_analysis_labels = [events_work.loc[events_work["event_id"] == event_id, "label_analysis"].iloc[0]]

analysis_options = df_current_pool["label_analysis"].tolist()
analysis_defaults = [x for x in page_prefs.get("analysis_event_labels", default_analysis_labels) if x in analysis_options] or default_analysis_labels
analysis_event_labels = st.sidebar.multiselect(
    "Event (Analyse) – frei kombinierbar",
    options=analysis_options,
    default=analysis_defaults,
    key="ha_analysis_events",
)

analysis_event_labels = [x for x in analysis_event_labels if x]
analysis_event_ids = events_work.loc[events_work["label_analysis"].isin(analysis_event_labels), "event_id"].tolist()
# always include current event for training/race context
if event_id not in analysis_event_ids:
    analysis_event_ids.append(event_id)

# Filters (order: Nation, Rider, Kategorie, Geschlecht)
if "nation_filter_main" not in st.session_state:
    st.session_state["nation_filter_main"] = page_prefs.get("nation_filter_main", "")
nation = st.sidebar.text_input("Nation Filter (z.B. SUI) – leer = alle", key="nation_filter_main").strip().upper()
show_times = st.sidebar.checkbox("Zeiten anzeigen (Start/T1)", value=bool(page_prefs.get("show_times", True)), key="ha_show_times")
training_live = st.sidebar.checkbox("Training-Live Ansicht", value=bool(page_prefs.get("training_live", False)), key="ha_training_live")
filter_bad_training = st.sidebar.checkbox("Fehlmessungen filtern", value=bool(page_prefs.get("filter_bad_training", True)), disabled=not training_live, key="ha_filter_bad_training")

# Current event data
df_event = load_picks_for_event(event_id)
df_train_current = load_training_for_events([event_id]) if training_live else pd.DataFrame()
has_training_current = not df_train_current.empty
if df_event.empty and not (training_live and has_training_current):
    if mode == "Live":
        st.info("Live-Daten sind noch nicht verfügbar. Bitte später erneut laden.")
    else:
        st.warning(f"Keine Picks-Daten für {event_id}.")
    st.stop()

# Rider filter(s) - show only riders that match other filters (nation/category/gender)
# Use session_state for category/gender (widgets are rendered below, but state persists)
level_sel_state = st.session_state.get("level_sel", [])
gender_sel_state = st.session_state.get("gender_sel", [])

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

if training_live:
    df_rider_pool = df_train_current.copy()
    if nation:
        df_rider_pool = df_rider_pool[df_rider_pool["nation"].fillna("").str.upper() == nation]
    if allowed_group_ids_preview:
        df_rider_pool = filter_training_by_allowed_groups(df_rider_pool, allowed_group_ids_preview)
else:
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
        default=[x for x in page_prefs.get("rider_filter_live", []) if x in all_names],
        key="rider_filter_live",
    )
    rider_selected = "Alle"
else:
    # keep current selections visible, but allow adding more
    options_riders = sorted(set(all_names) | set(st.session_state["rider_filter"]))
    rider_selected_list = st.sidebar.multiselect(
        "Rider Filter (optional, leer = alle)",
        options=options_riders,
        default=[x for x in page_prefs.get("rider_filter", []) if x in options_riders],
        key="rider_filter",
    )
    # allow multi-select; baseline comparison only if exactly one selected
    rider_selected = rider_selected_list[0] if len(rider_selected_list) == 1 else "Alle"
    rider_live_selected = []

# Kategorie Filter
level_sel = st.sidebar.multiselect(
    "Kategorie",
    options=["Elite", "U23", "Junior"],
    default=[x for x in page_prefs.get("level_sel", []) if x in ["Elite", "U23", "Junior"]],
    key="level_sel",
)
gender_sel = st.sidebar.multiselect(
    "Geschlecht",
    options=["Men", "Women"],
    default=[x for x in page_prefs.get("gender_sel", []) if x in ["Men", "Women"]],
    key="gender_sel",
)

update_page_prefs("heat_analyzer", {
    "mode": mode,
    "year_sel": year_sel if mode != "Live" else [],
    "type_sel": type_sel if mode != "Live" else [],
    "event_label_current": event_label_current,
    "analysis_event_labels": analysis_event_labels,
    "nation_filter_main": nation,
    "show_times": show_times,
    "training_live": training_live,
    "filter_bad_training": filter_bad_training,
    "rider_filter_live": rider_live_selected,
    "rider_filter": rider_selected_list if not training_live else [],
    "level_sel": level_sel,
    "gender_sel": gender_sel,
})

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

# Apply category filters to current event (empty selection = show all)
if allowed_group_ids and not df_event.empty and "group_id" in df_event.columns:
    df_event = df_event[df_event["group_id"].isin(allowed_group_ids)].copy()

# Cache reset (keep at bottom of sidebar)
if st.sidebar.button("Cache leeren"):
    st.cache_data.clear()
    st.session_state["cache_bust"] += 1
    st.rerun()

# (Rider filter moved above)

# ----------------------------
# Training Live View
# ----------------------------
if training_live:
    st.subheader("Training Live – Zeiten (aktuelles Event)")

    def training_datetime_label(raw_label: object, raw_time: object, fallback: object = "") -> str:
        label = str(raw_label or "").strip()
        time_txt = str(raw_time or "").strip()
        fallback_txt = str(fallback or "").strip()
        if label:
            return label
        if time_txt:
            return time_txt
        return fallback_txt

    metric_options = {
        "Start to Kink": "kink",
        "Split Kink to Bottom": "bottom",
        "Start to Bottom": "start",
        "Split first Straight Bottom to T1": "t1_in",
        "Start to first Straight": "t1",
    }
    metric_label = st.selectbox("Rundenzeit anzeigen:", list(metric_options.keys()), index=0, key="live_metric")
    metric_col = metric_options[metric_label]

    df_live_scope = df_train_current.copy()
    df_live_scope = filter_training_by_allowed_groups(df_live_scope, allowed_group_ids)
    if df_live_scope.empty:
        st.info("Keine Live-Trainingsdaten mit den aktuellen Filtern.")
        st.stop()

    df_live_scope = df_live_scope.copy()
    df_live_scope["split_t1"] = df_live_scope["t1_s"] - df_live_scope["start_s"]
    if metric_col in ["kink", "bottom", "start", "t1_in", "t1"]:
        df_live_scope["metric_s"] = df_live_scope[metric_col + "_s"]
    else:
        df_live_scope["metric_s"] = df_live_scope[metric_col]

    df_live_scope = df_live_scope[df_live_scope["metric_s"].notna()].copy()
    if df_live_scope.empty:
        st.info("Keine Trainingszeiten fuer die gewaehlte Metrik vorhanden.")
        st.stop()

    custom_abs_lower: Optional[float] = None
    custom_abs_upper: Optional[float] = None
    if filter_bad_training:
        c_min, c_max = st.columns(2)
        with c_min:
            min_raw = st.text_input(
                "Min. gueltige Zeit (optional)",
                value=page_prefs.get("training_live_min_time", ""),
                key="training_live_min_time",
            ).strip()
        with c_max:
            max_raw = st.text_input(
                "Max. gueltige Zeit (optional)",
                value=page_prefs.get("training_live_max_time", ""),
                key="training_live_max_time",
            ).strip()
        try:
            custom_abs_lower = float(min_raw) if min_raw else None
        except Exception:
            custom_abs_lower = None
        try:
            custom_abs_upper = float(max_raw) if max_raw else None
        except Exception:
            custom_abs_upper = None

    filtered_training_stats: Dict[str, Dict[str, float]] = {}
    if filter_bad_training:
        df_live_scope, filtered_training_stats = flag_training_metric_outliers(
            df_live_scope,
            "metric_s",
            absolute_lower=custom_abs_lower,
            absolute_upper=custom_abs_upper,
        )
        flagged_count = int(df_live_scope["measurement_flagged"].fillna(False).sum())
        if flagged_count > 0:
            st.caption(f"Fehlmessungen markiert und aus Rankings ausgeschlossen: {flagged_count}")
    else:
        df_live_scope["measurement_flagged"] = False
        df_live_scope["measurement_flag_reason"] = ""

    for frame in (df_live_scope,):
        frame["gate_label"] = frame["gate"].fillna("").astype(str).str.strip()
        frame["clock_time"] = frame.apply(
            lambda r: extract_clock_time(r.get("gate"), r.get("source_file"), r.get("start"), r.get("t1")),
            axis=1,
        )
        frame["training_block_id"] = frame["training_block_id"].fillna("").astype(str).str.strip()
        frame["start_label"] = frame.apply(
            lambda r: training_datetime_label(r.get("training_block_label"), r.get("training_block_time"), r.get("clock_time")),
            axis=1,
        )
        frame["start_id"] = frame["training_block_id"]
        frame.loc[frame["start_id"] == "", "start_id"] = frame["gate_label"]
        frame.loc[frame["start_id"] == "", "start_id"] = frame["clock_time"]
        frame.loc[frame["start_id"] == "", "start_id"] = "Training"
        frame["start_num"] = pd.to_numeric(
            frame["gate_label"].str.extract(r"Race\s+(\d+)", expand=False),
            errors="coerce",
        )

    df_live_focus = df_live_scope.copy()
    if nation:
        df_live_focus = df_live_focus[df_live_focus["nation"].fillna("").str.upper() == nation]
    if rider_live_selected:
        df_live_focus = df_live_focus[df_live_focus["name"].isin(rider_live_selected)]
    df_live_focus = df_live_focus[df_live_focus["metric_s"].notna()].copy()
    df_live_focus_valid = df_live_focus.loc[~df_live_focus["measurement_flagged"].fillna(False)].copy()

    available_riders = sorted(df_live_focus["name"].dropna().astype(str).unique().tolist())
    if rider_live_selected:
        riders = [r for r in rider_live_selected if r in available_riders]
    else:
        riders = (
            df_live_focus_valid.groupby("name")["metric_s"]
            .min()
            .sort_values(kind="stable")
            .head(10)
            .index.tolist()
        )
        if len(available_riders) > 10:
            st.caption("Ohne Rider-Auswahl werden die schnellsten 10 Athleten angezeigt.")
    if len(riders) > 10:
        st.warning("Training Live zeigt maximal 10 Athleten. Es werden die ersten 10 deiner Auswahl verwendet.")
        riders = riders[:10]
    if not riders and not df_live_focus.empty:
        base_pick_df = df_live_focus_valid if not df_live_focus_valid.empty else df_live_focus
        riders = base_pick_df.groupby("name")["metric_s"].min().sort_values(kind="stable").head(10).index.tolist()
    if df_live_focus.empty or not riders:
        st.info("Keine Athleten passend zu Nation-/Rider-Filter in der Start-Uebersicht.")
        st.stop()

    session_rank_df = (
        df_live_scope.loc[~df_live_scope["measurement_flagged"].fillna(False)]
        .groupby(["start_id", "category_label", "name"], as_index=False)["metric_s"]
        .min()
        .sort_values(["category_label", "metric_s", "name", "start_id"], kind="stable")
    )
    session_rank_df["segment_rank"] = session_rank_df.groupby(["category_label"]).cumcount() + 1
    session_rank_map = {
        (str(r.start_id), str(r.category_label), str(r.name)): int(r.segment_rank)
        for r in session_rank_df.itertuples()
    }

    athlete_best_df = (
        df_live_scope.loc[~df_live_scope["measurement_flagged"].fillna(False)]
        .groupby(["category_label", "name"], as_index=False)["metric_s"]
        .min()
        .sort_values(["category_label", "metric_s", "name"], kind="stable")
    )
    athlete_best_df["segment_rank_best"] = athlete_best_df.groupby("category_label").cumcount() + 1
    athlete_best_by_rider = (
        df_live_scope.loc[~df_live_scope["measurement_flagged"].fillna(False)]
        .groupby(["name"], as_index=False)["metric_s"]
        .min()
        .sort_values(["metric_s", "name"], kind="stable")
    )
    athlete_best_map = {
        str(r.name): {
            "best_metric": float(r.metric_s),
            "best_rank": int(
                athlete_best_df.loc[athlete_best_df["name"] == r.name, "segment_rank_best"].min()
                if not athlete_best_df.loc[athlete_best_df["name"] == r.name, "segment_rank_best"].empty
                else 1
            ),
        }
        for r in athlete_best_by_rider.itertuples()
    }
    category_best_map = (
        athlete_best_df.groupby("category_label")["metric_s"].min().to_dict()
        if not athlete_best_df.empty
        else {}
    )

    start_sort_mode = st.selectbox(
        "Start-Uebersicht sortieren:",
        options=[
            "Nach Datum / Uhrzeit",
            "Nach Schnellster Zeit (aufsteigend)",
            "Nach Schnellster Zeit (absteigend)",
        ],
        index=0,
        key="training_live_start_sort_mode",
    )

    start_summary = (
        df_live_focus.groupby("start_id", as_index=False)
        .agg(
            DatumZeit=("first_seen_label", lambda s: next((x for x in s if str(x).strip()), "Training")),
            Kategorie=("category_label", lambda s: next((x for x in s if str(x).strip()), "")),
            Athleten=(
                "name",
                lambda s: ", ".join(
                    [
                        n
                        for n in pd.unique([str(x).strip() for x in s if str(x).strip()])
                    ][:3]
                ),
            ),
            _sort_num=("start_num", "min"),
            _sort_time=("training_block_time", lambda s: next((x for x in s if str(x).strip()), "")),
        )
    )
    start_summary["Schnellste"] = start_summary["Kategorie"].map(category_best_map)
    if start_sort_mode == "Nach Schnellster Zeit (aufsteigend)":
        start_summary = start_summary.sort_values(
            ["Schnellste", "_sort_time", "DatumZeit"],
            ascending=[True, True, True],
            na_position="last",
            kind="stable",
        )
    elif start_sort_mode == "Nach Schnellster Zeit (absteigend)":
        start_summary = start_summary.sort_values(
            ["Schnellste", "_sort_time", "DatumZeit"],
            ascending=[False, True, True],
            na_position="last",
            kind="stable",
        )
    else:
        start_summary = start_summary.sort_values(
            ["_sort_time", "_sort_num", "DatumZeit"],
            na_position="last",
            kind="stable",
        )
    start_matrix = (
        df_live_focus[df_live_focus["name"].isin(riders)]
        .sort_values(["start_id", "name", "measurement_flagged", "metric_s"], ascending=[True, True, True, True], kind="stable")
        .groupby(["start_id", "name"], as_index=False)
        .first()[["start_id", "name", "metric_s", "measurement_flagged"]]
    )

    start_rows = []
    for row in start_summary.itertuples():
        row_data = {
            "Datum / Uhrzeit": str(row.DatumZeit or "Training"),
            "Schnellste": format_seconds_3(row.Schnellste),
        }
        for rider in riders:
            best_info = athlete_best_map.get(rider)
            rider_row = start_matrix[
                (start_matrix["start_id"] == row.start_id)
                & (start_matrix["name"] == rider)
            ]
            if rider_row.empty:
                row_data[rider] = ""
                continue
            metric_val = rider_row.iloc[0]["metric_s"]
            is_flagged_measurement = bool(rider_row.iloc[0].get("measurement_flagged", False))
            category_label = str(row.Kategorie or "")
            seg_rank = session_rank_map.get((str(row.start_id), category_label, rider))
            metric_txt = format_seconds_3(metric_val)
            rank_txt = format_rank_tag(seg_rank)
            is_best_start = bool(best_info) and abs(float(metric_val) - float(best_info.get("best_metric", float("nan")))) < 1e-9
            if metric_txt and rank_txt:
                content = (
                    f"{html_lib.escape(metric_txt)} "
                    f"<span style='font-size:11px;color:#6b7280'>{html_lib.escape(rank_txt)}</span>"
                )
                if is_flagged_measurement:
                    row_data[rider] = (
                        "<span style='display:inline-block;padding:1px 4px;"
                        "background:#fbe4e6;border-radius:4px;color:#991b1b'>"
                        f"{content}</span>"
                    )
                elif is_best_start:
                    row_data[rider] = (
                        "<span style='display:inline-block;padding:1px 4px;"
                        "background:#e7f6ea;border-radius:4px'>"
                        f"{content}</span>"
                    )
                else:
                    row_data[rider] = content
            else:
                plain = html_lib.escape(metric_txt)
                if is_flagged_measurement and plain:
                    row_data[rider] = (
                        "<span style='display:inline-block;padding:1px 4px;"
                        "background:#fbe4e6;border-radius:4px;color:#991b1b'>"
                        f"{plain}</span>"
                    )
                elif is_best_start and plain:
                    row_data[rider] = (
                        "<span style='display:inline-block;padding:1px 4px;"
                        "background:#e7f6ea;border-radius:4px'>"
                        f"{plain}</span>"
                    )
                else:
                    row_data[rider] = plain
        start_rows.append(row_data)

    best_rank_row = {
        "Datum / Uhrzeit": "Segment Rank best start",
        "Schnellste": "",
    }
    for rider in riders:
        best_info = athlete_best_map.get(rider)
        best_rank_row[rider] = html_lib.escape(format_rank_tag(best_info.get("best_rank"))) if best_info else ""

    start_columns = ["Datum / Uhrzeit", "Schnellste"] + riders
    start_html = [
        "<style>",
        ".training-live-table-wrap { max-height: 540px; overflow: auto; }",
        ".training-live-table { width: 100%; border-collapse: collapse; font-size: 12px; }",
        ".training-live-table th, .training-live-table td { border: 1px solid #e6e6e6; padding: 4px 6px; text-align: center; white-space: nowrap; background: white; }",
        ".training-live-table th:first-child, .training-live-table td:first-child { text-align: left; }",
        ".training-live-table thead th { position: sticky; top: 0; z-index: 3; background: #f6f7f9; font-weight: 600; }",
        ".training-live-table tbody tr.best-rank-row td { position: sticky; top: 29px; z-index: 2; background: #f6f7f9; font-weight: 600; }",
        ".training-live-table tbody tr:hover td { background: #fafafa; }",
        "@media (prefers-color-scheme: dark) {",
        ".training-live-table th, .training-live-table td { color: #f1f3f5; background: #1b1b1b; border: 1px solid #333; }",
        ".training-live-table thead th { background: #242424; }",
        ".training-live-table tbody tr.best-rank-row td { background: #242424; }",
        ".training-live-table tbody tr:hover td { background: #232323; }",
        "}",
        "</style>",
        "<div class='training-live-table-wrap'>",
        "<table class='training-live-table'>",
        "<thead><tr>",
    ]
    for col in start_columns:
        start_html.append(f"<th>{html_lib.escape(str(col))}</th>")
    start_html.append("</tr></thead><tbody>")
    start_html.append("<tr class='best-rank-row'>")
    for col in start_columns:
        start_html.append(f"<td>{best_rank_row.get(col, '')}</td>")
    start_html.append("</tr>")
    for row_data in start_rows:
        start_html.append("<tr>")
        for col in start_columns:
            cell = row_data.get(col, "")
            if col in {"Datum / Uhrzeit", "Schnellste"}:
                cell = html_lib.escape(str(cell or ""))
            start_html.append(f"<td>{cell}</td>")
        start_html.append("</tr>")
    start_html.append("</tbody></table></div>")
    st.markdown("**Start-Uebersicht:**")
    components.html("".join(start_html), height=560, scrolling=False)

    start_display_map = {}
    for _, row in start_summary.iterrows():
        dt_label = str(row.get("DatumZeit") or "Training")
        athlete_label = str(row.get("Athleten") or "").strip()
        start_display_map[str(row.get("start_id"))] = f"{dt_label} | {athlete_label}" if athlete_label else dt_label

    start_options = start_summary["start_id"].tolist()
    selected_start_id = st.selectbox(
        "Start ansehen",
        options=start_options,
        format_func=lambda sid: start_display_map.get(str(sid), str(sid)),
        key="training_live_start_select",
    )

    df_start = df_live_scope[df_live_scope["start_id"] == selected_start_id].copy()
    df_start = df_start.sort_values(["metric_s", "name"], na_position="last", kind="stable")
    if not df_start.empty:
        st.markdown("**Teilnehmer dieses Starts:**")
        df_start["Datum / Uhrzeit"] = df_start.apply(
            lambda r: training_datetime_label(r.get("training_block_label"), r.get("training_block_time"), r.get("clock_time")),
            axis=1,
        )
        df_start["Kink"] = df_start["kink_s"].apply(format_seconds_3) if "kink_s" in df_start.columns else ""
        df_start["Start"] = df_start["start_s"].apply(format_seconds_3)
        df_start["Split"] = df_start["split_t1"].apply(format_seconds_3)
        df_start["T1"] = df_start["t1_s"].apply(format_seconds_3)
        participant_cols = ["Datum / Uhrzeit", "nation", "name", "Kink", "Start", "Split", "T1"]
        participant_cols = [c for c in participant_cols if c in df_start.columns]
        participants_view = df_start[participant_cols].rename(
            columns={
                "nation": "Nation",
                "name": "Name",
            }
        )
        participant_flags = df_start["measurement_flagged"].fillna(False).tolist()
        def _highlight_participant_rows(row: pd.Series) -> List[str]:
            row_pos = participants_view.index.get_loc(row.name)
            style = "background-color: #fbe4e6; color: #991b1b;" if participant_flags[row_pos] else ""
            return [style] * len(row)
        participant_styler = participants_view.style.apply(_highlight_participant_rows, axis=1)
        st.dataframe(participant_styler, use_container_width=True, hide_index=True)

    training_tag_scope = df_live_focus.copy()
    if not training_tag_scope.empty:
        training_tag_scope["training_block_id"] = training_tag_scope["training_block_id"].fillna("").astype(str)
        training_tag_scope["training_block_label"] = training_tag_scope["training_block_label"].fillna("").astype(str)
        training_tag_scope["training_block_time"] = training_tag_scope["training_block_time"].fillna("").astype(str)
        training_block_summary = (
            training_tag_scope.groupby("training_block_id", as_index=False)
            .agg(
                label=("training_block_label", lambda s: next((x for x in s if str(x).strip()), "Training")),
                block_time=("training_block_time", lambda s: next((x for x in s if str(x).strip()), "")),
                athletes=(
                    "name",
                    lambda s: ", ".join(
                        [
                            n
                            for n in pd.unique([str(x).strip() for x in s if str(x).strip()])
                        ][:3]
                    ),
                ),
                athlete_count=("name", lambda s: len(pd.unique([str(x).strip() for x in s if str(x).strip()]))),
            )
            .sort_values(["block_time", "label"], na_position="last", kind="stable")
        )
        training_block_options = training_block_summary["training_block_id"].tolist()
        if training_block_options:
            st.markdown("**Training Tagging:**")
            block_label_map = {}
            for _, row in training_block_summary.iterrows():
                label = training_datetime_label(row.get("label"), row.get("block_time"), "Training")
                athlete_names = str(row.get("athletes") or "").strip()
                athletes_txt = f"{int(row.get('athlete_count') or 0)} Athleten"
                pretty = f"{label} | {athlete_names} | {athletes_txt}" if athlete_names else f"{label} | {athletes_txt}"
                block_label_map[str(row["training_block_id"])] = pretty

            selected_training_block_id = st.selectbox(
                "Training-Block wählen",
                options=training_block_options,
                format_func=lambda bid: block_label_map.get(str(bid), str(bid)),
                key="training_live_tagging_block_select",
            )
            df_training_block = training_tag_scope[
                training_tag_scope["training_block_id"].astype(str) == str(selected_training_block_id)
            ].copy()
            df_training_block = df_training_block.sort_values(["name", "gate"], na_position="last", kind="stable")
            athlete_tags_train, meta_tags_train = build_training_tag_payload(df_training_block)
            if not df_training_block.empty:
                df_training_block["Datum / Uhrzeit"] = df_training_block.apply(
                    lambda r: training_datetime_label(r.get("training_block_label"), r.get("training_block_time"), r.get("clock_time")),
                    axis=1,
                )
                block_cols = [c for c in ["Datum / Uhrzeit", "nation", "name"] if c in df_training_block.columns]
                if block_cols:
                    st.dataframe(
                        df_training_block[block_cols].rename(
                            columns={
                                "nation": "Nation",
                                "name": "Name",
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )
            if athlete_tags_train:
                render_copy_buttons("Athleten", athlete_tags_train, section_style="athlete", columns=2)
                render_copy_buttons(
                    "",
                    meta_tags_train,
                    section_style="meta",
                    columns=2,
                    show_title=False,
                    show_last_copied=False,
                )
                combined_training_values: List[str] = []
                seen_training_values = set()
                for t in athlete_tags_train + meta_tags_train:
                    val = str(t.get("value", "")).strip()
                    if not val or val in seen_training_values:
                        continue
                    seen_training_values.add(val)
                    combined_training_values.append(val)
                if combined_training_values:
                    render_copy_buttons(
                        "",
                        [{"label": "Alle Begriffe (CSV)", "value": ", ".join(combined_training_values)}],
                        section_style="meta",
                        columns=1,
                        show_title=False,
                        show_last_copied=False,
                    )

    best_per_category = (
        athlete_best_df.sort_values(["category_label", "metric_s", "name"], na_position="last", kind="stable")
        .groupby("category_label", group_keys=False)
        .head(5)[["category_label", "name", "metric_s"]]
        .copy()
    )
    best_meta = (
        df_live_scope.sort_values(["category_label", "name", "metric_s", "start_s", "t1_s"], na_position="last", kind="stable")
        .groupby(["category_label", "name"], as_index=False)
        .first()[["category_label", "name", "nation", "start_s", "t1_s"]]
    )
    all_starts = (
        df_live_scope.sort_values(["category_label", "name", "start_s"], na_position="last", kind="stable")
        .groupby(["category_label", "name"], as_index=False)["start_s"]
        .agg(lambda s: " | ".join([format_seconds_3(v) for v in s.tolist() if pd.notna(v)]))
        .rename(columns={"start_s": "all_start_times"})
    )
    def _consistency_score(series: pd.Series) -> float:
        s = series.dropna().astype(float)
        if s.empty:
            return float("nan")
        std = s.std()
        if pd.isna(std):
            return float("nan")
        return round(100.0 / (1.0 + std), 1)

    train_scores = (
        df_live_scope.groupby(["category_label", "name"], as_index=False)["start_s"]
        .agg(score=_consistency_score)
    )
    best_per_category = best_per_category.merge(best_meta, on=["category_label", "name"], how="left")
    best_per_category = best_per_category.merge(all_starts, on=["category_label", "name"], how="left")
    best_per_category = best_per_category.merge(train_scores, on=["category_label", "name"], how="left")
    best_per_category["metric_s"] = best_per_category["metric_s"].apply(format_seconds_3)
    best_per_category["start_s"] = best_per_category["start_s"].apply(format_seconds_3)
    best_per_category["t1_s"] = best_per_category["t1_s"].apply(format_seconds_3)
    st.markdown(f"**Schnellste im Training pro Kategorie ({metric_label}):**")
    st.dataframe(
        best_per_category.rename(
            columns={
                "category_label": "Kategorie",
                "name": "Athlet",
                "nation": "Nation",
                "metric_s": "Best",
                "start_s": "Start",
                "t1_s": "T1",
                "all_start_times": "Alle Starts",
                "score": "Score",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.stop()

# ----------------------------
# Heats table + selection
# ----------------------------
heats = build_heats(df_event)

# nation filter affects which heats shown (based on startlist membership)
df_filter = df_event.copy()
if nation:
    df_filter = df_filter[df_filter["nation"].fillna("").str.upper() == nation]
# Rider filter affects heats list only (multi-select supported)
if rider_selected_list:
    df_filter = df_filter[df_filter["name"].isin(rider_selected_list)].copy()

heats_f = build_heats(df_filter)
heats_f = add_heat_result_flags(heats_f, df_filter)

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

heats_f["round_filter_label"] = heats_f["round_title"].apply(canonical_round_label)
round_expected_key = {
    "Round 1": 1,
    "LCQ": 2,
    "1/32 Finals": 3,
    "1/16 Finals": 4,
    "1/8 Finals": 5,
    "1/4 Finals": 6,
    "1/2 Finals": 7,
    "Final": 8,
}
heats_f["_rk_num"] = pd.to_numeric(heats_f["round_key"], errors="coerce")
heats_f["_rk_expected"] = heats_f["round_filter_label"].map(round_expected_key)
heats_f["_rk_mismatch"] = np.where(
    heats_f["_rk_expected"].notna() & heats_f["_rk_num"].notna(),
    (heats_f["_rk_num"] - heats_f["_rk_expected"]).abs(),
    999.0,
)

# Deduplicate same displayed heat when multiple poll snapshots/mappings coexist.
dedup_cols = [c for c in ["group_id", "round_filter_label", "heat_title"] if c in heats_f.columns]
if dedup_cols:
    heats_f = (
        heats_f.sort_values(
            ["_rk_mismatch", "_rk_num", "start_dt", "round_key", "heat_id"],
            na_position="last",
            kind="stable",
        )
        .drop_duplicates(subset=dedup_cols, keep="first")
        .copy()
    )

# Header + live controls
if mode == "Live":
    round_titles = (
        heats_f[["round_filter_label", "_rk_expected", "_rk_num"]]
        .dropna(subset=["round_filter_label"])
        .drop_duplicates()
        .sort_values(["_rk_expected", "_rk_num", "round_filter_label"], na_position="last", kind="stable")
    )
    round_seen = set()
    round_values = []
    for raw_label in round_titles["round_filter_label"].fillna("").tolist():
        label = canonical_round_label(raw_label)
        if not label:
            continue
        key = re.sub(r"\s+", " ", str(label)).strip().lower()
        if not key or key in round_seen:
            continue
        round_seen.add(key)
        round_values.append(label)
    round_opts = ["Alle"] + round_values
    if not round_opts:
        round_opts = ["Alle"]

    col_h1, col_h2, col_h3 = st.columns([3, 2, 3])
    with col_h1:
        st.subheader("Heats (nach Filter)")
    with col_h2:
        only_upcoming = st.checkbox("Nur anstehende Heats (Live)", value=True, key="only_upcoming_live_main")
    with col_h3:
        selected_round = st.selectbox("Runde (Live)", round_opts, index=0, key="live_round_filter")
else:
    st.subheader("Heats (nach Filter)")
    only_upcoming = False
    selected_round = "Alle"

# Round filter (for live mode)
if selected_round != "Alle":
    heats_f = heats_f[heats_f["round_filter_label"].fillna("").astype(str) == selected_round].copy()

# Upcoming filter
if only_upcoming:
    heats_f = filter_upcoming_heats(heats_f)

heats_f = heats_f.drop(columns=["_rk_num", "_rk_expected", "_rk_mismatch"], errors="ignore")

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

# Selected-heat base dataframe (single source of truth for Startliste + Tagging)
rk = int(chosen["round_key"])
hid = int(chosen["heat_id"])
gid = int(chosen["group_id"]) if pd.notna(chosen.get("group_id")) else None
chosen_round_title = chosen.get("round_title")
chosen_heat_title = chosen.get("heat_title")
df_heat_ctx, athlete_tags, meta_tags = build_heat_tag_payload(df_event, chosen)

def prepare_selected_heat_df() -> pd.DataFrame:
    df_heat_local = df_heat_ctx.copy()
    df_heat_local["name_norm"] = df_heat_local["name"].apply(norm_name)
    df_heat_local["name_key"] = df_heat_local["name_norm"].apply(
        lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else ""
    )
    return df_heat_local

_analysis_hist_cache: Dict[str, Any] = {"loaded": False, "df": pd.DataFrame()}

def get_analysis_history() -> pd.DataFrame:
    if not _analysis_hist_cache["loaded"]:
        df_hist = load_picks_for_events(analysis_event_ids) if analysis_event_ids else pd.DataFrame()
        if not df_hist.empty and allowed_group_ids:
            df_hist = df_hist[df_hist["group_id"].isin(allowed_group_ids)].copy()
        _analysis_hist_cache["df"] = df_hist
        _analysis_hist_cache["loaded"] = True
    return _analysis_hist_cache["df"].copy()

# Tagging payload from current heat only (no archive/analysis event merge)
round_tag = next((t["value"] for t in meta_tags if t["value"] in {"LCQ", "1/32Final", "1/16Final", "1/8Final", "1/4Final", "1/2Final", "Final"} or t["value"].startswith("Round")), None)
heat_tag_value = next((t["value"] for t in meta_tags if str(t["value"]).startswith("Heat")), None)
heat_tag_label = next((t["label"] for t in meta_tags if str(t["value"]).startswith("Heat")), None)
class_tag = next((t["value"] for t in meta_tags if t["value"] in {"EliteMen", "EliteWomen", "U23Men", "U23Women", "JuniorMen", "JuniorWomen"}), None)

section_options = ["Startliste - Gate Pick", "Time Analyse", "Tagging"]
section_default = page_prefs.get("active_section", section_options[0])
if section_default not in section_options:
    section_default = section_options[0]
selected_heat_section = st.segmented_control(
    "Ansicht",
    section_options,
    default=section_default,
    key="ha_active_section",
)
update_page_prefs("heat_analyzer", {"active_section": selected_heat_section})

if selected_heat_section == "Startliste - Gate Pick":
    lane_pick_enabled = has_lane_pick_data(event_id)
    st.subheader("Startliste - Lane Pick" if lane_pick_enabled else "Startliste")

    df_heat = prepare_selected_heat_df()
    df_heat = df_heat.sort_values(["pick_order"], na_position="last", kind="stable")

    start_cols = ["nation", "bib", "name", "pick_order", "rank", "chosen_lane"]
    start_cols = [c for c in start_cols if c in df_heat.columns]
    start_df = df_heat[start_cols].copy()
    start_df["name_norm"] = start_df["name"].apply(norm_name)
    start_df["name_key"] = start_df["name_norm"].apply(lambda s: " ".join(sorted(s.split())) if isinstance(s, str) else "")
    start_df["name_short"] = df_heat["name_short"].values
    start_df["Rider"] = start_df["name_short"]
    heat_rider_names = start_df["name"].dropna().astype(str).tolist() if "name" in start_df.columns else []

    # Training stats for riders in heat
    if show_times:
        # Race/Training source routing for Startliste:
        # - Round 1, day 2 of same location/series: use previous day (vortag) as context.
        # - Round 1, day 1: use training from current event only (no previous weekend spillover).
        # - Other rounds: race from current event up to selected heat.
        is_round1 = False
        rt = str(chosen.get("round_title") or "").strip().lower()
        if rt.startswith("round 1") or rt.startswith("runde 1"):
            is_round1 = True

        prev_event_id = None
        prev_is_same_loc_year = False
        if is_round1:
            try:
                current_date = int(str(event_id)[:8])
                current_year = str(event_id)[:4]
                current_loc = (
                    events.loc[events["event_id"] == event_id, "loc_clean"].iloc[0]
                    if "loc_clean" in events.columns and (events["event_id"] == event_id).any()
                    else ""
                )
                current_row = events.loc[events["event_id"] == event_id].head(1)
                current_label = ""
                if not current_row.empty:
                    current_label = str(current_row.iloc[0].get("label_short", "") or current_row.iloc[0].get("display_name", ""))
                m_curr = re.search(r"ROUND\\s*(\\d+)", current_label, flags=re.IGNORECASE)
                current_round_num = int(m_curr.group(1)) if m_curr else None
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
                    ev = ev[ev["event_id"].astype(str).str.slice(0, 4) == current_year]
                    if not ev.empty:
                        ev["event_date_num"] = ev["event_id"].astype(str).str.slice(0, 8).astype(int)
                        prevs = ev[ev["event_date_num"] < current_date].sort_values("event_date_num")
                        if not prevs.empty:
                            # Prefer same-location previous round in same year (ROUND X-1).
                            if current_round_num is not None:
                                prevs["round_num"] = (
                                    prevs["label_short"]
                                    .fillna(prevs["display_name"])
                                    .astype(str)
                                    .str.extract(r"ROUND\\s*(\\d+)", flags=re.IGNORECASE)[0]
                                )
                                prevs["round_num"] = pd.to_numeric(prevs["round_num"], errors="coerce")
                                prev_round = prevs[prevs["round_num"] == (current_round_num - 1)]
                                if not prev_round.empty:
                                    prev_event_id = str(prev_round.iloc[-1]["event_id"])
                            if not prev_event_id:
                                prev_event_id = str(prevs.iloc[-1]["event_id"])
                            prev_is_same_loc_year = bool(prev_event_id)
            except Exception:
                prev_event_id = None
                prev_is_same_loc_year = False

        training_source_note = "Training-Zeiten: aktuelles Event (Gate Practice)"
        # Small values in Startliste:
        # - Prefer actual training_times from the current event.
        # - Only fall back to previous event at same location/year if current training is missing.
        df_train = load_training_for_events([event_id], rider_names=heat_rider_names)
        if not df_train.empty:
            if gid in GROUP_MAP:
                df_train = df_train[df_train["training_group_label"] == GROUP_MAP[gid]].copy()
            if not df_train.empty:
                df_train, _ = filter_training_metric_outliers(
                    df_train,
                    "start_s",
                    absolute_lower=custom_abs_lower,
                    absolute_upper=custom_abs_upper,
                )
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
        elif is_round1 and prev_is_same_loc_year and prev_event_id:
            df_prev_small = load_picks_for_event(prev_event_id)
            if not df_prev_small.empty:
                prev_small = race_stats(df_prev_small)
                if not prev_small.empty:
                    prev_small = prev_small[["name_norm", "best_start", "best_t1", "avg_top3_start", "avg_top3_t1", "cons_score"]]
                    start_df = start_df.merge(prev_small, on="name_norm", how="left")
                    start_df = start_df.rename(
                        columns={
                            "best_start": "train_best_start",
                            "best_t1": "train_best_t1",
                            "avg_top3_start": "train_avg3_start",
                            "avg_top3_t1": "train_avg3_t1",
                            "cons_score": "train_cons_score",
                        }
                    )
                    training_source_note = "Training-Zeiten: Fallback vorheriges Event gleiche Location (gleiches Jahr, gleiche Serie)"

        if is_round1:
            df_hist_all = get_analysis_history()
            df_race_hist = df_hist_all.copy() if not df_hist_all.empty else pd.DataFrame()
            if prev_is_same_loc_year and prev_event_id:
                if df_race_hist.empty or prev_event_id not in df_race_hist["event_id"].unique():
                    df_prev = load_picks_for_event(prev_event_id)
                    df_race_hist = df_prev.copy() if not df_prev.empty else df_race_hist.iloc[0:0].copy()
                else:
                    df_race_hist = df_race_hist[df_race_hist["event_id"] == prev_event_id].copy()
                race_source_note = "Race-Zeiten: vorheriges Event gleiche Location (gleiches Jahr, gleiche Serie)"
            else:
                df_race_hist = df_race_hist.iloc[0:0].copy()
                race_source_note = "Race-Zeiten: keine Daten (kein passendes Vor-Event gleiche Location/Jahr)"
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

        # Baseline: enable comparison as long as exactly one filtered athlete is in this heat.
        baseline_rider_name = None
        selected_in_heat = []
        nation_selected_in_heat = []
        if rider_selected_list:
            heat_rider_set = set(start_df["name"].dropna().astype(str).tolist())
            selected_in_heat = [name for name in rider_selected_list if name in heat_rider_set]
            if len(selected_in_heat) == 1:
                baseline_rider_name = selected_in_heat[0]
        elif nation and "nation" in start_df.columns:
            nation_selected_in_heat = (
                start_df.loc[
                    start_df["nation"].fillna("").astype(str).str.upper() == nation,
                    "name",
                ]
                .dropna()
                .astype(str)
                .drop_duplicates()
                .tolist()
            )
            if len(nation_selected_in_heat) == 1:
                baseline_rider_name = nation_selected_in_heat[0]

        baseline = {}
        if baseline_rider_name:
            base_rows = start_df[start_df["name"] == baseline_rider_name]
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
        view["is_baseline"] = view["name"] == baseline_rider_name

        # Heat Rank logic:
        # - if timing exists in selected heat: compute rank locally by time (1..8)
        # - if no timing: only show provided rank when heat is finished/official (and in 1..8)
        view["heat_rank_display"] = pd.NA
        try:
            tmp_rank = df_heat.copy()
            tmp_rank["time_s"] = tmp_rank["time"].apply(parse_time_to_seconds)
            has_timed_rows = tmp_rank["time_s"].notna().any()

            if has_timed_rows:
                tmp_rank = tmp_rank[tmp_rank["time_s"].notna()].copy()
                sort_cols = [c for c in ["time_s", "pick_order", "lane_idx", "name"] if c in tmp_rank.columns]
                tmp_rank = tmp_rank.sort_values(sort_cols, na_position="last", kind="stable")
                tmp_rank["heat_rank_calc"] = range(1, len(tmp_rank) + 1)
                tmp_rank["heat_rank_calc"] = pd.to_numeric(tmp_rank["heat_rank_calc"], errors="coerce")
                tmp_rank.loc[(tmp_rank["heat_rank_calc"] < 1) | (tmp_rank["heat_rank_calc"] > 8), "heat_rank_calc"] = pd.NA

                if "bib" in view.columns and "bib" in tmp_rank.columns:
                    view["_rk_key"] = view["bib"].fillna("").astype(str).str.strip()
                    tmp_rank["_rk_key"] = tmp_rank["bib"].fillna("").astype(str).str.strip()
                else:
                    view["_rk_key"] = view["name_full"].fillna("").astype(str).str.strip()
                    tmp_rank["_rk_key"] = tmp_rank["name"].fillna("").astype(str).str.strip()

                rank_map = tmp_rank.drop_duplicates("_rk_key").set_index("_rk_key")["heat_rank_calc"].to_dict()
                view["heat_rank_display"] = view["_rk_key"].map(rank_map)
                view = view.drop(columns=["_rk_key"], errors="ignore")
            else:
                heat_status = str(chosen.get("heat_status") or "").strip().lower()
                is_finished_heat = heat_status in NOT_UPCOMING_STATUS
                if is_finished_heat and "rank" in view.columns:
                    view["heat_rank_display"] = pd.to_numeric(view["rank"], errors="coerce")
                    view.loc[
                        (view["heat_rank_display"] < 1) | (view["heat_rank_display"] > 8),
                        "heat_rank_display",
                    ] = pd.NA
                else:
                    view["heat_rank_display"] = pd.NA
        except Exception:
            view["heat_rank_display"] = pd.NA

        # Avoid duplicate Rider column
        if "name" in view.columns and "Rider" in view.columns:
            view = view.drop(columns=["name"])
        view = view.rename(columns={"bib": "Plate", "name": "Rider"})
        view["Heat Rank"] = pd.to_numeric(view["heat_rank_display"], errors="coerce").astype("Int64")
        view = view.drop(columns=["heat_rank_display"], errors="ignore")

        if "name_short" in view.columns:
            view["Rider"] = view["name_short"]
        view["prev. Best Start"] = view.apply(
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
            "prev. Best Start",
            "Ø3 Start",
            "Best T1",
            "Ø3 T1",
            "Score",
            "Heat Rank",
        ]
        if lane_pick_enabled:
            show_cols.insert(show_cols.index("Heat Rank"), "chosen_lane")
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
        if baseline_rider_name:
            st.caption("Farben: Rot = schneller als gewählter Rider, Grün = langsamer. Vergleich nur für Start-Zeiten.")
        elif len(selected_in_heat) > 1:
            st.caption("Farben deaktiviert: Mehrere gefilterte Athleten sind im selben Heat.")
        elif len(nation_selected_in_heat) > 1:
            st.caption("Farben deaktiviert: Mehrere Athleten der gefilterten Nation sind im selben Heat.")
    else:
        start_df_simple = start_df.copy()
        if "name_short" in start_df_simple.columns:
            start_df_simple["Rider"] = start_df_simple["name_short"]
        if "name" in start_df_simple.columns:
            start_df_simple = start_df_simple.drop(columns=["name"])
        start_df_simple = start_df_simple.rename(columns={"bib": "Plate"})
        # drop any accidental duplicate columns
        start_df_simple = start_df_simple.loc[:, ~start_df_simple.columns.duplicated()]
        simple_cols = ["nation", "Plate", "Rider", "pick_order"]
        if lane_pick_enabled and "chosen_lane" in start_df_simple.columns:
            simple_cols.append("chosen_lane")
        start_df_simple = start_df_simple[simple_cols]
        st.table(start_df_simple)

    # --- startlist tab: analysis tables in requested order ---
    df_hist_heat = get_analysis_history() if lane_pick_enabled else pd.DataFrame()
    if lane_pick_enabled and (not df_hist_heat.empty) and (not df_heat.empty):
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
        if not lane_pick_enabled:
            st.info("Lane-Picks werden für diese Serie aktuell nicht angezeigt (nur World Cup).")
        else:
            st.info("Keine Lane-/Zusammenfassung verfügbar (Heat-Auswahl oder Picks fehlen).")

elif selected_heat_section == "Time Analyse":
    df_heat = prepare_selected_heat_df()
    st.markdown("<div id='time-analyse-anchor'></div>", unsafe_allow_html=True)
    col_m1, col_m2 = st.columns([5, 1])
    with col_m1:
        mode_time = st.radio("Modus", ["Heat", "Athleten"], index=0, horizontal=True)
    with col_m2:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button("Refresh", key="time_analyse_refresh_btn", use_container_width=True):
            st.cache_data.clear()
            st.session_state["cache_bust"] += 1
            st.session_state["scroll_to_time_tab"] = True
            st.rerun()

    if st.session_state.get("scroll_to_time_tab", False):
        components.html(
            """
            <script>
            const doc = window.parent.document;
            const el = doc.getElementById("time-analyse-anchor");
            if (el) {
              el.scrollIntoView({behavior: "instant", block: "start"});
            }
            </script>
            """,
            height=0,
        )
        st.session_state["scroll_to_time_tab"] = False

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
            "1/32 final": 3,
            "1/32 finals": 3,
            "1/32 finale": 3,
            "1/16 final": 4,
            "1/16 finals": 4,
            "1/16 finale": 4,
            "1/8 final": 5,
            "1/8 finals": 5,
            "1/8 finale": 5,
            "1/4 final": 6,
            "1/4 finals": 6,
            "1/4 finale": 6,
            "1/2 final": 7,
            "1/2 finals": 7,
            "1/2 finale": 7,
            "final": 8,
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
                "1/32 final": "1/32",
                "1/32 finals": "1/32",
                "1/32 finale": "1/32",
                "1/16 final": "1/16",
                "1/16 finals": "1/16",
                "1/16 finale": "1/16",
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
        df_hist = get_analysis_history()
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

elif selected_heat_section == "Tagging":
    st.subheader("Tagging")
    st.caption("One-Tap Copy für CoachNow (pro Tap genau ein Tag ins Clipboard).")

    video_match_time = st.session_state.get("video_match_time", "")
    video_match_tags = st.session_state.get("video_match_tags", "")
    with st.expander("CoachNow Match Debug / Backfill", expanded=False):
        st.caption(
            "Nur fuer gezielte CoachNow-Matches oder Backfill. Im Live-Betrieb normalerweise nicht noetig."
        )
        match_col_1, match_col_2 = st.columns([2, 3])
        with match_col_1:
            video_match_time = st.text_input(
                "Videozeit (HH:MM:SS)",
                value=video_match_time,
                placeholder="15:38:50",
                key="video_match_time",
                help="Nutze den Zeit-Tag des CoachNow-Videos als Heat-Anker.",
            )
        with match_col_2:
            video_match_tags = st.text_input(
                "CoachNow Tags (optional CSV)",
                value=video_match_tags,
                placeholder="RossCullen, Final, EliteMen",
                key="video_match_tags",
                help="Hilft bei identischen Startzeiten. Athlete-/Meta-Tags werden gegen den Heat verglichen.",
            )

        match_candidates = build_heat_match_candidates(
            heats_f,
            df_event,
            video_match_time,
            video_match_tags,
            time_source="start_time_string",
        )
        if video_match_time.strip():
            if not match_candidates:
                st.info("Keine Heats mit gueltiger Uhrzeit im aktuellen Filter gefunden.")
            else:
                best = match_candidates[0]
                tied = [
                    cand
                    for cand in match_candidates
                    if cand["diff_seconds"] == best["diff_seconds"] and cand["tag_overlap"] == best["tag_overlap"]
                ]
                if len(tied) == 1:
                    st.caption(
                        f"Bester Match: {best['label']} | Delta {format_match_delta(best['diff_seconds'])} | Tag-Overlap {best['tag_overlap']} | Quelle {best['time_source']}"
                    )
                else:
                    st.warning(
                        "Mehrere Heats sind gleich gut passend. Nutze die Buttons unten, um den richtigen Heat zu waehlen."
                    )

                for idx, candidate in enumerate(match_candidates[:5], start=1):
                    c_info, c_delta, c_overlap, c_action = st.columns([7, 2, 2, 2])
                    c_info.write(candidate["label"])
                    c_delta.write(f"Δ {format_match_delta(candidate['diff_seconds'])}")
                    c_overlap.write(f"{candidate['tag_overlap']} Tags")
                    if c_action.button("Waehlen", key=f"video_match_select_{idx}_{candidate['option']}"):
                        st.session_state["heat_choice"] = candidate["option"]
                        st.rerun()

    any_section = False
    meta_tags: List[Dict[str, str]] = []

    # 1) Athleten
    if athlete_tags:
        any_section = True
        render_copy_buttons("Athleten", athlete_tags, section_style="athlete", columns=2)
    else:
        st.info("No startlist loaded für den gewählten Heat.")

    # 2) Round, 3) Heat, 4) Class (direkt unter Athleten)
    if round_tag:
        meta_tags.append({"label": round_tag, "value": round_tag})

    if class_tag:
        meta_tags.append({"label": class_tag, "value": class_tag})

    if meta_tags:
        any_section = True
        render_copy_buttons(
            "",
            meta_tags,
            section_style="meta",
            columns=3,
            show_title=False,
            show_last_copied=False,
        )

    # 5) Sammel-Tag (alle sichtbaren Begriffe kommasepariert)
    combined_values: List[str] = []
    seen_combined = set()
    for t in athlete_tags + meta_tags:
        val = str(t.get("value", "")).strip()
        if not val or val in seen_combined:
            continue
        seen_combined.add(val)
        combined_values.append(val)

    if combined_values:
        combined_csv = ", ".join(combined_values)
        render_copy_buttons(
            "",
            [{"label": "Alle Begriffe (CSV)", "value": combined_csv}],
            section_style="meta",
            columns=1,
            show_title=False,
            show_last_copied=False,
        )

    if not any_section:
        st.info("Keine Tags für den aktuellen Heat verfügbar.")
