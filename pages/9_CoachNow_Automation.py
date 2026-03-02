import json
import os
import uuid
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
DEFAULT_LIBRARY_URL = "https://app.coachnow.io/resources"


def make_setup_id() -> str:
    return uuid.uuid4().hex


def normalize_setup(raw: Any, fallback_name: str = "Setup") -> Dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    setup_id = str(data.get("id", "")).strip() or make_setup_id()
    base_url = str(data.get("base_url", DEFAULT_BASE_URL)).strip() or DEFAULT_BASE_URL
    token = str(data.get("token", DEFAULT_TOKEN)).strip()
    name = str(data.get("name", "")).strip()
    if not name:
        host_hint = base_url.replace("http://", "").replace("https://", "").strip("/")
        name = host_hint or fallback_name
    return {
        "id": setup_id,
        "name": name,
        "base_url": base_url,
        "token": token,
    }


def normalize_target(raw: Any, fallback_name: str = "Target") -> Dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    target_id = str(data.get("id", "")).strip() or make_setup_id()
    library_url = str(data.get("library_url", DEFAULT_LIBRARY_URL)).strip() or DEFAULT_LIBRARY_URL
    group_url = str(data.get("group_url", "")).strip()
    account_label = str(data.get("account_label", "")).strip()
    name = str(data.get("name", "")).strip()
    if not name:
        if account_label:
            name = account_label
        elif group_url:
            name = group_url.replace("https://app.coachnow.io/groups/", "group-")
        else:
            name = fallback_name
    return {
        "id": target_id,
        "name": name,
        "library_url": library_url,
        "group_url": group_url,
        "account_label": account_label,
    }


def normalize_profile_payload(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}

    # Backward compatibility: old profile format with only base_url/token.
    if "setups" not in data:
        setup = normalize_setup(
            {
                "id": "default",
                "name": "Default",
                "base_url": str(data.get("base_url", DEFAULT_BASE_URL)).strip() or DEFAULT_BASE_URL,
                "token": str(data.get("token", DEFAULT_TOKEN)).strip(),
            },
            fallback_name="Default",
        )
        target = normalize_target(
            {
                "id": "default-target",
                "name": "Default Target",
                "library_url": DEFAULT_LIBRARY_URL,
                "group_url": "",
                "account_label": "",
            },
            fallback_name="Default Target",
        )
        return {
            "active_setup_id": setup["id"],
            "setups": [setup],
            "active_target_id": target["id"],
            "targets": [target],
        }

    setups_raw = data.get("setups", [])
    if not isinstance(setups_raw, list):
        setups_raw = []

    setups: List[Dict[str, str]] = []
    seen_ids = set()
    for idx, item in enumerate(setups_raw):
        setup = normalize_setup(item, fallback_name=f"Setup {idx + 1}")
        if setup["id"] in seen_ids:
            setup["id"] = make_setup_id()
        seen_ids.add(setup["id"])
        setups.append(setup)

    if not setups:
        setup = normalize_setup(
            {"id": "default", "name": "Default", "base_url": DEFAULT_BASE_URL, "token": DEFAULT_TOKEN},
            fallback_name="Default",
        )
        setups = [setup]

    active_setup_id = str(data.get("active_setup_id", "")).strip()
    if not active_setup_id or all(x["id"] != active_setup_id for x in setups):
        active_setup_id = setups[0]["id"]

    targets_raw = data.get("targets", [])
    if not isinstance(targets_raw, list):
        targets_raw = []
    targets: List[Dict[str, str]] = []
    seen_target_ids = set()
    for idx, item in enumerate(targets_raw):
        target = normalize_target(item, fallback_name=f"Target {idx + 1}")
        if target["id"] in seen_target_ids:
            target["id"] = make_setup_id()
        seen_target_ids.add(target["id"])
        targets.append(target)
    if not targets:
        target = normalize_target(
            {
                "id": "default-target",
                "name": "Default Target",
                "library_url": DEFAULT_LIBRARY_URL,
                "group_url": "",
                "account_label": "",
            },
            fallback_name="Default Target",
        )
        targets = [target]

    active_target_id = str(data.get("active_target_id", "")).strip()
    if not active_target_id or all(x["id"] != active_target_id for x in targets):
        active_target_id = targets[0]["id"]

    return {
        "active_setup_id": active_setup_id,
        "setups": setups,
        "active_target_id": active_target_id,
        "targets": targets,
    }


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


