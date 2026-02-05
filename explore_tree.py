import requests

URL = "https://prod.chronorace.be/api/results/uci/dh/cms/20250614_bmx"
HEADERS = {"accept": "application/json", "user-agent": "HeatScout/1.0"}

def node_label(n):
    name = n.get("DisplayName") or n.get("Name") or ""
    typ = n.get("Type") or ""
    return f"{name} [{typ}]".strip()

def find_nodes_with_key(root, key, max_hits=10):
    """Findet Knoten im Childs-Baum, die ein bestimmtes Key enthalten."""
    hits = []
    stack = [(root, "root")]
    while stack and len(hits) < max_hits:
        n, path = stack.pop()
        if isinstance(n, dict) and key in n:
            hits.append((path, n))
        childs = n.get("Childs") if isinstance(n, dict) else None
        if isinstance(childs, list):
            for i, c in enumerate(childs):
                stack.append((c, f"{path}.Childs[{i}]"))
    return hits

def print_tree(root, depth=0, max_depth=4, max_children=12):
    """Druckt eine Baumvorschau, damit du siehst wo Kategorien/Rounds liegen."""
    indent = "  " * depth
    if not isinstance(root, dict):
        return
    print(f"{indent}- {node_label(root)}")
    if depth >= max_depth:
        return
    childs = root.get("Childs")
    if isinstance(childs, list):
        for i, c in enumerate(childs[:max_children]):
            print_tree(c, depth+1, max_depth=max_depth, max_children=max_children)
        if len(childs) > max_children:
            print(f"{indent}  ... ({len(childs)-max_children} more)")

def main():
    r = requests.get(URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    print("Top-level keys:", list(data.keys()))
    print("\n=== Tree preview (depth 4) ===")
    print_tree(data, max_depth=4, max_children=20)

    # Jetzt gezielt nach deinem bekannten Feld suchen:
    # LaneSelectionOrder ist ein sehr guter Marker
    print("\n=== Search for nodes containing 'LaneSelectionOrder' ===")
    hits = find_nodes_with_key(data, "LaneSelectionOrder", max_hits=5)
    if not hits:
        # Wenn LaneSelectionOrder nicht direkt am Knoten hängt, suchen wir nach HeatData
        print("No direct hits. Searching for 'HeatData' nodes...")
        hits2 = find_nodes_with_key(data, "HeatData", max_hits=5)
        if hits2:
            for path, n in hits2:
                print("Hit:", path, "| label:", node_label(n))
                hd = n.get("HeatData")
                if isinstance(hd, list) and hd:
                    print("  HeatData[0] keys:", list(hd[0].keys())[:40])
                    print("  Example:", {k: hd[0].get(k) for k in ["Bib","FirstName","LastName","LaneSelectionOrder","Lane","LaneIdx"]})
        else:
            print("No HeatData hits either. We'll need to search for 'Heats' or 'Results' keys next.")
    else:
        for path, n in hits:
            print("Hit:", path, "| label:", node_label(n))
            print("  Keys:", list(n.keys())[:40])

if __name__ == "__main__":
    main()
