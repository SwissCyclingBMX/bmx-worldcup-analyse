# xPower External Backend Sync Setup

Dieses Setup spiegelt die neue Android-App-Infrastruktur read-only auf deinen VPS:

1. Postgres (`training_service.training` + User/Location/Training-Typ)
2. S3-Storage mit den Rohfiles
3. lokale Mirror-DB fuer deine eigene Analyse-App

## 1. Voraussetzungen

- keine Aenderung an der Original-DB
- alle Secrets liegen nur serverseitig in `/opt/bmx/xpower-sync/.env`
- die Streamlit-App liest spaeter nur die lokale Mirror-DB

## 2. VPS vorbereiten

```bash
sudo mkdir -p /opt/bmx/xpower-sync
sudo cp /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/.env.example /opt/bmx/xpower-sync/.env
sudo chmod 600 /opt/bmx/xpower-sync/.env
python3 -m venv /opt/bmx/xpower-sync/.venv
source /opt/bmx/xpower-sync/.venv/bin/activate
pip install -r /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/requirements.txt
```

## 3. Erst probe, dann sync

```bash
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env init-db
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env probe-db --limit 5
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env list-s3 --limit 20
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env sync-meta --dry-run --limit 20
```

Wenn `probe-db` zeigt, welche Spalte den File-Key enthaelt, trage sie in `XPOWER_EXTSYNC_S3_OBJECT_KEY_FIELD` ein.

## 4. Danach echter Sync

```bash
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env sync-meta
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env download-files
python /opt/bmx/bmx-worldcup-analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env import-archive
```

## 5. Optionaler Timer

```bash
sudo cp /opt/bmx/bmx-worldcup-analyse/deploy/xpower-external-sync.service.example /etc/systemd/system/xpower-external-sync.service
sudo cp /opt/bmx/bmx-worldcup-analyse/deploy/xpower-external-sync.timer.example /etc/systemd/system/xpower-external-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now xpower-external-sync.timer
```

The scaffold already assumes the common Infomaniak Swiss Backup defaults:
- endpoint `https://s3.swiss-backup02.infomaniak.com`
- region `RegionOne`
- bucket `default`

If `list-s3` still fails after entering the two secrets, ask the storage owner for the exact endpoint/bucket shown in Infomaniak Manager.

For Supabase S3 the scaffold now forces Signature V4 explicitly:
- `XPOWER_EXTSYNC_S3_FORCE_PATH_STYLE=true`
- `XPOWER_EXTSYNC_S3_SIGNATURE_VERSION=s3v4`

If `list-s3` still returns `SignatureDoesNotMatch`, test the alternate official endpoint host by replacing:
- `https://<project-ref>.storage.supabase.co/storage/v1/s3`
with
- `https://<project-ref>.supabase.co/storage/v1/s3`
