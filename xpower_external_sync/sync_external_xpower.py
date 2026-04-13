from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BASE_DIR = APP_DIR / "xpower_external_sync"
DEFAULT_ENV_PATH = DEFAULT_BASE_DIR / ".env"
DEFAULT_DB_PATH = DEFAULT_BASE_DIR / "external_xpower.db"
DEFAULT_FILE_DIR = DEFAULT_BASE_DIR / "files"
DEFAULT_QUERY = """
SELECT
  t.*,
  u."username" AS user_name,
  l."location_name" AS location_name,
  y."training_typ" AS training_name
FROM
  training_service.training t
  LEFT JOIN user_service.users u ON t."user_id" = u."id"
  LEFT JOIN training_service.location l ON t."location" = l."id"
  LEFT JOIN training_service.training_typ y ON t."training_typ" = y."id"
WHERE
  t.deleted = FALSE;
""".strip()
S3_KEY_CANDIDATES = (
    "s3_key",
    "object_key",
    "storage_key",
    "file_key",
    "file_path",
    "force_data_path",
    "path",
    "file_url",
    "url",
)
ID_CANDIDATES = ("id", "training_id", "external_training_id")
USER_ID_CANDIDATES = ("user_id", "external_user_id")
RECORDED_AT_CANDIDATES = ("recorded_at", "training_date", "created_at", "updated_at")


@dataclass(frozen=True)
class SyncConfig:
    env_path: Path
    sqlite_path: Path
    file_dir: Path
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    pg_sslmode: str
    pg_query: str
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_prefix: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_force_path_style: bool
    s3_signature_version: str
    s3_object_key_field: str
    archive_db_path: Path
    archive_data_dir: Path
    cli_import_path: Path
    cli_python_path: Path


