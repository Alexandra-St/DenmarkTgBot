# ODA snapshot feasibility

**Assessment date:** 2026-10-16

## Snapshot endpoint check

The endpoint could not be checked from this environment. A single bounded HTTPS `HEAD` request to `https://oda.ft.dk/odapublish/oda.bak` failed before connecting because `oda.ft.dk` did not resolve (`curl` exit 6). Therefore current availability, authentication response, redirect behavior, `Content-Length`, `Content-Type`, and `Last-Modified` are unverified. No backup body was requested or downloaded.

To check from a normal macOS Terminal in the repository, run:

```sh
python3 tools/oda_snapshot_head.py
```

The helper prompts for credentials without echoing the password, sends only `HEAD`, has a 12-second request timeout, reports only selected headers and redirect origins, follows HTTPS redirects only on `oda.ft.dk`, and never reads a response body. The guide includes access credentials; do not paste them into shell commands, tickets, logs, or this repository. The helper intentionally does not embed them.

## What the official guide establishes

The Folketinget ODA browser guide says the URL serves a full copy of the open-data database, protected by a username/password; the copy is made nightly at about 02:00 and is a backup of the same database used by the Web API. It states compatibility mode 110 (SQL Server 2012); this is the database compatibility level, not confirmation of the SQL Server engine version that created the backup. The guide's update procedure then queries each entity through OData using `opdateringsdato gt <backup time>`.

The guide does not enumerate the backup schema in its backup section. Its claim that this is a full copy of the API database supports the expectation that `Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, and `Sagsstatus` are included, but their presence in the current backup has not been directly verified. The OData metadata and prior bounded API checks confirm those six API entities, not the contents of this inaccessible backup.

## Restore and local processing

`.bak` is a SQL Server backup, not a SQLite file or a portable table archive. Extracting rows requires a compatible SQL Server restore (or a separately verified parser that understands the backup format). A restore requires storage for the backup plus the restored database files; the restored footprint can be larger than the backup. The endpoint size and database footprint are unknown, so disk and memory requirements cannot yet be estimated.

This Mac reports `arm64`. Microsoft's SQL Server Linux containers are supported only on x86-64 Linux; ARM emulation is explicitly unsupported. SQL Server has no native macOS restore engine. SQL Server Developer edition is free for non-production development/testing, but it does not remove the host architecture constraint. Thus a supported, straightforward, zero-cost local restore on this Mac has not been established. A separate x86-64 Windows/Linux machine with sufficient disk, or an official portable export, may make processing practical; neither is verified here. No software was installed and no restore was attempted.

## Update and event completeness

- **Snapshot-only:** restoring successive nightly full copies and comparing all six entity tables by primary key and full row contents can identify additions, modifications, and removals present between snapshots. Reprocessing the same snapshot must be idempotent (snapshot identity plus per-row content hash); events should be emitted only for substantive diffs. This would detect net state changes between snapshots, but cannot recover an item created and removed between nightly snapshots.
- **API-only:** remains unsafe for a full historical scan because strict timestamp filtering has returned its cursor row. ID-only pagination is not a change feed: it cannot identify edits to existing lower-ID rows, and snapshot consistency is undocumented.
- **Hybrid:** the guide's documented snapshot-plus-`opdateringsdato gt` catch-up is not safe until the timestamp filter behavior is fixed or independently shown complete. An overlap and row-hash comparison reduce some risks but do not prove completeness under the observed filter defect.
- **Deletion handling:** snapshot set comparison can identify rows absent from a later full restore only if the backup is confirmed to be a complete, consistent database copy and the relevant tables are not intentionally filtered. Confirm these properties with Folketinget before treating absence as deletion.
- **Notifications:** deduplicate event creation by stable publication/entity identity plus normalized substantive content; persist snapshot/run identity and compare transactionally. Queue idempotency prevents duplicate sends for a known event, but cannot reconstruct intermediate changes omitted between snapshots.

## Options and recommendation

| Option | Assessment |
| --- | --- |
| A. Nightly snapshot | Best potential completeness for historical baseline and net daily diffs, but endpoint access/size, current schema, and a supported restore host are unverified. |
| B. OData API | Simple and already integrated, but timestamp cursor behavior blocks a provably complete historical scan; ID pagination misses updates. |
| C. Snapshot + API | Not safe yet: the guide's catch-up uses the defective timestamp filter. |
| D. Ask Folketinget | Recommended next step. Request a supported change-feed/continuation mechanism or portable daily export, plus confirmation of backup availability, size, schema, SQL Server engine version, compression/encryption, consistency, and deletion semantics. |

No approach is currently demonstrated end-to-end as complete. Keep the Folketinget collector fail-closed and do not import from the backup until its endpoint, restore, six-table contents, and update reconciliation are verified. If Folketinget confirms the backup is a complete nightly snapshot and can provide a portable export or a supported x86-64 restore procedure, snapshot diffs are the simplest zero-recurring-service-cost route. Otherwise request a documented API continuation/change feed before resuming.

## References

- [Folketinget ODA browser guide](https://www.ft.dk/-/media/sites/ft/pdf/dokumenter/aabne-data/oda-browser_brugervejledning.pdf)
- [Microsoft SQL Server Linux installation guidance](https://learn.microsoft.com/sql/linux/install-upgrade/setup)
- [Microsoft SQL Server editions and Developer use](https://learn.microsoft.com/sql/sql-server/editions-and-components-of-sql-server-latest)
- [Microsoft SQL Server backup and restore overview](https://learn.microsoft.com/sql/relational-databases/backup-restore/back-up-and-restore-of-sql-server-databases/)
- [Microsoft SQL Server restore compatibility](https://learn.microsoft.com/sql/relational-databases/backup-restore/restore-database-general-page?view=sql-server-ver17)