def load_profile() -> Dict[str, Any]:
    if not PROFILE_PATH.exists():
        normalized = normalize_profile_payload({})
        active = next((x for x in normalized["setups"] if x["id"] == normalized["active_setup_id"]), None)
        active = active or normalized["setups"][0]
        active_target = next(
            (x for x in normalized["targets"] if x["id"] == normalized["active_target_id"]),
            None,
        )
        active_target = active_target or normalized["targets"][0]
        return {
            "base_url": active["base_url"],
            "token": active["token"],
            "setups": normalized["setups"],
            "active_setup_id": normalized["active_setup_id"],
            "targets": normalized["targets"],
            "active_target_id": normalized["active_target_id"],
            "library_url": active_target["library_url"],
            "group_url": active_target["group_url"],
            "account_label": active_target["account_label"],
        }
    try:
        raw_data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        data = normalize_profile_payload(raw_data)
        active = next((x for x in data["setups"] if x["id"] == data["active_setup_id"]), None)
        active = active or data["setups"][0]
        active_target = next((x for x in data["targets"] if x["id"] == data["active_target_id"]), None)
        active_target = active_target or data["targets"][0]
        return {
            "base_url": active["base_url"],
            "token": active["token"],
            "setups": data["setups"],
            "active_setup_id": data["active_setup_id"],
            "targets": data["targets"],
            "active_target_id": data["active_target_id"],
            "library_url": active_target["library_url"],
            "group_url": active_target["group_url"],
            "account_label": active_target["account_label"],
        }
    except Exception:
        normalized = normalize_profile_payload({})
        active = next((x for x in normalized["setups"] if x["id"] == normalized["active_setup_id"]), None)
        active = active or normalized["setups"][0]
        active_target = next(
            (x for x in normalized["targets"] if x["id"] == normalized["active_target_id"]),
            None,
        )
        active_target = active_target or normalized["targets"][0]
        return {
            "base_url": active["base_url"],
            "token": active["token"],
            "setups": normalized["setups"],
            "active_setup_id": normalized["active_setup_id"],
            "targets": normalized["targets"],
            "active_target_id": normalized["active_target_id"],
            "library_url": active_target["library_url"],
            "group_url": active_target["group_url"],
            "account_label": active_target["account_label"],
        }


def save_profile(
    base_url: str,
    token: str,
    setups: Optional[List[Dict[str, str]]] = None,
    active_setup_id: str = "",
    targets: Optional[List[Dict[str, str]]] = None,
    active_target_id: str = "",
) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if setups is None and targets is None:
        normalized = normalize_profile_payload(
            {
                "setups": [
                    {
                        "id": "default",
                        "name": "Default",
                        "base_url": base_url.strip(),
                        "token": token.strip(),
                    }
                ],
                "active_setup_id": "default",
                "targets": [
                    {
                        "id": "default-target",
                        "name": "Default Target",
                        "library_url": DEFAULT_LIBRARY_URL,
                        "group_url": "",
                        "account_label": "",
                    }
                ],
                "active_target_id": "default-target",
            }
        )
    else:
        normalized = normalize_profile_payload(
            {
                "setups": setups,
                "active_setup_id": active_setup_id,
                "targets": targets,
                "active_target_id": active_target_id,
            }
        )
    payload = {
        "active_setup_id": normalized["active_setup_id"],
        "setups": normalized["setups"],
        "active_target_id": normalized["active_target_id"],
        "targets": normalized["targets"],
    }
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
if "coachnow_setups" not in st.session_state:
    st.session_state["coachnow_setups"] = profile.get("setups", [])
if "coachnow_active_setup_id" not in st.session_state:
    st.session_state["coachnow_active_setup_id"] = profile.get("active_setup_id", "")
if "coachnow_selected_setup_id" not in st.session_state:
    st.session_state["coachnow_selected_setup_id"] = profile.get("active_setup_id", "")
