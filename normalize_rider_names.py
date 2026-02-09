#!/usr/bin/env python3
import argparse
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone


def clean_spaces(s: str) -> str:
    return " ".join((s or "").strip().split())


def is_valid_uci_id(v: str) -> bool:
    s = "".join(ch for ch in str(v or "").strip() if ch.isdigit())
    return len(s) >= 8


def norm_name_key(name: str) -> str:
    s = clean_spaces(name).lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(sorted(s.split()))


def choose_name(candidates):
    # candidates: list[(name, count)] -> pick highest count, then longest, then lexicographically.
    if not candidates:
        return ""
    return sorted(candidates, key=lambda x: (-x[1], -len(x[0]), x[0]))[0][0]


def build_canonical_map(conn: sqlite3.Connection):
    master_by_uci = {}
    rows = conn.execute(
        """
        SELECT uci_id, first_name, last_name, COUNT(*) c
        FROM master_results
        WHERE COALESCE(TRIM(uci_id), '') <> ''
        GROUP BY uci_id, first_name, last_name
        """
    ).fetchall()
    grouped = defaultdict(list)
    for uci_id, first_name, last_name, c in rows:
        if not is_valid_uci_id(uci_id):
            continue
        name = clean_spaces(f"{first_name or ''} {last_name or ''}")
        if name:
            grouped[str(uci_id).strip()].append((name, int(c)))
    for uci_id, candidates in grouped.items():
        master_by_uci[uci_id] = choose_name(candidates)

    pick_by_uci = defaultdict(list)
    rows = conn.execute(
        """
        SELECT uci_id, name, COUNT(*) c
        FROM picks
        WHERE COALESCE(TRIM(uci_id), '') <> ''
          AND COALESCE(TRIM(name), '') <> ''
        GROUP BY uci_id, name
        """
    ).fetchall()
    for uci_id, name, c in rows:
        if not is_valid_uci_id(uci_id):
            continue
        n = clean_spaces(name)
        if n:
            pick_by_uci[str(uci_id).strip()].append((n, int(c)))

    canonical = {}
    for uci_id, candidates in pick_by_uci.items():
        if uci_id in master_by_uci and master_by_uci[uci_id]:
            canonical[uci_id] = master_by_uci[uci_id]
        else:
            canonical[uci_id] = choose_name(candidates)
    return canonical


def apply_updates(conn: sqlite3.Connection, canonical: dict[str, str]):
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rider_canonical_names (
          uci_id TEXT PRIMARY KEY,
          canonical_name TEXT NOT NULL,
          source TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )

    mapping_rows = [(uci_id, name, "normalize_rider_names.py", now) for uci_id, name in canonical.items() if name]
    conn.executemany(
        """
        INSERT INTO rider_canonical_names (uci_id, canonical_name, source, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(uci_id) DO UPDATE SET
          canonical_name=excluded.canonical_name,
          source=excluded.source,
          updated_at=excluded.updated_at
        """,
        mapping_rows,
    )

    # Update picks names directly by uci_id.
    pick_updates = [(name, uci_id, name) for uci_id, name in canonical.items() if name]
    before = conn.total_changes
    conn.executemany(
        "UPDATE picks SET name=? WHERE uci_id=? AND COALESCE(TRIM(name),'')<>?",
        pick_updates,
    )
    picks_changed = conn.total_changes - before

    # Update training_times via (event_id,bib,nation) lookup from picks.
    key_to_uci = {}
    for event_id, bib, nation, uci_id in conn.execute(
        """
        SELECT DISTINCT event_id, bib, UPPER(TRIM(COALESCE(nation,''))) AS nation_u, uci_id
        FROM picks
        WHERE COALESCE(TRIM(uci_id), '') <> ''
          AND bib IS NOT NULL
        """
    ).fetchall():
        if not is_valid_uci_id(uci_id):
            continue
        key = (str(event_id), int(bib), str(nation or "").upper())
        if key not in key_to_uci:
            key_to_uci[key] = str(uci_id).strip()

    train_updates = []
    for rowid, event_id, bib, nation, name in conn.execute(
        "SELECT rowid, event_id, bib, UPPER(TRIM(COALESCE(nation,''))), name FROM training_times"
    ).fetchall():
        if bib is None:
            continue
        key = (str(event_id), int(bib), str(nation or "").upper())
        uci_id = key_to_uci.get(key)
        if not uci_id:
            continue
        canon = canonical.get(uci_id)
        if canon and clean_spaces(name or "") != canon:
            train_updates.append((canon, rowid))
    before = conn.total_changes
    if train_updates:
        conn.executemany("UPDATE training_times SET name=? WHERE rowid=?", train_updates)
    train_changed = conn.total_changes - before

    return picks_changed, train_changed, len(mapping_rows)


def preview(conn: sqlite3.Connection, canonical: dict[str, str], limit: int = 20):
    rows = conn.execute(
        """
        SELECT uci_id, COUNT(DISTINCT name) variants
        FROM picks
        WHERE COALESCE(TRIM(uci_id), '') <> ''
        GROUP BY uci_id
        HAVING COUNT(DISTINCT name) > 1
        ORDER BY variants DESC
        """
    ).fetchall()
    print(f"UCI IDs with >1 name variant in picks: {len(rows)}")
    shown = 0
    for uci_id, variants in rows:
        uci_id = str(uci_id).strip()
        if not is_valid_uci_id(uci_id):
            continue
        names = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT name FROM picks WHERE uci_id=? AND COALESCE(TRIM(name),'')<>'' ORDER BY name",
                (uci_id,),
            ).fetchall()
        ]
        print(f"- {uci_id} variants={variants} -> canonical='{canonical.get(uci_id, '')}'")
        print(f"  names: {', '.join(names[:6])}")
        shown += 1
        if shown >= limit:
            break


def main():
    ap = argparse.ArgumentParser(description="Normalize rider names by UCI ID in bmx.db.")
    ap.add_argument("--db", default="bmx.db")
    ap.add_argument("--apply", action="store_true", help="Apply updates. Without this flag, dry-run preview only.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        canonical = build_canonical_map(conn)
        print(f"Canonical map entries: {len(canonical)}")
        preview(conn, canonical, limit=25)
        if args.apply:
            picks_changed, train_changed, map_rows = apply_updates(conn, canonical)
            conn.commit()
            print(f"\nApplied.")
            print(f"Mapping rows upserted: {map_rows}")
            print(f"Rows updated in picks: {picks_changed}")
            print(f"Rows updated in training_times: {train_changed}")
        else:
            print("\nDry-run only. Re-run with --apply to write updates.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
