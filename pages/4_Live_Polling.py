import datetime
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
from typing import Dict, List, Tuple

import pandas as pd
import requests
import streamlit as st
from access_control import render_sidebar_nav, require_page_access

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_DIR, "bmx.db")
ARCHIVE_DB_PATH = os.path.join(REPO_DIR, "bmx_archive.db")
EVENT_TYPE_OPTIONS = ["WC", "WM", "EC", "EM", "USABMX", "FFC", "SCC", "Other"]

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


def db_path_exists(path: str) -> bool:
    return bool(path) and os.path.exists(path)


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
    rc, out, _ = run_cmd(
        [systemctl_bin(), "list-units", "--type=service", "--all", "bmx-poller@*.service", "--no-legend"]
    )
    rows: List[Dict[str, str]] = []
    if rc != 0 or not out:
        return rows
    for ln in out.splitlines():
        parts = ln.split()
        if not parts:
            continue
        rows.append(
            {
                "unit": parts[0],
                "load": parts[1] if len(parts) > 1 else "",
                "active": parts[2] if len(parts) > 2 else "",
                "sub": parts[3] if len(parts) > 3 else "",
            }
        )
    return rows


def tail_poller_logs(instance: str, lines: int = 60) -> str:
    rc, out, err = run_cmd([journalctl_bin(), "-u", poller_service_name(instance), "-n", str(lines), "--no-pager"])
    if rc != 0:
        return err or "Keine Logs verfügbar."
    return out or "Keine Logs verfügbar."


