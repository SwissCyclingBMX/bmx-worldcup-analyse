import requests
import json

URL = "https://prod.chronorace.be/api/results/uci/dh/cms/20250614_bmx"
HEADERS = {"accept": "application/json", "user-agent": "HeatScout/1.0"}

def walk(node, path="root"):
    if not isinstance(node, dict):
        return
    yield path, node
    childs = node.get("Childs")
    if isinstance(childs, list):
        for i, c in enumerate(childs):
            yield from walk(c, f"{path}.Childs[{i}]")

def is_live_timing(n):
    name = (n.get("DisplayName") or n.get("Name") or "").strip().lower()
    return name == "live timing"

def main():
    data = requests.get(URL, headers=HEADERS, timeout=20).json()

    hits = []
    for path, n in walk(data):
        if is_live_timing(n):
            hits.append((path, n))

    print(f"Found {len(hits)} Live Timing nodes.\n")

    for i, (path, n) in enumerate(hits[:30], start=1):
        print(f"--- Hit #{i} ---")
        print("Path:", path)
        print("Name:", n.get("Name"))
        print("DisplayName:", n.get("DisplayName"))
        print("Type:", n.get("Type"))
        print("All keys:", list(n.keys()))
        # Voll dumpen, um Link/Key/Id zu sehen
        print(json.dumps(n, ensure_ascii=False, indent=2)[:4000])
        print()

if __name__ == "__main__":
    main()
