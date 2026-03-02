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
DEFAULT_PROFILE_DIR = "coachnow_profile"


def make_id() -> str:
    return uuid.uuid4().hex


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


def normalize_setup(raw: Any, fallback_name: str = "Setup") -> Dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    setup_id = str(data.get("id", "")).strip() or make_id()
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


def normalize_account(raw: Any, fallback_name: str = "Account") -> Dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    account_id = str(data.get("id", "")).strip() or make_id()
    name = str(data.get("name", "")).strip() or fallback_name
    profile_dir = str(data.get("profile_dir", DEFAULT_PROFILE_DIR)).strip() or DEFAULT_PROFILE_DIR
    return {
        "id": account_id,
        "name": name,
        "profile_dir": profile_dir,
    }


def normalize_library(raw: Any, fallback_name: str = "Library") -> Dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    library_id = str(data.get("id", "")).strip() or make_id()
    name = str(data.get("name", "")).strip() or fallback_name
    url = str(data.get("url", DEFAULT_LIBRARY_URL)).strip() or DEFAULT_LIBRARY_URL
    account_label = str(data.get("account_label", "")).strip()
    return {
        "id": library_id,
        "name": name,
        "url": url,
        "account_label": account_label,
    }


def normalize_group(raw: Any, fallback_name: str = "Group") -> Dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    group_id = str(data.get("id", "")).strip() or make_id()
    name = str(data.get("name", "")).strip() or fallback_name
    url = str(data.get("url", "")).strip()
    return {
        "id": group_id,
        "name": name,
        "url": url,
    }


def _dedup_ids(items: List[Dict[str, str]], key: str = "id") -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        current = dict(item)
        item_id = str(current.get(key, "")).strip()
        if not item_id:
            item_id = make_id()
            current[key] = item_id
        if item_id in seen:
            current[key] = make_id()
        seen.add(current[key])
        out.append(current)
    return out


def normalize_profile_payload(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}

    if "setups" not in data:
        setup = normalize_setup(
            {
                "id": "default-setup",
                "name": "Default Setup",
                "base_url": str(data.get("base_url", DEFAULT_BASE_URL)).strip() or DEFAULT_BASE_URL,
                "token": str(data.get("token", DEFAULT_TOKEN)).strip(),
            },
            fallback_name="Default Setup",
        )
        account = normalize_account(
            {
                "id": "default-account",
                "name": "Default Account",
                "profile_dir": DEFAULT_PROFILE_DIR,
            },
            fallback_name="Default Account",
        )
        library = normalize_library(
            {
                "id": "default-library",
                "name": "Default Library",
                "url": DEFAULT_LIBRARY_URL,
                "account_label": "",
            },
            fallback_name="Default Library",
        )
        group = normalize_group(
            {
                "id": "default-group",
                "name": "Default Group",
                "url": "",
            },
            fallback_name="Default Group",
        )
        return {
            "active_setup_id": setup["id"],
            "setups": [setup],
            "active_account_id": account["id"],
            "accounts": [account],
            "active_library_id": library["id"],
            "libraries": [library],
            "active_group_id": group["id"],
            "groups": [group],
        }

    setups_raw = data.get("setups", [])
    if not isinstance(setups_raw, list):
        setups_raw = []
    setups = _dedup_ids([normalize_setup(item, fallback_name=f"Setup {idx + 1}") for idx, item in enumerate(setups_raw)])
    if not setups:
        setups = [
            normalize_setup(
                {
                    "id": "default-setup",
                    "name": "Default Setup",
                    "base_url": DEFAULT_BASE_URL,
                    "token": DEFAULT_TOKEN,
                },
                fallback_name="Default Setup",
            )
        ]

    accounts_raw = data.get("accounts", [])
    if not isinstance(accounts_raw, list):
        accounts_raw = []
    accounts = _dedup_ids(
        [normalize_account(item, fallback_name=f"Account {idx + 1}") for idx, item in enumerate(accounts_raw)]
    )
    if not accounts:
        accounts = [
            normalize_account(
                {
                    "id": "default-account",
                    "name": "Default Account",
                    "profile_dir": str(data.get("profile_dir", DEFAULT_PROFILE_DIR)).strip() or DEFAULT_PROFILE_DIR,
                },
                fallback_name="Default Account",
            )
        ]

    libraries_raw = data.get("libraries", [])
    if not isinstance(libraries_raw, list):
        libraries_raw = []

    if not libraries_raw and isinstance(data.get("targets", []), list):
        for idx, t in enumerate(data.get("targets", [])):
            if not isinstance(t, dict):
                continue
            libraries_raw.append(
                {
                    "id": str(t.get("id", "")).strip() or f"legacy-library-{idx + 1}",
                    "name": str(t.get("name", "")).strip() or f"Library {idx + 1}",
                    "url": str(t.get("library_url", DEFAULT_LIBRARY_URL)).strip() or DEFAULT_LIBRARY_URL,
                    "account_label": str(t.get("account_label", "")).strip(),
                }
            )

    libraries = _dedup_ids(
        [normalize_library(item, fallback_name=f"Library {idx + 1}") for idx, item in enumerate(libraries_raw)]
    )
    if not libraries:
        libraries = [
            normalize_library(
                {
                    "id": "default-library",
                    "name": "Default Library",
                    "url": DEFAULT_LIBRARY_URL,
                    "account_label": "",
                },
                fallback_name="Default Library",
            )
        ]

    groups_raw = data.get("groups", [])
    if not isinstance(groups_raw, list):
        groups_raw = []

    if not groups_raw and isinstance(data.get("targets", []), list):
        for idx, t in enumerate(data.get("targets", [])):
            if not isinstance(t, dict):
                continue
            groups_raw.append(
                {
                    "id": str(t.get("id", "")).strip() or f"legacy-group-{idx + 1}",
                    "name": str(t.get("name", "")).strip() or f"Group {idx + 1}",
                    "url": str(t.get("group_url", "")).strip(),
                }
            )

    groups = _dedup_ids([normalize_group(item, fallback_name=f"Group {idx + 1}") for idx, item in enumerate(groups_raw)])
    if not groups:
        groups = [
            normalize_group(
                {
                    "id": "default-group",
                    "name": "Default Group",
                    "url": "",
                },
                fallback_name="Default Group",
            )
        ]

    active_setup_id = str(data.get("active_setup_id", "")).strip()
    if not active_setup_id or all(x["id"] != active_setup_id for x in setups):
        active_setup_id = setups[0]["id"]

    active_account_id = str(data.get("active_account_id", "")).strip()
    if not active_account_id or all(x["id"] != active_account_id for x in accounts):
        active_account_id = accounts[0]["id"]

    active_library_id = str(data.get("active_library_id", "")).strip()
    if not active_library_id or all(x["id"] != active_library_id for x in libraries):
        active_library_id = libraries[0]["id"]

    active_group_id = str(data.get("active_group_id", "")).strip()
    if not active_group_id or all(x["id"] != active_group_id for x in groups):
        active_group_id = groups[0]["id"]

    return {
        "active_setup_id": active_setup_id,
        "setups": setups,
        "active_account_id": active_account_id,
        "accounts": accounts,
        "active_library_id": active_library_id,
        "libraries": libraries,
        "active_group_id": active_group_id,
        "groups": groups,
    }


