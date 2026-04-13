import os

import streamlit as st
import streamlit.components.v1 as components

from access_control import render_sidebar_nav, require_page_access


DEFAULT_XPOWER_REF_URL = "/xpower-ref/"


def xpower_ref_url() -> str:
    raw = str(os.environ.get("XPOWER_REF_URL", DEFAULT_XPOWER_REF_URL) or "").strip()
    if not raw:
        return DEFAULT_XPOWER_REF_URL
    return raw


st.set_page_config(page_title="xPower Ref", layout="wide", initial_sidebar_state="expanded")
require_page_access({"admin", "coach"}, "xPower Ref")
render_sidebar_nav()

app_url = xpower_ref_url()

st.title("xPower Ref")
st.caption("Unveraenderte Referenzinstanz von Micahs xPower-App.")

with st.sidebar:
    st.subheader("xPower Ref")
    st.caption(f"Quelle: {app_url}")

st.markdown(
    """
    Diese Seite zeigt die Referenzinstanz ohne deine Entwicklungsanpassungen.
    Nutze sie zum direkten Vergleich mit `xPower Dev`.
    """
)

cols = st.columns([1, 1, 6])
with cols[0]:
    if st.button("Neu laden", use_container_width=True):
        st.rerun()
with cols[1]:
    st.link_button("Extern oeffnen", app_url, use_container_width=True)

components.iframe(app_url, height=1400, scrolling=True)
