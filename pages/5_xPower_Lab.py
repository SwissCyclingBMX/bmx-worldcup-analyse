import os

import streamlit as st
import streamlit.components.v1 as components

from access_control import require_page_access


DEFAULT_XPOWER_DEV_URL = "/xpower/"


def xpower_dev_url() -> str:
    raw = str(os.environ.get("XPOWER_DEV_URL", DEFAULT_XPOWER_DEV_URL) or "").strip()
    if not raw:
        return DEFAULT_XPOWER_DEV_URL
    return raw


st.set_page_config(page_title="xPower Dev", layout="wide", initial_sidebar_state="collapsed")
require_page_access({"admin", "coach"}, "xPower Dev")

target_url = xpower_dev_url()

st.markdown("Redirecting to xPower Dev...")
st.link_button("Open xPower Dev", target_url, use_container_width=True)

components.html(
    f"""
    <a id="xpower-dev-direct-link" href="{target_url}" target="_top" rel="noopener noreferrer">Open xPower Dev</a>
    <script>
      (function() {{
        const url = {target_url!r};
        const go = () => {{
          try {{ window.parent.location.href = url; return; }} catch (e) {{}}
          try {{ window.top.location.href = url; return; }} catch (e) {{}}
          try {{ window.top.location.replace(url); return; }} catch (e) {{}}
          try {{ window.open(url, "_top"); return; }} catch (e) {{}}
          try {{ document.getElementById("xpower-dev-direct-link").click(); }} catch (e) {{}}
        }};
        setTimeout(go, 50);
        setTimeout(go, 250);
        setTimeout(go, 800);
      }})();
    </script>
    """,
    height=20,
    scrolling=False,
)