if "coachnow_targets" not in st.session_state:
    st.session_state["coachnow_targets"] = profile.get("targets", [])
if "coachnow_active_target_id" not in st.session_state:
    st.session_state["coachnow_active_target_id"] = profile.get("active_target_id", "")
if "coachnow_selected_target_id" not in st.session_state:
    st.session_state["coachnow_selected_target_id"] = profile.get("active_target_id", "")
if "coachnow_base_url" not in st.session_state:
    st.session_state["coachnow_base_url"] = profile["base_url"]
if "coachnow_token" not in st.session_state:
    st.session_state["coachnow_token"] = profile["token"]
if "coachnow_setup_name" not in st.session_state:
    active_profile_setup = next(
        (
            x
            for x in profile.get("setups", [])
            if str(x.get("id", "")).strip() == str(profile.get("active_setup_id", "")).strip()
        ),
        None,
    )
    st.session_state["coachnow_setup_name"] = str(
        (active_profile_setup or {}).get("name", "Default")
    ).strip() or "Default"
if "coachnow_loaded_setup_id" not in st.session_state:
    st.session_state["coachnow_loaded_setup_id"] = st.session_state.get("coachnow_selected_setup_id", "")
if "coachnow_run_library_url" not in st.session_state:
    st.session_state["coachnow_run_library_url"] = profile.get("library_url", DEFAULT_LIBRARY_URL)
if "coachnow_run_group_url" not in st.session_state:
    st.session_state["coachnow_run_group_url"] = profile.get("group_url", "")
if "coachnow_run_account_label" not in st.session_state:
    st.session_state["coachnow_run_account_label"] = profile.get("account_label", "")
if "coachnow_target_name" not in st.session_state:
    active_profile_target = next(
        (
            x
            for x in profile.get("targets", [])
            if str(x.get("id", "")).strip() == str(profile.get("active_target_id", "")).strip()
        ),
        None,
    )
    st.session_state["coachnow_target_name"] = str(
        (active_profile_target or {}).get("name", "Default Target")
    ).strip() or "Default Target"
if "coachnow_loaded_target_id" not in st.session_state:
    st.session_state["coachnow_loaded_target_id"] = st.session_state.get("coachnow_selected_target_id", "")
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


def get_normalized_setups_from_state() -> Tuple[List[Dict[str, str]], str]:
    normalized = normalize_profile_payload(
        {
            "setups": st.session_state.get("coachnow_setups", []),
            "active_setup_id": st.session_state.get("coachnow_active_setup_id", ""),
        }
    )
    return normalized["setups"], normalized["active_setup_id"]


def get_normalized_targets_from_state() -> Tuple[List[Dict[str, str]], str]:
    normalized = normalize_profile_payload(
        {
            "targets": st.session_state.get("coachnow_targets", []),
            "active_target_id": st.session_state.get("coachnow_active_target_id", ""),
            "setups": st.session_state.get("coachnow_setups", []),
            "active_setup_id": st.session_state.get("coachnow_active_setup_id", ""),
        }
    )
    return normalized["targets"], normalized["active_target_id"]


def persist_profile_from_state() -> None:
    setups, active_setup_id = get_normalized_setups_from_state()
    targets, active_target_id = get_normalized_targets_from_state()
    st.session_state["coachnow_setups"] = setups
    st.session_state["coachnow_active_setup_id"] = active_setup_id
    st.session_state["coachnow_targets"] = targets
    st.session_state["coachnow_active_target_id"] = active_target_id
    save_profile(
        st.session_state.get("coachnow_base_url", DEFAULT_BASE_URL),
        st.session_state.get("coachnow_token", DEFAULT_TOKEN),
        setups=setups,
        active_setup_id=active_setup_id,
        targets=targets,
        active_target_id=active_target_id,
    )


def find_setup_by_id(setups: List[Dict[str, str]], setup_id: str) -> Optional[Dict[str, str]]:
    wanted = str(setup_id or "").strip()
    for item in setups:
        if str(item.get("id", "")).strip() == wanted:
            return item
    return None


