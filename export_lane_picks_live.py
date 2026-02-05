import re
import json
import requests
from pathlib import Path
from datetime import datetime
import pandas as pd

EVENT_ID = "20250614_bmx"

CMS_URL = f"https://prod.chronorace.be/api/results/uci/dh/cms/{EVENT_ID}"
RESULTS_BASE = f"https://prod.chronorace.be/api/results/generic/uci/{EVENT_ID}/bmx?key="

HEADERS = {"accept": "application/json", "user-agent": "HeatScout/1.0"}

OUTDIR = Path("out")
OUTDIR.mkdir(exist_ok=True)

def walk(node):
    if not isinstance(node, dict):
        return
    yield node
    childs = node.get("Childs")
    if isinstance(childs, list):
        for c in childs:
            yield from walk(c)

def extract_round_keys_from_cms():
    """Findet alle Live Timing nodes und extrahiert Params.key (z.B. 9101, 9204...)."""
    data = requests.get(CMS_URL, headers=HEADERS, timeout=20).json()
    keys = set()

    for n in walk(data):
        name = (n.get("DisplayName") or n.get("Name") or "").strip().lower()
        if name == "live timing":
            route = n.get("Route", "")
            m = re.search(r'"key"\s*:\s*"(\d+)"', route)
            if m:
                keys.add(int(m.group(1)))

    return sorted(keys)

def fetch_round(round_key: int) -> dict:
    url = f"{RESULTS_BASE}{round_key}%2Fresults"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

def main():
    round_keys = extract_round_keys_from_cms()
    print("Round keys found:", round_keys)

    rows = []
    for rk in round_keys:
        try:
            data = fetch_round(rk)
        except Exception as e:
            print(f"Fetch failed for {rk}: {e}")
            continue

        group_id = data.get("GroupId")
        round_id = data.get("Id")
        round_title = data.get("Title")

        for heat in data.get("Heats", []):
            heat_id = heat.get("Id")
            heat_title = heat.get("Title")
            heat_status = heat.get("Status")
            start_time = heat.get("StartTimeString")

            for r in heat.get("HeatData", []):
                bib = r.get("Bib")
                name = (f"{r.get('FirstName','')} {r.get('LastName','')}".strip())
                nation = r.get("Nation") or r.get("NOC") or r.get("CountryCode")
                pick_order = r.get("LaneSelectionOrder")
                lane = r.get("Lane")
                lane_idx = r.get("LaneIdx")

                rows.append({
                    "event_id": EVENT_ID,
                    "group_id": group_id,
                    "round_key": rk,          # der key aus Route (9101..)
                    "round_id": round_id,     # Id aus JSON (sollte gleich sein)
                    "round_title": round_title,
                    "heat_id": heat_id,
                    "heat_title": heat_title,
                    "heat_status": heat_status,
                    "start_time_string": start_time,
                    "bib": bib,
                    "name": name,
                    "nation": nation,
                    "pick_order": pick_order,
                    "lane": lane,
                    "lane_idx": lane_idx,
                })

    df = pd.DataFrame(rows)

    if df.empty:
        print("No rows produced. Either endpoints returned empty or structure changed.")
        return

    df = df.sort_values(["group_id","round_key","heat_id","pick_order","lane_idx"], kind="stable")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTDIR / f"lane_picks_{EVENT_ID}_{ts}.csv"
    xlsx_path = OUTDIR / f"lane_picks_{EVENT_ID}_{ts}.xlsx"

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="lane_picks", index=False)

    # Konsole: schnelle Sichtkontrolle
    cols = ["group_id","round_title","heat_title","bib","name","pick_order","lane"]
    print("\nPreview (first 40 rows):")
    print(df[cols].head(40).to_string(index=False))

    print(f"\nWrote:\n- {csv_path}\n- {xlsx_path}")

if __name__ == "__main__":
    main()
