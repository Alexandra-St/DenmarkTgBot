# Folketinget backup restore PoC

This is a **manual-only, non-production** proof of concept. It has no schedule and does not call the DenmarkBot Worker, D1, Telegram, or ingestion APIs. It does not create a Folketinget baseline.

## Prerequisites

Add these repository Actions secrets before a run:

- `ODA_BACKUP_USERNAME`
- `ODA_BACKUP_PASSWORD`

They are HTTP Basic Authentication credentials for the official ODA backup endpoint. The workflow fails closed if either is absent. Do not put credentials in variables, workflow inputs, files, or logs.

The restore job uses a GitHub-hosted x64 `ubuntu-24.04` runner and installs **SQL Server 2025 Express only**. It stops if Express cannot be installed or verified; it does not fall back to Developer, Evaluation, or another edition.

## What a manual run does

In **Actions → Folketinget backup restore PoC → Run workflow**, the operator must explicitly enable the confirmation checkbox. The job then checks disk space, requests the backup size, downloads the official HTTPS backup to temporary runner storage, and reports only byte size, SHA-256 and download duration. It runs `RESTORE HEADERONLY`, `RESTORE FILELISTONLY`, restores to a temporary database, runs `DBCC CHECKDB`, and reports presence, row counts, primary-key columns, and declared foreign-key relationships for `Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, and `Sagsstatus`.

It does not print table contents, record IDs, credentials, or the authenticated URL. In `CREATE_MANIFEST_BASELINE` mode, it uploads only the validated `manifest.sqlite` for two days; backup and SQL Server database files are not uploaded. The temporary database and backup files are removed in cleanup; the runner is ephemeral and is discarded after the job.

## Comparing two manifest artifacts

The workflow remains `workflow_dispatch` only. Select `DIFF_MANIFESTS`, confirm the manual operation, and enter two distinct run IDs from successful manual `CREATE_MANIFEST_BASELINE` runs. The workflow checks that both runs completed successfully from this workflow and that each has exactly one unexpired manifest artifact before downloading it. The extracted artifact directory must contain only `manifest.sqlite`.

The diff tool checks SQLite integrity, the exact version 1 manifest table/column and key layout, entity counts, required entities, and matching schema fingerprints. It aggregates changes using SQLite joins and emits a machine-readable JSON report with aggregate counts and snapshot metadata only. It never prints record keys, individual hashes, or row contents. A fingerprint mismatch or invalid artifact stops comparison.

For local manifests, run:

```sh
python3 tools/folketinget_snapshot_diff.py --old /path/to/older/manifest.sqlite --new /path/to/newer/manifest.sqlite
```

No restore, source request, ingestion, Worker/D1 call, or notification occurs in `DIFF_MANIFESTS` mode.

## Limits and risks before authorizing a run

- The backup has not yet been downloaded or restored by this PoC. Its SQL Server source version, actual database size, restore duration, table layout, and entity completeness remain unverified.
- Express has a 50 GB relational database size ceiling. The job checks backup file-list sizes against that ceiling and checks available disk before download and restore. The standard runner has finite disk, so a guard may stop the run before restore.
- SQL Server package installation and restore may exceed the three-hour job limit or fail because of upstream repository availability. Failures are reported by safe category; no alternate SQL edition is selected.
- A successful restore proves the snapshot can be examined in this runner. It does not prove that OData pagination is reliable, that consecutive snapshots can be processed incrementally, or that a Folketinget import is safe.
- No workflow run is authorized by this preparation. Review the secrets and repo visibility/Actions settings before the first manual launch. Do not authorize ingestion or deployment based solely on this PoC.