def find_target_by_id(targets: List[Dict[str, str]], target_id: str) -> Optional[Dict[str, str]]:
    wanted = str(target_id or "").strip()
    for item in targets:
        if str(item.get("id", "")).strip() == wanted:
            return item
    return None


def save_run_urls_to_runner_settings(
    base_url: str,
    token: str,
    group_url: str,
    library_url: str,
) -> Tuple[bool, str]:
    clean_group_url = str(group_url or "").strip()
    clean_library_url = str(library_url or "").strip()
    if not clean_group_url:
        return False, "Group URL fehlt."
    if not clean_library_url:
        return False, "Library URL fehlt."

    current_settings = st.session_state.get("coachnow_settings_cache", {})
    if not isinstance(current_settings, dict) or not current_settings:
        ok, data = api_call(base_url, token, "GET", "/api/settings")
        if not ok:
            return False, f"Load settings failed: {data}"
        current_settings = data.get("settings", {})
        if not isinstance(current_settings, dict):
            current_settings = {}
        st.session_state["coachnow_settings_cache"] = current_settings

    payload_settings = dict(current_settings)
    payload_settings["groupUrl"] = clean_group_url
    payload_settings["libraryUrl"] = clean_library_url
    ok, data = api_call(base_url, token, "POST", "/api/settings", payload={"settings": payload_settings})
    if not ok:
        return False, f"Save group/library failed: {data}"

    st.session_state["coachnow_settings_cache"] = data.get("settings", payload_settings)
    return True, "Library + Group URL gespeichert."


setups, active_setup_id = get_normalized_setups_from_state()
st.session_state["coachnow_setups"] = setups
st.session_state["coachnow_active_setup_id"] = active_setup_id
if not find_setup_by_id(setups, st.session_state.get("coachnow_selected_setup_id", "")):
    st.session_state["coachnow_selected_setup_id"] = active_setup_id

st.subheader("Run Control")
targets, active_target_id = get_normalized_targets_from_state()
st.session_state["coachnow_targets"] = targets
st.session_state["coachnow_active_target_id"] = active_target_id
if not find_target_by_id(targets, st.session_state.get("coachnow_selected_target_id", "")):
    st.session_state["coachnow_selected_target_id"] = active_target_id

pick_a, pick_b = st.columns([1, 1])
setup_ids = [str(x.get("id", "")).strip() for x in setups if str(x.get("id", "")).strip()]
setup_label_map = {str(item["id"]): f"{item['name']} ({item['base_url']})" for item in setups}
selected_setup_id = pick_a.selectbox(
    "Saved Setups",
    options=setup_ids,
    index=max(0, setup_ids.index(st.session_state.get("coachnow_selected_setup_id", active_setup_id)))
    if setup_ids
    else 0,
    format_func=lambda setup_id: setup_label_map.get(setup_id, setup_id),
)
st.session_state["coachnow_selected_setup_id"] = selected_setup_id

target_ids = [str(x.get("id", "")).strip() for x in targets if str(x.get("id", "")).strip()]
target_label_map = {
    str(item["id"]): f"{item['name']} ({item['library_url']})"
    for item in targets
}
selected_target_id = pick_b.selectbox(
    "Saved Targets (Library + Group)",
    options=target_ids,
    index=max(0, target_ids.index(st.session_state.get("coachnow_selected_target_id", active_target_id)))
    if target_ids
    else 0,
    format_func=lambda target_id: target_label_map.get(target_id, target_id),
)
st.session_state["coachnow_selected_target_id"] = selected_target_id

selected_setup = find_setup_by_id(setups, selected_setup_id) or setups[0]
if st.session_state.get("coachnow_loaded_setup_id", "") != selected_setup_id:
    st.session_state["coachnow_base_url"] = selected_setup["base_url"]
    st.session_state["coachnow_token"] = selected_setup["token"]
    st.session_state["coachnow_setup_name"] = selected_setup["name"]
    st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
    st.rerun()

