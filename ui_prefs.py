import json
import os
from datetime import date, datetime


APP_DIR = os.path.dirname(os.path.abspath(__file__))
PREFS_PATH = os.environ.get("BMX_UI_PREFS_PATH", os.path.join(APP_DIR, "ui_prefs.json"))


def _load_all():
    if not os.path.exists(PREFS_PATH):
        return {}
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def load_page_prefs(page_name):
    prefs = _load_all().get(page_name, {})
    for key, value in list(prefs.items()):
        if isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) and len(v) == 10 for v in value):
            try:
                prefs[key] = tuple(datetime.strptime(v, "%Y-%m-%d").date() for v in value)
            except ValueError:
                pass
    return prefs


def update_page_prefs(page_name, patch):
    prefs = _load_all()
    page = dict(prefs.get(page_name, {}))
    for key, value in patch.items():
        if isinstance(value, tuple):
            value = [v.isoformat() if isinstance(v, date) else v for v in value]
        elif isinstance(value, list):
            value = [v.isoformat() if isinstance(v, date) else v for v in value]
        page[key] = value
    prefs[page_name] = page
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    with open(PREFS_PATH, "w", encoding="utf-8") as handle:
        json.dump(prefs, handle)
