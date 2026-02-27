import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # pragma: no cover - optional runtime dependency
    requests = None
import streamlit as st


st.set_page_config(page_title="CoachNow Automation", layout="wide")

PROFILE_PATH = Path(".streamlit/coachnow_control_profile.json")
DEFAULT_BASE_URL = os.environ.get("COACHNOW_CONTROL_URL", "http://127.0.0.1:8787").strip()
DEFAULT_TOKEN = os.environ.get("COACHNOW_CONTROL_TOKEN", "").strip()


def parse_iso(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "n/a"
    try:
        if raw.endswith("Z"):
            raw = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def load_profile() -> Dict[str, str]:
    if not PROFILE_PATH.exists():
        return {"base_url": DEFAULT_BASE_URL, "token": DEFAULT_TOKEN}
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return {
            "base_url": str(data.get("base_url", DEFAULT_BASE_URL)).strip() or DEFAULT_BASE_URL,
            "token": str(data.get("token", DEFAULT_TOKEN)).strip(),
        }
    except Exception:
        return {"base_url": DEFAULT_BASE_URL, "token": DEFAULT_TOKEN}


def save_profile(base_url: str, token: str) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"base_url": base_url.strip(), "token": token.strip()}
    PROFILE_PATH.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def api_call(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 25,
) -> Tuple[bool, Any]:
    if requests is None:
        return False, "Python package 'requests' is not installed on this host."
    url = f"{base_url.rstrip('/')}{path}"
    headers = {}
    if token.strip():
        headers["x-control-token"] = token.strip()
    if payload is not None:
        headers["content-type"] = "application/json"
    try:
        res = requests.request(method=method, url=url, headers=headers, json=payload, timeout=timeout)
        try:
            data = res.json()
        except Exception:
            data = {"error": res.text[:600]}
        if res.status_code >= 400:
            msg = data.get("error") if isinstance(data, dict) else str(data)
            return False, f"{res.status_code} {msg}"
        return True, data
    except Exception as exc:
        return False, str(exc)