selected_target = find_target_by_id(targets, selected_target_id) or targets[0]
if st.session_state.get("coachnow_loaded_target_id", "") != selected_target_id:
    st.session_state["coachnow_run_library_url"] = selected_target["library_url"]
    st.session_state["coachnow_run_group_url"] = selected_target["group_url"]
    st.session_state["coachnow_run_account_label"] = selected_target["account_label"]
    st.session_state["coachnow_target_name"] = selected_target["name"]
    st.session_state["coachnow_loaded_target_id"] = selected_target_id
    st.rerun()

base_url = st.session_state.get("coachnow_base_url", DEFAULT_BASE_URL)
token = st.session_state.get("coachnow_token", DEFAULT_TOKEN)

if not st.session_state["coachnow_status_cache"]:
    refresh_status(base_url, token)
if not st.session_state["coachnow_settings_cache"]:
    refresh_settings(base_url, token)
if not st.session_state["coachnow_athletes_cache"]:
    refresh_athletes(base_url, token)

status = st.session_state.get("coachnow_status_cache", {})
settings = st.session_state.get("coachnow_settings_cache", {})
is_running = bool(status.get("running", False))
if not str(st.session_state.get("coachnow_run_library_url", "")).strip():
    st.session_state["coachnow_run_library_url"] = string_or_default(
        settings, "libraryUrl", selected_target.get("library_url", DEFAULT_LIBRARY_URL)
    )
if not str(st.session_state.get("coachnow_run_group_url", "")).strip():
    st.session_state["coachnow_run_group_url"] = string_or_default(
        settings, "groupUrl", selected_target.get("group_url", "")
    )
if not str(st.session_state.get("coachnow_run_account_label", "")).strip():
    st.session_state["coachnow_run_account_label"] = selected_target.get("account_label", "")

active_setup = find_setup_by_id(setups, active_setup_id) or {}
active_target = find_target_by_id(targets, active_target_id) or {}
st.caption(
    f"Aktiv Setup: {active_setup.get('name', 'n/a')} | Aktiv Target: {active_target.get('name', 'n/a')}"
)

run_account_label = st.text_input("Library Account (label)", key="coachnow_run_account_label")
run_library_url = st.text_input(
    "Library URL",
    key="coachnow_run_library_url",
    placeholder="https://app.coachnow.io/resources",
)
run_group_url = st.text_input(
    "Group URL (Posting target)",
    key="coachnow_run_group_url",
    placeholder="https://app.coachnow.io/groups/<group-id>",
)

top_a, top_b, top_c, top_d = st.columns([1, 1, 1, 1])
if top_a.button("Use selected setup", use_container_width=True):
    st.session_state["coachnow_active_setup_id"] = selected_setup_id
    st.session_state["coachnow_base_url"] = selected_setup["base_url"]
    st.session_state["coachnow_token"] = selected_setup["token"]
    st.session_state["coachnow_setup_name"] = selected_setup["name"]
    st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
    persist_profile_from_state()
    base_url = st.session_state.get("coachnow_base_url", DEFAULT_BASE_URL)
    token = st.session_state.get("coachnow_token", DEFAULT_TOKEN)
    st.success(f"Aktiv gesetzt (Setup): {selected_setup['name']}")
if top_b.button("Use selected target", use_container_width=True):
    st.session_state["coachnow_active_target_id"] = selected_target_id
    st.session_state["coachnow_run_library_url"] = selected_target["library_url"]
    st.session_state["coachnow_run_group_url"] = selected_target["group_url"]
    st.session_state["coachnow_run_account_label"] = selected_target["account_label"]
    st.session_state["coachnow_target_name"] = selected_target["name"]
    st.session_state["coachnow_loaded_target_id"] = selected_target_id
    persist_profile_from_state()
    st.success(f"Aktiv gesetzt (Target): {selected_target['name']}")
if top_c.button("Connect selected", use_container_width=True):
    refresh_status(base_url, token)
    refresh_settings(base_url, token)
    refresh_logs(base_url, token, 200)
    refresh_athletes(base_url, token)
if top_d.button("Reload all", use_container_width=True):
    refresh_status(base_url, token)
    refresh_settings(base_url, token)
    refresh_logs(base_url, token, 400)
    refresh_athletes(base_url, token)

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
    ok_urls, urls_msg = save_run_urls_to_runner_settings(
        base_url, token, run_group_url, run_library_url
    )
    if not ok_urls:
        st.error(urls_msg)
    else:
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
    ok_urls, urls_msg = save_run_urls_to_runner_settings(
        base_url, token, run_group_url, run_library_url
    )
    if not ok_urls:
        st.error(urls_msg)
    else:
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

