#!/usr/bin/env python3
"""
run_poller.py

Generic background poller runner for systemd service instances.

Environment variables:
  POLLER_KIND: sqorz | jstiming | chronorace
  POLL_INTERVAL: seconds (default: 15)
  DB_PATH: sqlite path (default: bmx.db)

sqorz:
  EVENT_URL (required)
  EVENT_ID (required)
  SERIES (optional: FFC | USABMX | SCC | Other)
  SERIES_CODE (optional explicit code; overrides SERIES)
  CLASS_FILTERS (optional, newline/comma/semicolon separated)
  ALL_CLASSES=1 (optional)

jstiming:
  RACE_URLS (optional, newline/comma/semicolon separated)
  TRAINING_URLS (optional, newline/comma/semicolon separated)
  VERBOSE=1 (optional)

chronorace:
  EVENTS (required, separated by newline/comma/semicolon)
  WORKERS (optional, default 6)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from typing import List


REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_BIN = os.path.join(REPO_DIR, ".venv", "bin", "python")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def split_values(raw: str) -> List[str]:
    if not raw:
        return []
    vals: List[str] = []
    for chunk in raw.replace(";", "\n").replace(",", "\n").splitlines():
        s = chunk.strip()
        if s:
            vals.append(s)
    return vals


def int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.getenv(name, default)).strip()))
    except Exception:
        return default


def build_cmd() -> List[str]:
    kind = str(os.getenv("POLLER_KIND", "")).strip().lower()
    db_path = str(os.getenv("DB_PATH", "bmx.db")).strip() or "bmx.db"

    if kind == "sqorz":
        event_url = str(os.getenv("EVENT_URL", "")).strip()
        event_id = str(os.getenv("EVENT_ID", "")).strip()
        if not event_url or not event_id:
            raise RuntimeError("sqorz requires EVENT_URL and EVENT_ID")
        cmd = [
            PYTHON_BIN,
            os.path.join(REPO_DIR, "ingest_sqorz.py"),
            "--url",
            event_url,
            "--event-id",
            event_id,
            "--db",
            db_path,
        ]
        series_code = str(os.getenv("SERIES_CODE", "")).strip()
        series_label = str(os.getenv("SERIES", "")).strip()
        if series_code:
            cmd.extend(["--series-code", series_code])
        elif series_label:
            cmd.extend(["--series", series_label])
        if str(os.getenv("ALL_CLASSES", "0")).strip() in {"1", "true", "True"}:
            cmd.append("--all-classes")
        else:
            for c in split_values(str(os.getenv("CLASS_FILTERS", "")).strip()):
                cmd.extend(["--class-contains", c])
        return cmd

    if kind == "jstiming":
        race_urls = split_values(str(os.getenv("RACE_URLS", "")).strip())
        training_urls = split_values(str(os.getenv("TRAINING_URLS", "")).strip())
        if not race_urls and not training_urls:
            raise RuntimeError("jstiming requires at least one RACE_URLS or TRAINING_URLS value")
        cmd = [PYTHON_BIN, os.path.join(REPO_DIR, "ingest_jstiming.py"), "--db", db_path]
        for u in race_urls:
            cmd.extend(["--race", u])
        for u in training_urls:
            cmd.extend(["--training", u])
        if str(os.getenv("VERBOSE", "0")).strip() in {"1", "true", "True"}:
            cmd.append("--verbose")
        return cmd

    if kind == "chronorace":
        events = split_values(str(os.getenv("EVENTS", "")).strip())
        if not events:
            raise RuntimeError("chronorace requires EVENTS")
        workers = int_env("WORKERS", 6)
        cmd = [
            PYTHON_BIN,
            os.path.join(REPO_DIR, "ingest.py"),
            "--events",
            *events,
            "--once",
            "--db",
            db_path,
            "--workers",
            str(workers),
        ]
        return cmd

    raise RuntimeError(f"Unsupported POLLER_KIND='{kind}'")


def main() -> int:
    interval = int_env("POLL_INTERVAL", 15)
    print(f"[{now_iso()}] run_poller starting (interval={interval}s)")
    sys.stdout.flush()

    while True:
        try:
            cmd = build_cmd()
            print(f"[{now_iso()}] exec: {' '.join(cmd)}")
            sys.stdout.flush()
            proc = subprocess.run(cmd, cwd=REPO_DIR, check=False)
            print(f"[{now_iso()}] exit_code={proc.returncode}")
            sys.stdout.flush()
        except Exception as e:
            print(f"[{now_iso()}] poller_error: {e}")
            sys.stdout.flush()
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
