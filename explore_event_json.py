import requests

URL = "https://prod.chronorace.be/api/results/uci/dh/cms/20250614_bmx"
HEADERS = {"accept": "application/json", "user-agent": "HeatScout/1.0"}

def pick(d, *keys):
    """Sicheres dict-get für verschachtelte Keys."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur

def first_list_key(d, candidates):
    """Findet den ersten Key, der im Dict existiert und eine Liste ist."""
    for k in candidates:
        v = d.get(k)
        if isinstance(v, list):
            return k
    return None

def main():
    r = requests.get(URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    print("Top-level type:", type(data))
    if isinstance(data, dict):
        print("Top-level keys (first 40):", list(data.keys())[:40])

    # Typische Container-Namen in solchen ChronoRace JSONs
    group_key = first_list_key(data, ["Groups", "Group", "categories", "Categories", "data", "Data"])
    if not group_key:
        # Falls groups tiefer liegen:
        data2 = data.get("data") if isinstance(data, dict) else None
        if isinstance(data2, dict):
            group_key = first_list_key(data2, ["Groups", "Categories"])
            if group_key:
                data = data2

    if not group_key:
        print("\nKonnte keine Gruppenliste finden. Bitte poste die ersten 50 Zeilen der JSON (oder keys).")
        return

    groups = data[group_key]
    print(f"\nGefunden: {group_key} -> {len(groups)} Einträge")
    print("Beispiel group keys:", list(groups[0].keys())[:30])

    # In jeder Group liegen meist Rounds
    round_keys = ["Rounds", "Round", "rounds", "Stages", "Phases"]
    heat_keys = ["Heats", "Heat", "heats"]
    heatdata_keys = ["HeatData", "heatData", "Entries", "Riders", "Competitors"]

    # Wir laufen einmal durch die ersten paar Groups und zeigen Pfade
    for gi, g in enumerate(groups[:6]):
        g_name = g.get("Title") or g.get("Name") or g.get("Category") or g.get("CategoryCode") or f"Group#{gi}"
        g_id = g.get("GroupId") or g.get("Id")
        print(f"\n=== GROUP {gi}: {g_name} (GroupId/Id={g_id}) ===")

        rk = first_list_key(g, round_keys)
        if not rk:
            print("  Keine Rounds-Liste gefunden in dieser Group.")
            continue

        rounds = g[rk]
        print(f"  Pfad: {group_key}[{gi}].{rk} -> {len(rounds)} Rounds")
        # zeige 3 Rounds als Beispiel
        for ri, rd in enumerate(rounds[:3]):
            r_title = rd.get("Title") or rd.get("Name") or f"Round#{ri}"
            r_id = rd.get("Id")
            print(f"    - Round {ri}: {r_title} (Id={r_id})")

            hk = first_list_key(rd, heat_keys)
            if not hk:
                print("      Keine Heats-Liste in dieser Round.")
                continue

            heats = rd[hk]
            print(f"      Pfad: ...{hk} -> {len(heats)} Heats")
            if heats:
                h0 = heats[0]
                print("      Beispiel Heat keys:", list(h0.keys())[:25])

                hdk = first_list_key(h0, heatdata_keys)
                if not hdk:
                    print("      Keine HeatData/Entries-Liste im Heat gefunden.")
                    continue

                hd = h0[hdk]
                print(f"      Pfad: ...{hk}[0].{hdk} -> {len(hd)} Zeilen (Rider)")
                if hd:
                    rider0 = hd[0]
                    print("      Beispiel Rider keys:", list(rider0.keys())[:30])
                    print("      Erwartete Felder (falls vorhanden):",
                          "LaneSelectionOrder=", rider0.get("LaneSelectionOrder"),
                          "Lane=", rider0.get("Lane"),
                          "LaneIdx=", rider0.get("LaneIdx"))
        # nur Gruppenübersicht, nicht alles spammen

if __name__ == "__main__":
    main()