def load_profile() -> Dict[str, Any]:
    normalized: Dict[str, Any]
    if not PROFILE_PATH.exists():
        normalized = normalize_profile_payload({})
    else:
        try:
            raw_data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            normalized = normalize_profile_payload(raw_data)
        except Exception:
            normalized = normalize_profile_payload({})

    active_setup = next(
        (x for x in normalized["setups"] if x["id"] == normalized["active_setup_id"]),
        normalized["setups"][0],
    )
    active_account = next(
        (x for x in normalized["accounts"] if x["id"] == normalized["active_account_id"]),
        normalized["accounts"][0],
    )
    active_library = next(
        (x for x in normalized["libraries"] if x["id"] == normalized["active_library_id"]),
        normalized["libraries"][0],
    )
    active_group = next(
        (x for x in normalized["groups"] if x["id"] == normalized["active_group_id"]),
        normalized["groups"][0],
    )

    return {
        "base_url": active_setup["base_url"],
        "token": active_setup["token"],
        "setups": normalized["setups"],
        "active_setup_id": normalized["active_setup_id"],
        "accounts": normalized["accounts"],
        "active_account_id": normalized["active_account_id"],
        "libraries": normalized["libraries"],
        "active_library_id": normalized["active_library_id"],
        "groups": normalized["groups"],
        "active_group_id": normalized["active_group_id"],
        "profile_dir": active_account["profile_dir"],
        "account_name": active_account["name"],
        "library_url": active_library["url"],
        "library_name": active_library["name"],
        "group_url": active_group["url"],
        "group_name": active_group["name"],
    }


