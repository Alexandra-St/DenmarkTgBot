# Folketinget backup restore PoC

This is a **manual-only, non-production** proof of concept. It has no schedule and does not call the DenmarkBot Worker, D1, Telegram, or ingestion APIs. It does not create a Folketinget baseline.

## Prerequisites

Add these repository Actions secrets before a run:

- `ODA_BACKUP_USERNAME`
- `ODA_BACKUP_PASSWORD`

They are HTTP Basic Authentication credentials for the official ODA backup endpoint. The workflow fails closed if either is absent. Do not put credentials in variables, workflow inputs, files, or logs.

The job uses a GitHub-hosted x64 `ubuntu-latest` runner, checks that it is Ubuntu 24.04, and installs **SQL Server 2025 Express only**. It stops if Express cannot be installed or verified; it does not fall back to Developer, Evaluation, or another edition.

## What a manual run does

In **Actions → Folketinget backup restore PoC → Run workflow**, the operator must explicitly enable the confirmation checkbox. The job then checks disk space, requests the backup size, downloads the official HTTPS backup to temporary runner storage, and reports only byte size, SHA-256 and download duration. It runs `RESTORE HEADERONLY`, `RESTORE FILELISTONLY`, restores to a temporary database, runs `DBCC CHECKDB`, and reports presence, row counts, primary-key columns, and declared foreign-key relationships for `Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, and `Sagsstatus`.

It does not print table contents, record IDs, credentials, or the authenticated URL. It does not upload artifacts. The temporary database and backup files are removed in cleanup; the runner is ephemeral and is discarded after the job.

## Limits and risks before authorizing a run

- The backup has not yet been downloaded or restored by this PoC. Its SQL Server source version, actual database size, restore duration, table layout, and entity completeness remain unverified.
- Express has a 50 GB relational database size ceiling. The job checks backup file-list sizes against that ceiling and checks available disk before download and restore. The standard runner has finite disk, so a guard may stop the run before restore.
- SQL Server package installation and restore may exceed the three-hour job limit or fail because of upstream repository availability. Failures are reported by safe category; no alternate SQL edition is selected.
- A successful restore proves the snapshot can be examined in this runner. It does not prove that OData pagination is reliable, that consecutive snapshots can be processed incrementally, or that a Folketinget import is safe.
- No workflow run is authorized by this preparation. Review the secrets and repo visibility/Actions settings before the first manual launch. Do not authorize ingestion or deployment based solely on this PoC.