class ConfigError(RuntimeError):
    pass


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name)
    return Path(raw).expanduser() if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def load_config(env_path: Optional[Path] = None) -> SyncConfig:
    env_file = (env_path or DEFAULT_ENV_PATH).expanduser()
    _load_dotenv(env_file)
    try:
        port = int(_env("XPOWER_EXTSYNC_PG_PORT", "5432"))
    except ValueError as exc:
        raise ConfigError("XPOWER_EXTSYNC_PG_PORT must be an integer") from exc
    return SyncConfig(
        env_path=env_file,
        sqlite_path=_env_path("XPOWER_EXTSYNC_DB_PATH", DEFAULT_DB_PATH),
        file_dir=_env_path("XPOWER_EXTSYNC_FILE_DIR", DEFAULT_FILE_DIR),
        pg_host=_env("XPOWER_EXTSYNC_PG_HOST"),
        pg_port=port,
        pg_db=_env("XPOWER_EXTSYNC_PG_DB", "postgres"),
        pg_user=_env("XPOWER_EXTSYNC_PG_USER"),
        pg_password=_env("XPOWER_EXTSYNC_PG_PASSWORD"),
        pg_sslmode=_env("XPOWER_EXTSYNC_PG_SSLMODE", "require"),
        pg_query=_env("XPOWER_EXTSYNC_PG_QUERY", DEFAULT_QUERY),
        s3_endpoint=_env("XPOWER_EXTSYNC_S3_ENDPOINT", "https://s3.swiss-backup02.infomaniak.com"),
        s3_region=_env("XPOWER_EXTSYNC_S3_REGION", "RegionOne"),
        s3_bucket=_env("XPOWER_EXTSYNC_S3_BUCKET", "default"),
        s3_prefix=_env("XPOWER_EXTSYNC_S3_PREFIX"),
        s3_access_key_id=_env("XPOWER_EXTSYNC_S3_ACCESS_KEY_ID"),
        s3_secret_access_key=_env("XPOWER_EXTSYNC_S3_SECRET_ACCESS_KEY"),
        s3_force_path_style=_env_bool("XPOWER_EXTSYNC_S3_FORCE_PATH_STYLE", True),
        s3_signature_version=_env("XPOWER_EXTSYNC_S3_SIGNATURE_VERSION", "s3v4"),
        s3_object_key_field=_env("XPOWER_EXTSYNC_S3_OBJECT_KEY_FIELD"),
        archive_db_path=_env_path("XPOWER_ARCHIVE_DB_PATH", Path("/opt/bmx/xpower-data/dev/xpower_archive.sqlite3")),
        archive_data_dir=_env_path("XPOWER_DATA_DIR", Path("/opt/bmx/xpower-data/dev")),
        cli_import_path=_env_path("XPOWER_CLI_IMPORT_PATH", Path("/opt/bmx/bmx-xpower-lab/xpower_cli_import.py")),
        cli_python_path=_env_path("XPOWER_CLI_PYTHON_PATH", Path("/opt/bmx/bmx-xpower-lab/.venv/bin/python3")),
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_local_storage(config: SyncConfig) -> None:
    config.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    config.file_dir.mkdir(parents=True, exist_ok=True)


def connect_local_db(config: SyncConfig) -> sqlite3.Connection:
    ensure_local_storage(config)
    conn = sqlite3.connect(config.sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_training (
            external_training_id TEXT PRIMARY KEY,
            external_user_id TEXT,
            external_username TEXT,
            location_name TEXT,
            training_name TEXT,
            recorded_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            object_key TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_synced_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_training_files (
            external_training_id TEXT PRIMARY KEY,
            bucket TEXT,
            object_key TEXT,
            local_path TEXT,
            sha256 TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            downloaded_at TEXT,
            archive_import_status TEXT,
            archive_storage_key TEXT,
            archive_imported_at TEXT,
            archive_import_error TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(external_training_id) REFERENCES external_training(external_training_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_user_map (
            external_user_id TEXT PRIMARY KEY,
            external_username TEXT,
            athlete_name TEXT,
            website_user_email TEXT,
            mapping_status TEXT NOT NULL DEFAULT 'unmapped',
            notes TEXT,
            verified_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_training_user_id ON external_training(external_user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_training_recorded_at ON external_training(recorded_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_training_files_status ON external_training_files(status)")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(external_training_files)").fetchall()}
    for sql in (
        "ALTER TABLE external_training_files ADD COLUMN archive_import_status TEXT",
        "ALTER TABLE external_training_files ADD COLUMN archive_storage_key TEXT",
        "ALTER TABLE external_training_files ADD COLUMN archive_imported_at TEXT",
        "ALTER TABLE external_training_files ADD COLUMN archive_import_error TEXT",
    ):
        col = sql.split()[-2]
        if col not in cols:
            conn.execute(sql)
    conn.commit()
    return conn


def require_db_config(config: SyncConfig) -> None:
    missing = [
        name
        for name, value in {
            "XPOWER_EXTSYNC_PG_HOST": config.pg_host,
            "XPOWER_EXTSYNC_PG_USER": config.pg_user,
            "XPOWER_EXTSYNC_PG_PASSWORD": config.pg_password,
        }.items()
        if not value
    ]
    if missing:
        raise ConfigError("Missing Postgres settings: " + ", ".join(missing))


def require_s3_config(config: SyncConfig) -> None:
    missing = [
        name
        for name, value in {
            "XPOWER_EXTSYNC_S3_ENDPOINT": config.s3_endpoint,
            "XPOWER_EXTSYNC_S3_REGION": config.s3_region,
            "XPOWER_EXTSYNC_S3_BUCKET": config.s3_bucket,
            "XPOWER_EXTSYNC_S3_ACCESS_KEY_ID": config.s3_access_key_id,
            "XPOWER_EXTSYNC_S3_SECRET_ACCESS_KEY": config.s3_secret_access_key,
        }.items()
        if not value
    ]
    if missing:
        raise ConfigError("Missing S3 settings: " + ", ".join(missing))


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is not installed. Run `pip install -r /Users/davidgraf/BMX_WorldCup_Analyse/xpower_external_sync/requirements.txt`."
        ) from exc
    return psycopg, dict_row


def _import_boto3():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is not installed. Run `pip install -r /Users/davidgraf/BMX_WorldCup_Analyse/xpower_external_sync/requirements.txt`."
        ) from exc
    return boto3


def postgres_conninfo(config: SyncConfig) -> str:
    return (
        f"host={config.pg_host} port={config.pg_port} dbname={config.pg_db} "
        f"user={config.pg_user} password={config.pg_password} sslmode={config.pg_sslmode}"
    )


def fetch_query_rows(config: SyncConfig, query: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    require_db_config(config)
    psycopg, dict_row = _import_psycopg()
    sql = (query or config.pg_query).strip().rstrip(";")
    if limit:
        sql = f"SELECT * FROM ({sql}) q LIMIT {int(limit)}"
    with psycopg.connect(postgres_conninfo(config), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())


def fetch_training_columns(config: SyncConfig) -> List[Dict[str, Any]]:
    query = """
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'training_service'
      AND table_name = 'training'
    ORDER BY ordinal_position
    """.strip()
    return fetch_query_rows(config, query=query)


def build_s3_client(config: SyncConfig):
    require_s3_config(config)
    boto3 = _import_boto3()
    session = boto3.session.Session(
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        region_name=config.s3_region,
    )
    return session.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        config=boto3.session.Config(
            signature_version=config.s3_signature_version or "s3v4",
            s3={"addressing_style": "path" if config.s3_force_path_style else "auto"},
        ),
    )


def candidate_s3_endpoints(endpoint: str) -> List[str]:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return []
    candidates = [endpoint]
    if ".storage.supabase.co/" in endpoint:
        candidates.append(endpoint.replace(".storage.supabase.co/", ".supabase.co/", 1))
    elif ".supabase.co/" in endpoint and "/storage/v1/s3" in endpoint:
        candidates.append(endpoint.replace(".supabase.co/", ".storage.supabase.co/", 1))
    deduped: List[str] = []
    seen = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def call_s3_with_fallback(config: SyncConfig, operation_name: str, *args, **kwargs):
    last_exc: Optional[Exception] = None
    original_endpoint = config.s3_endpoint
    for endpoint in candidate_s3_endpoints(original_endpoint):
        endpoint_config = replace(config, s3_endpoint=endpoint)
        client = build_s3_client(endpoint_config)
        try:
            operation = getattr(client, operation_name)
            return operation(*args, **kwargs)
        except Exception as exc:
            message = str(exc)
            if "SignatureDoesNotMatch" in message and endpoint != candidate_s3_endpoints(original_endpoint)[-1]:
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"S3 operation {operation_name} could not be executed.")


def list_s3_objects(config: SyncConfig, limit: int = 20) -> List[Dict[str, Any]]:
    resp = call_s3_with_fallback(
        config,
        "list_objects_v2",
        Bucket=config.s3_bucket,
        Prefix=config.s3_prefix or "",
        MaxKeys=max(1, int(limit)),
    )
    return resp.get("Contents", []) or []


def coerce_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): coerce_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [coerce_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_field_name(name: str) -> str:
    chars = []
    for ch in str(name):
        if ch.isalnum():
            chars.append(ch.lower())
        else:
            chars.append('_')
    normalized = ''.join(chars)
    while '__' in normalized:
        normalized = normalized.replace('__', '_')
    return normalized.strip('_')


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in row.items():
        normalized[str(key)] = coerce_jsonable(value)
        normalized.setdefault(normalize_field_name(str(key)), coerce_jsonable(value))
    return normalized


def first_present(row: Dict[str, Any], candidates: Sequence[str]) -> Optional[str]:
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return str(row[key])
        normalized_key = normalize_field_name(key)
        if normalized_key in row and row[normalized_key] not in (None, ""):
            return str(row[normalized_key])
    return None


def infer_object_key(row: Dict[str, Any], config: SyncConfig) -> Optional[str]:
    field = config.s3_object_key_field.strip()
    if field and row.get(field):
        return extract_object_key(str(row[field]), config.s3_bucket)
    for key in S3_KEY_CANDIDATES:
        value = row.get(key)
        if value:
            derived = extract_object_key(str(value), config.s3_bucket)
            if derived:
                return derived
    return None


def extract_object_key(raw_value: str, bucket: str) -> Optional[str]:
    value = (raw_value or "").strip()
    if not value:
        return None
    if value.startswith("s3://"):
        without_scheme = value[5:]
        parts = without_scheme.split("/", 1)
        if len(parts) == 2:
            return parts[1]
        return None
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        path = parsed.path.lstrip("/")
        if path.startswith(bucket + "/"):
            return path[len(bucket) + 1 :]
        return path or None
    if bucket and value.startswith(bucket + "/"):
        return value[len(bucket) + 1 :]
    return value.lstrip("/")


def upsert_training_rows(config: SyncConfig, rows: Sequence[Dict[str, Any]], dry_run: bool = False) -> Dict[str, int]:
    now = now_iso()
    stats = {"seen": 0, "with_object_key": 0, "upserted": 0}
    conn = connect_local_db(config)
    try:
        for source_row in rows:
            row = normalize_row(source_row)
            stats["seen"] += 1
            external_training_id = first_present(row, ID_CANDIDATES)
            if not external_training_id:
                continue
            external_user_id = first_present(row, USER_ID_CANDIDATES)
            external_username = first_present(row, ("user_name", "username", "User name"))
            location_name = first_present(row, ("location_name", "Location name"))
            training_name = first_present(row, ("training_name", "training_typ", "Training name", "Training typ"))
            object_key = infer_object_key(row, config)
            if object_key:
                stats["with_object_key"] += 1
            deleted_value = row.get("deleted", False)
            deleted = 1 if str(deleted_value).lower() in {"1", "true", "t", "yes"} else 0
            recorded_at = first_present(row, RECORDED_AT_CANDIDATES + ("Created at",))
            created_at = first_present(row, ("created_at",))
            updated_at = first_present(row, ("updated_at",))
            if dry_run:
                stats["upserted"] += 1
                continue
            existing = conn.execute(
                "SELECT first_seen_at FROM external_training WHERE external_training_id = ?",
                (external_training_id,),
            ).fetchone()
            first_seen_at = existing["first_seen_at"] if existing else now
            conn.execute(
                """
                INSERT INTO external_training (
                    external_training_id, external_user_id, external_username, location_name, training_name,
                    recorded_at, created_at, updated_at, object_key, deleted, raw_json, first_seen_at, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(external_training_id) DO UPDATE SET
                    external_user_id = excluded.external_user_id,
                    external_username = excluded.external_username,
                    location_name = excluded.location_name,
                    training_name = excluded.training_name,
                    recorded_at = excluded.recorded_at,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    object_key = excluded.object_key,
                    deleted = excluded.deleted,
                    raw_json = excluded.raw_json,
                    last_synced_at = excluded.last_synced_at
                """,
                (
                    external_training_id,
                    external_user_id,
                    external_username,
                    location_name,
                    training_name,
                    recorded_at,
                    created_at,
                    updated_at,
                    object_key,
                    deleted,
                    json.dumps(row, ensure_ascii=True, sort_keys=True),
                    first_seen_at,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO external_user_map (external_user_id, external_username, mapping_status)
                VALUES (?, ?, 'unmapped')
                ON CONFLICT(external_user_id) DO UPDATE SET
                    external_username = COALESCE(excluded.external_username, external_user_map.external_username)
                """,
                (external_user_id, external_username),
            )
            conn.execute(
                """
                INSERT INTO external_training_files (external_training_id, bucket, object_key, status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(external_training_id) DO UPDATE SET
                    bucket = excluded.bucket,
                    object_key = excluded.object_key,
                    updated_at = excluded.updated_at,
                    status = CASE
                        WHEN external_training_files.status = 'downloaded' THEN external_training_files.status
                        ELSE excluded.status
                    END
                """,
                (
                    external_training_id,
                    config.s3_bucket or None,
                    object_key,
                    "pending" if object_key else "missing_key",
                    now,
                ),
            )
            stats["upserted"] += 1
        if not dry_run:
            conn.execute(
                "INSERT INTO external_sync_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                ("last_meta_sync_at", now, now),
            )
            conn.commit()
    finally:
        conn.close()
    return stats


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_missing_files(config: SyncConfig, limit: Optional[int] = None, dry_run: bool = False) -> Dict[str, int]:
    conn = connect_local_db(config)
    stats = {"pending": 0, "downloaded": 0, "errors": 0}
    query = (
        "SELECT external_training_id, object_key FROM external_training_files "
        "WHERE object_key IS NOT NULL AND status != 'downloaded' ORDER BY external_training_id ASC"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    now = now_iso()
    try:
        for row in rows:
            stats["pending"] += 1
            training_id = str(row["external_training_id"])
            object_key = str(row["object_key"])
            safe_name = object_key.replace("/", "__")
            local_path = config.file_dir / safe_name
            if dry_run:
                stats["downloaded"] += 1
                continue
            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                call_s3_with_fallback(config, "download_file", config.s3_bucket, object_key, str(local_path))
                sha = sha256_file(local_path)
                conn.execute(
                    """
                    UPDATE external_training_files
                    SET local_path = ?, sha256 = ?, status = 'downloaded', error = NULL, downloaded_at = ?, updated_at = ?
                    WHERE external_training_id = ?
                    """,
                    (str(local_path), sha, now, now, training_id),
                )
                stats["downloaded"] += 1
            except Exception as exc:
                conn.execute(
                    "UPDATE external_training_files SET status = 'download_failed', error = ?, updated_at = ? WHERE external_training_id = ?",
                    (str(exc)[:4000], now, training_id),
                )
                stats["errors"] += 1
        if not dry_run:
            conn.execute(
                "INSERT INTO external_sync_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                ("last_file_sync_at", now, now),
            )
            conn.commit()
    finally:
        conn.close()
    return stats


def print_json(data: Any) -> None:
    print(json.dumps(coerce_jsonable(data), indent=2, ensure_ascii=True))


def parse_json_from_mixed_output(output: str) -> Dict[str, Any]:
    text = (output or "").strip()
    if not text:
        raise ValueError("Empty output")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    brace_positions = [idx for idx, ch in enumerate(text) if ch == "{"] 
    for start in reversed(brace_positions):
        snippet = text[start:]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise ValueError("No JSON object found in command output")


def format_external_recorded_parts(recorded_at_raw: str) -> Tuple[str, str]:
    raw = str(recorded_at_raw or "").strip()
    if not raw:
        return "", ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%H:%M:%S")


def infer_processing_trial_type(training_name: str) -> str:
    name = str(training_name or "").strip().lower()
    if any(token in name for token in ("box sprint", "flat sprint", "sprint")):
        return "FS"
    if any(token in name for token in ("hill", "gate", "sx", "start", "5m", "small")):
        return "GS"
    return "GS"


def import_downloaded_files_into_archive(config: SyncConfig, limit: Optional[int] = None, dry_run: bool = False) -> Dict[str, int]:
    conn = connect_local_db(config)
    stats = {"pending": 0, "imported": 0, "errors": 0}
    query = """
        SELECT
            t.external_training_id,
            t.external_username,
            t.training_name,
            t.location_name,
            t.recorded_at,
            f.object_key,
            f.local_path,
            f.archive_import_status
        FROM external_training t
        JOIN external_training_files f ON f.external_training_id = t.external_training_id
        WHERE f.local_path IS NOT NULL
          AND f.status = 'downloaded'
          AND COALESCE(f.archive_import_status, '') != 'imported'
        ORDER BY COALESCE(t.recorded_at, t.created_at, t.updated_at) DESC, t.external_training_id DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    now = now_iso()
    env = os.environ.copy()
    env["XPOWER_ARCHIVE_DB_PATH"] = str(config.archive_db_path)
    env["XPOWER_DATA_DIR"] = str(config.archive_data_dir)
    env["PYTHONWARNINGS"] = "ignore"
    try:
        for row in rows:
            stats["pending"] += 1
            training_id = str(row["external_training_id"])
            local_path = Path(str(row["local_path"]))
            object_key = str(row["object_key"] or "")
            source_name = Path(object_key).name if object_key else local_path.name
            athlete = str(row["external_username"] or "").strip() or "external"
            training_name = str(row["training_name"] or "").strip()
            location_name = str(row["location_name"] or "").strip()
            trial_date, trial_time = format_external_recorded_parts(str(row["recorded_at"] or ""))
            uid = f"{athlete}_{trial_date}_{trial_time}" if trial_date and trial_time else athlete
            cmd = [
                str(config.cli_python_path),
                str(config.cli_import_path),
                "--csv", str(local_path),
                "--source-path", source_name,
                "--rider", athlete,
                "--trial-type", infer_processing_trial_type(training_name),
                "--archive-trial-type", training_name or infer_processing_trial_type(training_name),
                "--location", location_name,
                "--condition", training_name,
                "--original-filename", source_name,
                "--trial-name", athlete,
                "--json",
            ]
            if trial_date:
                cmd.extend(["--trial-date", trial_date])
            if trial_time:
                cmd.extend(["--trial-time", trial_time])
            if uid:
                cmd.extend(["--uid", uid])
            if dry_run:
                stats["imported"] += 1
                continue
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
                output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
                payload = parse_json_from_mixed_output(output)
                if payload.get("status") != "imported":
                    raise RuntimeError(payload.get("error") or output or "Archive import failed")
                conn.execute(
                    """
                    UPDATE external_training_files
                    SET archive_import_status = 'imported',
                        archive_storage_key = ?,
                        archive_imported_at = ?,
                        archive_import_error = NULL,
                        updated_at = ?
                    WHERE external_training_id = ?
                    """,
                    (payload.get("storage_key", ""), now, now, training_id),
                )
                stats["imported"] += 1
            except Exception as exc:
                conn.execute(
                    """
                    UPDATE external_training_files
                    SET archive_import_status = 'failed',
                        archive_import_error = ?,
                        archive_imported_at = ?,
                        updated_at = ?
                    WHERE external_training_id = ?
                    """,
                    (str(exc)[:4000], now, now, training_id),
                )
                stats["errors"] += 1
        if not dry_run:
            conn.execute(
                "INSERT INTO external_sync_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                ("last_archive_import_at", now, now),
            )
            conn.commit()
    finally:
        conn.close()
    return stats


def command_init_db(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    conn = connect_local_db(config)
    conn.close()
    print(f"Initialized local mirror DB at {config.sqlite_path}")


def command_probe_db(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    payload = {
        "columns": fetch_training_columns(config),
        "sample_rows": fetch_query_rows(config, limit=args.limit),
    }
    print_json(payload)


def command_list_s3(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    objects = list_s3_objects(config, limit=args.limit)
    print_json(objects)


def command_sync_meta(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    rows = fetch_query_rows(config, limit=args.limit)
    stats = upsert_training_rows(config, rows, dry_run=args.dry_run)
    print_json(stats)


def command_download_files(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    stats = download_missing_files(config, limit=args.limit, dry_run=args.dry_run)
    print_json(stats)


def command_show_local(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    conn = connect_local_db(config)
    try:
        rows = conn.execute(
            """
            SELECT t.external_training_id, t.external_username, t.training_name, t.recorded_at,
                   f.object_key, f.status, f.local_path
            FROM external_training t
            LEFT JOIN external_training_files f ON f.external_training_id = t.external_training_id
            ORDER BY COALESCE(t.recorded_at, t.created_at, t.updated_at) DESC, t.external_training_id DESC
            LIMIT ?
            """,
            (max(1, int(args.limit)),),
        ).fetchall()
        print_json([dict(r) for r in rows])
    finally:
        conn.close()


def command_import_archive(args: argparse.Namespace) -> None:
    config = load_config(Path(args.env) if args.env else None)
    stats = import_downloaded_files_into_archive(config, limit=args.limit, dry_run=args.dry_run)
    print_json(stats)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only mirror sync for external xPower metadata (Postgres) and raw files (S3).")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="Path to env file with source credentials.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Create the local mirror SQLite DB and tables.")
    p.set_defaults(func=command_init_db)

    p = sub.add_parser("probe-db", help="Inspect the upstream training schema and sample rows.")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=command_probe_db)

    p = sub.add_parser("list-s3", help="List objects from the upstream S3 bucket.")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=command_list_s3)

    p = sub.add_parser("sync-meta", help="Mirror upstream training metadata into the local SQLite DB.")
    p.add_argument("--limit", type=int, default=0, help="Optional row limit for safe testing.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=command_sync_meta)

    p = sub.add_parser("download-files", help="Download mirrored S3 files for rows that have an object key.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=command_download_files)

    p = sub.add_parser("show-local", help="Show recent mirrored rows from the local SQLite DB.")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=command_show_local)

    p = sub.add_parser("import-archive", help="Import downloaded external files into the xPower archive.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=command_import_archive)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigError as exc:
        parser.error(str(exc))
    except RuntimeError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
