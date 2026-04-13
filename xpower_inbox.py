from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


APP_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_DIR = APP_DIR / "xpower_sync"
DEFAULT_INBOX_DIR = DEFAULT_BASE_DIR / "incoming"
DEFAULT_ARCHIVE_DIR = DEFAULT_BASE_DIR / "archive"
DEFAULT_DB_PATH = DEFAULT_BASE_DIR / "xpower_inbox.db"
DEFAULT_STABILITY_SECONDS = 10

FINAL_STATUSES = {"archived", "duplicate", "forwarded"}


@dataclass(frozen=True)
class InboxConfig:
    inbox_dir: Path
    archive_dir: Path
    db_path: Path
    stability_seconds: int
    forward_command: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_path(name: str, default: Path) -> Path:
    raw = str(os.environ.get(name, "") or "").strip()
    return Path(raw).expanduser() if raw else default


def load_config() -> InboxConfig:
    try:
        stability_seconds = int(os.environ.get("XPOWER_SYNC_STABILITY_SECONDS", DEFAULT_STABILITY_SECONDS))
    except Exception:
        stability_seconds = DEFAULT_STABILITY_SECONDS
    if stability_seconds < 0:
        stability_seconds = DEFAULT_STABILITY_SECONDS
    return InboxConfig(
        inbox_dir=_env_path("XPOWER_SYNC_INBOX_DIR", DEFAULT_INBOX_DIR),
        archive_dir=_env_path("XPOWER_SYNC_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR),
        db_path=_env_path("XPOWER_SYNC_DB_PATH", DEFAULT_DB_PATH),
        stability_seconds=stability_seconds,
        forward_command=str(os.environ.get("XPOWER_INBOX_FORWARD_CMD", "") or "").strip(),
    )


def ensure_storage(config: InboxConfig) -> None:
    config.inbox_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)


def connect_db(config: InboxConfig) -> sqlite3.Connection:
    ensure_storage(config)
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inbox_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            source_name TEXT NOT NULL,
            source_size INTEGER NOT NULL DEFAULT 0,
            source_mtime REAL NOT NULL DEFAULT 0,
            sha256 TEXT,
            archived_path TEXT,
            status TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            processed_at TEXT,
            error TEXT,
            forward_command TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_files_status ON inbox_files(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_files_sha256 ON inbox_files(sha256)")
    conn.commit()
    return conn


def scan_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    summary = {k: 0 for k in ["total", "archived", "duplicate", "forwarded", "forward_failed", "pending"]}
    row = conn.execute("SELECT COUNT(*) AS cnt FROM inbox_files").fetchone()
    summary["total"] = int(row["cnt"] if row else 0)
    for status in ["archived", "duplicate", "forwarded", "forward_failed"]:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM inbox_files WHERE status = ?", (status,)).fetchone()
        summary[status] = int(row["cnt"] if row else 0)
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM inbox_files WHERE status NOT IN ('archived', 'duplicate', 'forwarded', 'forward_failed')"
    ).fetchone()
    summary["pending"] = int(row["cnt"] if row else 0)
    return summary


def recent_rows(conn: sqlite3.Connection, limit: int = 200) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id,
            source_name,
            source_path,
            source_size,
            sha256,
            archived_path,
            status,
            first_seen,
            last_seen,
            processed_at,
            error
        FROM inbox_files
        ORDER BY last_seen DESC, id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]