def read_env_file(instance: str) -> Dict[str, str]:
    path = os.path.join(POLLER_ENV_DIR, f"{instance}.env")
    data: Dict[str, str] = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or "=" not in line or line.startswith("#"):
                    continue
                key, val = line.split("=", 1)
                data[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        return {}
    return data


def list_known_instances() -> List[str]:
    known = set()
    for row in list_poller_units():
        unit = str(row.get("unit") or "")
        m = re.match(r"bmx-poller@(.+)\.service$", unit)
        if m and str(row.get("active") or "") == "active":
            known.add(m.group(1))
    for path in glob.glob(os.path.join(POLLER_ENV_DIR, "*.env")):
        known.add(os.path.basename(path)[:-4])
    return sorted(known)


def delete_poller_instance(instance: str) -> Tuple[bool, str]:
    env_path = os.path.join(POLLER_ENV_DIR, f"{instance}.env")
    unit = poller_service_name(instance)
    run_cmd([systemctl_bin(), "stop", unit])
    run_cmd([systemctl_bin(), "disable", unit])
    run_cmd([systemctl_bin(), "reset-failed", unit])
    try:
        if os.path.exists(env_path):
            os.remove(env_path)
    except Exception as e:
        return False, f"Env-Datei konnte nicht gelöscht werden: {e}"
    run_cmd([systemctl_bin(), "daemon-reload"])
    return True, f"Gelöscht: {unit}"


def _extract_attr_payload(text: str, attr: str):
    for quote in ['"', "'"]:
        token = f"{attr}={quote}"
        start = text.find(token)
        if start == -1:
            continue
        start += len(token)
        end = text.find(quote, start)
        if end == -1:
            continue
        try:
            return json.loads(html.unescape(text[start:end]))
        except Exception:
            return None
    return None


@st.cache_data(ttl=60)
def resolve_event_label(source: str, env_vals: Dict[str, str]) -> str:
    source = (source or "").strip().lower()
    db_path = env_vals.get("DB_PATH", DEFAULT_DB_PATH)
    if source == "sqorz":
        event_id = env_vals.get("EVENT_ID", "").strip()
        if event_id and db_path_exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                row = conn.execute("SELECT display_name FROM events WHERE event_id = ? LIMIT 1", (event_id,)).fetchone()
                conn.close()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                pass
        return event_id
    if source == "jstiming":
        raw = env_vals.get("RACE_URLS") or env_vals.get("TRAINING_URLS") or ""
        url = raw.splitlines()[0].strip() if raw else ""
        if not url:
            return ""
        try:
            headers = {"X-Inertia": "true", "X-Requested-With": "XMLHttpRequest"}
            resp = requests.get(url, headers=headers, timeout=10)
            payload = None
            if "application/json" in (resp.headers.get("Content-Type") or ""):
                payload = resp.json()
            else:
                text = resp.text
                payload = _extract_attr_payload(text, "data-page") or _extract_attr_payload(text, "data-payload")
            if not isinstance(payload, dict):
                return ""
            props = payload.get("props") or payload.get("view", {}).get("properties") or payload.get("view", {}).get("props") or payload
            event = props.get("event", {}) or {}
            return str(event.get("name") or "").strip()
        except Exception:
            return ""
    if source == "chronorace":
        return env_vals.get("EVENTS", "").splitlines()[0].strip()
    if source == "bmxracer":
        event_id = env_vals.get("EVENT_ID", "").strip()
        db_path = env_vals.get("DB_PATH", DEFAULT_DB_PATH)
        if event_id and db_path_exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                row = conn.execute("SELECT display_name FROM events WHERE event_id = ? LIMIT 1", (event_id,)).fetchone()
                conn.close()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                pass
        return env_vals.get("DISPLAY_NAME", "").strip() or event_id
    return ""


def ensure_poller_template_installed() -> Tuple[bool, str]:
    if os.path.exists(POLLER_UNIT_TEMPLATE):
        return True, "Service-Template vorhanden."
    src = os.path.join(REPO_DIR, "deploy", "bmx-poller@.service.example")
    if not os.path.exists(src):
        return False, f"Template-Datei fehlt: {src}"
    try:
        shutil.copyfile(src, POLLER_UNIT_TEMPLATE)
        run_cmd([systemctl_bin(), "daemon-reload"])
        return True, f"Template installiert: {POLLER_UNIT_TEMPLATE}"
    except Exception as e:
        return False, f"Template konnte nicht installiert werden: {e}"


def ensure_state(key: str, default):
    if key not in st.session_state:
        st.session_state[key] = default


st.set_page_config(page_title="Live Polling", layout="wide", initial_sidebar_state="expanded")
require_page_access(["admin"], "Live Polling")
render_sidebar_nav()

st.title("Live Polling")
st.caption("Mehrere Polling-Services parallel starten (Sqorz, JSTiming, Chronorace, BMX-Racer Weinfelden).")

if not running_on_systemd_host() or not systemctl_available():
    st.error("Diese Seite funktioniert nur auf dem VPS mit systemd.")
    st.stop()

if st.button("Service-Liste aktualisieren"):
    st.rerun()

units = list_poller_units()
if units:
    st.dataframe(pd.DataFrame(units), use_container_width=True, hide_index=True)
else:
    st.info("Noch keine bmx-poller Services gefunden.")

known_instances = list_known_instances()
if known_instances:
    st.subheader("Poller Verwalten")
    for instance in known_instances:
        env_vals = read_env_file(instance)
        status = poller_status(instance)
        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([2, 2, 1, 1, 1])
            with c1:
                st.markdown(f"**{instance}**")
                st.caption(f"{poller_service_name(instance)}")
            with c2:
                source = env_vals.get("POLLER_KIND", "unbekannt")
                target = env_vals.get("RACE_URLS") or env_vals.get("TRAINING_URLS") or env_vals.get("EVENT_URL") or env_vals.get("EVENTS") or ""
                target = target.splitlines()[0] if target else ""
                event_label = resolve_event_label(source, env_vals)
                st.caption(f"Quelle: {source}")
                if target:
                    st.caption(target)
                if event_label:
                    st.caption(f"Event: {event_label}")
            with c3:
                if st.button("Stop", key=f"manage_stop_{instance}", use_container_width=True):
                    rc_stop, out_stop, err_stop = run_cmd([systemctl_bin(), "stop", poller_service_name(instance)])
                    rc_disable, out_disable, err_disable = run_cmd([systemctl_bin(), "disable", poller_service_name(instance)])
                    if rc_stop == 0:
                        st.success(f"Gestoppt: {poller_service_name(instance)}")
                        st.rerun()
                    else:
                        st.error(err_stop or out_stop or err_disable or out_disable or "Stop fehlgeschlagen.")
            with c4:
                if st.button("Start", key=f"manage_start_{instance}", use_container_width=True):
                    run_cmd([systemctl_bin(), "daemon-reload"])
                    rc_enable, out_enable, err_enable = run_cmd([systemctl_bin(), "enable", poller_service_name(instance)])
                    rc_restart, out_restart, err_restart = run_cmd([systemctl_bin(), "restart", poller_service_name(instance)])
                    if rc_enable == 0 and rc_restart == 0:
                        st.success(f"Gestartet: {poller_service_name(instance)}")
                        st.rerun()
                    else:
                        st.error(err_restart or out_restart or err_enable or out_enable or "Start fehlgeschlagen.")
            with c5:
                if st.button("Löschen", key=f"manage_delete_{instance}", use_container_width=True):
                    ok_delete, msg_delete = delete_poller_instance(instance)
                    if ok_delete:
                        st.success(msg_delete)
                        st.rerun()
                    else:
                        st.error(msg_delete)
            st.caption(
                f"Status: {status.get('active', '?')} / {status.get('sub', '?')} | "
                f"Result: {status.get('result', '')} | Start: {status.get('started', '')}"
            )
            if st.button("Logs", key=f"manage_logs_{instance}", use_container_width=False):
                st.code(tail_poller_logs(instance), language="log")

if "live_poller_form_ids" not in st.session_state:
    st.session_state["live_poller_form_ids"] = [1]

col_add, col_template = st.columns([1, 2])
with col_add:
    if st.button("Konfiguration hinzufügen", use_container_width=True):
        nxt = max(st.session_state["live_poller_form_ids"]) + 1 if st.session_state["live_poller_form_ids"] else 1
        st.session_state["live_poller_form_ids"].append(nxt)
        st.rerun()
with col_template:
    if st.button("Service-Template installieren", use_container_width=True):
        ok, msg = ensure_poller_template_installed()
        if ok:
            st.success(msg)
        else:
            st.error(msg)

for i, fid in enumerate(st.session_state["live_poller_form_ids"], start=1):
    with st.expander(f"Konfiguration {i}", expanded=(i == 1)):
        source_key = f"poll_src_{fid}"
        ensure_state(source_key, "sqorz")
        source_label = st.selectbox("Quelle", ["Sqorz", "JSTiming", "Chronorace", "BMX-Racer Weinfelden"], key=f"poll_src_label_{fid}")
        source = {"Sqorz": "sqorz", "JSTiming": "jstiming", "Chronorace": "chronorace", "BMX-Racer Weinfelden": "bmxracer"}[source_label]

        ensure_state(f"poll_name_{fid}", f"{source}-{fid}")
        raw_name = st.text_input("Service Name", key=f"poll_name_{fid}")
        instance = poller_instance_slug(raw_name)
        st.caption(f"Unit: {poller_service_name(instance) if instance else '–'}")

        poll_interval = int(st.number_input("Intervall (Sekunden)", min_value=5, max_value=600, value=15, step=5, key=f"poll_int_{fid}"))
        db_key = f"poll_db_{fid}"
        db_mode_key = f"poll_db_mode_{fid}"
        ensure_state(db_key, DEFAULT_DB_PATH)

        env_values: Dict[str, str] = {
            "POLLER_KIND": source,
            "POLL_INTERVAL": str(poll_interval),
        }
        errors: List[str] = []

        if source == "sqorz":
            series_label = st.selectbox("Wettkampf Typ", ["USABMX", "FFC", "SCC", "Other"], key=f"poll_sqorz_series_{fid}")
            series_code_map = {"USABMX": "usap", "FFC": "ffc", "SCC": "scc", "Other": "other"}
            series_code = series_code_map.get(series_label, "other")
            if series_label == "Other":
                series_code_custom = st.text_input(
                    "Rennserie-Code (für event_id, z.B. other/france)",
                    key=f"poll_sqorz_series_custom_{fid}",
                    value="other",
                ).strip().lower()
                if series_code_custom:
                    series_code = re.sub(r"[^a-z0-9]+", "", series_code_custom) or "other"

            event_url = st.text_input(
                "Event URL",
                key=f"poll_sqorz_url_{fid}",
                placeholder="https://our.sqorz.com/org/.../event/<id>/classes",
            ).strip()
            parsed_id = sqorz_event_id_from_url(event_url)
            if parsed_id:
                st.caption(f"Erkannte Event-ID: {parsed_id}")

            event_target = st.text_input(
                "Ziel event_id in DB",
                key=f"poll_sqorz_eid_{fid}",
                placeholder="z.B. 20260228_ffc_6996ddc7_bmx",
            ).strip()
            if not event_target and parsed_id:
                today = datetime.date.today().strftime("%Y%m%d")
                event_target = f"{today}_{series_code}_{parsed_id[:8]}_bmx"

            all_classes = st.checkbox("Alle Klassen ingestieren", value=False, key=f"poll_sqorz_all_{fid}")
            class_filters = st.text_area(
                "Class Filter (eine pro Zeile, falls nicht alle Klassen)",
                value="Men Pro\nWomen Pro\nMen Elite\nWomen Elite",
                key=f"poll_sqorz_cls_{fid}",
                height=90,
            )

            if not event_url:
                errors.append("Event URL fehlt.")
            if not event_target:
                errors.append("Ziel event_id fehlt.")

            env_values["EVENT_URL"] = event_url
            env_values["EVENT_ID"] = event_target
            env_values["SERIES"] = series_label
            env_values["SERIES_CODE"] = series_code
            env_values["EVENT_TYPE"] = series_label
            env_values["ALL_CLASSES"] = "1" if all_classes else "0"
            if not all_classes:
                env_values["CLASS_FILTERS"] = class_filters

        elif source == "jstiming":
            event_type = st.selectbox("Wettkampf Typ", EVENT_TYPE_OPTIONS, index=7, key=f"poll_jst_event_type_{fid}")
            race_urls = st.text_area("Race URLs (eine pro Zeile)", key=f"poll_jst_race_{fid}", height=90)
            training_urls = st.text_area("Training URLs (eine pro Zeile)", key=f"poll_jst_training_{fid}", height=90)
            all_classes = st.checkbox(
                "Alle Klassen (Archiv)",
                value=False,
                key=f"poll_jst_all_{fid}",
                help="Nur fuer Archiv/Backfill sinnvoll. Dafuer am besten eine separate DB wie bmx_archive.db verwenden.",
            )
            verbose = st.checkbox("Verbose Logs", value=False, key=f"poll_jst_verbose_{fid}")
            if all_classes:
                if (
                    st.session_state.get(db_mode_key) != "archive"
                    and st.session_state.get(db_key, DEFAULT_DB_PATH) in ("", DEFAULT_DB_PATH, ARCHIVE_DB_PATH)
                ):
                    st.session_state[db_key] = ARCHIVE_DB_PATH
                st.session_state[db_mode_key] = "archive"
            else:
                if st.session_state.get(db_mode_key) == "archive" and st.session_state.get(db_key) == ARCHIVE_DB_PATH:
                    st.session_state[db_key] = DEFAULT_DB_PATH
                st.session_state[db_mode_key] = "main"
            if all_classes:
                st.caption("Hinweis: Fuer Archiv-Backfills eine separate DB verwenden, z. B. /opt/bmx/bmx-worldcup-analyse/bmx_archive.db")
            if not race_urls.strip() and not training_urls.strip():
                errors.append("Mindestens eine Race- oder Training-URL ist nötig.")
            env_values["RACE_URLS"] = race_urls
            env_values["TRAINING_URLS"] = training_urls
            env_values["EVENT_TYPE"] = event_type
            env_values["ALL_CLASSES"] = "1" if all_classes else "0"
            env_values["VERBOSE"] = "1" if verbose else "0"

        elif source == "chronorace":
            events = st.text_area(
                "Events (slug/event-id, eine pro Zeile)",
                key=f"poll_chrono_events_{fid}",
                height=90,
            )
            workers = int(st.number_input("Workers", min_value=1, max_value=24, value=6, step=1, key=f"poll_chrono_workers_{fid}"))
            if not events.strip():
                errors.append("Mindestens ein Event ist nötig.")
            env_values["EVENTS"] = events
            env_values["WORKERS"] = str(workers)
        else:
            url = st.text_input(
                "Display URL",
                key=f"poll_bmxracer_url_{fid}",
                value="https://weinfelden.bmx-racer.com/display.php?nr=1",
            ).strip()
            today = datetime.date.today().strftime("%Y%m%d")
            default_event_id = f"{today}_Weinfelden_Training"
            event_id = st.text_input(
                "Ziel event_id in DB",
                key=f"poll_bmxracer_event_id_{fid}",
                value=default_event_id,
            ).strip()
            display_name = st.text_input(
                "Display Name",
                key=f"poll_bmxracer_display_name_{fid}",
                value="Weinfelden Training",
            ).strip()
            location = st.text_input(
                "Location",
                key=f"poll_bmxracer_location_{fid}",
                value="Weinfelden",
            ).strip()
            country = st.text_input(
                "Country",
                key=f"poll_bmxracer_country_{fid}",
                value="SUI",
            ).strip().upper()
            st.caption("Dieser Mapper ist aktuell nur für weinfelden.bmx-racer.com gültig.")
            if not url:
                errors.append("Display URL fehlt.")
            if not event_id:
                errors.append("Ziel event_id fehlt.")
            if "weinfelden.bmx-racer.com" not in url.lower():
                errors.append("Aktuell wird nur weinfelden.bmx-racer.com unterstützt.")
            env_values["URL"] = url
            env_values["EVENT_ID"] = event_id
            env_values["DISPLAY_NAME"] = display_name
            env_values["LOCATION"] = location
            env_values["COUNTRY"] = country

        db_help = None
        if source == "jstiming" and st.session_state.get(f"poll_jst_all_{fid}", False):
            db_help = "Automatisch auf Archiv-DB gesetzt. Kann bei Bedarf ueberschrieben werden."
        db_path = st.text_input("DB Pfad", key=db_key, help=db_help)
        env_values["DB_PATH"] = db_path

        c1, c2, c3 = st.columns(3)
        if c1.button("Start/Update", key=f"poll_start_{fid}", use_container_width=True):
            if not instance:
                st.error("Service Name fehlt.")
            elif errors:
                st.error(" ".join(errors))
            else:
                ok, msg = ensure_poller_template_installed()
                if not ok:
                    st.error(msg)
                else:
                    env_path = write_poller_env(instance, env_values)
                    run_cmd([systemctl_bin(), "daemon-reload"])
                    rc_enable, out_enable, err_enable = run_cmd([systemctl_bin(), "enable", poller_service_name(instance)])
                    rc_restart, out_restart, err_restart = run_cmd([systemctl_bin(), "restart", poller_service_name(instance)])
                    if rc_enable == 0 and rc_restart == 0:
                        st.success(f"Gestartet: {poller_service_name(instance)} ({env_path})")
                    else:
                        st.error(err_restart or out_restart or err_enable or out_enable or "Start fehlgeschlagen.")

        if c2.button("Stop", key=f"poll_stop_{fid}", use_container_width=True):
            if not instance:
                st.error("Service Name fehlt.")
            else:
                rc, out, err = run_cmd([systemctl_bin(), "disable", "--now", poller_service_name(instance)])
                if rc == 0:
                    st.success(f"Gestoppt: {poller_service_name(instance)}")
                else:
                    st.error(err or out or "Stop fehlgeschlagen.")

        if c3.button("Logs", key=f"poll_logs_{fid}", use_container_width=True):
            if not instance:
                st.info("Service Name fehlt.")
            else:
                st.code(tail_poller_logs(instance), language="log")

        if instance:
            status = poller_status(instance)
            st.caption(
                f"Status: {status.get('active', '?')} / {status.get('sub', '?')} | "
                f"Result: {status.get('result', '')} | Start: {status.get('started', '')}"
            )