def save_profile(
    base_url: str,
    token: str,
    setups: Optional[List[Dict[str, str]]] = None,
    active_setup_id: str = "",
    accounts: Optional[List[Dict[str, str]]] = None,
    active_account_id: str = "",
    libraries: Optional[List[Dict[str, str]]] = None,
    active_library_id: str = "",
    groups: Optional[List[Dict[str, str]]] = None,
    active_group_id: str = "",
) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if setups is None and accounts is None and libraries is None and groups is None:
        normalized = normalize_profile_payload(
            {
                "setups": [
                    {
                        "id": "default-setup",
                        "name": "Default Setup",
                        "base_url": base_url.strip(),
                        "token": token.strip(),
                    }
                ],
                "active_setup_id": "default-setup",
                "accounts": [
                    {
                        "id": "default-account",
                        "name": "Default Account",
                        "profile_dir": DEFAULT_PROFILE_DIR,
                    }
                ],
                "active_account_id": "default-account",
                "libraries": [
                    {
                        "id": "default-library",
                        "name": "Default Library",
                        "url": DEFAULT_LIBRARY_URL,
                        "account_label": "",
                    }
                ],
                "active_library_id": "default-library",
                "groups": [
                    {
                        "id": "default-group",
                        "name": "Default Group",
                        "url": "",
                    }
                ],
                "active_group_id": "default-group",
            }
        )
    else:
        normalized = normalize_profile_payload(
            {
                "setups": setups,
                "active_setup_id": active_setup_id,
                "accounts": accounts,
                "active_account_id": active_account_id,
                "libraries": libraries,
                "active_library_id": active_library_id,
                "groups": groups,
                "active_group_id": active_group_id,
            }
        )

    payload = {
        "active_setup_id": normalized["active_setup_id"],
        "setups": normalized["setups"],
        "active_account_id": normalized["active_account_id"],
        "accounts": normalized["accounts"],
        "active_library_id": normalized["active_library_id"],
        "libraries": normalized["libraries"],
        "active_group_id": normalized["active_group_id"],
        "groups": normalized["groups"],
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


def find_by_id(items: List[Dict[str, str]], item_id: str) -> Optional[Dict[str, str]]:
    wanted = str(item_id or "").strip()
    for item in items:
        if str(item.get("id", "")).strip() == wanted:
            return item
    return None


def safe_sidebar_page_link(script_path: str, label: str) -> None:
    if Path(script_path).exists():
        st.sidebar.page_link(script_path, label=label)


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

safe_sidebar_page_link("app.py", "Heat Analyser")
safe_sidebar_page_link("pages/3_Athlete_Insights.py", "Athlete Insights")
safe_sidebar_page_link("pages/4_Live_Polling.py", "Live Polling")
safe_sidebar_page_link("pages/9_CoachNow_Automation.py", "CoachNow Automation")
st.sidebar.divider()

st.title("CoachNow Automation")
st.caption(
    "Minimal mode: Setup/Account/Library/Group wählen, Start/Stop und Status. Rest unter 'Erweiterte Einstellungen'."
)

profile = load_profile()
if "coachnow_setups" not in st.session_state:
    st.session_state["coachnow_setups"] = profile.get("setups", [])
if "coachnow_active_setup_id" not in st.session_state:
    st.session_state["coachnow_active_setup_id"] = profile.get("active_setup_id", "")
if "coachnow_selected_setup_id" not in st.session_state:
    st.session_state["coachnow_selected_setup_id"] = profile.get("active_setup_id", "")

if "coachnow_accounts" not in st.session_state:
    st.session_state["coachnow_accounts"] = profile.get("accounts", [])
if "coachnow_active_account_id" not in st.session_state:
    st.session_state["coachnow_active_account_id"] = profile.get("active_account_id", "")
if "coachnow_selected_account_id" not in st.session_state:
    st.session_state["coachnow_selected_account_id"] = profile.get("active_account_id", "")

if "coachnow_libraries" not in st.session_state:
    st.session_state["coachnow_libraries"] = profile.get("libraries", [])
if "coachnow_active_library_id" not in st.session_state:
    st.session_state["coachnow_active_library_id"] = profile.get("active_library_id", "")
if "coachnow_selected_library_id" not in st.session_state:
    st.session_state["coachnow_selected_library_id"] = profile.get("active_library_id", "")

if "coachnow_groups" not in st.session_state:
    st.session_state["coachnow_groups"] = profile.get("groups", [])
if "coachnow_active_group_id" not in st.session_state:
    st.session_state["coachnow_active_group_id"] = profile.get("active_group_id", "")
if "coachnow_selected_group_id" not in st.session_state:
    st.session_state["coachnow_selected_group_id"] = profile.get("active_group_id", "")

if "coachnow_base_url" not in st.session_state:
    st.session_state["coachnow_base_url"] = profile.get("base_url", DEFAULT_BASE_URL)
if "coachnow_token" not in st.session_state:
    st.session_state["coachnow_token"] = profile.get("token", DEFAULT_TOKEN)
if "coachnow_setup_name" not in st.session_state:
    active_setup = find_by_id(st.session_state["coachnow_setups"], st.session_state["coachnow_active_setup_id"]) or {}
    st.session_state["coachnow_setup_name"] = str(active_setup.get("name", "Default Setup")).strip() or "Default Setup"
if "coachnow_loaded_setup_id" not in st.session_state:
    st.session_state["coachnow_loaded_setup_id"] = st.session_state.get("coachnow_selected_setup_id", "")

if "coachnow_run_account_name" not in st.session_state:
    st.session_state["coachnow_run_account_name"] = profile.get("account_name", "")
if "coachnow_run_profile_dir" not in st.session_state:
    st.session_state["coachnow_run_profile_dir"] = profile.get("profile_dir", DEFAULT_PROFILE_DIR)
if "coachnow_account_profile_dir" not in st.session_state:
    st.session_state["coachnow_account_profile_dir"] = st.session_state["coachnow_run_profile_dir"]
if "coachnow_account_name" not in st.session_state:
    st.session_state["coachnow_account_name"] = profile.get("account_name", "") or "Default Account"
if "coachnow_loaded_account_id" not in st.session_state:
    st.session_state["coachnow_loaded_account_id"] = st.session_state.get("coachnow_selected_account_id", "")

if "coachnow_run_library_name" not in st.session_state:
    st.session_state["coachnow_run_library_name"] = profile.get("library_name", "")
if "coachnow_run_library_url" not in st.session_state:
    st.session_state["coachnow_run_library_url"] = profile.get("library_url", DEFAULT_LIBRARY_URL)
if "coachnow_library_url_editor" not in st.session_state:
    st.session_state["coachnow_library_url_editor"] = st.session_state["coachnow_run_library_url"]
if "coachnow_library_name" not in st.session_state:
    st.session_state["coachnow_library_name"] = profile.get("library_name", "") or "Default Library"
if "coachnow_loaded_library_id" not in st.session_state:
    st.session_state["coachnow_loaded_library_id"] = st.session_state.get("coachnow_selected_library_id", "")

if "coachnow_run_group_name" not in st.session_state:
    st.session_state["coachnow_run_group_name"] = profile.get("group_name", "")
if "coachnow_run_group_url" not in st.session_state:
    st.session_state["coachnow_run_group_url"] = profile.get("group_url", "")
if "coachnow_group_url_editor" not in st.session_state:
    st.session_state["coachnow_group_url_editor"] = st.session_state["coachnow_run_group_url"]
if "coachnow_group_name" not in st.session_state:
    st.session_state["coachnow_group_name"] = profile.get("group_name", "") or "Default Group"
if "coachnow_loaded_group_id" not in st.session_state:
    st.session_state["coachnow_loaded_group_id"] = st.session_state.get("coachnow_selected_group_id", "")

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


def persist_profile_from_state() -> None:
    normalized = normalize_profile_payload(
        {
            "setups": st.session_state.get("coachnow_setups", []),
            "active_setup_id": st.session_state.get("coachnow_active_setup_id", ""),
            "accounts": st.session_state.get("coachnow_accounts", []),
            "active_account_id": st.session_state.get("coachnow_active_account_id", ""),
            "libraries": st.session_state.get("coachnow_libraries", []),
            "active_library_id": st.session_state.get("coachnow_active_library_id", ""),
            "groups": st.session_state.get("coachnow_groups", []),
            "active_group_id": st.session_state.get("coachnow_active_group_id", ""),
        }
    )
    st.session_state["coachnow_setups"] = normalized["setups"]
    st.session_state["coachnow_active_setup_id"] = normalized["active_setup_id"]
    st.session_state["coachnow_accounts"] = normalized["accounts"]
    st.session_state["coachnow_active_account_id"] = normalized["active_account_id"]
    st.session_state["coachnow_libraries"] = normalized["libraries"]
    st.session_state["coachnow_active_library_id"] = normalized["active_library_id"]
    st.session_state["coachnow_groups"] = normalized["groups"]
    st.session_state["coachnow_active_group_id"] = normalized["active_group_id"]

    save_profile(
        st.session_state.get("coachnow_base_url", DEFAULT_BASE_URL),
        st.session_state.get("coachnow_token", DEFAULT_TOKEN),
        setups=normalized["setups"],
        active_setup_id=normalized["active_setup_id"],
        accounts=normalized["accounts"],
        active_account_id=normalized["active_account_id"],
        libraries=normalized["libraries"],
        active_library_id=normalized["active_library_id"],
        groups=normalized["groups"],
        active_group_id=normalized["active_group_id"],
    )


def save_run_settings_to_runner(
    base_url: str,
    token: str,
    run_group_url: str,
    run_library_url: str,
    run_profile_dir: str,
) -> Tuple[bool, str]:
    clean_group = str(run_group_url or "").strip()
    clean_library = str(run_library_url or "").strip()
    clean_profile = str(run_profile_dir or "").strip()

    if not clean_library:
        return False, "Library URL fehlt."
    if not clean_group:
        return False, "Group URL fehlt."
    if not clean_profile:
        return False, "Profile dir fehlt (CoachNow Account)."

    current_settings = st.session_state.get("coachnow_settings_cache", {})
    if not isinstance(current_settings, dict) or not current_settings:
        ok, data = api_call(base_url, token, "GET", "/api/settings")
        if not ok:
            return False, f"Load settings failed: {data}"
        current_settings = data.get("settings", {})
        if not isinstance(current_settings, dict):
            current_settings = {}

    payload_settings = dict(current_settings)
    payload_settings["libraryUrl"] = clean_library
    payload_settings["groupUrl"] = clean_group
    payload_settings["profileDir"] = clean_profile

    ok, data = api_call(base_url, token, "POST", "/api/settings", payload={"settings": payload_settings})
    if not ok:
        return False, f"Save settings failed: {data}"

    st.session_state["coachnow_settings_cache"] = data.get("settings", payload_settings)
    return True, "Account/Group gespeichert (Library fix auf /resources)."


# Normalize all profile lists/ids once per run.
normalized_state = normalize_profile_payload(
    {
        "setups": st.session_state.get("coachnow_setups", []),
        "active_setup_id": st.session_state.get("coachnow_active_setup_id", ""),
        "accounts": st.session_state.get("coachnow_accounts", []),
        "active_account_id": st.session_state.get("coachnow_active_account_id", ""),
        "libraries": st.session_state.get("coachnow_libraries", []),
        "active_library_id": st.session_state.get("coachnow_active_library_id", ""),
        "groups": st.session_state.get("coachnow_groups", []),
        "active_group_id": st.session_state.get("coachnow_active_group_id", ""),
    }
)
st.session_state["coachnow_setups"] = normalized_state["setups"]
st.session_state["coachnow_active_setup_id"] = normalized_state["active_setup_id"]
st.session_state["coachnow_accounts"] = normalized_state["accounts"]
st.session_state["coachnow_active_account_id"] = normalized_state["active_account_id"]
st.session_state["coachnow_libraries"] = normalized_state["libraries"]
st.session_state["coachnow_active_library_id"] = normalized_state["active_library_id"]
st.session_state["coachnow_groups"] = normalized_state["groups"]
st.session_state["coachnow_active_group_id"] = normalized_state["active_group_id"]

setups = st.session_state["coachnow_setups"]
accounts = st.session_state["coachnow_accounts"]
libraries = st.session_state["coachnow_libraries"]
groups = st.session_state["coachnow_groups"]

if not find_by_id(setups, st.session_state.get("coachnow_selected_setup_id", "")):
    st.session_state["coachnow_selected_setup_id"] = st.session_state["coachnow_active_setup_id"]
if not find_by_id(accounts, st.session_state.get("coachnow_selected_account_id", "")):
    st.session_state["coachnow_selected_account_id"] = st.session_state["coachnow_active_account_id"]
if not find_by_id(libraries, st.session_state.get("coachnow_selected_library_id", "")):
    st.session_state["coachnow_selected_library_id"] = st.session_state["coachnow_active_library_id"]
if not find_by_id(groups, st.session_state.get("coachnow_selected_group_id", "")):
    st.session_state["coachnow_selected_group_id"] = st.session_state["coachnow_active_group_id"]

st.subheader("Run Control")

pick_row_1, pick_row_2 = st.columns(2)
setup_ids = [x["id"] for x in setups]
setup_label_map = {x["id"]: f"{x['name']} ({x['base_url']})" for x in setups}
selected_setup_id = pick_row_1.selectbox(
    "Saved Setups (Machine)",
    options=setup_ids,
    index=max(0, setup_ids.index(st.session_state["coachnow_selected_setup_id"])),
    format_func=lambda sid: setup_label_map.get(sid, sid),
)
st.session_state["coachnow_selected_setup_id"] = selected_setup_id

account_ids = [x["id"] for x in accounts]
account_label_map = {x["id"]: f"{x['name']} ({x['profile_dir']})" for x in accounts}
selected_account_id = pick_row_2.selectbox(
    "CoachNow Accounts",
    options=account_ids,
    index=max(0, account_ids.index(st.session_state["coachnow_selected_account_id"])),
    format_func=lambda aid: account_label_map.get(aid, aid),
)
st.session_state["coachnow_selected_account_id"] = selected_account_id

pick_row_3 = st.columns(1)[0]
group_ids = [x["id"] for x in groups]
group_label_map = {x["id"]: f"{x['name']} ({x['url']})" for x in groups}
selected_group_id = pick_row_3.selectbox(
    "Saved Groups",
    options=group_ids,
    index=max(0, group_ids.index(st.session_state["coachnow_selected_group_id"])),
    format_func=lambda gid: group_label_map.get(gid, gid),
)
st.session_state["coachnow_selected_group_id"] = selected_group_id

selected_library_id = st.session_state.get("coachnow_active_library_id", "")
if not find_by_id(libraries, selected_library_id):
    selected_library_id = libraries[0]["id"]
st.session_state["coachnow_selected_library_id"] = selected_library_id

selected_setup = find_by_id(setups, selected_setup_id) or setups[0]
selected_account = find_by_id(accounts, selected_account_id) or accounts[0]
selected_library = find_by_id(libraries, selected_library_id) or libraries[0]
selected_group = find_by_id(groups, selected_group_id) or groups[0]

if st.session_state.get("coachnow_loaded_setup_id", "") != selected_setup_id:
    st.session_state["coachnow_base_url"] = selected_setup["base_url"]
    st.session_state["coachnow_token"] = selected_setup["token"]
    st.session_state["coachnow_setup_name"] = selected_setup["name"]
    st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
    st.rerun()

if st.session_state.get("coachnow_loaded_account_id", "") != selected_account_id:
    st.session_state["coachnow_run_account_name"] = selected_account["name"]
    st.session_state["coachnow_run_profile_dir"] = selected_account["profile_dir"]
    st.session_state["coachnow_account_profile_dir"] = selected_account["profile_dir"]
    st.session_state["coachnow_account_name"] = selected_account["name"]
    st.session_state["coachnow_loaded_account_id"] = selected_account_id
    st.rerun()

if st.session_state.get("coachnow_loaded_library_id", "") != selected_library_id:
    st.session_state["coachnow_run_library_name"] = selected_library["name"]
    st.session_state["coachnow_run_library_url"] = selected_library["url"]
    st.session_state["coachnow_library_url_editor"] = selected_library["url"]
    st.session_state["coachnow_library_name"] = selected_library["name"]
    st.session_state["coachnow_loaded_library_id"] = selected_library_id
    st.rerun()

if st.session_state.get("coachnow_loaded_group_id", "") != selected_group_id:
    st.session_state["coachnow_run_group_name"] = selected_group["name"]
    st.session_state["coachnow_run_group_url"] = selected_group["url"]
    st.session_state["coachnow_group_url_editor"] = selected_group["url"]
    st.session_state["coachnow_group_name"] = selected_group["name"]
    st.session_state["coachnow_loaded_group_id"] = selected_group_id
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

active_setup = find_by_id(setups, st.session_state["coachnow_active_setup_id"]) or {}
active_account = find_by_id(accounts, st.session_state["coachnow_active_account_id"]) or {}
active_library = find_by_id(libraries, st.session_state["coachnow_active_library_id"]) or {}
active_group = find_by_id(groups, st.session_state["coachnow_active_group_id"]) or {}

st.caption(
    f"Aktiv Setup: {active_setup.get('name', 'n/a')} | "
    f"Aktiv Account: {active_account.get('name', 'n/a')} | "
    f"Aktiv Group: {active_group.get('name', 'n/a')}"
)

run_account_name = st.text_input("CoachNow Account (label)", key="coachnow_run_account_name")
run_library_url = DEFAULT_LIBRARY_URL
st.session_state["coachnow_run_library_url"] = run_library_url
run_group_url = st.text_input(
    "Group URL (Posting target)",
    key="coachnow_run_group_url",
    placeholder="https://app.coachnow.io/groups/<group-id>",
)
run_profile_dir = st.text_input(
    "Account Profile Dir",
    key="coachnow_run_profile_dir",
    help="Playwright profile folder, bestimmt den eingeloggten CoachNow-Account.",
)

apply_a, apply_b, apply_c = st.columns(3)
if apply_a.button("Use selected setup", use_container_width=True):
    st.session_state["coachnow_active_setup_id"] = selected_setup_id
    st.session_state["coachnow_base_url"] = selected_setup["base_url"]
    st.session_state["coachnow_token"] = selected_setup["token"]
    st.session_state["coachnow_setup_name"] = selected_setup["name"]
    st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
    persist_profile_from_state()
    st.success(f"Aktiv gesetzt (Setup): {selected_setup['name']}")

if apply_b.button("Use selected account", use_container_width=True):
    st.session_state["coachnow_active_account_id"] = selected_account_id
    st.session_state["coachnow_run_account_name"] = selected_account["name"]
    st.session_state["coachnow_run_profile_dir"] = selected_account["profile_dir"]
    st.session_state["coachnow_account_name"] = selected_account["name"]
    st.session_state["coachnow_loaded_account_id"] = selected_account_id
    persist_profile_from_state()
    st.success(f"Aktiv gesetzt (Account): {selected_account['name']}")

if apply_c.button("Use selected group", use_container_width=True):
    st.session_state["coachnow_active_group_id"] = selected_group_id
    st.session_state["coachnow_run_group_name"] = selected_group["name"]
    st.session_state["coachnow_run_group_url"] = selected_group["url"]
    st.session_state["coachnow_group_url_editor"] = selected_group["url"]
    st.session_state["coachnow_group_name"] = selected_group["name"]
    st.session_state["coachnow_loaded_group_id"] = selected_group_id
    persist_profile_from_state()
    st.success(f"Aktiv gesetzt (Group): {selected_group['name']}")

conn_a, conn_b = st.columns([1, 1])
if conn_a.button("Connect selected", use_container_width=True):
    refresh_status(base_url, token)
    refresh_settings(base_url, token)
    refresh_logs(base_url, token, 200)
    refresh_athletes(base_url, token)
if conn_b.button("Reload all", use_container_width=True):
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
    ok_run, run_msg = save_run_settings_to_runner(
        base_url=base_url,
        token=token,
        run_group_url=run_group_url,
        run_library_url=run_library_url,
        run_profile_dir=run_profile_dir,
    )
    if not ok_run:
        st.error(run_msg)
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
    ok_run, run_msg = save_run_settings_to_runner(
        base_url=base_url,
        token=token,
        run_group_url=run_group_url,
        run_library_url=run_library_url,
        run_profile_dir=run_profile_dir,
    )
    if not ok_run:
        st.error(run_msg)
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
    setup_name = st.text_input("Setup Name", key="coachnow_setup_name")
    setup_base_url = st.text_input("Control URL", key="coachnow_base_url", help="Example: http://127.0.0.1:8787")
    setup_token = st.text_input("API Token (optional)", key="coachnow_token", type="password")

    setup_a, setup_b, setup_c = st.columns(3)
    if setup_a.button("Save active setup", use_container_width=True):
        clean_name = str(setup_name).strip()
        clean_url = str(setup_base_url).strip()
        if not clean_name:
            st.error("Setup Name fehlt.")
        elif not clean_url:
            st.error("Control URL fehlt.")
        else:
            for item in setups:
                if item["id"] == selected_setup_id:
                    item["name"] = clean_name
                    item["base_url"] = clean_url
                    item["token"] = str(setup_token).strip()
                    break
            st.session_state["coachnow_setups"] = setups
            st.session_state["coachnow_loaded_setup_id"] = selected_setup_id
            persist_profile_from_state()
            st.success("Setup gespeichert.")

    if setup_b.button("Save as new setup", use_container_width=True):
        clean_name = str(setup_name).strip()
        clean_url = str(setup_base_url).strip()
        if not clean_name:
            st.error("Setup Name fehlt.")
        elif not clean_url:
            st.error("Control URL fehlt.")
        else:
            new_setup = normalize_setup(
                {
                    "id": make_id(),
                    "name": clean_name,
                    "base_url": clean_url,
                    "token": str(setup_token).strip(),
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
    st.markdown("#### Account Details")
    account_name = st.text_input("Account Name", key="coachnow_account_name")
    account_profile_dir = st.text_input(
        "Account Profile Dir",
        key="coachnow_account_profile_dir",
        help="Dieser Profilordner steuert den CoachNow-Login.",
    )

    account_a, account_b, account_c = st.columns(3)
    if account_a.button("Save active account", use_container_width=True):
        clean_name = str(account_name).strip()
        clean_profile = str(account_profile_dir).strip()
        if not clean_name:
            st.error("Account Name fehlt.")
        elif not clean_profile:
            st.error("Profile Dir fehlt.")
        else:
            for item in accounts:
                if item["id"] == selected_account_id:
                    item["name"] = clean_name
                    item["profile_dir"] = clean_profile
                    break
            st.session_state["coachnow_accounts"] = accounts
            st.session_state["coachnow_run_account_name"] = clean_name
            st.session_state["coachnow_run_profile_dir"] = clean_profile
            st.session_state["coachnow_account_profile_dir"] = clean_profile
            st.session_state["coachnow_loaded_account_id"] = selected_account_id
            persist_profile_from_state()
            st.success("Account gespeichert.")

    if account_b.button("Save as new account", use_container_width=True):
        clean_name = str(account_name).strip()
        clean_profile = str(account_profile_dir).strip()
        if not clean_name:
            st.error("Account Name fehlt.")
        elif not clean_profile:
            st.error("Profile Dir fehlt.")
        else:
            new_account = normalize_account(
                {
                    "id": make_id(),
                    "name": clean_name,
                    "profile_dir": clean_profile,
                },
                fallback_name="Account",
            )
            accounts.append(new_account)
            st.session_state["coachnow_accounts"] = accounts
            st.session_state["coachnow_selected_account_id"] = new_account["id"]
            st.session_state["coachnow_loaded_account_id"] = new_account["id"]
            persist_profile_from_state()
            st.success(f"Neuer Account gespeichert: {new_account['name']}")
            st.rerun()

    if account_c.button("Activate selected account", use_container_width=True):
        st.session_state["coachnow_active_account_id"] = selected_account_id
        st.session_state["coachnow_run_account_name"] = selected_account["name"]
        st.session_state["coachnow_run_profile_dir"] = selected_account["profile_dir"]
        st.session_state["coachnow_account_profile_dir"] = selected_account["profile_dir"]
        st.session_state["coachnow_account_name"] = selected_account["name"]
        st.session_state["coachnow_loaded_account_id"] = selected_account_id
        persist_profile_from_state()
        st.success(f"Aktiv gesetzt: {selected_account['name']}")

    st.divider()
    st.markdown("#### Group Details")
    group_name = st.text_input("Group Name", key="coachnow_group_name")
    group_url = st.text_input("Group URL", key="coachnow_group_url_editor")

    grp_a, grp_b, grp_c = st.columns(3)
    if grp_a.button("Save active group", use_container_width=True):
        clean_name = str(group_name).strip()
        clean_url = str(group_url).strip()
        if not clean_name:
            st.error("Group Name fehlt.")
        elif not clean_url:
            st.error("Group URL fehlt.")
        else:
            for item in groups:
                if item["id"] == selected_group_id:
                    item["name"] = clean_name
                    item["url"] = clean_url
                    break
            st.session_state["coachnow_groups"] = groups
            st.session_state["coachnow_run_group_name"] = clean_name
            st.session_state["coachnow_run_group_url"] = clean_url
            st.session_state["coachnow_group_url_editor"] = clean_url
            st.session_state["coachnow_loaded_group_id"] = selected_group_id
            persist_profile_from_state()
            st.success("Group gespeichert.")

    if grp_b.button("Save as new group", use_container_width=True):
        clean_name = str(group_name).strip()
        clean_url = str(group_url).strip()
        if not clean_name:
            st.error("Group Name fehlt.")
        elif not clean_url:
            st.error("Group URL fehlt.")
        else:
            new_group = normalize_group(
                {
                    "id": make_id(),
                    "name": clean_name,
                    "url": clean_url,
                },
                fallback_name="Group",
            )
            groups.append(new_group)
            st.session_state["coachnow_groups"] = groups
            st.session_state["coachnow_selected_group_id"] = new_group["id"]
            st.session_state["coachnow_loaded_group_id"] = new_group["id"]
            persist_profile_from_state()
            st.success(f"Neue Group gespeichert: {new_group['name']}")
            st.rerun()

    if grp_c.button("Activate selected group", use_container_width=True):
        st.session_state["coachnow_active_group_id"] = selected_group_id
        st.session_state["coachnow_run_group_name"] = selected_group["name"]
        st.session_state["coachnow_run_group_url"] = selected_group["url"]
        st.session_state["coachnow_group_url_editor"] = selected_group["url"]
        st.session_state["coachnow_group_name"] = selected_group["name"]
        st.session_state["coachnow_loaded_group_id"] = selected_group_id
        persist_profile_from_state()
        st.success(f"Aktiv gesetzt: {selected_group['name']}")

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

    st.divider()
    st.markdown("#### Logs")
    l1, l2 = st.columns([1, 5])
    log_limit = l1.slider("Lines", min_value=50, max_value=2000, value=350, step=50)
    if l2.button("Refresh logs", use_container_width=True):
        refresh_logs(base_url, token, log_limit)
    if not st.session_state["coachnow_logs_cache"]:
        refresh_logs(base_url, token, log_limit)
    logs = st.session_state.get("coachnow_logs_cache", [])
    st.code("\n".join(logs[-log_limit:]) if logs else "(no logs)", language=None)

    st.divider()
    st.markdown("#### Runner Settings")
    cur = st.session_state.get("coachnow_settings_cache", {})
    add_env_raw = json.dumps(
        cur.get("additionalEnv", {}) if isinstance(cur.get("additionalEnv", {}), dict) else {},
        indent=2,
    )

    with st.form("coachnow_adv_settings_form"):
        c1, c2 = st.columns(2)
        group_url_adv = c1.text_input("GROUP_URL", value=string_or_default(cur, "groupUrl", ""))
        library_url_adv = c2.text_input(
            "LIBRARY_URL",
            value=string_or_default(cur, "libraryUrl", DEFAULT_LIBRARY_URL),
        )

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
        clip_start = f1.number_input(
            "CLIP_START_SECONDS",
            min_value=0.0,
            max_value=600.0,
            value=float(number_or_default(cur, "clipStartSeconds", 0)),
            step=0.5,
        )
        clip_seconds = f2.number_input(
            "CLIP_SECONDS",
            min_value=1.0,
            max_value=600.0,
            value=float(number_or_default(cur, "clipSeconds", 60)),
            step=1.0,
        )
        whisper_beam = f3.number_input(
            "WHISPER_BEAM",
            min_value=1,
            max_value=5,
            value=int(number_or_default(cur, "whisperBeam", 5)),
            step=1,
        )

        g1, g2, g3 = st.columns(3)
        multi_transcribe = g1.checkbox("MULTI_TRANSCRIBE", value=bool_or_default(cur, "multiTranscribe", True))
        adaptive_pass = g2.checkbox("ADAPTIVE_PASS_BY_DURATION", value=bool_or_default(cur, "adaptivePassByDuration", True))
        multi_pass_max = g3.number_input(
            "MULTI_PASS_MAX",
            min_value=1,
            max_value=24,
            value=int(number_or_default(cur, "multiPassMax", 6)),
            step=1,
        )

        h1, h2, h3 = st.columns(3)
        short_single = h1.number_input(
            "SHORT_SINGLE_PASS_MAX_SECONDS",
            min_value=5.0,
            max_value=120.0,
            value=float(number_or_default(cur, "shortSinglePassMaxSeconds", 6)),
            step=1.0,
        )
        medium_dual = h2.number_input(
            "MEDIUM_DUAL_PASS_MAX_SECONDS",
            min_value=10.0,
            max_value=180.0,
            value=float(number_or_default(cur, "mediumDualPassMaxSeconds", 25)),
            step=1.0,
        )
        first_name_mode = h3.selectbox(
            "FIRST_NAME_ONLY_MODE",
            options=["generic", "athlete"],
            index=0 if string_or_default(cur, "firstNameOnlyMode", "generic") == "generic" else 1,
        )

        i1, i2, i3 = st.columns(3)
        context_enabled = i1.checkbox("CONTEXT_TAGS_ENABLED", value=bool_or_default(cur, "contextTagsEnabled", True))
        context_tags = i2.text_input(
            "CONTEXT_TAGS",
            value=string_or_default(cur, "contextTags", "Gate,HalfLap,FullLap,Crash,Review"),
        )
        poll_ms = i3.number_input(
            "POLL_MS",
            min_value=1000,
            max_value=120000,
            value=int(number_or_default(cur, "pollMs", 5000)),
            step=500,
        )

        j1, j2, j3 = st.columns(3)
        python_bin = j1.text_input("PYTHON_BIN", value=string_or_default(cur, "pythonBin", ""))
        transcribe_script = j2.text_input("TRANSCRIBE_SCRIPT", value=string_or_default(cur, "transcribeScript", "transcribe.py"))
        athletes_path = j3.text_input("ATHLETES_PATH", value=string_or_default(cur, "athletesPath", "athletes.json"))

        k1, k2, k3 = st.columns(3)
        profile_dir_adv = k1.text_input("PROFILE_DIR", value=string_or_default(cur, "profileDir", DEFAULT_PROFILE_DIR))
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
                    "groupUrl": group_url_adv.strip(),
                    "libraryUrl": library_url_adv.strip(),
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
                    "profileDir": profile_dir_adv.strip(),
                    "additionalEnv": additional_env,
                }
                ok, data = api_call(base_url, token, "POST", "/api/settings", payload={"settings": payload_settings})
                if ok:
                    st.success("Advanced settings saved.")
                    st.session_state["coachnow_settings_cache"] = data.get("settings", payload_settings)
                    st.session_state["coachnow_run_library_url"] = payload_settings.get("libraryUrl", "")
                    st.session_state["coachnow_run_group_url"] = payload_settings.get("groupUrl", "")
                    st.session_state["coachnow_run_profile_dir"] = payload_settings.get("profileDir", DEFAULT_PROFILE_DIR)
                else:
                    st.error(f"Save failed: {data}")