with st.expander("Erweiterte Einstellungen", expanded=False):
    st.caption("Nur anpassen, wenn nötig.")
    st.markdown("#### Setup Details")
    setup_name = st.text_input(
        "Setup Name",
        key="coachnow_setup_name",
        help="Beliebiger Name, z.B. 'MacBook M3 Zuhause'",
    )
    base_url = st.text_input(
        "Control URL",
        key="coachnow_base_url",
        help="Example: http://127.0.0.1:8787",
    )
    token = st.text_input("API Token (optional)", key="coachnow_token", type="password")

    setup_a, setup_b, setup_c = st.columns([1, 1, 1])
    if setup_a.button("Save active setup", use_container_width=True):
        clean_name = str(setup_name).strip()
        clean_url = str(base_url).strip()
        if not clean_name:
            st.error("Setup Name fehlt.")
        elif not clean_url:
            st.error("Control URL fehlt.")
        else:
            for item in setups:
                if item["id"] == selected_setup_id:
                    item["name"] = clean_name
                    item["base_url"] = clean_url
                    item["token"] = str(token).strip()
                    break
            st.session_state["coachnow_setups"] = setups
            st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
            persist_profile_from_state()
            st.success("Setup gespeichert.")

    if setup_b.button("Save as new setup", use_container_width=True):
        clean_name = str(setup_name).strip()
        clean_url = str(base_url).strip()
        if not clean_name:
            st.error("Setup Name fehlt.")
        elif not clean_url:
            st.error("Control URL fehlt.")
        else:
            new_setup = normalize_setup(
                {
                    "id": make_setup_id(),
                    "name": clean_name,
                    "base_url": clean_url,
                    "token": str(token).strip(),
                },
                fallback_name="Setup",
            )
            setups.append(new_setup)
            st.session_state["coachnow_setups"] = setups
            st.session_state["coachnow_selected_setup_id"] = new_setup["id"]
            st.session_state["coachnow_loaded_setup_id"] = new_setup["id"]
            persist_profile_from_state()
            st.success(f"Neues Setup gespeichert: {new_setup['name']}")
            st.rerun()

    if setup_c.button("Activate selected setup", use_container_width=True):
        st.session_state["coachnow_active_setup_id"] = selected_setup_id
        st.session_state["coachnow_base_url"] = selected_setup["base_url"]
        st.session_state["coachnow_token"] = selected_setup["token"]
        st.session_state["coachnow_setup_name"] = selected_setup["name"]
        st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
        persist_profile_from_state()
        st.success(f"Aktiv gesetzt: {selected_setup['name']}")

    st.divider()
    st.markdown("#### Target Details (Library + Group)")
    target_name = st.text_input(
        "Target Name",
        key="coachnow_target_name",
        help="Beliebiger Name, z.B. 'Swiss Cycling - Elite'",
    )
    st.caption(
        f"Verwendet aktuelle Run-Werte: account='{st.session_state.get('coachnow_run_account_label', '')}', "
        f"library='{st.session_state.get('coachnow_run_library_url', '')}', "
        f"group='{st.session_state.get('coachnow_run_group_url', '')}'"
    )

    target_a, target_b, target_c = st.columns([1, 1, 1])
    if target_a.button("Save active target", use_container_width=True):
        clean_name = str(target_name).strip()
        clean_library = str(st.session_state.get("coachnow_run_library_url", "")).strip()
        clean_group = str(st.session_state.get("coachnow_run_group_url", "")).strip()
        clean_account = str(st.session_state.get("coachnow_run_account_label", "")).strip()
        if not clean_name:
            st.error("Target Name fehlt.")
        elif not clean_library:
            st.error("Library URL fehlt.")
        elif not clean_group:
            st.error("Group URL fehlt.")
        else:
            for item in targets:
                if item["id"] == selected_target_id:
                    item["name"] = clean_name
                    item["library_url"] = clean_library
                    item["group_url"] = clean_group
                    item["account_label"] = clean_account
                    break
            st.session_state["coachnow_targets"] = targets
            st.session_state["coachnow_loaded_target_id"] = selected_target_id
            persist_profile_from_state()
            st.success("Target gespeichert.")

    if target_b.button("Save as new target", use_container_width=True):
        clean_name = str(target_name).strip()
        clean_library = str(st.session_state.get("coachnow_run_library_url", "")).strip()
        clean_group = str(st.session_state.get("coachnow_run_group_url", "")).strip()
        clean_account = str(st.session_state.get("coachnow_run_account_label", "")).strip()
        if not clean_name:
            st.error("Target Name fehlt.")
        elif not clean_library:
            st.error("Library URL fehlt.")
        elif not clean_group:
            st.error("Group URL fehlt.")
        else:
            new_target = normalize_target(
                {
                    "id": make_setup_id(),
                    "name": clean_name,
                    "library_url": clean_library,
                    "group_url": clean_group,
                    "account_label": clean_account,
                },
                fallback_name="Target",
            )
            targets.append(new_target)
            st.session_state["coachnow_targets"] = targets
            st.session_state["coachnow_selected_target_id"] = new_target["id"]
            st.session_state["coachnow_loaded_target_id"] = new_target["id"]
            persist_profile_from_state()
            st.success(f"Neues Target gespeichert: {new_target['name']}")
            st.rerun()

    if target_c.button("Activate selected target", use_container_width=True):
        st.session_state["coachnow_active_target_id"] = selected_target_id
        st.session_state["coachnow_run_library_url"] = selected_target["library_url"]
        st.session_state["coachnow_run_group_url"] = selected_target["group_url"]
        st.session_state["coachnow_run_account_label"] = selected_target["account_label"]
        st.session_state["coachnow_target_name"] = selected_target["name"]
        st.session_state["coachnow_loaded_target_id"] = selected_target_id
        persist_profile_from_state()
        st.success(f"Aktiv gesetzt: {selected_target['name']}")

    st.divider()
    st.markdown("#### Session Athletes")
    athletes = st.session_state.get("coachnow_athletes_cache", [])
    athlete_options = []
    for athlete in athletes:
        if isinstance(athlete, dict):
            tag = str(athlete.get("tag", "")).strip()
            if tag:
                athlete_options.append(tag)
    athlete_options = sorted(set(athlete_options))

    current_settings = st.session_state.get("coachnow_settings_cache", {})
    current_env = current_settings.get("additionalEnv", {}) if isinstance(current_settings, dict) else {}
    if not isinstance(current_env, dict):
        current_env = {}
    default_selected = [x for x in parse_session_tags(current_env) if x in athlete_options]

    sel = st.multiselect(
        "Athleten dieser Session",
        options=athlete_options,
        default=default_selected,
        help="Optional. Wird als SESSION_ATHLETE_TAGS gespeichert.",
    )
    sa1, sa2 = st.columns([1, 1])
    if sa1.button("Save session athletes", use_container_width=True):
        payload_settings = dict(current_settings) if isinstance(current_settings, dict) else {}
        payload_env = dict(current_env)
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

    st.divider()
    st.markdown("#### Logs")
    logs_col1, logs_col2 = st.columns([1, 5])
    log_limit = logs_col1.slider("Lines", min_value=50, max_value=2000, value=350, step=50)
    if logs_col2.button("Refresh logs", use_container_width=True):
        refresh_logs(base_url, token, log_limit)
    if not st.session_state["coachnow_logs_cache"]:
        refresh_logs(base_url, token, log_limit)
    logs = st.session_state.get("coachnow_logs_cache", [])
    st.code("\n".join(logs[-log_limit:]) if logs else "(no logs)", language=None)

    st.divider()
    st.markdown("#### Runner Settings")
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
                    st.session_state["coachnow_run_library_url"] = payload_settings.get("libraryUrl", "")
                    st.session_state["coachnow_run_group_url"] = payload_settings.get("groupUrl", "")
                else:
                    st.error(f"Save failed: {data}")