def bool_or_default(settings: Dict[str, Any], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def number_or_default(settings: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except Exception:
        return float(default)


def string_or_default(settings: Dict[str, Any], key: str, default: str) -> str:
    value = settings.get(key, default)
    return str(value).strip() if value is not None else default


def parse_session_tags(additional_env: Dict[str, Any]) -> List[str]:
    raw = str(additional_env.get("SESSION_ATHLETE_TAGS", "")).strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def render_status_chip(is_running: bool) -> None:
    label = "RUNNING" if is_running else "STOPPED"
    color = "#0f766e" if is_running else "#b91c1c"
    bg = "#ccfbf1" if is_running else "#fee2e2"
    st.markdown(
        f"<span style='display:inline-block;padding:6px 12px;border-radius:999px;"
        f"font-weight:700;font-size:0.9rem;color:{color};background:{bg};'>{label}</span>",
        unsafe_allow_html=True,
    )


st.markdown(
    """
    <style>
      .block-container { padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1280px; }
      .cn-box {
        border: 1px solid #dbe3ef;
        border-radius: 14px;
        padding: 14px 16px;
        background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
      }
      .cn-muted { color: #4b5563; font-size: 0.9rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

def safe_sidebar_page_link(script_path: str, label: str) -> None:
    if Path(script_path).exists():
        st.sidebar.page_link(script_path, label=label)


safe_sidebar_page_link("app.py", "Heat Analyser")
safe_sidebar_page_link("pages/3_Athlete_Insights.py", "Athlete Insights")
safe_sidebar_page_link("pages/4_Live_Polling.py", "Live Polling")
safe_sidebar_page_link("pages/9_CoachNow_Automation.py", "CoachNow Automation")
st.sidebar.divider()

st.title("CoachNow Automation")
st.caption("Minimal mode: URL, Start/Stop, Status und Logs. Alles weitere unter 'Erweiterte Einstellungen'.")

profile = load_profile()
if "coachnow_base_url" not in st.session_state:
    st.session_state["coachnow_base_url"] = profile["base_url"]
if "coachnow_token" not in st.session_state:
    st.session_state["coachnow_token"] = profile["token"]
if "coachnow_status_cache" not in st.session_state:
    st.session_state["coachnow_status_cache"] = {}
if "coachnow_settings_cache" not in st.session_state:
    st.session_state["coachnow_settings_cache"] = {}
if "coachnow_logs_cache" not in st.session_state:
    st.session_state["coachnow_logs_cache"] = []
if "coachnow_athletes_cache" not in st.session_state:
    st.session_state["coachnow_athletes_cache"] = []


def refresh_status(base_url: str, token: str) -> None:
    ok, data = api_call(base_url, token, "GET", "/api/status")
    if ok:
        st.session_state["coachnow_status_cache"] = data.get("status", {})
    else:
        st.error(f"Status failed: {data}")


def refresh_settings(base_url: str, token: str) -> None:
    ok, data = api_call(base_url, token, "GET", "/api/settings")
    if ok:
        st.session_state["coachnow_settings_cache"] = data.get("settings", {})
    else:
        st.error(f"Load settings failed: {data}")


def refresh_logs(base_url: str, token: str, limit: int) -> None:
    ok, data = api_call(base_url, token, "GET", f"/api/logs?limit={int(limit)}")
    if ok:
        logs = data.get("logs", [])
        st.session_state["coachnow_logs_cache"] = logs if isinstance(logs, list) else []
    else:
        st.error(f"Load logs failed: {data}")


def refresh_athletes(base_url: str, token: str) -> None:
    ok, data = api_call(base_url, token, "GET", "/api/athletes")
    if ok:
        athletes = data.get("athletes", [])
        st.session_state["coachnow_athletes_cache"] = athletes if isinstance(athletes, list) else []
    else:
        st.session_state["coachnow_athletes_cache"] = []


base_url = st.text_input("Control URL", key="coachnow_base_url", help="Example: http://127.0.0.1:8787")
token = st.text_input("API Token (optional)", key="coachnow_token", type="password")

conn_a, conn_b, conn_c = st.columns([1, 1, 2])
if conn_a.button("Save URL/Token", use_container_width=True):
    save_profile(base_url, token)
    st.success("Connection profile saved.")
if conn_b.button("Connect", use_container_width=True):
    refresh_status(base_url, token)
    refresh_settings(base_url, token)
    refresh_logs(base_url, token, 200)
    refresh_athletes(base_url, token)
if conn_c.button("Reload all", use_container_width=True):
    refresh_status(base_url, token)
    refresh_settings(base_url, token)
    refresh_logs(base_url, token, 400)
    refresh_athletes(base_url, token)

if not st.session_state["coachnow_status_cache"]:
    refresh_status(base_url, token)
if not st.session_state["coachnow_settings_cache"]:
    refresh_settings(base_url, token)
if not st.session_state["coachnow_athletes_cache"]:
    refresh_athletes(base_url, token)

status = st.session_state.get("coachnow_status_cache", {})
settings = st.session_state.get("coachnow_settings_cache", {})
is_running = bool(status.get("running", False))

st.markdown("<div class='cn-box'>", unsafe_allow_html=True)
render_status_chip(is_running)
m1, m2, m3, m4 = st.columns(4)
m1.metric("PID", str(status.get("pid", "n/a")))
m2.metric("Host", str(status.get("host", "n/a")))
m3.metric("Port", str(status.get("port", "n/a")))
m4.metric("Token required", "Yes" if status.get("tokenRequired") else "No")
st.markdown(
    f"<span class='cn-muted'>Started: {parse_iso(status.get('startedAt'))} | Last exit: "
    f"{parse_iso((status.get('lastExit') or {}).get('at'))}</span>",
    unsafe_allow_html=True,
)
st.markdown("</div>", unsafe_allow_html=True)

ctl1, ctl2, ctl3, ctl4 = st.columns(4)
if ctl1.button("Start", type="primary", use_container_width=True):
    ok, data = api_call(base_url, token, "POST", "/api/start")
    if ok:
        st.success(data.get("message", "Runner started."))
    else:
        st.error(f"Start failed: {data}")
    refresh_status(base_url, token)
    refresh_logs(base_url, token, 200)
if ctl2.button("Stop", use_container_width=True):
    ok, data = api_call(base_url, token, "POST", "/api/stop")
    if ok:
        st.warning(data.get("message", "Stop signal sent."))
    else:
        st.error(f"Stop failed: {data}")
    refresh_status(base_url, token)
    refresh_logs(base_url, token, 200)
if ctl3.button("Restart", use_container_width=True):
    api_call(base_url, token, "POST", "/api/stop")
    ok, data = api_call(base_url, token, "POST", "/api/start")
    if ok:
        st.success(data.get("message", "Runner started."))
    else:
        st.error(f"Restart failed: {data}")
    refresh_status(base_url, token)
    refresh_logs(base_url, token, 250)
if ctl4.button("Refresh status", use_container_width=True):
    refresh_status(base_url, token)

st.subheader("Session Athletes")
athletes = st.session_state.get("coachnow_athletes_cache", [])
athlete_options = []
for athlete in athletes:
    if isinstance(athlete, dict):
        tag = str(athlete.get("tag", "")).strip()
        if tag:
            athlete_options.append(tag)
athlete_options = sorted(set(athlete_options))

additional_env = settings.get("additionalEnv", {})
if not isinstance(additional_env, dict):
    additional_env = {}
default_selected = [x for x in parse_session_tags(additional_env) if x in athlete_options]

sel = st.multiselect(
    "Athleten dieser Session",
    options=athlete_options,
    default=default_selected,
    help="Optional. Wird als SESSION_ATHLETE_TAGS gespeichert.",
)
sa1, sa2 = st.columns([1, 1])
if sa1.button("Save session athletes", use_container_width=True):
    payload_settings = dict(settings)
    payload_env = dict(additional_env)
    if sel:
        payload_env["SESSION_ATHLETE_TAGS"] = ",".join(sel)
    else:
        payload_env.pop("SESSION_ATHLETE_TAGS", None)
    payload_settings["additionalEnv"] = payload_env
    ok, data = api_call(base_url, token, "POST", "/api/settings", payload={"settings": payload_settings})
    if ok:
        st.success("Session athletes saved.")
        st.session_state["coachnow_settings_cache"] = data.get("settings", payload_settings)
    else:
        st.error(f"Save failed: {data}")
if sa2.button("Reload athlete list", use_container_width=True):
    refresh_athletes(base_url, token)

st.subheader("Logs")
l1, l2 = st.columns([1, 5])
log_limit = l1.slider("Lines", min_value=50, max_value=2000, value=350, step=50)
if l2.button("Refresh logs", use_container_width=True):
    refresh_logs(base_url, token, log_limit)
if not st.session_state["coachnow_logs_cache"]:
    refresh_logs(base_url, token, log_limit)
logs = st.session_state.get("coachnow_logs_cache", [])
st.code("\n".join(logs[-log_limit:]) if logs else "(no logs)", language=None)

with st.expander("Erweiterte Einstellungen", expanded=False):
    st.caption("Nur anpassen, wenn nötig.")
    cur = st.session_state.get("coachnow_settings_cache", {})
    add_env_raw = json.dumps(cur.get("additionalEnv", {}) if isinstance(cur.get("additionalEnv", {}), dict) else {}, indent=2)

    with st.form("coachnow_adv_settings_form"):
        c1, c2 = st.columns(2)
        group_url = c1.text_input("GROUP_URL", value=string_or_default(cur, "groupUrl", ""))
        library_url = c2.text_input("LIBRARY_URL", value=string_or_default(cur, "libraryUrl", "https://app.coachnow.io/resources"))

        d1, d2, d3, d4 = st.columns(4)
        dry_run = d1.checkbox("DRY_RUN", value=bool_or_default(cur, "dryRun", False))
        parallel_pipeline = d2.checkbox("PARALLEL_PIPELINE", value=bool_or_default(cur, "parallelPipeline", True))
        background_mode = d3.checkbox("BACKGROUND_MODE", value=bool_or_default(cur, "backgroundMode", True))
        headless = d4.checkbox("HEADLESS", value=bool_or_default(cur, "headless", False))

        e1, e2, e3 = st.columns(3)
        model_size = e1.text_input("MODEL_SIZE", value=string_or_default(cur, "modelSize", "medium"))
        whisper_lang = e2.text_input("WHISPER_LANGUAGE", value=string_or_default(cur, "whisperLanguage", "de"))
        ambiguous_mode = e3.selectbox(
            "AMBIGUOUS_MODE",
            options=["skip", "lastname", "priority"],
            index=["skip", "lastname", "priority"].index(
                string_or_default(cur, "ambiguousMode", "skip")
                if string_or_default(cur, "ambiguousMode", "skip") in {"skip", "lastname", "priority"}
                else "skip"
            ),
        )

        f1, f2, f3 = st.columns(3)
        clip_start = f1.number_input("CLIP_START_SECONDS", min_value=0.0, max_value=600.0, value=float(number_or_default(cur, "clipStartSeconds", 0)), step=0.5)
        clip_seconds = f2.number_input("CLIP_SECONDS", min_value=1.0, max_value=600.0, value=float(number_or_default(cur, "clipSeconds", 60)), step=1.0)
        whisper_beam = f3.number_input("WHISPER_BEAM", min_value=1, max_value=5, value=int(number_or_default(cur, "whisperBeam", 5)), step=1)

        g1, g2, g3 = st.columns(3)
        multi_transcribe = g1.checkbox("MULTI_TRANSCRIBE", value=bool_or_default(cur, "multiTranscribe", True))
        adaptive_pass = g2.checkbox("ADAPTIVE_PASS_BY_DURATION", value=bool_or_default(cur, "adaptivePassByDuration", True))
        multi_pass_max = g3.number_input("MULTI_PASS_MAX", min_value=1, max_value=24, value=int(number_or_default(cur, "multiPassMax", 6)), step=1)

        h1, h2, h3 = st.columns(3)
        short_single = h1.number_input("SHORT_SINGLE_PASS_MAX_SECONDS", min_value=5.0, max_value=120.0, value=float(number_or_default(cur, "shortSinglePassMaxSeconds", 6)), step=1.0)
        medium_dual = h2.number_input("MEDIUM_DUAL_PASS_MAX_SECONDS", min_value=10.0, max_value=180.0, value=float(number_or_default(cur, "mediumDualPassMaxSeconds", 25)), step=1.0)
        first_name_mode = h3.selectbox(
            "FIRST_NAME_ONLY_MODE",
            options=["generic", "athlete"],
            index=0 if string_or_default(cur, "firstNameOnlyMode", "generic") == "generic" else 1,
        )

        i1, i2, i3 = st.columns(3)
        context_enabled = i1.checkbox("CONTEXT_TAGS_ENABLED", value=bool_or_default(cur, "contextTagsEnabled", True))
        context_tags = i2.text_input("CONTEXT_TAGS", value=string_or_default(cur, "contextTags", "Gate,HalfLap,FullLap,Crash,Review"))
        poll_ms = i3.number_input("POLL_MS", min_value=1000, max_value=120000, value=int(number_or_default(cur, "pollMs", 5000)), step=500)

        j1, j2, j3 = st.columns(3)
        python_bin = j1.text_input("PYTHON_BIN", value=string_or_default(cur, "pythonBin", ""))
        transcribe_script = j2.text_input("TRANSCRIBE_SCRIPT", value=string_or_default(cur, "transcribeScript", "transcribe.py"))
        athletes_path = j3.text_input("ATHLETES_PATH", value=string_or_default(cur, "athletesPath", "athletes.json"))

        k1, k2, k3 = st.columns(3)
        profile_dir = k1.text_input("PROFILE_DIR", value=string_or_default(cur, "profileDir", "coachnow_profile"))
        require_tagged = k2.checkbox("REQUIRE_TAGGED_FOR_POSTING", value=bool_or_default(cur, "requireTaggedForPosting", True))
        allow_manual = k3.checkbox("ALLOW_MANUAL_TAGGED_POSTING", value=bool_or_default(cur, "allowManualTaggedPosting", True))

        l1, l2 = st.columns(2)
        detection_filter = l1.checkbox("DETECTION_FILTER_ENABLED", value=bool_or_default(cur, "detectionFilterEnabled", False))
        lock_baseline = l2.checkbox("LOCK_SESSION_TO_BASELINE_DATE", value=bool_or_default(cur, "lockSessionToBaselineDate", False))

        additional_env_raw = st.text_area("Additional ENV (JSON object)", value=add_env_raw, height=120)
        save_adv = st.form_submit_button("Save advanced settings", type="primary")

        if save_adv:
            try:
                additional_env = json.loads(additional_env_raw.strip() or "{}")
                if not isinstance(additional_env, dict):
                    raise ValueError("Additional ENV must be a JSON object.")
            except Exception as exc:
                st.error(f"Additional ENV error: {exc}")
            else:
                payload_settings = {
                    "groupUrl": group_url.strip(),
                    "libraryUrl": library_url.strip(),
                    "dryRun": dry_run,
                    "parallelPipeline": parallel_pipeline,
                    "backgroundMode": background_mode,
                    "headless": headless,
                    "modelSize": model_size.strip(),
                    "whisperLanguage": whisper_lang.strip(),
                    "clipStartSeconds": clip_start,
                    "clipSeconds": clip_seconds,
                    "multiTranscribe": multi_transcribe,
                    "adaptivePassByDuration": adaptive_pass,
                    "shortSinglePassMaxSeconds": short_single,
                    "mediumDualPassMaxSeconds": medium_dual,
                    "multiPassMax": int(multi_pass_max),
                    "whisperBeam": int(whisper_beam),
                    "firstNameOnlyMode": first_name_mode,
                    "ambiguousMode": ambiguous_mode,
                    "contextTagsEnabled": context_enabled,
                    "contextTags": context_tags.strip(),
                    "requireTaggedForPosting": require_tagged,
                    "allowManualTaggedPosting": allow_manual,
                    "detectionFilterEnabled": detection_filter,
                    "lockSessionToBaselineDate": lock_baseline,
                    "pollMs": int(poll_ms),
                    "pythonBin": python_bin.strip(),
                    "transcribeScript": transcribe_script.strip(),
                    "athletesPath": athletes_path.strip(),
                    "profileDir": profile_dir.strip(),
                    "additionalEnv": additional_env,
                }
                ok, data = api_call(base_url, token, "POST", "/api/settings", payload={"settings": payload_settings})
                if ok:
                    st.success("Advanced settings saved.")
                    st.session_state["coachnow_settings_cache"] = data.get("settings", payload_settings)
                else:
                    st.error(f"Save failed: {data}")
