# Folketinget collector CLI

The read-only CLI scans the six ODA streams independently (`Sag`, `Sagstrin`, `SagDokument`, `Dokument`, `Afstemning`, `Sagsstatus`), hydrates only affected cases, and writes one JSON object to stdout. Diagnostics go to stderr. It does not persist checkpoints, call D1, or send notifications.

```sh
python -m collectors.folketinget_sync --help
python -m collectors.folketinget_sync historical --page-size 100 --max-pages 1000
python -m collectors.folketinget_sync incremental --checkpoints checkpoints.json
```

Incremental input is a UTF-8 JSON object keyed by stream, for example:

```json
{"Sag":{"timestamp":"2026-10-14T08:46:08.6566667","id":123}}
```

Supply the last successfully accepted checkpoint per stream. The collector re-reads a 72-hour overlap before that timestamp and deduplicates by source timestamp and ID. Streams without a checkpoint use the historical window. The default historical lower boundary is 30 days plus a 72-hour allowance because ODA documents `Edm.DateTime` without a verified timezone. This is an approximate window, not a claim about publication or creation date. Historical case output has `historical: true` and `notify_eligible: false`.

Each stream reports `status`, page and record counts, `complete`, and its captured upper bound. The output never exposes committable checkpoints: this collector does not durably persist results, which must happen before a later phase can authorize checkpoint advancement. Any incomplete scan, unresolved relationship, or incomplete hydration makes the overall result incomplete and exits with status 2. The CLI has configurable page size and a hard page cap; the transport preserves partial rows when the cap is reached.

Document and vote updates are reverse-resolved through `SagDokument` and `Sagstrin`; status updates find `Sag` records referencing changed status IDs. Null vote stage references remain unresolved as a nonblocking observation. All IDs are official numeric IDs; no title matching is used. A known ODA cursor precision inconsistency is retried once by restarting that stream from its captured query boundary; only the complete restarted scan is used, and a recovered retry is recorded as a nonblocking diagnostic. If the inconsistency persists, the stream remains incomplete. Raw timestamp precision is retained; the source timezone is not inferred. Historical output uses `historical: true` and `notify_eligible: false`; incremental output leaves notification eligibility `null` until a later classification phase.

## Limits

ODA offers current records, not a guaranteed version log. Intermediate changes can be missed, deletions may be absent from the six streams, and an arbitrarily late backfill older than the overlap/window may not be found. The API provides no documented rate-limit or publication-latency guarantee. `Sag` has no verified creation timestamp, so a 30-day scan cannot identify newly created cases reliably. The collector does not persist data or notifications.