def _is_csv(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".csv"


def iter_candidate_files(config: InboxConfig) -> List[Path]:
    if not config.inbox_dir.exists():
        return []
    return sorted(path for path in config.inbox_dir.rglob("*") if _is_csv(path))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(path: Path) -> str:
    keep = []
    for ch in path.stem.strip():
        if ch.isalnum():
            keep.append(ch)
        elif ch in {"-", "_"}:
            keep.append(ch)
        else:
            keep.append("_")
    stem = "".join(keep).strip("._-") or "pedal"
    return f"{stem[:80]}{path.suffix.lower()}"


def _archive_path(config: InboxConfig, source_path: Path, sha256: str, seen_at: datetime) -> Path:
    day_dir = config.archive_dir / seen_at.strftime("%Y") / seen_at.strftime("%m") / seen_at.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"{seen_at.strftime('%H%M%S')}_{sha256[:12]}_{_safe_name(source_path)}"


def _fetch_existing_by_source(conn: sqlite3.Connection, source_path: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM inbox_files WHERE source_path = ?", (source_path,)).fetchone()


def _fetch_existing_by_hash(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM inbox_files WHERE sha256 = ? AND archived_path IS NOT NULL ORDER BY id ASC LIMIT 1",
        (sha256,),
    ).fetchone()


def _upsert_row(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    source_name: str,
    source_size: int,
    source_mtime: float,
    sha256: Optional[str],
    archived_path: Optional[str],
    status: str,
    first_seen: str,
    last_seen: str,
    processed_at: Optional[str],
    error: str,
    forward_command: str,
) -> None:
    conn.execute(
        """
        INSERT INTO inbox_files (
            source_path,
            source_name,
            source_size,
            source_mtime,
            sha256,
            archived_path,
            status,
            first_seen,
            last_seen,
            processed_at,
            error,
            forward_command
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            source_name = excluded.source_name,
            source_size = excluded.source_size,
            source_mtime = excluded.source_mtime,
            sha256 = excluded.sha256,
            archived_path = excluded.archived_path,
            status = excluded.status,
            first_seen = inbox_files.first_seen,
            last_seen = excluded.last_seen,
            processed_at = excluded.processed_at,
            error = excluded.error,
            forward_command = excluded.forward_command
        """,
        (
            source_path,
            source_name,
            source_size,
            source_mtime,
            sha256,
            archived_path,
            status,
            first_seen,
            last_seen,
            processed_at,
            error,
            forward_command,
        ),
    )


def _run_forward_command(command_template: str, archived_path: Path, source_path: Path, sha256: str) -> tuple[bool, str]:
    rendered = command_template.format(
        csv_path=str(archived_path),
        source_path=str(source_path),
        sha256=sha256,
    ).strip()
    if not rendered:
        return True, ""
    proc = subprocess.run(shlex.split(rendered), capture_output=True, text=True, check=False)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    output = "\n".join(part for part in [stdout, stderr] if part).strip()
    if proc.returncode == 0:
        return True, output
    message = output or f"Forward command exited with {proc.returncode}"
    return False, message[:4000]


def scan_inbox(config: Optional[InboxConfig] = None) -> Dict[str, Any]:
    config = config or load_config()
    conn = connect_db(config)
    stats = {
        "scanned": 0,
        "new": 0,
        "duplicates": 0,
        "unchanged": 0,
        "unstable": 0,
        "forwarded": 0,
        "forward_failed": 0,
    }
    current_ts = datetime.now(timezone.utc)
    current_iso = current_ts.replace(microsecond=0).isoformat()
    for path in iter_candidate_files(config):
        stats["scanned"] += 1
        source_path = str(path.resolve())
        try:
            file_stat = path.stat()
        except FileNotFoundError:
            continue
        if current_ts.timestamp() - file_stat.st_mtime < config.stability_seconds:
            stats["unstable"] += 1
            continue

        existing = _fetch_existing_by_source(conn, source_path)
        if (
            existing
            and float(existing["source_mtime"] or 0) == float(file_stat.st_mtime)
            and int(existing["source_size"] or 0) == int(file_stat.st_size)
            and str(existing["status"] or "") in FINAL_STATUSES
        ):
            conn.execute(
                "UPDATE inbox_files SET last_seen = ? WHERE source_path = ?",
                (current_iso, source_path),
            )
            stats["unchanged"] += 1
            continue

        sha256 = _file_sha256(path)
        duplicate_of = _fetch_existing_by_hash(conn, sha256)
        archived_path: Optional[Path] = None
        status = "archived"
        processed_at = current_iso
        error = ""

        if existing and existing["archived_path"]:
            archived_path = Path(str(existing["archived_path"]))
            if not archived_path.exists():
                archived_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, archived_path)
            if config.forward_command:
                ok, output = _run_forward_command(config.forward_command, archived_path, path, sha256)
                if ok:
                    status = "forwarded"
                    stats["forwarded"] += 1
                else:
                    status = "forward_failed"
                    error = output
                    stats["forward_failed"] += 1
            else:
                status = str(existing["status"] or "archived") or "archived"
        elif duplicate_of and duplicate_of["archived_path"]:
            archived_path = Path(str(duplicate_of["archived_path"]))
            status = "duplicate"
            stats["duplicates"] += 1
        else:
            archived_path = _archive_path(config, path, sha256, current_ts)
            shutil.copy2(path, archived_path)
            stats["new"] += 1
            if config.forward_command:
                ok, output = _run_forward_command(config.forward_command, archived_path, path, sha256)
                if ok:
                    status = "forwarded"
                    stats["forwarded"] += 1
                else:
                    status = "forward_failed"
                    error = output
                    stats["forward_failed"] += 1

        first_seen = str(existing["first_seen"]) if existing and existing["first_seen"] else current_iso
        _upsert_row(
            conn,
            source_path=source_path,
            source_name=path.name,
            source_size=int(file_stat.st_size),
            source_mtime=float(file_stat.st_mtime),
            sha256=sha256,
            archived_path=str(archived_path) if archived_path else None,
            status=status,
            first_seen=first_seen,
            last_seen=current_iso,
            processed_at=processed_at,
            error=error,
            forward_command=config.forward_command,
        )
    conn.commit()
    summary = scan_summary(conn)
    conn.close()
    return {"stats": stats, "summary": summary, "config": config}


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scan xPower FolderSync inbox and archive new CSV files.")
    parser.add_argument("--scan", action="store_true", help="Run a scan and print the result")
    parser.add_argument("--json", action="store_true", help="Print result as JSON")
    args = parser.parse_args(argv)

    config = load_config()
    connect_db(config).close()
    if not args.scan:
        parser.print_help()
        return 0
    result = scan_inbox(config)
    if args.json:
        printable = {
            "stats": result["stats"],
            "summary": result["summary"],
            "config": {
                "inbox_dir": str(config.inbox_dir),
                "archive_dir": str(config.archive_dir),
                "db_path": str(config.db_path),
                "stability_seconds": config.stability_seconds,
                "forward_command": config.forward_command,
            },
        }
        print(json.dumps(printable, indent=2, sort_keys=True))
        return 0
    print(
        "scan={scanned} new={new} duplicates={duplicates} unchanged={unchanged} unstable={unstable} "
        "forwarded={forwarded} forward_failed={forward_failed}".format(**result["stats"])
    )
    print(
        "summary total={total} archived={archived} duplicate={duplicate} forwarded={forwarded} "
        "forward_failed={forward_failed} pending={pending}".format(**result["summary"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
