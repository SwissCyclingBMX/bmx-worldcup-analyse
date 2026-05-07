import base64
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import streamlit as st

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROLE_OVERRIDE_PATH = os.path.join(APP_DIR, ".streamlit", "access_roles.json")

NAV_ITEMS: List[Tuple[str, str, Set[str]]] = [
    ("app.py", "Heat Analyser", {"admin", "coach"}),
    ("pages/3_Athlete_Insights.py", "Athlete Insights", {"admin", "coach"}),
    ("pages/5_xPower_Lab.py", "xPower Dev", {"admin", "coach"}),
    ("pages/8_xPower_Session_Coach.py", "xPower Session / Coach", {"admin", "coach"}),
    ("pages/6_xPower_Ref.py", "xPower Ref", {"admin", "coach"}),
    ("pages/7_xPower_Sync.py", "xPower Sync", {"admin", "coach"}),
    ("pages/4_Live_Polling.py", "Live Polling", {"admin"}),
    ("pages/9_CoachNow_Automation.py", "CoachNow Automation", {"admin"}),
]

DIRECT_NAV_TARGETS: Dict[str, str] = {
    "pages/5_xPower_Lab.py": "/xpower/",
    "pages/8_xPower_Session_Coach.py": "/session-coach/",
    "pages/6_xPower_Ref.py": "/xpower-ref/",
}


def _normalize_role(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", str(value or "").strip().lower())


def _split_roles(raw: object) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = re.split(r"[\s,;|]+", str(raw))
    return [role for role in (_normalize_role(item) for item in items) if role]


def _context_headers() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        headers = getattr(st.context, "headers", None)
        if headers:
            for key in headers.keys():
                val = headers.get(key)
                if isinstance(val, (list, tuple)):
                    val = ",".join(str(v) for v in val if v is not None)
                out[str(key).lower()] = str(val or "").strip()
    except Exception:
        return {}
    return out


def _decode_jwt_payload(token: str) -> Dict[str, object]:
    token = str(token or "").strip()
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = base64.urlsafe_b64decode(payload.encode("utf-8"))
        parsed = json.loads(data.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _claims_to_roles(claims: Dict[str, object]) -> List[str]:
    roles: List[str] = []
    if not claims:
        return roles
    for key, val in claims.items():
        key_l = str(key).strip().lower()
        if key_l in {"roles", "role", "groups", "permissions"} or key_l.endswith("/roles") or key_l.endswith("/role") or key_l.endswith("/groups"):
            roles.extend(_split_roles(val))
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        roles.extend(_split_roles(realm_access.get("roles")))
    return roles


def _load_role_overrides() -> Dict[str, List[str]]:
    path = os.environ.get("ACCESS_ROLE_OVERRIDES_PATH", ROLE_OVERRIDE_PATH)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, List[str]] = {}
        for email, roles in data.items():
            email_clean = str(email or "").strip().lower()
            if email_clean:
                out[email_clean] = _split_roles(roles)
        return out
    except Exception:
        return {}


def current_user() -> Dict[str, object]:
    headers = _context_headers()
    email = (
        headers.get("x-email")
        or headers.get("x-auth-request-email")
        or headers.get("x-forwarded-email")
        or headers.get("remote-user")
        or ""
    ).strip().lower()
    user = (
        headers.get("x-user")
        or headers.get("x-auth-request-user")
        or headers.get("x-forwarded-user")
        or email
    ).strip()

    roles: List[str] = []
    role_sources: Set[str] = set()
    for hdr in [
        "x-roles",
        "x-role",
        "x-groups",
        "x-auth-request-groups",
        "x-forwarded-groups",
    ]:
        hdr_roles = _split_roles(headers.get(hdr))
        if hdr_roles:
            role_sources.add("headers")
            roles.extend(hdr_roles)

    token_candidates = [
        headers.get("authorization"),
        headers.get("x-access-token"),
        headers.get("x-forwarded-access-token"),
    ]
    claims: Dict[str, object] = {}
    for token in token_candidates:
        if not token:
            continue
        claims = _decode_jwt_payload(token)
        if claims:
            claim_roles = _claims_to_roles(claims)
            if claim_roles:
                role_sources.add("claims")
                roles.extend(claim_roles)
            if not email:
                email = str(claims.get("email") or "").strip().lower()
            if not user:
                user = str(claims.get("nickname") or claims.get("name") or claims.get("sub") or "").strip()
            break

    overrides = _load_role_overrides()
    if email in overrides:
        role_sources.add("override")
        roles.extend(overrides[email])

    local_roles = _split_roles(os.environ.get("LOCAL_ACCESS_ROLES"))
    if local_roles and not headers.get("x-auth-request-email"):
        role_sources.add("local_env")
        roles.extend(local_roles)
        if not email:
            email = "local-dev"
        if not user:
            user = "local-dev"

    deduped: List[str] = []
    seen: Set[str] = set()
    for role in roles:
        if role and role not in seen:
            seen.add(role)
            deduped.append(role)

    return {
        "email": email,
        "user": user,
        "roles": deduped,
        "role_sources": sorted(role_sources),
        "headers": headers,
        "claims": claims,
    }


def current_roles() -> Set[str]:
    return set(current_user().get("roles", []))


def user_has_any_role(allowed_roles: Iterable[str]) -> bool:
    allowed = {_normalize_role(r) for r in allowed_roles if _normalize_role(r)}
    if not allowed:
        return True
    return bool(current_roles() & allowed)


def app_home_url() -> str:
    raw = str(os.environ.get("APP_HOME_URL", "/") or "").strip()
    return raw or "/"


def app_logout_url() -> str:
    raw = str(os.environ.get("APP_LOGOUT_URL", "") or "").strip()
    if raw:
        return raw
    return f"/oauth2/sign_out?rd={quote(app_home_url(), safe='/:%')}"


def render_sidebar_nav() -> None:
    user = current_user()
    email = str(user.get("email") or "").strip()
    if email:
        st.sidebar.caption(f"Angemeldet als: {email}")
    st.sidebar.link_button("Logout", app_logout_url(), use_container_width=True)
    st.sidebar.divider()

    roles = current_roles()
    for script_path, label, allowed_roles in NAV_ITEMS:
        if roles and not (roles & allowed_roles):
            continue
        direct_target = DIRECT_NAV_TARGETS.get(script_path)
        if direct_target:
            st.sidebar.markdown(
                f'<a href="{direct_target}" target="_self" '
                f'style="display:block;padding:0.35rem 0;color:inherit;text-decoration:none;">{label}</a>',
                unsafe_allow_html=True,
            )
            continue
        if os.path.exists(script_path):
            st.sidebar.page_link(script_path, label=label)
    st.sidebar.divider()


def require_page_access(allowed_roles: Sequence[str], page_label: str) -> None:
    allowed = {_normalize_role(r) for r in allowed_roles if _normalize_role(r)}
    user = current_user()
    roles = set(user.get("roles", []))
    email = str(user.get("email") or "").strip()
    role_sources = ",".join(user.get("role_sources", []))
    try:
        print(
            f"[access_control] page={page_label} email={email or '-'} roles={','.join(sorted(roles)) or '-'} sources={role_sources or '-'}",
            file=sys.stderr,
        )
    except Exception:
        pass
    if roles and roles & allowed:
        return
    st.error(f"Kein Zugriff auf {page_label}.")
    if email:
        st.caption(f"Angemeldet als: {email}")
    if roles:
        st.caption("Rollen: " + ", ".join(sorted(roles)))
    else:
        st.caption("Es wurde keine verwertbare Rolle im Login gefunden.")
    st.stop()
