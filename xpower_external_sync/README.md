# External xPower Sync

This directory prepares a read-only sync from the new mobile app backend into a local mirror on your VPS.

Upstream systems:
- Postgres metadata source (Supabase pooler)
- S3 raw file storage (Infomaniak)

Local result:
- SQLite mirror DB with external trainings, object keys, and a user-mapping table
- downloaded raw files in a local cache directory

## What you still need to fill

Only these real secrets and S3 details are still missing:
- `XPOWER_EXTSYNC_PG_PASSWORD`
- `XPOWER_EXTSYNC_S3_ACCESS_KEY_ID`
- `XPOWER_EXTSYNC_S3_SECRET_ACCESS_KEY`
- optionally `XPOWER_EXTSYNC_S3_OBJECT_KEY_FIELD`

## Suggested VPS setup

```bash
sudo mkdir -p /opt/bmx/xpower-sync
sudo cp /opt/BMX_WorldCup_Analyse/xpower_external_sync/.env.example /opt/bmx/xpower-sync/.env
sudo chmod 600 /opt/bmx/xpower-sync/.env
python3 -m venv /opt/bmx/xpower-sync/.venv
source /opt/bmx/xpower-sync/.venv/bin/activate
pip install -r /opt/BMX_WorldCup_Analyse/xpower_external_sync/requirements.txt
```

## Safe first-run sequence

### 1. Create the local mirror DB

```bash
source /opt/bmx/xpower-sync/.venv/bin/activate
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env init-db
```

### 2. Inspect the upstream schema and sample rows

```bash
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env probe-db --limit 5
```

What to look for:
- which `training_service.training` column points to the raw file in S3
- how the time fields are named (`recorded_at`, `created_at`, `updated_at`, ...)
- whether the upstream `id` and `user_id` look stable and complete

From the sample files you shared, the likely mapping is already:
- `ID` -> external training id
- `User ID` -> external user id
- `User name` -> athlete display name
- `Created at` -> recorded/created timestamp
- `Location name` -> location
- `Training name` or `Training typ` -> training label
- `Force data path` -> S3 object key


The config now assumes the usual Infomaniak Swiss Backup defaults:
- endpoint: `https://s3.swiss-backup02.infomaniak.com`
- region: `RegionOne`
- bucket: `default`

If `list-s3` fails with these defaults, then your storage uses a different endpoint or bucket and you will need the owner to confirm those two values.

### 3. Check the S3 access independently

```bash
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env list-s3 --limit 20
```

### 4. Mirror metadata only

```bash
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env sync-meta --limit 20 --dry-run
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env sync-meta
```

### 5. Download files once the object-key column is known

```bash
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env download-files --limit 10 --dry-run
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env download-files
```

### 6. Inspect what the local mirror contains

```bash
python /opt/BMX_WorldCup_Analyse/xpower_external_sync/sync_external_xpower.py --env /opt/bmx/xpower-sync/.env show-local --limit 25
```

## Local mirror tables

The CLI creates these tables in the local SQLite DB:
- `external_training`
- `external_training_files`
- `external_user_map`
- `external_sync_state`

## Recommended production model

- source Postgres = read-only metadata source
- source S3 = raw-file source
- local SQLite/Postgres = your application and analysis truth
- your Streamlit app should read only the local mirror, not the upstream systems directly

## Important security note

Because the S3 access key was already shared in plain text, treat it as exposed and rotate it after the setup is complete.
Use a read-only database account if the upstream team can provide one. If not, keep the current credentials strictly server-side and never inside Streamlit UI code.
