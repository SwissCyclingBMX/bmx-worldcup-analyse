import datetime
import os
import re
import shutil
import subprocess
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

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


def ensure_poller_template_installed() -> Tuple[bool, str]:
    if os.path.exists(POLLER_UNIT_TEMPLATE):
        return True, "Service-Template vorhanden."
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(repo_dir, "deploy", "bmx-poller@.service.example")
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
st.sidebar.page_link("app.py", label="Heat Analyser")
st.sidebar.page_link("pages/3_Athlete_Insights.py", label="Athlete Insights")
st.sidebar.page_link("pages/4_Live_Polling.py", label="Live Polling")
st.sidebar.page_link("pages/9_CoachNow_Automation.py", label="CoachNow Automation")
st.sidebar.divider()

st.title("Live Polling")
st.caption("Mehrere Polling-Services parallel starten (Sqorz/USABMX, JSTiming, Chronorace).")

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
        source_label = st.selectbox("Quelle", ["Sqorz/USABMX", "JSTiming", "Chronorace"], key=f"poll_src_label_{fid}")
        source = {"Sqorz/USABMX": "sqorz", "JSTiming": "jstiming", "Chronorace": "chronorace"}[source_label]

        ensure_state(f"poll_name_{fid}", f"{source}-{fid}")
        raw_name = st.text_input("Service Name", key=f"poll_name_{fid}")
        instance = poller_instance_slug(raw_name)
        st.caption(f"Unit: {poller_service_name(instance) if instance else '–'}")

        poll_interval = int(st.number_input("Intervall (Sekunden)", min_value=5, max_value=600, value=15, step=5, key=f"poll_int_{fid}"))
        db_path = st.text_input("DB Pfad", value="bmx.db", key=f"poll_db_{fid}")

        env_values: Dict[str, str] = {
            "POLLER_KIND": source,
            "POLL_INTERVAL": str(poll_interval),
            "DB_PATH": db_path,
        }
        errors: List[str] = []

        if source == "sqorz":
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
                placeholder="z.B. 20260228_ffc_caen_j1_bmx",
            ).strip()
            if not event_target and parsed_id:
                today = datetime.date.today().strftime("%Y%m%d")
                event_target = f"{today}_sqorz_{parsed_id[:8]}_bmx"

            all_classes = st.checkbox("Alle Klassen ingestieren", value=False, key=f"poll_sqorz_all_{fid}")
            class_filters = st.text_area(
                "Class Filter (eine pro Zeile, falls nicht alle Klassen)",
                value="Men Pro\nWomen Pro",
                key=f"poll_sqorz_cls_{fid}",
                height=90,
            )

            if not event_url:
                errors.append("Event URL fehlt.")
            if not event_target:
                errors.append("Ziel event_id fehlt.")

            env_values["EVENT_URL"] = event_url
            env_values["EVENT_ID"] = event_target
            env_values["ALL_CLASSES"] = "1" if all_classes else "0"
            if not all_classes:
                env_values["CLASS_FILTERS"] = class_filters

        elif source == "jstiming":
            race_urls = st.text_area("Race URLs (eine pro Zeile)", key=f"poll_jst_race_{fid}", height=90)
            training_urls = st.text_area("Training URLs (eine pro Zeile)", key=f"poll_jst_training_{fid}", height=90)
            verbose = st.checkbox("Verbose Logs", value=False, key=f"poll_jst_verbose_{fid}")
            if not race_urls.strip() and not training_urls.strip():
                errors.append("Mindestens eine Race- oder Training-URL ist nötig.")
            env_values["RACE_URLS"] = race_urls
            env_values["TRAINING_URLS"] = training_urls
            env_values["VERBOSE"] = "1" if verbose else "0"

        else:
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
                    rc, out, err = run_cmd([systemctl_bin(), "enable", "--now", poller_service_name(instance)])
                    if rc == 0:
                        st.success(f"Gestartet: {poller_service_name(instance)} ({env_path})")
                    else:
                        st.error(err or out or "Start fehlgeschlagen.")

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
