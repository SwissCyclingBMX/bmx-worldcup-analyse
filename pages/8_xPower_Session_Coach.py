import os

import streamlit as st
import streamlit.components.v1 as components

from access_control import render_sidebar_nav, require_page_access


DEFAULT_XPOWER_SESSION_COACH_URL = "/session-coach/"


def xpower_session_coach_url() -> str:
    raw = str(os.environ.get("XPOWER_SESSION_COACH_URL", DEFAULT_XPOWER_SESSION_COACH_URL) or "").strip()
    if not raw:
        return DEFAULT_XPOWER_SESSION_COACH_URL
    return raw


st.set_page_config(page_title="xPower Session / Coach", layout="wide", initial_sidebar_state="expanded")
require_page_access({"admin", "coach"}, "xPower Session / Coach")
render_sidebar_nav()

components.iframe(xpower_session_coach_url(), height=1600, scrolling=True)
